import logging
import json
import time
from typing import Optional, Dict, Any, List, Set, Tuple
from datetime import datetime, timezone

from plex_recommender.config import settings
from plex_recommender.discovery.tautulli import tautulli
from plex_recommender.db import (
    get_connection,
    get_user_seen_index,
    normalize_title,
    get_all_users,
    get_stats,
)

logger = logging.getLogger(__name__)

# Simple in-memory cache with 10-minute TTL to keep page renders ultra-fast
_CACHE: Dict[str, Any] = {}
_CACHE_TIMESTAMP: float = 0
CACHE_TTL_SECONDS = 600


class CommunityService:
    """Provides server-wide viewing trends, peer comparisons, and community discovery."""

    def get_community_data(self, current_user: Dict[str, Any], force_refresh: bool = False) -> Dict[str, Any]:
        global _CACHE, _CACHE_TIMESTAMP
        user_key = str(current_user.get("user_key", ""))
        cache_key = f"community_{user_key}"

        now = time.time()
        if not force_refresh and (now - _CACHE_TIMESTAMP < CACHE_TTL_SECONDS) and cache_key in _CACHE:
            return _CACHE[cache_key]

        # Gather local user data for cross-referencing
        seen_index = get_user_seen_index(user_key)
        local_user_stats = get_stats(user_key)
        conn = get_connection()
        user_item_ids = {
            r["item_id"]
            for r in conn.execute("SELECT item_id FROM user_media WHERE user_key = ?", (user_key,)).fetchall()
        }

        # Cache media_items metadata map (title, genres, summary, tmdb_id, imdb_id)
        media_meta_rows = conn.execute(
            "SELECT item_id, media_type, title, year, genres, summary, tmdb_id, imdb_id FROM media_items"
        ).fetchall()
        media_meta: Dict[str, Dict[str, Any]] = {}
        for mr in media_meta_rows:
            d = dict(mr)
            if isinstance(d.get("genres"), str):
                try:
                    d["genres"] = json.loads(d["genres"])
                except Exception:
                    d["genres"] = []
            elif not isinstance(d.get("genres"), list):
                d["genres"] = []
            media_meta[d["item_id"]] = d
            # Also index by numeric ratingKey if prefixed
            if d["item_id"].startswith("movie_") or d["item_id"].startswith("show_"):
                raw_id = d["item_id"].split("_", 1)[1]
                media_meta[raw_id] = d
        conn.close()

        # Check if Tautulli is configured and can provide server-wide history
        if tautulli.is_configured():
            data = self._build_from_tautulli(current_user, seen_index, user_item_ids, local_user_stats, media_meta)
        else:
            data = self._build_from_db(current_user, seen_index, user_item_ids, local_user_stats, media_meta)

        _CACHE[cache_key] = data
        _CACHE_TIMESTAMP = now
        return data

    def _build_from_tautulli(
        self,
        current_user: Dict[str, Any],
        seen_index: Dict[str, Set[str]],
        user_item_ids: Set[str],
        local_user_stats: Dict[str, Any],
        media_meta: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Aggregate community data using Tautulli's server-wide statistics and history."""
        tautulli_users = tautulli.get_users()
        total_community_members = max(len(tautulli_users), len(get_all_users()))

        home_stats = tautulli.get_home_stats(time_range=30)
        recent_history = tautulli.get_history(length=30)

        # 1. Parse Popular Movies & TV
        popular_movies = []
        popular_tv = []

        for group in home_stats:
            stat_id = group.get("stat_id")
            rows = group.get("rows", []) or []

            if stat_id in ("popular_movies", "top_movies"):
                for r in rows:
                    item = self._format_media_item(r, "movie", seen_index, user_item_ids, media_meta)
                    if item and not any(m["title"] == item["title"] and m["year"] == item["year"] for m in popular_movies):
                        popular_movies.append(item)

            elif stat_id in ("popular_tv", "top_tv"):
                for r in rows:
                    item = self._format_media_item(r, "show", seen_index, user_item_ids, media_meta)
                    if item and not any(s["title"] == item["title"] for s in popular_tv):
                        popular_tv.append(item)

        # Slice to top 10 each
        popular_movies = popular_movies[:10]
        popular_tv = popular_tv[:10]

        # 2. Unseen Peer Recommendations (Watched by others on server, not seen by you)
        all_popular = popular_movies + popular_tv
        unseen_recommendations = [item for item in all_popular if not item["seen_by_user"]]
        # Sort by user viewers descending
        unseen_recommendations.sort(key=lambda x: (x.get("users_watched", 0), x.get("total_plays", 0)), reverse=True)

        # 3. Anonymized Recent Activity Feed (Option B)
        recent_activity = []
        my_username = (current_user.get("username") or "").lower()
        my_email = (current_user.get("email") or "").lower()

        server_movie_plays = 0
        server_tv_plays = 0
        server_total_duration_sec = 0

        for row in recent_history:
            row_user = (row.get("user") or "").lower()
            row_email = (row.get("email") or "").lower()
            is_me = bool(
                (my_username and row_user == my_username)
                or (my_email and row_email == my_email)
            )

            mtype = row.get("media_type", "movie")
            if mtype == "movie":
                server_movie_plays += 1
            else:
                server_tv_plays += 1

            dur = int(row.get("duration", 0) or 0)
            server_total_duration_sec += dur

            # Seen status for current user
            title = row.get("full_title") or row.get("title") or "Unknown"
            year = row.get("year")
            norm_t = normalize_title(title)
            seen_key = f"{norm_t}::{year or ''}"
            rk = str(row.get("rating_key") or "")
            item_seen = (
                (seen_key in seen_index["title_year"])
                or (rk in user_item_ids)
                or (f"movie_{rk}" in user_item_ids)
                or (f"show_{rk}" in user_item_ids)
            )

            # Clean timestamp
            started = row.get("started") or row.get("date")
            time_str = ""
            if started:
                try:
                    dt = datetime.fromtimestamp(int(started), tz=timezone.utc)
                    time_str = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    time_str = ""

            dur_str = ""
            if dur > 0:
                h = dur // 3600
                m = (dur % 3600) // 60
                dur_str = f"{h}h {m}m" if h > 0 else f"{m}m"

            # Strictly anonymized label (Option B)
            attribution = "Watched by You" if is_me else "Watched by a community member"

            recent_activity.append({
                "title": title,
                "year": year,
                "media_type": mtype,
                "time_str": time_str,
                "duration_formatted": dur_str,
                "is_me": is_me,
                "attribution": attribution,
                "seen_by_user": item_seen,
            })

        # 4. Benchmarks (You vs Community)
        user_events = local_user_stats.get("events_count", 0)
        user_movies = local_user_stats.get("movies_count", 0)
        user_shows = local_user_stats.get("shows_count", 0)
        user_total_items = user_movies + user_shows

        user_movie_pct = int(round((user_movies / user_total_items) * 100)) if user_total_items > 0 else 50
        user_tv_pct = 100 - user_movie_pct if user_total_items > 0 else 50

        server_total_plays = server_movie_plays + server_tv_plays
        comm_movie_pct = int(round((server_movie_plays / server_total_plays) * 100)) if server_total_plays > 0 else 55
        comm_tv_pct = 100 - comm_movie_pct if server_total_plays > 0 else 45

        # Community overlap calculation
        overlap_items = [m for m in all_popular if m["seen_by_user"]]
        overlap_pct = int(round((len(overlap_items) / max(1, len(all_popular))) * 100)) if all_popular else 0

        is_single_user = total_community_members <= 1

        return {
            "is_single_user": is_single_user,
            "total_community_members": total_community_members,
            "source": "tautulli",
            "benchmarks": {
                "user_plays": user_events,
                "user_movie_pct": user_movie_pct,
                "user_tv_pct": user_tv_pct,
                "community_movie_pct": comm_movie_pct,
                "community_tv_pct": comm_tv_pct,
                "overlap_count": len(overlap_items),
                "overlap_pct": overlap_pct,
            },
            "popular_movies": popular_movies,
            "popular_tv": popular_tv,
            "unseen_recommendations": unseen_recommendations,
            "recent_activity": recent_activity[:15],
        }

    def _build_from_db(
        self,
        current_user: Dict[str, Any],
        seen_index: Dict[str, Set[str]],
        user_item_ids: Set[str],
        local_user_stats: Dict[str, Any],
        media_meta: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Aggregate community data purely from Mindarr's local database (fallback/offline mode)."""
        all_users = get_all_users()
        total_community_members = len(all_users)
        user_key = str(current_user.get("user_key", ""))

        conn = get_connection()
        # Top movies across users in DB
        movie_rows = conn.execute("""
            SELECT um.item_id, m.title, m.year, m.genres, m.summary, m.tmdb_id, m.imdb_id,
                   COUNT(DISTINCT um.user_key) as user_count, SUM(um.view_count) as total_plays
            FROM user_media um
            LEFT JOIN media_items m ON m.item_id = um.item_id OR m.item_id = ('movie_' || um.item_id)
            WHERE m.media_type = 'movie'
            GROUP BY um.item_id
            ORDER BY user_count DESC, total_plays DESC
            LIMIT 10
        """).fetchall()

        # Top shows across users in DB
        show_rows = conn.execute("""
            SELECT um.item_id, m.title, m.year, m.genres, m.summary, m.tmdb_id, m.imdb_id,
                   COUNT(DISTINCT um.user_key) as user_count, SUM(um.view_count) as total_plays
            FROM user_media um
            LEFT JOIN media_items m ON m.item_id = um.item_id OR m.item_id = ('show_' || um.item_id)
            WHERE m.media_type IN ('show', 'episode')
            GROUP BY um.item_id
            ORDER BY user_count DESC, total_plays DESC
            LIMIT 10
        """).fetchall()

        # Recent server events
        recent_events = conn.execute("""
            SELECT w.item_id, w.title, w.media_type, w.viewed_at, w.duration_watched, w.user_key
            FROM watch_events w
            ORDER BY w.viewed_at DESC
            LIMIT 15
        """).fetchall()
        conn.close()

        def _format_db_item(r, mtype):
            d = dict(r)
            title = d.get("title") or "Unknown"
            year = d.get("year")
            norm_t = normalize_title(title)
            seen_key = f"{norm_t}::{year or ''}"
            rk = str(d.get("item_id") or "")
            seen = (
                (seen_key in seen_index["title_year"])
                or (rk in user_item_ids)
                or (f"movie_{rk}" in user_item_ids)
                or (f"show_{rk}" in user_item_ids)
            )
            genres = d.get("genres") or []
            if isinstance(genres, str):
                try:
                    genres = json.loads(genres)
                except Exception:
                    genres = []
            return {
                "title": title,
                "year": year,
                "media_type": mtype,
                "users_watched": d.get("user_count", 1),
                "total_plays": d.get("total_plays", 1),
                "seen_by_user": seen,
                "genres": genres,
                "summary": d.get("summary") or "",
                "tmdb_id": d.get("tmdb_id"),
                "imdb_id": d.get("imdb_id"),
            }

        popular_movies = [_format_db_item(r, "movie") for r in movie_rows]
        popular_tv = [_format_db_item(r, "show") for r in show_rows]
        all_popular = popular_movies + popular_tv
        unseen_recommendations = [item for item in all_popular if not item["seen_by_user"]]

        recent_activity = []
        for ev in recent_events:
            ev_d = dict(ev)
            is_me = str(ev_d.get("user_key", "")) == user_key
            title = ev_d.get("title") or "Unknown"
            dur = int(ev_d.get("duration_watched", 0) or 0)
            if dur > 10000:
                dur = dur // 1000
            dur_str = f"{dur // 60}m" if dur > 0 else ""
            recent_activity.append({
                "title": title,
                "media_type": ev_d.get("media_type", "movie"),
                "time_str": (ev_d.get("viewed_at") or "")[:16].replace("T", " "),
                "duration_formatted": dur_str,
                "is_me": is_me,
                "attribution": "Watched by You" if is_me else "Watched by a community member",
                "seen_by_user": is_me or (ev_d.get("item_id") in user_item_ids),
            })

        user_events = local_user_stats.get("events_count", 0)
        user_movies = local_user_stats.get("movies_count", 0)
        user_shows = local_user_stats.get("shows_count", 0)
        user_total_items = user_movies + user_shows
        user_movie_pct = int(round((user_movies / user_total_items) * 100)) if user_total_items > 0 else 50
        user_tv_pct = 100 - user_movie_pct if user_total_items > 0 else 50

        overlap_items = [m for m in all_popular if m["seen_by_user"]]
        overlap_pct = int(round((len(overlap_items) / max(1, len(all_popular))) * 100)) if all_popular else 0

        return {
            "is_single_user": total_community_members <= 1,
            "total_community_members": total_community_members,
            "source": "db",
            "benchmarks": {
                "user_plays": user_events,
                "user_movie_pct": user_movie_pct,
                "user_tv_pct": user_tv_pct,
                "community_movie_pct": 50,
                "community_tv_pct": 50,
                "overlap_count": len(overlap_items),
                "overlap_pct": overlap_pct,
            },
            "popular_movies": popular_movies,
            "popular_tv": popular_tv,
            "unseen_recommendations": unseen_recommendations,
            "recent_activity": recent_activity,
        }

    def _format_media_item(
        self,
        r: Dict[str, Any],
        media_type: str,
        seen_index: Dict[str, Set[str]],
        user_item_ids: Set[str],
        media_meta: Dict[str, Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        title = r.get("title")
        if not title:
            return None
        year = r.get("year")
        rk = str(r.get("rating_key") or r.get("grandparent_rating_key") or "")
        users_watched = r.get("users_watched") or 1
        try:
            users_watched = int(users_watched)
        except (ValueError, TypeError):
            users_watched = 1
        total_plays = r.get("total_plays") or 1
        try:
            total_plays = int(total_plays)
        except (ValueError, TypeError):
            total_plays = 1

        norm_t = normalize_title(title)
        seen_key = f"{norm_t}::{year or ''}"
        seen = (
            (seen_key in seen_index["title_year"])
            or (rk in user_item_ids)
            or (f"movie_{rk}" in user_item_ids)
            or (f"show_{rk}" in user_item_ids)
        )

        meta = media_meta.get(rk) or media_meta.get(f"{media_type}_{rk}") or {}

        return {
            "title": title,
            "year": year,
            "media_type": media_type,
            "rating_key": rk,
            "users_watched": users_watched,
            "total_plays": total_plays,
            "seen_by_user": seen,
            "genres": meta.get("genres", []),
            "summary": meta.get("summary", ""),
            "tmdb_id": meta.get("tmdb_id"),
            "imdb_id": meta.get("imdb_id"),
        }


community_service = CommunityService()
