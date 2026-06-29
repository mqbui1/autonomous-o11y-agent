"""
Alert dispatcher for the streaming pipeline.

Deduplicates findings (same service+pattern won't fire more than once per
cooldown window) and routes to configured outputs: stdout always,
webhook when ALERT_WEBHOOK_URL is set.
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
    Thread-safe alert dispatcher with deduplication.

    cooldown_seconds: minimum interval between identical alerts.
    webhook_url: optional HTTP endpoint to POST alerts as JSON.
    """

    def __init__(
        self,
        environment: str,
        cooldown_seconds: int = 300,
        webhook_url: str = "",
    ):
        self.environment = environment
        self.cooldown = cooldown_seconds
        self.webhook_url = webhook_url
        self._lock = threading.Lock()
        self._last_fired: dict[str, float] = {}

    def fire(self, alert: StreamingAlert) -> bool:
        """
        Dispatch an alert. Returns True if the alert was dispatched,
        False if suppressed by deduplication.
        """
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
            self._post_webhook(alert)

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
