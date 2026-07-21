#!/usr/bin/env python3
"""
Automated training pipeline orchestrator.

Monitors labeled assessment counts and advances through phases defined in
pipeline_config.yaml. At each phase boundary, applies environment fixes,
optionally exports train.jsonl, and optionally triggers fine-tuning.

Run alongside auto_labeler.py:
    python3 auto_labeler.py &
    python3 training_pipeline.py

State persists in pipeline_state.json so the process can be restarted safely.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

SCRIPT_DIR     = Path(__file__).parent
STATE_FILE     = SCRIPT_DIR / "pipeline_state.json"
CONFIG_FILE    = SCRIPT_DIR / "pipeline_config.yaml"
EXPORT_DIR     = SCRIPT_DIR / "training_exports"
LOG_FILE       = SCRIPT_DIR / "pipeline.log"
COMPOSE_DIR    = SCRIPT_DIR          # docker compose runs from here
AGENT_DIR      = SCRIPT_DIR.parent   # autonomous-o11y-agent root
GALILEO_SCORES_FILE = SCRIPT_DIR / "galileo_scores.jsonl"  # written by auto_labeler.py

SUPERVISOR_URL = "http://localhost:9090/api"
POLL_INTERVAL  = 60  # seconds between checks


# ── Logging ───────────────────────────────────────────────────────────────────

def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def log(msg, level="INFO"):
    line = f"[{_ts()}] [{level}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")

def log_banner(msg):
    bar = "=" * 65
    log(bar)
    log(f"  {msg}")
    log(bar)


# ── State persistence ─────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "current_phase": 0,
        "phase_start_labeled": 0,   # total labeled count when phase started
        "phases_completed": [],
        "started_at": _ts(),
    }

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Supervisor / Agent API ────────────────────────────────────────────────────

def get_labeled_counts():
    """Return (total, approved, rejected) labeled examples."""
    try:
        r = requests.get(f"{SUPERVISOR_URL}/training/decisions", timeout=10)
        r.raise_for_status()
        d = r.json()
        return d.get("total", 0), d.get("approved", 0), d.get("rejected", 0)
    except Exception as e:
        log(f"Could not fetch decision counts: {e}", "WARN")
        return 0, 0, 0

def get_labeled_run_count():
    """Count distinct run_ids that have at least one label."""
    try:
        r = requests.get(f"{SUPERVISOR_URL}/training/decisions", timeout=10)
        r.raise_for_status()
        index = r.json().get("index", {})
        run_ids = {k.split("|")[0] for k in index}
        return len(run_ids)
    except Exception as e:
        log(f"Could not count labeled runs: {e}", "WARN")
        return 0

def _load_galileo_scores():
    """{(run_id, domain): scores_dict} from galileo_scores.jsonl, or {} if absent."""
    index = {}
    if not GALILEO_SCORES_FILE.exists():
        return index
    with GALILEO_SCORES_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            run_id, domain, scores = rec.get("run_id"), rec.get("domain"), rec.get("scores")
            if run_id and domain and scores is not None:
                index[(run_id, domain)] = scores
    return index


def _enrich_with_galileo_scores(out_path):
    """Add a 'galileo_scores' field to each line of an exported train.jsonl,
    matched by (run_id, domain), so the raw scores travel with the label
    into fine-tuning instead of only the binary approve/reject decision."""
    scores_index = _load_galileo_scores()
    if not scores_index:
        return 0

    lines = out_path.read_text().splitlines()
    enriched = 0
    with out_path.open("w") as f:
        for line in lines:
            if not line.strip():
                continue
            rec = json.loads(line)
            scores = scores_index.get((rec.get("run_id"), rec.get("domain")))
            if scores is not None:
                rec["galileo_scores"] = scores
                enriched += 1
            f.write(json.dumps(rec) + "\n")
    return enriched


def export_jsonl():
    """Download train.jsonl from supervisor and save to export dir."""
    EXPORT_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = EXPORT_DIR / f"train_{ts}.jsonl"
    try:
        r = requests.get(f"{SUPERVISOR_URL}/training/export", timeout=120, stream=True)
        r.raise_for_status()
        with out_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        lines = sum(1 for _ in out_path.open())
        log(f"Exported {lines} training examples → {out_path}")
        enriched = _enrich_with_galileo_scores(out_path)
        if enriched:
            log(f"Enriched {enriched}/{lines} examples with galileo_scores")
        return out_path
    except Exception as e:
        log(f"Export failed: {e}", "ERROR")
        return None


# ── Fix applicators ───────────────────────────────────────────────────────────

def _run(cmd, check=True, capture=False):
    log(f"  $ {cmd}")
    result = subprocess.run(
        cmd, shell=True, cwd=str(COMPOSE_DIR),
        capture_output=capture, text=True
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed (rc={result.returncode}): {cmd}")
    return result

def fix_governance_syntax(fix_cfg):
    """Fix the f-string backslash SyntaxError in cardinality_governance.py.

    In Python < 3.12, backslash sequences are forbidden inside f-string
    expression parts. Replace the problematic expression with string
    concatenation, which works across all Python versions.
    """
    rel_path = fix_cfg.get("file", "../../o11y-usage-governance/cardinality_governance.py")
    gov_path = (SCRIPT_DIR / rel_path).resolve()

    if not gov_path.exists():
        log(f"  governance file not found: {gov_path}", "WARN")
        return False

    content = gov_path.read_text(encoding="utf-8")

    # The buggy pattern (escaped quotes inside f-string expression):
    old = """f'<td>{det_html or "<span style=\\'color:var(--subtle);font-size:11px\\'>—</span>"}</td>'"""
    # Replacement: plain string concatenation, valid in Python 3.6+
    new = """('<td>' + (det_html or "<span style='color:var(--subtle);font-size:11px'>—</span>") + '</td>')"""

    if old not in content:
        # Try alternate quoting (file may use different escaping on disk)
        old_alt = 'f\'<td>{det_html or "<span style=\\\'color:var(--subtle);font-size:11px\\\'>—</span>"}</td>\''
        if old_alt not in content:
            log("  governance SyntaxError pattern not found — may already be fixed or has different form", "WARN")
            # Try a regex-based match as fallback
            pattern = r"""f'<td>\{det_html or "<span style=\\\'[^']*\\\'>[^']*</span>"\}</td>'"""
            if not re.search(pattern, content):
                log("  regex fallback also failed — skipping governance patch", "WARN")
                return False
            content = re.sub(pattern, new, content)
        else:
            content = content.replace(old_alt, new)
    else:
        content = content.replace(old, new)

    gov_path.write_text(content, encoding="utf-8")
    log(f"  patched: {gov_path}")
    return True


def _read_collector_config():
    cfg_path = SCRIPT_DIR / "otelcol-config.yml"
    return cfg_path, cfg_path.read_text(encoding="utf-8")

def _write_collector_config(content):
    cfg_path = SCRIPT_DIR / "otelcol-config.yml"
    cfg_path.write_text(content, encoding="utf-8")

def fix_collector_filter_span(fix_cfg):
    """Add a filter processor to drop a specific span name."""
    proc_name = fix_cfg["processor_name"]
    span_name  = fix_cfg["span_name"]

    cfg_path, content = _read_collector_config()

    if proc_name in content:
        log(f"  filter processor '{proc_name}' already present — skipping")
        return True

    # Insert processor definition after the 'processors:' key
    processor_block = (
        f"\n  {proc_name}:\n"
        f"    spans:\n"
        f"      exclude:\n"
        f"        match_type: strict\n"
        f"        span_names:\n"
        f'          - "{span_name}"\n'
    )
    content = content.replace("processors:\n", f"processors:{processor_block}", 1)

    # Add to the traces pipeline processors list
    content = re.sub(
        r"(processors: \[memory_limiter, batch, resourcedetection, resource/add_environment, transform/promote_env_to_span\])",
        rf"\1".replace(
            "transform/promote_env_to_span]",
            f"transform/promote_env_to_span, {proc_name}]"
        ),
        content
    )
    _write_collector_config(content)
    log(f"  added processor '{proc_name}' to collector config")
    return True


def fix_collector_pii_redaction(fix_cfg):
    """Add a redaction processor for private IPv4 addresses."""
    proc_name  = fix_cfg["processor_name"]
    attributes = fix_cfg.get("attributes", [])

    cfg_path, content = _read_collector_config()

    if proc_name in content:
        log(f"  redaction processor '{proc_name}' already present — skipping")
        return True

    attr_list = "\n".join(f'          - "{a}"' for a in attributes)
    processor_block = (
        f"\n  {proc_name}:\n"
        f"    allow_all_keys: true\n"
        f"    blocked_values:\n"
        f"      - '\\\\b10\\\\.\\\\d+\\\\.\\\\d+\\\\.\\\\d+\\\\b'\n"
        f"      - '\\\\b172\\\\.(1[6-9]|2\\\\d|3[01])\\\\.\\\\d+\\\\.\\\\d+\\\\b'\n"
        f"      - '\\\\b192\\\\.168\\\\.\\\\d+\\\\.\\\\d+\\\\b'\n"
        f"    blocked_keys:\n"
        f"{attr_list}\n"
        f"    summary: debug\n"
    )
    content = content.replace("processors:\n", f"processors:{processor_block}", 1)

    # Add to traces pipeline
    content = content.replace(
        "transform/promote_env_to_span]",
        f"transform/promote_env_to_span, {proc_name}]"
    )
    _write_collector_config(content)
    log(f"  added processor '{proc_name}' to collector config")
    return True


def fix_env_var(fix_cfg):
    """Update a key=value pair in deploy/.env."""
    key   = fix_cfg["key"]
    value = fix_cfg["value"]
    env_path = SCRIPT_DIR / ".env"
    content  = env_path.read_text()

    pattern = rf"^{re.escape(key)}=.*$"
    new_line = f"{key}={value}"

    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, new_line, content, flags=re.MULTILINE)
    else:
        content += f"\n{new_line}\n"

    env_path.write_text(content)
    log(f"  updated .env: {key}={value}")
    return True


def rebuild_service(fix_cfg):
    service = fix_cfg["service"]
    log(f"  rebuilding {service}...")
    _run(f"docker compose build {service}")
    _run(f"docker compose up -d {service}")
    log(f"  {service} rebuilt and restarted")
    return True


def restart_service(fix_cfg):
    service = fix_cfg["service"]
    log(f"  restarting {service}...")
    _run(f"docker compose up -d {service}")
    log(f"  {service} restarted")
    return True


def start_browser_simulator(fix_cfg):
    """Build the browser-sim image and start the container (profile: rum)."""
    log("  building browser-sim image (Playwright — first build may take 2-3 min)...")
    _run("docker compose --profile rum build browser-sim")
    log("  starting browser-sim container...")
    _run("docker compose --profile rum up -d browser-sim")
    log("  browser-sim running — RUM sessions will appear in Splunk within ~2 min")
    return True


def run_finetune(export_path):
    """Trigger MLX fine-tuning with the exported JSONL."""
    finetune_script = AGENT_DIR / "training" / "finetune.py"
    if not finetune_script.exists():
        log(f"Fine-tune script not found: {finetune_script}", "WARN")
        return False
    log(f"Starting fine-tuning with {export_path}...")
    result = _run(
        f"python3 {finetune_script} --mlx --data {export_path}",
        check=False,  # don't abort pipeline if fine-tune fails
        capture=True,
    )
    if result.returncode != 0:
        log(f"Fine-tune failed (rc={result.returncode}):\n{result.stderr}", "ERROR")
        return False
    log("Fine-tuning completed.")
    return True


FIX_HANDLERS = {
    "patch_governance_syntax":  fix_governance_syntax,
    "collector_filter_span":    fix_collector_filter_span,
    "collector_pii_redaction":  fix_collector_pii_redaction,
    "env_var":                  fix_env_var,
    "rebuild_service":          rebuild_service,
    "restart_service":          restart_service,
    "start_browser_simulator":  start_browser_simulator,
}


# ── Phase execution ───────────────────────────────────────────────────────────

def apply_phase_fixes(phase_cfg):
    fixes = phase_cfg.get("fixes", [])
    if not fixes:
        log("  (no fixes for this phase)")
        return

    for fix in fixes:
        fix_type = fix.get("type")
        desc     = fix.get("description", fix_type)
        log(f"  Applying: {desc}")
        handler = FIX_HANDLERS.get(fix_type)
        if not handler:
            log(f"  Unknown fix type '{fix_type}' — skipping", "WARN")
            continue
        try:
            ok = handler(fix)
            if ok:
                log(f"  ✓ {desc}")
            else:
                log(f"  ⚠ {desc} (no-op — already applied or pattern not found)", "WARN")
        except Exception as e:
            log(f"  ✗ {desc} FAILED: {e}", "ERROR")


def advance_phase(phases, state, total_labeled):
    phase_idx = state["current_phase"]
    phase_cfg = phases[phase_idx]

    log_banner(f"PHASE {phase_idx} COMPLETE: {phase_cfg['name'].upper()}")
    log(f"  Labeled runs in phase: {total_labeled - state['phase_start_labeled']}")
    log(f"  Total labeled examples: {total_labeled}")

    # Export JSONL if configured
    if phase_cfg.get("export_jsonl"):
        log("Exporting training data...")
        export_path = export_jsonl()
    else:
        export_path = None

    # Move to next phase
    next_idx = phase_idx + 1
    state["phases_completed"].append({
        "phase": phase_idx,
        "name": phase_cfg["name"],
        "completed_at": _ts(),
        "labeled_at_completion": total_labeled,
    })

    if next_idx >= len(phases):
        log_banner("ALL PHASES COMPLETE — pipeline finished")
        # Final fine-tune if the last phase had it enabled
        if phase_cfg.get("finetune") and export_path:
            run_finetune(export_path)
        state["current_phase"] = next_idx  # sentinel: done
        save_state(state)
        return False  # signal: stop

    # Apply fixes for the NEXT phase (they take effect before new runs are labeled)
    next_phase = phases[next_idx]
    log_banner(f"ENTERING PHASE {next_idx}: {next_phase['name'].upper()}")
    log(f"  {next_phase.get('description', '').strip()}")
    log(f"  Target: {next_phase['min_labeled_runs']} labeled runs")
    log("Applying fixes...")
    apply_phase_fixes(next_phase)

    # Fine-tune if configured on the completed phase
    if phase_cfg.get("finetune") and export_path:
        run_finetune(export_path)

    state["current_phase"]        = next_idx
    state["phase_start_labeled"]  = total_labeled
    save_state(state)
    return True  # signal: continue


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    config  = yaml.safe_load(CONFIG_FILE.read_text())
    phases  = config["phases"]
    state   = load_state()

    log_banner("TRAINING PIPELINE STARTED")
    log(f"  Config:  {CONFIG_FILE}")
    log(f"  State:   {STATE_FILE}")
    log(f"  Log:     {LOG_FILE}")
    log(f"  Phases:  {len(phases)}")
    log(f"  Current: phase {state['current_phase']} ({phases[state['current_phase']]['name'] if state['current_phase'] < len(phases) else 'DONE'})")
    log("")

    for i, p in enumerate(phases):
        marker = "→" if i == state["current_phase"] else ("✓" if i < state["current_phase"] else " ")
        log(f"  {marker} Phase {i}: {p['name']:35s} ({p['min_labeled_runs']} runs)")
    log("")

    if state["current_phase"] >= len(phases):
        log("Pipeline already complete. Delete pipeline_state.json to restart.")
        return

    while True:
        try:
            phase_idx = state["current_phase"]
            if phase_idx >= len(phases):
                log("Pipeline complete.")
                break

            phase_cfg     = phases[phase_idx]
            total_labeled = get_labeled_run_count()
            total_ex, approved, rejected = get_labeled_counts()
            phase_labeled = total_labeled - state["phase_start_labeled"]
            phase_target  = phase_cfg["min_labeled_runs"]
            remaining     = max(0, phase_target - phase_labeled)

            log(
                f"Phase {phase_idx} ({phase_cfg['name']}) | "
                f"runs in phase: {phase_labeled}/{phase_target} | "
                f"total examples: {total_ex} ({approved}✓ {rejected}✗) | "
                f"{'READY TO ADVANCE' if remaining == 0 else f'{remaining} runs to go'}"
            )

            if phase_labeled >= phase_target:
                ok = advance_phase(phases, state, total_labeled)
                if not ok:
                    break
                # Reload state after advance
                state = load_state()

        except KeyboardInterrupt:
            log("Pipeline interrupted by user.")
            break
        except Exception as e:
            log(f"Loop error: {e}", "ERROR")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
