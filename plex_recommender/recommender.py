import logging
from typing import Optional, Dict, Any, List
from plex_recommender.analyzer import analyzer
from plex_recommender.db import (
    get_user_seen_index,
    is_seen,
    normalize_title,
    get_cached_recommendations,
    set_cached_recommendations,
    get_library_availability_index,
    get_setting,
    get_user_media_items
)
from plex_recommender.discovery.tmdb import TMDbClient
from plex_recommender.discovery.overseerr import overseerr
from plex_recommender.config import settings

logger = logging.getLogger(__name__)

# TMDb genre IDs treated as children's content: Family (movie/TV) + Kids (TV).
# Animation (16) is intentionally excluded so anime / adult animation is kept.
KIDS_GENRE_IDS = {10751, 10762}

class ContentRecommender:
    def __init__(self, tmdb_client: Optional[TMDbClient] = None):
        self.tmdb = tmdb_client or TMDbClient()

    def get_recommendations(
        self,
        user_key: str,
        media_type: str = "all",         # "movie", "show", or "all"
        limit: int = 12,
        page: int = 1,
        min_rating: float = 7.0,
        genre_filter: Optional[str] = None,
        language: Optional[str] = "en",  # e.g. "en", "ja", "ko", "fr", or "all"
        only_available: bool = False,
        include_kids: bool = False,
        force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        Discover unseen movies and shows matching a user's taste profile.
        """
        profile = analyzer.analyze(user_key)
        if not profile.get("has_data"):
            return {
                "success": False,
                "error": "No watch history found. Recommendations require Tautulli history for your account.",
                "recommendations": []
            }

        target_language = language if language is not None else "en"

        # Pagination: allow up to MAX_PAGES page sets of `limit` items each.
        MAX_PAGES = 3
        page = max(1, min(int(page or 1), MAX_PAGES))

        # Check cache tied to sync freshness, scoped per user
        current_sync = get_setting("last_sync_time")
        cache_key = f"{user_key}:{media_type}:{genre_filter or ''}:{target_language}:{min_rating}:{limit}:{page}:{int(only_available)}:{int(include_kids)}"

        if not force_refresh:
            cached = get_cached_recommendations(cache_key, current_sync)
            if cached:
                logger.info(f"Serving recommendations from cache for key='{cache_key}' (sync: {current_sync})")
                return cached

        seen_index = get_user_seen_index(user_key)
        seen_imdb = seen_index["imdb"]
        seen_tmdb = seen_index["tmdb"]
        seen_tvdb = seen_index["tvdb"]
        seen_titles = seen_index["title_year"]

        # Library availability index (whole Plex library, keyed by external IDs)
        availability_index = get_library_availability_index()
        machine_id = settings.plex_machine_id

        top_genres = profile.get("top_genres", [])
        if not top_genres:
            return {
                "success": False,
                "error": "Not enough genre data in watch history to recommend content.",
                "recommendations": []
            }

        # Build query genre list & fast lookup
        genre_affinity_map = {g["genre"].lower(): g for g in top_genres}
        selected_genres = [genre_filter] if genre_filter else [g["genre"] for g in top_genres[:4]]
        favored_decades_map = {d["decade"]: d["count"] for d in profile.get("decades", [])}

        # Keyword affinity: fast lookup + top TMDb keyword ids to steer discovery.
        top_keywords = profile.get("top_keywords", [])
        keyword_affinity_map = {k["keyword"].lower(): k for k in top_keywords}
        query_keyword_ids = [k["id"] for k in top_keywords[:5] if k.get("id")]

        candidates = []
        raw_candidates_seen_ids = set()

        types_to_query = []
        if media_type in ("movie", "all"):
            types_to_query.append("movie")
        if media_type in ("show", "all"):
            types_to_query.append("show")

        lang_code = None if target_language == "all" else target_language

        for mtype in types_to_query:
            genre_ids = self.tmdb.map_genres(selected_genres, media_type=mtype)
            
            try:
                if mtype == "movie":
                    results = self.tmdb.discover_movies(
                        with_genres=genre_ids[:2],
                        with_original_language=lang_code,
                        with_keywords=query_keyword_ids,
                        min_rating=min_rating,
                        sort_by="vote_average.desc"
                    )
                    if genre_ids:
                        results += self.tmdb.discover_movies(
                            with_genres=[genre_ids[0]],
                            with_original_language=lang_code,
                            min_rating=min_rating,
                            sort_by="popularity.desc"
                        )
                else:
                    results = self.tmdb.discover_tv(
                        with_genres=genre_ids[:2],
                        with_original_language=lang_code,
                        with_keywords=query_keyword_ids,
                        min_rating=min_rating,
                        sort_by="vote_average.desc"
                    )
                    if genre_ids:
                        results += self.tmdb.discover_tv(
                            with_genres=[genre_ids[0]],
                            with_original_language=lang_code,
                            min_rating=min_rating,
                            sort_by="popularity.desc"
                        )

                for item in results:
                    cand_id = f"{mtype}_{item['id']}"
                    if cand_id not in raw_candidates_seen_ids:
                        raw_candidates_seen_ids.add(cand_id)
                        cand_item = self.tmdb.format_item(item, media_type=mtype)
                        # Filter by language if specified
                        if lang_code and cand_item.get("original_language", "en") != lang_code:
                            continue
                        candidates.append(cand_item)
            except Exception as e:
                logger.error(f"Error discovering {mtype} candidates: {e}")

        # Seed-based expansion: fan out from the user's highest-affinity SEEN
        # titles to TMDb's own recommendations/similar graph ("Because you
        # watched X"). Bounded to keep API usage reasonable.
        seed_origin = {}  # tmdb_id -> seed title
        try:
            seen_items = get_user_media_items(user_key)
            seeds = []
            for it in seen_items:
                st = it.get("tmdb_id")
                smtype = it.get("media_type")
                # Only movie/show seeds map cleanly to TMDb endpoints.
                if not st or smtype not in ("movie", "show"):
                    continue
                rating = it.get("user_rating") or it.get("audience_rating") or 0
                seeds.append((rating or 0, it.get("view_count") or 0, st, smtype, it.get("title", "")))
            # Highest-rated / most-watched first; cap the number of seeds.
            seeds.sort(key=lambda x: (x[0], x[1]), reverse=True)
            for _r, _vc, st, smtype, stitle in seeds[:8]:
                if media_type != "all" and media_type != smtype:
                    continue
                seed_type = "movie" if smtype == "movie" else "show"
                fanned = (
                    self.tmdb.get_tmdb_recommendations(int(st), media_type=seed_type)
                    + self.tmdb.get_similar(int(st), media_type=seed_type)
                )
                for item in fanned[:10]:
                    cand_id = f"{smtype}_{item['id']}"
                    if cand_id in raw_candidates_seen_ids:
                        continue
                    raw_candidates_seen_ids.add(cand_id)
                    cand_item = self.tmdb.format_item(item, media_type=smtype)
                    if lang_code and cand_item.get("original_language", "en") != lang_code:
                        continue
                    seed_origin[cand_item["tmdb_id"]] = stitle
                    candidates.append(cand_item)
        except Exception as e:
            logger.error(f"Error expanding seed-based recommendations: {e}")

        # Strict Unseen Deduplication & Enrichment
        unseen_recommendations = []
        for cand in candidates:
            tmdb_str = cand["tmdb_id"]
            if tmdb_str in seen_tmdb:
                continue

            # Exclude children's content (Family/Kids) unless explicitly requested.
            if not include_kids and set(cand.get("genre_ids", [])) & KIDS_GENRE_IDS:
                continue

            # Fetch external IDs (IMDb, TVDb)
            ext_ids = self.tmdb.get_external_ids(int(tmdb_str), media_type=cand["media_type"])
            imdb_id = ext_ids.get("imdb_id")
            tvdb_id = ext_ids.get("tvdb_id")

            # Fetch candidate keywords for finer-grained overlap scoring.
            cand_keywords = self.tmdb.get_keywords(int(tmdb_str), media_type=cand["media_type"])

            if imdb_id and imdb_id.lower() in seen_imdb:
                continue
            if tvdb_id and tvdb_id.lower() in seen_tvdb:
                continue

            # Check normalized title
            norm_title = normalize_title(cand["title"])
            year = cand.get("year")
            norm_key = f"{norm_title}::{year or ''}"
            if norm_key in seen_titles:
                continue
            if not year and any(k.startswith(f"{norm_title}::") for k in seen_titles):
                continue

            # Calculate Match Score & Detailed Decision Factors
            cand_genres = cand.get("genres", [])
            overlap_details = []
            for g in cand_genres:
                low_g = g.lower()
                if low_g in genre_affinity_map:
                    info = genre_affinity_map[low_g]
                    overlap_details.append({
                        "name": g,
                        "affinity": f"{info['score']}%",
                        "watched_count": info["count"]
                    })
            
            # Base match score from genre overlap
            match_score = 50.0
            score_points = [{"factor": "Base Discovery Match", "points": "+50%"}]

            genre_bonus = min(30.0, len(overlap_details) * 15.0)
            if genre_bonus > 0:
                match_score += genre_bonus
                names_str = ", ".join([d["name"] for d in overlap_details[:2]])
                score_points.append({"factor": f"Genre Overlap ({names_str})", "points": f"+{int(genre_bonus)}%"})

            # Keyword overlap bonus (finer-grained than genre)
            keyword_overlap = []
            for kw in cand_keywords:
                kw_name = (kw.get("name") or "").strip()
                if kw_name and kw_name.lower() in keyword_affinity_map:
                    info = keyword_affinity_map[kw_name.lower()]
                    keyword_overlap.append({
                        "name": kw_name,
                        "affinity": f"{info['score']}%",
                        "watched_count": info["count"],
                    })
            keyword_bonus = min(20.0, len(keyword_overlap) * 7.0)
            if keyword_bonus > 0:
                match_score += keyword_bonus
                kw_str = ", ".join([d["name"] for d in keyword_overlap[:3]])
                score_points.append({"factor": f"Theme Overlap ({kw_str})", "points": f"+{int(keyword_bonus)}%"})

            # Rating boost
            rating = cand.get("rating", 0)
            rating_bonus = 0
            if rating >= 8.0:
                rating_bonus = 15
                match_score += 15.0
            elif rating >= 7.5:
                rating_bonus = 10
                match_score += 10.0
            if rating_bonus:
                score_points.append({"factor": f"High Community Rating ({rating}/10)", "points": f"+{rating_bonus}%"})

            # Decade boost
            decade_str = None
            is_favored_decade = False
            if year:
                decade_str = f"{(year // 10) * 10}s"
                if decade_str in favored_decades_map and favored_decades_map[decade_str] >= 3:
                    is_favored_decade = True
                    match_score += 5.0
                    score_points.append({"factor": f"Favored Era ({decade_str})", "points": "+5%"})

            # Seed-based ("Because you watched X") bonus
            seed_title = seed_origin.get(tmdb_str)
            if seed_title:
                match_score += 10.0
                score_points.append({"factor": f"Similar to watched ({seed_title})", "points": "+10%"})

            cand["match_score"] = min(99, int(round(match_score)))
            cand["imdb_id"] = imdb_id
            cand["tvdb_id"] = tvdb_id
            cand["imdb_url"] = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None
            cand["tmdb_url"] = f"https://www.themoviedb.org/{cand['media_type']}/{tmdb_str}"
            cand["overseerr_url"] = overseerr.get_web_url(int(tmdb_str), media_type=cand["media_type"])

            # Determine Plex library availability by external IDs (GUID match)
            avail_info = None
            if tmdb_str and tmdb_str.lower() in availability_index["tmdb"]:
                avail_info = availability_index["tmdb"][tmdb_str.lower()]
            elif imdb_id and imdb_id.lower() in availability_index["imdb"]:
                avail_info = availability_index["imdb"][imdb_id.lower()]
            elif tvdb_id and str(tvdb_id).lower() in availability_index["tvdb"]:
                avail_info = availability_index["tvdb"][str(tvdb_id).lower()]

            cand["available_on_plex"] = avail_info is not None
            cand["plex_url"] = None
            if avail_info and machine_id and avail_info.get("rating_key"):
                cand["plex_url"] = (
                    f"https://app.plex.tv/desktop/#!/server/{machine_id}"
                    f"/details?key=/library/metadata/{avail_info['rating_key']}"
                )

            # Optionally restrict to server-available content
            if only_available and not cand["available_on_plex"]:
                continue

            # Formulate Rationale & Deep Decision Breakdown
            reasons = []
            if seed_title:
                reasons.append(f"Because you watched {seed_title}")
            if overlap_details:
                reasons.append(f"Matches top genre{'s' if len(overlap_details) > 1 else ''}: {', '.join([d['name'] for d in overlap_details[:2]])}")
            if keyword_overlap:
                reasons.append(f"Shared themes: {', '.join([d['name'] for d in keyword_overlap[:2]])}")
            if rating:
                reasons.append(f"Rating {rating}/10 ({cand.get('vote_count', 0):,} votes)")
            if year:
                reasons.append(f"Released {year}")

            cand["rationale"] = " • ".join(reasons)

            # Rich Decision Breakdown for "?" Tooltip
            cand["decision_factors"] = {
                "matched_genres": overlap_details,
                "matched_keywords": keyword_overlap,
                "seed_title": seed_title,
                "is_favored_decade": is_favored_decade,
                "decade_label": decade_str,
                "decade_watched": favored_decades_map.get(decade_str, 0) if decade_str else 0,
                "rating": rating,
                "vote_count": cand.get("vote_count", 0),
                "language": cand.get("language_name", "English"),
                "score_points": score_points
            }

            unseen_recommendations.append(cand)

        # Sort by match score descending, then rating descending
        unseen_recommendations.sort(key=lambda x: (x["match_score"], x["rating"]), reverse=True)

        # Pagination: cap total pages at MAX_PAGES and slice the requested page.
        unseen_count = len(unseen_recommendations)
        total_pages = min(MAX_PAGES, max(1, (unseen_count + limit - 1) // limit)) if limit > 0 else 1
        page = min(page, total_pages)
        start = (page - 1) * limit
        page_items = unseen_recommendations[start:start + limit]

        result = {
            "success": True,
            "total_candidates_scanned": len(candidates),
            "unseen_count": unseen_count,
            "recommendations": page_items,
            "page": page,
            "total_pages": total_pages,
            "limit": limit,
            "query_genres": selected_genres,
            "from_cache": False,
            "sync_version": current_sync
        }

        # Cache the calculated recommendations
        try:
            set_cached_recommendations(cache_key, current_sync, result)
        except Exception as e:
            logger.error(f"Failed to cache recommendations: {e}")

        return result

recommender = ContentRecommender()
