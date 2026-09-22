import logging
import concurrent.futures
from typing import Optional, Dict, Any, List, Tuple
from plex_recommender.analyzer import analyzer
from plex_recommender.db.watch import get_user_seen_index, get_user_media_items
from plex_recommender.db._util import normalize_title
from plex_recommender.db.recommendations import get_cached_recommendations, set_cached_recommendations
from plex_recommender.db.media import get_library_availability_index
from plex_recommender.db.recommendations import get_setting
from plex_recommender.db.engagement import get_user_votes, get_user_vote_items
from plex_recommender.discovery.tmdb import (
    TMDbClient,
    candidate_matches_genre,
    to_canonical_genre,
    genres_align
)
from plex_recommender.discovery.overseerr import overseerr
from plex_recommender.config import settings

logger = logging.getLogger(__name__)

# TMDb genre IDs treated as children's content: Family (movie/TV) + Kids (TV).
# Animation (16) is intentionally excluded so anime / adult animation is kept.
KIDS_GENRE_IDS = {10751, 10762}
MAX_PAGES = 3


def _exec_discover(fn, kwargs, mtype):
    """Run a single TMDb discover query, swallowing errors into an empty result."""
    try:
        res = fn(**kwargs)
        return mtype, res
    except Exception as e:
        logger.error(f"Error in discovery for {mtype}: {e}")
        return mtype, []


def build_discover_tasks(
    tmdb: TMDbClient,
    types_to_query: List[str],
    selected_genres: List[str],
    genre_filter: Optional[str],
    lang_code: Optional[str],
    query_keyword_ids: Optional[List[int]],
    min_rating: float,
) -> List[Tuple[Any, Dict[str, Any], str]]:
    """Build the list of (fn, kwargs, media_type) TMDb discover query tasks.

    Returns an empty task for a media type if the selected genres don't map
    to any TMDb genre ID for that format (e.g. Romance has no TV genre ID).
    """
    discover_tasks: List[Tuple[Any, Dict[str, Any], str]] = []
    for mtype in types_to_query:
        genre_ids = tmdb.map_genres(selected_genres, media_type=mtype)
        if genre_filter and not genre_ids:
            # Format does not support this genre in TMDb (e.g. Romance in TV)
            continue

        if mtype == "movie":
            discover_tasks.append((
                tmdb.discover_movies,
                {
                    "with_genres": genre_ids[:2],
                    "with_original_language": lang_code,
                    "with_keywords": query_keyword_ids,
                    "min_rating": min_rating,
                    "sort_by": "vote_average.desc"
                },
                "movie"
            ))
            if genre_ids:
                discover_tasks.append((
                    tmdb.discover_movies,
                    {
                        "with_genres": [genre_ids[0]],
                        "with_original_language": lang_code,
                        "min_rating": min_rating,
                        "sort_by": "popularity.desc"
                    },
                    "movie"
                ))
        else:
            discover_tasks.append((
                tmdb.discover_tv,
                {
                    "with_genres": genre_ids[:2],
                    "with_original_language": lang_code,
                    "with_keywords": query_keyword_ids,
                    "min_rating": min_rating,
                    "sort_by": "vote_average.desc"
                },
                "show"
            ))
            if genre_ids:
                discover_tasks.append((
                    tmdb.discover_tv,
                    {
                        "with_genres": [genre_ids[0]],
                        "with_original_language": lang_code,
                        "min_rating": min_rating,
                        "sort_by": "popularity.desc"
                    },
                    "show"
                ))
    return discover_tasks


def run_discover_stage(
    tmdb: TMDbClient,
    discover_tasks: List[Tuple[Any, Dict[str, Any], str]],
    lang_code: Optional[str],
) -> Tuple[List[Dict[str, Any]], set]:
    """Execute discover queries in parallel and return deduped, formatted candidates.

    Returns (candidates, raw_candidates_seen_ids).
    """
    candidates: List[Dict[str, Any]] = []
    raw_candidates_seen_ids: set = set()

    max_disc_workers = max(1, min(len(discover_tasks), 4))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_disc_workers) as executor:
        fut_to_task = [executor.submit(_exec_discover, t[0], t[1], t[2]) for t in discover_tasks]
        for fut in concurrent.futures.as_completed(fut_to_task):
            mtype, results = fut.result()
            for item in results:
                cand_id = f"{mtype}_{item['id']}"
                if cand_id not in raw_candidates_seen_ids:
                    raw_candidates_seen_ids.add(cand_id)
                    cand_item = tmdb.format_item(item, media_type=mtype)
                    if lang_code and cand_item.get("original_language", "en") != lang_code:
                        continue
                    candidates.append(cand_item)

    return candidates, raw_candidates_seen_ids


def build_seed_list(
    user_key: str,
    media_type: str,
    genre_filter: Optional[str],
    upvoted_items: List[Dict[str, Any]],
    seen_items: List[Dict[str, Any]],
) -> List[Tuple[float, int, Any, str, str]]:
    """Build the ranked list of (rating, view_count, tmdb_id, media_type, title) seed tuples.

    Upvoted items are injected with top priority (rating=999.0). Watch-history
    items are ranked by user/audience rating then view count, filtered to the
    requested media type and (if set) genre.
    """
    seeds: List[Tuple[float, int, Any, str, str]] = []

    for uv in upvoted_items:
        st = uv.get("tmdb_id")
        smtype = uv.get("media_type")
        if not st or smtype not in ("movie", "show"):
            continue
        if media_type != "all" and media_type != smtype:
            continue
        seeds.append((999.0, 999, st, smtype, uv.get("title", "")))

    for it in seen_items:
        st = it.get("tmdb_id")
        smtype = it.get("media_type")
        if not st or smtype not in ("movie", "show"):
            continue
        if media_type != "all" and media_type != smtype:
            continue
        # Strict seed genre filtering: only seed from history matching the selected genre
        if genre_filter:
            it_genres = it.get("genres", [])
            if not any(candidate_matches_genre({"genres": [g], "genre_ids": []}, genre_filter) for g in it_genres):
                continue

        rating = it.get("user_rating") or it.get("audience_rating") or 0
        seeds.append((rating or 0, it.get("view_count") or 0, st, smtype, it.get("title", "")))

    seeds.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return seeds


def _fetch_seed_recs(tmdb: TMDbClient, seed_info: Tuple[float, int, Any, str, str]) -> Tuple[str, str, List[Dict[str, Any]]]:
    """Fan out a single seed item into TMDb 'recommendations' + 'similar' results."""
    _r, _vc, st, smtype, stitle = seed_info
    seed_type = "movie" if smtype == "movie" else "show"
    fanned = (
        tmdb.get_tmdb_recommendations(int(st), media_type=seed_type)
        + tmdb.get_similar(int(st), media_type=seed_type)
    )
    return smtype, stitle, fanned


def run_seed_expansion_stage(
    tmdb: TMDbClient,
    top_seeds: List[Tuple[float, int, Any, str, str]],
    candidates: List[Dict[str, Any]],
    raw_candidates_seen_ids: set,
    lang_code: Optional[str],
) -> Dict[str, str]:
    """Fan out top seeds into TMDb similar/recommendation candidates.

    Mutates `candidates` and `raw_candidates_seen_ids` in place (appending
    newly discovered items). Returns seed_origin: tmdb_id -> seed title, used
    later for "Because you watched X" rationale and scoring bonuses.
    """
    seed_origin: Dict[str, str] = {}
    if not top_seeds:
        return seed_origin

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(top_seeds))) as executor:
        seed_futs = [executor.submit(_fetch_seed_recs, tmdb, s) for s in top_seeds]
        for fut in concurrent.futures.as_completed(seed_futs):
            try:
                smtype, stitle, fanned = fut.result()
                for item in fanned[:10]:
                    cand_id = f"{smtype}_{item['id']}"
                    if cand_id in raw_candidates_seen_ids:
                        continue
                    raw_candidates_seen_ids.add(cand_id)
                    cand_item = tmdb.format_item(item, media_type=smtype)
                    if lang_code and cand_item.get("original_language", "en") != lang_code:
                        continue
                    seed_origin[cand_item["tmdb_id"]] = stitle
                    candidates.append(cand_item)
            except Exception as e:
                logger.debug(f"Error resolving seed future: {e}")

    return seed_origin


def passes_seen_filters(
    cand: Dict[str, Any],
    seen_tmdb: set,
    seen_titles: set,
) -> bool:
    """Return False (and should be counted as seen-filtered) if cand matches a seen tmdb id or normalized title."""
    tmdb_str = cand["tmdb_id"]
    if tmdb_str in seen_tmdb:
        return False

    norm_title = normalize_title(cand["title"])
    year = cand.get("year")
    norm_key = f"{norm_title}::{year or ''}"
    if norm_key in seen_titles:
        return False
    if not year and any(k.startswith(f"{norm_title}::") for k in seen_titles):
        return False

    return True


def passes_pool_filters(
    cand: Dict[str, Any],
    genre_filter: Optional[str],
    include_kids: bool,
    min_rating: float,
) -> bool:
    """Genre/kids/rating filters that don't count toward the seen-exclusion metric."""
    if genre_filter and not candidate_matches_genre(cand, genre_filter):
        return False
    if not include_kids and set(cand.get("genre_ids", [])) & KIDS_GENRE_IDS:
        return False
    rating = cand.get("rating", 0)
    if rating < min_rating:
        return False
    return True


def compute_genre_overlap(
    cand_genres: List[str],
    genre_affinity_map: Dict[str, Dict[str, Any]],
    top_genres: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return overlap details between a candidate's genres and the user's top genres."""
    overlap_details = []
    for g in cand_genres:
        low_g = g.lower()
        info = None
        if low_g in genre_affinity_map:
            info = genre_affinity_map[low_g]
        else:
            for tg in top_genres:
                if genres_align(g, tg.get("genre")):
                    info = tg
                    break
        if info:
            overlap_details.append({
                "name": g,
                "affinity": f"{info['score']}%",
                "watched_count": info["count"]
            })
    return overlap_details


def compute_keyword_overlap(
    cand_keywords: List[Any],
    keyword_affinity_map: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return overlap details between a candidate's keywords and the user's top keywords."""
    keyword_overlap = []
    for kw in cand_keywords:
        kw_name = (kw.get("name") if isinstance(kw, dict) else str(kw) or "").strip()
        if kw_name and kw_name.lower() in keyword_affinity_map:
            info = keyword_affinity_map[kw_name.lower()]
            keyword_overlap.append({
                "name": kw_name,
                "affinity": f"{info['score']}%",
                "watched_count": info["count"],
            })
    return keyword_overlap


def score_candidate(
    cand: Dict[str, Any],
    *,
    top_genres: List[Dict[str, Any]],
    genre_affinity_map: Dict[str, Dict[str, Any]],
    keyword_affinity_map: Dict[str, Dict[str, Any]],
    favored_decades_map: Dict[str, int],
    upvoted_ids: set,
    upvoted_titles: set,
    seed_origin: Dict[str, str],
    genre_filter: Optional[str],
) -> None:
    """Compute match score, rationale-support fields, and genre/badge display order.

    Mutates `cand` in place, adding: genres (reordered), matched_category,
    theme_badges, match_score, is_upvoted, and the temporary `_*` fields
    consumed later by `build_rationale` (and cleaned up by the caller).
    """
    tmdb_str = cand["tmdb_id"]
    rating = cand.get("rating", 0)
    year = cand.get("year")

    cand_genres = cand.get("genres", [])
    overlap_details = compute_genre_overlap(cand_genres, genre_affinity_map, top_genres)

    match_score = 50.0
    score_points = [{"factor": "Base Discovery Match", "points": "+50%"}]

    genre_bonus = min(30.0, len(overlap_details) * 15.0)
    if genre_bonus > 0:
        match_score += genre_bonus
        names_str = ", ".join([d["name"] for d in overlap_details[:2]])
        score_points.append({"factor": f"Genre Overlap ({names_str})", "points": f"+{int(genre_bonus)}%"})

    cand_keywords = cand.get("keywords", [])
    keyword_overlap = compute_keyword_overlap(cand_keywords, keyword_affinity_map)
    keyword_bonus = min(20.0, len(keyword_overlap) * 7.0)
    if keyword_bonus > 0:
        match_score += keyword_bonus
        kw_str = ", ".join([d["name"] for d in keyword_overlap[:3]])
        score_points.append({"factor": f"Theme Overlap ({kw_str})", "points": f"+{int(keyword_bonus)}%"})

    rating_bonus = 0
    if rating >= 8.0:
        rating_bonus = 15
        match_score += 15.0
    elif rating >= 7.5:
        rating_bonus = 10
        match_score += 10.0
    if rating_bonus:
        score_points.append({"factor": f"High Community Rating ({rating}/10)", "points": f"+{rating_bonus}%"})

    decade_str = None
    is_favored_decade = False
    if year:
        decade_str = f"{(year // 10) * 10}s"
        if decade_str in favored_decades_map and favored_decades_map[decade_str] >= 3:
            is_favored_decade = True
            match_score += 5.0
            score_points.append({"factor": f"Favored Era ({decade_str})", "points": "+5%"})

    # Direct Upvote boost
    is_upvoted = tmdb_str in upvoted_ids
    cand["is_upvoted"] = is_upvoted
    if is_upvoted:
        match_score += 20.0
        score_points.append({"factor": "Explicitly Upvoted (Thumbs Up)", "points": "+20%"})

    seed_title = seed_origin.get(tmdb_str)
    if seed_title:
        is_upvoted_seed = seed_title in upvoted_titles
        bonus = 15.0 if is_upvoted_seed else 10.0
        pts_label = f"+{int(bonus)}%"
        factor_text = f"Aligned with upvoted title ({seed_title})" if is_upvoted_seed else f"Similar to watched ({seed_title})"
        match_score += bonus
        score_points.append({"factor": factor_text, "points": pts_label})

    # Re-order genres so that the matched category is first
    sorted_genres = list(cand_genres)
    if genre_filter:
        matched_g = []
        other_g = []
        for g in sorted_genres:
            if genres_align(g, genre_filter):
                matched_g.append(g)
            else:
                other_g.append(g)
        sorted_genres = matched_g + other_g
    cand["genres"] = sorted_genres
    cand["matched_category"] = genre_filter if genre_filter else None
    cand["theme_badges"] = [kw["name"] for kw in keyword_overlap[:3]]

    cand["match_score"] = min(99, int(round(match_score)))
    cand["_seed_title"] = seed_title
    cand["_overlap_details"] = overlap_details
    cand["_keyword_overlap"] = keyword_overlap
    cand["_is_favored_decade"] = is_favored_decade
    cand["_decade_str"] = decade_str
    cand["_score_points"] = score_points


def build_rationale(
    cand: Dict[str, Any],
    genre_filter: Optional[str],
    favored_decades_map: Dict[str, int],
) -> Tuple[str, Dict[str, Any]]:
    """Build the human-readable rationale string and structured decision_factors dict.

    Reads and clears the temporary `_*` fields set by `score_candidate`.
    """
    seed_title = cand.get("_seed_title")
    overlap_details = cand.get("_overlap_details", [])
    keyword_overlap = cand.get("_keyword_overlap", [])
    is_favored_decade = cand.get("_is_favored_decade", False)
    decade_str = cand.get("_decade_str")
    score_points = cand.get("_score_points", [])
    rating = cand.get("rating", 0)
    year = cand.get("year")

    reasons = []
    if seed_title:
        reasons.append(f"Because you watched {seed_title}")
    if genre_filter:
        reasons.append(f"Matches category {to_canonical_genre(genre_filter)}")
    elif overlap_details:
        reasons.append(f"Matches top genre{'s' if len(overlap_details) > 1 else ''}: {', '.join([d['name'] for d in overlap_details[:2]])}")
    if keyword_overlap:
        reasons.append(f"Shared themes: {', '.join([d['name'] for d in keyword_overlap[:2]])}")
    if rating:
        reasons.append(f"Rating {rating}/10 ({cand.get('vote_count', 0):,} votes)")
    if year:
        reasons.append(f"Released {year}")

    rationale = " • ".join(reasons)
    decision_factors = {
        "matched_genres": overlap_details,
        "matched_keywords": keyword_overlap,
        "matched_category": genre_filter if genre_filter else None,
        "seed_title": seed_title,
        "is_favored_decade": is_favored_decade,
        "decade_label": decade_str,
        "decade_watched": favored_decades_map.get(decade_str, 0) if decade_str else 0,
        "rating": rating,
        "vote_count": cand.get("vote_count", 0),
        "language": cand.get("language_name", "English"),
        "score_points": score_points
    }

    for k in ("_seed_title", "_overlap_details", "_keyword_overlap", "_is_favored_decade", "_decade_str", "_score_points"):
        cand.pop(k, None)

    return rationale, decision_factors


class ContentRecommender:
    def __init__(self, tmdb_client: Optional[TMDbClient] = None):
        self.tmdb = tmdb_client or TMDbClient()

    def _get_external_ids_safe(self, items: List[Tuple[int, str]]) -> Dict[str, Dict[str, Optional[str]]]:
        """Fetch external IDs with fallback for mocks or standard TMDbClient."""
        if hasattr(self.tmdb, "batch_get_external_ids"):
            try:
                res = self.tmdb.batch_get_external_ids(items)
                if type(res) is dict:
                    return res
            except Exception as e:
                logger.debug(f"Batch external ID fetch failed, falling back to sequential: {e}")

        res = {}
        for tid, mtype in items:
            try:
                res[str(tid)] = self.tmdb.get_external_ids(tid, media_type=mtype)
            except Exception:
                res[str(tid)] = {"imdb_id": None, "tvdb_id": None}
        return res

    def _enrich_candidate(
        self,
        cand: Dict[str, Any],
        *,
        ext_ids_map: Dict[str, Dict[str, Optional[str]]],
        seen_imdb: set,
        seen_tvdb: set,
        overseerr_queue: Dict[str, Any],
        availability_index: Dict[str, Any],
        machine_id: Optional[str],
        only_available: bool,
        genre_filter: Optional[str],
        favored_decades_map: Dict[str, int],
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Attach external IDs, URLs, Overseerr/Plex availability, and rationale to a candidate.

        Returns (candidate_or_None, was_seen_match). `was_seen_match` is True
        only when dropped via a secondary seen-match on external ID (counts
        toward `seen_filtered_count`); an `only_available` exclusion returns
        (None, False) and is not counted, matching prior behavior.
        """
        tmdb_str = cand["tmdb_id"]
        ext_ids = ext_ids_map.get(tmdb_str, {"imdb_id": None, "tvdb_id": None})
        imdb_id = ext_ids.get("imdb_id")
        tvdb_id = ext_ids.get("tvdb_id")

        # Secondary seen checks using external IDs
        if imdb_id and imdb_id.lower() in seen_imdb:
            return None, True
        if tvdb_id and str(tvdb_id).lower() in seen_tvdb:
            return None, True

        cand["imdb_id"] = imdb_id
        cand["tvdb_id"] = tvdb_id
        cand["imdb_url"] = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None

        # Generate TMDb web URL safely (handling mocks in tests)
        tmdb_url = None
        if hasattr(self.tmdb, "get_web_url"):
            try:
                res_url = self.tmdb.get_web_url(tmdb_str, media_type=cand["media_type"])
                if isinstance(res_url, str):
                    tmdb_url = res_url
            except Exception:
                pass
        if not tmdb_url:
            mtype = "tv" if str(cand["media_type"]).lower() in ("show", "tv", "episode") else "movie"
            tmdb_url = f"https://www.themoviedb.org/{mtype}/{tmdb_str}"
        cand["tmdb_url"] = tmdb_url

        cand["overseerr_url"] = overseerr.get_web_url(int(tmdb_str), media_type=cand["media_type"])

        # Check Overseerr server-wide queue status (prevents duplicate requests)
        cand_key = f"{cand['media_type']}_{tmdb_str}"
        q_info = overseerr_queue.get(cand_key)
        if q_info and q_info.get("status_code") in (2, 3, 4):
            cand["overseerr_requested"] = True
            cand["overseerr_status"] = q_info.get("status_name", "PENDING")
            cand["overseerr_status_code"] = q_info.get("status_code", 2)
        else:
            cand["overseerr_requested"] = False
            cand["overseerr_status"] = None
            cand["overseerr_status_code"] = None

        # Check Plex library availability
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

        if only_available and not cand["available_on_plex"]:
            return None, False

        rationale, decision_factors = build_rationale(cand, genre_filter, favored_decades_map)
        cand["rationale"] = rationale
        cand["decision_factors"] = decision_factors

        return cand, False

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
        force_refresh: bool = False,
        stage: str = "full"              # "fast" (top 6 quick discover) or "full" (complete pool)
    ) -> Dict[str, Any]:
        """
        Discover unseen movies and shows matching a user's taste profile.
        Supports progressive 2-stage execution: 'fast' (quick discovery) and 'full' (deep seeds).
        """
        profile = analyzer.analyze(user_key)
        if not profile.get("has_data"):
            return {
                "success": False,
                "is_complete": True,
                "stage": stage,
                "error": "No watch history found. Recommendations require watch history for your account.",
                "recommendations": []
            }

        target_language = language if language is not None else "en"
        page = max(1, min(int(page or 1), MAX_PAGES))

        # Check cache tied to sync freshness, scoped per user
        current_sync = get_setting("last_sync_time")
        pool_cache_key = f"{user_key}:{media_type}:{genre_filter or ''}:{target_language}:{min_rating}:{int(only_available)}:{int(include_kids)}"
        legacy_cache_key = f"{user_key}:{media_type}:{genre_filter or ''}:{target_language}:{min_rating}:{limit}:{page}:{int(only_available)}:{int(include_kids)}"

        if not force_refresh:
            # Check pool cache first (supports instant pagination)
            cached_pool = get_cached_recommendations(pool_cache_key, current_sync)
            if cached_pool and cached_pool.get("all_recommendations"):
                logger.info(f"Serving recommendations from pool cache for key='{pool_cache_key}' (sync: {current_sync})")
                all_recs = cached_pool["all_recommendations"]
                unseen_count = len(all_recs)
                total_pages = min(MAX_PAGES, max(1, (unseen_count + limit - 1) // limit)) if limit > 0 else 1
                cur_page = min(page, total_pages)
                start = (cur_page - 1) * limit
                page_items = all_recs[start:start + limit]
                if stage == "fast":
                    page_items = page_items[:min(6, limit)]
                return {
                    "success": True,
                    "is_complete": True,
                    "stage": stage,
                    "total_candidates_scanned": cached_pool.get("total_candidates_scanned", unseen_count),
                    "unseen_count": unseen_count,
                    "recommendations": page_items,
                    "page": cur_page,
                    "total_pages": total_pages,
                    "limit": limit,
                    "query_genres": cached_pool.get("query_genres", [genre_filter] if genre_filter else []),
                    "from_cache": True,
                    "sync_version": current_sync,
                    "exhaustion_reason": cached_pool.get("exhaustion_reason", "exhausted" if unseen_count == 0 else None)
                }

            # Check legacy cache key fallback
            cached_legacy = get_cached_recommendations(legacy_cache_key, current_sync)
            if cached_legacy:
                logger.info(f"Serving recommendations from legacy cache for key='{legacy_cache_key}' (sync: {current_sync})")
                cached_legacy["is_complete"] = True
                cached_legacy["stage"] = stage
                return cached_legacy

        seen_index = get_user_seen_index(user_key)
        seen_imdb = seen_index["imdb"]
        seen_tmdb = seen_index["tmdb"]
        seen_tvdb = seen_index["tvdb"]
        seen_titles = seen_index["title_year"]

        availability_index = get_library_availability_index()
        machine_id = settings.plex_machine_id

        user_votes = get_user_votes(user_key)
        upvoted_items = get_user_vote_items(user_key, vote=1)
        upvoted_ids = {str(uv["tmdb_id"]) for uv in upvoted_items}
        upvoted_titles = {str(uv.get("title")) for uv in upvoted_items if uv.get("title")}

        top_genres = profile.get("top_genres", [])
        if not top_genres:
            return {
                "success": False,
                "is_complete": True,
                "stage": stage,
                "error": "Not enough genre data in watch history to recommend content.",
                "recommendations": []
            }

        genre_affinity_map = {g["genre"].lower(): g for g in top_genres}
        selected_genres = [genre_filter] if genre_filter else [g["genre"] for g in top_genres[:4]]
        favored_decades_map = {d["decade"]: d["count"] for d in profile.get("decades", [])}

        top_keywords = profile.get("top_keywords", [])
        keyword_affinity_map = {k["keyword"].lower(): k for k in top_keywords}
        # Avoid biasing explicit genre discovery with conflicting global keywords
        query_keyword_ids = [k["id"] for k in top_keywords[:5] if k.get("id")] if not genre_filter else None

        types_to_query = []
        if media_type in ("movie", "all"):
            types_to_query.append("movie")
        if media_type in ("show", "all"):
            types_to_query.append("show")

        lang_code = None if target_language == "all" else target_language

        # Stage 1: Parallel Discover Queries
        discover_tasks = build_discover_tasks(
            self.tmdb, types_to_query, selected_genres, genre_filter, lang_code, query_keyword_ids, min_rating
        )
        candidates, raw_candidates_seen_ids = run_discover_stage(self.tmdb, discover_tasks, lang_code)

        # Stage 2: Genre-Filtered Seed Expansion (only in 'full' stage)
        seed_origin: Dict[str, str] = {}
        if stage == "full":
            try:
                seen_items = get_user_media_items(user_key)
                seeds = build_seed_list(user_key, media_type, genre_filter, upvoted_items, seen_items)
                top_seeds = seeds[:6]
                seed_origin = run_seed_expansion_stage(
                    self.tmdb, top_seeds, candidates, raw_candidates_seen_ids, lang_code
                )
            except Exception as e:
                logger.error(f"Error expanding seed-based recommendations: {e}")

        # In-Memory Deduplication, Genre Enforcement & Pre-Scoring
        scored_candidates = []
        seen_filtered_count = 0
        for cand in candidates:
            if not passes_seen_filters(cand, seen_tmdb, seen_titles):
                seen_filtered_count += 1
                continue

            if not passes_pool_filters(cand, genre_filter, include_kids, min_rating):
                continue

            score_candidate(
                cand,
                top_genres=top_genres,
                genre_affinity_map=genre_affinity_map,
                keyword_affinity_map=keyword_affinity_map,
                favored_decades_map=favored_decades_map,
                upvoted_ids=upvoted_ids,
                upvoted_titles=upvoted_titles,
                seed_origin=seed_origin,
                genre_filter=genre_filter,
            )
            scored_candidates.append(cand)

        # Sort candidate pool by match score descending, then rating descending
        scored_candidates.sort(key=lambda x: (x["match_score"], x["rating"]), reverse=True)

        # Determine how many items to enrich:
        # In 'fast' stage, enrich top 6 display candidates.
        # In 'full' stage, enrich up to MAX_PAGES * limit (top 36) for the full candidate pool.
        enrich_count = min(6, limit) if stage == "fast" else min(MAX_PAGES * limit, len(scored_candidates))
        candidates_to_enrich = scored_candidates[:enrich_count]

        # Batch fetch external IDs concurrently
        items_for_ext = [(int(c["tmdb_id"]), c["media_type"]) for c in candidates_to_enrich]
        ext_ids_map = self._get_external_ids_safe(items_for_ext)

        # Check Overseerr active request queue (server-wide)
        overseerr_queue = {}
        if overseerr.is_configured():
            try:
                overseerr_queue = overseerr.get_request_queue()
            except Exception as e:
                logger.debug(f"Failed to load Overseerr queue for recommendations: {e}")

        enriched_recommendations = []
        for cand in candidates_to_enrich:
            result, was_seen_match = self._enrich_candidate(
                cand,
                ext_ids_map=ext_ids_map,
                seen_imdb=seen_imdb,
                seen_tvdb=seen_tvdb,
                overseerr_queue=overseerr_queue,
                availability_index=availability_index,
                machine_id=machine_id,
                only_available=only_available,
                genre_filter=genre_filter,
                favored_decades_map=favored_decades_map,
            )
            if result is None:
                if was_seen_match:
                    seen_filtered_count += 1
                continue
            enriched_recommendations.append(result)

        unseen_count = len(enriched_recommendations)
        total_pages = min(MAX_PAGES, max(1, (unseen_count + limit - 1) // limit)) if limit > 0 else 1
        page = min(page, total_pages)
        start = (page - 1) * limit
        page_items = enriched_recommendations[start:start + limit]

        exhaustion_reason = None
        if unseen_count == 0:
            if not candidates:
                exhaustion_reason = "no_results"
            elif seen_filtered_count > 0:
                exhaustion_reason = "exhausted"
            else:
                exhaustion_reason = "no_results"

        is_complete = (stage == "full")
        result = {
            "success": True,
            "is_complete": is_complete,
            "stage": stage,
            "total_candidates_scanned": len(candidates),
            "unseen_count": unseen_count,
            "recommendations": page_items,
            "page": page,
            "total_pages": total_pages,
            "limit": limit,
            "query_genres": selected_genres,
            "from_cache": False,
            "sync_version": current_sync,
            "exhaustion_reason": exhaustion_reason
        }

        # Cache complete results
        if is_complete:
            try:
                # Save full candidate pool for instant pagination
                pool_data = {
                    "all_recommendations": enriched_recommendations,
                    "total_candidates_scanned": len(candidates),
                    "query_genres": selected_genres,
                    "exhaustion_reason": exhaustion_reason
                }
                set_cached_recommendations(pool_cache_key, current_sync, pool_data)
                # Also save legacy format for exact key hits
                set_cached_recommendations(legacy_cache_key, current_sync, result)
            except Exception as e:
                logger.error(f"Failed to cache recommendations: {e}")

        return result


recommender = ContentRecommender()
