"""
ObservationBuffer — thread-safe sliding-window of streaming pipeline events.

Written by the streaming pipeline detectors as telemetry flows through;
read by batch assessments to provide real-time context about what happened
since the last scheduled run.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Literal

ObservationType = Literal["pii", "new_service", "attribute_gap", "cardinality_spike"]


@dataclass
class Observation:
    type: ObservationType
    service: str
    detail: str
    severity: str
    timestamp: float = field(default_factory=time.time)


class ObservationBuffer:
    """
    Thread-safe ring buffer for streaming pipeline observations.
    Observations older than retention_minutes are discarded on write.
    """

    def __init__(self, retention_minutes: int = 120):
        self._lock = threading.Lock()
        self._observations: list[Observation] = []
        self._retention_seconds = retention_minutes * 60

    def add(self, obs: Observation):
        cutoff = time.time() - self._retention_seconds
        with self._lock:
            self._observations.append(obs)
            # Trim expired entries
            self._observations = [o for o in self._observations if o.timestamp > cutoff]

    def since(self, minutes: int) -> list[Observation]:
        cutoff = time.time() - (minutes * 60)
        with self._lock:
            return [o for o in self._observations if o.timestamp > cutoff]

    def summarize(self, window_minutes: int = 60, include_pii: bool = True) -> str:
        """
        Return a human-readable summary for injection into batch assessment context.
        Returns empty string if there are no observations in the window.

        include_pii: set False for all specialists except governance so PII
        detections are not duplicated across every specialist domain.
        """
        recent = self.since(window_minutes)
        if not recent:
            return ""

        by_type: dict[str, list[Observation]] = {}
        for obs in recent:
            by_type.setdefault(obs.type, []).append(obs)

        lines = [
            f"## Streaming Pipeline Observations (last {window_minutes} minutes)\n",
            f"Total events: {len(recent)}\n",
        ]

        if include_pii and "pii" in by_type:
            lines.append("**PII/PCI detections (CRITICAL — review before proceeding):**")
            for obs in by_type["pii"][:10]:
                lines.append(f"  - [{obs.service}] {obs.detail}")

        if "new_service" in by_type:
            services = [obs.service for obs in by_type["new_service"]]
            lines.append(
                f"\n**New services detected this window:** "
                + ", ".join(f"`{s}`" for s in services)
            )

        if "cardinality_spike" in by_type:
            lines.append("\n**Cardinality spikes (verify governance findings):**")
            for obs in by_type["cardinality_spike"][:5]:
                lines.append(f"  - [{obs.service}] {obs.detail}")

        if "attribute_gap" in by_type:
            by_svc: dict[str, list[str]] = {}
            for obs in by_type["attribute_gap"]:
                by_svc.setdefault(obs.service, []).append(obs.detail)
            lines.append(
                f"\n**Attribute gaps in {len(by_svc)} service(s) "
                "(cross-reference instrumentation findings):**"
            )
            for svc, details in list(by_svc.items())[:8]:
                lines.append(f"  - `{svc}`: {'; '.join(details[:3])}")

        return "\n".join(lines)

    @property
    def pii_hit_count(self) -> int:
        with self._lock:
            return sum(1 for o in self._observations if o.type == "pii")
