"""
Trace-correlated heap allocation SpanProcessor for Python (tracemalloc).

Activated when HEAP_SNAPSHOT_ENABLED=true. For each span that ends,
takes a tracemalloc snapshot, diffs it against the snapshot taken at
span start, serialises the top allocation frames as a json-alloc-v1
OTLP log record, and ships it via OTLP/HTTP to the same endpoint the
service already uses for traces.

Wire format (OTLP/HTTP JSON):
  resourceLogs[0].scopeLogs[0].logRecords[0]:
    attributes:
      com.splunk.sourcetype        = "otel.profiling"
      profiling.data.type          = "allocation"
      profiling.data.format        = "json-alloc-v1"
      profiling.instrumentation.source = "snapshot"
    body.stringValue:
      {"trace_id": "<hex>", "frames": [
        {"function": "...", "file": "...", "line": 42,
         "size_bytes": 102400, "count": 5}
      ]}
"""

import json
import os
import threading
import time
import tracemalloc
import urllib.request
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor


_OTLP_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://localhost:4318",
).rstrip("/")
_TOP_FRAMES    = int(os.getenv("HEAP_SNAPSHOT_TOP_FRAMES", "10"))
_MIN_SIZE_KB   = int(os.getenv("HEAP_SNAPSHOT_MIN_SIZE_KB", "16"))   # skip tiny allocs
_SERVICE_NAME  = os.getenv("OTEL_SERVICE_NAME", "unknown")

# Keep one tracemalloc snapshot per span-id; spans nest so we use a dict
# rather than assuming a single active span.
_span_start_snaps: dict[str, Any] = {}
_lock = threading.Lock()


class HeapSnapshotProcessor(SpanProcessor):
    """
    Take tracemalloc diffs around each span and emit allocation records.
    Only activated when tracemalloc is started (which this class does once).
    """

    def __init__(self) -> None:
        if not tracemalloc.is_tracing():
            tracemalloc.start(5)   # 5-frame callstack depth

    def on_start(self, span, parent_context=None) -> None:
        sid = _span_id_hex(span)
        if not sid:
            return
        snap = tracemalloc.take_snapshot()
        with _lock:
            _span_start_snaps[sid] = snap

    def on_end(self, span: ReadableSpan) -> None:
        sid = _span_id_hex(span)
        if not sid:
            return
        end_snap = tracemalloc.take_snapshot()
        with _lock:
            start_snap = _span_start_snaps.pop(sid, None)
        if start_snap is None:
            return

        trace_id = _trace_id_hex(span)
        if not trace_id:
            return

        frames = _diff_snapshots(start_snap, end_snap)
        if not frames:
            return

        _emit_allocation_log(trace_id, frames, span)

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


# ── Helpers ──────────────────────────────────────────────────────────────────

def _span_id_hex(span) -> str:
    try:
        ctx = span.get_span_context()
        sid = ctx.span_id
        if not sid:
            return ""
        return format(sid, "016x")
    except Exception:
        return ""


def _trace_id_hex(span) -> str:
    try:
        ctx = span.get_span_context()
        tid = ctx.trace_id
        if not tid:
            return ""
        return format(tid, "032x")
    except Exception:
        return ""


def _diff_snapshots(start, end) -> list[dict]:
    """Return top allocation frames between two tracemalloc snapshots."""
    try:
        stats = end.compare_to(start, "lineno")
        results = []
        for stat in stats[:_TOP_FRAMES]:
            size_bytes = stat.size_diff
            if size_bytes < _MIN_SIZE_KB * 1024:
                continue
            tb = stat.traceback
            if not tb:
                continue
            frame = tb[0]
            results.append({
                "function": _fn_name(frame),
                "file":     frame.filename,
                "line":     frame.lineno,
                "size_bytes": size_bytes,
                "count":    stat.count_diff,
            })
        return results
    except Exception:
        return []


def _fn_name(frame) -> str:
    """Best-effort function name from a tracemalloc frame."""
    # tracemalloc frames only carry filename+lineno; no function name.
    # Use the filename's basename as a proxy.
    import os as _os
    return _os.path.basename(frame.filename).replace(".py", "")


def _emit_allocation_log(trace_id: str, frames: list[dict], span) -> None:
    """Ship one OTLP/HTTP JSON log record carrying the allocation diff."""
    body = json.dumps({"trace_id": trace_id, "frames": frames})

    resource_attrs = [
        _str_attr("service.name", _SERVICE_NAME),
    ]

    log_record = {
        "timeUnixNano": str(int(time.time() * 1e9)),
        "body": {"stringValue": body},
        "attributes": [
            _str_attr("com.splunk.sourcetype",            "otel.profiling"),
            _str_attr("profiling.data.type",              "allocation"),
            _str_attr("profiling.data.format",            "json-alloc-v1"),
            _str_attr("profiling.instrumentation.source", "snapshot"),
        ],
    }

    payload = {
        "resourceLogs": [{
            "resource": {"attributes": resource_attrs},
            "scopeLogs": [{
                "scope": {"name": "heap_snapshot_collector"},
                "logRecords": [log_record],
            }],
        }]
    }

    try:
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(
            f"{_OTLP_ENDPOINT}/v1/logs",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass   # best-effort; never break the span


def _str_attr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}
