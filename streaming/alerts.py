"""
Alert dispatcher for the streaming pipeline.

Deduplicates findings (same service+pattern won't fire more than once per
cooldown window) and routes to configured outputs: stdout always,
webhook when ALERT_WEBHOOK_URL is set.

Webhook delivery is asynchronous (daemon thread) so it never blocks
the OTLP receiver request path.

Suppression: set ALERT_SUPPRESS=pii:test-service,attribute:load-generator
to permanently silence matching alerts (format: detector:service).
"""

import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

Severity = Literal["critical", "high", "medium", "low"]


@dataclass
class StreamingAlert:
    severity: Severity
    detector: str          # pii | attribute | cardinality | new_service
    service: str
    title: str
    detail: str
    environment: str
    timestamp: float = field(default_factory=time.time)

    def key(self) -> str:
        """Deduplication key — same alert won't re-fire within cooldown."""
        return f"{self.detector}:{self.service}:{self.title}"

    def suppress_key(self) -> str:
        """Suppression key — matches ALERT_SUPPRESS entries."""
        return f"{self.detector}:{self.service}"

    def to_dict(self) -> dict:
        return {
            "severity":    self.severity,
            "detector":    self.detector,
            "service":     self.service,
            "title":       self.title,
            "detail":      self.detail,
            "environment": self.environment,
            "timestamp":   self.timestamp,
        }


class AlertDispatcher:
    """
    Thread-safe alert dispatcher with deduplication and suppression.

    cooldown_seconds: minimum interval between identical alerts.
    webhook_url: optional HTTP endpoint to POST alerts as JSON (async).
    suppress_patterns: list of "detector:service" strings to permanently silence.
    """

    def __init__(
        self,
        environment: str,
        cooldown_seconds: int = 300,
        webhook_url: str = "",
        suppress_patterns: list[str] | None = None,
    ):
        self.environment = environment
        self.cooldown = cooldown_seconds
        self.webhook_url = webhook_url
        self._suppress: set[str] = set(suppress_patterns or [])
        self._lock = threading.Lock()
        self._last_fired: dict[str, float] = {}

    def suppress(self, detector: str, service: str):
        """Permanently suppress alerts for a detector+service combination."""
        self._suppress.add(f"{detector}:{service}")
        logger.info("[alerts] Suppressed: %s:%s", detector, service)

    def fire(self, alert: StreamingAlert) -> bool:
        """
        Dispatch an alert. Returns True if dispatched,
        False if suppressed by deduplication or suppression list.
        """
        # Permanent suppression check
        if alert.suppress_key() in self._suppress:
            return False

        key = alert.key()
        now = time.time()

        with self._lock:
            if now - self._last_fired.get(key, 0) < self.cooldown:
                return False
            self._last_fired[key] = now

        self._emit(alert)
        return True

    def _emit(self, alert: StreamingAlert):
        level = logging.CRITICAL if alert.severity == "critical" else logging.WARNING
        logger.log(
            level,
            "[STREAMING:%s] [%s] %s — %s | %s",
            alert.detector.upper(),
            alert.severity.upper(),
            alert.service,
            alert.title,
            alert.detail,
        )

        if self.webhook_url:
            # Fire-and-forget: never block the OTLP receiver thread
            t = threading.Thread(
                target=self._post_webhook, args=(alert,), daemon=True
            )
            t.start()

    def _post_webhook(self, alert: StreamingAlert):
        try:
            payload = json.dumps(alert.to_dict()).encode()
            req = urllib.request.Request(
                self.webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
        except Exception as exc:
            logger.warning("Webhook delivery failed: %s", exc)
