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

from flask import Flask, Response, request

if TYPE_CHECKING:
    from streaming.pipeline import StreamingPipeline

logger = logging.getLogger(__name__)


def create_app(pipeline: "StreamingPipeline") -> Flask:
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

    @app.get("/v1/logs")
    @app.post("/v1/logs")
    def receive_logs():
        # Accept but don't process logs — avoids 404 errors in gateway
        return Response(
            json.dumps({"partialSuccess": {}}),
            status=200,
            mimetype="application/json",
        )

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
) -> threading.Thread:
    """
    Start the OTLP receiver in a daemon thread.
    Returns the thread so the caller can join it if needed.
    """
    app = create_app(pipeline)

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
