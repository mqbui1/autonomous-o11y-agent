"""
sitecustomize.py — injected via PYTHONPATH=/opt/heap_profiling.

Python automatically imports sitecustomize at interpreter startup, BEFORE
any application code or opentelemetry-instrument runs. We use this to patch
TracerProvider.__init__ so that HeapSnapshotProcessor is registered as a
SpanProcessor before any spans are recorded.

The patch is one-shot: it restores the original __init__ after the first call
so subsequent TracerProvider constructions (tests, secondary providers) are
not affected.

Only active when HEAP_SNAPSHOT_ENABLED=true to avoid overhead on other services.
"""

import os as _os

if _os.getenv("HEAP_SNAPSHOT_ENABLED") == "true":
    try:
        from opentelemetry.sdk import trace as _sdk_trace

        _orig_init = _sdk_trace.TracerProvider.__init__

        def _patched_init(self, *args, **kwargs):
            # Restore immediately so only the first TracerProvider is patched
            _sdk_trace.TracerProvider.__init__ = _orig_init
            _orig_init(self, *args, **kwargs)
            try:
                import sys as _sys
                _sys.path.insert(0, "/opt/heap_profiling")
                from heap_snapshot_collector import HeapSnapshotProcessor
                self.add_span_processor(HeapSnapshotProcessor())
            except Exception as _e:
                import logging
                logging.getLogger(__name__).warning(
                    "HeapSnapshotProcessor failed to load: %s", _e
                )

        _sdk_trace.TracerProvider.__init__ = _patched_init

    except Exception:
        pass  # Never crash the service during startup
