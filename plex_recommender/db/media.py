"""media_items: shared metadata cache upserts and availability/keyword lookups."""

import json
from typing import Optional, Dict, Any, List

from plex_recommender.db import get_connection

_UPSERT_SQL = """
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


def _item_params(item: Dict[str, Any]) -> tuple:
    return (
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


def upsert_media_item(item: Dict[str, Any]):
    """Insert or update a media item and update seen identifiers."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(_UPSERT_SQL, _item_params(item))
    conn.commit()
    conn.close()


def upsert_media_items_batch(items: List[Dict[str, Any]], batch_size: int = 250):
    """Batch insert or update media items using executemany for high performance."""
    if not items:
        return
    conn = get_connection()
    cur = conn.cursor()

    for i in range(0, len(items), batch_size):
        chunk = items[i:i + batch_size]
        params = [_item_params(item) for item in chunk]
        cur.executemany(_UPSERT_SQL, params)
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
