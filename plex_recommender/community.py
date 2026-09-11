import logging
import json
import time
import hashlib
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

        # Local user counts per item from user_media to cross-reference / fallback
        local_user_counts: Dict[str, Dict[str, int]] = {}
        try:
            counts_rows = conn.execute("""
                SELECT item_id, COUNT(DISTINCT user_key) as user_count, SUM(view_count) as total_plays
                FROM user_media
                GROUP BY item_id
            """).fetchall()
            for cr in counts_rows:
                iid = str(cr["item_id"])
                cnt_info = {
                    "user_count": int(cr["user_count"] or 1),
                    "total_plays": int(cr["total_plays"] or 1),
                }
                local_user_counts[iid] = cnt_info
                if iid.startswith("movie_") or iid.startswith("show_"):
                    raw_id = iid.split("_", 1)[1]
                    local_user_counts[raw_id] = cnt_info

            title_counts_rows = conn.execute("""
                SELECT m.title, m.year, COUNT(DISTINCT um.user_key) as user_count, SUM(um.view_count) as total_plays
                FROM user_media um
                JOIN media_items m ON m.item_id = um.item_id OR m.item_id = ('movie_' || um.item_id) OR m.item_id = ('show_' || um.item_id)
                GROUP BY m.title, m.year
            """).fetchall()
            for tr in title_counts_rows:
                if tr["title"]:
                    norm_k = f"{normalize_title(tr['title'])}::{tr['year'] or ''}"
                    local_user_counts[norm_k] = {
                        "user_count": int(tr["user_count"] or 1),
                        "total_plays": int(tr["total_plays"] or 1),
                    }
        except Exception as e:
            logger.debug(f"Could not compute local community user counts: {e}")

        # Collect local user metrics for leaderboard & gamification
        local_user_metrics: Dict[str, Dict[str, Any]] = {}
        try:
            um_rows = conn.execute("""
                SELECT um.user_key,
                       COUNT(*) as media_count,
                       COALESCE(SUM(um.view_count), 0) as total_plays,
                       COALESCE(SUM(CASE WHEN m.media_type = 'movie' THEN 1 ELSE 0 END), 0) as movie_count,
                       COALESCE(SUM(CASE WHEN m.media_type IN ('show', 'episode') THEN 1 ELSE 0 END), 0) as show_count
                FROM user_media um
                LEFT JOIN media_items m ON m.item_id = um.item_id OR m.item_id = ('movie_' || um.item_id) OR m.item_id = ('show_' || um.item_id)
                GROUP BY um.user_key
            """).fetchall()
            for r in um_rows:
                uk = str(r["user_key"] or "")
                local_user_metrics[uk] = {
                    "media_count": int(r["media_count"] or 0),
                    "total_plays": int(r["total_plays"] or 0),
                    "movie_count": int(r["movie_count"] or 0),
                    "show_count": int(r["show_count"] or 0),
                    "total_duration_seconds": 0,
                    "event_count": 0,
                    "genres": [],
                }

            we_rows = conn.execute("""
                SELECT user_key, duration_watched
                FROM watch_events
                WHERE duration_watched IS NOT NULL AND duration_watched > 0
            """).fetchall()
            for r in we_rows:
                uk = str(r["user_key"] or "")
                if uk not in local_user_metrics:
                    local_user_metrics[uk] = {
                        "media_count": 0, "total_plays": 0, "movie_count": 0,
                        "show_count": 0, "total_duration_seconds": 0, "event_count": 0, "genres": [],
                    }
                raw_d = int(r["duration_watched"] or 0)
                sec = raw_d // 1000 if raw_d > 10000 else raw_d
                local_user_metrics[uk]["total_duration_seconds"] += sec
                local_user_metrics[uk]["event_count"] += 1

            ug_rows = conn.execute("""
                SELECT um.user_key, m.genres
                FROM user_media um
                JOIN media_items m ON m.item_id = um.item_id OR m.item_id = ('movie_' || um.item_id) OR m.item_id = ('show_' || um.item_id)
                WHERE m.genres IS NOT NULL AND m.genres != '' AND m.genres != '[]'
            """).fetchall()
            for r in ug_rows:
                uk = str(r["user_key"] or "")
                if uk in local_user_metrics:
                    raw_g = r["genres"]
                    g_list = []
                    if isinstance(raw_g, str):
                        try:
                            g_list = json.loads(raw_g)
                        except Exception:
                            g_list = []
                    elif isinstance(raw_g, list):
                        g_list = raw_g
                    local_user_metrics[uk]["genres"].extend(g_list)
        except Exception as e:
            logger.debug(f"Could not compute local user metrics: {e}")

        conn.close()

        # Check if Tautulli is configured and can provide server-wide history
        if tautulli.is_configured():
            data = self._build_from_tautulli(current_user, seen_index, user_item_ids, local_user_stats, media_meta, local_user_counts, local_user_metrics)
        else:
            data = self._build_from_db(current_user, seen_index, user_item_ids, local_user_stats, media_meta, local_user_metrics)

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
        local_user_counts: Optional[Dict[str, Dict[str, int]]] = None,
        local_user_metrics: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Aggregate community data using Tautulli's server-wide statistics and history."""
        local_user_counts = local_user_counts or {}
        local_user_metrics = local_user_metrics or {}
        tautulli_users = tautulli.get_users()
        total_community_members = max(len(tautulli_users), len(get_all_users()))

        home_stats = tautulli.get_home_stats(time_range=30)
        recent_history = tautulli.get_history(length=30)

        # 1. Parse Popular Movies & TV (prioritize popular_* over top_* to capture true distinct viewer counts)
        stat_groups = {g.get("stat_id"): g.get("rows", []) or [] for g in home_stats}
        movie_rows = stat_groups.get("popular_movies", []) + stat_groups.get("top_movies", [])
        tv_rows = stat_groups.get("popular_tv", []) + stat_groups.get("top_tv", [])

        def _merge_popular_items(rows, mtype):
            merged: Dict[Tuple[str, Optional[int]], Dict[str, Any]] = {}
            for r in rows:
                item = self._format_media_item(r, mtype, seen_index, user_item_ids, media_meta, local_user_counts)
                if not item:
                    continue
                key = (normalize_title(item["title"]), item.get("year"))
                if key not in merged:
                    merged[key] = item
                else:
                    existing = merged[key]
                    existing["users_watched"] = max(existing.get("users_watched", 1), item.get("users_watched", 1))
                    existing["total_plays"] = max(existing.get("total_plays", 1), item.get("total_plays", 1))
            items_list = list(merged.values())
            items_list.sort(key=lambda x: (x.get("users_watched", 1), x.get("total_plays", 1)), reverse=True)
            return items_list[:10]

        popular_movies = _merge_popular_items(movie_rows, "movie")
        popular_tv = _merge_popular_items(tv_rows, "show")

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

        # Build candidate users from Tautulli top_users + local DB users
        tautulli_top_users = stat_groups.get("top_users", []) + stat_groups.get("popular_users", [])
        all_users = get_all_users()
        user_key = str(current_user.get("user_key", ""))

        # Index Mindarr users by lowercase username, email, title, and user_key
        user_by_name: Dict[str, Dict[str, Any]] = {}
        for u in all_users:
            if u.get("username"):
                user_by_name[u["username"].lower()] = u
            if u.get("email"):
                user_by_name[u["email"].lower()] = u
            if u.get("title"):
                user_by_name[u["title"].lower()] = u

        candidate_map: Dict[str, Dict[str, Any]] = {}

        # 1. Process Tautulli top users
        for tr in tautulli_top_users:
            raw_u = str(tr.get("user") or "")
            fname = str(tr.get("friendly_name") or raw_u or "Community Member")
            t_dur = int(tr.get("total_duration") or 0)
            t_plays = int(tr.get("total_plays") or 0)
            t_thumb = tr.get("thumb") or tr.get("user_thumb")

            matched_mindarr_u = (
                user_by_name.get(raw_u.lower())
                or user_by_name.get(fname.lower())
                or user_by_name.get(str(tr.get("email") or "").lower())
            )

            if matched_mindarr_u:
                uk = str(matched_mindarr_u["user_key"])
                local_info = local_user_metrics.get(uk, {})
                is_me = (uk == user_key)
                plays = max(t_plays, local_info.get("total_plays", 0))
                dur_sec = max(t_dur, local_info.get("total_duration_seconds", 0))
                m_cnt = local_info.get("movie_count", 0)
                s_cnt = local_info.get("show_count", 0)
                if is_me:
                    m_cnt = max(m_cnt, user_movies)
                    s_cnt = max(s_cnt, user_shows)
                if dur_sec == 0 and plays > 0:
                    dur_sec = m_cnt * 5400 + s_cnt * 2400
                candidate_map[uk] = {
                    "user_key": uk,
                    "username": matched_mindarr_u.get("username") or raw_u,
                    "display_name": matched_mindarr_u.get("title") or fname,
                    "email": matched_mindarr_u.get("email") or "",
                    "thumb": matched_mindarr_u.get("thumb") or t_thumb,
                    "is_me": is_me,
                    "total_plays": plays,
                    "total_duration_seconds": dur_sec,
                    "movie_count": m_cnt,
                    "show_count": s_cnt,
                    "genres": local_info.get("genres", []),
                    "is_registered": True,
                }
            else:
                c_key = f"tautulli_{tr.get('user_id') or raw_u}"
                is_me = bool(
                    (my_username and raw_u.lower() == my_username)
                    or (my_email and str(tr.get("email") or "").lower() == my_email)
                )
                candidate_map[c_key] = {
                    "user_key": c_key,
                    "username": raw_u or fname,
                    "display_name": fname,
                    "email": str(tr.get("email") or ""),
                    "thumb": t_thumb,
                    "is_me": is_me,
                    "total_plays": t_plays,
                    "total_duration_seconds": t_dur,
                    "movie_count": 0,
                    "show_count": 0,
                    "genres": [],
                    "is_registered": False,
                }

        # 2. Add registered Mindarr users not in Tautulli's top list
        for u in all_users:
            uk = str(u.get("user_key") or "")
            if uk not in candidate_map:
                is_me = (uk == user_key)
                local_info = local_user_metrics.get(uk, {})
                plays = local_info.get("total_plays", 0)
                if is_me and user_events > plays:
                    plays = user_events
                dur_sec = local_info.get("total_duration_seconds", 0)
                m_cnt = local_info.get("movie_count", 0)
                s_cnt = local_info.get("show_count", 0)
                if is_me:
                    m_cnt = max(m_cnt, user_movies)
                    s_cnt = max(s_cnt, user_shows)
                if dur_sec == 0 and plays > 0:
                    dur_sec = m_cnt * 5400 + s_cnt * 2400
                candidate_map[uk] = {
                    "user_key": uk,
                    "username": u.get("username") or "Community Member",
                    "display_name": u.get("title") or u.get("username") or "Community Member",
                    "email": u.get("email") or "",
                    "thumb": u.get("thumb"),
                    "is_me": is_me,
                    "total_plays": plays,
                    "total_duration_seconds": dur_sec,
                    "movie_count": m_cnt,
                    "show_count": s_cnt,
                    "genres": local_info.get("genres", []),
                    "is_registered": True,
                }

        candidate_users = list(candidate_map.values())
        leaderboard, user_comparison, podium = self._build_leaderboard_and_comparison(
            current_user=current_user,
            candidate_users=candidate_users,
            overlap_pct=overlap_pct,
        )

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
            "leaderboard": leaderboard,
            "user_comparison": user_comparison,
            "podium": podium,
        }

    def _build_from_db(
        self,
        current_user: Dict[str, Any],
        seen_index: Dict[str, Set[str]],
        user_item_ids: Set[str],
        local_user_stats: Dict[str, Any],
        media_meta: Dict[str, Dict[str, Any]],
        local_user_metrics: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Aggregate community data purely from Mindarr's local database (fallback/offline mode)."""
        all_users = get_all_users()
        total_community_members = len(all_users)
        user_key = str(current_user.get("user_key", ""))
        local_user_metrics = local_user_metrics or {}

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

        # Build candidate users from local DB
        candidate_users = []
        for u in all_users:
            uk = str(u.get("user_key") or "")
            is_me = (uk == user_key)
            uname = u.get("username") or "Community Member"
            dname = u.get("title") or uname
            um_info = local_user_metrics.get(uk, {})
            plays = um_info.get("total_plays", 0)
            if is_me and user_events > plays:
                plays = user_events
            dur_sec = um_info.get("total_duration_seconds", 0)
            m_cnt = um_info.get("movie_count", 0)
            s_cnt = um_info.get("show_count", 0)
            if is_me:
                m_cnt = max(m_cnt, user_movies)
                s_cnt = max(s_cnt, user_shows)
            if dur_sec == 0 and plays > 0:
                dur_sec = m_cnt * 5400 + s_cnt * 2400
            candidate_users.append({
                "user_key": uk,
                "username": uname,
                "display_name": dname,
                "email": u.get("email") or "",
                "thumb": u.get("thumb"),
                "is_me": is_me,
                "total_plays": plays,
                "total_duration_seconds": dur_sec,
                "movie_count": m_cnt,
                "show_count": s_cnt,
                "genres": um_info.get("genres", []),
                "is_registered": True,
            })

        leaderboard, user_comparison, podium = self._build_leaderboard_and_comparison(
            current_user=current_user,
            candidate_users=candidate_users,
            overlap_pct=overlap_pct,
        )

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
            "leaderboard": leaderboard,
            "user_comparison": user_comparison,
            "podium": podium,
        }

    def _format_media_item(
        self,
        r: Dict[str, Any],
        media_type: str,
        seen_index: Dict[str, Set[str]],
        user_item_ids: Set[str],
        media_meta: Dict[str, Dict[str, Any]],
        local_user_counts: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> Optional[Dict[str, Any]]:
        title = r.get("title")
        if not title:
            return None
        year = r.get("year")
        rk = str(r.get("rating_key") or r.get("grandparent_rating_key") or "")
        norm_t = normalize_title(title)
        seen_key = f"{norm_t}::{year or ''}"

        local_user_counts = local_user_counts or {}
        local_info = (
            local_user_counts.get(rk)
            or local_user_counts.get(f"{media_type}_{rk}")
            or local_user_counts.get(seen_key)
            or {}
        )
        local_user_cnt = local_info.get("user_count", 1)
        local_total_plays = local_info.get("total_plays", 1)

        raw_users = r.get("users_watched")
        users_watched = None
        if raw_users is not None and str(raw_users).strip().isdigit():
            users_watched = int(str(raw_users).strip())

        # If Tautulli didn't supply viewer count or gave 1, fallback to local DB user count
        if users_watched is None or users_watched <= 1:
            users_watched = max(users_watched or 1, local_user_cnt)

        raw_plays = r.get("total_plays")
        total_plays = None
        if raw_plays is not None and str(raw_plays).strip().isdigit():
            total_plays = int(str(raw_plays).strip())
        if total_plays is None or total_plays <= 1:
            total_plays = max(total_plays or 1, local_total_plays)

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

    @staticmethod
    def _format_duration(seconds: int) -> str:
        if seconds <= 0:
            return "0m"
        hrs = seconds // 3600
        mins = (seconds % 3600) // 60
        if hrs > 0:
            return f"{hrs}h {mins}m" if mins > 0 else f"{hrs}h"
        return f"{mins}m" if mins > 0 else f"{seconds}s"

    @staticmethod
    def _generate_anonymized_name(seed_key: str) -> str:
        """Generate a consistent, realistic pseudonym using Faker seeded by user identity."""
        try:
            from faker import Faker
            f = Faker()
            # Derive a deterministic integer seed from SHA-256 of the user key
            seed_int = int(hashlib.sha256(str(seed_key).encode("utf-8")).hexdigest()[:8], 16)
            f.seed_instance(seed_int)
            first = f.first_name()
            last_initial = f.last_name()[:1]
            return f"{first} {last_initial}."
        except Exception:
            clean_seed = abs(hash(str(seed_key))) % 1000
            return f"Community Member #{clean_seed}"

    @staticmethod
    def _compute_level_and_xp(plays: int, duration_seconds: int) -> Dict[str, Any]:
        """Compute RPG-style Watcher Level and XP progression."""
        dur_mins = max(0, duration_seconds) // 60
        xp = int(max(0, plays) * 25 + (dur_mins * 0.5))

        # Threshold formula: Level n requires total XP = int(100 * ((n - 1) ** (1 / 0.55)))
        level = 1
        while level < 50:
            next_req = int(100 * (level ** (1 / 0.55)))
            if xp < next_req:
                break
            level += 1

        curr_tier_xp = 0 if level == 1 else int(100 * ((level - 1) ** (1 / 0.55)))
        next_tier_xp = int(100 * (level ** (1 / 0.55)))
        xp_in_level = max(0, xp - curr_tier_xp)
        xp_needed_in_level = max(1, next_tier_xp - curr_tier_xp)
        progress_pct = min(100, max(0, int(round((xp_in_level / xp_needed_in_level) * 100))))

        return {
            "xp": xp,
            "level": level,
            "progress_pct": progress_pct,
            "current_tier_xp": curr_tier_xp,
            "next_tier_xp": next_tier_xp,
            "xp_needed": max(0, next_tier_xp - xp),
        }

    @staticmethod
    def _determine_watcher_title(rank: int, plays: int, total_hours: float, movie_pct: int, tv_pct: int) -> str:
        """Assign dynamic watcher titles based on rank, volume, and movie/show mix."""
        if rank == 1 and plays > 0:
            return "Server Champion 👑"
        if rank == 2 and plays > 0:
            return "Binge Prodigy 🥈"
        if rank == 3 and plays > 0:
            return "Silver Screen Ace 🥉"
        if movie_pct >= 65 and plays >= 10:
            return "Cinephile Extraordinaire 🎬"
        if tv_pct >= 65 and plays >= 10:
            return "Series Devotee 📺"
        if total_hours >= 100:
            return "Marathon Legend 🏃"
        if total_hours >= 35 or plays >= 40:
            return "Dedicated Streamer 🍿"
        if plays >= 15:
            return "Culture Connoisseur 🎭"
        return "Rising Critic 🌟"

    @staticmethod
    def _evaluate_badges(
        rank: int,
        plays: int,
        total_hours: float,
        movie_count: int,
        show_count: int,
        genre_count: int,
        overlap_pct: int,
        is_registered: bool,
    ) -> List[Dict[str, Any]]:
        """Award badges earned by viewing behavior and server participation."""
        badges = []
        if rank == 1 and plays > 0:
            badges.append({
                "id": "champion",
                "name": "Server Champion",
                "icon": "👑",
                "desc": "Ranked #1 Top Watcher on the server",
                "unlocked": True,
            })
        if total_hours >= 100:
            badges.append({
                "id": "century",
                "name": "Century Club",
                "icon": "⏱️",
                "desc": "Logged 100+ hours of playback",
                "unlocked": True,
            })
        elif total_hours >= 25:
            badges.append({
                "id": "marathon",
                "name": "Marathoner",
                "icon": "🍿",
                "desc": "Logged 25+ hours of playback",
                "unlocked": True,
            })
        if movie_count >= 15:
            badges.append({
                "id": "cinephile",
                "name": "Film Buff",
                "icon": "🎬",
                "desc": "Streamed 15+ feature films",
                "unlocked": True,
            })
        if show_count >= 25 or (plays - movie_count) >= 25:
            badges.append({
                "id": "binge",
                "name": "Binge Titan",
                "icon": "📺",
                "desc": "Watched 25+ TV episodes or series",
                "unlocked": True,
            })
        if genre_count >= 4:
            badges.append({
                "id": "explorer",
                "name": "Genre Explorer",
                "icon": "🧭",
                "desc": "Explored 4 or more diverse genres",
                "unlocked": True,
            })
        if overlap_pct >= 40:
            badges.append({
                "id": "trendsetter",
                "name": "Taste Maker",
                "icon": "🎯",
                "desc": "Seen 40%+ of server trending titles",
                "unlocked": True,
            })
        if is_registered:
            badges.append({
                "id": "pioneer",
                "name": "Server Pioneer",
                "icon": "🌟",
                "desc": "Registered community member of this Plex server",
                "unlocked": True,
            })
        return badges

    def _build_leaderboard_and_comparison(
        self,
        current_user: Dict[str, Any],
        candidate_users: List[Dict[str, Any]],
        overlap_pct: int = 0,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
        """Construct server-wide leaderboard, rank ordering, badges, and user head-to-head comparison."""
        from collections import Counter

        my_uk = str(current_user.get("user_key") or "")
        my_username = (current_user.get("username") or "").lower()
        my_email = (current_user.get("email") or "").lower()

        # Ensure current user is in candidate list
        has_me = any(c.get("is_me") for c in candidate_users)
        if not has_me:
            c_key = my_uk or "1"
            c_user = current_user.get("username") or "You"
            c_title = current_user.get("title") or c_user
            candidate_users.append({
                "user_key": c_key,
                "username": c_user,
                "display_name": c_title,
                "email": current_user.get("email") or "",
                "thumb": current_user.get("thumb"),
                "is_me": True,
                "total_plays": 0,
                "total_duration_seconds": 0,
                "movie_count": 0,
                "show_count": 0,
                "genres": [],
                "is_registered": True,
            })

        is_admin = bool(current_user.get("is_admin"))

        for c in candidate_users:
            if not c.get("is_me"):
                raw_u = c.get("username")
                raw_d = c.get("display_name")
                anon_name = self._generate_anonymized_name(c.get("user_key") or raw_u or "user")
                c["display_name"] = anon_name
                c["username"] = anon_name
                c["thumb"] = None  # Privacy: mask avatar photos of other users
                c["is_anonymized"] = True
                # Expose real username only if requesting viewer is server admin
                if is_admin:
                    c["real_username"] = raw_u or raw_d
                    c["real_display_name"] = raw_d or raw_u
                else:
                    c["real_username"] = None
                    c["real_display_name"] = None
            else:
                c["is_anonymized"] = False
                c["real_username"] = c.get("username")
                c["real_display_name"] = c.get("display_name")

            plays = int(c.get("total_plays", 0) or 0)
            dur_sec = int(c.get("total_duration_seconds", 0) or 0)
            m_cnt = int(c.get("movie_count", 0) or 0)
            s_cnt = int(c.get("show_count", 0) or 0)
            genres = c.get("genres") or []

            top_genre = "Varied"
            if genres:
                gc = Counter(genres)
                top_genre = gc.most_common(1)[0][0]

            tot_items = m_cnt + s_cnt
            if tot_items > 0:
                m_pct = int(round((m_cnt / tot_items) * 100))
                s_pct = 100 - m_pct
            else:
                m_pct = 50
                s_pct = 50

            tot_hours = round(dur_sec / 3600.0, 1) if dur_sec > 0 else 0.0
            gamification = self._compute_level_and_xp(plays, dur_sec)

            c["total_plays"] = plays
            c["total_duration_seconds"] = dur_sec
            c["total_hours"] = tot_hours
            c["duration_formatted"] = self._format_duration(dur_sec)
            c["movie_count"] = m_cnt
            c["show_count"] = s_cnt
            c["movie_pct"] = m_pct
            c["tv_pct"] = s_pct
            c["top_genre"] = top_genre
            c["genre_count"] = len(set(genres))
            c["gamification"] = gamification

        # Sort descending: XP, then total plays, then watch duration
        candidate_users.sort(
            key=lambda x: (
                x["gamification"]["xp"],
                x["total_plays"],
                x["total_duration_seconds"]
            ),
            reverse=True
        )

        for i, c in enumerate(candidate_users):
            rank = i + 1
            c["rank"] = rank
            w_title = self._determine_watcher_title(
                rank=rank,
                plays=c["total_plays"],
                total_hours=c["total_hours"],
                movie_pct=c["movie_pct"],
                tv_pct=c["tv_pct"]
            )
            c["gamification"]["title"] = w_title
            badges = self._evaluate_badges(
                rank=rank,
                plays=c["total_plays"],
                total_hours=c["total_hours"],
                movie_count=c["movie_count"],
                show_count=c["show_count"],
                genre_count=c["genre_count"],
                overlap_pct=overlap_pct if c.get("is_me") else 0,
                is_registered=c.get("is_registered", False)
            )
            c["gamification"]["badges"] = badges

        # User head-to-head comparison
        me = next((c for c in candidate_users if c.get("is_me")), candidate_users[0])
        user_rank = me["rank"]
        leader = candidate_users[0]
        is_leader = (user_rank == 1)
        total_watchers = len(candidate_users)
        percentile = max(1, int(round((user_rank / max(1, total_watchers)) * 100)))

        plays_behind = max(0, leader["total_plays"] - me["total_plays"])
        hours_behind = max(0.0, round(leader["total_hours"] - me["total_hours"], 1))
        plays_pct_of_leader = min(100, int(round((me["total_plays"] / max(1, leader["total_plays"])) * 100)))
        hours_pct_of_leader = min(100, int(round((me["total_hours"] / max(0.1, leader["total_hours"])) * 100)))

        next_rank_gap = 0
        next_rank_user = None
        if not is_leader and user_rank > 1:
            next_entry = candidate_users[user_rank - 2]
            next_rank_gap = max(1, (next_entry["total_plays"] - me["total_plays"]) + 1)
            next_rank_user = next_entry.get("display_name") or next_entry.get("username")

        if is_leader:
            if total_watchers > 1:
                lead_margin = me["total_plays"] - candidate_users[1]["total_plays"]
                rank_message = f"You reign supreme! 👑 Leading the server by {lead_margin} play{'s' if lead_margin != 1 else ''}."
            else:
                rank_message = "You are the reigning #1 Server Pioneer! 👑"
        elif user_rank == 2:
            rank_message = f"Hot on their heels! ⚔️ Only {next_rank_gap} more play{'s' if next_rank_gap != 1 else ''} to take #1 from {leader.get('display_name') or leader.get('username')}!"
        else:
            rank_message = f"Climbing the ranks! 🚀 Only {next_rank_gap} more play{'s' if next_rank_gap != 1 else ''} to overtake #{user_rank - 1} ({next_rank_user})!"

        unlocked_ids = {b["id"] for b in me["gamification"]["badges"]}
        all_badge_catalog = [
            {"id": "champion", "name": "Server Champion", "icon": "👑", "desc": "Ranked #1 Top Watcher on the server", "hint": "Claim the #1 spot on the leaderboard"},
            {"id": "century", "name": "Century Club", "icon": "⏱️", "desc": "Logged 100+ hours of playback", "hint": "Stream 100 hours of content"},
            {"id": "marathon", "name": "Marathoner", "icon": "🍿", "desc": "Logged 25+ hours of playback", "hint": "Stream 25 hours of content"},
            {"id": "cinephile", "name": "Film Buff", "icon": "🎬", "desc": "Streamed 15+ feature films", "hint": "Watch 15 feature films"},
            {"id": "binge", "name": "Binge Titan", "icon": "📺", "desc": "Watched 25+ TV episodes or series", "hint": "Watch 25 TV episodes"},
            {"id": "explorer", "name": "Genre Explorer", "icon": "🧭", "desc": "Explored 4 or more diverse genres", "hint": "Watch titles from 4 different genres"},
            {"id": "trendsetter", "name": "Taste Maker", "icon": "🎯", "desc": "Seen 40%+ of server trending titles", "hint": "Watch more titles trending in community"},
            {"id": "pioneer", "name": "Server Pioneer", "icon": "🌟", "desc": "Registered community member of this Plex server", "hint": "Sign in with your Plex account"},
        ]
        unlocked_badges = me["gamification"]["badges"]
        locked_badges = [b for b in all_badge_catalog if b["id"] not in unlocked_ids]

        user_comparison = {
            "user_rank": user_rank,
            "total_watchers": total_watchers,
            "percentile": percentile,
            "is_leader": is_leader,
            "leader": leader,
            "current_user": me,
            "plays_behind": plays_behind,
            "hours_behind": hours_behind,
            "plays_pct_of_leader": plays_pct_of_leader,
            "hours_pct_of_leader": hours_pct_of_leader,
            "next_rank_gap": next_rank_gap,
            "next_rank_user": next_rank_user,
            "rank_message": rank_message,
            "unlocked_badges": unlocked_badges,
            "locked_badges": locked_badges,
        }

        podium = {
            "first": candidate_users[0] if len(candidate_users) >= 1 else None,
            "second": candidate_users[1] if len(candidate_users) >= 2 else None,
            "third": candidate_users[2] if len(candidate_users) >= 3 else None,
        }

        return candidate_users, user_comparison, podium


community_service = CommunityService()
