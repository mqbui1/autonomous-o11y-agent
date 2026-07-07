"""
Adoption tools — org-level asset hygiene and SDK coverage checks.

Pulls three targeted signals from o11y-adoption data:
  1. Broken detectors  — no notifications or disabled
  2. Token health      — expired or expiring within 7 days
  3. SDK coverage      — telemetry.sdk.language per service (Dimension API)
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ._runner import get_config

logger = logging.getLogger(__name__)


def _api(path: str, params: dict | None = None) -> dict:
    cfg = get_config()
    url = f"https://api.{cfg.realm}.signalfx.com{path}"
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    req = urllib.request.Request(url, headers={"X-SF-TOKEN": cfg.token})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


# ── 1. Broken detectors ───────────────────────────────────────────────────────

def get_broken_detectors() -> str:
    """
    List detectors that are silent black-holes: disabled or have zero notification
    rules configured. These will never page anyone when they fire.

    Returns counts and names of broken detectors so the specialist can flag them.
    """
    try:
        results = _api("/v2/detector", {"limit": 1000}).get("results", [])
    except Exception as exc:
        return json.dumps({"error": str(exc), "note": "Could not fetch detector list"})

    disabled, no_notif = [], []
    for d in results:
        name = d.get("name", d.get("id", "?"))
        if not d.get("active", True):
            disabled.append(name)
            continue
        # A detector fires but no rule has notifications → silent alert
        rules = d.get("rules") or []
        if all(not (r.get("notifications") or []) for r in rules):
            no_notif.append(name)

    return json.dumps({
        "total_detectors": len(results),
        "disabled_count": len(disabled),
        "no_notification_count": len(no_notif),
        "disabled": disabled[:50],
        "no_notifications": no_notif[:50],
        "note": (
            "Disabled detectors will never fire. Detectors with no notification rules "
            "fire internally but page nobody — equivalent to a broken smoke alarm."
        ),
    }, indent=2)


# ── 2. Token health ───────────────────────────────────────────────────────────

def get_token_health() -> str:
    """
    Check for expired or soon-to-expire API/ingest tokens.

    Returns tokens expiring within 7 days and already-expired tokens.
    An expired token silently breaks all API calls and data ingestion.
    """
    try:
        results = _api("/v2/token", {"limit": 1000}).get("results", [])
    except Exception as exc:
        return json.dumps({"error": str(exc), "note": "Could not fetch token list"})

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    seven_days_ms = 7 * 24 * 3600 * 1000

    expired, expiring_soon, healthy = [], [], []
    for t in results:
        name = t.get("name", t.get("id", "?"))
        expiry = t.get("expires")  # None means never expires
        if expiry is None:
            healthy.append({"name": name, "expires": "never"})
            continue
        if expiry < now_ms:
            expired.append({"name": name, "expires_ms": expiry})
        elif expiry < now_ms + seven_days_ms:
            days_left = (expiry - now_ms) // (24 * 3600 * 1000)
            expiring_soon.append({"name": name, "days_remaining": days_left})
        else:
            healthy.append({"name": name, "expires": "ok"})

    return json.dumps({
        "total_tokens": len(results),
        "expired": expired,
        "expiring_within_7_days": expiring_soon,
        "healthy_count": len(healthy),
        "note": (
            "Expired tokens silently break data ingestion and API calls with no error "
            "surfaced in dashboards. Rotate before expiry."
        ),
    }, indent=2)


# ── 3. SDK coverage ───────────────────────────────────────────────────────────

def get_sdk_coverage() -> str:
    """
    Query the Splunk Dimension API for telemetry.sdk.language and
    telemetry.sdk.version to understand which languages and SDK versions
    are in use. Flags outdated SDK versions that miss modern semantic conventions.
    """
    def dim_values(key: str, limit: int = 200) -> list[dict]:
        try:
            r = _api("/v2/dimension", {"query": f"key:{key}", "limit": limit})
            return r.get("results", [])
        except Exception:
            return []

    lang_dims    = dim_values("telemetry.sdk.language")
    version_dims = dim_values("telemetry.sdk.version")
    name_dims    = dim_values("telemetry.sdk.name")

    languages = sorted({d["value"] for d in lang_dims if d.get("value")})
    versions  = sorted({d["value"] for d in version_dims if d.get("value")})
    sdk_names = sorted({d["value"] for d in name_dims if d.get("value")})

    # Known minimum versions for full semantic convention coverage (OTel 1.x stable)
    MIN_VERSIONS: dict[str, str] = {
        "java":   "1.32.0",
        "python": "1.20.0",
        "nodejs": "1.18.0",
        "go":     "1.21.0",
        "dotnet": "1.7.0",
    }

    outdated = []
    for v in versions:
        parts = v.split(".")
        if len(parts) >= 2:
            try:
                major = int(parts[0])
                if major == 0:
                    outdated.append({"version": v, "reason": "pre-1.0 SDK — unstable semconv"})
            except ValueError:
                pass

    return json.dumps({
        "languages_detected": languages,
        "sdk_names": sdk_names,
        "sdk_versions_in_use": versions,
        "outdated_or_pre_stable": outdated,
        "note": (
            "SDK language/version come from telemetry.sdk.* span resource attributes. "
            "Pre-1.0 SDKs use unstable semantic conventions — span attribute names may "
            "differ from current OTel spec, causing gaps in service maps and APM metrics."
        ),
    }, indent=2)


# ── Schemas + registry ────────────────────────────────────────────────────────

SCHEMAS: list[dict] = [
    {
        "toolSpec": {
            "name": "get_broken_detectors",
            "description": (
                "List detectors that are disabled or have no notification rules configured. "
                "These will never alert on-call when they fire."
            ),
            "inputSchema": {"json": {"type": "object", "properties": {}, "required": []}},
        }
    },
    {
        "toolSpec": {
            "name": "get_token_health",
            "description": (
                "Check for expired or soon-to-expire API/ingest tokens. "
                "An expired token silently breaks data ingestion with no dashboard warning."
            ),
            "inputSchema": {"json": {"type": "object", "properties": {}, "required": []}},
        }
    },
    {
        "toolSpec": {
            "name": "get_sdk_coverage",
            "description": (
                "Query which OTel SDK languages and versions are in use across the org. "
                "Flags pre-1.0 (unstable semconv) SDK versions that cause attribute coverage gaps."
            ),
            "inputSchema": {"json": {"type": "object", "properties": {}, "required": []}},
        }
    },
]

TOOL_FNS: dict = {
    "get_broken_detectors": lambda **_: get_broken_detectors(),
    "get_token_health":     lambda **_: get_token_health(),
    "get_sdk_coverage":     lambda **_: get_sdk_coverage(),
}
