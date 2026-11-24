"""User engagement signals: dismissals, thumbs up/down votes, and watchlist.

Votes and dismissals interact with `seen_identifiers` (so discover queries
immediately drop dismissed/downvoted items) and invalidate the per-user
recommendations cache pool.
"""

import json
from typing import Optional, Dict, Any, List, Set

from plex_recommender.db import get_connection
from plex_recommender.db._util import normalize_title
from plex_recommender.db.recommendations import clear_user_recommendations_cache


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

    # Dismissing an item (watched or not interested) removes it from the watchlist
    cur.execute("DELETE FROM user_watchlist WHERE user_key = ? AND tmdb_id = ?", (uk, tid))

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
    cur.execute("DELETE FROM user_votes WHERE user_key = ? AND tmdb_id = ? AND vote = -1", (uk, tid))
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


def record_vote(
    user_key: str,
    tmdb_id: str,
    media_type: str = "movie",
    title: str = "",
    year: Optional[int] = None,
    vote: int = 1
):
    """Record an upvote (+1) or downvote (-1) for a user recommendation."""
    conn = get_connection()
    cur = conn.cursor()
    uk = str(user_key)
    tid = str(tmdb_id).strip()

    cur.execute("""
    INSERT INTO user_votes (user_key, tmdb_id, media_type, title, year, vote, created_at)
    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(user_key, tmdb_id) DO UPDATE SET
        vote = excluded.vote,
        title = COALESCE(excluded.title, user_votes.title),
        year = COALESCE(excluded.year, user_votes.year),
        created_at = CURRENT_TIMESTAMP
    """, (uk, tid, media_type, title, year, int(vote)))
    conn.commit()
    conn.close()

    if int(vote) == -1:
        # Also mark as dismissed so it's placed in seen_identifiers and user_dismissals
        dismiss_item(
            user_key=uk,
            tmdb_id=tid,
            media_type=media_type,
            title=title,
            year=year,
            reason="not_interested"
        )
    else:
        # If it was previously dismissed (e.g. downvoted), remove dismissal
        undismiss_item(user_key=uk, tmdb_id=tid)


def remove_vote(user_key: str, tmdb_id: str):
    """Remove a vote (thumbs up or down). If downvoted, also restores from dismissals."""
    conn = get_connection()
    cur = conn.cursor()
    uk = str(user_key)
    tid = str(tmdb_id).strip()

    cur.execute("SELECT vote FROM user_votes WHERE user_key = ? AND tmdb_id = ?", (uk, tid))
    row = cur.fetchone()
    prev_vote = row["vote"] if row else None

    cur.execute("DELETE FROM user_votes WHERE user_key = ? AND tmdb_id = ?", (uk, tid))
    conn.commit()
    conn.close()

    if prev_vote == -1:
        undismiss_item(user_key=uk, tmdb_id=tid)
    else:
        clear_user_recommendations_cache(uk)


def get_user_votes(user_key: str) -> Dict[str, int]:
    """Return a mapping of tmdb_id -> vote (-1 or 1) for a user."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT tmdb_id, vote FROM user_votes WHERE user_key = ?", (str(user_key),))
    votes = {str(r["tmdb_id"]): int(r["vote"]) for r in cur.fetchall()}
    conn.close()
    return votes


def get_user_vote_items(user_key: str, vote: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return all voted items for a user, optionally filtered by vote polarity."""
    conn = get_connection()
    cur = conn.cursor()
    if vote is not None:
        cur.execute("""
        SELECT user_key, tmdb_id, media_type, title, year, vote, created_at
        FROM user_votes
        WHERE user_key = ? AND vote = ?
        ORDER BY created_at DESC, rowid DESC
        """, (str(user_key), int(vote)))
    else:
        cur.execute("""
        SELECT user_key, tmdb_id, media_type, title, year, vote, created_at
        FROM user_votes
        WHERE user_key = ?
        ORDER BY created_at DESC, rowid DESC
        """, (str(user_key),))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_user_action_counts(user_key: str) -> Dict[str, int]:
    """Return count of dismissals, upvotes, downvotes, and seen identifiers for a user."""
    conn = get_connection()
    cur = conn.cursor()
    uk = str(user_key)

    cur.execute("SELECT COUNT(*) AS c FROM user_dismissals WHERE user_key = ?", (uk,))
    dismissals = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM user_votes WHERE user_key = ? AND vote = 1", (uk,))
    upvotes = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM user_votes WHERE user_key = ? AND vote = -1", (uk,))
    downvotes = cur.fetchone()["c"]

    cur.execute("SELECT COUNT(*) AS c FROM seen_identifiers WHERE user_key = ?", (uk,))
    seen = cur.fetchone()["c"]

    conn.close()
    return {
        "dismissals_count": dismissals,
        "upvotes_count": upvotes,
        "downvotes_count": downvotes,
        "seen_count": seen,
    }


def add_to_watchlist(
    user_key: str,
    tmdb_id: str,
    media_type: str = "movie",
    title: str = "",
    year: Optional[int] = None,
    poster_url: Optional[str] = None,
    rating: Optional[float] = None,
    genres: Optional[List[str]] = None,
    summary: str = "",
) -> None:
    """Save a recommendation to the user's personal watchlist."""
    conn = get_connection()
    cur = conn.cursor()
    uk = str(user_key)
    tid = str(tmdb_id).strip()
    genres_json = json.dumps(list(genres or []))

    cur.execute("""
    INSERT INTO user_watchlist (
        user_key, tmdb_id, media_type, title, year, poster_url, rating, genres, summary, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(user_key, tmdb_id) DO UPDATE SET
        media_type = excluded.media_type,
        title = COALESCE(NULLIF(excluded.title, ''), user_watchlist.title),
        year = COALESCE(excluded.year, user_watchlist.year),
        poster_url = COALESCE(excluded.poster_url, user_watchlist.poster_url),
        rating = COALESCE(excluded.rating, user_watchlist.rating),
        genres = CASE WHEN excluded.genres != '[]' THEN excluded.genres ELSE user_watchlist.genres END,
        summary = COALESCE(NULLIF(excluded.summary, ''), user_watchlist.summary),
        created_at = CURRENT_TIMESTAMP
    """, (uk, tid, media_type, title, year, poster_url, rating, genres_json, summary))
    conn.commit()
    conn.close()


def remove_from_watchlist(user_key: str, tmdb_id: str) -> bool:
    """Remove a title from the user's personal watchlist. Returns True if removed."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM user_watchlist WHERE user_key = ? AND tmdb_id = ?",
        (str(user_key), str(tmdb_id).strip()),
    )
    removed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return removed


def is_in_watchlist(user_key: str, tmdb_id: str) -> bool:
    """Return True if tmdb_id is currently saved in the user's watchlist."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM user_watchlist WHERE user_key = ? AND tmdb_id = ? LIMIT 1",
        (str(user_key), str(tmdb_id).strip()),
    )
    found = cur.fetchone() is not None
    conn.close()
    return found


def get_user_watchlist(user_key: str) -> List[Dict[str, Any]]:
    """Return all watchlist items for a user, ordered most recently saved first."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_key, tmdb_id, media_type, title, year, poster_url, rating, genres, summary, created_at
    FROM user_watchlist
    WHERE user_key = ?
    ORDER BY created_at DESC, rowid DESC
    """, (str(user_key),))
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        raw_g = d.get("genres")
        if isinstance(raw_g, str):
            try:
                d["genres"] = json.loads(raw_g)
            except Exception:
                d["genres"] = []
        elif not isinstance(raw_g, list):
            d["genres"] = []
        rows.append(d)
    conn.close()
    return rows


def get_user_watchlist_ids(user_key: str) -> Set[str]:
    """Return the set of tmdb_id strings saved in the user's watchlist."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT tmdb_id FROM user_watchlist WHERE user_key = ?", (str(user_key),))
    ids = {str(r["tmdb_id"]) for r in cur.fetchall()}
    conn.close()
    return ids


def get_user_watchlist_count(user_key: str) -> int:
    """Return the number of items in a user's watchlist."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM user_watchlist WHERE user_key = ?", (str(user_key),))
    cnt = int(cur.fetchone()["c"])
    conn.close()
    return cnt
