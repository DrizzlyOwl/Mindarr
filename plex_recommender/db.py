import sqlite3
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Set, Tuple
from plex_recommender.config import settings

logger = logging.getLogger(__name__)

def get_connection() -> sqlite3.Connection:
    """Returns SQLite connection with dict-like row factory."""
    db_path = settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize database tables and indexes."""
    conn = get_connection()
    cur = conn.cursor()
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS watch_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT UNIQUE,
        user_key TEXT,
        item_id TEXT,
        title TEXT,
        media_type TEXT,
        viewed_at TIMESTAMP,
        account_id TEXT,
        duration_watched INTEGER,
        source TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_key TEXT PRIMARY KEY,
        plex_uuid TEXT,
        username TEXT,
        email TEXT,
        title TEXT,
        thumb TEXT,
        plex_token TEXT,
        is_admin INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_login TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS media_items (
        item_id TEXT PRIMARY KEY,
        media_type TEXT,
        title TEXT,
        year INTEGER,
        release_date TEXT,
        genres TEXT,          -- JSON list of strings
        directors TEXT,       -- JSON list of strings
        writers TEXT,         -- JSON list of strings
        actors TEXT,          -- JSON list of strings
        summary TEXT,
        user_rating REAL,
        audience_rating REAL,
        critic_rating REAL,
        imdb_id TEXT,
        tmdb_id TEXT,
        tvdb_id TEXT,
        view_count INTEGER DEFAULT 0,
        last_viewed_at TIMESTAMP,
        raw_guids TEXT        -- JSON list of guid strings
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_media (
        user_key TEXT,
        item_id TEXT,
        view_count INTEGER DEFAULT 1,
        last_viewed_at TIMESTAMP,
        PRIMARY KEY (user_key, item_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen_identifiers (
        user_key TEXT,
        id_type TEXT,          -- 'imdb', 'tmdb', 'tvdb', 'title_year'
        id_value TEXT,
        title TEXT,
        year INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_key, id_type, id_value)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS recommendations_cache (
        cache_key TEXT PRIMARY KEY,
        sync_version TEXT,
        cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        response_json TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS job_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_type TEXT,           -- e.g. 'manual_sync', 'scheduled_sync'
        trigger TEXT,            -- 'manual' or 'scheduled'
        user_key TEXT,           -- who/what the job ran for (nullable)
        status TEXT,             -- 'running', 'success', 'failed'
        detail TEXT,             -- summary or error message
        started_at TIMESTAMP,
        finished_at TIMESTAMP,
        duration_ms INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_dismissals (
        user_key TEXT,
        tmdb_id TEXT,
        media_type TEXT,
        title TEXT,
        year INTEGER,
        reason TEXT,           -- 'already_watched' or 'not_interested'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_key, tmdb_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS system_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        epoch REAL,
        level TEXT,
        logger_name TEXT,
        message TEXT
    )
    """)

    # Indexes for fast lookup
    cur.execute("CREATE INDEX IF NOT EXISTS idx_watch_viewed_at ON watch_events(viewed_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_watch_user ON watch_events(user_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_media_type ON media_items(media_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_seen_lookup ON seen_identifiers(user_key, id_type, id_value)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_media ON user_media(user_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rec_cache_sync ON recommendations_cache(sync_version)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_job_started ON job_history(started_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_dismissals ON user_dismissals(user_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_epoch ON system_logs(epoch DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON system_logs(level)")

    conn.commit()

    _migrate_add_columns(conn)
    _migrate_discard_global(conn)
    _reconcile_watch_events_migration(conn)

    conn.close()


def _migrate_add_columns(conn: sqlite3.Connection):
    """Add columns introduced after initial release to existing databases."""
    cur = conn.cursor()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(watch_events)").fetchall()]
    if "source" not in cols:
        cur.execute("ALTER TABLE watch_events ADD COLUMN source TEXT")
        conn.commit()

    media_cols = [r[1] for r in cur.execute("PRAGMA table_info(media_items)").fetchall()]
    if "keywords" not in media_cols:
        cur.execute("ALTER TABLE media_items ADD COLUMN keywords TEXT")
        conn.commit()

    user_media_cols = [r[1] for r in cur.execute("PRAGMA table_info(user_media)").fetchall()]
    if "user_rating" not in user_media_cols:
        cur.execute("ALTER TABLE user_media ADD COLUMN user_rating REAL")
        conn.commit()


def _migrate_discard_global(conn: sqlite3.Connection):
    """One-time migration: discard pre-multi-user global watch data.

    Older schemas stored server-wide seen/watch state without a user_key.
    Detect legacy rows (user_key IS NULL) and clear them so data becomes
    strictly per-user going forward.
    """
    cur = conn.cursor()
    try:
        legacy = cur.execute(
            "SELECT COUNT(*) AS c FROM seen_identifiers WHERE user_key IS NULL"
        ).fetchone()["c"]
        legacy += cur.execute(
            "SELECT COUNT(*) AS c FROM watch_events WHERE user_key IS NULL"
        ).fetchone()["c"]
    except sqlite3.OperationalError:
        return

    if legacy:
        logger.info("Discarding %s legacy global watch rows (multi-user migration).", legacy)
        cur.execute("DELETE FROM seen_identifiers WHERE user_key IS NULL")
        cur.execute("DELETE FROM watch_events WHERE user_key IS NULL")
        cur.execute("DELETE FROM recommendations_cache")
        conn.commit()


def _parse_iso_to_epoch(ts_str: Optional[str]) -> Optional[float]:
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


def _reconcile_watch_events_migration(conn: sqlite3.Connection):
    """One-time & startup reconciliation: deduplicate watch events across Plex & Tautulli.

    Plex is Primary: when a Plex event and Tautulli event describe the same watch session
    (same user_key and item_id within 4 hours), the Plex event is enriched with Tautulli's
    exact stream duration and composite title, and the duplicate Tautulli event is removed.
    """
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM watch_events WHERE source = 'plex'")
        plex_rows = cur.fetchall()
        cur.execute("SELECT * FROM watch_events WHERE source = 'tautulli'")
        tautulli_rows = cur.fetchall()
    except sqlite3.OperationalError:
        return

    if not plex_rows or not tautulli_rows:
        return

    tautulli_by_key: Dict[Tuple[str, str], List[sqlite3.Row]] = {}
    for tr in tautulli_rows:
        key = (str(tr["user_key"]), str(tr["item_id"]))
        tautulli_by_key.setdefault(key, []).append(tr)

    to_delete_ids: List[int] = []
    to_update_plex: List[Tuple[int, str, int]] = []

    for pr in plex_rows:
        key = (str(pr["user_key"]), str(pr["item_id"]))
        if key not in tautulli_by_key:
            continue
        pr_epoch = _parse_iso_to_epoch(pr["viewed_at"])
        if pr_epoch is None:
            continue

        for tr in tautulli_by_key[key]:
            tr_id = tr["id"]
            if tr_id in to_delete_ids:
                continue
            tr_epoch = _parse_iso_to_epoch(tr["viewed_at"])
            if tr_epoch is None:
                continue

            if abs(pr_epoch - tr_epoch) <= 14400:  # 4 hours
                pr_dur = int(pr["duration_watched"] or 0)
                tr_dur = int(tr["duration_watched"] or 0)
                pr_title = str(pr["title"] or "")
                tr_title = str(tr["title"] or "")

                best_dur = tr_dur if (pr_dur <= 0 and tr_dur > 0) else pr_dur
                best_title = tr_title if (" - " in tr_title and " - " not in pr_title) else pr_title

                if best_dur != pr_dur or best_title != pr_title:
                    to_update_plex.append((best_dur, best_title, pr["id"]))

                to_delete_ids.append(tr_id)
                break

    for dur, title, pid in to_update_plex:
        cur.execute("UPDATE watch_events SET duration_watched = ?, title = ? WHERE id = ?", (dur, title, pid))

    if to_delete_ids:
        for i in range(0, len(to_delete_ids), 500):
            batch = to_delete_ids[i:i + 500]
            placeholders = ",".join("?" * len(batch))
            cur.execute(f"DELETE FROM watch_events WHERE id IN ({placeholders})", batch)
        conn.commit()
        logger.info("Reconciled and removed %s duplicate Tautulli watch events.", len(to_delete_ids))

def normalize_title(title: str) -> str:
    """Normalize title for fuzzy matching."""
    if not title:
        return ""
    import re
    cleaned = re.sub(r"[^a-zA-Z0-9 ]+", "", title.lower()).strip()
    return " ".join(cleaned.split())

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

def upsert_media_item(item: Dict[str, Any]):
    """Insert or update a media item and update seen identifiers."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO media_items (
        item_id, media_type, title, year, release_date, genres, directors,
        writers, actors, summary, user_rating, audience_rating, critic_rating,
        imdb_id, tmdb_id, tvdb_id, view_count, last_viewed_at, raw_guids, keywords
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(item_id) DO UPDATE SET
        media_type = excluded.media_type,
        title = excluded.title,
        year = excluded.year,
        release_date = excluded.release_date,
        genres = CASE WHEN excluded.genres IN ('[]', '') OR excluded.genres IS NULL
                      THEN media_items.genres ELSE excluded.genres END,
        directors = CASE WHEN excluded.directors IN ('[]', '') OR excluded.directors IS NULL
                         THEN media_items.directors ELSE excluded.directors END,
        writers = excluded.writers,
        actors = CASE WHEN excluded.actors IN ('[]', '') OR excluded.actors IS NULL
                      THEN media_items.actors ELSE excluded.actors END,
        summary = excluded.summary,
        user_rating = excluded.user_rating,
        audience_rating = excluded.audience_rating,
        critic_rating = excluded.critic_rating,
        imdb_id = excluded.imdb_id,
        tmdb_id = excluded.tmdb_id,
        tvdb_id = excluded.tvdb_id,
        view_count = MAX(media_items.view_count, excluded.view_count),
        last_viewed_at = excluded.last_viewed_at,
        raw_guids = excluded.raw_guids,
        keywords = CASE WHEN excluded.keywords IN ('[]', '') OR excluded.keywords IS NULL
                        THEN media_items.keywords ELSE excluded.keywords END
    """, (
        str(item.get("item_id")),
        item.get("media_type", "movie"),
        item.get("title", ""),
        item.get("year"),
        item.get("release_date"),
        json.dumps(item.get("genres", [])),
        json.dumps(item.get("directors", [])),
        json.dumps(item.get("writers", [])),
        json.dumps(item.get("actors", [])),
        item.get("summary", ""),
        item.get("user_rating"),
        item.get("audience_rating"),
        item.get("critic_rating"),
        item.get("imdb_id"),
        item.get("tmdb_id"),
        item.get("tvdb_id"),
        item.get("view_count", 0),
        item.get("last_viewed_at"),
        json.dumps(item.get("raw_guids", [])),
        json.dumps(item.get("keywords", []))
    ))

    conn.commit()
    conn.close()


def upsert_media_items_batch(items: List[Dict[str, Any]], batch_size: int = 250):
    """Batch insert or update media items using executemany for high performance."""
    if not items:
        return
    conn = get_connection()
    cur = conn.cursor()

    sql = """
    INSERT INTO media_items (
        item_id, media_type, title, year, release_date, genres, directors,
        writers, actors, summary, user_rating, audience_rating, critic_rating,
        imdb_id, tmdb_id, tvdb_id, view_count, last_viewed_at, raw_guids, keywords
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(item_id) DO UPDATE SET
        media_type = excluded.media_type,
        title = excluded.title,
        year = excluded.year,
        release_date = excluded.release_date,
        genres = CASE WHEN excluded.genres IN ('[]', '') OR excluded.genres IS NULL
                      THEN media_items.genres ELSE excluded.genres END,
        directors = CASE WHEN excluded.directors IN ('[]', '') OR excluded.directors IS NULL
                         THEN media_items.directors ELSE excluded.directors END,
        writers = excluded.writers,
        actors = CASE WHEN excluded.actors IN ('[]', '') OR excluded.actors IS NULL
                      THEN media_items.actors ELSE excluded.actors END,
        summary = excluded.summary,
        user_rating = excluded.user_rating,
        audience_rating = excluded.audience_rating,
        critic_rating = excluded.critic_rating,
        imdb_id = excluded.imdb_id,
        tmdb_id = excluded.tmdb_id,
        tvdb_id = excluded.tvdb_id,
        view_count = MAX(media_items.view_count, excluded.view_count),
        last_viewed_at = excluded.last_viewed_at,
        raw_guids = excluded.raw_guids,
        keywords = CASE WHEN excluded.keywords IN ('[]', '') OR excluded.keywords IS NULL
                        THEN media_items.keywords ELSE excluded.keywords END
    """

    for i in range(0, len(items), batch_size):
        chunk = items[i:i + batch_size]
        params = [
            (
                str(item.get("item_id")),
                item.get("media_type", "movie"),
                item.get("title", ""),
                item.get("year"),
                item.get("release_date"),
                json.dumps(item.get("genres", [])),
                json.dumps(item.get("directors", [])),
                json.dumps(item.get("writers", [])),
                json.dumps(item.get("actors", [])),
                item.get("summary", ""),
                item.get("user_rating"),
                item.get("audience_rating"),
                item.get("critic_rating"),
                item.get("imdb_id"),
                item.get("tmdb_id"),
                item.get("tvdb_id"),
                item.get("view_count", 0),
                item.get("last_viewed_at"),
                json.dumps(item.get("raw_guids", [])),
                json.dumps(item.get("keywords", [])),
            )
            for item in chunk
        ]
        cur.executemany(sql, params)
        conn.commit()

    conn.close()


def get_existing_keywords_map() -> Dict[str, List[Dict[str, Any]]]:
    """Return a mapping of tmdb_id -> keywords list from existing media_items to prevent redundant TMDb API calls."""
    conn = get_connection()
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT tmdb_id, keywords FROM media_items WHERE tmdb_id IS NOT NULL AND keywords IS NOT NULL AND keywords != '[]' AND keywords != ''"
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        tid = str(r["tmdb_id"])
        kw = r["keywords"]
        if isinstance(kw, str):
            try:
                result[tid] = json.loads(kw)
            except Exception:
                pass
        elif isinstance(kw, list):
            result[tid] = kw
    return result


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

SESSION_WINDOW_SECONDS = 14400  # 4-hour window for matching playback sessions

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
    incoming_epoch = _parse_iso_to_epoch(viewed_at)
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
            existing_epoch = _parse_iso_to_epoch(row["viewed_at"])
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


def get_library_availability_index() -> Dict[str, Dict[str, Dict[str, str]]]:
    """Return maps of external IDs -> library item info for availability checks.

    Only movies and shows are considered (not individual episodes). Each value
    is {"item_id": ..., "media_type": ..., "rating_key": ...}, where rating_key
    is the Plex ratingKey (item_id with its 'movie_'/'show_' prefix stripped),
    usable to build a Plex web deep-link.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT item_id, media_type, imdb_id, tmdb_id, tvdb_id
        FROM media_items
        WHERE media_type IN ('movie', 'show')
    """)
    rows = cur.fetchall()
    conn.close()

    index: Dict[str, Dict[str, Dict[str, str]]] = {"imdb": {}, "tmdb": {}, "tvdb": {}}
    for r in rows:
        item_id = r["item_id"] or ""
        rating_key = item_id.split("_", 1)[-1] if "_" in item_id else item_id
        info = {
            "item_id": item_id,
            "media_type": r["media_type"],
            "rating_key": rating_key,
        }
        if r["imdb_id"]:
            index["imdb"][str(r["imdb_id"]).strip().lower()] = info
        if r["tmdb_id"]:
            index["tmdb"][str(r["tmdb_id"]).strip().lower()] = info
        if r["tvdb_id"]:
            index["tvdb"][str(r["tvdb_id"]).strip().lower()] = info
    return index


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
    for table in ("user_media", "watch_events", "seen_identifiers"):
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


def start_job(job_type: str, trigger: str, user_key: Optional[str] = None) -> int:
    """Record the start of a background job. Returns the job id."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO job_history (job_type, trigger, user_key, status, started_at)
        VALUES (?, ?, ?, 'running', ?)
        """,
        (job_type, trigger, user_key, datetime.now().isoformat()),
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()
    return job_id


def finish_job(job_id: int, status: str, detail: Optional[str] = None):
    """Mark a job finished, computing its duration from started_at."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT started_at FROM job_history WHERE id = ?", (job_id,))
    row = cur.fetchone()
    finished = datetime.now()
    duration_ms = None
    if row and row["started_at"]:
        try:
            started = datetime.fromisoformat(row["started_at"])
            duration_ms = int((finished - started).total_seconds() * 1000)
        except (ValueError, TypeError):
            duration_ms = None
    cur.execute(
        """
        UPDATE job_history
        SET status = ?, detail = ?, finished_at = ?, duration_ms = ?
        WHERE id = ?
        """,
        (status, (detail or "")[:2000], finished.isoformat(), duration_ms, job_id),
    )
    conn.commit()
    conn.close()


def get_job_history(limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent job history, most recent first."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM job_history ORDER BY started_at DESC, id DESC LIMIT ?",
        (int(limit),),
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_job_history():
    """Delete all job history records."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM job_history")
    conn.commit()
    conn.close()


def clear_user_recommendations_cache(user_key: str):
    """Clear all cached recommendations pools for a specific user."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM recommendations_cache WHERE cache_key LIKE ?", (f"{user_key}:%",))
    conn.commit()
    conn.close()


def dismiss_item(
    user_key: str,
    tmdb_id: str,
    media_type: str = "movie",
    title: str = "",
    year: Optional[int] = None,
    reason: str = "not_interested"
):
    """Dismiss a recommendation (already watched outside Plex, or not interested)."""
    conn = get_connection()
    cur = conn.cursor()
    uk = str(user_key)
    tid = str(tmdb_id).strip()

    cur.execute("""
    INSERT INTO user_dismissals (user_key, tmdb_id, media_type, title, year, reason, created_at)
    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(user_key, tmdb_id) DO UPDATE SET
        reason = excluded.reason,
        title = COALESCE(excluded.title, user_dismissals.title),
        year = COALESCE(excluded.year, user_dismissals.year),
        created_at = CURRENT_TIMESTAMP
    """, (uk, tid, media_type, title, year, reason))

    # Also add to seen_identifiers so discover queries & in-memory filters drop it immediately
    cur.execute(
        "INSERT OR IGNORE INTO seen_identifiers (user_key, id_type, id_value, title, year) VALUES (?, 'tmdb', ?, ?, ?)",
        (uk, tid.lower(), title, year)
    )
    if title:
        norm_title = normalize_title(title)
        if norm_title:
            val = f"{norm_title}::{year or ''}"
            cur.execute(
                "INSERT OR IGNORE INTO seen_identifiers (user_key, id_type, id_value, title, year) VALUES (?, 'title_year', ?, ?, ?)",
                (uk, val, title, year)
            )

    conn.commit()
    conn.close()

    clear_user_recommendations_cache(uk)


def undismiss_item(user_key: str, tmdb_id: str):
    """Remove a dismissal, allowing the item to be recommended again if not otherwise seen."""
    conn = get_connection()
    cur = conn.cursor()
    uk = str(user_key)
    tid = str(tmdb_id).strip()

    # Find dismissal details to remove corresponding seen_identifier
    cur.execute("SELECT title, year FROM user_dismissals WHERE user_key = ? AND tmdb_id = ?", (uk, tid))
    row = cur.fetchone()

    cur.execute("DELETE FROM user_dismissals WHERE user_key = ? AND tmdb_id = ?", (uk, tid))
    cur.execute("DELETE FROM seen_identifiers WHERE user_key = ? AND id_type = 'tmdb' AND id_value = ?", (uk, tid.lower()))

    if row and row["title"]:
        norm_title = normalize_title(row["title"])
        val = f"{norm_title}::{row['year'] or ''}"
        cur.execute("DELETE FROM seen_identifiers WHERE user_key = ? AND id_type = 'title_year' AND id_value = ?", (uk, val))

    conn.commit()
    conn.close()

    clear_user_recommendations_cache(uk)


def get_user_dismissals(user_key: str) -> List[Dict[str, Any]]:
    """Return all items dismissed by a user, sorted by most recently dismissed."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_key, tmdb_id, media_type, title, year, reason, created_at
    FROM user_dismissals
    WHERE user_key = ?
    ORDER BY created_at DESC, rowid DESC
    """, (str(user_key),))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def insert_system_log(level: str, logger_name: str, message: str, epoch: Optional[float] = None):
    """Insert a log entry into system_logs table."""
    if epoch is None:
        epoch = datetime.now(timezone.utc).timestamp()
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO system_logs (timestamp, epoch, level, logger_name, message)
        VALUES (CURRENT_TIMESTAMP, ?, ?, ?, ?)
        """, (epoch, level, logger_name, message))
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_system_logs(
    limit: int = 250,
    level: Optional[str] = None,
    search: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Retrieve system logs sorted by most recent timestamp first (epoch DESC)."""
    conn = get_connection()
    cur = conn.cursor()

    query = "SELECT id, timestamp, epoch, level, logger_name, message FROM system_logs WHERE 1=1"
    params: List[Any] = []

    if level and level.upper() != "ALL":
        query += " AND level = ?"
        params.append(level.upper())

    if search and search.strip():
        query += " AND (message LIKE ? OR logger_name LIKE ?)"
        term = f"%{search.strip()}%"
        params.extend([term, term])

    query += " ORDER BY epoch DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))

    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def truncate_system_logs(days: int = 7) -> int:
    """Delete logs older than retention days (default 7 days). Returns number of pruned rows."""
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM system_logs WHERE epoch < ?", (cutoff,))
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count


def clear_all_system_logs():
    """Wipe all system logs."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM system_logs")
    conn.commit()
    conn.close()


class SQLiteLogHandler(logging.Handler):
    """Thread-safe logging handler that persists log records into the system_logs SQLite table."""

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            epoch = record.created
            level = record.levelname
            name = record.name
            if name.startswith("sqlite") or name == "plex_recommender.db":
                return
            insert_system_log(level=level, logger_name=name, message=msg, epoch=epoch)
        except Exception:
            self.handleError(record)

