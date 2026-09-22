"""Persisted integration health status (TMDb, Overseerr, Tautulli).

Backed by the generic app_settings key/value store (same mechanism as
`last_sync_time`), so no schema migration is needed. Written by the
`healthcheck` background job (see jobs.py) and read by the web layer to
render the admin health banner and Settings-page status badges.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from plex_recommender.db.recommendations import get_setting, set_setting

INTEGRATIONS = ("tmdb", "overseerr", "tautulli")


def record_health(name: str, ok: bool, message: str) -> None:
    """Persist the latest health check result for an integration."""
    if name not in INTEGRATIONS:
        raise ValueError(f"Unknown integration: {name}")
    set_setting(f"health_{name}_ok", "true" if ok else "false")
    set_setting(f"health_{name}_message", message)
    set_setting(f"health_{name}_checked_at", datetime.now(timezone.utc).isoformat())


def clear_health(name: str) -> None:
    """Clear a stored health result (e.g. when an integration becomes unconfigured)."""
    if name not in INTEGRATIONS:
        raise ValueError(f"Unknown integration: {name}")
    set_setting(f"health_{name}_ok", "")
    set_setting(f"health_{name}_message", "")
    set_setting(f"health_{name}_checked_at", "")


def get_health(name: str) -> Dict[str, Any]:
    """Return the last known health status for an integration.

    {"ok": True/False/None, "message": str|None, "checked_at": str|None}
    `ok` is None when no health check has ever run (or was cleared).
    """
    if name not in INTEGRATIONS:
        raise ValueError(f"Unknown integration: {name}")
    raw_ok = get_setting(f"health_{name}_ok")
    ok: Optional[bool]
    if raw_ok == "true":
        ok = True
    elif raw_ok == "false":
        ok = False
    else:
        ok = None
    return {
        "ok": ok,
        "message": get_setting(f"health_{name}_message") or None,
        "checked_at": get_setting(f"health_{name}_checked_at") or None,
    }


def get_all_health() -> Dict[str, Dict[str, Any]]:
    """Return health status for every tracked integration."""
    return {name: get_health(name) for name in INTEGRATIONS}


def get_unhealthy() -> Dict[str, Dict[str, Any]]:
    """Return only the integrations currently flagged unhealthy (ok is False)."""
    return {name: status for name, status in get_all_health().items() if status["ok"] is False}
