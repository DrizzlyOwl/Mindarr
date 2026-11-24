import logging
import random
import concurrent.futures
from collections import defaultdict
from typing import Optional, Dict, Any, List, Tuple, Set
from plex_recommender.analyzer import analyzer
from plex_recommender.db.watch import get_user_seen_index, get_user_media_items
from plex_recommender.db._util import normalize_title
from plex_recommender.db.recommendations import get_cached_recommendations, set_cached_recommendations
from plex_recommender.db.media import get_library_availability_index
from plex_recommender.db.recommendations import get_setting
from plex_recommender.db.engagement import get_user_vote_items, get_user_watchlist
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


def blend_taste_profiles(profiles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Combine multiple users' TasteAnalyzer profiles into a consensus group profile.

    Genres and themes shared across all participants receive a consensus multiplier
    so titles everyone can enjoy float above niche single-user preferences.
    """
    active = [p for p in profiles if p and p.get("has_data")]
    n = len(active)
    if n == 0:
        return {"has_data": False}

    genre_sum: Dict[str, float] = defaultdict(float)
    genre_count_sum: Dict[str, int] = defaultdict(int)
    genre_user_hits: Dict[str, int] = defaultdict(int)
    genre_display: Dict[str, str] = {}

    kw_sum: Dict[str, float] = defaultdict(float)
    kw_count_sum: Dict[str, int] = defaultdict(int)
    kw_user_hits: Dict[str, int] = defaultdict(int)
    kw_ids: Dict[str, Any] = {}
    kw_display: Dict[str, str] = {}

    decade_counts: Dict[str, int] = defaultdict(int)

    for prof in active:
        seen_g = set()
        for g in prof.get("top_genres", []):
            raw_name = g.get("genre") or ""
            canon = to_canonical_genre(raw_name) or raw_name
            low = canon.lower()
            if not low or low in seen_g:
                continue
            seen_g.add(low)
            genre_display[low] = canon
            genre_sum[low] += float(g.get("score") or 0)
            genre_count_sum[low] += int(g.get("count") or 0)
            genre_user_hits[low] += 1

        seen_k = set()
        for k in prof.get("top_keywords", []):
            name = (k.get("keyword") or "").strip()
            low = name.lower()
            if not low or low in seen_k:
                continue
            seen_k.add(low)
            kw_display[low] = name
            kw_sum[low] += float(k.get("score") or 0)
            kw_count_sum[low] += int(k.get("count") or 0)
            kw_user_hits[low] += 1
            if k.get("id") is not None and low not in kw_ids:
                kw_ids[low] = k["id"]

        for dec in prof.get("decades", []):
            if dec.get("decade"):
                decade_counts[dec["decade"]] += int(dec.get("count") or 0)

    blended_genre_raw: Dict[str, float] = {}
    for low, total in genre_sum.items():
        avg = total / n
        shared_ratio = genre_user_hits[low] / n
        blended_genre_raw[low] = avg * (0.5 + 0.5 * shared_ratio)

    max_g = max(blended_genre_raw.values()) if blended_genre_raw else 1.0
    top_genres = [
        {
            "genre": genre_display[low],
            "score": round((val / max_g) * 100, 1),
            "count": genre_count_sum[low],
            "shared_by_all": genre_user_hits[low] == n,
        }
        for low, val in sorted(blended_genre_raw.items(), key=lambda x: x[1], reverse=True)
    ]

    blended_kw_raw: Dict[str, float] = {}
    for low, total in kw_sum.items():
        avg = total / n
        shared_ratio = kw_user_hits[low] / n
        blended_kw_raw[low] = avg * (0.5 + 0.5 * shared_ratio)

    max_k = max(blended_kw_raw.values()) if blended_kw_raw else 1.0
    top_keywords = [
        {
            "keyword": kw_display[low],
            "id": kw_ids.get(low),
            "score": round((val / max_k) * 100, 1),
            "count": kw_count_sum[low],
        }
        for low, val in sorted(blended_kw_raw.items(), key=lambda x: x[1], reverse=True)[:20]
    ]

    decades = [
        {"decade": dec, "count": decade_counts[dec]}
        for dec in sorted(decade_counts.keys())
    ]

    return {
        "has_data": True,
        "group_size": n,
        "top_genres": top_genres[:15],
        "shared_genres": [g["genre"] for g in top_genres if g.get("shared_by_all")][:5],
        "top_keywords": top_keywords,
        "decades": decades,
    }


TONIGHT_MOODS: Dict[str, Dict[str, Any]] = {
    "best": {"label": "Best Match", "min_rating": 7.0},
    "hidden_gems": {"label": "Hidden Gems", "min_rating": 7.4, "max_votes": 1800, "min_votes": 80},
    "acclaimed": {"label": "Critically Acclaimed", "min_rating": 8.0},
    "quick_watch": {"label": "Quick Watch (<105m)", "min_rating": 7.0, "with_runtime_lte": 105, "media_type": "movie"},
    "comfort": {"label": "Comfort & Light", "min_rating": 6.8, "genres": ["Comedy", "Animation", "Family", "Romance"]},
    "mind_bending": {"label": "Mind-Bending", "min_rating": 7.2, "genres": ["Sci-Fi", "Mystery", "Thriller"]},
}


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

    def get_group_recommendations(
        self,
        user_keys: List[str],
        media_type: str = "all",
        limit: int = 18,
        page: int = 1,
        min_rating: float = 7.0,
        genre_filter: Optional[str] = None,
        language: Optional[str] = "en",
        only_available: bool = False,
        include_kids: bool = False,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """Discover titles unseen by ALL selected users, ranked by blended group taste."""
        unique_keys: List[str] = []
        for uk in user_keys:
            s = str(uk).strip()
            if s and s not in unique_keys:
                unique_keys.append(s)

        if not unique_keys:
            return {
                "success": False,
                "is_complete": True,
                "error": "Select at least one viewer for Watch Together.",
                "recommendations": [],
            }

        profiles = [analyzer.analyze(uk) for uk in unique_keys]
        blended = blend_taste_profiles(profiles)
        if not blended.get("has_data"):
            return {
                "success": False,
                "is_complete": True,
                "error": "No watch history available for the selected viewers yet.",
                "recommendations": [],
            }

        target_language = language if language is not None else "en"
        page = max(1, min(int(page or 1), MAX_PAGES))
        sorted_group = ",".join(sorted(unique_keys))
        current_sync = get_setting("last_sync_time")
        pool_cache_key = (
            f"group:{sorted_group}:{media_type}:{genre_filter or ''}:"
            f"{target_language}:{min_rating}:{int(only_available)}:{int(include_kids)}"
        )

        if not force_refresh:
            cached_pool = get_cached_recommendations(pool_cache_key, current_sync)
            if cached_pool and cached_pool.get("all_recommendations") is not None:
                all_recs = cached_pool["all_recommendations"]
                unseen_count = len(all_recs)
                total_pages = min(MAX_PAGES, max(1, (unseen_count + limit - 1) // limit)) if limit > 0 else 1
                cur_page = min(page, total_pages)
                start = (cur_page - 1) * limit
                return {
                    "success": True,
                    "is_complete": True,
                    "stage": "full",
                    "group_size": len(unique_keys),
                    "shared_genres": blended.get("shared_genres", []),
                    "total_candidates_scanned": cached_pool.get("total_candidates_scanned", unseen_count),
                    "unseen_count": unseen_count,
                    "recommendations": all_recs[start:start + limit],
                    "page": cur_page,
                    "total_pages": total_pages,
                    "limit": limit,
                    "query_genres": cached_pool.get("query_genres", []),
                    "from_cache": True,
                    "sync_version": current_sync,
                    "exhaustion_reason": cached_pool.get("exhaustion_reason", "exhausted" if unseen_count == 0 else None),
                }

        # Union of seen indexes across all participants so nobody has watched any pick
        seen_imdb: Set[str] = set()
        seen_tmdb: Set[str] = set()
        seen_tvdb: Set[str] = set()
        seen_titles: Set[str] = set()
        upvoted_items: List[Dict[str, Any]] = []
        upvoted_ids: Set[str] = set()
        upvoted_titles: Set[str] = set()

        for uk in unique_keys:
            idx = get_user_seen_index(uk)
            seen_imdb |= idx["imdb"]
            seen_tmdb |= idx["tmdb"]
            seen_tvdb |= idx["tvdb"]
            seen_titles |= idx["title_year"]
            uvs = get_user_vote_items(uk, vote=1)
            upvoted_items.extend(uvs)
            upvoted_ids |= {str(uv["tmdb_id"]) for uv in uvs}
            upvoted_titles |= {str(uv.get("title")) for uv in uvs if uv.get("title")}

        top_genres = blended.get("top_genres", [])
        if not top_genres:
            return {
                "success": False,
                "is_complete": True,
                "error": "Not enough genre data across the group to recommend content.",
                "recommendations": [],
            }

        genre_affinity_map = {g["genre"].lower(): g for g in top_genres}
        selected_genres = [genre_filter] if genre_filter else [g["genre"] for g in top_genres[:4]]
        favored_decades_map = {d["decade"]: d["count"] for d in blended.get("decades", [])}

        top_keywords = blended.get("top_keywords", [])
        keyword_affinity_map = {k["keyword"].lower(): k for k in top_keywords}
        query_keyword_ids = [k["id"] for k in top_keywords[:5] if k.get("id")] if not genre_filter else None

        types_to_query = []
        if media_type in ("movie", "all"):
            types_to_query.append("movie")
        if media_type in ("show", "all"):
            types_to_query.append("show")

        lang_code = None if target_language == "all" else target_language
        discover_tasks = build_discover_tasks(
            self.tmdb, types_to_query, selected_genres, genre_filter, lang_code, query_keyword_ids, min_rating
        )
        candidates, raw_candidates_seen_ids = run_discover_stage(self.tmdb, discover_tasks, lang_code)

        # Round-robin top seeds from each participant
        seed_origin: Dict[str, str] = {}
        try:
            per_user_seeds: List[List[Tuple[float, int, Any, str, str]]] = []
            for uk in unique_keys:
                u_uvs = get_user_vote_items(uk, vote=1)
                u_seen = get_user_media_items(uk)
                per_user_seeds.append(build_seed_list(uk, media_type, genre_filter, u_uvs, u_seen))

            mixed_seeds: List[Tuple[float, int, Any, str, str]] = []
            seen_seed_ids: Set[Tuple[str, Any]] = set()
            for idx_pos in range(6):
                for s_list in per_user_seeds:
                    if idx_pos < len(s_list):
                        s = s_list[idx_pos]
                        sk = (s[3], s[2])
                        if sk not in seen_seed_ids:
                            seen_seed_ids.add(sk)
                            mixed_seeds.append(s)
                    if len(mixed_seeds) >= 6:
                        break
                if len(mixed_seeds) >= 6:
                    break

            seed_origin = run_seed_expansion_stage(
                self.tmdb, mixed_seeds[:6], candidates, raw_candidates_seen_ids, lang_code
            )
        except Exception as e:
            logger.error(f"Error expanding group seed recommendations: {e}")

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

        scored_candidates.sort(key=lambda x: (x["match_score"], x["rating"]), reverse=True)
        candidates_to_enrich = scored_candidates[:min(MAX_PAGES * limit, len(scored_candidates))]

        items_for_ext = [(int(c["tmdb_id"]), c["media_type"]) for c in candidates_to_enrich]
        ext_ids_map = self._get_external_ids_safe(items_for_ext)

        availability_index = get_library_availability_index()
        machine_id = settings.plex_machine_id
        overseerr_queue = {}
        if overseerr.is_configured():
            try:
                overseerr_queue = overseerr.get_request_queue()
            except Exception as e:
                logger.debug(f"Failed to load Overseerr queue for group recs: {e}")

        group_count = len(unique_keys)
        shared_genres = blended.get("shared_genres", [])
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
            prefix = f"Unseen by all {group_count} viewers"
            result["rationale"] = f"{prefix} • {result['rationale']}" if result.get("rationale") else prefix
            if isinstance(result.get("decision_factors"), dict):
                result["decision_factors"]["group_size"] = group_count
                result["decision_factors"]["shared_genres"] = shared_genres
            enriched_recommendations.append(result)

        unseen_count = len(enriched_recommendations)
        total_pages = min(MAX_PAGES, max(1, (unseen_count + limit - 1) // limit)) if limit > 0 else 1
        page = min(page, total_pages)
        start = (page - 1) * limit
        page_items = enriched_recommendations[start:start + limit]

        exhaustion_reason = None
        if unseen_count == 0:
            exhaustion_reason = "exhausted" if seen_filtered_count > 0 else "no_results"

        try:
            set_cached_recommendations(
                pool_cache_key,
                current_sync,
                {
                    "all_recommendations": enriched_recommendations,
                    "total_candidates_scanned": len(candidates),
                    "query_genres": selected_genres,
                    "exhaustion_reason": exhaustion_reason,
                },
            )
        except Exception as e:
            logger.error(f"Failed to cache group recommendations: {e}")

        return {
            "success": True,
            "is_complete": True,
            "stage": "full",
            "group_size": group_count,
            "shared_genres": shared_genres,
            "total_candidates_scanned": len(candidates),
            "unseen_count": unseen_count,
            "recommendations": page_items,
            "page": page,
            "total_pages": total_pages,
            "limit": limit,
            "query_genres": selected_genres,
            "from_cache": False,
            "sync_version": current_sync,
            "exhaustion_reason": exhaustion_reason,
        }

    def get_tonight_picks(
        self,
        user_key: str,
        *,
        source: str = "discover",
        group_user_keys: Optional[List[str]] = None,
        media_type: str = "all",
        mood: str = "best",
        only_available: bool = False,
        exclude_ids: Optional[Set[str]] = None,
        count: int = 3,
    ) -> Dict[str, Any]:
        """Curate 1-3 finalists for tonight based on source (discover/together/watchlist) and mood."""
        mood_cfg = TONIGHT_MOODS.get(mood, TONIGHT_MOODS["best"])
        eff_type = mood_cfg.get("media_type") or (media_type if media_type in ("movie", "show") else "all")
        min_rating = float(mood_cfg.get("min_rating", 7.0))
        exclude_set = {str(x).strip() for x in (exclude_ids or set()) if str(x).strip()}

        pool: List[Dict[str, Any]] = []

        if source == "watchlist":
            saved = get_user_watchlist(user_key)
            if not saved:
                return {"success": True, "picks": [], "total_pool": 0, "mood": mood, "source": source, "empty_reason": "empty_watchlist"}

            avail_idx = get_library_availability_index()
            machine_id = settings.plex_machine_id
            overseerr_queue = overseerr.get_request_queue() if overseerr.is_configured() else {}
            mood_genres = mood_cfg.get("genres")

            for raw in saved:
                tid = str(raw["tmdb_id"])
                if tid in exclude_set:
                    continue
                mtype = "show" if str(raw.get("media_type")).lower() in ("show", "tv", "episode") else "movie"
                if eff_type != "all" and mtype != eff_type:
                    continue
                if (raw.get("rating") or 0) and float(raw.get("rating") or 0) < min_rating:
                    continue
                if mood_genres:
                    it_genres = raw.get("genres") or []
                    if it_genres and not any(
                        any(genres_align(g, mg) for mg in mood_genres)
                        for g in it_genres
                    ):
                        continue

                avail_info = avail_idx["tmdb"].get(tid.lower())
                available_on_plex = avail_info is not None
                if only_available and not available_on_plex:
                    continue

                plex_url = None
                if avail_info and machine_id and avail_info.get("rating_key"):
                    plex_url = (
                        f"https://app.plex.tv/desktop/#!/server/{machine_id}"
                        f"/details?key=/library/metadata/{avail_info['rating_key']}"
                    )
                q_info = overseerr_queue.get(f"{mtype}_{tid}")
                overseerr_requested = bool(q_info and q_info.get("status_code") in (2, 3, 4))

                pool.append({
                    "tmdb_id": tid,
                    "media_type": mtype,
                    "title": raw.get("title") or f"TMDb {tid}",
                    "year": raw.get("year"),
                    "rating": raw.get("rating") or 0,
                    "genres": raw.get("genres") or [],
                    "summary": raw.get("summary") or "",
                    "poster_url": raw.get("poster_url"),
                    "available_on_plex": available_on_plex,
                    "plex_url": plex_url,
                    "overseerr_requested": overseerr_requested,
                    "overseerr_status": q_info.get("status_name") if overseerr_requested else None,
                    "overseerr_url": overseerr.get_web_url(int(tid), media_type=mtype) if (overseerr.is_configured() and tid.isdigit()) else None,
                    "tmdb_url": self.tmdb.get_web_url(tid, media_type=mtype),
                    "imdb_url": None,
                    "match_score": None,
                    "rationale": f"From your Watchlist • {mood_cfg['label']}",
                })
        elif mood in ("quick_watch", "hidden_gems"):
            # Targeted TMDb discovery for runtime-capped or hidden-gem queries
            target_keys = list(group_user_keys or [user_key]) if source == "together" else [user_key]
            profiles = [analyzer.analyze(uk) for uk in target_keys]
            blended = blend_taste_profiles(profiles) if len(target_keys) > 1 else (profiles[0] if profiles else {"has_data": False})
            if not blended.get("has_data"):
                return {"success": False, "error": "No watch history available yet.", "picks": [], "total_pool": 0}

            seen_tmdb: Set[str] = set()
            seen_imdb: Set[str] = set()
            seen_tvdb: Set[str] = set()
            seen_titles: Set[str] = set()
            for uk in target_keys:
                idx = get_user_seen_index(uk)
                seen_tmdb |= idx["tmdb"]
                seen_imdb |= idx["imdb"]
                seen_tvdb |= idx["tvdb"]
                seen_titles |= idx["title_year"]

            top_genres = blended.get("top_genres", [])
            genre_affinity_map = {g["genre"].lower(): g for g in top_genres}
            keyword_affinity_map = {k["keyword"].lower(): k for k in blended.get("top_keywords", [])}
            favored_decades_map = {d["decade"]: d["count"] for d in blended.get("decades", [])}
            selected_genres = [g["genre"] for g in top_genres[:3]]

            candidates: List[Dict[str, Any]] = []
            seen_cand_ids: Set[str] = set()
            types = ["movie"] if eff_type == "movie" else (["show"] if eff_type == "show" else ["movie", "show"])
            for mt in types:
                g_ids = self.tmdb.map_genres(selected_genres, media_type=mt)
                try:
                    if mt == "movie":
                        raw_list = self.tmdb.discover_movies(
                            with_genres=g_ids[:2] if g_ids else None,
                            with_original_language="en",
                            min_rating=min_rating,
                            min_votes=int(mood_cfg.get("min_votes", 200)),
                            max_votes=mood_cfg.get("max_votes"),
                            with_runtime_lte=mood_cfg.get("with_runtime_lte"),
                            sort_by="vote_average.desc",
                        )
                    else:
                        raw_list = self.tmdb.discover_tv(
                            with_genres=g_ids[:2] if g_ids else None,
                            with_original_language="en",
                            min_rating=min_rating,
                            min_votes=int(mood_cfg.get("min_votes", 150)),
                            sort_by="vote_average.desc",
                        )
                except Exception as e:
                    logger.debug(f"Tonight targeted query error ({mt}): {e}")
                    raw_list = []

                for r in raw_list:
                    cid = f"{mt}_{r['id']}"
                    if cid in seen_cand_ids:
                        continue
                    seen_cand_ids.add(cid)
                    cand = self.tmdb.format_item(r, media_type=mt)
                    if cand.get("original_language", "en") != "en":
                        continue
                    if mood == "hidden_gems" and (cand.get("vote_count") or 0) > 2500:
                        continue
                    if str(cand["tmdb_id"]) in exclude_set:
                        continue
                    if not passes_seen_filters(cand, seen_tmdb, seen_titles):
                        continue
                    if not passes_pool_filters(cand, None, False, min_rating):
                        continue
                    score_candidate(
                        cand,
                        top_genres=top_genres,
                        genre_affinity_map=genre_affinity_map,
                        keyword_affinity_map=keyword_affinity_map,
                        favored_decades_map=favored_decades_map,
                        upvoted_ids=set(),
                        upvoted_titles=set(),
                        seed_origin={},
                        genre_filter=None,
                    )
                    candidates.append(cand)

            candidates.sort(key=lambda x: (x["match_score"], x["rating"]), reverse=True)
            top_cands = candidates[:18]
            ext_ids_map = self._get_external_ids_safe([(int(c["tmdb_id"]), c["media_type"]) for c in top_cands])
            avail_idx = get_library_availability_index()
            machine_id = settings.plex_machine_id
            ov_queue = overseerr.get_request_queue() if overseerr.is_configured() else {}

            for cand in top_cands:
                res, _ = self._enrich_candidate(
                    cand,
                    ext_ids_map=ext_ids_map,
                    seen_imdb=seen_imdb,
                    seen_tvdb=seen_tvdb,
                    overseerr_queue=ov_queue,
                    availability_index=avail_idx,
                    machine_id=machine_id,
                    only_available=only_available,
                    genre_filter=None,
                    favored_decades_map=favored_decades_map,
                )
                if res is not None:
                    res["rationale"] = f"{mood_cfg['label']} • {res['rationale']}"
                    pool.append(res)
        else:
            # Standard moods ("best", "acclaimed", "comfort", "mind_bending")
            mood_genres = mood_cfg.get("genres")
            genre_arg = None
            if mood_genres:
                # Pick a matching top genre for the user/group if possible, else first mood genre
                genre_arg = mood_genres[0]
                if source != "together":
                    prof = analyzer.analyze(user_key)
                    for ug in prof.get("top_genres", []):
                        if any(genres_align(ug.get("genre"), mg) for mg in mood_genres):
                            genre_arg = to_canonical_genre(ug["genre"])
                            break

            if source == "together" and group_user_keys:
                res = self.get_group_recommendations(
                    user_keys=group_user_keys,
                    media_type=eff_type,
                    limit=36,
                    page=1,
                    min_rating=min_rating,
                    genre_filter=genre_arg,
                    only_available=only_available,
                )
            else:
                res = self.get_recommendations(
                    user_key=user_key,
                    media_type=eff_type,
                    limit=36,
                    page=1,
                    min_rating=min_rating,
                    genre_filter=genre_arg,
                    only_available=only_available,
                    stage="full",
                )

            if not res.get("success") and res.get("error"):
                return {"success": False, "error": res["error"], "picks": [], "total_pool": 0}

            for item in res.get("recommendations", []):
                if str(item.get("tmdb_id")) in exclude_set:
                    continue
                pool.append(item)

        if not pool:
            return {
                "success": True,
                "picks": [],
                "total_pool": 0,
                "mood": mood,
                "source": source,
                "empty_reason": "no_matches",
            }

        # Pick finalists from top slice with gentle randomization so Re-roll feels fresh
        top_slice = pool[:min(12, len(pool))]
        if len(top_slice) <= count:
            picks = list(top_slice)
        else:
            # Keep the #1 top match or sample cleanly across the top slice
            picks = random.sample(top_slice, count)
            picks.sort(key=lambda x: ((x.get("match_score") or 0), (x.get("rating") or 0)), reverse=True)

        return {
            "success": True,
            "picks": picks,
            "total_pool": len(pool),
            "mood": mood,
            "mood_label": mood_cfg["label"],
            "source": source,
        }


recommender = ContentRecommender()
