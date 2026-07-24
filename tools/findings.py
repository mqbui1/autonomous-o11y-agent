"""
Structured findings output — each specialist calls submit_findings as its final action.

The coordinator reads the structured data to:
  - Build rich persistent state           (Gap 3)
  - Detect cross-domain issues            (Gap 4)
  - Feed the synthesis pass               (Gap 5 + 6)
  - Track action feedback between runs    (Gap 7)
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from agent_loop import _sanitize_final_text

# Defense-in-depth against specialist-level generation defects observed on the local
# fine-tuned model (2026-07-22 live-test regression): raw JSON dict fragments leaking
# into free-text fields (e.g. summary=`"submitted_run", {"severity": "critical", ...`),
# and text truncated mid-sentence. Cheaper than retraining and catches the symptom
# regardless of root cause — same philosophy as agent_loop.py's _sanitize_final_text.
_JSON_LEAK_START_RE = re.compile(r'^\s*"[a-z_]+"\s*,\s*\{')
_DICT_KEY_RE = re.compile(r'"(severity|domain|description|recommendation|action_args|action_tool)"\s*:')
_DESC_VALUE_RE = re.compile(r'"description"\s*:\s*"([^"]{10,300})"')
_SENTENCE_END_RE = re.compile(r'[.!?]["\')]*\s')


def _looks_like_json_leak(text: str) -> bool:
    if not text:
        return False
    return bool(_JSON_LEAK_START_RE.match(text)) or len(_DICT_KEY_RE.findall(text)) >= 2


def _trim_truncated_tail(text: str) -> str:
    """If text appears cut off mid-sentence, trim back to the last complete sentence."""
    stripped = text.rstrip()
    if not stripped or len(stripped) < 40 or stripped[-1] in '.!?"\')':
        return text
    matches = list(_SENTENCE_END_RE.finditer(text))
    if matches:
        cut = matches[-1].end()
        if cut > len(text) * 0.4:  # don't throw away most of the text
            return text[:cut].strip()
    return text.strip()


def _clean_findings_text(text: str, fallback: str = "") -> str:
    """Sanitize a free-text field from a specialist's submit_findings call."""
    cleaned = _sanitize_final_text(text or "")
    if _looks_like_json_leak(cleaned):
        m = _DESC_VALUE_RE.search(cleaned)
        if m:
            return m.group(1).strip()
        return fallback or "[Malformed specialist output — raw JSON leaked into this field]"
    trimmed = _trim_truncated_tail(cleaned)
    # A genuinely empty result (e.g. the model gave up after a blank-summary retry
    # without producing anything at all) is just as unusable as a JSON leak — apply
    # the same fallback instead of silently persisting an empty field. Confirmed
    # 2026-07-23: db specialist's second submit_findings attempt after the blank-
    # summary nudge returned a fully empty end_turn, leaving summary="" in the report.
    return trimmed or fallback


@dataclass
class Issue:
    severity: str        # critical | high | medium | low
    domain: str
    description: str
    recommendation: str = ""
    service: str = ""
    # Optional: tool function name + args for the approval workflow to auto-apply
    action_tool: str = ""
    action_args: dict = field(default_factory=dict)


@dataclass
class SpecialistFindings:
    domain: str
    summary: str
    services_active: list[str] = field(default_factory=list)
    services_silent: list[str] = field(default_factory=list)
    instrumentation_score: int | None = None
    issues: list[Issue] = field(default_factory=list)
    # Domain-specific metrics:
    #   health        → {"detectors_healthy": int, "detectors_critical": int, "silent_service_count": int}
    #   instrumentation → {"score": int, "span_coverage_pct": float}
    #   governance    → {"top_cardinality_mts": int, "anomaly_count": int, "top_metrics": [str]}
    #   detector      → {"deployed_count": int, "dark_service_count": int, "deployed_ids": [str]}
    metrics: dict[str, Any] = field(default_factory=dict)
    actions_taken: list[str] = field(default_factory=list)  # audit trail of changes made
    raw_text: str = ""   # full prose from run_agent preserved here
    # True only when the specialist actually called submit_findings successfully.
    # Every agents/*.py module falls back to a raw_text[:500] summary (services_active
    # left at its [] default) when the model never calls submit_findings — that fallback
    # is indistinguishable from a genuine "zero active services" finding unless callers
    # check this flag. Added 2026-07-23 to fix coordinator._is_convergent_blackout()
    # falsely triggering on output-quality failures instead of real telemetry blackouts.
    structured: bool = False


SUBMIT_SCHEMA = {
    "toolSpec": {
        "name": "submit_findings",
        "description": (
            "Submit your structured findings. Call this as your FINAL action after "
            "completing all investigation. This ends your assessment turn."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "required": ["summary", "issues"],
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "2-4 sentence summary of key findings for this domain.",
                    },
                    "services_active": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Services currently reporting telemetry.",
                    },
                    "services_silent": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Services with no telemetry in the observation window.",
                    },
                    "instrumentation_score": {
                        "type": "integer",
                        "description": "Instrumentation quality score 0-100 (instrumentation specialist only).",
                    },
                    "issues": {
                        "type": "array",
                        "description": "All findings ranked by severity.",
                        "items": {
                            "type": "object",
                            "required": ["severity", "domain", "description", "recommendation"],
                            "properties": {
                                "severity": {
                                    "type": "string",
                                    "enum": ["critical", "high", "medium", "low"],
                                },
                                "domain": {"type": "string"},
                                "service": {
                                    "type": "string",
                                    "description": "Affected service name, or empty for org-wide issues.",
                                },
                                "description": {"type": "string"},
                                "recommendation": {"type": "string"},
                                "action_tool": {
                                    "type": "string",
                                    "description": "Tool function name to auto-apply this fix (e.g. 'provision_detectors'). Leave empty for manual-only actions.",
                                },
                                "action_args": {
                                    "type": "object",
                                    "description": "Keyword arguments for action_tool (e.g. {\"service\": \"frontend\", \"auto_deploy\": true}).",
                                },
                            },
                        },
                    },
                    "metrics": {
                        "type": "object",
                        "description": (
                            "Domain-specific key metrics. Examples: "
                            "health → {detectors_healthy, detectors_critical, silent_service_count}, "
                            "instrumentation → {score, span_coverage_pct}, "
                            "governance → {top_cardinality_mts, anomaly_count, top_metrics}, "
                            "detector → {deployed_count, dark_service_count, deployed_ids}"
                        ),
                    },
                    "actions_taken": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Audit trail of changes actually made this run. "
                            "Examples: 'Deployed detector HxYZ for payment-service (error rate p95)', "
                            "'Applied drop rule for k8s.pod.uid in http.server.duration'. "
                            "Leave empty for dry-run / recommendation-only runs."
                        ),
                    },
                },
            }
        },
    }
}


def _split_malformed_summary_issues(items: Any) -> tuple[str, list]:
    """
    Handle the local fine-tuned model's most common submit_findings malformation:
    passing summary+issues combined under a single non-schema `summary_issues` list
    kwarg instead of separate `summary` (str) and `issues` (list[dict]) params.
    Confirmed 2026-07-23 (governance/rum/synthetics): observed shapes include
    [str], [{}, str], and [issue_dict, ...] (sometimes using "details" instead of
    "description"). This is why the blank-summary retry nudge didn't help — the
    model repeats the same wrong parameter shape on retry, just with different text.
    """
    if not isinstance(items, list):
        return "", []
    summary_parts: list[str] = []
    extra_issues: list = []
    for item in items:
        if isinstance(item, str) and item.strip():
            summary_parts.append(item.strip())
        elif isinstance(item, dict) and item:
            if not item.get("description") and item.get("details"):
                details = item["details"]
                item = {**item, "description": details if isinstance(details, str) else json.dumps(details)}
            extra_issues.append(item)
    return " ".join(summary_parts), extra_issues


def make_submit_fn(collector: dict, domain: str):
    """
    Return a submit_findings callable that stores structured findings in collector[domain].
    Designed to be registered in tool_fns as "submit_findings".
    """

    def submit_findings(
        summary: str = "",
        issues: list = None,
        services_active: list = None,
        services_silent: list = None,
        instrumentation_score: int = None,
        metrics: dict = None,
        actions_taken: list = None,
        **kwargs,  # absorb extra fields the model may pass
    ) -> str:
        extra_summary, extra_issues = _split_malformed_summary_issues(kwargs.pop("summary_issues", None))
        if not str(summary or "").strip() and extra_summary:
            summary = extra_summary
        if not issues and extra_issues:
            issues = extra_issues
        parsed_issues = []
        for i in (issues or []):
            if isinstance(i, dict):
                # Strip unknown keys so Issue() doesn't choke on extra fields. Also
                # backfill missing required fields (severity/domain/description) with
                # safe defaults — confirmed 2026-07-22 round 7: the local fine-tuned
                # model sometimes omits a required field (e.g. nests "recommendation"/
                # "domain" inside "action_args" instead of top-level), which previously
                # crashed Issue(**{...}) with a raw TypeError. That exception propagated
                # all the way up and silently discarded a forced last-turn submit_findings
                # call, producing "Agent reached max turns without completing." instead of
                # a partial (if imperfect) report.
                known = {f.name for f in Issue.__dataclass_fields__.values()}
                filtered = {k: v for k, v in i.items() if k in known}
                filtered.setdefault("severity", "medium")
                filtered.setdefault("domain", domain)
                filtered.setdefault("description", "")
                issue = Issue(**filtered)
            else:
                issue = i
            raw_description = str(issue.description) if issue.description else ""
            raw_recommendation = str(issue.recommendation) if issue.recommendation else ""
            # Confirmed 2026-07-23 (round 8 live validation): health/db/synthetics
            # specialists sometimes omit "description" entirely but put the actual
            # finding text in "recommendation". Previously this showed the generic
            # "[Malformed specialist output for this finding]" placeholder even
            # though real, usable content was present — salvage it instead.
            if not raw_description.strip() and raw_recommendation.strip():
                issue.description = _clean_findings_text(raw_recommendation)
                issue.recommendation = "No specific recommendation provided."
            else:
                issue.description = _clean_findings_text(
                    raw_description,
                    fallback="[Malformed specialist output for this finding]",
                )
                issue.recommendation = (
                    _clean_findings_text(raw_recommendation) or "No specific recommendation provided."
                )
            parsed_issues.append(issue)
        # summary is normally a string, but the model occasionally passes a dict
        # (confirmed 2026-07-22 round 7: "Tool submit_findings failed: expected
        # string or bytes-like object, got 'dict'") — coerce defensively.
        if isinstance(summary, dict):
            summary = summary.get("text") or summary.get("summary") or json.dumps(summary)
        collector[domain] = SpecialistFindings(
            domain=domain,
            summary=_clean_findings_text(str(summary or ""), fallback=f"[{domain} specialist output malformed]"),
            services_active=services_active or [],
            services_silent=services_silent or [],
            instrumentation_score=instrumentation_score,
            issues=parsed_issues,
            metrics=metrics or {},
            actions_taken=actions_taken or [],
            structured=True,
        )
        return "Findings recorded. Assessment complete."

    return submit_findings
