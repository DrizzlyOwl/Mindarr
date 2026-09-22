"""app_settings key/value store and recommendations_cache."""

import json
import logging
from typing import Optional, Dict, Any

from plex_recommender.db import get_connection

logger = logging.getLogger(__name__)


def set_setting(key: str, value: str):
    """Store key/value setting in app_settings table."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO app_settings (key, value, updated_at)
    VALUES (?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
    """, (key, value))
    conn.commit()
    conn.close()


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Retrieve setting by key."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def get_cached_recommendations(cache_key: str, current_sync_version: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Retrieve cached recommendations response if present and fresh against Plex sync version.
    Returns None if cache miss or if sync_version does not match current_sync_version.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT sync_version, cached_at, response_json FROM recommendations_cache WHERE cache_key = ?",
        (cache_key,)
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None

    # Invalidate if Plex has synced to a new version
    if current_sync_version is not None and row["sync_version"] != current_sync_version:
        return None

    try:
        data = json.loads(row["response_json"])
        data["from_cache"] = True
        data["cached_at"] = row["cached_at"]
        data["sync_version"] = row["sync_version"]
        return data
    except Exception as e:
        logger.error(f"Failed to parse cached recommendations for key '{cache_key}': {e}")
        return None


def set_cached_recommendations(cache_key: str, sync_version: Optional[str], response_data: Dict[str, Any]):
    """
    Cache recommendation response tied to specific Plex sync version.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO recommendations_cache (cache_key, sync_version, cached_at, response_json)
    VALUES (?, ?, CURRENT_TIMESTAMP, ?)
    ON CONFLICT(cache_key) DO UPDATE SET
        sync_version = excluded.sync_version,
        cached_at = CURRENT_TIMESTAMP,
        response_json = excluded.response_json
    """, (cache_key, sync_version, json.dumps(response_data)))
    conn.commit()
    conn.close()


def clear_recommendations_cache(cache_key: Optional[str] = None):
    """
    Clear all cached recommendations, or a specific cache_key.
    """
    conn = get_connection()
    cur = conn.cursor()
    if cache_key:
        cur.execute("DELETE FROM recommendations_cache WHERE cache_key = ?", (cache_key,))
    else:
        cur.execute("DELETE FROM recommendations_cache")
    conn.commit()
    conn.close()


def clear_user_recommendations_cache(user_key: str):
    """Clear all cached recommendations pools for a specific user."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM recommendations_cache WHERE cache_key LIKE ?", (f"{user_key}:%",))
    conn.commit()
    conn.close()
