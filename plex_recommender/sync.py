import logging
import requests
import urllib3
from datetime import datetime, timezone
from typing import Optional, Callable, Dict, Any, List
from plexapi.server import PlexServer
from plex_recommender.config import settings
from plex_recommender.db import (
    init_db,
    upsert_media_item,
    upsert_media_items_batch,
    get_existing_keywords_map,
    upsert_user_media,
    record_watch_event,
    set_setting,
    get_stats,
    clear_recommendations_cache,
    get_user,
)
from plex_recommender.discovery.tautulli import tautulli
from plex_recommender.discovery.tmdb import TMDbClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger(__name__)

_tmdb_client = TMDbClient()


def _fetch_tmdb_keywords(tmdb_id: Optional[str], media_type: str) -> List[Dict[str, Any]]:
    """Best-effort TMDb keyword fetch for a watched item. Never raises."""
    if not tmdb_id or not settings.tmdb_api_key:
        return []
    try:
        return _tmdb_client.get_keywords(int(tmdb_id), media_type=media_type)
    except Exception as e:
        logger.debug("Keyword fetch failed for %s %s: %s", media_type, tmdb_id, e)
        return []

def parse_guids(item) -> Dict[str, Optional[str]]:
    """Extract imdb_id, tmdb_id, tvdb_id from Plex item guids."""
    res = {
        "imdb_id": None,
        "tmdb_id": None,
        "tvdb_id": None,
        "raw_guids": []
    }
    
    # Check item.guids (list of Guid objects)
    guids_list = getattr(item, "guids", []) or []
    for g in guids_list:
        guid_str = str(getattr(g, "id", g))
        res["raw_guids"].append(guid_str)
        if guid_str.startswith("imdb://"):
            res["imdb_id"] = guid_str.replace("imdb://", "").strip()
        elif guid_str.startswith("tmdb://"):
            res["tmdb_id"] = guid_str.replace("tmdb://", "").strip()
        elif guid_str.startswith("tvdb://"):
            res["tvdb_id"] = guid_str.replace("tvdb://", "").strip()

    # Fallback to item.guid if secondary IDs weren't found
    main_guid = str(getattr(item, "guid", "") or "")
    if main_guid:
        res["raw_guids"].append(main_guid)
        if "imdb://" in main_guid and not res["imdb_id"]:
            res["imdb_id"] = main_guid.split("imdb://")[-1].split("?")[0].strip()
        elif "tmdb://" in main_guid and not res["tmdb_id"]:
            res["tmdb_id"] = main_guid.split("tmdb://")[-1].split("?")[0].strip()
        elif "tvdb://" in main_guid and not res["tvdb_id"]:
            res["tvdb_id"] = main_guid.split("tvdb://")[-1].split("?")[0].strip()

    return res

def get_plex_instance() -> PlexServer:
    """Connect to Plex Media Server with SSL verification disabled for local IPs."""
    url = settings.plex_url
    token = settings.plex_token
    if not token:
        raise ValueError("Plex token is not configured. Run setup/auth first.")
    
    session = requests.Session()
    session.verify = False
    return PlexServer(url, token, session=session, timeout=15)

def sync_library_metadata(progress_callback: Optional[Callable[[str, float], None]] = None) -> Dict[str, Any]:
    """
    Scan Plex libraries and cache shared media metadata into the local database.

    This populates the shared `media_items` table (genres, GUIDs, ratings, etc.)
    used to enrich per-user recommendations. It does NOT record per-user watch
    state; that comes from Tautulli via sync_user_history().
    """
    init_db()
    if progress_callback:
        progress_callback("Connecting to Plex Media Server...", 0.05)

    plex = get_plex_instance()
    logger.info(f"Connected to Plex: {plex.friendlyName}")

    # Pre-load known keywords from database to eliminate redundant TMDb API calls
    existing_keywords = get_existing_keywords_map()

    # Scan Libraries for watched items (metadata cache only)
    sections = plex.library.sections()
    total_sections = len(sections)
    
    for idx, section in enumerate(sections):
        sec_name = section.title
        sec_type = section.type
        step_base = 0.15 + (0.45 * (idx / max(total_sections, 1)))
        
        if progress_callback:
            progress_callback(f"Scanning '{sec_name}' library (batching catalog)...", step_base)

        if sec_type == "movie":
            # Scan the full library (watched + unwatched) in a single fast batch
            try:
                all_movies = section.all()
                movie_batch = []
                for movie in all_movies:
                    guids_info = parse_guids(movie)
                    genres = [g.tag for g in getattr(movie, "genres", [])]
                    directors = [d.tag for d in getattr(movie, "directors", [])]
                    writers = [w.tag for w in getattr(movie, "writers", [])]
                    actors = [r.tag for r in getattr(movie, "roles", [])[:15]]
                    tmdb_id_str = str(guids_info["tmdb_id"]) if guids_info["tmdb_id"] else None
                    kws = existing_keywords.get(tmdb_id_str, []) if tmdb_id_str else []

                    movie_batch.append({
                        "item_id": str(movie.ratingKey),
                        "media_type": "movie",
                        "title": movie.title,
                        "year": getattr(movie, "year", None),
                        "release_date": str(getattr(movie, "originallyAvailableAt", "") or ""),
                        "genres": genres,
                        "directors": directors,
                        "writers": writers,
                        "actors": actors,
                        "summary": getattr(movie, "summary", "") or "",
                        "user_rating": getattr(movie, "userRating", None),
                        "audience_rating": getattr(movie, "audienceRating", None),
                        "critic_rating": getattr(movie, "rating", None),
                        "imdb_id": guids_info["imdb_id"],
                        "tmdb_id": guids_info["tmdb_id"],
                        "tvdb_id": guids_info["tvdb_id"],
                        "view_count": getattr(movie, "viewCount", 1) or 1,
                        "last_viewed_at": getattr(movie, "lastViewedAt", None),
                        "raw_guids": guids_info["raw_guids"],
                        "keywords": kws,
                    })

                upsert_media_items_batch(movie_batch)
                logger.info("Batched %s movies into media_items cache.", len(movie_batch))
            except Exception as e:
                logger.error(f"Error scanning movie library '{sec_name}': {e}")

        elif sec_type == "show":
            # First, index every show in the library (watched + unwatched) in a fast batch
            try:
                all_shows = section.all()
                show_batch = []
                for show in all_shows:
                    try:
                        show_guids = parse_guids(show)
                        show_genres = [g.tag for g in getattr(show, "genres", [])]
                        show_directors = [d.tag for d in getattr(show, "directors", [])]
                        show_actors = [r.tag for r in getattr(show, "roles", [])[:15]]
                        tmdb_id_str = str(show_guids["tmdb_id"]) if show_guids["tmdb_id"] else None
                        kws = existing_keywords.get(tmdb_id_str, []) if tmdb_id_str else []

                        show_batch.append({
                            "item_id": str(show.ratingKey),
                            "media_type": "show",
                            "title": show.title,
                            "year": getattr(show, "year", None),
                            "release_date": str(getattr(show, "originallyAvailableAt", "") or ""),
                            "genres": show_genres,
                            "directors": show_directors,
                            "writers": [],
                            "actors": show_actors,
                            "summary": getattr(show, "summary", "") or "",
                            "user_rating": getattr(show, "userRating", None),
                            "audience_rating": getattr(show, "audienceRating", None),
                            "critic_rating": getattr(show, "rating", None),
                            "imdb_id": show_guids["imdb_id"],
                            "tmdb_id": show_guids["tmdb_id"],
                            "tvdb_id": show_guids["tvdb_id"],
                            "view_count": getattr(show, "viewedLeafCount", 0) or 0,
                            "last_viewed_at": getattr(show, "lastViewedAt", None),
                            "raw_guids": show_guids["raw_guids"],
                            "keywords": kws,
                        })
                    except Exception as ex:
                        logger.debug(f"Could not index show in '{sec_name}': {ex}")

                upsert_media_items_batch(show_batch)
                logger.info("Batched %s shows into media_items cache.", len(show_batch))
            except Exception as e:
                logger.error(f"Error scanning show library '{sec_name}': {e}")

            # Then walk watched episodes to capture per-show view stats & rollup.
            try:
                if progress_callback:
                    progress_callback(f"Indexing watched episodes in '{sec_name}'...", step_base + 0.15)
                watched_episodes = section.searchEpisodes(unwatched=False)
                logger.info(f"Found {len(watched_episodes)} watched episodes in '{sec_name}'")
                
                # Group by show to avoid re-querying show metadata repeatedly
                seen_shows = set()
                episode_batch = []
                for ep in watched_episodes:
                    show_key = getattr(ep, "grandparentRatingKey", None)
                    show_title = getattr(ep, "grandparentTitle", getattr(ep, "showTitle", ""))
                    
                    if show_key and show_key not in seen_shows:
                        seen_shows.add(show_key)
                        try:
                            show = plex.fetchItem(show_key)
                            show_guids = parse_guids(show)
                            show_genres = [g.tag for g in getattr(show, "genres", [])]
                            show_directors = [d.tag for d in getattr(show, "directors", [])]
                            show_actors = [r.tag for r in getattr(show, "roles", [])[:15]]
                            tmdb_id_str = str(show_guids["tmdb_id"]) if show_guids["tmdb_id"] else None
                            # If keywords not known for this watched show, fetch once
                            if tmdb_id_str and tmdb_id_str not in existing_keywords:
                                kws = _fetch_tmdb_keywords(show_guids["tmdb_id"], "show")
                                if kws:
                                    existing_keywords[tmdb_id_str] = kws
                            else:
                                kws = existing_keywords.get(tmdb_id_str, []) if tmdb_id_str else []

                            upsert_media_item({
                                "item_id": str(show.ratingKey),
                                "media_type": "show",
                                "title": show.title,
                                "year": getattr(show, "year", None),
                                "release_date": str(getattr(show, "originallyAvailableAt", "") or ""),
                                "genres": show_genres,
                                "directors": show_directors,
                                "writers": [],
                                "actors": show_actors,
                                "summary": getattr(show, "summary", "") or "",
                                "user_rating": getattr(show, "userRating", None),
                                "audience_rating": getattr(show, "audienceRating", None),
                                "critic_rating": getattr(show, "rating", None),
                                "imdb_id": show_guids["imdb_id"],
                                "tmdb_id": show_guids["tmdb_id"],
                                "tvdb_id": show_guids["tvdb_id"],
                                "view_count": getattr(show, "viewedLeafCount", 1) or 1,
                                "last_viewed_at": getattr(show, "lastViewedAt", None),
                                "raw_guids": show_guids["raw_guids"],
                                "keywords": kws,
                            })
                        except Exception as ex:
                            logger.debug(f"Could not fetch parent show {show_key}: {ex}")

                    # Also index individual episode
                    ep_guids = parse_guids(ep)
                    episode_batch.append({
                        "item_id": str(ep.ratingKey),
                        "media_type": "episode",
                        "title": f"{show_title} - {ep.title}",
                        "year": getattr(ep, "year", None),
                        "release_date": str(getattr(ep, "originallyAvailableAt", "") or ""),
                        "genres": [],
                        "directors": [d.tag for d in getattr(ep, "directors", [])],
                        "writers": [w.tag for w in getattr(ep, "writers", [])],
                        "actors": [],
                        "summary": getattr(ep, "summary", "") or "",
                        "user_rating": getattr(ep, "userRating", None),
                        "audience_rating": None,
                        "critic_rating": None,
                        "imdb_id": ep_guids["imdb_id"],
                        "tmdb_id": ep_guids["tmdb_id"],
                        "tvdb_id": ep_guids["tvdb_id"],
                        "view_count": getattr(ep, "viewCount", 1) or 1,
                        "last_viewed_at": getattr(ep, "lastViewedAt", None),
                        "raw_guids": ep_guids["raw_guids"],
                        "keywords": [],
                    })

                upsert_media_items_batch(episode_batch)
                logger.info("Batched %s watched episodes into media_items cache.", len(episode_batch))
            except Exception as e:
                logger.error(f"Error scanning show library '{sec_name}': {e}")

    now_iso = datetime.now().isoformat()
    set_setting("last_sync_time", now_iso)
    clear_recommendations_cache()
    logger.info("Cleared recommendations cache following successful library metadata sync.")

    if progress_callback:
        progress_callback("Library metadata sync completed!", 1.0)

    stats = get_stats()
    return stats


def _epoch_to_iso(value: Any) -> Optional[str]:
    """Convert a Tautulli epoch timestamp to an ISO datetime string."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def _viewed_at_iso(value: Any) -> Optional[str]:
    """Normalise a Plex viewedAt (datetime or epoch) to an ISO string in UTC."""
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.astimezone()
        return value.astimezone(timezone.utc).isoformat()
    return _epoch_to_iso(value)


def _resolve_plex_account_id(plex: PlexServer, user: Dict[str, Any]) -> Optional[int]:
    """Map a stored plex.tv account (user_key) to the server-local systemAccount id.

    Plex server play history is keyed by a *server-local* accountID which differs
    from the global plex.tv account id we store as user_key. The server owner is
    account id 1. Other users are matched by username or email.
    """
    username = (user.get("username") or "").strip().lower()
    email = (user.get("email") or "").strip().lower()
    title = (user.get("title") or "").strip().lower()

    try:
        accounts = plex.systemAccounts()
    except Exception as e:
        logger.warning("Could not list Plex system accounts: %s", e)
        return None

    # The owner is always local account id 1 in Plex history.
    if user.get("is_admin"):
        for a in accounts:
            if getattr(a, "accountID", None) == 1:
                return 1

    for a in accounts:
        name = (getattr(a, "name", "") or "").strip().lower()
        if name and name in (username, email, title):
            return getattr(a, "accountID", None)

    return None


def sync_plex_user_history(
    user_key: str,
    plex: Optional[PlexServer] = None,
    progress_callback: Optional[Callable[[str, float], None]] = None,
) -> int:
    """
    Pull a user's server-side watch history from the Plex API (primary source).

    Uses the admin/server token to query play history filtered to this user's
    server-local account id, records watch events, and indexes the watched items
    per-user. Returns the number of events recorded.
    """
    if not settings.plex_token:
        logger.info("No Plex token configured; skipping Plex history for %s.", user_key)
        return 0

    user = get_user(user_key) or {"user_key": user_key}
    plex = plex or get_plex_instance()

    account_id = _resolve_plex_account_id(plex, user)
    if account_id is None:
        logger.info(
            "Could not map user %s to a Plex server account; no Plex history pulled.",
            user_key,
        )
        return 0

    if progress_callback:
        progress_callback("Fetching Plex watch history...", 0.4)

    try:
        history_entries = plex.history(maxresults=10000, accountID=account_id)
    except Exception as e:
        logger.warning("Could not fetch Plex history for user %s: %s", user_key, e)
        return 0

    count = 0
    metadata_cache: Dict[str, Dict[str, Any]] = {}
    # Tally view counts per target (movie or parent-show) across this full
    # history pull, since Plex history is per-playback-event, not per-item.
    target_tally: Dict[str, Dict[str, Any]] = {}

    for h in history_entries:
        rating_key = str(getattr(h, "ratingKey", "") or "")
        if not rating_key:
            continue
        media_type = getattr(h, "type", "movie")
        raw_title = getattr(h, "title", "") or ""
        grandparent_title = getattr(h, "grandparentTitle", "") or ""
        if media_type == "episode" and grandparent_title:
            title = f"{grandparent_title} - {raw_title}"
        else:
            title = raw_title
        viewed_at = _viewed_at_iso(getattr(h, "viewedAt", None))

        record_watch_event({
            "user_key": user_key,
            "item_id": rating_key,
            "title": title,
            "media_type": media_type,
            "viewed_at": viewed_at,
            "account_id": user_key,
            "duration_watched": getattr(h, "duration", 0) or 0,
            "source": "plex",
        })

        # For episodes, attribute the watch to the parent SHOW (which carries
        # genres/creators). Roll up to the grandparent so the taste profile has
        # genre data instead of empty per-episode metadata.
        target_key = rating_key
        target_type = media_type
        if media_type == "episode":
            gp = getattr(h, "grandparentRatingKey", None)
            if gp:
                target_key = str(gp)
                target_type = "show"

        info = metadata_cache.get(target_key)
        if info is None:
            info = {
                "imdb_id": None, "tmdb_id": None, "tvdb_id": None,
                "year": None, "title": None,
                "genres": [], "directors": [], "actors": [],
                "user_rating": None,
            }
            try:
                item = plex.fetchItem(int(target_key))
                guids = parse_guids(item)
                info.update({
                    "imdb_id": guids["imdb_id"],
                    "tmdb_id": guids["tmdb_id"],
                    "tvdb_id": guids["tvdb_id"],
                    "year": getattr(item, "year", None),
                    "title": getattr(item, "title", None),
                    "genres": [g.tag for g in getattr(item, "genres", []) or []],
                    "directors": [d.tag for d in getattr(item, "directors", []) or []],
                    "actors": [r.tag for r in (getattr(item, "roles", []) or [])[:15]],
                    "user_rating": getattr(item, "userRating", None),
                })
            except Exception as e:
                logger.debug("Could not fetch Plex metadata for %s: %s", target_key, e)
            metadata_cache[target_key] = info

        tally = target_tally.get(target_key)
        if tally is None:
            tally = {
                "media_type": target_type,
                "title": info.get("title") or title,
                "info": info,
                "view_count": 0,
                "last_viewed_at": viewed_at,
            }
            target_tally[target_key] = tally
        tally["view_count"] += 1
        # Track the most recent view across all events for this target.
        if viewed_at and (not tally["last_viewed_at"] or viewed_at > tally["last_viewed_at"]):
            tally["last_viewed_at"] = viewed_at

        count += 1

    for target_key, tally in target_tally.items():
        info = tally["info"]
        upsert_user_media(user_key, {
            "item_id": target_key,
            "media_type": tally["media_type"],
            "title": tally["title"],
            "year": info.get("year"),
            "genres": info.get("genres", []),
            "directors": info.get("directors", []),
            "actors": info.get("actors", []),
            "imdb_id": info.get("imdb_id"),
            "tmdb_id": info.get("tmdb_id"),
            "tvdb_id": info.get("tvdb_id"),
            "view_count": tally["view_count"],
            "last_viewed_at": tally["last_viewed_at"],
            "user_rating": info.get("user_rating"),
        })

    logger.info("Recorded %s Plex history events for user %s.", count, user_key)
    return count


def sync_tautulli_user_history(
    user_key: str,
    progress_callback: Optional[Callable[[str, float], None]] = None,
) -> int:
    """
    Enrich a user's watch history from Tautulli (optional secondary source).

    Tautulli is optional enrichment. If it is not configured, or the user cannot
    be matched to a Tautulli account, or that account has no history, nothing is
    added from Tautulli. Returns the number of events recorded.
    """
    user = get_user(user_key)
    if not user:
        return 0

    if not tautulli.is_configured():
        logger.info("Tautulli not configured; skipping enrichment for %s.", user_key)
        return 0

    # Only enrich from Tautulli if it monitors the same server the admin linked.
    machine_id = settings.plex_machine_id
    if not tautulli.monitors_server(machine_id):
        logger.info(
            "Tautulli does not monitor the linked server (%s); skipping enrichment for %s.",
            machine_id, user_key,
        )
        return 0

    if progress_callback:
        progress_callback("Matching Tautulli account...", 0.7)

    tautulli_user_id = tautulli.resolve_user_id(
        username=user.get("username"),
        email=user.get("email"),
    )
    if not tautulli_user_id:
        logger.info("No Tautulli account matched for user %s; no enrichment.", user_key)
        return 0

    if progress_callback:
        progress_callback("Fetching Tautulli history...", 0.8)

    history = tautulli.get_history(tautulli_user_id, machine_id=machine_id)
    if not history:
        logger.info("No Tautulli history for user %s; no enrichment.", user_key)
        return 0

    count = 0
    metadata_cache: Dict[str, Dict[str, Any]] = {}
    # Tally view counts per target (movie or parent-show) across this full
    # history pull, since Tautulli history is per-playback-event, not per-item.
    target_tally: Dict[str, Dict[str, Any]] = {}

    for row in history:
        media_type = row.get("media_type", "movie")
        viewed_at = _epoch_to_iso(row.get("date") or row.get("started"))
        episode_key = str(row.get("rating_key") or "")

        # Record the raw play event against the actual item watched.
        if episode_key:
            record_watch_event({
                "user_key": user_key,
                "item_id": episode_key,
                "title": row.get("full_title") or row.get("title") or "",
                "media_type": media_type,
                "viewed_at": viewed_at,
                "account_id": tautulli_user_id,
                "duration_watched": row.get("duration", 0) or 0,
                "source": "tautulli",
            })

        # For episodes, attribute the watch to the parent SHOW (genres/creators
        # live at show level). Roll up to the grandparent rating key.
        if media_type == "episode" and row.get("grandparent_rating_key"):
            target_key = str(row.get("grandparent_rating_key"))
            target_type = "show"
            fallback_title = row.get("grandparent_title") or ""
        else:
            target_key = episode_key or str(row.get("grandparent_rating_key") or "")
            target_type = media_type
            fallback_title = row.get("full_title") or row.get("title") or ""

        if not target_key:
            continue

        if target_key not in metadata_cache:
            metadata_cache[target_key] = tautulli.get_metadata(target_key)
        meta = metadata_cache[target_key]

        tally = target_tally.get(target_key)
        if tally is None:
            tally = {
                "media_type": target_type,
                "title": meta.get("title") or fallback_title,
                "year": meta.get("year") or row.get("year"),
                "meta": meta,
                "view_count": 0,
                "last_viewed_at": viewed_at,
            }
            target_tally[target_key] = tally
        tally["view_count"] += 1
        if viewed_at and (not tally["last_viewed_at"] or viewed_at > tally["last_viewed_at"]):
            tally["last_viewed_at"] = viewed_at

        count += 1

    for target_key, tally in target_tally.items():
        meta = tally["meta"]
        upsert_user_media(user_key, {
            "item_id": target_key,
            "media_type": tally["media_type"],
            "title": tally["title"],
            "year": tally["year"],
            "genres": meta.get("genres", []),
            "directors": meta.get("directors", []),
            "actors": meta.get("actors", []),
            "imdb_id": meta.get("imdb_id"),
            "tmdb_id": meta.get("tmdb_id"),
            "tvdb_id": meta.get("tvdb_id"),
            "view_count": tally["view_count"],
            "last_viewed_at": tally["last_viewed_at"],
        })

    logger.info("Recorded %s Tautulli history events for user %s.", count, user_key)
    return count


def sync_user_history(
    user_key: str,
    progress_callback: Optional[Callable[[str, float], None]] = None,
) -> Dict[str, Any]:
    """
    Build a user's watch history from Plex (primary) + Tautulli (optional enrichment).

    Plex server-side watch history is always used when available; Tautulli, when
    configured and matched, augments it. Deduplication is handled at the DB layer
    (event_key + INSERT OR IGNORE, and per-user upserts keyed by item_id).
    """
    init_db()
    result = {"plex_events": 0, "tautulli_events": 0, "total": 0}

    user = get_user(user_key)
    if not user:
        result["reason"] = "unknown_user"
        return result

    if progress_callback:
        progress_callback("Syncing Plex playback history...", 0.65)
    result["plex_events"] = sync_plex_user_history(user_key, progress_callback=progress_callback)

    if progress_callback:
        progress_callback("Enriching with Tautulli watch history...", 0.82)
    result["tautulli_events"] = sync_tautulli_user_history(user_key, progress_callback=progress_callback)
    result["total"] = result["plex_events"] + result["tautulli_events"]

    if progress_callback:
        progress_callback("Updating recommendations cache...", 0.95)
    clear_recommendations_cache()
    if progress_callback:
        progress_callback("History sync completed!", 1.0)

    logger.info(
        "User %s history: %s Plex + %s Tautulli events.",
        user_key, result["plex_events"], result["tautulli_events"],
    )
    return result


def sync_plex_data(
    user_key: Optional[str] = None,
    progress_callback: Optional[Callable[[str, float], None]] = None,
) -> Dict[str, Any]:
    """
    Full sync: refresh shared library metadata, then (if a user is given) build
    that user's watch history from Plex + optional Tautulli enrichment.
    """
    stats = sync_library_metadata(progress_callback=progress_callback)
    if user_key:
        sync_user_history(user_key, progress_callback=progress_callback)
        stats = get_stats(user_key)
    return stats


def discover_and_register_shared_users() -> List[Dict[str, Any]]:
    """Discover shared users from Plex and persist them so their history can be synced."""
    from plex_recommender.auth import discover_shared_users
    from plex_recommender.db import get_admin, upsert_discovered_user
    admin = get_admin()
    token = (admin.get("plex_token") if admin else None) or settings.plex_token
    if not token:
        return []
    try:
        users = discover_shared_users(token, settings.plex_machine_id)
        for u in users:
            upsert_discovered_user(u)
        logger.info("Discovered and registered %s shared users.", len(users))
        return users
    except Exception as e:
        logger.warning("Could not discover shared users: %s", e)
        return []

