import logging
import time
import requests
from typing import Optional, Dict, Any, Tuple, List
from plex_recommender.config import settings

logger = logging.getLogger(__name__)

STATUS_MAP = {
    1: "UNKNOWN",
    2: "PENDING",
    3: "PROCESSING",
    4: "PARTIALLY_AVAILABLE",
    5: "AVAILABLE"
}

class OverseerrClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        # Explicit overrides (mainly for tests). When None, read live from
        # settings on each access so config changes take effect immediately.
        self._base_url_override = base_url.rstrip("/") if base_url is not None else None
        self._api_key_override = api_key
        self._users_cache: Optional[List[Dict[str, Any]]] = None
        self._users_cache_time: float = 0.0
        self._queue_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._queue_cache_time: float = 0.0

    @property
    def base_url(self) -> str:
        if self._base_url_override is not None:
            return self._base_url_override
        return (settings.overseerr_url or "").rstrip("/")

    @property
    def api_key(self) -> Optional[str]:
        if self._api_key_override is not None:
            return self._api_key_override
        return settings.overseerr_api_key

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        key = self.api_key or settings.overseerr_api_key
        if key:
            headers["X-Api-Key"] = key
        return headers

    def test_connection(self) -> Tuple[bool, str]:
        """Test Overseerr server reachability and API key authentication."""
        url = f"{self.base_url}/api/v1/status"
        try:
            resp = requests.get(url, headers=self._get_headers(), timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                version = data.get("version", "unknown")
                if not (self.api_key or settings.overseerr_api_key):
                    return True, f"Reachable (Overseerr v{version}), but API key is missing."
                return True, f"Connected to Overseerr v{version}"
            elif resp.status_code in (401, 403):
                return False, "Unauthorized: Invalid Overseerr API Key."
            else:
                return False, f"HTTP Error {resp.status_code}: {resp.text}"
        except Exception as e:
            return False, f"Connection failed: {e}"

    def get_users(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Fetch list of Overseerr users, cached in memory for 15 minutes."""
        now = time.time()
        if not force_refresh and self._users_cache is not None and (now - self._users_cache_time < 900):
            return self._users_cache

        url = f"{self.base_url}/api/v1/user?take=100&skip=0"
        try:
            resp = requests.get(url, headers=self._get_headers(), timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                users = data.get("results", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                self._users_cache = users
                self._users_cache_time = now
                return users
        except Exception as e:
            logger.debug(f"Failed to fetch Overseerr users: {e}")
        return self._users_cache or []

    def resolve_user_id(
        self,
        plex_user_key: Optional[str] = None,
        email: Optional[str] = None,
        username: Optional[str] = None
    ) -> Optional[int]:
        """Resolve an Overseerr internal user ID matching a Plex user identity."""
        users = self.get_users()
        if not users:
            return None

        clean_pkey = str(plex_user_key).strip() if plex_user_key else None
        clean_email = email.strip().lower() if email else None
        clean_username = username.strip().lower() if username else None

        # 1. Match by Plex account ID (most accurate)
        if clean_pkey:
            for u in users:
                u_plex_id = u.get("plexId")
                if u_plex_id is not None and str(u_plex_id).strip() == clean_pkey:
                    return int(u["id"])

        # 2. Match by email
        if clean_email:
            for u in users:
                u_email = (u.get("email") or "").strip().lower()
                if u_email and u_email == clean_email:
                    return int(u["id"])

        # 3. Match by username / plexUsername
        if clean_username:
            for u in users:
                u_name = (u.get("username") or "").strip().lower()
                u_pname = (u.get("plexUsername") or "").strip().lower()
                if clean_username in (u_name, u_pname):
                    return int(u["id"])

        return None

    def get_media_info(self, tmdb_id: int, media_type: str = "movie") -> Dict[str, Any]:
        """Fetch media status from Overseerr (PENDING, PROCESSING, AVAILABLE, etc.)."""
        endpoint_type = "movie" if media_type == "movie" else "tv"
        url = f"{self.base_url}/api/v1/{endpoint_type}/{tmdb_id}"
        try:
            resp = requests.get(url, headers=self._get_headers(), timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                media_info = data.get("mediaInfo") or {}
                status_code = media_info.get("status", 1)
                return {
                    "exists": True,
                    "status_code": status_code,
                    "status_name": STATUS_MAP.get(status_code, "UNKNOWN"),
                    "overseerr_id": media_info.get("id"),
                    "download_status": media_info.get("downloadStatus")
                }
        except Exception as e:
            logger.debug(f"Overseerr get_media_info failed for {media_type} {tmdb_id}: {e}")
        return {
            "exists": False,
            "status_code": 1,
            "status_name": "NOT_REQUESTED"
        }

    def get_request_queue(self, force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        """Fetch server-wide active request queue from Overseerr, cached in memory for 60 seconds."""
        now = time.time()
        if not force_refresh and self._queue_cache is not None and (now - self._queue_cache_time < 60):
            return self._queue_cache

        key = self.api_key or settings.overseerr_api_key
        if not key or not self.base_url:
            return {}

        url = f"{self.base_url}/api/v1/request?take=500&skip=0&filter=all"
        queue_index: Dict[str, Dict[str, Any]] = {}
        try:
            resp = requests.get(url, headers=self._get_headers(), timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                for r in results:
                    media = r.get("media") or {}
                    tmdb_id = media.get("tmdbId")
                    if not tmdb_id:
                        continue
                    mtype = media.get("mediaType") or r.get("type") or "movie"
                    status_code = media.get("status")
                    if status_code is None:
                        # Fallback to request level status: 1=PENDING, 2=APPROVED(PROCESSING)
                        req_status = r.get("status", 1)
                        status_code = 2 if req_status == 1 else 3

                    # Only track active queue statuses (2=PENDING, 3=PROCESSING, 4=PARTIALLY_AVAILABLE)
                    if status_code in (2, 3, 4):
                        status_name = STATUS_MAP.get(status_code, "PENDING")
                        info = {
                            "status_code": status_code,
                            "status_name": status_name,
                            "request_id": r.get("id"),
                            "tmdb_id": tmdb_id,
                            "media_type": mtype,
                        }
                        queue_index[f"{mtype}_{tmdb_id}"] = info
                        if mtype in ("show", "tv"):
                            queue_index[f"show_{tmdb_id}"] = info
                            queue_index[f"tv_{tmdb_id}"] = info

                self._queue_cache = queue_index
                self._queue_cache_time = now
                return queue_index
        except Exception as e:
            logger.debug(f"Failed to fetch Overseerr request queue: {e}")

        return self._queue_cache or {}

    def mark_queued(self, tmdb_id: int, media_type: str, status_code: int = 2):
        """Record an item in the active in-memory queue cache immediately."""
        if self._queue_cache is None:
            self._queue_cache = {}
        info = {
            "status_code": status_code,
            "status_name": STATUS_MAP.get(status_code, "PENDING"),
            "tmdb_id": tmdb_id,
            "media_type": media_type,
        }
        self._queue_cache[f"{media_type}_{tmdb_id}"] = info
        if media_type in ("show", "tv"):
            self._queue_cache[f"show_{tmdb_id}"] = info
            self._queue_cache[f"tv_{tmdb_id}"] = info

    def request_media(
        self,
        tmdb_id: int,
        media_type: str = "movie",
        is_4k: bool = False,
        seasons: Any = None,
        user_id: Optional[int] = None
    ) -> Tuple[bool, str]:
        """Submit a new media request to Overseerr. TV shows default to Season 1 only."""
        key = self.api_key or settings.overseerr_api_key
        if not key:
            return False, "Overseerr API key is not configured."

        endpoint_type = "movie" if media_type == "movie" else "tv"
        url = f"{self.base_url}/api/v1/request"

        payload: Dict[str, Any] = {
            "mediaType": endpoint_type,
            "mediaId": int(tmdb_id),
            "is4k": is_4k
        }
        if user_id is not None:
            payload["userId"] = int(user_id)

        if endpoint_type == "tv":
            # Default TV requests to only Season 1 ([1]) unless explicitly specified
            payload["seasons"] = [1] if seasons is None else seasons

        try:
            resp = requests.post(url, json=payload, headers=self._get_headers(), timeout=10)
            if resp.status_code in (200, 201):
                season_str = " (Season 1)" if (endpoint_type == "tv" and payload["seasons"] == [1]) else ""
                return True, f"Request submitted successfully to Overseerr{season_str}!"
            elif resp.status_code == 409:
                return False, "This item has already been requested or is already available."
            else:
                try:
                    err_json = resp.json()
                    err_msg = err_json.get("message") or str(err_json)
                except Exception:
                    err_msg = resp.text
                return False, f"Overseerr Error: {err_msg}"
        except Exception as e:
            logger.error(f"Overseerr request failed: {e}")
            return False, str(e)

    def get_web_url(self, tmdb_id: int, media_type: str = "movie") -> str:
        """Get direct browser link to the title on Overseerr web interface."""
        endpoint_type = "movie" if media_type == "movie" else "tv"
        return f"{self.base_url}/{endpoint_type}/{tmdb_id}"

overseerr = OverseerrClient()
