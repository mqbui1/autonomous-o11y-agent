"""
Coordinator — runs all four specialist agents in parallel and synthesizes findings.

Architecture:
    coordinator
     ├── [parallel] health_agent         → detectors, APM, collector, license
     ├── [parallel] instrumentation_agent → span/metric/log quality
     ├── [parallel] governance_agent     → cardinality, cost, trace volume
     └── [parallel] detector_agent       → provisioning, baselines, lifecycle
     └── _cross_domain_analysis()        → finds services/issues spanning domains (Gap 4)
     └── _synthesize()                   → LLM pass with all tools available  (Gap 5)
     └── build_run_record() + save_state → structured persistence             (Gap 3)
"""

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from config import AgentConfig
from agent_loop import run_agent
from state import load_state, save_state, build_run_record
from tools.findings import SpecialistFindings
import agents.health as health_agent
import agents.instrumentation as instrumentation_agent
import agents.governance as governance_agent
import agents.detector as detector_agent

logger = logging.getLogger(__name__)

_SYNTHESIS_SYSTEM = """\
You are a principal observability engineer synthesizing findings from four specialist \
agents for Splunk Observability Cloud.

Your scope is EXCLUSIVELY the environment named below. Discard anything from other \
environments or unrelated services.

You have access to all investigation tools and MAY use them to drill into specific \
cross-domain issues that the specialists surfaced but did not fully resolve. Only \
call tools when a targeted follow-up would materially improve the assessment.

Produce a complete prioritized assessment:
1. Executive summary table (Domain | Status | Key Finding)
2. Cross-domain issues — services or problems appearing in multiple specialist domains
3. Detailed findings per domain — specific numbers, service names, attribute names
4. Prioritized action plan: Immediate / Short-term / Ongoing
5. Health snapshot table (Area | Status | Key Metric)

Lead with the highest-impact findings. Be specific — vague recommendations have no value.
"""


def run_assessment(config: AgentConfig, prompt: str = None) -> str:
    """
    Run all four specialist agents in parallel, perform cross-domain analysis,
    synthesize with full tool access, and persist structured state.
    """
    state = load_state(config.environment)
    state_context = state.trend_context()

    specialists = {
        "health": health_agent,
        "instrumentation": instrumentation_agent,
        "governance": governance_agent,
        "detector": detector_agent,
    }

    logger.info(
        "Launching %d specialist agents in parallel for environment=%s",
        len(specialists),
        config.environment,
    )

    findings: dict[str, SpecialistFindings] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(mod.run, config, state_context): name
            for name, mod in specialists.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                findings[name] = future.result()
                logger.info("Specialist '%s' complete", name)
            except Exception as exc:
                findings[name] = SpecialistFindings(
                    domain=name,
                    summary=f"[{name} agent error: {exc}]",
                    raw_text=str(exc),
                )
                logger.error("Specialist '%s' failed: %s", name, exc, exc_info=True)

    # Gap 4: cross-domain analysis before synthesis
    cross_domain = _cross_domain_analysis(findings)
    if cross_domain:
        logger.info("Cross-domain issues detected — injecting into synthesis")

    synthesis = _synthesize(config, findings, cross_domain, prompt)

    # Gap 3: persist rich structured state
    record = build_run_record(config.environment, findings)
    state.add_run(record)
    save_state(state)

    return synthesis


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


def _format_findings_for_synthesis(
    config: AgentConfig,
    findings: dict[str, SpecialistFindings],
    cross_domain: str,
    custom_prompt: str | None,
) -> str:
    """Build the synthesis prompt from structured findings."""
    parts = [
        f"# Specialist Agent Findings — `{config.environment}`",
        f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Mode: {'auto-apply' if config.auto_apply else 'dry-run'}",
        "",
    ]

    if cross_domain:
        parts.append(cross_domain)
        parts.append("")

    for domain in ("health", "instrumentation", "governance", "detector"):
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
            for issue in sorted(f.issues, key=lambda i: ["critical","high","medium","low"].index(i.severity)):
                svc = f" [{issue.service}]" if issue.service else ""
                parts.append(
                    f"  - [{issue.severity.upper()}]{svc} {issue.description} "
                    f"→ {issue.recommendation}"
                )
        if f.metrics:
            parts.append(f"**Metrics:** {f.metrics}")
        if f.raw_text:
            parts.append(f"\n**Full findings:**\n{f.raw_text}")
        parts.append("")

    if custom_prompt:
        parts.append(f"## USER QUESTION\n{custom_prompt}\n")

    parts.append(
        "Synthesize all findings above into a complete, prioritized observability "
        "assessment. Pay special attention to cross-domain issues. Include the "
        "executive summary table, cross-domain section, and health snapshot."
    )

    return "\n".join(parts)


def _synthesize(
    config: AgentConfig,
    findings: dict[str, SpecialistFindings],
    cross_domain: str,
    custom_prompt: str | None = None,
) -> str:
    """
    Final synthesis pass — LLM with full tool access so it can drill into
    cross-cutting issues that specialists surfaced but didn't fully resolve (Gap 5).
    """
    from tools.health_check import SCHEMAS as H_SCHEMAS, TOOL_FNS as H_FNS
    from tools.analyzer import SCHEMAS as A_SCHEMAS, TOOL_FNS as A_FNS
    from tools.governance import SCHEMAS as G_SCHEMAS, TOOL_FNS as G_FNS
    from tools.provisioner import SCHEMAS as P_SCHEMAS, TOOL_FNS as P_FNS

    all_schemas = H_SCHEMAS + A_SCHEMAS + G_SCHEMAS + P_SCHEMAS
    all_fns = {**H_FNS, **A_FNS, **G_FNS, **P_FNS}

    message = _format_findings_for_synthesis(config, findings, cross_domain, custom_prompt)
    system = _SYNTHESIS_SYSTEM + f'\n\nEnvironment: "{config.environment}"'

    return run_agent(
        model_id=config.bedrock_model_id,
        region=config.aws_region,
        system_prompt=system,
        tools=all_schemas,
        tool_fns=all_fns,
        initial_message=message,
    )
