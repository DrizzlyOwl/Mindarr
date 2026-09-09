import logging
import requests
from typing import Optional, Dict, Any, List, Tuple
from plex_recommender.config import settings

logger = logging.getLogger(__name__)

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

MOVIE_GENRES = {
    "action": 28,
    "adventure": 12,
    "animation": 16,
    "anime": 16,
    "comedy": 35,
    "crime": 80,
    "documentary": 99,
    "drama": 18,
    "family": 10751,
    "fantasy": 14,
    "history": 36,
    "horror": 27,
    "music": 10402,
    "mystery": 9648,
    "romance": 10749,
    "sci-fi": 878,
    "science fiction": 878,
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
    "romance": 18,  # TVDb/TMDb TV often categorizes romance under Drama
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

class TMDbClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.tmdb_api_key

    def _get_headers_params(self, params: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, Any]]:
        key = self.api_key or settings.tmdb_api_key
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
        resp = requests.get(url, headers=headers, params=qparams, timeout=10)
        resp.raise_for_status()
        return resp.json()

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
        genre_names = [TMDB_ID_TO_GENRE.get(gid, "Other") for gid in genre_ids if gid in TMDB_ID_TO_GENRE]
        
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
