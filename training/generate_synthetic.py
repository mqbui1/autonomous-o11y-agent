"""
Generate a large synthetic training dataset of Splunk Observability Cloud
instruction-following examples for fine-tuning a small local model.

Covers every domain the o11y-agent operates in:
  - APM service health & topology
  - Detector quality assessment
  - Profiling flamegraph interpretation
  - Exception root-cause analysis
  - Code fix generation
  - Log analysis
  - RUM / synthetic monitoring
  - Database & external dependency health
  - Instrumentation coverage gaps
  - SignalFlow / detector creation

Output: training/data/synthetic.jsonl  (~500+ examples)
Merge with training/data/train.jsonl before fine-tuning.

Usage:
    python3 training/generate_synthetic.py
    python3 training/generate_synthetic.py --count 1000 --output training/data/synthetic.jsonl
"""

import argparse
import json
import pathlib
import random

OUT_PATH = pathlib.Path(__file__).parent / "data" / "synthetic.jsonl"

SYSTEM_BASE = (
    "You are an autonomous observability specialist for Splunk Observability Cloud. "
    "You analyse APM traces, profiling data, detectors, logs, and service topology to "
    "identify issues, explain root causes, and recommend precise fixes. "
    "Respond in clear, structured prose. When producing JSON output follow the schema exactly."
)

# ── Template factories ────────────────────────────────────────────────────────

def _svc():
    return random.choice([
        "frontend", "payment", "recommendation", "checkout", "cart",
        "shipping", "product-catalog", "order-service", "inventory",
        "notification", "auth-service", "api-gateway", "fraud-detection",
        "pricing-engine", "search-service",
    ])

def _env():
    return random.choice(["production", "staging", "dev", "us1-prod", "eu-prod"])

def _exc():
    return random.choice([
        "grpc.StatusRuntimeException", "io.grpc._ChannelNotFound",
        "redis.exceptions.ConnectionError", "psycopg2.OperationalError",
        "java.lang.NullPointerException", "requests.exceptions.Timeout",
        "sqlalchemy.exc.OperationalError", "kafka.errors.NoBrokersAvailable",
        "com.mongodb.MongoTimeoutException", "java.net.SocketTimeoutException",
    ])

def _fn():
    return random.choice([
        "PlaceOrder", "ProcessPayment", "GetRecommendations", "AddToCart",
        "CheckInventory", "ShipOrder", "AuthenticateUser", "SearchProducts",
        "CalculatePrice", "SendNotification", "QueryDatabase", "CacheGet",
    ])

def _ms():
    return random.randint(120, 4500)

def _pct():
    return round(random.uniform(5, 55), 1)

def _detector_name():
    return random.choice([
        "High Error Rate", "P99 Latency Spike", "Service Unreachable",
        "Anomalous Throughput Drop", "Database Saturation", "Memory Pressure",
        "CPU Runaway", "Cache Hit Rate Degradation", "Queue Depth Critical",
        "Error Budget Burn Rate",
    ])

def _make_example(user_text: str, assistant_text: str) -> dict:
    return {
        "messages": [
            {"role": "system",  "content": SYSTEM_BASE},
            {"role": "user",    "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
    }

# ── Domain generators ─────────────────────────────────────────────────────────

def gen_service_health():
    svc = _svc(); env = _env(); p99 = _ms(); err = round(random.uniform(1.2, 18), 1)
    return _make_example(
        f"Assess the health of the {svc} service in {env}. "
        f"Current metrics: P99 latency {p99}ms (baseline 180ms), error rate {err}%, "
        f"throughput 420 rpm (baseline 430 rpm).",
        f"## {svc} health assessment — {env}\n\n"
        f"**Status: {'DEGRADED' if p99 > 500 or err > 5 else 'WARNING'}**\n\n"
        f"### Latency\nP99 at {p99}ms is "
        f"{'{'}{round(p99/180, 1)}x baseline ({'critical — investigate immediately' if p99 > 1000 else 'elevated'}){'}'} "
        f"Likely cause: downstream dependency blocking the request thread. "
        f"Check APM trace waterfall for the longest child span.\n\n"
        f"### Error rate\n{err}% error rate "
        f"({'exceeds 5% SLO — incidents likely firing' if err > 5 else 'approaching SLO threshold'}). "
        f"Examine exception traces for recurring error types. "
        f"Top candidate: connection timeout or upstream 5xx propagation.\n\n"
        f"### Throughput\nWithin normal range — no traffic anomaly. "
        f"This rules out a traffic spike as the root cause.\n\n"
        f"### Recommended next steps\n"
        f"1. Open APM trace explorer, filter to {svc}, sort by duration — inspect the slowest trace.\n"
        f"2. Check Service Map for red edges from {svc} to its dependencies.\n"
        f"3. Review detectors: confirm 'High Error Rate' fired and alert was routed correctly."
    )

def gen_detector_ghost():
    det = _detector_name(); svc = _svc(); days = random.randint(14, 180)
    return _make_example(
        f"Evaluate this detector: '{det}' targeting {svc}. Last fired: {days} days ago. "
        f"Current state: OK. SignalFlow uses a static threshold of 500ms with no dynamic baseline.",
        f"## Detector evaluation: '{det}'\n\n"
        f"**Finding: Potential ghost detector**\n\n"
        f"This detector has not fired in {days} days. Possible explanations:\n\n"
        f"1. **Threshold too high** — static 500ms threshold may never be breached if "
        f"{svc} P99 runs at 120-200ms. The alert would only fire during catastrophic failures, "
        f"missing gradual regressions.\n"
        f"2. **Service retired** — {svc} may no longer exist in this environment.\n"
        f"3. **Metric name changed** — an OTel SDK upgrade may have renamed the metric, "
        f"causing the SignalFlow to evaluate against no data (silent OK).\n\n"
        f"**Recommended fix:**\n"
        f"Replace the static threshold with a dynamic baseline:\n"
        f"```\nA = data('service.duration.p99', filter=filter('service.name', '{svc}'))\n"
        f"B = A.percentile(pct=99, over='1h')\n"
        f"detect(when(A > B * 1.5, lasting='5m')).publish('{det}')\n```\n\n"
        f"Also add `has_no_data_alert=True` so a silent metric surface as an alert rather than a ghost OK."
    )

def gen_flamegraph_hotspot():
    svc = _svc(); fn = _fn(); pct = _pct(); ms = _ms()
    lib = random.choice(["grpc", "psycopg2", "redis", "requests", "kafka", "pymongo"])
    return _make_example(
        f"Interpret this AlwaysOn CPU flamegraph hotspot for {svc}:\n"
        f"Top frame: {lib}.recv() — {pct}% CPU, ~{ms}ms self-time per sample.\n"
        f"App caller: {fn}() at /app/src/service.py:142",
        f"## Flamegraph analysis — {svc}\n\n"
        f"**Issue: Synchronous I/O blocking the thread in {fn}()**\n\n"
        f"`{lib}.recv()` is a blocking network call that holds the thread for ~{ms}ms per request. "
        f"At {pct}% CPU share, it dominates the profile — every sampled request is spending "
        f"significant time waiting for a network response.\n\n"
        f"**Root cause:** `{fn}()` at `service.py:142` is calling `{lib}` synchronously. "
        f"In a {'gRPC' if lib == 'grpc' else lib} context, this means the thread cannot serve "
        f"other requests during this wait.\n\n"
        f"**Fix options:**\n"
        f"1. **Connection pooling** — verify a connection pool is configured with adequate size. "
        f"Without pooling, each request opens a new connection ({ms}ms includes TCP handshake).\n"
        f"2. **Async rewrite** — replace synchronous call with `asyncio`-compatible client to "
        f"allow the event loop to serve other requests during the wait.\n"
        f"3. **Caching** — if `{fn}` is called repeatedly with the same inputs, cache the result "
        f"in Redis or an in-process LRU cache.\n\n"
        f"**Estimated impact:** Fixing connection overhead alone typically reduces P99 by 40-60% "
        f"for I/O-bound services."
    )

def gen_exception_rca():
    svc = _svc(); exc = _exc(); msg = random.choice([
        "Connection refused", "Deadline exceeded", "No route to host",
        "too many connections", "UNAVAILABLE: upstream connect error",
        "context deadline exceeded", "connection pool exhausted",
    ])
    return _make_example(
        f"Root-cause this exception from {svc}:\n"
        f"Type: {exc}\nMessage: {msg}\n"
        f"Stack: {svc}/app/handler.py:89 → client.call() → transport.send()",
        f"## Exception root-cause: {exc}\n\n"
        f"**Root cause: downstream service or infrastructure failure**\n\n"
        f"`{exc}: {msg}` indicates the {svc} service is unable to reach a dependency. "
        f"The stack trace shows the failure originates in `transport.send()`, meaning "
        f"the application code in `handler.py:89` issued a valid call but the network layer failed.\n\n"
        f"**Most likely causes (in order of probability):**\n"
        f"1. Target service is down or restarting — check APM Service Map for red edges.\n"
        f"2. Connection pool exhausted — too many concurrent requests, pool size too small.\n"
        f"3. Network policy / firewall change — recent infrastructure change blocking the port.\n"
        f"4. DNS resolution failure — Kubernetes service name changed.\n\n"
        f"**Diagnostic steps:**\n"
        f"1. Open APM, find this trace, inspect the inferred downstream span — "
        f"the error tags will show the target service and port.\n"
        f"2. Check if the error rate spiked at a specific time (deployment? HPA scale-down?).\n"
        f"3. Confirm connection pool configuration: `pool_size`, `max_overflow`, `pool_timeout`.\n\n"
        f"**Fix:** Add retry with exponential backoff + circuit breaker around `client.call()`. "
        f"Set a pool_size of at least 2× expected concurrent RPS."
    )

def gen_signalflow_create():
    svc = _svc(); metric = random.choice([
        "service.duration", "http.server.request.duration",
        "db.client.operation.duration", "rpc.server.duration",
        "system.cpu.utilization", "jvm.memory.used",
    ])
    threshold = random.choice([500, 1000, 2000, 95, 90])
    return _make_example(
        f"Write SignalFlow for a P99 latency detector on {svc} that fires when P99 exceeds "
        f"{threshold}ms for 5 consecutive minutes, with a 15-minute sensitivity baseline.",
        f"```signalflow\n"
        f"from signalfx.detectors.against_recent import against_recent\n\n"
        f"A = data('{metric}', filter=(\n"
        f"    filter('service.name', '{svc}') and\n"
        f"    filter('sf_environment', '*')\n"
        f"), rollup='p99').mean(over='1m')\n\n"
        f"# Static threshold alert\n"
        f"threshold = {threshold}\n"
        f"detect(\n"
        f"    when(A > threshold, lasting='5m'),\n"
        f"    off=when(A < threshold * 0.9, lasting='2m'),\n"
        f"    annotations=[annotate('P99 latency', parameters={{'threshold': threshold}})],\n"
        f").publish(\n"
        f"    label='{svc} P99 Latency Critical',\n"
        f"    event_annotations={{'runbook': 'https://wiki/runbooks/{svc}-latency'}}\n"
        f")\n```\n\n"
        f"**Notes:**\n"
        f"- `lasting='5m'` prevents flapping — condition must hold for 5 consecutive minutes.\n"
        f"- `off=when(... < threshold * 0.9 ...)` adds 10% hysteresis to avoid alert thrashing.\n"
        f"- Use `rollup='p99'` on the data call to get histogram percentiles, not averages.\n"
        f"- Add `filter('sf_environment', 'production')` to avoid staging noise if needed."
    )

def gen_instrumentation_gap():
    svc = _svc(); span_count = random.randint(1, 3); total = random.randint(15, 40)
    return _make_example(
        f"Assess instrumentation quality for {svc}. "
        f"APM data: {span_count} span types received (expected ~{total}), "
        f"no db.statement attributes, no http.url on outbound spans, "
        f"service.version missing from resource attributes.",
        f"## Instrumentation gap analysis — {svc}\n\n"
        f"**Coverage: {round(span_count/total*100)}% — POOR**\n\n"
        f"Only {span_count} of ~{total} expected span types are present. "
        f"This means {total - span_count} internal operations are invisible in APM.\n\n"
        f"### Specific gaps\n\n"
        f"**1. Missing `db.statement`** — Database queries are not captured. "
        f"Slow query identification is impossible. "
        f"Fix: enable `OTEL_INSTRUMENTATION_DB_CAPTURE_STATEMENT=true` (Python) or "
        f"`dbStatementSerializer` (Node.js).\n\n"
        f"**2. Missing `http.url` on outbound spans** — Cannot trace which downstream endpoint "
        f"is slow. Fix: upgrade `opentelemetry-instrumentation-requests` to ≥0.45b0.\n\n"
        f"**3. Missing `service.version`** — Cannot correlate latency changes with deployments. "
        f"Fix: set `OTEL_RESOURCE_ATTRIBUTES=service.version=${{IMAGE_TAG}}` in the deployment manifest.\n\n"
        f"### Priority\n"
        f"Add `db.statement` first — it provides the highest immediate diagnostic value "
        f"for performance issues in most services."
    )

def gen_log_analysis():
    svc = _svc(); count = random.randint(1200, 50000); pattern = random.choice([
        "Connection timeout after 30s",
        "Failed to acquire lock: deadlock detected",
        "WARN: retry attempt 3 of 3 — giving up",
        "ERROR: upstream returned 503",
        "NullPointerException at line 234",
    ])
    return _make_example(
        f"Analyse error logs for {svc}: {count} error events in the last 15 minutes. "
        f"Top pattern (72% of errors): '{pattern}'",
        f"## Log analysis — {svc}\n\n"
        f"**{count} errors in 15 minutes is {'critical' if count > 10000 else 'elevated'} — "
        f"{'~{} errors/second'.format(round(count/900))}**\n\n"
        f"### Top pattern: `{pattern}`\n\n"
        f"This single pattern accounts for 72% of all errors, making it the primary incident driver. "
        f"This is not noise — it is a systematic failure.\n\n"
        f"**Interpretation:**\n"
        + (
            "Connection timeout indicates a downstream service or infrastructure component is "
            "unreachable or overloaded. Check APM Service Map for red edges and verify "
            "connection pool configuration."
            if "timeout" in pattern.lower() else
            "Retry exhaustion means the client attempted the operation multiple times before "
            "giving up. The underlying failure (503, lock, etc.) is the real cause. "
            "Investigate the dependency that is failing, not just the retries."
        ) + "\n\n"
        f"### Action items\n"
        f"1. Identify when the error spike started (correlate with deployments, cron jobs, traffic changes).\n"
        f"2. Isolate one trace where this error occurs — trace the full call path.\n"
        f"3. Check if a detector for `{svc}` error rate is firing — if not, create one with "
        f"SignalFlow: `data('logs.errors', filter=filter('service.name', '{svc}')).sum(over='1m')`."
    )

def gen_rum_assessment():
    page = random.choice(["/checkout", "/product/:id", "/cart", "/home", "/search"])
    cls = round(random.uniform(0.12, 0.85), 2)
    lcp = random.randint(2800, 8500)
    return _make_example(
        f"Evaluate RUM performance for '{page}': "
        f"LCP {lcp}ms (target <2500ms), CLS {cls} (target <0.1), "
        f"FID 280ms, error rate 3.2%.",
        f"## RUM assessment — `{page}`\n\n"
        f"**Core Web Vitals: FAILING** — all three metrics exceed Google's 'good' thresholds.\n\n"
        f"| Metric | Value | Target | Status |\n"
        f"|--------|-------|--------|--------|\n"
        f"| LCP    | {lcp}ms | <2500ms | {'POOR' if lcp > 4000 else 'NEEDS IMPROVEMENT'} |\n"
        f"| CLS    | {cls}  | <0.1   | {'POOR' if cls > 0.25 else 'NEEDS IMPROVEMENT'} |\n"
        f"| FID    | 280ms | <100ms | POOR |\n\n"
        f"### LCP ({lcp}ms)\nLarge Contentful Paint at {lcp}ms means users wait over "
        f"{round(lcp/1000, 1)}s before the main content appears. "
        f"Primary causes: unoptimised hero image, render-blocking JS, slow server response. "
        f"Check the RUM waterfall — identify the largest element and its load time.\n\n"
        f"### CLS ({cls})\nLayout shift of {cls} is {'severe' if cls > 0.25 else 'noticeable'} — "
        f"elements are moving after initial render. Common cause: images/ads without explicit dimensions.\n\n"
        f"### FID (280ms)\n280ms main-thread blocking indicates heavy JavaScript execution "
        f"during page load. Profile with Chrome DevTools — look for synchronous XHR or large parsing tasks.\n\n"
        f"**Impact:** Poor CWV reduces Google search ranking and increases bounce rate."
    )

def gen_db_assessment():
    svc = _svc(); db = random.choice(["PostgreSQL", "MySQL", "MongoDB", "Redis", "DynamoDB"])
    query_ms = random.randint(800, 5000)
    return _make_example(
        f"Assess database health for {svc} connecting to {db}. "
        f"Slowest query: {query_ms}ms avg, 50 calls/min. No `db.statement` attribute captured. "
        f"Connection pool: size=5, max_overflow=0, pool timeout errors: 12/min.",
        f"## Database health — {svc} → {db}\n\n"
        f"**Two distinct problems: slow queries AND connection pool exhaustion.**\n\n"
        f"### Problem 1: Pool exhaustion (pool timeout errors: 12/min)\n"
        f"With pool_size=5 and max_overflow=0, only 5 concurrent connections are allowed. "
        f"At 50 calls/min with {query_ms}ms avg duration, steady-state concurrency = "
        f"50 * {query_ms}/60000 = {round(50 * query_ms / 60000, 1)} connections. "
        f"{'This **exceeds** pool capacity' if round(50 * query_ms / 60000) >= 5 else 'This is near pool capacity'}. "
        f"**Fix:** Increase `pool_size` to at least {max(10, round(50 * query_ms / 60000) * 2)} "
        f"and set `max_overflow=10`.\n\n"
        f"### Problem 2: Slow query ({query_ms}ms)\n"
        f"No `db.statement` attribute means the specific query cannot be identified from APM. "
        f"**Fix:** Enable statement capture:\n"
        f"- SQLAlchemy: `create_engine(..., echo=True)` or set `OTEL_INSTRUMENTATION_DB_CAPTURE_STATEMENT=true`\n"
        f"- Then sort APM spans by `db.operation` to isolate the slow query.\n\n"
        f"**Query optimisation candidates:** missing index on a high-cardinality filter column, "
        f"N+1 query pattern (many small queries instead of one batch query)."
    )

def gen_service_map_topology():
    svc = _svc(); dep1 = _svc(); dep2 = _svc()
    while dep1 == svc: dep1 = _svc()
    while dep2 == svc or dep2 == dep1: dep2 = _svc()
    return _make_example(
        f"Map the dependency topology for {svc}. "
        f"Known outbound spans: gRPC call to {dep1}, HTTP call to an inferred service "
        f"(no instrumentation — shows as grey node), Redis GET/SET operations.",
        f"## Service dependency topology — {svc}\n\n"
        f"```\n{svc}\n"
        f"  ├─ gRPC → {dep1} [instrumented ✓]\n"
        f"  ├─ HTTP → (inferred-{dep2}) [NOT instrumented ⚠]\n"
        f"  └─ Redis → cache layer [instrumented ✓]\n"
        f"```\n\n"
        f"### Instrumented dependencies\n"
        f"- **{dep1}** — gRPC spans present, P99 visible in APM. Topology complete.\n"
        f"- **Redis** — GET/SET spans captured. Cache hit rate and latency visible.\n\n"
        f"### Blind spots\n"
        f"- **Inferred `{dep2}`** — {svc} makes outbound HTTP calls to a service that has no "
        f"OTel instrumentation. This appears as a grey 'inferred' node in the Service Map. "
        f"If this service degrades, {svc} will show elevated errors with no root-cause trace.\n\n"
        f"**Action:** Instrument the `{dep2}` service with `opentelemetry-auto-instrumentation` "
        f"and propagate the W3C trace context header (`traceparent`) on all outbound calls from {svc}."
    )

def gen_memory_allocation():
    svc = _svc(); fn = _fn(); kb = random.randint(200, 2048); pct = _pct()
    return _make_example(
        f"Interpret trace-correlated heap allocation data for {svc}, trace abc123:\n"
        f"Top allocation site: {fn}() at /app/handler.js:78 — {kb}KB ({pct}% of trace allocations)",
        f"## Heap allocation analysis — {svc} trace abc123\n\n"
        f"**{fn}() is the dominant allocation site, consuming {pct}% of all heap allocations in this trace.**\n\n"
        f"`{fn}()` at `handler.js:78` allocated {kb}KB in a single request. "
        f"{'This is **abnormally high** — ' if kb > 512 else 'This is elevated — '}"
        f"typical request allocations should be under 50KB for most handler functions.\n\n"
        f"**Likely causes:**\n"
        f"1. **Large in-memory collection** — building a full result set in memory before streaming "
        f"(e.g., `Array.from(cursor)` over a large dataset).\n"
        f"2. **Object creation in a loop** — allocating new objects inside `.map()` or `for..of` "
        f"over a large array, holding all in memory simultaneously.\n"
        f"3. **Unintended serialisation** — `JSON.stringify()` of a large object graph creates "
        f"a string as large as the object itself.\n\n"
        f"**Fix:** Open `handler.js:78`, check what data structure is being constructed. "
        f"Replace bulk-in-memory patterns with streaming pagination: return a cursor or use "
        f"async generators to process data in chunks."
    )

def gen_apm_slowest_trace():
    svc = _svc(); total_ms = _ms() * 3; fn = _fn()
    child_ms = round(total_ms * 0.72)
    return _make_example(
        f"Analyse the slowest APM trace for {svc} (duration: {total_ms}ms):\n"
        f"- {fn}() root span: {total_ms}ms\n"
        f"- grpc.call to payment: {child_ms}ms (72% of trace)\n"
        f"- db.query: 48ms\n"
        f"- cache.get: 12ms",
        f"## Slowest trace analysis — {svc}\n\n"
        f"**{total_ms}ms total — dominated by a single downstream gRPC call ({child_ms}ms, 72%)**\n\n"
        f"The trace waterfall is clear: `{fn}()` blocks for {child_ms}ms waiting for a gRPC "
        f"response from the payment service. Database ({48}ms) and cache ({12}ms) operations "
        f"are fast and not the issue.\n\n"
        f"### Root cause\nThe gRPC call to payment is the bottleneck. Options:\n\n"
        f"1. **Payment service is slow internally** — drill into payment's APM traces "
        f"during this time window. Check payment's P99 and its own downstream calls.\n"
        f"2. **Network / connection overhead** — if payment's own traces show fast execution "
        f"but {svc} waits {child_ms}ms, the overhead is in connection establishment. "
        f"Enable gRPC keepalive and ensure connection pooling.\n"
        f"3. **Sequential calls that could be parallel** — if {fn}() calls payment "
        f"synchronously before using the result, verify there isn't a parallel opportunity.\n\n"
        f"**Next step:** Click into the payment span in the APM trace waterfall to see "
        f"payment's internal spans and identify where time is spent."
    )

def gen_detector_noisy():
    det = _detector_name(); svc = _svc(); fires = random.randint(50, 500)
    return _make_example(
        f"Evaluate noisy detector: '{det}' for {svc}. "
        f"Fired {fires} times in the last 7 days. Team has muted it. "
        f"SignalFlow: static threshold, no `lasting` clause.",
        f"## Noisy detector evaluation: '{det}'\n\n"
        f"**{fires} alerts in 7 days = {round(fires/7)} per day. This is alert fatigue.**\n\n"
        f"The detector has been muted — meaning real incidents will be missed. "
        f"This is more dangerous than the noise itself.\n\n"
        f"### Root cause of noise\nMissing `lasting` clause. Without it, any single data point "
        f"above the threshold fires an alert — including momentary spikes during rolling deploys, "
        f"GC pauses, and autoscaler events.\n\n"
        f"### Fix\n```signalflow\n"
        f"A = data('service.duration.p99', filter=filter('service.name', '{svc}')).mean(over='1m')\n"
        f"detect(\n"
        f"    when(A > threshold, lasting='5m'),     # must hold for 5m\n"
        f"    off=when(A < threshold * 0.85, lasting='3m')  # hysteresis on recovery\n"
        f").publish('{det}')\n```\n\n"
        f"Add `lasting='5m'` to require the condition to hold continuously before firing. "
        f"This eliminates transient spikes. After applying:\n"
        f"1. Unmute the detector.\n"
        f"2. Monitor for 48 hours — if still noisy, raise the threshold or switch to "
        f"a dynamic baseline using `against_recent`."
    )

def gen_health_score():
    svc = _svc()
    score = random.randint(20, 85)
    sev = "critical" if score < 40 else ("warning" if score < 65 else "healthy")
    return _make_example(
        f"Generate a JSON health summary for {svc} with score {score}.",
        json.dumps({
            "severity": sev,
            "score": score,
            "summary": (
                f"{svc} is in a {sev} state. "
                + ("Immediate attention required — multiple signals indicate active degradation." if sev == "critical" else
                   "Performance is below baseline with elevated error signals." if sev == "warning" else
                   "Service is operating within normal parameters.")
            ),
            "findings": [
                {
                    "severity": "critical" if score < 40 else "warning",
                    "title": "P99 latency elevated" if score < 65 else "CPU idle profile healthy",
                    "detail": (
                        f"P99 at {random.randint(500, 3000)}ms, {round(random.uniform(2,4),1)}x above baseline."
                        if score < 65 else
                        f"AlwaysOn profiling shows no hot paths. CPU profile is distributed normally."
                    )
                },
                {
                    "severity": "warning" if score < 65 else "info",
                    "title": "Exception rate elevated" if score < 65 else "Exception rate nominal",
                    "detail": (
                        f"{random.randint(10, 200)} errors in the last 15 minutes."
                        if score < 65 else
                        "< 0.1% error rate, no recurring exception types."
                    )
                }
            ]
        }, indent=2)
    )

def _tc(name: str, args: dict, tc_id: str | None = None) -> dict:
    """Build an assistant tool-call turn."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": tc_id or f"call_{random.randint(100000, 999999)}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    }


def _tr(content, tc_id: str) -> dict:
    """Build a tool-result turn."""
    return {
        "role": "tool",
        "content": json.dumps(content) if not isinstance(content, str) else content,
        "tool_call_id": tc_id,
    }


def _sf(answer: dict) -> dict:
    """Build a submit_findings tool-call turn."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": f"call_{random.randint(100000, 999999)}",
            "type": "function",
            "function": {"name": "submit_findings", "arguments": json.dumps(answer)},
        }],
    }


def gen_submit_findings():
    """
    Multi-turn training examples: tool call → JSON result → submit_findings.

    The model sees realistic JSON tool output and learns to extract a
    concise, direct summary — never starting with 'Based on...' or
    'According to...'.  Summary rules baked into every example:
      • Start with a noun/number (e.g. "8 services active…")
      • 2–4 sentences, no explanatory preamble
      • Never: "Based on", "According to", "The data shows", "It seems"
    """
    domain = random.choice([
        "health", "instrumentation", "governance", "detector",
        "logs", "rum", "rca", "synthetics", "db", "performance",
    ])
    svc = _svc()
    env = _env()

    if domain == "health":
        active = [_svc() for _ in range(random.randint(2, 5))]
        silent = [_svc() for _ in range(random.randint(0, 2))]
        healthy = random.randint(10, 30)
        critical = random.randint(0, 3)
        tc1_id = f"call_{random.randint(100000, 999999)}"
        tc2_id = f"call_{random.randint(100000, 999999)}"
        apm_result = {"services_active": active, "services_silent": silent,
                      "total_services": len(active) + len(silent)}
        det_result = {"detectors_healthy": healthy, "detectors_critical": critical,
                      "open_incidents": critical}
        issues = []
        if critical:
            issues.append({"severity": "critical", "domain": "health",
                           "service": active[0] if active else svc,
                           "description": f"{critical} detectors critical with open P1 incidents.",
                           "recommendation": f"Investigate active incidents for {active[0] if active else svc}."})
        if silent:
            issues.append({"severity": "high", "domain": "health", "service": silent[0],
                           "description": f"{silent[0]} emits no telemetry.",
                           "recommendation": "Verify OTEL_EXPORTER_OTLP_ENDPOINT in deployment manifest."})
        answer = {
            "summary": (
                f"{len(active)} services active, {len(silent)} silent. "
                f"{healthy} detectors healthy, {critical} critical."
                + (f" {len(silent)} service(s) dark — no spans or metrics received." if silent else "")
            ),
            "services_active": active, "services_silent": silent, "issues": issues,
            "metrics": {"detectors_healthy": healthy, "detectors_critical": critical,
                        "silent_service_count": len(silent)},
        }
        msgs = [
            {"role": "system", "content": SYSTEM_BASE},
            {"role": "user", "content": f"You are the health specialist for {env}. Assess environment health and call submit_findings."},
            _tc("check_apm_health", {}, tc1_id),
            _tr(apm_result, tc1_id),
            _tc("check_detector_health", {}, tc2_id),
            _tr(det_result, tc2_id),
            _sf(answer),
        ]

    elif domain == "instrumentation":
        score = random.randint(25, 90)
        coverage = round(score * 0.95, 1)
        gaps = random.sample(["db.statement", "http.url", "service.version",
                               "exception.stacktrace", "http.route"], k=random.randint(1, 3))
        tc1_id = f"call_{random.randint(100000, 999999)}"
        result = {"instrumentation_score": score, "span_coverage_pct": coverage,
                  "missing_attributes": gaps, "service": svc}
        answer = {
            "summary": (
                f"Instrumentation score {score}/100 — {'poor' if score < 50 else 'fair' if score < 75 else 'good'}. "
                f"{coverage}% span coverage. "
                f"Missing: {', '.join(gaps)}."
            ),
            "instrumentation_score": score,
            "issues": [{
                "severity": "high" if score < 50 else "medium",
                "domain": "instrumentation", "service": svc,
                "description": f"Missing {gaps[0]} — reduces APM diagnostic capability.",
                "recommendation": f"Enable {gaps[0]} capture: set OTEL_INSTRUMENTATION_DB_CAPTURE_STATEMENT=true or upgrade SDK to ≥0.45b0.",
            }],
            "metrics": {"score": score, "span_coverage_pct": coverage},
        }
        msgs = [
            {"role": "system", "content": SYSTEM_BASE},
            {"role": "user", "content": f"You are the instrumentation specialist for {env}. Assess SDK coverage and call submit_findings."},
            _tc("analyze_instrumentation", {}, tc1_id),
            _tr(result, tc1_id),
            _sf(answer),
        ]

    elif domain == "governance":
        top_mts = random.randint(5000, 500000)
        anomalies = random.randint(0, 8)
        top_metric = random.choice(["http.server.duration", "k8s.pod.cpu.usage",
                                     "db.client.operation.duration"])
        tc1_id = f"call_{random.randint(100000, 999999)}"
        result = {"top_metric": top_metric, "top_mts": top_mts,
                  "anomaly_count": anomalies, "over_budget": top_mts > 50000}
        answer = {
            "summary": (
                f"{top_metric} has {top_mts:,} MTS — "
                f"{'exceeds 50k limit' if top_mts > 50000 else 'within normal range'}. "
                f"{anomalies} cardinality anomalies detected."
            ),
            "issues": ([{
                "severity": "high" if top_mts > 100000 else "medium",
                "domain": "governance",
                "description": f"{top_metric} at {top_mts:,} MTS — high-cardinality dimension inflating cost.",
                "recommendation": "Add MetricTransform processor drop rule for the high-cardinality dimension.",
            }] if top_mts > 50000 else []),
            "metrics": {"top_cardinality_mts": top_mts, "anomaly_count": anomalies,
                        "top_metrics": [top_metric]},
        }
        msgs = [
            {"role": "system", "content": SYSTEM_BASE},
            {"role": "user", "content": f"You are the governance specialist for {env}. Scan cardinality and call submit_findings."},
            _tc("full_cardinality_scan", {}, tc1_id),
            _tr(result, tc1_id),
            _sf(answer),
        ]

    elif domain == "detector":
        deployed = random.randint(5, 40)
        dark = random.randint(0, 5)
        broken = random.randint(0, 3)
        tc1_id = f"call_{random.randint(100000, 999999)}"
        result = {"detectors_deployed": deployed, "detectors_broken": broken,
                  "dark_services": dark, "environment": env}
        answer = {
            "summary": (
                f"{deployed} detectors deployed. "
                f"{broken} dark (evaluating against no data). "
                f"{dark} services have no detector coverage."
            ),
            "issues": ([{
                "severity": "high", "domain": "detector", "service": svc,
                "description": f"{broken} detectors evaluate against no data — metric names may have changed.",
                "recommendation": "Audit SignalFlow metric names against current OTel semantic conventions.",
            }] if broken > 0 else []),
            "metrics": {"deployed_count": deployed, "dark_service_count": dark},
        }
        msgs = [
            {"role": "system", "content": SYSTEM_BASE},
            {"role": "user", "content": f"You are the detector specialist for {env}. Audit detector health and call submit_findings."},
            _tc("audit_detectors", {}, tc1_id),
            _tr(result, tc1_id),
            _sf(answer),
        ]

    elif domain == "logs":
        err_count = random.randint(0, 50000)
        pattern = random.choice([
            "Connection timeout after 30s", "Failed to acquire lock",
            "upstream returned 503", "NullPointerException at line 234",
        ])
        tc1_id = f"call_{random.randint(100000, 999999)}"
        if err_count == 0:
            result = {"status": 404, "error": "Log Observer not configured"}
            answer = {
                "summary": "Log Observer returned 404 — log pipeline not configured for this environment.",
                "issues": [{"severity": "high", "domain": "logs",
                             "description": "Log Observer API returned 404; HEC log pipeline missing.",
                             "recommendation": "Configure Splunk OTel Collector log pipeline with Splunk HEC exporter."}],
            }
        else:
            result = {"error_count": err_count, "window_minutes": 15,
                      "top_pattern": pattern, "top_pattern_pct": 68}
            answer = {
                "summary": (
                    f"{err_count:,} error events in 15 minutes — "
                    f"{'critical volume' if err_count > 10000 else 'elevated'}. "
                    f"'{pattern}' accounts for 68% of errors."
                ),
                "issues": [{
                    "severity": "critical" if err_count > 10000 else "high",
                    "domain": "logs", "service": svc,
                    "description": f"'{pattern}' is the dominant error pattern ({round(err_count * 0.68):,} occurrences).",
                    "recommendation": "Correlate with APM traces to find the root-cause service.",
                }],
            }
        msgs = [
            {"role": "system", "content": SYSTEM_BASE},
            {"role": "user", "content": f"You are the logs specialist for {env}. Analyse log volume and call submit_findings."},
            _tc("get_log_volume", {}, tc1_id),
            _tr(result, tc1_id),
            _sf(answer),
        ]

    elif domain == "rum":
        no_data = random.random() < 0.4
        lcp = random.randint(1800, 7000)
        tc1_id = f"call_{random.randint(100000, 999999)}"
        if no_data:
            result = {"apps": [], "error": "No RUM applications found"}
            answer = {
                "summary": "No RUM applications reporting data. RUM snippet likely not deployed.",
                "issues": [{"severity": "medium", "domain": "rum",
                             "description": "No RUM apps found — frontend performance unobservable.",
                             "recommendation": "Add Splunk RUM JS snippet with beaconUrl and rumAuth."}],
            }
        else:
            result = {"apps": ["storefront", "checkout"], "lcp_ms": lcp,
                      "cls": 0.18, "fid_ms": 280, "error_rate_pct": 2.1}
            answer = {
                "summary": (
                    f"2 RUM apps active. LCP={lcp}ms "
                    f"({'exceeds 2500ms target' if lcp > 2500 else 'within target'}). "
                    "CLS=0.18 exceeds 0.1 target."
                ),
                "issues": ([{
                    "severity": "medium", "domain": "rum",
                    "description": f"LCP={lcp}ms exceeds 2500ms threshold.",
                    "recommendation": "Profile hero image and render-blocking JS in RUM waterfall.",
                }] if lcp > 2500 else []),
            }
        msgs = [
            {"role": "system", "content": SYSTEM_BASE},
            {"role": "user", "content": f"You are the RUM specialist for {env}. Check RUM metrics and call submit_findings. Do NOT retry on error."},
            _tc("list_rum_apps", {}, tc1_id),
            _tr(result, tc1_id),
            _sf(answer),
        ]

    elif domain == "rca":
        n_incidents = random.randint(0, 3)
        tc1_id = f"call_{random.randint(100000, 999999)}"
        if n_incidents == 0:
            result = {"incidents": [], "open_count": 0}
            answer = {
                "summary": f"No active incidents in {env}. All services within SLO.",
                "issues": [],
            }
        else:
            deploy_ver = f"v{random.randint(1,4)}.{random.randint(0,9)}.{random.randint(0,9)}"
            p99_ms = random.randint(1800, 5000)
            result = {"incidents": [{"id": f"INC-{random.randint(1000,9999)}",
                                      "service": svc, "p99_ms": p99_ms,
                                      "trigger": f"deployment {deploy_ver}"}],
                      "open_count": n_incidents}
            answer = {
                "summary": (
                    f"{n_incidents} active incident(s). "
                    f"{svc} P99 spiked to {p99_ms}ms after deployment {deploy_ver}. "
                    "Regression introduced in latest release."
                ),
                "issues": [{
                    "severity": "critical", "domain": "rca", "service": svc,
                    "description": f"{svc} P99 regressed to {p99_ms}ms (baseline 180ms) after {deploy_ver}.",
                    "recommendation": f"Roll back {deploy_ver} or hotfix; compare flamegraphs before/after.",
                }],
            }
        msgs = [
            {"role": "system", "content": SYSTEM_BASE},
            {"role": "user", "content": f"You are the RCA specialist for {env}. Check active incidents and call submit_findings."},
            _tc("get_active_incidents", {}, tc1_id),
            _tr(result, tc1_id),
            _sf(answer),
        ]

    elif domain == "synthetics":
        api_err = random.random() < 0.3
        tests = random.randint(2, 10)
        failing = random.randint(0, 3)
        tc1_id = f"call_{random.randint(100000, 999999)}"
        if api_err:
            result = {"status": 402, "error": "Entitlement not enabled"}
            answer = {
                "summary": "Synthetics not available — 402 Entitlement error. Synthetic monitoring not licensed.",
                "issues": [{"severity": "medium", "domain": "synthetics",
                             "description": "Synthetic monitoring returns 402 — not licensed.",
                             "recommendation": "Enable Splunk Synthetic Monitoring entitlement."}],
            }
        else:
            result = {"tests_total": tests, "tests_failing": failing,
                      "tests_passing": tests - failing}
            answer = {
                "summary": (
                    f"{tests} synthetic tests configured. "
                    + (f"{failing} failing — endpoint unreachable or assertion failure."
                       if failing else "All passing.")
                ),
                "issues": ([{
                    "severity": "high", "domain": "synthetics",
                    "description": f"{failing} synthetic tests failing.",
                    "recommendation": "Review failing test HTTP status codes; check PoP reachability.",
                }] if failing else []),
            }
        msgs = [
            {"role": "system", "content": SYSTEM_BASE},
            {"role": "user", "content": f"You are the synthetics specialist for {env}. List tests and call submit_findings."},
            _tc("list_synthetics_tests", {}, tc1_id),
            _tr(result, tc1_id),
            _sf(answer),
        ]

    elif domain == "db":
        no_db = random.random() < 0.3
        query_ms = random.randint(400, 4000)
        tc1_id = f"call_{random.randint(100000, 999999)}"
        tc2_id = f"call_{random.randint(100000, 999999)}"
        if no_db:
            result1 = {"services_with_db_spans": [], "count": 0}
            answer = {
                "summary": f"No DB instrumentation in {env}. Database query performance is unobservable.",
                "issues": [{"severity": "medium", "domain": "db",
                             "description": "No db.* span attributes found — slow queries unidentifiable.",
                             "recommendation": "Enable db.statement capture: OTEL_INSTRUMENTATION_DB_CAPTURE_STATEMENT=true."}],
            }
            msgs = [
                {"role": "system", "content": SYSTEM_BASE},
                {"role": "user", "content": f"You are the DB specialist for {env}. Find DB-instrumented services and call submit_findings."},
                _tc("find_db_instrumented_services", {}, tc1_id),
                _tr(result1, tc1_id),
                _sf(answer),
            ]
        else:
            result1 = {"services_with_db_spans": [svc], "slowest_query_ms": query_ms}
            result2 = {"service": svc, "target": "postgres", "error_rate_pct": 4.2}
            answer = {
                "summary": (
                    f"{svc} has DB instrumentation. "
                    f"Slowest query: {query_ms}ms — "
                    f"{'critical' if query_ms > 2000 else 'elevated'}. "
                    "Postgres connection error rate: 4.2%."
                ),
                "issues": [{
                    "severity": "high" if query_ms > 1000 else "medium",
                    "domain": "db", "service": svc,
                    "description": f"DB query P99={query_ms}ms; postgres error rate 4.2%.",
                    "recommendation": "Check for missing indexes; increase connection pool size.",
                }],
            }
            msgs = [
                {"role": "system", "content": SYSTEM_BASE},
                {"role": "user", "content": f"You are the DB specialist for {env}. Analyse DB health and call submit_findings."},
                _tc("find_db_instrumented_services", {}, tc1_id),
                _tr(result1, tc1_id),
                _tc("get_outbound_call_error_rates", {}, tc2_id),
                _tr(result2, tc2_id),
                _sf(answer),
            ]

    else:  # performance
        p99 = _ms()
        err = round(random.uniform(0.5, 12.0), 1)
        downstream_ms = round(p99 * 0.7)
        tc1_id = f"call_{random.randint(100000, 999999)}"
        tc2_id = f"call_{random.randint(100000, 999999)}"
        result1 = {"service": svc, "p99_ms": p99, "error_rate_pct": err,
                   "throughput_rpm": random.randint(200, 800)}
        result2 = {"top_slow_call": {"target": _svc(), "p99_ms": downstream_ms,
                                      "error_rate_pct": round(err * 0.6, 1)}}
        answer = {
            "summary": (
                f"{svc} P99={p99}ms ({round(p99/180, 1)}x above 180ms baseline). "
                f"Error rate {err}%"
                f"{' — exceeds 5% SLO' if err > 5 else ''}. "
                f"Downstream call at {downstream_ms}ms is {round(downstream_ms/p99*100)}% of trace time."
            ),
            "issues": [{
                "severity": "critical" if p99 > 2000 or err > 8 else "high",
                "domain": "performance", "service": svc,
                "description": f"{svc} P99={p99}ms; downstream call at {downstream_ms}ms is primary bottleneck.",
                "recommendation": "Inspect APM trace waterfall for slowest downstream span; add connection pooling or circuit breaker.",
            }],
        }
        msgs = [
            {"role": "system", "content": SYSTEM_BASE},
            {"role": "user", "content": f"You are the performance specialist for {env}. Assess {svc} and call submit_findings."},
            _tc("check_apm_health", {}, tc1_id),
            _tr(result1, tc1_id),
            _tc("search_slow_outbound_calls", {}, tc2_id),
            _tr(result2, tc2_id),
            _sf(answer),
        ]

    return {"messages": msgs}


# ── Master list of generators ────────────────────────────────────────────────

GENERATORS = [
    (gen_service_health,      8),
    (gen_detector_ghost,      7),
    (gen_flamegraph_hotspot,  8),
    (gen_exception_rca,       7),
    (gen_signalflow_create,   6),
    (gen_instrumentation_gap, 6),
    (gen_log_analysis,        6),
    (gen_rum_assessment,      5),
    (gen_db_assessment,       6),
    (gen_service_map_topology,5),
    (gen_memory_allocation,   5),
    (gen_apm_slowest_trace,   7),
    (gen_detector_noisy,      6),
    (gen_health_score,        4),
    (gen_submit_findings,    12),  # highest weight — fixes prose bleed-through
]

# Weighted pool
_POOL = [fn for fn, weight in GENERATORS for _ in range(weight)]


def generate(n: int) -> list[dict]:
    random.seed(42)
    examples = []
    for _ in range(n):
        fn = random.choice(_POOL)
        examples.append(fn())
    return examples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count",  type=int, default=600,
                        help="Number of synthetic examples to generate (default: 600)")
    parser.add_argument("--output", default=str(OUT_PATH),
                        help=f"Output JSONL path (default: {OUT_PATH})")
    parser.add_argument("--merge",  action="store_true",
                        help="Merge with existing train.jsonl → merged_train.jsonl")
    args = parser.parse_args()

    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    examples = generate(args.count)
    with out.open("w") as f:
        for ex in examples:
            f.write(json.dumps(ex) + "\n")

    print(f"Generated {len(examples)} synthetic examples → {out}")

    if args.merge:
        real = pathlib.Path(__file__).parent / "data" / "train.jsonl"
        merged = pathlib.Path(__file__).parent / "data" / "merged_train.jsonl"
        lines = []
        if real.exists():
            lines = real.read_text().splitlines()
            print(f"Loaded {len(lines)} real examples from {real}")
        with merged.open("w") as f:
            for l in lines:
                f.write(l + "\n")
            for ex in examples:
                f.write(json.dumps(ex) + "\n")
        print(f"Merged → {merged}  ({len(lines) + len(examples)} total examples)")


if __name__ == "__main__":
    main()
