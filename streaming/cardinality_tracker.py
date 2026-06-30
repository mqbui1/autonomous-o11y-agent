"""
Real-time cardinality tracker for the streaming pipeline.

Counts unique dimension combinations per metric in a sliding time window.
Fires before the cardinality explosion reaches Splunk and incurs MTS cost.

Identifies the worst-offending dimension so the fix YAML is immediately
actionable (same pattern as the batch governance tool but pre-ingestion).
"""

import logging
import threading
import time
from collections import defaultdict

from .alerts import AlertDispatcher, StreamingAlert

logger = logging.getLogger(__name__)

# Default thresholds
DEFAULT_THRESHOLD  = 10_000   # unique dim combos — fire warning
CRITICAL_THRESHOLD = 50_000   # critical — likely runaway label
WINDOW_SECONDS     = 300      # 5-minute sliding window


class CardinalityTracker:
    """
    Sliding-window unique-combination counter per metric name.

    Memory is bounded: observations older than window_seconds are pruned
    on each check. With a 5-min window at 1,000 spans/sec, worst-case
    memory per metric is ~1,000 × 5 × 60 = 300K frozensets.
    For most metrics this is negligible; for exploding metrics the
    alert fires before memory grows large.
    """

    def __init__(
        self,
        dispatcher: AlertDispatcher,
        warn_threshold: int = DEFAULT_THRESHOLD,
        critical_threshold: int = CRITICAL_THRESHOLD,
        window_seconds: int = WINDOW_SECONDS,
    ):
        self.dispatcher = dispatcher
        self.warn_threshold = warn_threshold
        self.critical_threshold = critical_threshold
        self.window = window_seconds
        self._lock = threading.Lock()
        # metric_name -> list of (timestamp, frozenset of (k,v) pairs)
        self._windows: dict[str, list] = defaultdict(list)
        # metric_name -> set of dim keys seen
        self._dim_keys: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def observe_metric(self, metric_name: str, attributes: dict):
        """Record one data point for a metric. Thread-safe."""
        now = time.time()
        cutoff = now - self.window
        combo = frozenset(
            (k, str(v)) for k, v in attributes.items()
            if k not in ("_timestamp", "start_time_unix_nano", "time_unix_nano")
        )

        with self._lock:
            window = self._windows[metric_name]
            window.append((now, combo))
            # Prune expired entries
            pruned = [(t, c) for t, c in window if t > cutoff]
            self._windows[metric_name] = pruned
            # If the window just emptied, evict dimension tracking too (prevents memory leak)
            if not pruned:
                self._dim_keys.pop(metric_name, None)
                return
            # Count unique combos
            unique_combos = {c for _, c in pruned}
            unique_count = len(unique_combos)

            # Track per-dimension key frequency for blast-radius analysis
            dim_counts = self._dim_keys[metric_name]
            for k in attributes:
                dim_counts[k] += 1

        if unique_count >= self.critical_threshold:
            self._fire(metric_name, unique_count, "critical", attributes)
        elif unique_count >= self.warn_threshold:
            self._fire(metric_name, unique_count, "high", attributes)

    def _fire(
        self,
        metric_name: str,
        unique_count: int,
        severity: str,
        sample_attrs: dict,
    ):
        # Find the highest-cardinality dimension
        with self._lock:
            dim_counts = dict(self._dim_keys.get(metric_name, {}))

        worst_dim = max(dim_counts, key=dim_counts.get) if dim_counts else "unknown"

        drop_yaml = (
            f"processors:\n"
            f"  attributes/drop_{worst_dim.replace('.','_')}:\n"
            f"    actions:\n"
            f"      - key: {worst_dim}\n"
            f"        action: delete\n"
            f"  # Alternative: hash to preserve groupability\n"
            f"  # transform/hash_{worst_dim.replace('.','_')}:\n"
            f"  #   metric_statements:\n"
            f"  #     - context: datapoint\n"
            f"  #       statements:\n"
            f"  #         - set(attributes[\"{worst_dim}\"], "
            f"SHA256(attributes[\"{worst_dim}\"]))"
        )

        self.dispatcher.fire(StreamingAlert(
            severity=severity,
            detector="cardinality",
            service=metric_name,
            title=f"Cardinality explosion building: `{metric_name}` ({unique_count:,} unique combos)",
            detail=(
                f"unique_combinations={unique_count:,}  "
                f"window={self.window}s  "
                f"worst_dimension={worst_dim}  "
                f"pre-ingestion — no MTS cost yet.  "
                f"Fix YAML:\n{drop_yaml}"
            ),
            environment=self.dispatcher.environment,
        ))
        logger.warning(
            "[cardinality] %s: %d unique combos in %ds window, worst dim=%s",
            metric_name, unique_count, self.window, worst_dim,
        )

    def top_metrics(self, n: int = 10) -> list[tuple[str, int]]:
        """Return top-n metrics by current unique combination count."""
        with self._lock:
            now = time.time()
            cutoff = now - self.window
            result = []
            for metric, window in self._windows.items():
                pruned = [c for t, c in window if t > cutoff]
                result.append((metric, len(set(pruned))))
        return sorted(result, key=lambda x: x[1], reverse=True)[:n]
