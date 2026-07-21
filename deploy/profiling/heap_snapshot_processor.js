/**
 * Trace-correlated heap allocation SpanProcessor for Node.js (V8 HeapProfiler).
 *
 * Loaded via --require before @splunk/otel/instrument. Intercepts
 * ProxyTracerProvider.setDelegate to inject HeapSnapshotProcessor into the
 * real TracerProvider once the Splunk distro initialises it.
 *
 * For each span: starts V8 heap sampling on onStart, stops it on onEnd,
 * and ships the allocation diff as a json-alloc-v1 OTLP log record.
 *
 * Wire format matches heap_snapshot_collector.py — both feed the same
 * snapshot_store.observe_allocation() path in the agent.
 */

'use strict';

if (process.env.HEAP_SNAPSHOT_ENABLED !== 'true') {
  return;  // no-op when not enabled
}

const inspector = require('inspector');
const http      = require('http');
const https     = require('https');

const OTLP_ENDPOINT   = (process.env.OTEL_EXPORTER_OTLP_ENDPOINT || 'http://localhost:4318').replace(/\/$/, '');
const SERVICE_NAME    = process.env.OTEL_SERVICE_NAME || 'unknown';
const TOP_FRAMES      = parseInt(process.env.HEAP_SNAPSHOT_TOP_FRAMES || '10', 10);
const MIN_SIZE_BYTES  = parseInt(process.env.HEAP_SNAPSHOT_MIN_SIZE_KB || '16', 10) * 1024;
const SAMPLING_INTERVAL = 65536;  // 64 KB — low overhead

// One inspector session; reused across all spans.
const session = new inspector.Session();
session.connect();

// Active sampling contexts keyed by span-id hex string.
// V8 HeapProfiler is process-wide, so we track allocation deltas via
// periodic snapshots rather than per-span start/stop (which would fight
// each other under concurrency). For simplicity at demo scale (low
// concurrency) we do start/stop per span and accept that concurrent
// spans will share sampling time.
const _spanProfiles = new Map();  // spanId → { startAllocs: Map<key, bytes> }

// ── HeapSnapshotProcessor ────────────────────────────────────────────────────

class HeapSnapshotProcessor {
  onStart(span, _parentCtx) {
    const sid = spanIdHex(span);
    if (!sid) return;
    // Take a baseline allocation snapshot at span start
    try {
      const baseline = sampleAllocations();
      _spanProfiles.set(sid, { baseline, startMs: Date.now() });
    } catch (_) {}
  }

  onEnd(span) {
    const sid = spanIdHex(span);
    if (!sid) return;
    const ctx = _spanProfiles.get(sid);
    _spanProfiles.delete(sid);
    if (!ctx) return;

    const traceId = traceIdHex(span);
    if (!traceId) return;

    try {
      const endAllocs = sampleAllocations();
      const frames    = diffAllocations(ctx.baseline, endAllocs);
      if (frames.length > 0) {
        emitAllocationLog(traceId, frames, span).catch(() => {});
      }
    } catch (_) {}
  }

  shutdown()               { return Promise.resolve(); }
  forceFlush()             { return Promise.resolve(); }
}

// ── V8 heap sampling ─────────────────────────────────────────────────────────

let _samplingActive = false;

function ensureSampling() {
  if (_samplingActive) return;
  _samplingActive = true;
  try {
    session.post('HeapProfiler.startSampling', { samplingInterval: SAMPLING_INTERVAL });
  } catch (_) {}
}

function sampleAllocations() {
  ensureSampling();
  return new Promise((resolve, reject) => {
    session.post('HeapProfiler.getSamplingProfile', (err, result) => {
      if (err) return reject(err);
      resolve(parseHeapProfile(result.profile));
    });
  });
}

function parseHeapProfile(profile) {
  // Returns Map<"file:fn:line" → {size, count, fn, file, line}>
  const allocs = new Map();
  if (!profile || !profile.head) return allocs;

  function walk(node) {
    const { callFrame, selfSize } = node;
    if (selfSize > 0) {
      const fn   = callFrame.functionName || '(anonymous)';
      const file = callFrame.url          || 'unknown';
      const line = callFrame.lineNumber   || 0;
      const key  = `${file}:${fn}:${line}`;
      const existing = allocs.get(key);
      if (existing) {
        existing.size  += selfSize;
        existing.count += 1;
      } else {
        allocs.set(key, { fn, file, line, size: selfSize, count: 1 });
      }
    }
    if (node.children) node.children.forEach(walk);
  }
  walk(profile.head);
  return allocs;
}

function diffAllocations(baseline, end) {
  // Returns top frames by (end.size - baseline.size), filtered by MIN_SIZE_BYTES
  const diff = [];
  for (const [key, endEntry] of end) {
    const baseSize = baseline.has(key) ? baseline.get(key).size : 0;
    const delta    = endEntry.size - baseSize;
    if (delta >= MIN_SIZE_BYTES) {
      diff.push({
        function:   endEntry.fn,
        file:       endEntry.file,
        line:       endEntry.line,
        size_bytes: delta,
        count:      endEntry.count - (baseline.has(key) ? baseline.get(key).count : 0),
      });
    }
  }
  diff.sort((a, b) => b.size_bytes - a.size_bytes);
  return diff.slice(0, TOP_FRAMES);
}

// ── OTLP emit ────────────────────────────────────────────────────────────────

async function emitAllocationLog(traceId, frames, span) {
  const body = JSON.stringify({ trace_id: traceId, frames });

  const logRecord = {
    timeUnixNano: String(BigInt(Date.now()) * 1000000n),
    body:         { stringValue: body },
    attributes: [
      strAttr('com.splunk.sourcetype',            'otel.profiling'),
      strAttr('profiling.data.type',              'allocation'),
      strAttr('profiling.data.format',            'json-alloc-v1'),
      strAttr('profiling.instrumentation.source', 'snapshot'),
    ],
  };

  const payload = {
    resourceLogs: [{
      resource: { attributes: [strAttr('service.name', SERVICE_NAME)] },
      scopeLogs: [{
        scope:      { name: 'heap_snapshot_processor' },
        logRecords: [logRecord],
      }],
    }],
  };

  await postJson(`${OTLP_ENDPOINT}/v1/logs`, payload);
}

function strAttr(key, value) {
  return { key, value: { stringValue: value } };
}

function postJson(url, payload) {
  return new Promise((resolve) => {
    const data    = Buffer.from(JSON.stringify(payload));
    const parsed  = new URL(url);
    const lib     = parsed.protocol === 'https:' ? https : http;
    const opts    = {
      hostname: parsed.hostname,
      port:     parsed.port || (parsed.protocol === 'https:' ? 443 : 80),
      path:     parsed.pathname,
      method:   'POST',
      headers:  {
        'Content-Type':   'application/json',
        'Content-Length': data.length,
      },
      timeout: 2000,
    };
    const req = lib.request(opts, (res) => {
      res.resume();
      resolve();
    });
    req.on('error', resolve);
    req.on('timeout', () => { req.destroy(); resolve(); });
    req.write(data);
    req.end();
  });
}

// ── Span ID helpers ───────────────────────────────────────────────────────────

function spanIdHex(span) {
  try {
    const ctx = span.spanContext ? span.spanContext() : span._spanContext;
    return ctx && ctx.spanId ? ctx.spanId : '';
  } catch (_) { return ''; }
}

function traceIdHex(span) {
  try {
    const ctx = span.spanContext ? span.spanContext() : span._spanContext;
    return ctx && ctx.traceId ? ctx.traceId : '';
  } catch (_) { return ''; }
}

// ── Injection into the real TracerProvider via ProxyTracerProvider.setDelegate ──

try {
  const { trace } = require('@opentelemetry/api');
  const proxy = trace.getTracerProvider();

  if (proxy && typeof proxy.setDelegate === 'function') {
    const _origSetDelegate = proxy.setDelegate.bind(proxy);
    proxy.setDelegate = function (delegate) {
      _origSetDelegate(delegate);
      try {
        if (delegate && typeof delegate.addSpanProcessor === 'function') {
          delegate.addSpanProcessor(new HeapSnapshotProcessor());
        }
      } catch (_) {}
    };
  }
} catch (_) {
  // @opentelemetry/api not yet available at require time — that's fine,
  // the Splunk distro will call setDelegate when it initialises.
}
