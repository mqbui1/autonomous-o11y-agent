"""
Real-time log tracker for the streaming pipeline.

Processes OTLP log records from the gateway to:
1. Track ERROR+ log counts per service in a sliding window — fires on error bursts
2. Scan log bodies for PII patterns (same patterns as span scanner)
3. Check for missing correlation attributes (trace_id, span_id)

This gives the streaming pipeline visibility into log signals without waiting
for the batch Log Specialist to query Splunk REST API.
"""

import logging
import re
import threading
import time
from collections import defaultdict

from .alerts import AlertDispatcher, StreamingAlert

logger = logging.getLogger(__name__)

# OTLP severity numbers: ERROR=17, FATAL=21
_ERROR_SEVERITY_THRESHOLD = 17

# Error burst threshold: N errors per service within the window
_ERROR_BURST_THRESHOLD = 50
_WINDOW_SECONDS = 300  # 5-minute window

# PII patterns for log body scanning (subset of pii_scanner patterns for speed)
_LOG_PII_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("visa_card",     re.compile(r'\b4[0-9]{12}(?:[0-9]{3})?\b'), "critical"),
    ("ssn",           re.compile(r'\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b'), "critical"),
    ("email",         re.compile(r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b'), "high"),
    ("generic_card",  re.compile(r'\b(?:\d[ -]?){13,16}\b'), "high"),
]

# Attributes that should be on log records for APM correlation
_REQUIRED_LOG_ATTRS = [
    ("trace_id",               "high",     "APM->Logs correlation broken — add trace context injection"),
    ("deployment.environment", "critical", "Log Observer scoping broken — add deployment.environment to resource"),
]


class LogTracker:
    """
    Tracks streaming log records for error bursts, PII, and correlation gaps.

    Designed to be called per log record in the OTLP receiver path.
    All operations are thread-safe and O(1) per record.
    """

    def __init__(
        self,
        dispatcher: AlertDispatcher,
        error_burst_threshold: int = _ERROR_BURST_THRESHOLD,
        window_seconds: int = _WINDOW_SECONDS,
    ):
        self.dispatcher = dispatcher
        self.burst_threshold = error_burst_threshold
        self.window = window_seconds
        self._lock = threading.Lock()
        # service -> list of (timestamp) for ERROR+ records
        self._error_windows: dict[str, list[float]] = defaultdict(list)
        # service -> set of already-reported correlation gaps
        self._reported_gaps: dict[str, set[str]] = defaultdict(set)
        # service -> set of already-reported PII patterns
        self._reported_pii: dict[str, set[str]] = defaultdict(set)

    def observe_log(
        self,
        service: str,
        severity_number: int,
        severity_text: str,
        body: str,
        attributes: dict,
        resource_attrs: dict,
    ):
        """Process one log record. Thread-safe."""
        merged = {**resource_attrs, **attributes}

        # 1. Error burst detection
        if severity_number >= _ERROR_SEVERITY_THRESHOLD or (
            severity_text and severity_text.upper() in ("ERROR", "FATAL", "CRITICAL")
        ):
            self._track_error(service)

        # 2. PII scan on log body
        if body:
            self._scan_body(service, body)

        # 3. Correlation attribute check
        self._check_log_attrs(service, merged)

    def _track_error(self, service: str):
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            window = self._error_windows[service]
            window.append(now)
            pruned = [t for t in window if t > cutoff]
            self._error_windows[service] = pruned
            count = len(pruned)

        if count >= self.burst_threshold:
            self.dispatcher.fire(StreamingAlert(
                severity="high",
                detector="log_tracker",
                service=service,
                title=f"Error log burst: `{service}` ({count} ERROR records in {self.window}s)",
                detail=(
                    f"error_count={count}  window={self.window}s  "
                    "— investigate service for recurring failure or exception storm."
                ),
                environment=self.dispatcher.environment,
            ))

    def _scan_body(self, service: str, body: str):
        for pattern_name, pattern, severity in _LOG_PII_PATTERNS:
            if pattern.search(body):
                pii_key = f"{service}:{pattern_name}"
                with self._lock:
                    if pii_key in self._reported_pii.get(service, set()):
                        continue
                    self._reported_pii[service].add(pii_key)
                self.dispatcher.fire(StreamingAlert(
                    severity=severity,
                    detector="pii",
                    service=service,
                    title=f"PII pattern `{pattern_name}` detected in log body",
                    detail=(
                        f"pattern={pattern_name}  source=log_record  "
                        "— data is IN FLIGHT, not yet in Splunk. "
                        "Deploy redactionprocessor and log body scrubbing immediately."
                    ),
                    environment=self.dispatcher.environment,
                ))
                break  # one match per log record

    def _check_log_attrs(self, service: str, attrs: dict):
        for attr, severity, impact in _REQUIRED_LOG_ATTRS:
            if attr not in attrs or not attrs[attr]:
                gap_key = f"{service}:{attr}"
                with self._lock:
                    if attr in self._reported_gaps.get(service, set()):
                        continue
                    self._reported_gaps[service].add(attr)
                self.dispatcher.fire(StreamingAlert(
                    severity=severity,
                    detector="attribute",
                    service=service,
                    title=f"Missing log correlation attribute: `{attr}`",
                    detail=f"impact={impact}",
                    environment=self.dispatcher.environment,
                ))

    def error_counts(self) -> dict[str, int]:
        """Return current error counts per service for status endpoint."""
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            return {
                svc: len([t for t in ts if t > cutoff])
                for svc, ts in self._error_windows.items()
            }
