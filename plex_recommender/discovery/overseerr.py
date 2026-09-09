import logging
import requests
from typing import Optional, Dict, Any, Tuple
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

    def request_media(
        self,
        tmdb_id: int,
        media_type: str = "movie",
        is_4k: bool = False,
        seasons: Any = None
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
