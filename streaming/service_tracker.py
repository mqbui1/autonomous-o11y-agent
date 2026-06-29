"""
New service detector for the streaming pipeline.

Fires the moment a service.name never seen before appears in the span
stream — enabling zero-lag detector provisioning rather than waiting
up to 60 minutes for the next batch assessment cycle.
"""

import logging
import threading
import time
from typing import Callable

from .alerts import AlertDispatcher, StreamingAlert

logger = logging.getLogger(__name__)


class ServiceTracker:
    """
    Tracks known services. On first appearance of a new service.name,
    fires an alert and calls any registered provisioning callbacks.

    Callbacks receive (service_name: str) and are invoked in a daemon
    thread to avoid blocking the OTLP receiver.
    """

    def __init__(self, dispatcher: AlertDispatcher):
        self.dispatcher = dispatcher
        self._lock = threading.Lock()
        self._known: set[str] = set()
        self._callbacks: list[Callable[[str], None]] = []
        # service -> first_seen_timestamp
        self._first_seen: dict[str, float] = {}

    def on_new_service(self, callback: Callable[[str], None]):
        """Register a callback to run when a new service is first seen."""
        self._callbacks.append(callback)

    def observe(self, service: str, attributes: dict):
        """Record a span from a service. Fires on first appearance."""
        if not service or service == "unknown_service":
            return

        with self._lock:
            if service in self._known:
                return
            self._known.add(service)
            self._first_seen[service] = time.time()

        logger.info("[service_tracker] New service detected: %s", service)
        self._fire(service, attributes)
        self._run_callbacks(service)

    def _fire(self, service: str, attributes: dict):
        env = attributes.get("deployment.environment", "unknown")
        sdk = attributes.get("telemetry.sdk.language", "unknown")
        self.dispatcher.fire(StreamingAlert(
            severity="medium",
            detector="new_service",
            service=service,
            title=f"New service detected: `{service}`",
            detail=(
                f"language={sdk}  environment={env}  "
                "Detector provisioning triggered automatically. "
                "Baseline learning will complete in ~2 minutes."
            ),
            environment=self.dispatcher.environment,
        ))

    def _run_callbacks(self, service: str):
        for cb in self._callbacks:
            t = threading.Thread(
                target=self._safe_callback, args=(cb, service), daemon=True
            )
            t.start()

    def _safe_callback(self, cb: Callable, service: str):
        try:
            cb(service)
        except Exception as exc:
            logger.error(
                "Service tracker callback failed for %s: %s", service, exc
            )

    def known_services(self) -> set[str]:
        with self._lock:
            return set(self._known)

    def seed(self, services: list[str]):
        """Pre-populate known services so existing ones don't trigger callbacks."""
        with self._lock:
            self._known.update(services)
        logger.info(
            "[service_tracker] Seeded with %d known services", len(services)
        )
