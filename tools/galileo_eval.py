"""
Galileo-based eval engine for o11y-agent assessment auto-labeling.

Fully API-driven — no UI interaction. Batches every domain of a single
assessment run into one Galileo experiment, polls for scored results via
the SDK, and maps scores back to approve/reject decisions.

Used by deploy/auto_labeler.py when EVAL_ENGINE=galileo (the default).
Any failure here (auth, network, timeout, missing SDK) should be caught by
the caller, which falls back to the rule-based evaluate() in auto_labeler.py.

Requires: GALILEO_API_KEY, GALILEO_CONSOLE_URL env vars (see deploy/.env).
Project name: GALILEO_PROJECT env var (default: o11y-agent-eval).
"""

import importlib
import json
import os
import time

import galileo

PROJECT_NAME = os.environ.get("GALILEO_PROJECT", "o11y-agent-eval")

METRICS = ["context_adherence", "correctness", "completeness", "output_pii", "instruction_adherence"]
# reasoning_coherence was evaluated and deliberately excluded: it grades the
# span's structured `events` (step-by-step reasoning/tool-call trace), which
# we don't log (specialists only produce a final flat text blob). Empirically
# it always returns a trivial 1.0 ("no reasoning to evaluate against") —
# pure noise, not worth the API cost.

# metric_id -> friendly alias used in Galileo's flat span/trace result dicts
_ALIAS_FOR_METRIC = {
    "groundedness": "groundedness",     # context_adherence
    "factuality": "factuality",         # correctness
    "completeness_gpt": "completeness_gpt",
    "pii": "pii",                       # output_pii — trace-level only
    "instruction_adherence": "instruction_adherence",
}

MIN_SCORE = 0.5   # below this, groundedness/factuality is considered a failure

POLL_INTERVAL_SEC = 3
POLL_TIMEOUT_SEC = 180  # a full 10-domain run took ~90s in testing; give headroom

# Same universal garbage filters as the rule-based evaluate() — cheap to check
# before spending a Galileo API call on output that's clearly not real analysis.
_AGENT_ERROR_PATTERNS = [
    "agent error:",
    "An error occurred (ValidationException)",
    "An error occurred (ThrottlingException)",
    "An error occurred (ModelErrorException)",
    "An error occurred (ServiceUnavailableException)",
    "AWS credentials expired or invalid",
    "ExpiredTokenException",
    "UnrecognizedClientException",
    "Agent reached max turns without completing",
]


def _garbage_filter(raw):
    """Return (decision, reason) if raw output is obviously unusable, else None."""
    if not raw or len(raw) < 150:
        return "reject", "specialist returned no usable output (timeout or tooling failure)"
    for pattern in _AGENT_ERROR_PATTERNS:
        if pattern.lower() in raw.lower():
            return "reject", f"agent runtime error — not real analysis: {pattern}"
    return None


_project_cache = None


def get_or_create_project():
    global _project_cache
    if _project_cache is not None:
        return _project_cache
    try:
        project = galileo.Project.get(name=PROJECT_NAME)
        project.id  # raises AttributeError if None (not found)
    except AttributeError:
        project = galileo.Project(name=PROJECT_NAME)
        project.create()
    _project_cache = project
    return project


def _build_context(run_id, domain, spec, env_facts):
    active = spec.get("services_active") or []
    silent = spec.get("services_silent") or []
    return (
        f"Run: {run_id}  Domain: {domain}\n"
        f"Environment: {env_facts.get('environment')} "
        f"(Kubernetes={env_facts.get('is_kubernetes')})\n"
        f"Log Observer entitled: {env_facts.get('has_log_observer')}\n"
        f"Synthetics entitled: {env_facts.get('has_synthetics')}\n"
        f"RUM active: {env_facts.get('has_rum')}\n"
        f"Active services observed: {active}\n"
        f"Silent services (no telemetry): {len(silent)}"
    )


_prompt_cache = {}


def _specialist_prompt(domain):
    """(system_prompt, task_prompt) for a domain, loaded from agents.<domain>.

    Used to give instruction_adherence a real system prompt to grade against —
    without it, the metric falls back to grading against the first user
    message instead, which is much weaker signal.
    """
    if domain in _prompt_cache:
        return _prompt_cache[domain]
    try:
        mod = importlib.import_module(f"agents.{domain}")
        system = getattr(mod, "_SYSTEM", "").strip()
        task = getattr(mod, "_TASK", "").strip()
    except Exception:
        system, task = "", ""
    _prompt_cache[domain] = (system, task)
    return system, task


_TERMINAL_METRIC_STATUSES = {"success", "not_applicable", "error", "roll_up"}


def _metrics_still_computing(exp):
    """True if any metric on any llm span/trace hasn't reached a terminal status.

    exp.get_status().is_complete only reflects log generation, not metric
    scoring — scores can still be 'pending'/'computing' well after logs are
    complete.
    """
    for record in list(exp.get_spans()) + list(exp.get_traces()):
        for key, value in record.items():
            if key.endswith("_status_type") and value not in _TERMINAL_METRIC_STATUSES:
                return True
    return False


def _wait_for_completion(exp, name, timeout=POLL_TIMEOUT_SEC, interval=POLL_INTERVAL_SEC):
    deadline = time.time() + timeout
    while time.time() < deadline:
        exp.refresh()
        status = exp.get_status()
        if getattr(status, "is_complete", False) and not _metrics_still_computing(exp):
            return
        time.sleep(interval)
    raise TimeoutError(f"Galileo experiment {name} did not finish scoring within {timeout}s")


def _extract_metric(record, alias):
    """Find metric_info_{id}_status_type / _value in a flat span/trace dict by alias."""
    for key, value in record.items():
        if key.endswith("_metric_key_alias") and value == alias:
            metric_id = key[len("metric_info_"):-len("_metric_key_alias")]
            status = record.get(f"metric_info_{metric_id}_status_type")
            val = record.get(f"metric_info_{metric_id}_value")
            return status, val
    return None, None


def evaluate_run(run_id, specialists, env_facts, domains):
    """
    Score every domain of a run with a single Galileo experiment.

    Returns dict: domain -> (decision, reason, scores).
    scores is a dict with groundedness/factuality/completeness/pii_status/
    instruction_adherence keys (all None if the domain was rejected by the
    pre-Galileo garbage filter, or scoring didn't complete in time), so
    callers can persist raw scores alongside the decision for later use in
    training data.

    Each domain's span input is a [system, user] chat message pair — the
    specialist's real system prompt (agents.<domain>._SYSTEM) plus the task
    + environment facts as the user message — instead of a flat context
    string. This lets instruction_adherence grade against the specialist's
    actual instructions, while groundedness/factuality/completeness still
    grade against the environment facts embedded in the user message.

    Raises on any Galileo/SDK failure — caller should catch and fall back
    to the rule-based evaluate().
    """
    get_or_create_project()

    decisions = {}
    key_to_raw = {}
    key_to_domain = {}
    key_to_messages = {}

    for domain in domains:
        spec = specialists.get(domain)
        if not spec:
            continue
        raw = (spec.get("raw_text") or "").strip()

        pre = _garbage_filter(raw)
        if pre:
            decision, reason = pre
            decisions[domain] = (decision, reason, None)
            continue

        system_prompt, task_prompt = _specialist_prompt(domain)
        facts = _build_context(run_id, domain, spec, env_facts)
        user_content = f"{task_prompt}\n\n{facts}" if task_prompt else facts
        messages = [
            {"role": "system", "content": system_prompt or f"You are the {domain} observability specialist."},
            {"role": "user", "content": user_content},
        ]
        # dataset_input comes back from Galileo as json.dumps(messages) — use
        # the same string as the lookup key so results can be matched back
        # to a domain without needing a hashable form of `messages` itself.
        key = json.dumps(messages)
        key_to_raw[key] = raw
        key_to_domain[key] = domain
        key_to_messages[key] = messages

    if not key_to_raw:
        return decisions  # everything was filtered out before hitting Galileo

    @galileo.log(span_type="llm")
    def score_fn(messages):
        return key_to_raw[json.dumps(messages)]

    experiment_name = f"run-{run_id}-{int(time.time())}"
    result = galileo.experiments.run_experiment(
        experiment_name,
        project=PROJECT_NAME,
        dataset=[{"input": key_to_messages[k]} for k in key_to_raw],
        function=score_fn,
        metrics=METRICS,
    )

    exp = None
    for _ in range(10):
        exp = galileo.Experiment.get(name=experiment_name, project_name=PROJECT_NAME)
        if exp is not None:
            break
        time.sleep(1)
    if exp is None:
        raise RuntimeError(f"Galileo experiment {experiment_name} not found after creation")
    _wait_for_completion(exp, experiment_name)

    # get_spans() returns both the "llm" span (has real metric statuses) and a
    # parent "workflow" roll-up span (status_type "roll_up", no real values) per
    # record — keep only the llm span or extraction reads the roll-up instead.
    spans_by_key = {
        s.get("dataset_input"): s for s in exp.get_spans() if s.get("type") == "llm"
    }
    traces_by_key = {t.get("dataset_input"): t for t in exp.get_traces()}

    for key, domain in key_to_domain.items():
        span = spans_by_key.get(key)
        trace = traces_by_key.get(key)
        if not span or not trace:
            decisions[domain] = ("approve", "Galileo scoring incomplete for this record — no penalty applied", None)
            continue

        g_status, g_val = _extract_metric(span, "groundedness")
        f_status, f_val = _extract_metric(span, "factuality")
        c_status, c_val = _extract_metric(span, "completeness_gpt")
        p_status, p_val = _extract_metric(trace, "pii")
        ia_status, ia_val = _extract_metric(span, "instruction_adherence")

        groundedness = (g_val[0] if isinstance(g_val, list) and g_val else g_val) if g_status == "success" else None
        factuality = (f_val[0] if isinstance(f_val, list) and f_val else f_val) if f_status == "success" else None
        completeness = (c_val[0] if isinstance(c_val, list) and c_val else c_val) if c_status == "success" else None
        pii_value = p_val if p_status == "success" else None
        pii_found = p_status == "success" and p_val not in (None, "Not Found")
        instruction_adherence = (ia_val[0] if isinstance(ia_val, list) and ia_val else ia_val) if ia_status == "success" else None

        scores = {
            "groundedness": groundedness,
            "factuality": factuality,
            "completeness": completeness,
            "pii_status": pii_value,
            "instruction_adherence": instruction_adherence,
        }

        if pii_found:
            decisions[domain] = ("reject", f"Galileo detected PII in output: {p_val}", scores)
            continue

        low_scores = [
            (name, val) for name, val in (("groundedness", groundedness), ("factuality", factuality))
            if val is not None and val < MIN_SCORE
        ]
        if instruction_adherence is not None and instruction_adherence < 1:
            low_scores.append(("instruction_adherence", instruction_adherence))
        if low_scores:
            summary = ", ".join(f"{name}={val:.2f}" for name, val in low_scores)
            decisions[domain] = ("reject", f"low Galileo score(s): {summary}", scores)
            continue

        parts = []
        if groundedness is not None:
            parts.append(f"groundedness={groundedness:.2f}")
        if factuality is not None:
            parts.append(f"factuality={factuality:.2f}")
        if completeness is not None:
            parts.append(f"completeness={completeness:.2f}")
        if instruction_adherence is not None:
            parts.append(f"instruction_adherence={instruction_adherence:.2f}")
        decisions[domain] = ("approve", f"Galileo scores: {', '.join(parts) if parts else 'no applicable metrics'}", scores)

    return decisions
