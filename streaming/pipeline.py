"""
Streaming pipeline — wires all detectors together.

Receives parsed OTLP payloads from the receiver and fans them out
to: PII scanner, attribute checker, cardinality tracker, service tracker,
and log tracker (for real-time log error/PII detection).

All processing is synchronous within the receiver request so that
PII findings are logged before the HTTP response is returned —
the gateway's retry queue handles any latency this introduces.
"""

import logging
from typing import Callable

from config import AgentConfig
from .alerts import AlertDispatcher, StreamingAlert
from .observations import ObservationBuffer, Observation
from .pii_scanner import PIIScanner
from .attribute_checker import AttributeChecker
from .cardinality_tracker import CardinalityTracker
from .service_tracker import ServiceTracker
from .log_tracker import LogTracker
from . import profiling_store

logger = logging.getLogger(__name__)


class StreamingPipeline:
    """
    Central fan-out for all streaming detectors.

    Usage:
        pipeline = StreamingPipeline.from_config(config)
        pipeline.service_tracker.on_new_service(provision_callback)
        pipeline.service_tracker.seed(existing_services)

        # In OTLP receiver:
        pipeline.process_resource_spans(resource_spans_list)
        pipeline.process_resource_metrics(resource_metrics_list)
        pipeline.process_resource_logs(resource_logs_list)
    """

    def __init__(
        self,
        dispatcher: AlertDispatcher,
        pii_scanner: PIIScanner,
        attribute_checker: AttributeChecker,
        cardinality_tracker: CardinalityTracker,
        service_tracker: ServiceTracker,
        log_tracker: LogTracker,
    ):
        self.dispatcher = dispatcher
        self.pii_scanner = pii_scanner
        self.attribute_checker = attribute_checker
        self.cardinality_tracker = cardinality_tracker
        self.service_tracker = service_tracker
        self.log_tracker = log_tracker
        self._obs_buffer: ObservationBuffer | None = None

    def set_observation_buffer(self, buf: ObservationBuffer):
        """Attach an ObservationBuffer so detectors write events for batch consumption."""
        self._obs_buffer = buf
        # Patch detector callbacks to also write to the buffer
        self._patch_dispatcher(buf)

    def _patch_dispatcher(self, buf: ObservationBuffer):
        """Wrap AlertDispatcher.fire() to mirror alerts into the observation buffer."""
        original_fire = self.dispatcher.fire

        def fire_and_record(alert: StreamingAlert):
            original_fire(alert)
            obs_type = {
                "pii":              "pii",
                "attribute":        "attribute_gap",
                "cardinality":      "cardinality_spike",
                "new_service":      "new_service",
                "log_tracker":      "attribute_gap",  # log error bursts → attribute_gap bucket
            }.get(alert.detector)
            if obs_type:
                buf.add(Observation(
                    type=obs_type,
                    service=alert.service,
                    detail=alert.detail[:200],
                    severity=alert.severity,
                ))

        self.dispatcher.fire = fire_and_record

    @classmethod
    def from_config(cls, config: AgentConfig) -> "StreamingPipeline":
        dispatcher = AlertDispatcher(
            environment=config.environment,
            cooldown_seconds=config.alert_cooldown_seconds,
            webhook_url=config.alert_webhook_url,
            suppress_patterns=config.alert_suppress_patterns,
        )
        return cls(
            dispatcher=dispatcher,
            pii_scanner=PIIScanner(dispatcher),
            attribute_checker=AttributeChecker(dispatcher),
            cardinality_tracker=CardinalityTracker(dispatcher),
            service_tracker=ServiceTracker(dispatcher),
            log_tracker=LogTracker(dispatcher),
        )

    # ── OTLP/traces ──────────────────────────────────────────────────────────

    def process_resource_spans(self, resource_spans: list[dict]):
        """
        Process a resourceSpans array from an OTLP/HTTP JSON payload.

        OTLP/HTTP JSON shape:
        [
          {
            "resource": {"attributes": [{"key": "...", "value": {"stringValue": "..."}}]},
            "scopeSpans": [
              {"spans": [{"name": "...", "attributes": [...]}]}
            ]
          }
        ]
        """
        for rs in resource_spans:
            resource_attrs = _parse_attributes(
                rs.get("resource", {}).get("attributes", [])
            )
            service = resource_attrs.get("service.name", "unknown")

            self.service_tracker.observe(service, resource_attrs)

            for scope in rs.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    span_name = span.get("name", "unknown")
                    span_attrs = _parse_attributes(span.get("attributes", []))
                    merged = {**resource_attrs, **span_attrs}

                    self.pii_scanner.scan_span(service, span_name, merged)
                    self.attribute_checker.check_span(service, span_name, merged)

    # ── OTLP/metrics ─────────────────────────────────────────────────────────

    def process_resource_metrics(self, resource_metrics: list[dict]):
        """
        Process a resourceMetrics array from an OTLP/HTTP JSON payload.
        """
        for rm in resource_metrics:
            resource_attrs = _parse_attributes(
                rm.get("resource", {}).get("attributes", [])
            )

            for scope in rm.get("scopeMetrics", []):
                for metric in scope.get("metrics", []):
                    metric_name = metric.get("name", "unknown")
                    data_points = _extract_data_points(metric)

                    for dp in data_points:
                        dp_attrs = _parse_attributes(dp.get("attributes", []))
                        merged = {**resource_attrs, **dp_attrs}

                        self.cardinality_tracker.observe_metric(metric_name, merged)
                        self.attribute_checker.check_metric(metric_name, merged)

    # ── OTLP/logs ─────────────────────────────────────────────────────────────

    def process_resource_logs(self, resource_logs: list[dict]):
        """
        Process a resourceLogs array from an OTLP/HTTP JSON payload.

        OTLP/HTTP JSON shape:
        [
          {
            "resource": {"attributes": [...]},
            "scopeLogs": [
              {"logRecords": [{"severityText": "ERROR", "severityNumber": 17,
                               "body": {"stringValue": "..."}, "attributes": [...]}]}
            ]
          }
        ]
        """
        for rl in resource_logs:
            resource_attrs = _parse_attributes(
                rl.get("resource", {}).get("attributes", [])
            )
            service = resource_attrs.get("service.name", "unknown")

            for scope in rl.get("scopeLogs", []):
                for record in scope.get("logRecords", []):
                    severity_text = record.get("severityText", "")
                    severity_number = record.get("severityNumber", 0)
                    body_container = record.get("body", {})
                    body = body_container.get("stringValue", "") or str(body_container)
                    record_attrs = _parse_attributes(record.get("attributes", []))

                    # Route profiling records to the local profiling store
                    if record_attrs.get("com.splunk.sourcetype") == "otel.profiling":
                        data_type = record_attrs.get("profiling.data.type", "cpu")
                        data_format = record_attrs.get("profiling.data.format", "")
                        environment = resource_attrs.get("deployment.environment", "unknown")
                        profiling_store.observe(
                            service=service,
                            environment=environment,
                            data_type=data_type,
                            body=body,
                            data_format=data_format,
                        )
                        continue

                    self.log_tracker.observe_log(
                        service=service,
                        severity_number=severity_number,
                        severity_text=severity_text,
                        body=body[:2000],  # cap body scan length
                        attributes=record_attrs,
                        resource_attrs=resource_attrs,
                    )

    def stats(self) -> dict:
        """Summary stats for the /status endpoint."""
        return {
            "known_services": sorted(self.service_tracker.known_services()),
            "top_cardinality": self.cardinality_tracker.top_metrics(10),
            "log_error_counts": self.log_tracker.error_counts(),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_attributes(attr_list: list[dict]) -> dict:
    """
    Convert OTLP attribute list to a flat dict.

    OTLP attribute format:
      [{"key": "service.name", "value": {"stringValue": "payment"}}, ...]
    """
    result = {}
    for item in attr_list:
        key = item.get("key", "")
        value_container = item.get("value", {})
        # Extract whichever value type is present
        for vtype in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if vtype in value_container:
                result[key] = value_container[vtype]
                break
        else:
            if "arrayValue" in value_container:
                result[key] = value_container["arrayValue"]
            elif "kvlistValue" in value_container:
                result[key] = value_container["kvlistValue"]
    return result


def _extract_data_points(metric: dict) -> list[dict]:
    """Extract data points from any OTLP metric type."""
    for kind in ("gauge", "sum", "histogram", "exponentialHistogram", "summary"):
        if kind in metric:
            return metric[kind].get("dataPoints", [])
    return []
