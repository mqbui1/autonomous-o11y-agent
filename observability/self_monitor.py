"""
Agent self-observability — emits OTel spans and metrics for the agent's own operations.

This makes the o11y-agent itself a first-class observable service in Splunk Observability
Cloud. You can build dashboards and detectors on the agent's own health just like any
other service.

Requires opentelemetry-sdk and opentelemetry-exporter-otlp-proto-http.
Falls back gracefully (no-op) if these packages are not installed.

Signals emitted:
  Spans:
    o11y_agent.assessment          — full run (root span)
    o11y_agent.specialist_run      — per-specialist execution
    o11y_agent.synthesis           — synthesis pass

  Metrics (via exemplar-linked histogram):
    o11y_agent.run.duration        — assessment wall time (seconds)
    o11y_agent.issues.found        — issues found per run (by severity)
    o11y_agent.instrumentation_score — current score (gauge)
    o11y_agent.silent_services     — count of silent services
"""

import logging
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import AgentConfig
    from tools.findings import SpecialistFindings

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace, metrics
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.resources import Resource
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


class SelfMonitor:
    """
    Wraps agent operations with OTel instrumentation.

    Usage:
        monitor = SelfMonitor.from_config(config)
        with monitor.assessment_span() as span:
            result = run_assessment(config)
            monitor.record_run_metrics(findings, elapsed_seconds)
    """

    def __init__(self, tracer=None, meter=None, enabled: bool = True):
        self._tracer = tracer
        self._meter = meter
        self._enabled = enabled and _OTEL_AVAILABLE and tracer is not None

        if self._enabled:
            self._run_duration = meter.create_histogram(
                "o11y_agent.run.duration",
                unit="s",
                description="Wall time for a full assessment run",
            )
            self._issues_counter = meter.create_counter(
                "o11y_agent.issues.found",
                description="Issues found per run, by severity",
            )
            self._score_gauge = meter.create_gauge(
                "o11y_agent.instrumentation_score",
                description="Current instrumentation quality score (0-100)",
            )
            self._silent_gauge = meter.create_gauge(
                "o11y_agent.silent_services",
                description="Number of services with no telemetry",
            )

    @classmethod
    def from_config(cls, config: "AgentConfig") -> "SelfMonitor":
        """Create a SelfMonitor wired to the OTel endpoint in config."""
        import os
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        if not endpoint or not _OTEL_AVAILABLE:
            if not _OTEL_AVAILABLE:
                logger.debug(
                    "opentelemetry-sdk not installed — agent self-observability disabled. "
                    "Install with: pip install opentelemetry-sdk "
                    "opentelemetry-exporter-otlp-proto-http"
                )
            return cls(enabled=False)

        resource = Resource.create({
            "service.name": "o11y-agent",
            "service.version": "0.1.0",
            "deployment.environment": config.environment,
        })

        # Traces
        trace_exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
        trace.set_tracer_provider(tracer_provider)
        tracer = trace.get_tracer("o11y-agent")

        # Metrics
        metric_exporter = OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics")
        reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=30_000)
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(meter_provider)
        meter = metrics.get_meter("o11y-agent")

        logger.info("Agent self-observability enabled → %s", endpoint)
        return cls(tracer=tracer, meter=meter, enabled=True)

    @contextmanager
    def assessment_span(self, environment: str, auto_apply: bool):
        if not self._enabled:
            yield None
            return
        with self._tracer.start_as_current_span(
            "o11y_agent.assessment",
            attributes={
                "environment": environment,
                "auto_apply": auto_apply,
            },
        ) as span:
            yield span

    @contextmanager
    def specialist_span(self, domain: str):
        if not self._enabled:
            yield None
            return
        with self._tracer.start_as_current_span(
            "o11y_agent.specialist_run",
            attributes={"specialist.domain": domain},
        ) as span:
            yield span

    def record_run_metrics(
        self,
        findings: dict,
        elapsed_seconds: float,
        environment: str,
    ):
        """Emit metrics from a completed run's findings."""
        if not self._enabled:
            return

        attrs = {"environment": environment}
        self._run_duration.record(elapsed_seconds, attrs)

        # Count issues by severity across all specialists
        sev_counts: dict[str, int] = {}
        score = None
        silent_count = 0
        for f in findings.values():
            if not hasattr(f, "issues"):
                continue
            for issue in f.issues:
                sev_counts[issue.severity] = sev_counts.get(issue.severity, 0) + 1
            if hasattr(f, "instrumentation_score") and f.instrumentation_score is not None:
                score = f.instrumentation_score
            if hasattr(f, "services_silent"):
                silent_count += len(f.services_silent)

        for severity, count in sev_counts.items():
            self._issues_counter.add(count, {**attrs, "severity": severity})

        if score is not None:
            self._score_gauge.set(score, attrs)

        self._silent_gauge.set(silent_count, attrs)

    @staticmethod
    def noop() -> "SelfMonitor":
        return SelfMonitor(enabled=False)
