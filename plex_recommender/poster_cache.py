import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

from plex_recommender.discovery.tmdb import TMDbClient, TMDB_IMAGE_BASE

logger = logging.getLogger(__name__)

POSTER_TTL_DAYS = 7

CACHE_DIR = Path(__file__).resolve().parent / "web" / "static" / "posters"


def _cache_dir() -> Path:
    """Return the poster cache directory, creating it on first use."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


def _cache_path(tmdb_id, media_type: str) -> Path:
    mtype = "tv" if str(media_type).lower() in ("show", "tv", "episode") else "movie"
    return _cache_dir() / f"{mtype}_{tmdb_id}.jpg"


def touch_poster(path: Path) -> None:
    """Refresh the access time of a cached poster so it survives the TTL sweep,
    regardless of the host filesystem's atime mount behavior (relatime/noatime).
    """
    try:
        os.utime(path, None)
    except OSError as e:
        logger.warning(f"Could not refresh atime for cached poster {path}: {e}")


def _download_and_cache(path: Path, remote_image_url: str) -> bool:
    """Download an image from a fully-qualified URL and write it to the cache
    path. Returns True on success. Never raises."""
    try:
        resp = requests.get(remote_image_url, timeout=10)
        resp.raise_for_status()
        path.write_bytes(resp.content)
        return True
    except Exception as e:
        logger.debug(f"Could not download poster from {remote_image_url}: {e}")
        return False


def get_cached_poster_url(tmdb_id, media_type: str, low_bandwidth: bool = False) -> Optional[str]:
    """Return a local /static/posters/... URL for the given TMDb item, downloading
    and caching it on first use. Returns None if low-bandwidth mode is enabled,
    no tmdb_id is known, or the poster could not be fetched. Never raises.

    Use this when only a tmdb_id is known (e.g. a watched item pulled from the
    local DB) and a TMDb detail lookup is needed to discover the poster_path.
    If the source poster URL is already known (e.g. from a TMDb discovery/search
    response), prefer get_cached_poster_url_from_source instead to avoid the
    extra detail API call.
    """
    if low_bandwidth or not tmdb_id:
        return None

    path = _cache_path(tmdb_id, media_type)
    if path.exists():
        touch_poster(path)
        return f"/static/posters/{path.name}"

    try:
        client = TMDbClient()
        mtype = "tv" if str(media_type).lower() in ("show", "tv", "episode") else "movie"
        data = client._request(f"/{mtype}/{tmdb_id}")
        poster_path = data.get("poster_path")
        if not poster_path:
            return None

        image_url = f"{TMDB_IMAGE_BASE}{poster_path}"
        if _download_and_cache(path, image_url):
            return f"/static/posters/{path.name}"
        return None
    except Exception as e:
        logger.debug(f"Could not cache poster for {media_type} tmdb_id={tmdb_id}: {e}")
        return None


def get_cached_poster_url_from_source(
    tmdb_id, media_type: str, source_poster_url: Optional[str], low_bandwidth: bool = False
) -> Optional[str]:
    """Like get_cached_poster_url, but for callers that already have a
    fully-qualified TMDb poster URL on hand (e.g. recommendations/discovery
    results), skipping the extra TMDb detail API call entirely.

    Returns the local cached URL on success. On cache-miss download failure,
    falls back to the original source_poster_url (graceful degradation, so a
    transient network hiccup doesn't hide a poster the caller already had).
    Returns None only if low-bandwidth mode is on or no tmdb_id/source URL exists.
    """
    if low_bandwidth or not tmdb_id or not source_poster_url:
        return None

    path = _cache_path(tmdb_id, media_type)
    if path.exists():
        touch_poster(path)
        return f"/static/posters/{path.name}"

    if _download_and_cache(path, source_poster_url):
        return f"/static/posters/{path.name}"
    return source_poster_url


def sweep_expired_posters(ttl_days: int = POSTER_TTL_DAYS) -> dict:
    """Delete cached posters not accessed within ttl_days. Returns a summary dict
    with deleted/kept counts and bytes freed. Never raises.
    """
    result = {"deleted": 0, "kept": 0, "bytes_freed": 0}
    directory = CACHE_DIR
    if not directory.exists():
        return result

    cutoff = time.time() - (ttl_days * 86400)
    try:
        entries = list(directory.iterdir())
    except OSError as e:
        logger.warning(f"Could not list poster cache directory: {e}")
        return result

    for entry in entries:
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
            if stat.st_atime < cutoff:
                size = stat.st_size
                entry.unlink()
                result["deleted"] += 1
                result["bytes_freed"] += size
            else:
                result["kept"] += 1
        except OSError as e:
            logger.warning(f"Could not evaluate/delete cached poster {entry}: {e}")

    if result["deleted"]:
        logger.info(
            f"Poster cache sweep: deleted {result['deleted']} expired poster(s), "
            f"freed {result['bytes_freed']} bytes, kept {result['kept']}."
        )
    return result
