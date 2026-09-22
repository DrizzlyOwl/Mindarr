"""Users table: accounts, onboarding, and per-user data footprint/admin tools."""

from pathlib import Path
from typing import Optional, Dict, Any, List
import sqlite3

from plex_recommender.config import settings
from plex_recommender.db import get_connection


def create_or_update_user(user: Dict[str, Any]) -> None:
    """Insert or update a user record."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO users (user_key, plex_uuid, username, email, title, thumb, plex_token, is_admin, last_login)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(user_key) DO UPDATE SET
        plex_uuid = excluded.plex_uuid,
        username = excluded.username,
        email = excluded.email,
        title = excluded.title,
        thumb = excluded.thumb,
        plex_token = excluded.plex_token,
        last_login = CURRENT_TIMESTAMP
    """, (
        str(user.get("user_key")),
        user.get("plex_uuid"),
        user.get("username"),
        user.get("email"),
        user.get("title"),
        user.get("thumb"),
        user.get("plex_token"),
        1 if user.get("is_admin") else 0,
    ))
    conn.commit()
    conn.close()


def upsert_discovered_user(user: Dict[str, Any]) -> None:
    """Insert a newly discovered shared user without overwriting login state or credentials."""
    user_key = str(user.get("user_key") or "")
    if not user_key:
        return
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO users (user_key, plex_uuid, username, email, title, thumb, plex_token, is_admin, last_login)
    VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL)
    ON CONFLICT(user_key) DO UPDATE SET
        username = COALESCE(excluded.username, users.username),
        email = COALESCE(excluded.email, users.email),
        title = COALESCE(excluded.title, users.title),
        thumb = COALESCE(excluded.thumb, users.thumb)
    """, (
        user_key,
        user.get("plex_uuid"),
        user.get("username"),
        user.get("email"),
        user.get("title"),
        user.get("thumb"),
    ))
    conn.commit()
    conn.close()


def get_user(user_key: str) -> Optional[Dict[str, Any]]:
    """Fetch a user by user_key."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_key = ?", (str(user_key),))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def touch_last_seen(user_key: str) -> Optional[str]:
    """Update a user's last_seen_at to now and return the *previous* value.

    Intended to be called once per login, right after the session is created,
    so callers can surface a "welcome back" message using the value that was
    current before this call overwrote it.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT last_seen_at FROM users WHERE user_key = ?", (str(user_key),))
    row = cur.fetchone()
    previous = row["last_seen_at"] if row else None
    cur.execute(
        "UPDATE users SET last_seen_at = CURRENT_TIMESTAMP WHERE user_key = ?",
        (str(user_key),),
    )
    conn.commit()
    conn.close()
    return previous


def mark_onboarded(user_key: str) -> None:
    """Mark a user as having completed onboarding (idempotent)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET onboarded_at = CURRENT_TIMESTAMP WHERE user_key = ? AND onboarded_at IS NULL",
        (str(user_key),),
    )
    conn.commit()
    conn.close()


def get_admin() -> Optional[Dict[str, Any]]:
    """Return the administrator user, if one exists."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE is_admin = 1 ORDER BY created_at ASC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def admin_exists() -> bool:
    """Return True if an administrator has been established."""
    return get_admin() is not None


def get_all_users() -> List[Dict[str, Any]]:
    """Return all known users."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY is_admin DESC, created_at ASC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_database_stats() -> Dict[str, Any]:
    """Return database file size and per-table row counts."""
    conn = get_connection()
    cur = conn.cursor()
    tables = [
        "users", "user_media", "media_items", "watch_events",
        "seen_identifiers", "recommendations_cache", "job_history", "app_settings",
        "user_dismissals", "user_votes",
    ]
    table_counts = {}
    for t in tables:
        try:
            table_counts[t] = cur.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
        except sqlite3.OperationalError:
            table_counts[t] = 0
    conn.close()

    db_path = settings.db_path
    try:
        size_bytes = int(Path(db_path).stat().st_size)
    except (OSError, TypeError):
        size_bytes = 0

    return {
        "size_bytes": size_bytes,
        "tables": table_counts,
        "total_rows": sum(table_counts.values()),
    }


def get_user_data_summary() -> List[Dict[str, Any]]:
    """Per-user data footprint: watched items, watch events, seen ids, last login."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY is_admin DESC, created_at ASC")
    users = [dict(r) for r in cur.fetchall()]

    for u in users:
        uk = u["user_key"]
        u["media_count"] = cur.execute(
            "SELECT COUNT(*) AS c FROM user_media WHERE user_key = ?", (uk,)
        ).fetchone()["c"]
        u["event_count"] = cur.execute(
            "SELECT COUNT(*) AS c FROM watch_events WHERE user_key = ?", (uk,)
        ).fetchone()["c"]
        u["seen_count"] = cur.execute(
            "SELECT COUNT(*) AS c FROM seen_identifiers WHERE user_key = ?", (uk,)
        ).fetchone()["c"]
    conn.close()
    return users


def clear_user_data(user_key: str) -> Dict[str, int]:
    """Delete a user's watch data (media, events, seen index, cached recs).

    Keeps the user account itself so they remain authorized. Returns row counts
    deleted per table.
    """
    conn = get_connection()
    cur = conn.cursor()
    uk = str(user_key)
    deleted = {}
    for table in ("user_media", "watch_events", "seen_identifiers", "user_dismissals", "user_votes"):
        cur.execute(f"DELETE FROM {table} WHERE user_key = ?", (uk,))
        deleted[table] = cur.rowcount
    # Recommendations cache is keyed by "<user_key>:..." — clear this user's entries.
    cur.execute("DELETE FROM recommendations_cache WHERE cache_key LIKE ?", (f"{uk}:%",))
    deleted["recommendations_cache"] = cur.rowcount
    conn.commit()
    conn.close()
    return deleted


def delete_user(user_key: str) -> Dict[str, int]:
    """Delete a user account and all their associated watch and cache data.

    Returns row counts deleted per table.
    """
    deleted = clear_user_data(user_key)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE user_key = ?", (str(user_key),))
    deleted["users"] = cur.rowcount
    conn.commit()
    conn.close()
    return deleted
