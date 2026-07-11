"""
Lightweight OTLP/HTTP receiver.

Runs a Flask server on the configured port (default 4318) that accepts
OTLP/HTTP JSON payloads from the gateway collector's otlp/http exporter.

Endpoints:
  POST /v1/traces   — OTLP trace export
  POST /v1/metrics  — OTLP metric export
  GET  /status      — pipeline stats (known services, top cardinality)
  GET  /healthz     — liveness probe

The gateway collector sends here via a secondary otlp/http exporter
configured with sending_queue.enabled=true so the receiver's latency
never blocks the main Splunk export path.
"""

import json
import logging
import threading
from typing import TYPE_CHECKING

import os
from flask import Flask, Response, request, send_file

if TYPE_CHECKING:
    from streaming.pipeline import StreamingPipeline

logger = logging.getLogger(__name__)

# Set by POST /api/assessment/trigger; waited on by the batch assessment loop.
trigger_event = threading.Event()

# Set while an assessment is actively running; cleared when it finishes.
_running_lock = threading.Lock()
_assessment_running = False
_assessment_progress: dict = {
    "phase": "idle", "completed": 0, "total": 0,
    "completed_specialists": [], "started_at": None,
}


def set_assessment_running(value: bool) -> None:
    global _assessment_running
    with _running_lock:
        _assessment_running = value


def is_assessment_running() -> bool:
    with _running_lock:
        return _assessment_running


def reset_assessment_progress(total: int = 9) -> None:
    import time as _t
    with _running_lock:
        _assessment_progress.update({
            "phase": "starting", "completed": 0, "total": total,
            "completed_specialists": [], "started_at": _t.time(),
        })


def update_assessment_progress(phase: str, completed: int | None = None,
                                name: str | None = None) -> None:
    with _running_lock:
        _assessment_progress["phase"] = phase
        if completed is not None:
            _assessment_progress["completed"] = completed
        if name and name not in _assessment_progress["completed_specialists"]:
            _assessment_progress["completed_specialists"].append(name)


def create_app(pipeline: "StreamingPipeline", environment: str = "") -> Flask:
    app = Flask(__name__)
    app.logger.setLevel(logging.WARNING)   # suppress Flask access log noise

    @app.post("/v1/traces")
    def receive_traces():
        payload = _parse_body()
        if payload is None:
            return Response("Bad Request", status=400)

        resource_spans = payload.get("resourceSpans", [])
        if resource_spans:
            try:
                pipeline.process_resource_spans(resource_spans)
            except Exception as exc:
                logger.error("Error processing traces: %s", exc, exc_info=True)

        return Response(
            json.dumps({"partialSuccess": {}}),
            status=200,
            mimetype="application/json",
        )

    @app.post("/v1/metrics")
    def receive_metrics():
        payload = _parse_body()
        if payload is None:
            return Response("Bad Request", status=400)

        resource_metrics = payload.get("resourceMetrics", [])
        if resource_metrics:
            try:
                pipeline.process_resource_metrics(resource_metrics)
            except Exception as exc:
                logger.error("Error processing metrics: %s", exc, exc_info=True)

        return Response(
            json.dumps({"partialSuccess": {}}),
            status=200,
            mimetype="application/json",
        )

    @app.post("/v1/logs")
    def receive_logs():
        payload = _parse_body()
        if payload is None:
            return Response("Bad Request", status=400)

        resource_logs = payload.get("resourceLogs", [])
        if resource_logs:
            try:
                pipeline.process_resource_logs(resource_logs)
            except Exception as exc:
                logger.error("Error processing logs: %s", exc, exc_info=True)

        return Response(
            json.dumps({"partialSuccess": {}}),
            status=200,
            mimetype="application/json",
        )

    @app.get("/v1/logs")
    def receive_logs_get():
        # Some collectors do a GET probe — return 200 to avoid 404 noise
        return Response(json.dumps({"partialSuccess": {}}), status=200, mimetype="application/json")

    @app.get("/status")
    def status():
        return Response(
            json.dumps(pipeline.stats(), indent=2),
            status=200,
            mimetype="application/json",
        )

    @app.get("/healthz")
    def healthz():
        return Response("ok", status=200)

    @app.get("/api/profiling/status")
    def profiling_status():
        try:
            import sys as _sys, os as _os
            agent_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            if agent_root not in _sys.path:
                _sys.path.insert(0, agent_root)
            from streaming import profiling_store as ps
            env = environment or (pipeline.environment if hasattr(pipeline, "environment") else "")
            services = ps.get_services(env) if env else []
            flamegraphs = {}
            for svc in services:
                frames = ps.get_flamegraph(svc, env)
                flamegraphs[svc] = {"frame_count": len(frames), "top_frame": frames[0] if frames else None}
            from streaming import exception_store as es
            exc_services = es.services()
            return Response(
                json.dumps({
                    "environment": env,
                    "profiling_services": services,
                    "exception_services": exc_services,
                    "flamegraphs": flamegraphs,
                }),
                status=200, mimetype="application/json",
            )
        except Exception as exc:
            return Response(
                json.dumps({"error": str(exc)}),
                status=500, mimetype="application/json",
            )

    @app.get("/api/profiling/callgraph/<service>/<trace_id>")
    def callgraph_lookup(service, trace_id):
        try:
            import sys as _sys, os as _os
            agent_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            if agent_root not in _sys.path:
                _sys.path.insert(0, agent_root)
            from streaming import snapshot_store as ss
            methods = ss.get_slowest_methods(service, trace_id, limit=10)
            return Response(
                json.dumps({'service': service, 'trace_id': trace_id, 'slowest_methods': methods, 'found': bool(methods)}),
                status=200, mimetype="application/json",
            )
        except Exception as exc:
            return Response(json.dumps({'error': str(exc)}), status=500, mimetype="application/json")

    @app.get("/api/profiling/snapshot")
    def snapshot_debug():
        try:
            import sys as _sys, os as _os
            agent_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            if agent_root not in _sys.path:
                _sys.path.insert(0, agent_root)
            from streaming import snapshot_store as ss
            with ss._store._lock:
                keys = []
                for (svc, tid), recs in ss._store._records.items():
                    arrived_at = max((r['ts'] for r in recs), default=0)
                    keys.append({
                        'service': svc, 'trace_id': tid,
                        'record_count': len(recs), 'arrived_at': arrived_at,
                    })
            return Response(
                json.dumps({'total_traces': len(keys), 'traces': keys}),
                status=200, mimetype="application/json",
            )
        except Exception as exc:
            return Response(json.dumps({'error': str(exc)}), status=500, mimetype="application/json")

    @app.get("/api/profiling/flamegraph/<service>")
    def flamegraph_data(service):
        """Return all AlwaysOn CPU frames for a service (for the UI icicle chart)."""
        try:
            import sys as _sys, os as _os
            agent_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            if agent_root not in _sys.path:
                _sys.path.insert(0, agent_root)
            from streaming import profiling_store as ps
            env = environment or (pipeline.environment if hasattr(pipeline, "environment") else "")
            since = float(request.args.get("since", 0) or 0)
            until = float(request.args.get("until", 0) or 0)
            frames = ps.get_flamegraph(service, env, since=since, until=until)
            return Response(
                json.dumps({"service": service, "environment": env, "frames": frames}),
                status=200, mimetype="application/json",
            )
        except Exception as exc:
            return Response(json.dumps({"error": str(exc)}), status=500, mimetype="application/json")

    @app.get("/api/profiling/hotspots/<service>")
    def hotspots_data(service):
        """Return aggregated method hotspots across all snapshot traces for a service."""
        try:
            import sys as _sys, os as _os
            agent_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            if agent_root not in _sys.path:
                _sys.path.insert(0, agent_root)
            from streaming import snapshot_store as ss
            since = float(request.args.get("since", 0) or 0)
            until = float(request.args.get("until", 0) or 0)
            data = ss.get_hotspots(service, since=since, until=until)
            return Response(json.dumps(data), status=200, mimetype="application/json")
        except Exception as exc:
            return Response(json.dumps({"error": str(exc)}), status=500, mimetype="application/json")

    @app.get("/api/exceptions")
    def exceptions_list():
        """Return recent exception summaries (newest first). Optional ?service= filter."""
        try:
            import sys as _sys, os as _os
            agent_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            if agent_root not in _sys.path:
                _sys.path.insert(0, agent_root)
            from streaming import exception_store as es, snapshot_store as ss
            svc   = request.args.get("service") or None
            limit = int(request.args.get("limit", 200) or 200)
            data  = es.list_recent(service=svc, limit=limit)
            for item in data:
                item["has_snapshot"] = ss.has_data(item["service"], item["trace_id"])
            return Response(json.dumps({"exceptions": data, "count": len(data)}),
                            status=200, mimetype="application/json")
        except Exception as exc:
            return Response(json.dumps({"error": str(exc)}), status=500, mimetype="application/json")

    @app.get("/api/exceptions/<service>/<trace_id>")
    def exception_detail(service, trace_id):
        """Return full exception records (with parsed frames) for a specific trace."""
        try:
            import sys as _sys, os as _os
            agent_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
            if agent_root not in _sys.path:
                _sys.path.insert(0, agent_root)
            from streaming import exception_store as es, snapshot_store as ss
            records = es.get(service, trace_id)
            has_snapshot = ss.has_data(service, trace_id)
            return Response(
                json.dumps({
                    "service":      service,
                    "trace_id":     trace_id,
                    "records":      records,
                    "has_snapshot": has_snapshot,
                }),
                status=200, mimetype="application/json",
            )
        except Exception as exc:
            return Response(json.dumps({"error": str(exc)}), status=500, mimetype="application/json")

    @app.get("/profiling")
    def profiling_ui():
        """Serve the profiling flamegraph + snapshot UI."""
        ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiling_ui.html")
        return send_file(ui_path, mimetype="text/html")

    # ── Assessment API (serves UI data to the Supervisor) ─────────────────────

    @app.get("/api/assessment/latest")
    def assessment_latest():
        import sys as _sys, os as _os
        # Ensure the agent root is importable when running inside the container
        agent_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if agent_root not in _sys.path:
            _sys.path.insert(0, agent_root)
        from state import load_assessment_detail
        env = environment or (pipeline.environment if hasattr(pipeline, "environment") else "")
        data = load_assessment_detail(env) if env else None
        if data is None:
            return Response(
                json.dumps({"error": "No assessment available yet", "environment": env}),
                status=404, mimetype="application/json",
            )
        return Response(json.dumps(data), status=200, mimetype="application/json")

    @app.get("/api/assessment/history")
    def assessment_history():
        import sys as _sys, os as _os
        agent_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if agent_root not in _sys.path:
            _sys.path.insert(0, agent_root)
        from state import load_state
        env = environment or (pipeline.environment if hasattr(pipeline, "environment") else "")
        if not env:
            return Response(json.dumps({"runs": []}), status=200, mimetype="application/json")
        state = load_state(env)
        runs = [
            {
                "run_id": r.run_id,
                "timestamp": r.timestamp,
                "instrumentation_score": r.instrumentation_score,
                "services_active": r.services_active,
                "services_silent": r.services_silent,
                "detector_count": r.detector_count,
                "critical_issues": r.critical_issues[:3],
                "top_findings": r.top_findings[:3],
            }
            for r in reversed(state.runs[-20:])
        ]
        return Response(json.dumps({"runs": runs, "environment": env}), status=200, mimetype="application/json")

    @app.get("/api/assessment/<run_id>")
    def assessment_by_id(run_id):
        import sys as _sys, os as _os
        agent_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if agent_root not in _sys.path:
            _sys.path.insert(0, agent_root)
        from state import load_assessment_detail_by_id
        env = environment or (pipeline.environment if hasattr(pipeline, "environment") else "")
        data = load_assessment_detail_by_id(env, run_id) if env else None
        if data is None:
            return Response(
                json.dumps({"error": f"No assessment found for run_id: {run_id}"}),
                status=404, mimetype="application/json",
            )
        return Response(json.dumps(data), status=200, mimetype="application/json")

    @app.get("/api/assessment/running")
    def assessment_running_status():
        running = is_assessment_running()
        queued = trigger_event.is_set()
        with _running_lock:
            progress = dict(_assessment_progress)
        return Response(
            json.dumps({"running": running, "queued": queued, "progress": progress}),
            status=200, mimetype="application/json",
        )

    @app.post("/api/fix")
    def code_fix():
        """
        Generate a code fix for a profiling hotspot using the LLM.

        POST body (JSON): service, blocking_fn, blocking_file, blocking_line,
        self_time_ms, app_fn, app_file, app_line, source_lines.
        """
        import sys as _sys, os as _os
        agent_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if agent_root not in _sys.path:
            _sys.path.insert(0, agent_root)
        from receiver.fix_generator import generate_fix as _generate_fix

        body = request.get_json(force=True, silent=True) or {}
        if not body:
            return Response(
                json.dumps({"error": "Request body required"}),
                status=400, mimetype="application/json",
            )
        try:
            # Build a minimal config from env vars — avoid AgentConfig's required Splunk args
            import os as _os2
            from providers.bedrock import BedrockProvider as _BP
            from providers.openai_compat import OpenAICompatProvider as _OAP
            _llm = _os2.environ.get("LLM_PROVIDER", "bedrock").lower()
            if _llm in ("ollama", "openai"):
                _provider = _OAP(
                    base_url=_os2.environ.get("OPENAI_BASE_URL", _os2.environ.get("OLLAMA_BASE_URL", "")),
                    api_key=_os2.environ.get("OPENAI_API_KEY", "ollama"),
                    model=_os2.environ.get("OPENAI_MODEL", _os2.environ.get("OLLAMA_MODEL", "")),
                )
            else:
                _provider = _BP(
                    model_id=_os2.environ.get("BEDROCK_MODEL_ID", ""),
                    region=_os2.environ.get("AWS_DEFAULT_REGION", "us-west-2"),
                )
            result = _generate_fix(_provider, body)
        except Exception as exc:
            logger.error("Fix generation error: %s", exc, exc_info=True)
            result = {"error": str(exc)}
        return Response(json.dumps(result), status=200, mimetype="application/json")

    @app.get("/api/source")
    def source_view():
        """
        Return source lines for a file inside a service container.
        Query params: service, file, line (1-based), context (lines around target).
        """
        import sys as _sys, os as _os
        agent_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if agent_root not in _sys.path:
            _sys.path.insert(0, agent_root)
        from receiver.source_reader import read_source as _read_source
        svc  = request.args.get("service", "")
        file = request.args.get("file", "")
        line = int(request.args.get("line", 0) or 0)
        ctx  = int(request.args.get("context", 25) or 25)
        if not svc or not file:
            return Response(
                json.dumps({"error": "service and file params required"}),
                status=400, mimetype="application/json",
            )
        data = _read_source(svc, file, line=line, context=ctx)
        return Response(json.dumps(data), status=200, mimetype="application/json")

    @app.post("/api/assessment/trigger")
    def assessment_trigger():
        running = is_assessment_running()
        queued = trigger_event.is_set()
        if running and queued:
            return Response(
                json.dumps({"status": "already_pending", "message": "Assessment is running and another is already queued."}),
                status=200, mimetype="application/json",
            )
        if queued:
            return Response(
                json.dumps({"status": "already_pending", "message": "A triggered run is already queued."}),
                status=200, mimetype="application/json",
            )
        trigger_event.set()
        logger.info("Assessment trigger received via API — waking up batch loop.")
        if running:
            return Response(
                json.dumps({"status": "queued_after_current", "message": "Assessment is running — your request will execute immediately after."}),
                status=202, mimetype="application/json",
            )
        return Response(
            json.dumps({"status": "triggered", "message": "Assessment starting shortly."}),
            status=202, mimetype="application/json",
        )

    return app


_protobuf_warned = False


def _parse_body() -> dict | None:
    """
    Parse OTLP/HTTP body. Only JSON is supported.

    Production gateways default to protobuf encoding. To ensure this receiver
    gets parseable data, add `encoding: json` to the otlp/http exporter in your
    gateway config (already included in the gateway-patch-configmap.yaml Helm template).
    """
    global _protobuf_warned
    try:
        ct = request.content_type or ""
        if "json" in ct or not ct:
            return request.get_json(force=True, silent=True)
        if "protobuf" in ct or "octet-stream" in ct:
            if not _protobuf_warned:
                logger.warning(
                    "OTLP receiver received protobuf-encoded payload (content-type: %s). "
                    "This receiver only supports JSON encoding. "
                    "Add 'encoding: json' to your gateway otlp/http exporter config — "
                    "see the gateway-patch-configmap.yaml for the correct snippet. "
                    "Telemetry will NOT be processed until encoding is switched to JSON. "
                    "(This warning logs once per process.)",
                    ct,
                )
                _protobuf_warned = True
            return {}  # return empty success so gateway doesn't retry
        logger.debug("Received unrecognised content-type: %s — skipping", ct)
        return {}
    except Exception as exc:
        logger.warning("Failed to parse request body: %s", exc)
        return None


def start_receiver(
    pipeline: "StreamingPipeline",
    port: int = 4318,
    host: str = "0.0.0.0",
    environment: str = "",
) -> threading.Thread:
    """
    Start the OTLP receiver in a daemon thread.
    Returns the thread so the caller can join it if needed.
    """
    app = create_app(pipeline, environment=environment)

    def _serve():
        logger.info("OTLP/HTTP receiver listening on %s:%d", host, port)
        try:
            # Use werkzeug's production server — not dev server
            from werkzeug.serving import make_server
            srv = make_server(host, port, app, threaded=True)
            srv.serve_forever()
        except Exception as exc:
            logger.error("OTLP receiver failed: %s", exc, exc_info=True)

    thread = threading.Thread(target=_serve, daemon=True, name="otlp-receiver")
    thread.start()
    return thread
