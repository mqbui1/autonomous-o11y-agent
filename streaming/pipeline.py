"""
Streaming pipeline — wires all four detectors together.

Receives parsed OTLP payloads from the receiver and fans them out
to: PII scanner, attribute checker, cardinality tracker, service tracker.

All processing is synchronous within the receiver request so that
PII findings are logged before the HTTP response is returned —
the gateway's retry queue handles any latency this introduces.
"""

import logging
from typing import Callable

from config import AgentConfig
from .alerts import AlertDispatcher
from .pii_scanner import PIIScanner
from .attribute_checker import AttributeChecker
from .cardinality_tracker import CardinalityTracker
from .service_tracker import ServiceTracker

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
    """

    def __init__(
        self,
        dispatcher: AlertDispatcher,
        pii_scanner: PIIScanner,
        attribute_checker: AttributeChecker,
        cardinality_tracker: CardinalityTracker,
        service_tracker: ServiceTracker,
    ):
        self.dispatcher = dispatcher
        self.pii_scanner = pii_scanner
        self.attribute_checker = attribute_checker
        self.cardinality_tracker = cardinality_tracker
        self.service_tracker = service_tracker

    @classmethod
    def from_config(cls, config: AgentConfig) -> "StreamingPipeline":
        dispatcher = AlertDispatcher(
            environment=config.environment,
            cooldown_seconds=config.alert_cooldown_seconds,
            webhook_url=config.alert_webhook_url,
        )
        return cls(
            dispatcher=dispatcher,
            pii_scanner=PIIScanner(dispatcher),
            attribute_checker=AttributeChecker(dispatcher),
            cardinality_tracker=CardinalityTracker(dispatcher),
            service_tracker=ServiceTracker(dispatcher),
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

    def stats(self) -> dict:
        """Summary stats for the /status endpoint."""
        return {
            "known_services": sorted(self.service_tracker.known_services()),
            "top_cardinality": self.cardinality_tracker.top_metrics(10),
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
