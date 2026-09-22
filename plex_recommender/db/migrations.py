"""Schema migrations and one-time/startup data reconciliation.

All functions here take an already-open `sqlite3.Connection` (as passed by
`init_db()`), since they run inline during startup before the connection used
for DDL is closed.
"""

import sqlite3
import logging
from typing import Dict, List, Tuple

from plex_recommender.db._util import parse_iso_to_epoch

logger = logging.getLogger(__name__)


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

    user_cols = [r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()]
    if "onboarded_at" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN onboarded_at TIMESTAMP")
        conn.commit()
    if "last_seen_at" not in user_cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_seen_at TIMESTAMP")
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
        pr_epoch = parse_iso_to_epoch(pr["viewed_at"])
        if pr_epoch is None:
            continue

        for tr in tautulli_by_key[key]:
            tr_id = tr["id"]
            if tr_id in to_delete_ids:
                continue
            tr_epoch = parse_iso_to_epoch(tr["viewed_at"])
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


def _migrate_hidden_cards_to_votes(conn: sqlite3.Connection):
    """Migrate legacy 'not_interested' dismissals to user_votes as downvotes (vote = -1)."""
    cur = conn.cursor()
    try:
        cur.execute("""
        INSERT OR IGNORE INTO user_votes (user_key, tmdb_id, media_type, title, year, vote, created_at)
        SELECT user_key, tmdb_id, media_type, title, year, -1, created_at
        FROM user_dismissals
        WHERE reason = 'not_interested'
        """)
        migrated = cur.rowcount
        conn.commit()
        if migrated > 0:
            logger.info("Migrated %s legacy 'not_interested' hidden cards to user_votes (downvotes, vote = -1).", migrated)
    except sqlite3.OperationalError as e:
        logger.warning("Could not migrate legacy hidden cards to user_votes: %s", e)


def _migrate_normalize_media_item_ids(conn: sqlite3.Connection):
    """One-time & startup reconciliation: normalize media_items.item_id to the bare
    Plex ratingKey format.

    Historically, the library metadata scan (sync_library_metadata) prefixed item_id
    with 'movie_'/'show_'/'episode_', while watch-history sync (sync_plex_user_history,
    sync_tautulli_user_history) and watch_events/user_media always used the bare
    ratingKey. This created two disconnected media_items rows per physical title.
    Since user_media and watch_events NEVER use the prefixed form, we standardize on
    the bare form here: merge each prefixed row into its bare-keyed twin (preferring
    richer/non-empty data, like upsert_media_item's own conflict logic), or rename it
    in place if no bare twin exists yet (e.g. unwatched library items).

    Operates on the given connection directly (rather than calling upsert_media_item,
    which opens its own connection and would deadlock against this one mid-migration).
    """
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT item_id, media_type, title, year, release_date, genres, directors,
                   writers, actors, summary, user_rating, audience_rating, critic_rating,
                   imdb_id, tmdb_id, tvdb_id, view_count, last_viewed_at, raw_guids, keywords
            FROM media_items
        """)
        all_rows = {str(r["item_id"]): dict(r) for r in cur.fetchall()}
    except sqlite3.OperationalError:
        return

    prefixed_ids = [
        iid for iid in all_rows
        if iid.startswith(("movie_", "show_", "episode_"))
    ]
    if not prefixed_ids:
        return

    merged = 0
    for old_id in prefixed_ids:
        d = all_rows[old_id]
        for prefix in ("movie_", "show_", "episode_"):
            if old_id.startswith(prefix):
                bare_id = old_id[len(prefix):]
                break
        else:
            continue

        existing = all_rows.get(bare_id)
        if existing is None:
            # No bare twin: simply rename this row in place.
            cur.execute("UPDATE media_items SET item_id = ? WHERE item_id = ?", (bare_id, old_id))
        else:
            def _prefer_non_empty(new_val, old_val):
                if new_val in (None, "", "[]"):
                    return old_val
                return new_val

            merged_row = {
                "media_type": d.get("media_type") or existing.get("media_type"),
                "title": d.get("title") or existing.get("title"),
                "year": d.get("year") or existing.get("year"),
                "release_date": d.get("release_date") or existing.get("release_date"),
                "genres": _prefer_non_empty(d.get("genres"), existing.get("genres")),
                "directors": _prefer_non_empty(d.get("directors"), existing.get("directors")),
                "writers": _prefer_non_empty(d.get("writers"), existing.get("writers")),
                "actors": _prefer_non_empty(d.get("actors"), existing.get("actors")),
                "summary": d.get("summary") or existing.get("summary"),
                "user_rating": d.get("user_rating") if d.get("user_rating") is not None else existing.get("user_rating"),
                "audience_rating": d.get("audience_rating") if d.get("audience_rating") is not None else existing.get("audience_rating"),
                "critic_rating": d.get("critic_rating") if d.get("critic_rating") is not None else existing.get("critic_rating"),
                "imdb_id": d.get("imdb_id") or existing.get("imdb_id"),
                "tmdb_id": d.get("tmdb_id") or existing.get("tmdb_id"),
                "tvdb_id": d.get("tvdb_id") or existing.get("tvdb_id"),
                "view_count": max(int(d.get("view_count") or 0), int(existing.get("view_count") or 0)),
                "last_viewed_at": d.get("last_viewed_at") or existing.get("last_viewed_at"),
                "raw_guids": _prefer_non_empty(d.get("raw_guids"), existing.get("raw_guids")),
                "keywords": _prefer_non_empty(d.get("keywords"), existing.get("keywords")),
            }
            cur.execute("""
                UPDATE media_items SET
                    media_type = ?, title = ?, year = ?, release_date = ?,
                    genres = ?, directors = ?, writers = ?, actors = ?, summary = ?,
                    user_rating = ?, audience_rating = ?, critic_rating = ?,
                    imdb_id = ?, tmdb_id = ?, tvdb_id = ?, view_count = ?,
                    last_viewed_at = ?, raw_guids = ?, keywords = ?
                WHERE item_id = ?
            """, (
                merged_row["media_type"], merged_row["title"], merged_row["year"], merged_row["release_date"],
                merged_row["genres"], merged_row["directors"], merged_row["writers"], merged_row["actors"],
                merged_row["summary"], merged_row["user_rating"], merged_row["audience_rating"],
                merged_row["critic_rating"], merged_row["imdb_id"], merged_row["tmdb_id"], merged_row["tvdb_id"],
                merged_row["view_count"], merged_row["last_viewed_at"], merged_row["raw_guids"],
                merged_row["keywords"], bare_id,
            ))
            cur.execute("DELETE FROM media_items WHERE item_id = ?", (old_id,))

        merged += 1

    conn.commit()
    if merged:
        logger.info(
            "Normalized %s prefixed media_items id(s) to the bare ratingKey format.", merged
        )


def run_all(conn: sqlite3.Connection):
    """Run all migrations/reconciliations, in order, against an open connection."""
    _migrate_add_columns(conn)
    _migrate_discard_global(conn)
    _reconcile_watch_events_migration(conn)
    _migrate_hidden_cards_to_votes(conn)
    _migrate_normalize_media_item_ids(conn)
