# Autonomous O11y Agent — Roadmap

## Auto-Remediation Pipeline

### Phase 1 — AI Fix Button (Complete)
AI Fix button in the profiling UI. Human identifies a hotspot, clicks, reviews the
LLM-generated fix. All the intelligence exists; human provides the trigger and final approval.

**Built:** `receiver/fix_generator.py`, `/api/fix` endpoint, source viewer + issue badges in profiling UI.

---

### Phase 2 — Autonomous Detection + GitHub PR (Near-term)
Trigger fires automatically when a slow span arrives AND snapshot profiling data exists for
its trace_id. Confidence gate before generating the fix and opening a GitHub PR. Human reviews
and merges.

**What needs to be built:**
- Trigger hook in `streaming/pipeline.py` → `process_resource_spans()` checks `snapshot_store.has_data(service, trace_id)` for spans above a latency threshold
- Confidence scoring: `contribution_pct > 20%`, `traces_affected > N`, issue type must be LLM-amenable (Sync I/O, Serial Awaits)
- GitHub API integration: create branch, commit suggested change, open PR with LLM explanation as PR body
- Deduplication: track (service, file, line) hotspots that already have an open PR, with 24h cooldown

---

### Phase 3 — Post-Deploy Validation (Medium-term)
After a fix is merged and deployed, the agent monitors subsequent profiling data to confirm
it worked. Escalates to human review if the fix made things worse.

**What needs to be built:**
- Remediation history store: persist (service, file, line, fix, deploy_timestamp) to disk so it survives container restarts
- Validation loop: after deploy, compare contribution_pct and traces_affected before vs. after
- Success signal: blocking frame disappears from snapshot call stacks, contribution_pct drops toward 0%
- Failure signal: contribution_pct unchanged or increased → escalate, comment on PR

---

### Phase 4 — Direct Deploy for High-Confidence Fixes (Longer-term)
Narrow class of mechanical rewrites (sync→async, serial→parallel awaits) pushed directly to
a deploy branch and CI/CD without human code review.

**Prerequisites:**
- Phase 3 must be stable and validated (feedback loop proven reliable)
- Strict confidence thresholds (contribution_pct > 50%, issue type == sync_io or serial_awaits only)
- Rollback mechanism in place (auto-revert if P99 latency increases after deploy)

---

## Method Hotspots

### Phase 1 — Current State (Complete)
- Aggregated contribution% across all traces equally
- `traces_affected` count and `total_traces`
- `worst_trace_id` + "View Worst" jump to trace
- App only / All filter (hides library/framework frames)
- `avg_self_time_ms` estimate (sample_count × 10ms)

### Phase 2 — P50 vs P99 Split (Scoped, not started)
Aggregate slow outlier traces separately from typical traces. Surface methods that appear
disproportionately in slow tail requests vs. normal requests.

**What needs to be built:**
- Span duration index: store root span duration keyed by trace_id as spans flow through `process_resource_spans()`
- Update `snapshot_store.get_hotspots()` to split aggregation: slow traces (above P90 duration) vs. typical
- UI: two contribution bars side-by-side, or a "slow-only" filter toggle

### Phase 3 — Caller/Callee Breakdown (Scoped, not started)
For each hot method, show the full call chain — who calls it and what it calls — not just the
single `app_frame` above it. Achievable with current data (full stacks already stored).

**What needs to be built:**
- Update `get_hotspots()` to return the top N caller frames per method (already in `stacks_per_key`)
- UI: expandable caller chain row beneath each hotspot entry

### Phase 4 — Before/After Comparison Mode (Scoped, not started)
Select two time windows and diff the hotspot rankings. What appeared, what disappeared, what
got worse. Directly useful for validating auto-remediation fixes (Phase 3 above) and
correlating regressions to deploys.

**Prerequisite:** persistent storage beyond 30-minute in-memory window (see Storage section below).

### Phase 5 — Thread/Wait State Breakdown (Scoped, not started)
Separate CPU time, I/O wait, and lock wait per method. Currently all sample types are
aggregated equally. For Python services, `threading.wait` dominates — the breakdown would
distinguish lock contention from network wait, which have different fixes.

### Phase 6 — External Call Hotspots View (Scoped, not started)
Dedicated tab for outbound calls (DB queries, gRPC, HTTP). Methods like `makeUnaryRequest`
currently appear as `unknown` category. Rank by latency contribution separately from
CPU hotspots.

### Phase 7 — Call Tree View (Scoped, not started)
Hierarchical call tree from the HTTP handler entry point down to the hot method, instead of
a flat ranked list. Shows the full execution path. Full stacks already stored; requires a
tree-building aggregation and a new UI component.

### Phase 8 — Time Series / Trend View (Scoped, not started)
Show contribution% as a trend over time — is this method getting worse after a deploy?
**Prerequisite:** persistent storage beyond 30-minute window.

---

## Storage Persistence (Cross-cutting prerequisite)

Several roadmap items above are blocked by the in-memory-only storage design. Both
`profiling_store` and `snapshot_store` reset on container restart and only retain 30 minutes.

**What needs to be built:**
- SQLite (or append-only flat file) persistence for snapshot records, keyed by (service, trace_id, arrived_at)
- Retention policy: keep raw records for 7 days, aggregated hotspot summaries for 30 days
- On startup, load persisted records back into the in-memory store
- Enables: historical custom time ranges, before/after comparison, trend views, remediation history

---

## CPU Utilization Detection (Not started)

The profiling system currently detects *which functions* are consuming the most CPU (relative
hotspots) and classifies their code patterns. It does not detect whether a service is actually
CPU-stressed at the infrastructure level (absolute utilization).

**The gap:** pprof data tells us the shape of CPU consumption, not the magnitude. A service can
have a clean flamegraph and still be CPU-saturated, or have a messy flamegraph but be completely
fine because it handles low traffic. For auto-remediation Phase 2 to have a reliable trigger,
both signals need to be combined.

**What needs to be built:**
- In `streaming/pipeline.py` → `process_resource_metrics()`: check host CPU utilization metrics
  (already flowing through the OTel Collector) against a per-service threshold (e.g. >80%)
- When threshold is exceeded, cross-reference `profiling_store.get_flamegraph()` for that service
  to identify the responsible functions
- Combined signal (high CPU% + known hotspot) becomes a high-confidence trigger for Phase 2
  auto-remediation — replacing the current latency-only trigger
- Surface in the profiling UI as a CPU utilization indicator alongside the flamegraph

**Why this matters for auto-remediation:**
High CPU + known code hotspot = high confidence that fixing the hotspot will directly reduce
CPU load. Without the CPU utilization signal, the trigger is latency-based only and may fire
on I/O-bound slowness where a code fix won't help.

---

## Memory Profiler (Not yet scoped)

V8 heap sampling profiler + heap metrics timeline. Not yet scoped in detail.
Planned as a parallel workstream independent of the CPU profiling roadmap.
