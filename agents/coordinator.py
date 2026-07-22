"""
Coordinator — runs all specialist agents in parallel and synthesizes findings.

Architecture:
    coordinator
     ├── [parallel] health_agent         → detectors, APM, collector, license
     ├── [parallel] instrumentation_agent → span/metric/log quality
     ├── [parallel] governance_agent     → cardinality, cost, trace volume
     ├── [parallel] detector_agent       → provisioning, baselines, lifecycle
     ├── [parallel] logs_agent           → log anomalies, error bursts
     ├── [parallel] rum_agent            → frontend UX, Core Web Vitals
     └── [parallel] rca_agent            → incident root cause analysis, causal chain
     └── _cross_domain_analysis()        → finds services/issues spanning domains (Gap 4)
     └── _synthesize()                   → LLM pass with all tools available  (Gap 5)
     └── build_run_record() + save_state → structured persistence             (Gap 3)

run_incident_rca(config, alert):
    Triggered by streaming critical alerts — runs targeted RCA for a specific incident.
"""

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from config import AgentConfig
from agent_loop import run_agent
from providers import get_provider
from state import load_state, save_state, build_run_record, save_assessment_detail, list_run_details
from tools.findings import SpecialistFindings
import agents.health as health_agent
import agents.instrumentation as instrumentation_agent
import agents.governance as governance_agent
import agents.detector as detector_agent
import agents.logs as logs_agent
import agents.rum as rum_agent
import agents.rca as rca_agent
import agents.synthetics as synthetics_agent
import agents.db as db_agent
import agents.performance as performance_agent
from agents.remediation import generate_remediations

logger = logging.getLogger(__name__)

_SYNTHESIS_SYSTEM = """\
You are a principal observability engineer for Splunk Observability Cloud, answering a \
specific question from a user about an observability assessment that has already run.

Your scope is EXCLUSIVELY the environment named below. Discard anything from other \
environments or unrelated services.

A full structured report (executive summary, detailed findings, action plan, health \
snapshot) has ALREADY been generated from the specialist findings below and will be \
shown to the user alongside your answer — do NOT regenerate or repeat any of those \
sections. Your ONLY job is to directly answer the user's question using the findings \
as evidence.

Do NOT call any tools or emit tool-call syntax. Be specific — cite service names, \
numbers, and attribute names from the findings. Keep it tight: a few sentences to a \
short paragraph, no filler like "Based on the results...".
"""


def run_assessment(
    config: AgentConfig,
    prompt: str = None,
    observation_buffer=None,
    monitor=None,
) -> str:
    """
    Run all specialist agents in parallel, perform cross-domain analysis,
    synthesize with full tool access, and persist structured state.

    observation_buffer: optional ObservationBuffer — streaming observations
    injected into specialist context so batch and streaming context are unified.

    monitor: optional SelfMonitor — records run metrics after findings are collected.
    """
    state = load_state(config.environment)
    trend_context = state.trend_context()

    # Build two streaming summaries: one with PII (governance only) and one without.
    # PII detections must not be injected into every specialist — each would independently
    # raise the same PII findings, creating 10× duplicate issues. Governance owns PII.
    state_context = trend_context
    state_context_with_pii = trend_context
    if observation_buffer is not None:
        streaming_no_pii = observation_buffer.summarize(window_minutes=60, include_pii=False)
        streaming_with_pii = observation_buffer.summarize(window_minutes=60, include_pii=True)
        if streaming_no_pii:
            state_context = (trend_context + "\n\n" + streaming_no_pii).strip()
        if streaming_with_pii:
            state_context_with_pii = (trend_context + "\n\n" + streaming_with_pii).strip()

    specialists = {
        "health": health_agent,
        "instrumentation": instrumentation_agent,
        "governance": governance_agent,
        "detector": detector_agent,
        "logs": logs_agent,
        "rum": rum_agent,
        "rca": rca_agent,
        "synthetics": synthetics_agent,
        "db": db_agent,
        "performance": performance_agent,
    }

    # Governance is the only specialist that should see PII observations.
    _pii_owners = {"governance"}

    logger.info(
        "Launching %d specialist agents in parallel for environment=%s",
        len(specialists),
        config.environment,
    )

    import time as _time
    _run_start = _time.time()

    try:
        from receiver.otlp_receiver import update_assessment_progress as _update_progress
    except ImportError:
        _update_progress = None

    findings: dict[str, SpecialistFindings] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            pool.submit(
                mod.run, config,
                state_context_with_pii if name in _pii_owners else state_context,
            ): name
            for name, mod in specialists.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                findings[name] = future.result(timeout=config.specialist_timeout)
                # Guard against empty summary (observed 2026-07-22: rum/db specialists on the
                # local fine-tuned model sometimes call submit_findings with summary="").
                if not findings[name].summary or not findings[name].summary.strip():
                    findings[name].summary = (findings[name].raw_text or "No summary provided.")[:500]
                logger.info("Specialist '%s' complete", name)
            except TimeoutError:
                findings[name] = SpecialistFindings(
                    domain=name,
                    summary=f"[{name} specialist timed out after {config.specialist_timeout}s]",
                    raw_text="timeout",
                )
                logger.error("Specialist '%s' timed out after %ds", name, config.specialist_timeout)
            except Exception as exc:
                findings[name] = SpecialistFindings(
                    domain=name,
                    summary=f"[{name} agent error: {exc}]",
                    raw_text=str(exc),
                )
                logger.error("Specialist '%s' failed: %s", name, exc, exc_info=True)
            if _update_progress:
                _update_progress("specialists", len(findings), name=name)

    # Deduplicate issues across specialists before synthesis/save
    _dedup_cross_specialist_issues(findings)

    # Cross-domain analysis before synthesis
    cross_domain = _cross_domain_analysis(findings)
    if cross_domain:
        logger.info("Cross-domain issues detected — injecting into synthesis")

    if _update_progress:
        _update_progress("synthesizing")

    if _is_convergent_blackout(findings):
        logger.info(
            "Convergent blackout detected (all specialists report zero telemetry) — "
            "skipping LLM synthesis, building report from structured findings"
        )
        synthesis = _fast_blackout_synthesis(config, findings, cross_domain)
    else:
        synthesis = _synthesize(config, findings, cross_domain, prompt)

    if _update_progress:
        _update_progress("saving")

    # Persist rich structured state
    import uuid as _uuid
    run_id = f"run_{_uuid.uuid4().hex[:10]}"
    record = build_run_record(config.environment, findings)
    record.run_id = run_id
    state.add_run(record)
    save_state(state)

    # Persist full assessment detail for the UI/API
    elapsed = round(_time.time() - _run_start, 1)
    save_assessment_detail(config.environment, {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "environment": config.environment,
        "elapsed_seconds": elapsed,
        "status": "complete",
        "specialists": {
            name: {
                "domain": f.domain,
                "summary": f.summary,
                "instrumentation_score": f.instrumentation_score,
                "services_active": f.services_active,
                "services_silent": f.services_silent,
                "issues": [
                    {
                        "severity": i.severity,
                        "domain": i.domain,
                        "service": i.service,
                        "description": i.description,
                        "recommendation": i.recommendation,
                        "action_tool": getattr(i, "action_tool", ""),
                        "action_args": getattr(i, "action_args", {}),
                    }
                    for i in f.issues
                ],
                "metrics": f.metrics,
                "actions_taken": f.actions_taken,
                "raw_text": f.raw_text,
            }
            for name, f in findings.items()
        },
        "cross_domain": cross_domain,
        "synthesis": synthesis,
        "pending_remediations": generate_remediations(
            findings,
            splunk_config={"realm": config.realm, "access_token": config.token},
            environment=config.environment,
        ),
    })

    # Emit self-observability metrics now that we have the real findings dict
    if monitor is not None:
        try:
            elapsed = _time.time() - _run_start
            monitor.record_run_metrics(findings, elapsed, config.environment)
        except Exception as _exc:
            logger.debug("SelfMonitor record_run_metrics failed: %s", _exc)

    return synthesis


def _issue_topic(description: str) -> str:
    """Extract semantic topic from an issue description for deduplication."""
    d = description.lower()
    if "rum" in d or "real user monitoring" in d:               return "rum"
    if "log observer" in d or "log ingestion" in d:             return "logging"
    if "/v1/log/entries" in d or "log data" in d:               return "logging"
    if "synthetic" in d:                                         return "synthetics"
    if "detector" in d or "no alert" in d:                       return "alerting"
    if "otelcol" in d or "otel collector" in d:                  return "collector"
    if "db.system" in d or "database span" in d:                 return "db_attrs"
    if "silent" in d and "service" in d:                         return "service_silence"
    if "error rate" in d and ("%" in d or "errors out of" in d): return "error_rate"
    if "trace storm" in d or "trace volume" in d:                return "trace_volume"
    if "k8s" in d or "kubernetes" in d:                          return "k8s"
    if "cardinality" in d:                                       return "cardinality"
    if "runtime metric" in d:                                    return "runtime_metrics"
    if "correlation" in d and "log" in d:                        return "log_correlation"
    return description.strip()[:50].lower()


def _issue_fingerprint(issue) -> str:
    """Semantic fingerprint for deduplicating issues across specialists."""
    svc = (issue.service or "").lower().strip()
    topic = _issue_topic(issue.description or "")
    return f"{svc}|{issue.severity}|{topic}"


def _dedup_cross_specialist_issues(findings: dict[str, SpecialistFindings]) -> None:
    """
    Remove duplicate issues that appear in multiple specialist domains.
    Keeps the first occurrence (by specialist priority order) and removes
    near-identical issues from other specialists so the UI doesn't show the
    same root problem 5-10 times.
    """
    # Priority order — more specific specialists keep their issues
    PRIORITY = ["instrumentation", "db", "rum", "rca", "health", "logs",
                "synthetics", "detector", "governance"]
    ordered = [k for k in PRIORITY if k in findings] + \
              [k for k in findings if k not in PRIORITY]

    seen: set[str] = set()
    for name in ordered:
        f = findings.get(name)
        if not f:
            continue
        deduped = []
        for issue in f.issues:
            fp = _issue_fingerprint(issue)
            if fp not in seen:
                seen.add(fp)
                deduped.append(issue)
        f.issues = deduped


def _cross_domain_analysis(findings: dict[str, SpecialistFindings]) -> str:
    """
    Identify services and issues that appear across multiple specialist domains.
    This context is injected into the synthesis prompt so the final LLM pass
    can reason explicitly about cross-cutting problems (Gap 4).
    """
    # Map each service to the domains that flagged it
    service_domains: dict[str, set[str]] = defaultdict(set)
    for domain, f in findings.items():
        for issue in f.issues:
            if issue.service:
                service_domains[issue.service].add(domain)
        for svc in f.services_silent:
            service_domains[svc].add(domain)

    cross_cutting = {
        svc: sorted(domains)
        for svc, domains in service_domains.items()
        if len(domains) > 1
    }

    # Collect all critical issues across domains
    critical = [
        (f.domain, issue)
        for f in findings.values()
        for issue in f.issues
        if issue.severity == "critical"
    ]

    # Silent services appearing in multiple domain reports
    all_silent: set[str] = set()
    for f in findings.values():
        all_silent.update(f.services_silent)

    if not cross_cutting and not critical:
        return ""

    lines = ["## Cross-Domain Analysis\n"]

    if cross_cutting:
        lines.append(
            "**Services with issues across multiple domains (highest priority for synthesis):**"
        )
        for svc, domains in sorted(cross_cutting.items()):
            lines.append(f"- `{svc}`: flagged by {', '.join(domains)}")

    if critical:
        lines.append("\n**Critical issues requiring immediate attention:**")
        for domain, issue in critical:
            svc_tag = f" [{issue.service}]" if issue.service else ""
            lines.append(f"- [{domain}]{svc_tag} {issue.description}")

    if all_silent:
        lines.append(
            f"\n**All silent services (no telemetry):** "
            + ", ".join(f"`{s}`" for s in sorted(all_silent))
        )

    return "\n".join(lines)


_DOMAIN_ORDER = ("health", "instrumentation", "governance", "detector", "logs", "rum", "rca", "synthetics", "db", "performance")
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_SEV_STATUS = {"critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM", "low": "LOW"}


def _build_executive_summary_table(findings: dict[str, SpecialistFindings]) -> str:
    """
    Build the executive summary table deterministically from structured findings.
    Built in code (not by the LLM) so it always covers every domain exactly once —
    the LLM-generated version was observed (2026-07-22) to sometimes drop domains
    or emit the table twice with conflicting content.
    """
    lines = ["## Executive Summary", "", "| Domain | Status | Key Finding |", "|--------|--------|-------------|"]
    for name in _DOMAIN_ORDER:
        f = findings.get(name)
        if not f:
            continue
        worst = min((i.severity for i in f.issues), key=lambda s: _SEV_ORDER.get(s, 9), default=None)
        status = _SEV_STATUS.get(worst, "NO DATA")
        lines.append(f"| {name.upper()} | {status} | {(f.summary or '')[:120]} |")
    return "\n".join(lines)


def _build_detailed_findings(findings: dict[str, SpecialistFindings]) -> str:
    """
    Build the "Detailed Findings Per Domain" section deterministically from
    structured findings. Built in code (not by the LLM) because the 3B synthesis
    model was observed (2026-07-22) to misattribute findings across domains and
    invent nonexistent specialist names even when given clean, accurate input —
    a per-domain listing is exactly the kind of exhaustive enumeration an LLM
    adds no value to and hallucinates most on.
    """
    lines = ["## Detailed Findings Per Domain"]
    for name in _DOMAIN_ORDER:
        f = findings.get(name)
        if not f:
            continue
        lines.append(f"\n### {name.upper()}")
        lines.append(f"**Summary:** {f.summary}")
        if f.services_active:
            lines.append(f"**Active services:** {', '.join(f.services_active)}")
        if f.services_silent:
            lines.append(f"**Silent services:** {', '.join(f.services_silent)}")
        if f.instrumentation_score is not None:
            lines.append(f"**Instrumentation score:** {f.instrumentation_score}/100")
        if f.issues:
            for issue in sorted(f.issues, key=lambda i: _SEV_ORDER.get(i.severity, 9)):
                svc = f" [{issue.service}]" if issue.service else ""
                lines.append(
                    f"- **[{issue.severity.upper()}]**{svc} {issue.description} "
                    f"→ {issue.recommendation}"
                )
        if f.metrics:
            lines.append(f"**Metrics:** {f.metrics}")
        if f.actions_taken:
            lines.append(f"**Actions taken:** {', '.join(f.actions_taken)}")
    return "\n".join(lines)


def _build_action_plan(findings: dict[str, SpecialistFindings]) -> str:
    """Build the deduplicated, severity-sorted action plan deterministically."""
    all_issues = sorted(
        [(f.domain, i) for f in findings.values() for i in f.issues],
        key=lambda x: _SEV_ORDER.get(x[1].severity, 9),
    )
    if not all_issues:
        return ""
    lines = ["## Prioritized Action Plan", ""]
    seen: set[str] = set()
    for domain, issue in all_issues:
        key = issue.recommendation[:80]
        if key in seen:
            continue
        seen.add(key)
        svc = f" [{issue.service}]" if issue.service else ""
        lines.append(f"- **[{issue.severity.upper()}][{domain}{svc}]** {issue.description}")
        lines.append(f"  → {issue.recommendation}")
    return "\n".join(lines)


def _build_health_snapshot(findings: dict[str, SpecialistFindings]) -> str:
    """Build the Area | Status | Key Metric table deterministically."""
    lines = ["## Health Snapshot", "", "| Area | Status | Key Metric |", "|------|--------|------------|"]
    for name in _DOMAIN_ORDER:
        f = findings.get(name)
        if not f:
            continue
        worst = min((i.severity for i in f.issues), key=lambda s: _SEV_ORDER.get(s, 9), default=None)
        status = _SEV_STATUS.get(worst, "OK" if f.services_active else "NO DATA")
        if f.metrics:
            k, v = next(iter(f.metrics.items()))
            key_metric = f"{k}: {v}"
        elif f.instrumentation_score is not None:
            key_metric = f"score: {f.instrumentation_score}/100"
        else:
            key_metric = f"{len(f.issues)} issue(s)"
        lines.append(f"| {name.upper()} | {status} | {key_metric} |")
    return "\n".join(lines)


def _build_deterministic_report(
    config: AgentConfig,
    findings: dict[str, SpecialistFindings],
    cross_domain: str,
    footer: str,
) -> str:
    """
    Assemble the complete assessment report from structured findings only —
    no LLM call. Used both for the convergent-blackout fast path and as the
    default synthesis path (2026-07-22: eliminates hallucination/rambling in
    the "detailed findings"/"action plan" sections that a 3B synthesis LLM
    could not reliably produce even from clean input).
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        f"# Observability Assessment — `{config.environment}`",
        f"**Timestamp:** {ts}  |  **Mode:** {'auto-apply' if config.auto_apply else 'dry-run'}",
        "",
        _build_executive_summary_table(findings),
    ]
    if cross_domain:
        parts.extend(["", cross_domain])
    parts.extend(["", _build_detailed_findings(findings)])
    action_plan = _build_action_plan(findings)
    if action_plan:
        parts.extend(["", action_plan])
    parts.extend(["", _build_health_snapshot(findings)])
    parts.extend(["", "---", footer])
    return "\n".join(parts)


def _format_findings_for_custom_prompt(
    config: AgentConfig,
    findings: dict[str, SpecialistFindings],
    cross_domain: str,
    custom_prompt: str,
) -> str:
    """Build the LLM prompt for answering an ad-hoc `--prompt` question against findings."""
    parts = [
        f"# Specialist Agent Findings — `{config.environment}`",
        f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Mode: {'auto-apply' if config.auto_apply else 'dry-run'}",
        "",
    ]

    if cross_domain:
        parts.append(cross_domain)
        parts.append("")

    for domain in _DOMAIN_ORDER:
        f = findings.get(domain)
        if not f:
            continue
        parts.append(f"## {domain.upper()} SPECIALIST")
        parts.append(f"**Summary:** {f.summary}")
        if f.services_silent:
            parts.append(f"**Silent services:** {', '.join(f.services_silent)}")
        if f.instrumentation_score is not None:
            parts.append(f"**Instrumentation score:** {f.instrumentation_score}/100")
        if f.issues:
            parts.append("**Issues:**")
            for issue in sorted(f.issues, key=lambda i: _SEV_ORDER.get(i.severity, 9)):
                svc = f" [{issue.service}]" if issue.service else ""
                parts.append(
                    f"  - [{issue.severity.upper()}]{svc} {issue.description} "
                    f"→ {issue.recommendation}"
                )
        if f.metrics:
            parts.append(f"**Metrics:** {f.metrics}")
        parts.append("")

    parts.append(f"## USER QUESTION\n{custom_prompt}\n")
    parts.append("Answer the user's question above directly, citing specifics from the findings.")

    return "\n".join(parts)


def _is_convergent_blackout(findings: dict[str, SpecialistFindings]) -> bool:
    """
    True when the assessment is a total blackout — essentially all specialists
    report zero active services and no usable telemetry.

    Criteria (both must hold):
      1. At least 7 of 10 specialists have services_active == 0 (or None)
      2. Every specialist with an instrumentation_score reports <= 20
    """
    zero_active = sum(1 for f in findings.values() if not f.services_active)
    scored = [f.instrumentation_score for f in findings.values() if f.instrumentation_score is not None]
    low_scores = all(s <= 20 for s in scored) if scored else True
    return zero_active >= 7 and low_scores


def _fast_blackout_synthesis(
    config: AgentConfig,
    findings: dict[str, SpecialistFindings],
    cross_domain: str,
) -> str:
    """
    Build a synthesis report directly from structured findings when there is
    a convergent blackout — skips the LLM pass to save 5-10 minutes.
    """
    footer = (
        "*Synthesis LLM skipped — convergent blackout detected (all specialists "
        "report zero active telemetry). Report built directly from structured findings.*"
    )
    return _build_deterministic_report(config, findings, cross_domain, footer)


def _synthesize(
    config: AgentConfig,
    findings: dict[str, SpecialistFindings],
    cross_domain: str,
    custom_prompt: str | None = None,
) -> str:
    """
    Final assessment report.

    The report itself (executive summary, detailed findings, action plan, health
    snapshot) is always built deterministically from structured findings — no LLM
    call, no risk of hallucination or rambling (2026-07-22: even given clean input,
    the 3B synthesis model misattributed findings across domains and invented
    nonexistent specialist names when asked to freely write these sections).

    The LLM is only invoked when the caller passed a custom `--prompt` question —
    in that case its ONLY job is to directly answer that question using the
    findings as evidence; the deterministic report is prepended either way.
    """
    footer = "*Report built deterministically from structured specialist findings.*"
    report = _build_deterministic_report(config, findings, cross_domain, footer)

    if not custom_prompt:
        return report

    message = _format_findings_for_custom_prompt(config, findings, cross_domain, custom_prompt)
    system = _SYNTHESIS_SYSTEM + f'\n\nEnvironment: "{config.environment}"'

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_agent,
            provider=get_provider(config),
            system_prompt=system,
            tools=[],
            tool_fns={},
            initial_message=message,
            max_turns=1,
        )
        try:
            answer = future.result(timeout=config.synthesis_timeout)
        except TimeoutError:
            logger.error("Synthesis timed out after %ds", config.synthesis_timeout)
            answer = f"[Answer timed out after {config.synthesis_timeout}s]"

    return f"{report}\n\n## Answer to your question\n\n{answer}"


def run_incident_rca(config: AgentConfig, service: str, incident_id: str = "",
                     start_ms: int = 0, severity: str = "critical") -> SpecialistFindings:
    """
    Run a targeted RCA for a specific incident. Intended to be called from the
    streaming alert path when a critical alert fires.

    Example (from main.py streaming loop):
        from agents.coordinator import run_incident_rca
        findings = run_incident_rca(config, service="checkout-service",
                                    incident_id="abc123", start_ms=1719700000000)

    Args:
        config: AgentConfig with realm, token, environment.
        service: The primary service the incident is about.
        incident_id: Splunk incident ID (optional, for context).
        start_ms: Unix millisecond timestamp when the incident fired.
        severity: Incident severity (critical/high/medium).
    """
    import json as _json
    incident_context = _json.dumps({
        "service": service,
        "incident_id": incident_id,
        "start_ms": start_ms,
        "severity": severity,
        "environment": config.environment,
    })
    logger.info(
        "Running targeted RCA for service=%s incident=%s severity=%s env=%s",
        service, incident_id, severity, config.environment,
    )
    return rca_agent.run(config, incident_context=incident_context)
