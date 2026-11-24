import logging
import time
import threading
import requests
from requests.adapters import HTTPAdapter
import concurrent.futures
from typing import Optional, Dict, Any, List, Tuple, Set
from plex_recommender.config import settings

logger = logging.getLogger(__name__)

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

MOVIE_GENRES = {
    "action": 28,
    "action & adventure": 28,
    "adventure": 12,
    "animation": 16,
    "anime": 16,
    "comedy": 35,
    "crime": 80,
    "documentary": 99,
    "drama": 18,
    "family": 10751,
    "kids": 10751,
    "fantasy": 14,
    "history": 36,
    "horror": 27,
    "music": 10402,
    "mystery": 9648,
    "romance": 10749,
    "sci-fi": 878,
    "science fiction": 878,
    "sci-fi & fantasy": 878,
    "tv movie": 10770,
    "thriller": 53,
    "suspense": 53,
    "war": 10752,
    "western": 37
}

TV_GENRES = {
    "action": 10759,
    "action & adventure": 10759,
    "adventure": 10759,
    "animation": 16,
    "anime": 16,
    "comedy": 35,
    "crime": 80,
    "documentary": 99,
    "drama": 18,
    "family": 10751,
    "kids": 10762,
    "mystery": 9648,
    "news": 10763,
    "reality": 10764,
    "sci-fi": 10765,
    "science fiction": 10765,
    "sci-fi & fantasy": 10765,
    "fantasy": 10765,
    "soap": 10766,
    "talk": 10767,
    "thriller": 9648,
    "suspense": 9648,
    "war": 10768,
    "war & politics": 10768,
    "western": 37
}

# Reverse mapping for display
TMDB_ID_TO_GENRE = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Sci-Fi",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action & Adventure", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics"
}

GENRE_NAME_ALIASES: Dict[str, Set[str]] = {
    "sci-fi": {"sci-fi", "science fiction", "sci-fi & fantasy"},
    "science fiction": {"sci-fi", "science fiction", "sci-fi & fantasy"},
    "action": {"action", "action & adventure"},
    "adventure": {"adventure", "action & adventure"},
    "action & adventure": {"action", "adventure", "action & adventure"},
    "fantasy": {"fantasy", "sci-fi & fantasy"},
    "sci-fi & fantasy": {"fantasy", "sci-fi", "science fiction", "sci-fi & fantasy"},
    "animation": {"animation", "anime"},
    "anime": {"animation", "anime"},
    "family": {"family", "kids"},
    "kids": {"family", "kids"},
    "thriller": {"thriller", "suspense", "mystery"},
    "suspense": {"thriller", "suspense"},
    "war": {"war", "war & politics"},
    "war & politics": {"war", "war & politics"}
}

CANONICAL_GENRES: Dict[str, str] = {
    "science fiction": "Sci-Fi",
    "sci-fi": "Sci-Fi",
    "scifi": "Sci-Fi",
    "sci fi": "Sci-Fi",
    "sci-fi & fantasy": "Sci-Fi & Fantasy",
    "action": "Action",
    "action & adventure": "Action & Adventure",
    "adventure": "Adventure",
    "animation": "Animation",
    "anime": "Animation",
    "animated": "Animation",
    "comedy": "Comedy",
    "stand-up": "Comedy",
    "crime": "Crime",
    "documentary": "Documentary",
    "drama": "Drama",
    "family": "Family",
    "kids": "Family",
    "fantasy": "Fantasy",
    "history": "History",
    "historical": "History",
    "horror": "Horror",
    "music": "Music",
    "musical": "Music",
    "mystery": "Mystery",
    "romance": "Romance",
    "romantic": "Romance",
    "romantic comedy": "Romance",
    "thriller": "Thriller",
    "suspense": "Thriller",
    "war": "War",
    "war & politics": "War",
    "western": "Western",
}

# Verified categories that appear officially in TMDb
TMDB_MOVIE_CATEGORIES: List[str] = [
    "Action",
    "Adventure",
    "Animation",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Family",
    "Fantasy",
    "History",
    "Horror",
    "Music",
    "Mystery",
    "Romance",
    "Sci-Fi",
    "Thriller",
    "War",
    "Western",
]

TMDB_TV_CATEGORIES: List[str] = [
    "Action & Adventure",
    "Animation",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Family",
    "Kids",
    "Mystery",
    "News",
    "Reality",
    "Sci-Fi & Fantasy",
    "Soap",
    "Talk",
    "War & Politics",
    "Western",
]

TMDB_ALL_CATEGORIES: List[str] = [
    "Action",
    "Adventure",
    "Action & Adventure",
    "Animation",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Family",
    "Fantasy",
    "History",
    "Horror",
    "Kids",
    "Music",
    "Mystery",
    "Romance",
    "Sci-Fi",
    "Sci-Fi & Fantasy",
    "Thriller",
    "War",
    "War & Politics",
    "Western",
]

def get_tmdb_categories(media_type: str = "all") -> List[str]:
    """Return verified official categories that appear in TMDb for the given media format."""
    if media_type == "movie":
        return list(TMDB_MOVIE_CATEGORIES)
    elif media_type in ("show", "tv"):
        return list(TMDB_TV_CATEGORIES)
    return list(TMDB_ALL_CATEGORIES)

def to_canonical_genre(genre_name: Optional[str]) -> str:
    """Normalize a genre name from Plex or TMDb to a standard canonical display name."""
    if not genre_name:
        return ""
    clean = genre_name.lower().strip()
    if clean in CANONICAL_GENRES:
        return CANONICAL_GENRES[clean]
    return genre_name.strip().title()

def genres_align(genre_a: Optional[str], genre_b: Optional[str]) -> bool:
    """Check if two genre labels belong to the same semantic cluster or are aliases."""
    if not genre_a or not genre_b:
        return False
    clean_a = genre_a.lower().strip()
    clean_b = genre_b.lower().strip()
    if clean_a == clean_b:
        return True

    canon_a = to_canonical_genre(clean_a).lower()
    canon_b = to_canonical_genre(clean_b).lower()
    if canon_a == canon_b:
        return True

    aliases_a = GENRE_NAME_ALIASES.get(clean_a, {clean_a})
    if clean_b in aliases_a or canon_b in aliases_a:
        return True

    aliases_b = GENRE_NAME_ALIASES.get(clean_b, {clean_b})
    if clean_a in aliases_b or canon_a in aliases_b:
        return True

    return False

def get_all_genre_ids_for_name(genre_name: str) -> Set[int]:
    """Retrieve all possible TMDb genre IDs across movie and TV for a genre name."""
    clean = genre_name.lower().strip()
    names_to_check = {clean}
    if clean in GENRE_NAME_ALIASES:
        names_to_check.update(GENRE_NAME_ALIASES[clean])
    canon = to_canonical_genre(clean).lower()
    if canon in GENRE_NAME_ALIASES:
        names_to_check.update(GENRE_NAME_ALIASES[canon])
    names_to_check.add(canon)

    ids = set()
    for n in names_to_check:
        if n in MOVIE_GENRES:
            ids.add(MOVIE_GENRES[n])
        if n in TV_GENRES:
            ids.add(TV_GENRES[n])
    return ids

def candidate_matches_genre(cand: Dict[str, Any], target_genre: Optional[str]) -> bool:
    """Return True if candidate item matches target genre by ID, canonical name, or alias."""
    if not target_genre:
        return True

    clean_target = target_genre.lower().strip()
    target_ids = get_all_genre_ids_for_name(clean_target)

    cand_ids = set(cand.get("genre_ids", []))
    if cand_ids & target_ids:
        return True

    for g in cand.get("genres", []):
        if genres_align(g, clean_target):
            return True

    return False

class TMDbClient:
    def __init__(self, api_key: Optional[str] = None):
        # Explicit override (mainly for tests). When None, read live from
        # settings on each access so config changes take effect immediately
        # without requiring a process restart.
        self._api_key_override = api_key
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25, max_retries=2)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._details_cache: Dict[Tuple[str, int], Tuple[float, Dict[str, Any]]] = {}
        self._details_lock = threading.Lock()

    @property
    def api_key(self) -> Optional[str]:
        if self._api_key_override is not None:
            return self._api_key_override
        return settings.tmdb_api_key

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get_headers_params(self, params: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, Any]]:
        key = self.api_key
        if not key:
            raise ValueError("TMDb API Key is not configured. Please set TMDB_API_KEY.")
        
        headers = {"Accept": "application/json"}
        # Check if user provided v4 Read Access Token or v3 API Key
        if len(key) > 50:
            headers["Authorization"] = f"Bearer {key}"
        else:
            params["api_key"] = key
        return headers, params

    def _request(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = params or {}
        headers, qparams = self._get_headers_params(params)
        url = f"{TMDB_BASE_URL}{endpoint}"
        sess = getattr(self, "session", requests)
        resp = sess.get(url, headers=headers, params=qparams, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def test_connection(self) -> Tuple[bool, str]:
        """Test TMDb API key validity via a lightweight, side-effect-free endpoint."""
        if not self.is_configured():
            return False, "TMDb API Key is not configured."
        try:
            data = self._request("/authentication")
            if data.get("success"):
                return True, "Connected to TMDb"
            return False, "TMDb authentication failed."
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 401:
                return False, "TMDb authentication failed: invalid API key."
            return False, f"TMDb request failed: HTTP {status}."
        except Exception as e:
            return False, f"Could not reach TMDb: {e}"

    def map_genres(self, genre_names: List[str], media_type: str = "movie") -> List[int]:
        """Convert string genre names into TMDb numeric genre IDs."""
        mapping = MOVIE_GENRES if media_type == "movie" else TV_GENRES
        ids = []
        for name in genre_names:
            key = name.lower().strip()
            if key in mapping and mapping[key] not in ids:
                ids.append(mapping[key])
        return ids

    def discover_movies(
        self,
        with_genres: Optional[List[int]] = None,
        with_people: Optional[int] = None,
        with_original_language: Optional[str] = None,
        with_keywords: Optional[List[int]] = None,
        min_rating: float = 7.0,
        min_votes: int = 200,
        max_votes: Optional[int] = None,
        with_runtime_lte: Optional[int] = None,
        sort_by: str = "vote_average.desc",
        page: int = 1
    ) -> List[Dict[str, Any]]:
        """Query /discover/movie."""
        params: Dict[str, Any] = {
            "page": page,
            "sort_by": sort_by,
            "vote_average.gte": min_rating,
            "vote_count.gte": min_votes,
            "include_adult": "false"
        }
        if max_votes is not None:
            params["vote_count.lte"] = int(max_votes)
        if with_runtime_lte is not None:
            params["with_runtime.lte"] = int(with_runtime_lte)
        if with_genres:
            params["with_genres"] = ",".join(str(g) for g in with_genres)
        if with_people:
            params["with_people"] = with_people
        if with_original_language and with_original_language != "all":
            params["with_original_language"] = with_original_language
        if with_keywords:
            # OR-match keywords so candidates aren't over-constrained.
            params["with_keywords"] = "|".join(str(k) for k in with_keywords)

        data = self._request("/discover/movie", params)
        return data.get("results", [])

    def discover_tv(
        self,
        with_genres: Optional[List[int]] = None,
        with_original_language: Optional[str] = None,
        with_keywords: Optional[List[int]] = None,
        min_rating: float = 7.0,
        min_votes: int = 150,
        sort_by: str = "vote_average.desc",
        page: int = 1
    ) -> List[Dict[str, Any]]:
        """Query /discover/tv."""
        params: Dict[str, Any] = {
            "page": page,
            "sort_by": sort_by,
            "vote_average.gte": min_rating,
            "vote_count.gte": min_votes,
            "include_null_first_air_dates": "false"
        }
        if with_genres:
            params["with_genres"] = ",".join(str(g) for g in with_genres)
        if with_original_language and with_original_language != "all":
            params["with_original_language"] = with_original_language
        if with_keywords:
            params["with_keywords"] = "|".join(str(k) for k in with_keywords)

        data = self._request("/discover/tv", params)
        return data.get("results", [])

    def get_keywords(self, item_id: int, media_type: str = "movie") -> List[Dict[str, Any]]:
        """Fetch TMDb keywords for a movie/show. Returns [{"id","name"}]. Non-fatal."""
        endpoint = f"/{'movie' if media_type == 'movie' else 'tv'}/{item_id}/keywords"
        try:
            data = self._request(endpoint)
            # Movies expose "keywords"; TV exposes "results".
            raw = data.get("keywords")
            if raw is None:
                raw = data.get("results", [])
            return [{"id": k.get("id"), "name": k.get("name")} for k in raw if k.get("id")]
        except Exception as e:
            logger.debug(f"Could not fetch keywords for {media_type} {item_id}: {e}")
            return []

    def get_similar(self, item_id: int, media_type: str = "movie", page: int = 1) -> List[Dict[str, Any]]:
        """Fetch TMDb 'similar' titles for a seed item. Non-fatal."""
        endpoint = f"/{'movie' if media_type == 'movie' else 'tv'}/{item_id}/similar"
        try:
            return self._request(endpoint, {"page": page}).get("results", [])
        except Exception as e:
            logger.debug(f"Could not fetch similar for {media_type} {item_id}: {e}")
            return []

    def get_tmdb_recommendations(self, item_id: int, media_type: str = "movie", page: int = 1) -> List[Dict[str, Any]]:
        """Fetch TMDb 'recommendations' for a seed item. Non-fatal."""
        endpoint = f"/{'movie' if media_type == 'movie' else 'tv'}/{item_id}/recommendations"
        try:
            return self._request(endpoint, {"page": page}).get("results", [])
        except Exception as e:
            logger.debug(f"Could not fetch recommendations for {media_type} {item_id}: {e}")
            return []

    def get_external_ids(self, item_id: int, media_type: str = "movie") -> Dict[str, Optional[str]]:
        """Fetch external IDs (imdb_id, tvdb_id)."""
        endpoint = f"/{media_type}/{item_id}/external_ids"
        try:
            data = self._request(endpoint)
            return {
                "imdb_id": data.get("imdb_id"),
                "tvdb_id": str(data.get("tvdb_id")) if data.get("tvdb_id") else None,
                "wikidata_id": data.get("wikidata_id")
            }
        except Exception as e:
            logger.debug(f"Could not fetch external IDs for {media_type} {item_id}: {e}")
            return {"imdb_id": None, "tvdb_id": None}

    def batch_get_external_ids(
        self,
        items: List[Tuple[int, str]],
        max_workers: int = 8
    ) -> Dict[str, Dict[str, Optional[str]]]:
        """Concurrently fetch external IDs for a list of (tmdb_id, media_type) tuples."""
        results: Dict[str, Dict[str, Optional[str]]] = {}
        if not items:
            return results

        def _fetch(it: Tuple[int, str]):
            tid, mtype = it
            return str(tid), self.get_external_ids(tid, media_type=mtype)

        workers = min(max_workers, len(items))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_fetch, it) for it in items]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    tid_str, ids = fut.result()
                    results[tid_str] = ids
                except Exception as e:
                    logger.debug(f"Error in batch external ID fetch: {e}")

        return results

    def search_person(self, name: str) -> Optional[int]:
        """Search person by name (e.g. director) to retrieve TMDb person ID."""
        try:
            data = self._request("/search/person", {"query": name})
            results = data.get("results", [])
            if results:
                return results[0]["id"]
        except Exception as e:
            logger.debug(f"Could not search person '{name}': {e}")
        return None

    def format_item(self, raw: Dict[str, Any], media_type: str) -> Dict[str, Any]:
        """Normalize raw TMDb candidate item."""
        tmdb_id = raw.get("id")
        title = raw.get("title") or raw.get("name") or "Unknown"
        release_date = raw.get("release_date") or raw.get("first_air_date") or ""
        year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None
        
        poster_path = raw.get("poster_path")
        poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
        
        genre_ids = raw.get("genre_ids", [])
        genre_names = []
        seen_g = set()
        for gid in genre_ids:
            if gid in TMDB_ID_TO_GENRE:
                canon = to_canonical_genre(TMDB_ID_TO_GENRE[gid])
                if canon not in seen_g:
                    seen_g.add(canon)
                    genre_names.append(canon)
        
        orig_lang = raw.get("original_language", "en")
        language_map = {
            "en": "English", "ja": "Japanese", "ko": "Korean", "fr": "French",
            "es": "Spanish", "de": "German", "it": "Italian", "da": "Danish",
            "sv": "Swedish", "no": "Norwegian", "nl": "Dutch", "zh": "Chinese",
            "pt": "Portuguese", "hi": "Hindi", "ru": "Russian", "pl": "Polish"
        }
        lang_name = language_map.get(orig_lang, orig_lang.upper())

        return {
            "tmdb_id": str(tmdb_id),
            "media_type": media_type,
            "title": title,
            "year": year,
            "release_date": release_date,
            "rating": round(raw.get("vote_average", 0), 1),
            "vote_count": raw.get("vote_count", 0),
            "summary": raw.get("overview", ""),
            "poster_url": poster_url,
            "genres": genre_names,
            "genre_ids": genre_ids,
            "keywords": raw.get("keywords", []),
            "original_language": orig_lang,
            "language_name": lang_name
        }

    def get_web_url(self, item_id: Any, media_type: str = "movie") -> str:
        """Get direct browser link to the title on TMDb web interface."""
        mtype = "tv" if str(media_type).lower() in ("show", "tv", "episode") else "movie"
        return f"https://www.themoviedb.org/{mtype}/{item_id}"

    def get_details(
        self,
        item_id: int,
        media_type: str = "movie",
        force_refresh: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Fetch rich metadata, trailer, cast/crew, watch providers, and TV seasons."""
        if not self.is_configured():
            return None

        is_tv = str(media_type).lower() in ("show", "tv", "episode")
        ep_type = "tv" if is_tv else "movie"
        canon_type = "show" if is_tv else "movie"
        cache_key = (ep_type, int(item_id))

        now = time.time()
        if not force_refresh:
            with self._details_lock:
                entry = self._details_cache.get(cache_key)
                if entry and (now - entry[0] < 900):
                    return entry[1]

        try:
            raw = self._request(
                f"/{ep_type}/{int(item_id)}",
                {"append_to_response": "videos,credits,watch/providers,external_ids"},
            )
        except Exception as e:
            logger.debug(f"Could not fetch TMDb details for {ep_type} {item_id}: {e}")
            return None

        title = raw.get("title") or raw.get("name") or "Unknown"
        release_date = raw.get("release_date") or raw.get("first_air_date") or ""
        year = int(release_date[:4]) if len(release_date) >= 4 and release_date[:4].isdigit() else None

        poster_path = raw.get("poster_path")
        poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
        backdrop_path = raw.get("backdrop_path")
        backdrop_url = f"https://image.tmdb.org/t/p/w780{backdrop_path}" if backdrop_path else None

        genre_names: List[str] = []
        seen_g: Set[str] = set()
        for g in raw.get("genres") or []:
            gname = g.get("name") if isinstance(g, dict) else str(g)
            canon = to_canonical_genre(gname)
            if canon and canon not in seen_g:
                seen_g.add(canon)
                genre_names.append(canon)

        orig_lang = raw.get("original_language", "en")
        language_map = {
            "en": "English", "ja": "Japanese", "ko": "Korean", "fr": "French",
            "es": "Spanish", "de": "German", "it": "Italian", "da": "Danish",
            "sv": "Swedish", "no": "Norwegian", "nl": "Dutch", "zh": "Chinese",
            "pt": "Portuguese", "hi": "Hindi", "ru": "Russian", "pl": "Polish",
        }
        lang_name = language_map.get(orig_lang, (orig_lang or "EN").upper())

        runtime = raw.get("runtime")
        if not runtime and is_tv:
            ert = raw.get("episode_run_time") or []
            if isinstance(ert, list) and ert:
                runtime = ert[0]

        # Directors / Creators
        directors: List[str] = []
        if is_tv:
            for c in raw.get("created_by") or []:
                if c.get("name") and c["name"] not in directors:
                    directors.append(c["name"])
        credits_data = raw.get("credits") or {}
        if not directors:
            for crew in credits_data.get("crew") or []:
                if crew.get("job") == "Director" and crew.get("name") and crew["name"] not in directors:
                    directors.append(crew["name"])

        cast: List[Dict[str, Any]] = []
        for actor in (credits_data.get("cast") or [])[:8]:
            if not actor.get("name"):
                continue
            prof = actor.get("profile_path")
            cast.append({
                "name": actor["name"],
                "character": actor.get("character") or "",
                "profile_url": f"https://image.tmdb.org/t/p/w185{prof}" if prof else None,
            })

        # Pick best YouTube trailer
        trailer = None
        videos = (raw.get("videos") or {}).get("results") or []
        yt_videos = [v for v in videos if (v.get("site") or "").lower() == "youtube" and v.get("key")]
        if yt_videos:
            def _video_rank(v: Dict[str, Any]) -> Tuple[int, int]:
                vtype = (v.get("type") or "").lower()
                type_score = 3 if vtype == "trailer" else (2 if vtype == "teaser" else 1)
                official_score = 1 if v.get("official") else 0
                return (type_score, official_score)

            best = max(yt_videos, key=_video_rank)
            trailer = {
                "key": best["key"],
                "name": best.get("name") or "Trailer",
                "type": best.get("type") or "Trailer",
            }

        # TV Seasons
        seasons: List[Dict[str, Any]] = []
        if is_tv:
            raw_seasons = raw.get("seasons") or []
            positive_seasons = [s for s in raw_seasons if (s.get("season_number") or 0) > 0]
            chosen = positive_seasons if positive_seasons else raw_seasons
            for s in sorted(chosen, key=lambda x: x.get("season_number") or 0):
                snum = s.get("season_number")
                if snum is None:
                    continue
                seasons.append({
                    "season_number": int(snum),
                    "name": s.get("name") or f"Season {snum}",
                    "episode_count": int(s.get("episode_count") or 0),
                    "air_date": s.get("air_date") or "",
                })

        # Watch Providers (prefer US -> GB -> first available with flatrate)
        watch_providers: List[Dict[str, Any]] = []
        provider_region = None
        wp_results = (raw.get("watch/providers") or {}).get("results") or {}
        if isinstance(wp_results, dict):
            candidate_regions = ["US", "GB"] + [k for k in wp_results.keys() if k not in ("US", "GB")]
            for reg in candidate_regions:
                reg_data = wp_results.get(reg)
                if not isinstance(reg_data, dict):
                    continue
                flatrate = reg_data.get("flatrate") or []
                if flatrate:
                    provider_region = reg
                    for p in flatrate[:6]:
                        if p.get("provider_name"):
                            logo = p.get("logo_path")
                            watch_providers.append({
                                "name": p["provider_name"],
                                "logo_url": f"https://image.tmdb.org/t/p/w92{logo}" if logo else None,
                            })
                    break

        ext = raw.get("external_ids") or {}
        imdb_id = ext.get("imdb_id") or raw.get("imdb_id")
        tvdb_id = str(ext["tvdb_id"]) if ext.get("tvdb_id") else None

        details = {
            "tmdb_id": str(item_id),
            "media_type": canon_type,
            "title": title,
            "tagline": raw.get("tagline") or "",
            "overview": raw.get("overview") or "",
            "release_date": release_date,
            "year": year,
            "rating": round(float(raw.get("vote_average") or 0), 1),
            "vote_count": int(raw.get("vote_count") or 0),
            "runtime": int(runtime) if runtime else None,
            "status": raw.get("status") or "",
            "genres": genre_names,
            "poster_url": poster_url,
            "backdrop_url": backdrop_url,
            "original_language": orig_lang,
            "language_name": lang_name,
            "imdb_id": imdb_id,
            "tvdb_id": tvdb_id,
            "imdb_url": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None,
            "tmdb_url": self.get_web_url(item_id, media_type=canon_type),
            "directors": directors,
            "cast": cast,
            "trailer": trailer,
            "seasons": seasons,
            "number_of_seasons": raw.get("number_of_seasons") or len(seasons),
            "number_of_episodes": raw.get("number_of_episodes"),
            "watch_providers": watch_providers,
            "provider_region": provider_region,
        }

        with self._details_lock:
            if len(self._details_cache) >= 256:
                oldest = min(self._details_cache.keys(), key=lambda k: self._details_cache[k][0])
                self._details_cache.pop(oldest, None)
            self._details_cache[cache_key] = (now, details)

        return details


tmdb = TMDbClient()

