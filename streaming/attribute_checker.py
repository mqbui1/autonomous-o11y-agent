"""
Required attribute checker for the streaming pipeline.

Validates that spans and metrics carry the attributes needed for
Splunk Related Content links to function. Fires on first missing
attribute per service so customers get root-cause diagnosis at the
gateway rather than inferring it from post-ingestion gaps.
"""

import logging
import threading
from collections import defaultdict

from .alerts import AlertDispatcher, StreamingAlert

logger = logging.getLogger(__name__)

# Attributes required on every span for full Splunk observability
REQUIRED_SPAN_ATTRS = [
    ("deployment.environment", "critical",
     "Service Centric View scoping broken; APM->Logs and APM->IM RC broken"),
    ("host.name",              "high",
     "APM->Infrastructure Related Content broken; Host Navigator empty"),
    ("k8s.pod.name",           "high",
     "K8s Navigator APM overlay broken"),
    ("service.name",           "critical",
     "Service identity missing — span cannot be attributed to a service"),
]

# Attributes required on every metric data point
REQUIRED_METRIC_ATTRS = [
    ("deployment.environment", "critical",
     "Environment-scoped dashboard filtering broken for this metric"),
    ("service.name",           "high",
     "IM->APM service link broken"),
    ("host.name",              "high",
     "Host Navigator grouping broken"),
]


class AttributeChecker:
    """
    Checks incoming spans and metrics for required attributes.

    Tracks which (service, missing_attr) pairs have already been reported
    so each gap fires once per cooldown window rather than on every span.
    """

    def __init__(self, dispatcher: AlertDispatcher):
        self.dispatcher = dispatcher
        self._lock = threading.Lock()
        # service -> set of missing attribute keys already reported
        self._reported: dict[str, set[str]] = defaultdict(set)

    def check_span(self, service: str, span_name: str, attributes: dict):
        for attr, severity, impact in REQUIRED_SPAN_ATTRS:
            if attr not in attributes or not attributes[attr]:
                self._fire_span(service, span_name, attr, severity, impact)

    def check_metric(self, metric_name: str, attributes: dict):
        service = attributes.get("service.name", metric_name)
        for attr, severity, impact in REQUIRED_METRIC_ATTRS:
            if attr not in attributes or not attributes[attr]:
                self._fire_metric(service, metric_name, attr, severity, impact)

    def _fire_span(
        self, service: str, span_name: str,
        attr: str, severity: str, impact: str,
    ):
        key = f"span:{attr}"
        with self._lock:
            if attr in self._reported[service]:
                return
            self._reported[service].add(attr)

        fix = _span_fix(attr)
        self.dispatcher.fire(StreamingAlert(
            severity=severity,
            detector="attribute",
            service=service,
            title=f"Missing required span attribute: `{attr}`",
            detail=(
                f"span={span_name}  impact={impact}  "
                f"fix={fix}"
            ),
            environment=self.dispatcher.environment,
        ))
        logger.info(
            "[attribute] %s: spans missing %s  fix: %s",
            service, attr, fix,
        )

    def _fire_metric(
        self, service: str, metric_name: str,
        attr: str, severity: str, impact: str,
    ):
        key = f"metric:{attr}"
        with self._lock:
            if key in self._reported[service]:
                return
            self._reported[service].add(key)

        fix = _metric_fix(attr)
        self.dispatcher.fire(StreamingAlert(
            severity=severity,
            detector="attribute",
            service=service,
            title=f"Missing required metric dimension: `{attr}`",
            detail=(
                f"metric={metric_name}  impact={impact}  "
                f"fix={fix}"
            ),
            environment=self.dispatcher.environment,
        ))

    def reset_reported(self):
        """Called by batch assessment on each run to re-enable gap detection."""
        with self._lock:
            self._reported.clear()


def _span_fix(attr: str) -> str:
    fixes = {
        "deployment.environment":
            "OTEL_RESOURCE_ATTRIBUTES=deployment.environment=<env> on each pod, "
            "or resource processor in gateway: {key: deployment.environment, value: <env>, action: upsert}",
        "host.name":
            "resourcedetection processor with detectors=[system,k8snode] in gateway pipeline",
        "k8s.pod.name":
            "k8sattributes processor in gateway with extract.metadata=[k8s.pod.name,k8s.node.name,k8s.namespace.name]",
        "service.name":
            "OTEL_SERVICE_NAME=<service> env var on each pod",
    }
    return fixes.get(attr, f"Add {attr} via OTEL_RESOURCE_ATTRIBUTES or resource processor")


def _metric_fix(attr: str) -> str:
    fixes = {
        "deployment.environment":
            "resource processor: {key: deployment.environment, value: <env>, action: upsert}",
        "service.name":
            "OTEL_SERVICE_NAME=<service> on each pod",
        "host.name":
            "resourcedetection processor with detectors=[system,k8snode]",
    }
    return fixes.get(attr, f"Add {attr} via resource processor in gateway")
