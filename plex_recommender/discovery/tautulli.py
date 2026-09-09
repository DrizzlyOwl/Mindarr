import logging
from typing import Optional, Dict, Any, Tuple, List
import requests
from plex_recommender.config import settings

logger = logging.getLogger(__name__)


class TautulliClient:
    """Client for Tautulli's /api/v2 endpoint.

    Optional enrichment source. Never raises to callers; degrades gracefully
    so that a missing/misconfigured Tautulli never breaks the sync flow.
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        # Explicit overrides (mainly for tests). When None, values are read live
        # from settings on each access so config changes take effect immediately.
        self._base_url_override = base_url.rstrip("/") if base_url is not None else None
        self._api_key_override = api_key

    @property
    def base_url(self) -> str:
        if self._base_url_override is not None:
            return self._base_url_override
        return (settings.tautulli_url or "").rstrip("/")

    @property
    def api_key(self) -> Optional[str]:
        if self._api_key_override is not None:
            return self._api_key_override
        return settings.tautulli_api_key

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _call(self, cmd: str, **params: Any) -> Optional[Any]:
        """Call a Tautulli command, returning response.data (or None on error)."""
        if not self.is_configured():
            return None
        url = f"{self.base_url}/api/v2"
        query = {
            "apikey": self.api_key,
            "cmd": cmd,
            "out_type": "json",
        }
        query.update(params)
        try:
            resp = requests.get(url, params=query, timeout=15)
            if resp.status_code != 200:
                logger.debug("Tautulli %s HTTP %s", cmd, resp.status_code)
                return None
            payload = resp.json().get("response", {})
            if payload.get("result") != "success":
                logger.debug("Tautulli %s non-success: %s", cmd, payload.get("message"))
                return None
            return payload.get("data")
        except Exception as e:
            logger.debug("Tautulli %s failed: %s", cmd, e)
            return None

    def test_connection(self) -> Tuple[bool, str]:
        """Test Tautulli reachability and API key authentication."""
        if not self.base_url:
            return False, "Tautulli URL is not configured."
        if not (self.api_key):
            return False, "Tautulli API key is not configured."
        data = self._call("get_server_info")
        if data is None:
            return False, "Connection failed: check Tautulli URL and API key."
        server_name = data.get("pms_name", "Tautulli")
        return True, f"Connected to Tautulli ({server_name})"

    def get_users(self) -> List[Dict[str, Any]]:
        """Return Tautulli users: [{user_id, username, email}, ...]."""
        data = self._call("get_users")
        if not data:
            return []
        users = []
        for u in data:
            users.append({
                "user_id": str(u.get("user_id")),
                "username": u.get("username"),
                "email": u.get("email"),
            })
        return users

    def resolve_user_id(self, username: Optional[str] = None, email: Optional[str] = None) -> Optional[str]:
        """Match a Plex identity to a Tautulli user_id by email or username."""
        email_l = (email or "").strip().lower()
        username_l = (username or "").strip().lower()
        for u in self.get_users():
            if email_l and (u.get("email") or "").strip().lower() == email_l:
                return u["user_id"]
        for u in self.get_users():
            if username_l and (u.get("username") or "").strip().lower() == username_l:
                return u["user_id"]
        return None

    def get_server_id(self) -> Optional[str]:
        """Return the machineIdentifier of the Plex server Tautulli monitors."""
        data = self._call("get_server_info")
        if not data:
            return None
        return data.get("pms_identifier")

    def monitors_server(self, machine_id: Optional[str]) -> bool:
        """True if Tautulli monitors the given Plex server (by machineIdentifier).

        If no machine_id is configured, we cannot verify scope, so return False
        to avoid pulling data from an unrelated server.
        """
        if not machine_id:
            return False
        return self.get_server_id() == machine_id

    def get_history(self, user_id: Optional[str] = None, length: int = 10000, machine_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return watch history rows for a Tautulli user_id (or all users if user_id is None), optionally scoped to a server."""
        params: Dict[str, Any] = {"length": length}
        if user_id:
            params["user_id"] = user_id
        if machine_id:
            params["machine_id"] = machine_id
        data = self._call("get_history", **params)
        if not data:
            return []
        return data.get("data", []) if isinstance(data, dict) else []

    def get_home_stats(self, time_range: int = 30) -> List[Dict[str, Any]]:
        """Return Tautulli home stats (top_movies, popular_movies, top_tv, popular_tv, etc.)."""
        data = self._call("get_home_stats", time_range=time_range)
        if not data or not isinstance(data, list):
            return []
        return data

    def get_metadata(self, rating_key: str) -> Dict[str, Any]:
        """Fetch identifiers and basic metadata (genres/cast/title/year) for a rating_key."""
        result: Dict[str, Any] = {
            "imdb_id": None, "tmdb_id": None, "tvdb_id": None,
            "title": None, "year": None, "genres": [],
            "directors": [], "actors": [],
        }
        data = self._call("get_metadata", rating_key=rating_key)
        if not data:
            return result
        result["imdb_id"] = data.get("imdb_id") or None
        result["tmdb_id"] = data.get("themoviedb_id") or data.get("tmdb_id") or None
        result["tvdb_id"] = data.get("thetvdb_id") or data.get("tvdb_id") or None
        result["title"] = data.get("title") or None
        result["year"] = data.get("year") or None
        result["genres"] = [str(g) for g in (data.get("genres") or []) if g]
        result["directors"] = [str(d) for d in (data.get("directors") or []) if d]
        result["actors"] = [str(a) for a in (data.get("actors") or []) if a][:15]
        for g in data.get("guids", []) or []:
            gs = str(g)
            if gs.startswith("imdb://") and not result["imdb_id"]:
                result["imdb_id"] = gs.replace("imdb://", "").strip()
            elif gs.startswith("tmdb://") and not result["tmdb_id"]:
                result["tmdb_id"] = gs.replace("tmdb://", "").strip()
            elif gs.startswith("tvdb://") and not result["tvdb_id"]:
                result["tvdb_id"] = gs.replace("tvdb://", "").strip()
        return result


tautulli = TautulliClient()
