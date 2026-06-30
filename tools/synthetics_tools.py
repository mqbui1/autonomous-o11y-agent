"""
Synthetics tools for Splunk Observability Cloud (Splunk Synthetics).

Assesses synthetic test coverage and health:
- List all browser, API, and uptime tests configured in the org
- Retrieve recent pass/fail results and compute uptime percentages
- Identify services and URLs with no synthetic coverage
- Surface tests that are failing, paused, or running too infrequently

API: https://api.{realm}.signalfx.com/v2/synthetics/
"""

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

from ._runner import get_config

logger = logging.getLogger(__name__)


def _api(path: str, payload: dict = None, method: str = "GET") -> dict:
    cfg = get_config()
    url = f"https://api.{cfg.realm}.signalfx.com{path}"
    headers = {"X-SF-TOKEN": cfg.token, "Content-Type": "application/json"}
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"API {method} {path} returned {e.code}: {body}") from e


# ── Synthetics Tool Functions ─────────────────────────────────────────────────

def list_synthetics_tests(environment: str = "") -> str:
    """
    List all Splunk Synthetics tests configured in the org.

    Returns each test's name, type (browser/api/uptime), active status,
    frequency, last run status, and location coverage. Use this as the
    starting point to assess what synthetic coverage exists and find gaps.

    Args:
        environment: Optional filter — only return tests whose name contains
                     this string (case-insensitive). Leave empty for all tests.
    """
    try:
        data = _api("/v2/synthetics/tests")
        tests = data.get("tests", [])

        if environment:
            env_lower = environment.lower()
            tests = [
                t for t in tests
                if env_lower in t.get("name", "").lower()
                or env_lower in json.dumps(t.get("tags", [])).lower()
            ]

        trimmed = []
        for t in tests:
            trimmed.append({
                "id": t.get("id"),
                "name": t.get("name"),
                "type": t.get("type"),          # browser | api | uptime
                "active": t.get("active", True),
                "frequency_minutes": t.get("frequency"),
                "last_run_status": t.get("lastRunStatus", {}).get("status") if isinstance(t.get("lastRunStatus"), dict) else t.get("lastRunStatus"),
                "last_run_at": t.get("lastRunAt"),
                "location_count": len(t.get("locationIds", [])),
                "locations": t.get("locationIds", [])[:3],  # first 3
            })

        by_type = {}
        for t in trimmed:
            by_type.setdefault(t["type"], 0)
            by_type[t["type"]] += 1

        failing = [t for t in trimmed if t.get("last_run_status") in ("failure", "failed", "FAILED")]
        inactive = [t for t in trimmed if not t.get("active")]

        return json.dumps({
            "total_tests": len(trimmed),
            "by_type": by_type,
            "failing_count": len(failing),
            "inactive_count": len(inactive),
            "failing_tests": [t["name"] for t in failing],
            "tests": trimmed,
        }, indent=2)
    except Exception as exc:
        return f"[list_synthetics_tests error]: {exc}"


def get_test_results(test_id: str, hours: int = 24) -> str:
    """
    Get recent run results for a specific synthetic test.

    Returns each individual test run with duration, status (success/failure),
    and any error details. Use this to understand how frequently a test fails
    and what errors are occurring.

    Args:
        test_id: Synthetic test ID (from list_synthetics_tests).
        hours: How far back to retrieve results (default: 24).
    """
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)
        start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        path = (
            f"/v2/synthetics/tests/{test_id}/results"
            f"?page=1&perPage=50&startTime={start_iso}&endTime={end_iso}"
        )
        data = _api(path)
        results = data.get("results", [])

        total = len(results)
        successes = sum(1 for r in results if r.get("status") in ("success", "SUCCESS", "pass", "PASS"))
        failures = total - successes
        uptime_pct = round(successes / total * 100, 1) if total > 0 else None

        # Collect unique error messages
        errors: dict[str, int] = {}
        for r in results:
            for err in r.get("errors", []) or []:
                msg = err.get("message") or str(err)[:120]
                errors[msg] = errors.get(msg, 0) + 1

        durations = [r.get("duration") for r in results if r.get("duration")]
        avg_duration_ms = round(sum(durations) / len(durations)) if durations else None
        p99_duration_ms = sorted(durations)[int(len(durations) * 0.99)] if len(durations) > 1 else durations[0] if durations else None

        return json.dumps({
            "test_id": test_id,
            "lookback_hours": hours,
            "total_runs": total,
            "successful_runs": successes,
            "failed_runs": failures,
            "uptime_pct": uptime_pct,
            "avg_duration_ms": avg_duration_ms,
            "p99_duration_ms": p99_duration_ms,
            "top_errors": sorted(errors.items(), key=lambda x: -x[1])[:5],
            "recent_results": [
                {
                    "startedAt": r.get("startedAt"),
                    "status": r.get("status"),
                    "duration_ms": r.get("duration"),
                    "location": r.get("locationId") or r.get("location"),
                }
                for r in results[:10]
            ],
        }, indent=2)
    except Exception as exc:
        return f"[get_test_results error]: {exc}"


def get_synthetics_coverage_gaps(services: list, environment: str = "") -> str:
    """
    Identify which services have no synthetic test coverage.

    Compares the provided list of instrumented services against the names of
    existing synthetic tests. Services with no matching test are flagged as
    coverage gaps — they have no external health validation.

    Args:
        services: List of service names known to be active (e.g. from APM).
        environment: Optional filter to scope tests by name/tag.
    """
    try:
        data = _api("/v2/synthetics/tests")
        tests = data.get("tests", [])

        # Build a set of words from all test names for fuzzy matching
        test_names_lower = {t.get("name", "").lower() for t in tests}
        test_name_blob = " ".join(test_names_lower)

        covered = []
        gaps = []
        for svc in services:
            svc_lower = svc.lower().replace("-", " ").replace("_", " ").replace("service", "").strip()
            if any(svc_lower in name for name in test_names_lower) or svc_lower in test_name_blob:
                covered.append(svc)
            else:
                gaps.append(svc)

        return json.dumps({
            "total_services": len(services),
            "covered_count": len(covered),
            "gap_count": len(gaps),
            "coverage_pct": round(len(covered) / len(services) * 100, 1) if services else 0,
            "services_with_no_synthetics": gaps,
            "services_with_synthetics": covered,
            "total_tests": len(tests),
            "note": (
                "Coverage is inferred by matching service names to test names. "
                "A match does not guarantee the test actually exercises that service's "
                "critical paths — review test details for completeness."
            ),
        }, indent=2)
    except Exception as exc:
        return f"[get_synthetics_coverage_gaps error]: {exc}"


def get_test_performance_trend(test_id: str, hours: int = 48) -> str:
    """
    Get the performance trend for a synthetic test over time.

    Computes average duration per hour to surface tests that are getting
    progressively slower — a leading indicator of degradation before outright
    failure. Also identifies tests with high location variance (failing in
    some regions but passing in others).

    Args:
        test_id: Synthetic test ID.
        hours: Lookback window in hours (default: 48).
    """
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)
        start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        path = (
            f"/v2/synthetics/tests/{test_id}/results"
            f"?page=1&perPage=200&startTime={start_iso}&endTime={end_iso}"
        )
        data = _api(path)
        results = data.get("results", [])

        if not results:
            return json.dumps({"test_id": test_id, "note": "No results in this time window"})

        # Group by hour and by location
        by_hour: dict[str, list] = {}
        by_location: dict[str, dict] = {}

        for r in results:
            started = r.get("startedAt", "")[:13]  # "2024-01-15T10"
            dur = r.get("duration")
            loc = r.get("locationId") or r.get("location") or "unknown"
            status = r.get("status", "")
            success = status in ("success", "SUCCESS", "pass", "PASS")

            if dur:
                by_hour.setdefault(started, []).append(dur)

            if loc not in by_location:
                by_location[loc] = {"total": 0, "success": 0, "durations": []}
            by_location[loc]["total"] += 1
            if success:
                by_location[loc]["success"] += 1
            if dur:
                by_location[loc]["durations"].append(dur)

        hourly_avg = {
            hour: round(sum(durs) / len(durs))
            for hour, durs in sorted(by_hour.items())
        }

        location_summary = {}
        for loc, stats in by_location.items():
            uptime = round(stats["success"] / stats["total"] * 100, 1) if stats["total"] else 0
            avg_dur = round(sum(stats["durations"]) / len(stats["durations"])) if stats["durations"] else None
            location_summary[loc] = {"uptime_pct": uptime, "avg_duration_ms": avg_dur, "runs": stats["total"]}

        # Detect trend: compare first half avg vs second half avg
        hours_list = sorted(hourly_avg.keys())
        mid = len(hours_list) // 2
        if mid > 0:
            first_half_avg = sum(hourly_avg[h] for h in hours_list[:mid]) / mid
            second_half_avg = sum(hourly_avg[h] for h in hours_list[mid:]) / max(len(hours_list) - mid, 1)
            trend_pct = round((second_half_avg - first_half_avg) / first_half_avg * 100, 1) if first_half_avg else 0
        else:
            trend_pct = 0

        return json.dumps({
            "test_id": test_id,
            "lookback_hours": hours,
            "duration_trend_pct": trend_pct,
            "trend_direction": "degrading" if trend_pct > 10 else "improving" if trend_pct < -10 else "stable",
            "hourly_avg_duration_ms": hourly_avg,
            "by_location": location_summary,
        }, indent=2)
    except Exception as exc:
        return f"[get_test_performance_trend error]: {exc}"


# ── Tool registry ─────────────────────────────────────────────────────────────

SCHEMAS = [
    {
        "toolSpec": {
            "name": "list_synthetics_tests",
            "description": (
                "List all Splunk Synthetics tests (browser, API, uptime) configured in the org. "
                "Returns test names, types, active status, frequency, last run status, and location count. "
                "Use as the starting point to assess synthetic coverage and find failing tests."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "environment": {
                            "type": "string",
                            "description": "Filter tests whose name/tags contain this string. Leave empty for all tests.",
                        }
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_test_results",
            "description": (
                "Get recent run results for a specific synthetic test. "
                "Returns pass/fail per run, computed uptime %, average duration, p99 duration, "
                "and top error messages. Use to understand failure frequency and error types."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["test_id"],
                    "properties": {
                        "test_id": {"type": "string", "description": "Test ID from list_synthetics_tests."},
                        "hours": {"type": "integer", "description": "Lookback window in hours (default: 24)."},
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_synthetics_coverage_gaps",
            "description": (
                "Identify which services have no synthetic test coverage. "
                "Compares a list of known active services against existing test names. "
                "Services with no matching test are returned as coverage gaps — "
                "they have no external health validation."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["services"],
                    "properties": {
                        "services": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of active service names to check for synthetics coverage.",
                        },
                        "environment": {
                            "type": "string",
                            "description": "Optional: scope tests by name/tag match.",
                        },
                    },
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "get_test_performance_trend",
            "description": (
                "Get the performance trend for a synthetic test over time. "
                "Detects tests that are getting progressively slower (degrading trend) and "
                "identifies location-specific failures (test fails in one region but passes in others)."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["test_id"],
                    "properties": {
                        "test_id": {"type": "string", "description": "Synthetic test ID."},
                        "hours": {"type": "integer", "description": "Lookback window in hours (default: 48)."},
                    },
                }
            },
        }
    },
]

TOOL_FNS = {
    "list_synthetics_tests": list_synthetics_tests,
    "get_test_results": get_test_results,
    "get_synthetics_coverage_gaps": get_synthetics_coverage_gaps,
    "get_test_performance_trend": get_test_performance_trend,
}
