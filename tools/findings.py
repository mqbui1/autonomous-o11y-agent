"""
Structured findings output — each specialist calls submit_findings as its final action.

The coordinator reads the structured data to:
  - Build rich persistent state           (Gap 3)
  - Detect cross-domain issues            (Gap 4)
  - Feed the synthesis pass               (Gap 5 + 6)
  - Track action feedback between runs    (Gap 7)
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Issue:
    severity: str        # critical | high | medium | low
    domain: str
    description: str
    recommendation: str
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


def make_submit_fn(collector: dict, domain: str):
    """
    Return a submit_findings callable that stores structured findings in collector[domain].
    Designed to be registered in tool_fns as "submit_findings".
    """

    def submit_findings(
        summary: str,
        issues: list,
        services_active: list = None,
        services_silent: list = None,
        instrumentation_score: int = None,
        metrics: dict = None,
        actions_taken: list = None,
    ) -> str:
        parsed_issues = []
        for i in (issues or []):
            if isinstance(i, dict):
                # Strip unknown keys so Issue() doesn't choke on extra fields
                known = {f.name for f in Issue.__dataclass_fields__.values()}
                parsed_issues.append(Issue(**{k: v for k, v in i.items() if k in known}))
            else:
                parsed_issues.append(i)
        collector[domain] = SpecialistFindings(
            domain=domain,
            summary=summary,
            services_active=services_active or [],
            services_silent=services_silent or [],
            instrumentation_score=instrumentation_score,
            issues=parsed_issues,
            metrics=metrics or {},
            actions_taken=actions_taken or [],
        )
        return "Findings recorded. Assessment complete."

    return submit_findings
