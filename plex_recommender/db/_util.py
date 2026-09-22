"""Small shared helpers used by multiple db submodules (cycle-free)."""

import re
from datetime import datetime
from typing import Optional


def normalize_title(title: str) -> str:
    """Normalize title for fuzzy matching."""
    if not title:
        return ""
    cleaned = re.sub(r"[^a-zA-Z0-9 ]+", "", title.lower()).strip()
    return " ".join(cleaned.split())


def parse_iso_to_epoch(ts_str: Optional[str]) -> Optional[float]:
    """Parse an ISO 8601 string (with or without timezone) into a UTC epoch timestamp."""
    if not ts_str:
        return None
    try:
        cleaned = str(ts_str).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is not None:
            return dt.timestamp()
        return dt.astimezone().timestamp()
    except Exception:
        return None
