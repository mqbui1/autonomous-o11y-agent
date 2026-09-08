# o11y-agent-14b Demo Runbook — Management / Cross-Team

**Goal:** show the self-contained fine-tuned 14B model (no cloud LLM dependency)
correlating cross-domain root causes on the Astronomy Shop demo app, then closing
the loop by applying a remediation live through the Splunk OTel Supervisor.

**Pre-req:** `o11y-agent` stack running with `LLM_PROVIDER=ollama`,
`OLLAMA_MODEL=o11y-agent-14b`, pointed at a dedicated (uncontended) Ollama
instance. Supervisor running alongside with `SUPERVISOR_MODE=HUMAN_REQUIRED`.
Confirm `astroshop-local` load generator has been running ≥10min before starting
(cold traffic gives false "NO DATA" results).

**Fallback:** latency varies with local Ollama load — have a recorded run of each
scenario ready in case live inference is slow during the actual presentation.

Total time budget: ~12-15 min live + narration.

---

## Scenario 1 — Opener: `paymentFailure100` (simple single fault, ~2 min)

**Talking point:** "Single fault, clean root cause, model runs entirely on our own
hardware — no AWS/Bedrock call in this loop."

```bash
cd autonomous-o11y-agent
python3 - deploy/demo.flagd.json paymentFailure 100% <<'PYEOF'
import json, sys
path, flag, variant = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    data = json.load(f)
data["flags"][flag]["defaultVariant"] = variant
with open(path, "w") as f:
    json.dump(data, f, indent=2)
PYEOF
# wait ~75s for flagd hot-reload + traffic to reflect the fault
```

Run the agent (or point at the already-running watch-mode container's latest
assessment). Call out: Payments specialist flags the failure rate, RCA
specialist traces it back to the injected fault with a causal chain, Detector
specialist would fire a real alert.

**Revert before moving on:**
```bash
python3 - deploy/demo.flagd.json paymentFailure off <<'PYEOF'
import json, sys
path, flag, variant = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    data = json.load(f)
data["flags"][flag]["defaultVariant"] = variant
with open(path, "w") as f:
    json.dump(data, f, indent=2)
PYEOF
```

---

## Scenario 2 — Sophistication showcase: `compound_checkout` (~4 min)

Two independent faults fired together — cart AND payment both failing at 50%.

**Talking point:** "This is the scenario that used to break the local model
during our reliability testing — it now passes clean in our full 17-scenario
parity suite. The model has to correlate two independent failures into one
checkout-flow story instead of two separate anonymous outages."

```bash
python3 - deploy/demo.flagd.json cartFailure 50% <<'PYEOF'
import json, sys
path, flag, variant = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f: data = json.load(f)
data["flags"][flag]["defaultVariant"] = variant
with open(path, "w") as f: json.dump(data, f, indent=2)
PYEOF
python3 - deploy/demo.flagd.json paymentFailure 50% <<'PYEOF'
import json, sys
path, flag, variant = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f: data = json.load(f)
data["flags"][flag]["defaultVariant"] = variant
with open(path, "w") as f: json.dump(data, f, indent=2)
PYEOF
# wait ~75s
```

Call out: multiple specialists (Cart/Payments findings, RCA cross-referencing
both, Detector correlating alerts) converge on one coherent narrative instead
of the audience having to mentally merge two dashboards.

**Revert both flags to `off` the same way before the next scenario.**

---

## Scenario 3 — ROI/toil argument: `emailMemoryLeak1000x` (~2 min)

**Talking point:** "This is the slow-burn case traditional threshold alerting
misses — a leak that's fine right now but will page someone at 3am in a few
days. The agent catches the trend today, not after the outage."

```bash
python3 - deploy/demo.flagd.json emailMemoryLeak 1000x <<'PYEOF'
import json, sys
path, flag, variant = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f: data = json.load(f)
data["flags"][flag]["defaultVariant"] = variant
with open(path, "w") as f: json.dump(data, f, indent=2)
PYEOF
```

Highlight the Performance/Instrumentation specialist output flagging the trend
and projected time-to-impact, not just a snapshot number.

**Revert to `off`.**

---

## Closer — Remediation loop (~3 min, the "feels like a product" moment)

With any of the above findings on screen, switch to the Supervisor UI:

1. Open the **Pending Remediations** panel — the same finding just shown now
   appears as a concrete, actionable card.
2. Point out `SUPERVISOR_MODE=HUMAN_REQUIRED` — human-in-the-loop is visible
   and intentional, not a black box.
3. Click **Apply** live.
4. Show the actual result landing (config diff / detector created / whatever
   the remediation produced) — proves it's a real action, not a mockup.

**Closing line:** "Everything you just saw — detection, correlation, root
cause, and the fix — ran on infrastructure we control, with a model we trained
ourselves. No external API dependency in the loop."

---

## Cleanup after the demo

Confirm all flags reverted to `off`:
```bash
grep -A2 "defaultVariant" deploy/demo.flagd.json
```
Any flag still non-`off` should be manually reset the same way as above.
