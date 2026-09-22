"""Per-user watch history: watch_events, user_media, seen_identifiers, and stats."""

import json
from typing import Optional, Dict, Any, List, Set

from plex_recommender.db import get_connection
from plex_recommender.db._util import normalize_title, parse_iso_to_epoch
from plex_recommender.db.media import upsert_media_item
from plex_recommender.db.migrations import _reconcile_watch_events_migration

SESSION_WINDOW_SECONDS = 14400  # 4-hour window for matching playback sessions


def add_seen_identifier(user_key: str, id_type: str, id_value: str, title: str = "", year: Optional[int] = None):
    """Add a seen identifier to the fast lookup index for a specific user."""
    if not id_value:
        return
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    INSERT OR IGNORE INTO seen_identifiers (user_key, id_type, id_value, title, year)
    VALUES (?, ?, ?, ?, ?)
    """, (str(user_key), id_type.lower(), str(id_value).strip().lower(), title, year))
    conn.commit()
    conn.close()


def upsert_user_media(user_key: str, item: Dict[str, Any]):
    """Record that a user has watched an item, and register per-user seen identifiers.

    The rich metadata is stored (shared) in media_items via upsert_media_item;
    this function records the per-user relation + seen-index entries.
    """
    upsert_media_item(item)

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO user_media (user_key, item_id, view_count, last_viewed_at, user_rating)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(user_key, item_id) DO UPDATE SET
        view_count = MAX(user_media.view_count, excluded.view_count),
        last_viewed_at = excluded.last_viewed_at,
        user_rating = COALESCE(excluded.user_rating, user_media.user_rating)
    """, (
        str(user_key),
        str(item.get("item_id")),
        item.get("view_count", 1) or 1,
        item.get("last_viewed_at"),
        item.get("user_rating"),
    ))

    title = item.get("title", "")
    year = item.get("year")
    for id_type in ("imdb_id", "tmdb_id", "tvdb_id"):
        if item.get(id_type):
            cur.execute(
                "INSERT OR IGNORE INTO seen_identifiers (user_key, id_type, id_value, title, year) VALUES (?, ?, ?, ?, ?)",
                (str(user_key), id_type.replace("_id", ""), str(item[id_type]).lower(), title, year),
            )

    norm_title = normalize_title(title)
    if norm_title:
        val = f"{norm_title}::{year or ''}"
        cur.execute(
            "INSERT OR IGNORE INTO seen_identifiers (user_key, id_type, id_value, title, year) VALUES (?, ?, ?, ?, ?)",
            (str(user_key), "title_year", val, title, year),
        )

    conn.commit()
    conn.close()


def record_watch_event(event: Dict[str, Any]):
    """Record a playback history event (scoped to a user_key), reconciling duplicates across Plex & Tautulli.

    Plex is Primary: when a Tautulli event arrives for a session already logged by Plex
    within 4 hours, the existing Plex event is enriched with duration and title rather
    than inserting a duplicate row.
    """
    conn = get_connection()
    cur = conn.cursor()
    user_key = str(event.get("user_key", ""))
    item_id = str(event.get("item_id", ""))
    incoming_source = (event.get("source") or "plex").lower()
    viewed_at = event.get("viewed_at")
    incoming_epoch = parse_iso_to_epoch(viewed_at)
    incoming_duration = int(event.get("duration_watched", 0) or 0)
    incoming_title = str(event.get("title", "") or "")

    # Look for existing events for this user and item within the session window
    if user_key and item_id and incoming_epoch is not None:
        cur.execute(
            "SELECT id, event_key, title, viewed_at, duration_watched, source FROM watch_events WHERE user_key = ? AND item_id = ?",
            (user_key, item_id),
        )
        existing_rows = cur.fetchall()
        for row in existing_rows:
            existing_epoch = parse_iso_to_epoch(row["viewed_at"])
            if existing_epoch is not None and abs(existing_epoch - incoming_epoch) <= SESSION_WINDOW_SECONDS:
                row_source = (row["source"] or "plex").lower()
                existing_dur = int(row["duration_watched"] or 0)
                existing_title = str(row["title"] or "")

                if incoming_source == "tautulli" and row_source == "plex":
                    # Plex event already exists; enrich with Tautulli's duration and title
                    updates = []
                    params = []
                    if existing_dur <= 0 and incoming_duration > 0:
                        updates.append("duration_watched = ?")
                        params.append(incoming_duration)
                    if " - " in incoming_title and " - " not in existing_title:
                        updates.append("title = ?")
                        params.append(incoming_title)
                    if updates:
                        params.append(row["id"])
                        cur.execute(f"UPDATE watch_events SET {', '.join(updates)} WHERE id = ?", params)
                        conn.commit()
                    conn.close()
                    return

                elif incoming_source == "plex" and row_source == "tautulli":
                    # Tautulli was logged first; promote to Plex as Primary
                    best_dur = incoming_duration if incoming_duration > 0 else existing_dur
                    best_title = incoming_title if " - " in incoming_title or " - " not in existing_title else existing_title
                    cur.execute(
                        "UPDATE watch_events SET source = 'plex', viewed_at = ?, duration_watched = ?, title = ? WHERE id = ?",
                        (viewed_at, best_dur, best_title, row["id"]),
                    )
                    conn.commit()
                    conn.close()
                    return

    # No duplicate session found -> insert new event
    event_key = f"{user_key}_{item_id}_{viewed_at}_{incoming_source}"
    cur.execute("""
    INSERT OR IGNORE INTO watch_events (
        event_key, user_key, item_id, title, media_type, viewed_at, account_id, duration_watched, source
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        event_key,
        user_key,
        item_id,
        incoming_title,
        event.get("media_type", "movie"),
        viewed_at,
        str(event.get("account_id", "")),
        incoming_duration,
        incoming_source,
    ))
    conn.commit()
    conn.close()


def reconcile_watch_events(user_key: Optional[str] = None) -> int:
    """Manually trigger reconciliation of duplicate watch events across Plex & Tautulli."""
    conn = get_connection()
    _reconcile_watch_events_migration(conn)
    conn.close()
    return 0


def is_seen(
    user_key: str,
    imdb_id: Optional[str] = None,
    tmdb_id: Optional[Any] = None,
    tvdb_id: Optional[Any] = None,
    title: Optional[str] = None,
    year: Optional[int] = None
) -> bool:
    """Check whether a given title has already been seen by a specific user."""
    conn = get_connection()
    cur = conn.cursor()
    uk = str(user_key)

    if imdb_id:
        clean_imdb = str(imdb_id).strip().lower()
        cur.execute("SELECT 1 FROM seen_identifiers WHERE user_key = ? AND id_type = 'imdb' AND id_value = ?", (uk, clean_imdb))
        if cur.fetchone():
            conn.close()
            return True

    if tmdb_id:
        clean_tmdb = str(tmdb_id).strip().lower()
        cur.execute("SELECT 1 FROM seen_identifiers WHERE user_key = ? AND id_type = 'tmdb' AND id_value = ?", (uk, clean_tmdb))
        if cur.fetchone():
            conn.close()
            return True

    if tvdb_id:
        clean_tvdb = str(tvdb_id).strip().lower()
        cur.execute("SELECT 1 FROM seen_identifiers WHERE user_key = ? AND id_type = 'tvdb' AND id_value = ?", (uk, clean_tvdb))
        if cur.fetchone():
            conn.close()
            return True

    if title:
        norm_title = normalize_title(title)
        val = f"{norm_title}::{year or ''}"
        cur.execute("SELECT 1 FROM seen_identifiers WHERE user_key = ? AND id_type = 'title_year' AND id_value = ?", (uk, val))
        if cur.fetchone():
            conn.close()
            return True
        # Also check just title if year is missing
        if not year:
            cur.execute("SELECT 1 FROM seen_identifiers WHERE user_key = ? AND id_type = 'title_year' AND id_value LIKE ?", (uk, f"{norm_title}::%"))
            if cur.fetchone():
                conn.close()
                return True

    conn.close()
    return False


def get_user_seen_index(user_key: str) -> Dict[str, Set[str]]:
    """Return a user's seen IDs as sets in memory for fast bulk filtering."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id_type, id_value FROM seen_identifiers WHERE user_key = ?", (str(user_key),))
    rows = cur.fetchall()
    conn.close()

    seen: Dict[str, Set[str]] = {
        "imdb": set(),
        "tmdb": set(),
        "tvdb": set(),
        "title_year": set()
    }
    for row in rows:
        t = row["id_type"]
        if t in seen:
            seen[t].add(row["id_value"])
    return seen


def get_user_media_items(user_key: str) -> List[Dict[str, Any]]:
    """Fetch a user's watched media items (metadata joined from media_items)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT m.*, um.view_count AS user_view_count, um.last_viewed_at AS user_last_viewed_at,
               um.user_rating AS um_user_rating
        FROM user_media um
        JOIN media_items m ON m.item_id = um.item_id
        WHERE um.user_key = ?
        ORDER BY um.last_viewed_at DESC
    """, (str(user_key),))
    rows = cur.fetchall()
    conn.close()

    items = []
    for r in rows:
        d = dict(r)
        d["genres"] = json.loads(d.get("genres") or "[]")
        d["directors"] = json.loads(d.get("directors") or "[]")
        d["writers"] = json.loads(d.get("writers") or "[]")
        d["actors"] = json.loads(d.get("actors") or "[]")
        d["keywords"] = json.loads(d.get("keywords") or "[]")
        d["raw_guids"] = json.loads(d.get("raw_guids") or "[]")
        # Prefer per-user view stats for analysis
        d["view_count"] = d.get("user_view_count") or d.get("view_count")
        d["last_viewed_at"] = d.get("user_last_viewed_at") or d.get("last_viewed_at")
        # Per-user rating is authoritative; fall back to shared media rating.
        if d.get("um_user_rating") is not None:
            d["user_rating"] = d["um_user_rating"]
        items.append(d)
    return items


def has_user_history(user_key: str) -> bool:
    """Return True if the user has any recorded watch data."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM user_media WHERE user_key = ? LIMIT 1", (str(user_key),))
    row = cur.fetchone()
    conn.close()
    return bool(row)


def get_watch_source_breakdown(user_key: str) -> Dict[str, int]:
    """Return counts of watch events per source (plex/tautulli) for a user."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(source, 'plex') AS src, COUNT(*) AS cnt "
        "FROM watch_events WHERE user_key = ? GROUP BY src",
        (str(user_key),),
    )
    rows = cur.fetchall()
    conn.close()
    result = {"plex": 0, "tautulli": 0, "total": 0}
    for r in rows:
        src = r["src"] if r["src"] in ("plex", "tautulli") else "plex"
        result[src] += r["cnt"]
    result["total"] = result["plex"] + result["tautulli"]
    return result


def get_watch_events(user_key: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Fetch watch events, optionally scoped to a user."""
    conn = get_connection()
    cur = conn.cursor()
    if user_key:
        query = "SELECT * FROM watch_events WHERE user_key = ? ORDER BY viewed_at DESC"
        params: tuple = (str(user_key),)
    else:
        query = "SELECT * FROM watch_events ORDER BY viewed_at DESC"
        params = ()
    if limit:
        query += f" LIMIT {int(limit)}"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_enriched_watch_events(user_key: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Fetch recent watch events for user, enriched with media_items metadata and formatted duration.

    Returns up to `limit` rows (max 100), sorted by most recent first.
    """
    conn = get_connection()
    cur = conn.cursor()
    query = """
    SELECT 
        w.id,
        w.event_key,
        w.user_key,
        w.item_id,
        w.title AS event_title,
        w.media_type,
        w.viewed_at,
        w.account_id,
        w.duration_watched,
        COALESCE(w.source, 'plex') AS source,
        m.title AS media_title,
        m.year,
        m.genres,
        m.directors,
        m.actors,
        m.summary,
        m.user_rating,
        m.critic_rating,
        m.audience_rating,
        m.imdb_id,
        m.tmdb_id,
        m.tvdb_id
    FROM watch_events w
    LEFT JOIN media_items m ON (
        m.item_id = w.item_id 
        OR m.item_id = ('movie_' || w.item_id)
        OR m.item_id = ('show_' || w.item_id)
    )
    WHERE w.user_key = ?
    GROUP BY w.id
    ORDER BY w.viewed_at DESC
    LIMIT ?
    """
    cur.execute(query, (str(user_key), int(limit)))
    rows = cur.fetchall()
    conn.close()

    enriched = []
    for r in rows:
        d = dict(r)
        # Parse JSON fields safely
        for field in ("genres", "directors", "actors"):
            val = d.get(field)
            if isinstance(val, str):
                try:
                    d[field] = json.loads(val)
                except Exception:
                    d[field] = []
            elif not isinstance(val, list):
                d[field] = []

        # Title fallback
        d["title"] = d["event_title"] or d["media_title"] or "Unknown Title"

        # Normalized source
        raw_src = (d.get("source") or "plex").lower()
        d["source"] = "tautulli" if raw_src == "tautulli" else "plex"
        d["is_tautulli"] = d["source"] == "tautulli"
        d["provenance_tag"] = "Archive" if d["is_tautulli"] else "Live"

        # Duration formatting
        raw_dur = d.get("duration_watched") or 0
        try:
            raw_dur = int(raw_dur)
        except (ValueError, TypeError):
            raw_dur = 0
        d["raw_duration"] = raw_dur
        # If > 10,000, value is likely milliseconds (standard in Plex)
        seconds = raw_dur // 1000 if raw_dur > 10000 else raw_dur
        d["duration_seconds"] = seconds
        if seconds > 0:
            hrs = seconds // 3600
            mins = (seconds % 3600) // 60
            if hrs > 0:
                d["duration_formatted"] = f"{hrs}h {mins}m" if mins > 0 else f"{hrs}h"
            elif mins > 0:
                d["duration_formatted"] = f"{mins}m"
            else:
                d["duration_formatted"] = f"{seconds}s"
        else:
            d["duration_formatted"] = ""

        # ISO timestamp cleanup for display
        raw_viewed = d.get("viewed_at") or ""
        d["viewed_at_clean"] = raw_viewed[:19].replace("T", " ") if raw_viewed else ""

        enriched.append(d)

    return enriched


def get_stats(user_key: Optional[str] = None) -> Dict[str, Any]:
    """Return summary statistics, scoped to a user when user_key is provided."""
    conn = get_connection()
    cur = conn.cursor()

    if user_key:
        uk = str(user_key)
        cur.execute("""
            SELECT COUNT(*) as cnt FROM user_media um
            JOIN media_items m ON m.item_id = um.item_id
            WHERE um.user_key = ? AND m.media_type = 'movie'
        """, (uk,))
        movies_count = cur.fetchone()["cnt"]

        cur.execute("""
            SELECT COUNT(*) as cnt FROM user_media um
            JOIN media_items m ON m.item_id = um.item_id
            WHERE um.user_key = ? AND (m.media_type = 'show' OR m.media_type = 'episode')
        """, (uk,))
        shows_count = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM watch_events WHERE user_key = ?", (uk,))
        events_count = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM seen_identifiers WHERE user_key = ?", (uk,))
        seen_count = cur.fetchone()["cnt"]
    else:
        cur.execute("SELECT COUNT(*) as cnt FROM media_items WHERE media_type = 'movie'")
        movies_count = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM media_items WHERE media_type = 'show' OR media_type = 'episode'")
        shows_count = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM watch_events")
        events_count = cur.fetchone()["cnt"]

        cur.execute("SELECT COUNT(*) as cnt FROM seen_identifiers")
        seen_count = cur.fetchone()["cnt"]

    cur.execute("SELECT value FROM app_settings WHERE key = 'last_sync_time'")
    last_sync = cur.fetchone()
    last_sync_str = last_sync["value"] if last_sync else None

    conn.close()
    return {
        "movies_count": movies_count,
        "shows_count": shows_count,
        "events_count": events_count,
        "seen_count": seen_count,
        "last_sync": last_sync_str
    }


def get_top_watched(user_key: str, media_type: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Return the user's most-watched items of a given media_type (movie/show),
    ranked by view_count desc, then most-recently-viewed as a tiebreaker.

    For shows, view_count reflects total episodes watched (viewedLeafCount rollup),
    consistent with the analyzer's episode-to-show attribution.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT m.item_id, m.title, m.year, m.tmdb_id, m.imdb_id, m.media_type,
               um.view_count, um.last_viewed_at
        FROM user_media um
        JOIN media_items m ON m.item_id = um.item_id
        WHERE um.user_key = ? AND m.media_type = ? AND um.view_count > 0
        ORDER BY um.view_count DESC, um.last_viewed_at DESC
        LIMIT ?
    """, (str(user_key), media_type, limit))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows
