import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

from plex_recommender import __version__
from plex_recommender.config import settings, _parse_bool
from plex_recommender.logs import log_handler, LogHandler
from plex_recommender.db import init_db
from plex_recommender.db.watch import (
    get_stats, has_user_history, get_enriched_watch_events, get_watch_source_breakdown,
    get_user_seen_index,
)
from plex_recommender.db.media import get_library_availability_index
from plex_recommender.db.recommendations import (
    set_setting, get_setting, clear_recommendations_cache, clear_user_recommendations_cache,
)
from plex_recommender.db.users import (
    admin_exists, get_admin, get_user, get_all_users, create_or_update_user,
    get_database_stats, get_user_data_summary, clear_user_data,
    touch_last_seen, mark_onboarded,
)
from plex_recommender.db.jobs import start_job, finish_job, get_job_history, clear_job_history
from plex_recommender.db.engagement import (
    dismiss_item, undismiss_item, get_user_dismissals,
    record_vote, remove_vote, get_user_votes, get_user_vote_items,
    get_user_action_counts,
    add_to_watchlist, remove_from_watchlist, is_in_watchlist,
    get_user_watchlist, get_user_watchlist_ids, get_user_watchlist_count,
)
from plex_recommender.db.logs import get_system_logs, truncate_system_logs, clear_all_system_logs
from plex_recommender.auth import (
    create_plex_pin, check_plex_pin, verify_plex_connection,
    get_plex_account, get_server_machine_id, check_user_access,
)
from plex_recommender.sync import sync_plex_data, sync_user_history
from plex_recommender.analyzer import analyzer
from plex_recommender.recommender import recommender, TONIGHT_MOODS
from plex_recommender.community import community_service
from plex_recommender.discovery.tmdb import to_canonical_genre, get_tmdb_categories, tmdb
from plex_recommender.discovery.overseerr import overseerr
from plex_recommender.discovery.tautulli import tautulli
from plex_recommender.jobs import job_manager, JOB_DEFINITIONS
from plex_recommender.poster_cache import get_cached_poster_url, get_cached_poster_url_from_source, _cache_dir as get_poster_cache_dir
from plex_recommender.health import get_unhealthy

logger = logging.getLogger("plex_recommender.web")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["app_version"] = __version__
templates.env.globals["get_unhealthy_integrations"] = get_unhealthy

SESSION_MAX_AGE = 3600  # 1 hour

scheduler = BackgroundScheduler()
sync_lock = job_manager._lock
sync_state = job_manager.sync_state

def scheduled_sync_job():
    """Background scheduled sync job: refresh shared metadata, discover users, sync history, chain recs."""
    if not settings.plex_token:
        logger.info("Skipping scheduled sync: No Plex token.")
        return
    logger.info("Starting scheduled background sync...")
    job_manager.run_sync(trigger="scheduled", chain_recommendations=True)


def scheduled_recommendations_job():
    """Background scheduled recommendations pre-fetch job."""
    if not tmdb.is_configured():
        logger.info("Skipping scheduled recommendations: No TMDb API key.")
        return
    logger.info("Starting scheduled recommendations pre-fetch...")
    job_manager.run_recommendations(trigger="scheduled")


def scheduled_log_cleanup_job():
    """Background scheduled job: truncate logs older than 7 days."""
    logger.info("Starting scheduled log truncation (7-day retention)...")
    job_manager.run_truncate_logs(trigger="scheduled", retention_days=7)


def scheduled_poster_cleanup_job():
    """Background scheduled job: delete cached posters not accessed within the TTL window."""
    logger.info("Starting scheduled poster cache cleanup...")
    job_manager.run_poster_cleanup(trigger="scheduled")


def scheduled_healthcheck_job():
    """Background scheduled job: test TMDb/Overseerr/Tautulli connectivity."""
    logger.info("Starting scheduled integration health check...")
    job_manager.run_healthcheck(trigger="scheduled")


def get_scheduled_jobs_info():
    """Return configured background jobs with their scheduler status and next run times."""
    jobs_info = []

    # 1. Sync Job
    sync_job = scheduler.get_job("plex_sync_job") if scheduler.running else None
    next_sync = sync_job.next_run_time.isoformat() if sync_job and sync_job.next_run_time else None
    sync_st = job_manager.get_job_state("sync")
    jobs_info.append({
        "id": "sync",
        "name": JOB_DEFINITIONS["sync"]["name"],
        "description": JOB_DEFINITIONS["sync"]["description"],
        "interval_hours": settings.auto_sync_hours,
        "next_run": next_sync,
        "state": sync_st,
    })

    # 2. Recommendations Job
    rec_job = scheduler.get_job("plex_recommendations_job") if scheduler.running else None
    next_rec = rec_job.next_run_time.isoformat() if rec_job and rec_job.next_run_time else None
    rec_st = job_manager.get_job_state("recommendations")
    jobs_info.append({
        "id": "recommendations",
        "name": JOB_DEFINITIONS["recommendations"]["name"],
        "description": JOB_DEFINITIONS["recommendations"]["description"],
        "interval_hours": settings.auto_recommendations_hours,
        "next_run": next_rec,
        "state": rec_st,
    })

    # 3. Truncate Logs Job
    log_job = scheduler.get_job("plex_log_cleanup_job") if scheduler.running else None
    next_log = log_job.next_run_time.isoformat() if log_job and log_job.next_run_time else None
    log_st = job_manager.get_job_state("truncate_logs")
    jobs_info.append({
        "id": "truncate_logs",
        "name": JOB_DEFINITIONS["truncate_logs"]["name"],
        "description": JOB_DEFINITIONS["truncate_logs"]["description"],
        "interval_hours": 24,
        "next_run": next_log,
        "state": log_st,
    })

    # 4. Poster Cache Cleanup Job
    poster_job = scheduler.get_job("plex_poster_cleanup_job") if scheduler.running else None
    next_poster = poster_job.next_run_time.isoformat() if poster_job and poster_job.next_run_time else None
    poster_st = job_manager.get_job_state("poster_cleanup")
    jobs_info.append({
        "id": "poster_cleanup",
        "name": JOB_DEFINITIONS["poster_cleanup"]["name"],
        "description": JOB_DEFINITIONS["poster_cleanup"]["description"],
        "interval_hours": 24,
        "next_run": next_poster,
        "state": poster_st,
    })

    # 5. Integration Health Check Job
    health_job = scheduler.get_job("plex_healthcheck_job") if scheduler.running else None
    next_health = health_job.next_run_time.isoformat() if health_job and health_job.next_run_time else None
    health_st = job_manager.get_job_state("healthcheck")
    jobs_info.append({
        "id": "healthcheck",
        "name": JOB_DEFINITIONS["healthcheck"]["name"],
        "description": JOB_DEFINITIONS["healthcheck"]["description"],
        "interval_hours": settings.healthcheck_interval_hours,
        "next_run": next_health,
        "state": health_st,
    })

    return jobs_info


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Attach LogHandler to persist logs with 7-day retention
    pkg_logger = logging.getLogger("plex_recommender")
    if not any(isinstance(h, LogHandler) for h in pkg_logger.handlers):
        pkg_logger.addHandler(log_handler)

    if not scheduler.running:
        scheduler.add_job(
            scheduled_sync_job,
            "interval",
            hours=settings.auto_sync_hours,
            id="plex_sync_job",
            replace_existing=True
        )
        scheduler.add_job(
            scheduled_recommendations_job,
            "interval",
            hours=settings.auto_recommendations_hours,
            id="plex_recommendations_job",
            replace_existing=True
        )
        scheduler.add_job(
            scheduled_log_cleanup_job,
            "interval",
            hours=24,
            id="plex_log_cleanup_job",
            replace_existing=True
        )
        scheduler.add_job(
            scheduled_poster_cleanup_job,
            "interval",
            hours=24,
            id="plex_poster_cleanup_job",
            replace_existing=True
        )
        scheduler.add_job(
            scheduled_healthcheck_job,
            "interval",
            hours=settings.healthcheck_interval_hours,
            id="plex_healthcheck_job",
            replace_existing=True
        )
        scheduler.start()
        logger.info(
            f"Started background scheduler (sync every {settings.auto_sync_hours}h, recs every {settings.auto_recommendations_hours}h, "
            f"log truncation every 24h, poster cleanup every 24h, healthcheck every {settings.healthcheck_interval_hours}h)."
        )
    yield
    if scheduler.running:
        scheduler.shutdown()

_enable_api_docs = _parse_bool(os.getenv("ENABLE_API_DOCS"), default=False)
app = FastAPI(
    title="Mindarr",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if _enable_api_docs else None,
    redoc_url="/redoc" if _enable_api_docs else None,
    openapi_url="/openapi.json" if _enable_api_docs else None,
)


_UNSET_USER = object()


def get_current_user(request: Request) -> Optional[dict]:
    """Resolve the logged-in user from the session, or None (cached per request)."""
    cached = getattr(request.state, "_current_user", _UNSET_USER)
    if cached is not _UNSET_USER:
        return cached
    user_key = request.session.get("user_key")
    user = get_user(user_key) if user_key else None
    request.state._current_user = user
    return user


def recommendations_ready(user: Optional[dict]) -> bool:
    """True once the user's initial watch history sync has produced data."""
    if not user:
        return False
    return has_user_history(user["user_key"])


def first_name_of(user: Optional[dict]) -> str:
    """Best-effort first name for a personalized greeting."""
    if not user:
        return ""
    name = user.get("title") or user.get("username") or ""
    return name.split(" ")[0] if name else ""


def needs_first_visit_tour(user: Optional[dict]) -> bool:
    """Shared (non-admin) users see a one-time welcome tour; admins get the /welcome wizard instead."""
    return bool(user and not user.get("is_admin") and not user.get("onboarded_at"))


def admin_setup_incomplete() -> bool:
    """True while the admin hasn't finished the essentials: TMDb key, linked Plex server, first sync."""
    return bool(
        not tmdb.is_configured()
        or not settings.plex_machine_id
        or not get_setting("last_sync_time")
    )


# Paths the onboarding redirect gate never intercepts.
_ONBOARDING_GATE_ALLOWLIST = (
    "/welcome", "/logout", "/login", "/api/", "/static/",
    "/manifest.json", "/manifest.webmanifest", "/sw.js", "/offline",
)


# API routes that are legitimately accessible before authentication (OAuth PIN flow).
_PUBLIC_API_ROUTES = frozenset({"/api/auth/start", "/api/auth/poll"})


_CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "frame-src https://www.youtube-nocookie.com https://www.youtube.com; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Attach baseline OWASP A05 HTTP security headers to every response."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", _CSP_POLICY)
    return response


@app.middleware("http")
async def same_origin_csrf_guard(request: Request, call_next):
    """Reject cross-origin state-changing requests via Sec-Fetch-Site, Origin, or Referer."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if sec_fetch_site == "cross-site":
            logger.warning("Blocked cross-site %s %s (Sec-Fetch-Site=cross-site)", request.method, request.url.path)
            return JSONResponse({"error": "cross_origin_forbidden"}, status_code=403)

        target_host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc
        ).split(",")[0].strip().lower()

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        source_header = origin if origin and origin != "null" else referer
        if source_header:
            parsed = urlparse(source_header)
            source_host = (parsed.netloc or "").strip().lower()
            if not source_host or (target_host and source_host != target_host):
                logger.warning(
                    "Blocked cross-origin %s %s (source=%s, target=%s)",
                    request.method, request.url.path, source_host, target_host,
                )
                return JSONResponse({"error": "cross_origin_forbidden"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def api_auth_guard(request: Request, call_next):
    """Enforce authentication across all /api/ routes except pre-login OAuth endpoints."""
    path = request.url.path
    if path.startswith("/api/") and path not in _PUBLIC_API_ROUTES:
        if not get_current_user(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def onboarding_gate(request: Request, call_next):
    """Redirect an admin who hasn't finished first-run setup to /welcome.

    Only applies to admins; shared users are never redirected here. Excludes
    the wizard route itself, auth/logout, and all API/static endpoints so the
    app never gets stuck in a redirect loop.
    """
    path = request.url.path
    if not any(path == p or path.startswith(p) for p in _ONBOARDING_GATE_ALLOWLIST):
        user = get_current_user(request)
        if user and user.get("is_admin") and not user.get("onboarded_at") and admin_setup_incomplete():
            return RedirectResponse(url="/welcome", status_code=303)
    return await call_next(request)


# Registered after onboarding_gate so SessionMiddleware wraps it (outermost),
# ensuring request.session is populated before the gate reads it.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=settings.session_https_only,
)


@app.get("/static/posters/{filename}")
def serve_cached_poster(filename: str):
    """Serve cached TMDb posters from the persistent config/posters directory."""
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        raise HTTPException(status_code=404, detail="Poster not found.")
    poster_path = get_poster_cache_dir() / safe_name
    if not poster_path.is_file():
        raise HTTPException(status_code=404, detail="Poster not found.")
    return FileResponse(str(poster_path), media_type="image/jpeg")


# Mount static if directory exists
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/manifest.json")
@app.get("/manifest.webmanifest")
def pwa_manifest():
    """Web app manifest, required for Chrome/Android/desktop install prompts."""
    return FileResponse(
        str(static_dir / "manifest.json"),
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def pwa_service_worker():
    """Service worker script. Served from the root so its scope covers the
    whole app (`Service-Worker-Allowed: /`), even though the file physically
    lives under /static."""
    return FileResponse(
        str(static_dir / "sw.js"),
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


@app.get("/offline", response_class=HTMLResponse)
def pwa_offline(request: Request):
    """Offline fallback page, precached by the service worker for use when
    the network is unavailable."""
    return templates.TemplateResponse(request, "offline.html", {})


def is_low_bandwidth(request: Request) -> bool:
    """Check if low-bandwidth mode is active via cookie or query param."""
    if request.cookies.get("low_bandwidth") == "true":
        return True
    if request.query_params.get("low_bandwidth") in ("1", "true"):
        return True
    return False

@app.get("/", response_class=HTMLResponse)
def index_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    profile = analyzer.analyze(user["user_key"])
    stats = get_stats(user["user_key"])

    low_bw = is_low_bandwidth(request)
    for item in profile.get("top_movies", []):
        item["poster_url"] = get_cached_poster_url(item.get("tmdb_id"), "movie", low_bw)
    for item in profile.get("top_shows", []):
        item["poster_url"] = get_cached_poster_url(item.get("tmdb_id"), "show", low_bw)

    # "Welcome back" caption: only surfaced once, right after login, and only
    # if the user was previously seen more than 24h ago.
    welcome_back_at = None
    previous_seen = request.session.pop("previous_last_seen", None)
    if previous_seen:
        try:
            prev_dt = datetime.fromisoformat(str(previous_seen).replace("Z", "+00:00"))
            if prev_dt.tzinfo is None:
                prev_dt = prev_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - prev_dt > timedelta(hours=24):
                welcome_back_at = previous_seen
        except Exception:
            pass

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "profile": profile,
            "stats": stats,
            "settings": settings,
            "sync_state": sync_state,
            "current_user": user,
            "recs_ready": recommendations_ready(user),
            "low_bandwidth": is_low_bandwidth(request),
            "first_name": first_name_of(user),
            "show_first_visit_tour": needs_first_visit_tour(user),
            "welcome_back_at": welcome_back_at,
        }
    )

@app.get("/community", response_class=HTMLResponse)
@app.get("/community/", response_class=HTMLResponse)
def community_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    community_data = community_service.get_community_data(user)

    return templates.TemplateResponse(
        request=request,
        name="community.html",
        context={
            "community": community_data,
            "settings": settings,
            "sync_state": sync_state,
            "current_user": user,
            "recs_ready": recommendations_ready(user),
            "low_bandwidth": is_low_bandwidth(request),
            "first_name": first_name_of(user),
            "show_first_visit_tour": needs_first_visit_tour(user),
        }
    )

@app.get("/sources", response_class=HTMLResponse)
@app.get("/sources/", response_class=HTMLResponse)
@app.get("/history", response_class=HTMLResponse)
@app.get("/history/", response_class=HTMLResponse)
def sources_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    user_key = user["user_key"]
    events = get_enriched_watch_events(user_key, limit=100)
    source_stats = get_watch_source_breakdown(user_key)

    total_duration_sec = sum(ev.get("duration_seconds", 0) for ev in events)
    total_hours = round(total_duration_sec / 3600, 1) if total_duration_sec > 0 else 0

    return templates.TemplateResponse(
        request=request,
        name="sources.html",
        context={
            "events": events,
            "source_stats": source_stats,
            "total_hours": total_hours,
            "settings": settings,
            "sync_state": sync_state,
            "current_user": user,
            "recs_ready": recommendations_ready(user),
            "low_bandwidth": is_low_bandwidth(request),
            "first_name": first_name_of(user),
            "show_first_visit_tour": needs_first_visit_tour(user),
        }
    )

@app.get("/recommendations", response_class=HTMLResponse)
def recommendations_page(
    request: Request,
    type: str = "all",
    genre: Optional[str] = None,
    language: Optional[str] = "en",
    min_rating: float = 7.0,
    limit: int = 18,
    available: bool = False,
    include_kids: bool = False,
    force_refresh: bool = False,
    view: str = "discover",
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    # Gate the route until the initial watch history sync has produced data.
    if not recommendations_ready(user):
        return RedirectResponse(url="/?recs_not_ready=1", status_code=303)

    # Render the page shell immediately (with skeletons). The heavy
    # recommendation computation is fetched in the background via
    # /api/recommendations so the page never blocks on TMDb/Plex calls.
    profile = analyzer.analyze(user["user_key"])
    selected_lang = language or "en"

    # Filter categories so ONLY categories that appear in TMDb are shown
    valid_tmdb_cats = set(get_tmdb_categories(type))
    category_options = []
    seen_cat = set()
    for g in profile.get("top_genres", []):
        raw_g = g.get("genre", "") if isinstance(g, dict) else str(g)
        canon = to_canonical_genre(raw_g)
        if canon and canon in valid_tmdb_cats and canon not in seen_cat:
            seen_cat.add(canon)
            category_options.append(canon)

    tmdb_categories_map = {
        "all": get_tmdb_categories("all"),
        "movie": get_tmdb_categories("movie"),
        "show": get_tmdb_categories("show"),
    }

    # Surface only the setup-blocking errors up front; data errors are handled
    # by the async endpoint and rendered client-side.
    error_msg = None
    if not tmdb.is_configured():
        error_msg = "TMDb API Key is not configured. Please ask your administrator to add it in Settings."
    elif not profile.get("has_data"):
        error_msg = "No watch history available for your account. Please sync your Plex watch history first."

    selected_view = view if view in ("discover", "watchlist", "together") else "discover"
    together_peers = _list_together_peers(user)
    my_watch_code = _user_watch_code(user["user_key"])

    return templates.TemplateResponse(
        request=request,
        name="recommendations.html",
        context={
            "results": {"success": False, "recommendations": []},
            "profile": profile,
            "category_options": category_options,
            "tmdb_categories_map": tmdb_categories_map,
            "selected_type": type,
            "selected_genre": genre,
            "selected_language": selected_lang,
            "min_rating": min_rating,
            "selected_available": available,
            "selected_include_kids": include_kids,
            "error_msg": error_msg,
            "settings": settings,
            "current_user": user,
            "recs_ready": True,
            "has_overseerr": overseerr.is_configured(),
            "low_bandwidth": is_low_bandwidth(request),
            "force_refresh": force_refresh,
            "user_votes": get_user_votes(user["user_key"]),
            "watchlist_count": get_user_watchlist_count(user["user_key"]),
            "selected_view": selected_view,
            "together_peers": together_peers,
            "my_watch_code": my_watch_code,
            "tonight_moods": TONIGHT_MOODS,
            "first_name": first_name_of(user),
            "show_first_visit_tour": needs_first_visit_tour(user),
        }
    )


@app.get("/api/recommendations", response_class=JSONResponse)
def api_recommendations(
    request: Request,
    type: str = "all",
    genre: Optional[str] = None,
    language: Optional[str] = "en",
    min_rating: float = 7.0,
    limit: int = 18,
    page: int = 1,
    available: bool = False,
    include_kids: bool = False,
    force_refresh: bool = False,
    stage: str = "full"
):
    """Compute recommendations and return rendered card HTML + cache metadata.

    Called in the background by the recommendations page so the initial load
    isn't blocked on TMDb/Plex discovery. Supports progressive stages ('fast' and 'full').
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    profile = analyzer.analyze(user["user_key"])
    selected_lang = language or "en"
    results = {"success": False, "recommendations": []}
    error_msg = None

    if not tmdb.is_configured():
        error_msg = "TMDb API Key is not configured. Please ask your administrator to add it in Settings."
    elif not profile.get("has_data"):
        error_msg = "No watch history available for your account. Please sync your Plex watch history first."
    else:
        try:
            results = recommender.get_recommendations(
                user_key=user["user_key"],
                media_type=type,
                limit=limit,
                page=page,
                min_rating=min_rating,
                genre_filter=genre,
                language=selected_lang,
                only_available=available,
                include_kids=include_kids,
                force_refresh=force_refresh,
                stage=stage
            )
        except Exception as e:
            logger.error(f"Recommendation generation error: {e}")
            error_msg = "Failed to generate recommendations. Please check your configuration and try again."

    low_bw = is_low_bandwidth(request)
    for item in results.get("recommendations", []):
        cached = get_cached_poster_url_from_source(
            item.get("tmdb_id"), item.get("media_type"), item.get("poster_url"), low_bw
        )
        if cached:
            item["poster_url"] = cached

    user_counts = get_user_action_counts(user["user_key"])
    watchlist_ids = get_user_watchlist_ids(user["user_key"])
    html = templates.get_template("_recommendation_cards.html").render(
        {
            "request": request,
            "results": results,
            "error_msg": error_msg,
            "has_overseerr": overseerr.is_configured(),
            "low_bandwidth": low_bw,
            "user_votes": get_user_votes(user["user_key"]),
            "watchlist_ids": watchlist_ids,
            "is_watchlist_view": False,
            "pool_stats": user_counts,
        }
    )

    return JSONResponse({
        "html": html,
        "has_results": bool(results.get("recommendations")),
        "from_cache": bool(results.get("from_cache")),
        "is_complete": bool(results.get("is_complete", True)),
        "stage": results.get("stage", stage),
        "page": results.get("page", 1),
        "total_pages": results.get("total_pages", 1),
        "unseen_count": results.get("unseen_count", 0),
        "watchlist_count": len(watchlist_ids),
        "exhaustion_reason": results.get("exhaustion_reason"),
        "error": error_msg,
    })


@app.post("/api/recommendations/clear-cache")
def api_clear_recommendations_cache(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    clear_user_recommendations_cache(user["user_key"])
    logger.info("User '%s' cleared their recommendations cache.", user.get("username") or user.get("user_key"))
    return {"success": True, "message": "Recommendations cache cleared"}


_TMDB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ALLOWED_MEDIA_TYPES = frozenset({"movie", "show", "tv", "episode"})
_ALLOWED_DISMISS_REASONS = frozenset({"not_interested", "already_watched"})


def _validate_tmdb_id(tmdb_id: str) -> str:
    cleaned = tmdb_id.strip()
    if not _TMDB_ID_RE.match(cleaned):
        raise HTTPException(status_code=400, detail="Invalid tmdb_id format.")
    return cleaned


def _validate_media_type(media_type: str) -> str:
    cleaned = media_type.strip().lower()
    if cleaned not in _ALLOWED_MEDIA_TYPES:
        raise HTTPException(status_code=400, detail="Invalid media_type.")
    return cleaned


def _sanitize_title(title: str) -> str:
    return title.replace("\r", " ").replace("\n", " ").replace("\x00", "").strip()[:200]


@app.post("/api/recommendations/vote")
def api_vote_recommendation(
    request: Request,
    tmdb_id: str = Form(...),
    media_type: str = Form("movie"),
    title: str = Form(""),
    year: Optional[int] = Form(None),
    vote: int = Form(1),
):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    tmdb_id = _validate_tmdb_id(tmdb_id)
    media_type = _validate_media_type(media_type)
    title = _sanitize_title(title)

    v = 1 if int(vote) > 0 else -1
    record_vote(
        user_key=user["user_key"],
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=title,
        year=year,
        vote=v
    )
    if v == 1:
        logger.info(
            "User '%s' upvoted recommendation '%s' (%s, TMDb: %s) [vote: +1].",
            user.get("username") or user.get("user_key"),
            title or tmdb_id,
            media_type,
            tmdb_id
        )
    else:
        logger.info(
            "User '%s' downvoted recommendation '%s' (%s, TMDb: %s) [vote: -1].",
            user.get("username") or user.get("user_key"),
            title or tmdb_id,
            media_type,
            tmdb_id
        )
    return JSONResponse({
        "success": True,
        "tmdb_id": tmdb_id,
        "vote": v,
        "watchlist_count": get_user_watchlist_count(user["user_key"]),
    })


@app.post("/api/recommendations/unvote")
def api_unvote_recommendation(
    request: Request,
    tmdb_id: str = Form(...)
):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    tmdb_id = _validate_tmdb_id(tmdb_id)
    remove_vote(user_key=user["user_key"], tmdb_id=tmdb_id)
    logger.info(
        "User '%s' removed vote on recommendation TMDb: %s.",
        user.get("username") or user.get("user_key"),
        tmdb_id
    )
    return JSONResponse({"success": True, "tmdb_id": tmdb_id})


@app.post("/api/recommendations/dismiss")
def api_dismiss_recommendation(
    request: Request,
    tmdb_id: str = Form(...),
    media_type: str = Form("movie"),
    title: str = Form(""),
    year: Optional[int] = Form(None),
    reason: str = Form("not_interested"),
):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    tmdb_id = _validate_tmdb_id(tmdb_id)
    media_type = _validate_media_type(media_type)
    title = _sanitize_title(title)
    if reason not in _ALLOWED_DISMISS_REASONS:
        raise HTTPException(status_code=400, detail="Invalid dismissal reason.")

    if reason == "not_interested":
        record_vote(
            user_key=user["user_key"],
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=title,
            year=year,
            vote=-1
        )
        reason_label = "not interested (thumbs down)"
    else:
        dismiss_item(
            user_key=user["user_key"],
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=title,
            year=year,
            reason=reason
        )
        reason_label = "watched outside Plex (marked as seen)" if reason == "already_watched" else reason

    logger.info(
        "User '%s' dismissed recommendation '%s' (%s, TMDb: %s) as %s.",
        user.get("username") or user.get("user_key"),
        title or tmdb_id,
        media_type,
        tmdb_id,
        reason_label
    )
    return JSONResponse({
        "success": True,
        "tmdb_id": tmdb_id,
        "reason": reason,
        "watchlist_count": get_user_watchlist_count(user["user_key"]),
    })


@app.post("/api/recommendations/undismiss")
def api_undismiss_recommendation(
    request: Request,
    tmdb_id: str = Form(...)
):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    tmdb_id = _validate_tmdb_id(tmdb_id)
    undismiss_item(user_key=user["user_key"], tmdb_id=tmdb_id)
    logger.info(
        "User '%s' restored recommendation TMDb: %s to active pools.",
        user.get("username") or user.get("user_key"),
        tmdb_id
    )
    return JSONResponse({"success": True, "tmdb_id": tmdb_id})


@app.get("/api/recommendations/dismissed")
def api_get_dismissed_recommendations(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    dismissals = get_user_dismissals(user["user_key"])
    votes = get_user_vote_items(user["user_key"])
    return JSONResponse({"success": True, "dismissals": dismissals, "votes": votes})


def _user_watch_code(user_key: str) -> str:
    """Return a stable, non-guessable Watch Code (MIND-XXXXXXXX) for a user."""
    digest = hmac.new(
        settings.session_secret.encode("utf-8"),
        f"watch-code:{user_key}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:8].upper()
    return f"MIND-{digest[:4]}-{digest[4:]}"


def _normalize_watch_code(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", (raw or "").upper())
    if cleaned.startswith("MIND"):
        cleaned = cleaned[4:]
    return cleaned[:8]


def _peer_display_name(current_user: dict, peer_user: dict) -> tuple[str, bool]:
    """Return (display_name, is_anonymized) using real Plex user names."""
    name = peer_user.get("title") or peer_user.get("username") or "Community Member"
    return name, False


def _list_together_peers(current_user: dict) -> list[dict]:
    """Return peers with watch history eligible for Watch Together."""
    my_uk = str(current_user.get("user_key") or "")
    peers = []
    for u in get_all_users():
        uk = str(u.get("user_key") or "")
        if not uk or uk == my_uk:
            continue
        if not has_user_history(uk):
            continue
        dname, is_anon = _peer_display_name(current_user, u)
        peers.append({
            "user_key": uk,
            "display_name": dname,
            "is_anonymized": is_anon,
        })
    return peers


def _resolve_peer_keys(current_user: dict, peers_raw: Optional[str]) -> list[str]:
    """Validate and resolve comma-separated peer user_keys (or watch codes) into user_keys including current user."""
    my_uk = str(current_user.get("user_key") or "")
    resolved = [my_uk]
    if not peers_raw:
        return resolved

    all_users = get_all_users()
    by_uk = {str(u.get("user_key") or ""): u for u in all_users if u.get("user_key")}
    by_code = {_normalize_watch_code(_user_watch_code(uk)): uk for uk in by_uk}

    for token in peers_raw.split(","):
        tok = token.strip()
        if not tok:
            continue
        target_uk = None
        if tok in by_uk:
            target_uk = tok
        else:
            norm = _normalize_watch_code(tok)
            if norm in by_code:
                target_uk = by_code[norm]
        if target_uk and target_uk not in resolved:
            resolved.append(target_uk)
        if len(resolved) >= 6:
            break
    return resolved


@app.get("/api/recommendations/pool-stats")
def api_recommendations_pool_stats(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    counts = get_user_action_counts(user["user_key"])
    return JSONResponse({"success": True, **counts})


@app.get("/api/together/peers", response_class=JSONResponse)
def api_together_peers(request: Request):
    """Return Watch Together eligible peers (anonymized for non-admins) and current user's Watch Code."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return JSONResponse({
        "success": True,
        "my_watch_code": _user_watch_code(user["user_key"]),
        "peers": _list_together_peers(user),
    })


@app.post("/api/together/resolve-code", response_class=JSONResponse)
def api_together_resolve_code(
    request: Request,
    code: str = Form(...),
):
    """Resolve a friend's Watch Code (MIND-XXXX-XXXX) into a blendable peer entry."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    target_norm = _normalize_watch_code(code)
    if len(target_norm) != 8:
        return JSONResponse({"success": False, "error": "Enter a valid 8-character Watch Code (e.g. MIND-XXXX-XXXX)."}, status_code=400)

    my_uk = str(user.get("user_key") or "")
    matched = None
    for u in get_all_users():
        uk = str(u.get("user_key") or "")
        if not uk:
            continue
        u_norm = _normalize_watch_code(_user_watch_code(uk))
        if hmac.compare_digest(target_norm, u_norm):
            matched = u
            break

    if not matched:
        return JSONResponse({"success": False, "error": "Watch Code not found on this server."}, status_code=404)

    uk = str(matched["user_key"])
    if uk == my_uk:
        return JSONResponse({"success": False, "error": "That's your own Watch Code — pick a friend's code!"}, status_code=400)
    if not has_user_history(uk):
        return JSONResponse({"success": False, "error": "That viewer doesn't have synced watch history yet."}, status_code=400)

    # Explicit code exchange: reveal friendly title/username if available, else anonymized name
    friendly = matched.get("title") or matched.get("username")
    dname, _ = _peer_display_name(user, matched)
    return JSONResponse({
        "success": True,
        "peer": {
            "user_key": uk,
            "display_name": friendly or dname,
        },
    })


@app.get("/api/recommendations/together", response_class=JSONResponse)
def api_recommendations_together(
    request: Request,
    peers: str = "",
    type: str = "all",
    genre: Optional[str] = None,
    language: Optional[str] = "en",
    min_rating: float = 7.0,
    limit: int = 18,
    page: int = 1,
    available: bool = False,
    include_kids: bool = False,
    force_refresh: bool = False,
):
    """Compute blended Watch Together recommendations unseen by all selected viewers."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    group_keys = _resolve_peer_keys(user, peers)
    if len(group_keys) < 2:
        return JSONResponse({
            "html": "",
            "has_results": False,
            "needs_peers": True,
            "group_size": len(group_keys),
            "shared_genres": [],
            "watchlist_count": get_user_watchlist_count(user["user_key"]),
            "error": None,
        })

    if not tmdb.is_configured():
        return JSONResponse({
            "html": "",
            "has_results": False,
            "error": "TMDb API Key is not configured. Please ask your administrator to add it in Settings.",
        })

    results = recommender.get_group_recommendations(
        user_keys=group_keys,
        media_type=type,
        limit=limit,
        page=page,
        min_rating=min_rating,
        genre_filter=genre,
        language=language or "en",
        only_available=available,
        include_kids=include_kids,
        force_refresh=force_refresh,
    )

    low_bw = is_low_bandwidth(request)
    for item in results.get("recommendations", []):
        cached = get_cached_poster_url_from_source(
            item.get("tmdb_id"), item.get("media_type"), item.get("poster_url"), low_bw
        )
        if cached:
            item["poster_url"] = cached

    uk = user["user_key"]
    watchlist_ids = get_user_watchlist_ids(uk)
    html = templates.get_template("_recommendation_cards.html").render(
        {
            "request": request,
            "results": results,
            "error_msg": results.get("error"),
            "has_overseerr": overseerr.is_configured(),
            "low_bandwidth": low_bw,
            "user_votes": get_user_votes(uk),
            "watchlist_ids": watchlist_ids,
            "is_watchlist_view": False,
            "pool_stats": get_user_action_counts(uk),
        }
    )

    return JSONResponse({
        "html": html,
        "has_results": bool(results.get("recommendations")),
        "from_cache": bool(results.get("from_cache")),
        "is_complete": True,
        "page": results.get("page", 1),
        "total_pages": results.get("total_pages", 1),
        "unseen_count": results.get("unseen_count", 0),
        "group_size": results.get("group_size", len(group_keys)),
        "shared_genres": results.get("shared_genres", []),
        "watchlist_count": len(watchlist_ids),
        "exhaustion_reason": results.get("exhaustion_reason"),
        "error": results.get("error"),
    })


_ALLOWED_TONIGHT_SOURCES = frozenset({"discover", "together", "watchlist"})


@app.get("/api/recommendations/tonight", response_class=JSONResponse)
def api_recommendations_tonight(
    request: Request,
    source: str = "discover",
    peers: str = "",
    type: str = "all",
    mood: str = "best",
    available: bool = False,
    exclude_ids: str = "",
):
    """Tonight Picker ('Surprise Me'): return 1-3 mood/runtime-curated finalists."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    src = source.strip().lower()
    if src not in _ALLOWED_TONIGHT_SOURCES:
        raise HTTPException(status_code=400, detail="Invalid Tonight Picker source.")
    if mood not in TONIGHT_MOODS:
        raise HTTPException(status_code=400, detail="Invalid mood preset.")
    mtype = type.strip().lower()
    if mtype not in ("all", "movie", "show"):
        raise HTTPException(status_code=400, detail="Invalid media type.")

    excl: set[str] = set()
    if exclude_ids:
        for raw_id in exclude_ids.split(","):
            tok = raw_id.strip()
            if tok:
                excl.add(_validate_tmdb_id(tok))

    uk = user["user_key"]
    group_keys = _resolve_peer_keys(user, peers) if src == "together" else None
    if src == "together" and (not group_keys or len(group_keys) < 2):
        return JSONResponse({
            "success": False,
            "error": "Select at least 1 friend in Watch Together before spinning Group Picks.",
            "picks": [],
        }, status_code=400)

    if src != "watchlist" and not tmdb.is_configured():
        return JSONResponse({
            "success": False,
            "error": "TMDb API Key is not configured.",
            "picks": [],
        }, status_code=400)

    res = recommender.get_tonight_picks(
        user_key=uk,
        source=src,
        group_user_keys=group_keys,
        media_type=mtype,
        mood=mood,
        only_available=available,
        exclude_ids=excl,
        count=3,
    )

    low_bw = is_low_bandwidth(request)
    wl_ids = get_user_watchlist_ids(uk)
    for pick in res.get("picks", []):
        if low_bw:
            pick["poster_url"] = None
        else:
            cached = get_cached_poster_url_from_source(
                pick.get("tmdb_id"), pick.get("media_type"), pick.get("poster_url"), low_bw
            )
            if cached:
                pick["poster_url"] = cached
        pick["is_in_watchlist"] = str(pick.get("tmdb_id")) in wl_ids

    return JSONResponse({
        **res,
        "has_overseerr": overseerr.is_configured(),
        "low_bandwidth": low_bw,
    })


def _sanitize_poster_url(poster_url: Optional[str]) -> Optional[str]:
    if not poster_url:
        return None
    cleaned = poster_url.strip()
    if cleaned.startswith("/static/posters/") or cleaned.startswith("https://image.tmdb.org/"):
        return cleaned[:300]
    return None


@app.get("/api/media/details", response_class=JSONResponse)
def api_media_details(
    request: Request,
    tmdb_id: str,
    media_type: str = "movie",
):
    """Fetch rich TMDb details (trailer, cast, runtime, TV seasons) + Plex/Overseerr/Watchlist state."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    cleaned_id = _validate_tmdb_id(tmdb_id)
    if not cleaned_id.isdigit():
        raise HTTPException(status_code=400, detail="tmdb_id must be numeric.")
    cleaned_type = _validate_media_type(media_type)
    norm_type = "show" if cleaned_type in ("show", "tv", "episode") else "movie"

    if not tmdb.is_configured():
        return JSONResponse({"success": False, "error": "TMDb API Key is not configured."}, status_code=400)

    details = tmdb.get_details(int(cleaned_id), media_type=norm_type)
    if not details:
        return JSONResponse({"success": False, "error": "Could not load details from TMDb."}, status_code=404)

    # Copy so we don't mutate the cached TMDb dict across users/requests
    payload = dict(details)
    low_bw = is_low_bandwidth(request)
    if low_bw:
        payload["poster_url"] = None
        payload["backdrop_url"] = None
    else:
        cached = get_cached_poster_url_from_source(
            cleaned_id, norm_type, payload.get("poster_url"), low_bw
        )
        if cached:
            payload["poster_url"] = cached

    # Plex availability
    availability_index = get_library_availability_index()
    machine_id = settings.plex_machine_id
    avail_info = None
    if cleaned_id.lower() in availability_index["tmdb"]:
        avail_info = availability_index["tmdb"][cleaned_id.lower()]
    elif payload.get("imdb_id") and payload["imdb_id"].lower() in availability_index["imdb"]:
        avail_info = availability_index["imdb"][payload["imdb_id"].lower()]
    elif payload.get("tvdb_id") and str(payload["tvdb_id"]).lower() in availability_index["tvdb"]:
        avail_info = availability_index["tvdb"][str(payload["tvdb_id"]).lower()]

    payload["available_on_plex"] = avail_info is not None
    payload["plex_url"] = None
    if avail_info and machine_id and avail_info.get("rating_key"):
        payload["plex_url"] = (
            f"https://app.plex.tv/desktop/#!/server/{machine_id}"
            f"/details?key=/library/metadata/{avail_info['rating_key']}"
        )

    # Overseerr status & per-season status
    payload["has_overseerr"] = overseerr.is_configured()
    payload["overseerr_url"] = overseerr.get_web_url(int(cleaned_id), media_type=norm_type) if payload["has_overseerr"] else None
    payload["overseerr_info"] = None
    if payload["has_overseerr"]:
        payload["overseerr_info"] = overseerr.get_media_info(int(cleaned_id), media_type=norm_type)

    # User-specific engagement state
    uk = user["user_key"]
    payload["in_watchlist"] = is_in_watchlist(uk, cleaned_id)
    payload["user_vote"] = get_user_votes(uk).get(cleaned_id)

    return JSONResponse({"success": True, "details": payload})


@app.post("/api/watchlist/toggle", response_class=JSONResponse)
def api_watchlist_toggle(
    request: Request,
    tmdb_id: str = Form(...),
    media_type: str = Form("movie"),
    title: str = Form(""),
    year: Optional[int] = Form(None),
    poster_url: Optional[str] = Form(None),
    rating: Optional[float] = Form(None),
    genres: Optional[str] = Form(None),
    summary: str = Form(""),
):
    """Add or remove a title from the current user's personal watchlist."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    cleaned_id = _validate_tmdb_id(tmdb_id)
    cleaned_type = _validate_media_type(media_type)
    norm_type = "show" if cleaned_type in ("show", "tv", "episode") else "movie"
    uk = user["user_key"]

    if is_in_watchlist(uk, cleaned_id):
        remove_from_watchlist(uk, cleaned_id)
        in_wl = False
        logger.info("User '%s' removed TMDb %s from watchlist.", user.get("username") or uk, cleaned_id)
    else:
        clean_title = _sanitize_title(title)
        clean_summary = summary.replace("\r", " ").replace("\n", " ").replace("\x00", "").strip()[:1000]
        clean_poster = _sanitize_poster_url(poster_url)
        parsed_genres: list = []
        if genres:
            try:
                loaded = json.loads(genres)
                if isinstance(loaded, list):
                    parsed_genres = [str(g).strip()[:40] for g in loaded if str(g).strip()][:10]
            except Exception:
                parsed_genres = [g.strip()[:40] for g in genres.split(",") if g.strip()][:10]
        add_to_watchlist(
            user_key=uk,
            tmdb_id=cleaned_id,
            media_type=norm_type,
            title=clean_title,
            year=year,
            poster_url=clean_poster,
            rating=rating,
            genres=parsed_genres,
            summary=clean_summary,
        )
        in_wl = True
        logger.info("User '%s' saved '%s' (TMDb %s) to watchlist.", user.get("username") or uk, clean_title or cleaned_id, cleaned_id)

    return JSONResponse({
        "success": True,
        "tmdb_id": cleaned_id,
        "in_watchlist": in_wl,
        "watchlist_count": get_user_watchlist_count(uk),
    })


@app.get("/api/watchlist", response_class=JSONResponse)
def api_get_watchlist(
    request: Request,
    type: str = "all",
    available: bool = False,
):
    """Return the user's saved watchlist enriched with live Plex/Overseerr availability and rendered card HTML."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    uk = user["user_key"]
    saved_items = get_user_watchlist(uk)
    total_count = len(saved_items)

    availability_index = get_library_availability_index()
    machine_id = settings.plex_machine_id
    seen_index = get_user_seen_index(uk)
    overseerr_queue = {}
    has_overseerr = overseerr.is_configured()
    if has_overseerr:
        try:
            overseerr_queue = overseerr.get_request_queue()
        except Exception as e:
            logger.debug(f"Failed to load Overseerr queue for watchlist: {e}")

    low_bw = is_low_bandwidth(request)
    enriched = []
    for raw in saved_items:
        mtype = "show" if str(raw.get("media_type")).lower() in ("show", "tv", "episode") else "movie"
        if type in ("movie", "show") and mtype != type:
            continue

        tid = str(raw["tmdb_id"])
        avail_info = availability_index["tmdb"].get(tid.lower())
        available_on_plex = avail_info is not None
        if available and not available_on_plex:
            continue

        plex_url = None
        if avail_info and machine_id and avail_info.get("rating_key"):
            plex_url = (
                f"https://app.plex.tv/desktop/#!/server/{machine_id}"
                f"/details?key=/library/metadata/{avail_info['rating_key']}"
            )

        q_info = overseerr_queue.get(f"{mtype}_{tid}")
        overseerr_requested = bool(q_info and q_info.get("status_code") in (2, 3, 4))
        overseerr_status = q_info.get("status_name", "PENDING") if overseerr_requested else None
        overseerr_status_code = q_info.get("status_code", 2) if overseerr_requested else None

        poster_url = get_cached_poster_url_from_source(tid, mtype, raw.get("poster_url"), low_bw)

        tmdb_url = tmdb.get_web_url(tid, media_type=mtype)
        overseerr_url = overseerr.get_web_url(int(tid), media_type=mtype) if (has_overseerr and tid.isdigit()) else None

        enriched.append({
            "tmdb_id": tid,
            "media_type": mtype,
            "title": raw.get("title") or f"TMDb {tid}",
            "year": raw.get("year"),
            "rating": raw.get("rating") or 0,
            "genres": raw.get("genres") or [],
            "summary": raw.get("summary") or "",
            "poster_url": poster_url,
            "original_language": "en",
            "available_on_plex": available_on_plex,
            "plex_url": plex_url,
            "overseerr_requested": overseerr_requested,
            "overseerr_status": overseerr_status,
            "overseerr_status_code": overseerr_status_code,
            "overseerr_url": overseerr_url,
            "tmdb_url": tmdb_url,
            "imdb_url": None,
            "is_in_watchlist": True,
            "seen_by_user": tid.lower() in seen_index["tmdb"],
            "match_score": None,
            "rationale": "",
            "decision_factors": None,
        })

    results = {
        "success": True,
        "recommendations": enriched,
        "is_complete": True,
    }
    watchlist_ids = {item["tmdb_id"] for item in enriched}
    html = templates.get_template("_recommendation_cards.html").render(
        {
            "request": request,
            "results": results,
            "error_msg": None,
            "has_overseerr": has_overseerr,
            "low_bandwidth": low_bw,
            "user_votes": get_user_votes(uk),
            "watchlist_ids": watchlist_ids,
            "is_watchlist_view": True,
            "pool_stats": get_user_action_counts(uk),
        }
    )

    return JSONResponse({
        "html": html,
        "has_results": bool(enriched),
        "watchlist_count": total_count,
        "filtered_count": len(enriched),
    })

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Administrator access required.")
    stats = get_stats()
    connected, conn_msg = (False, "")
    if settings.plex_token:
        connected, conn_msg = verify_plex_connection()

    tmdb_connected, tmdb_msg = tmdb.test_connection()
    overseerr_connected, overseerr_msg = overseerr.test_connection()
    tautulli_connected, tautulli_msg = tautulli.test_connection()

    # Warn if Tautulli is reachable but monitors a different Plex server than the
    # one the admin linked — its history would not apply to this server.
    tautulli_server_mismatch = False
    if tautulli_connected and not tautulli.monitors_server(settings.plex_machine_id):
        tautulli_server_mismatch = True

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "stats": stats,
            "settings": settings,
            "connected": connected,
            "conn_msg": conn_msg,
            "tmdb_connected": tmdb_connected,
            "tmdb_msg": tmdb_msg,
            "overseerr_connected": overseerr_connected,
            "overseerr_msg": overseerr_msg,
            "tautulli_connected": tautulli_connected,
            "tautulli_msg": tautulli_msg,
            "tautulli_server_mismatch": tautulli_server_mismatch,
            "sync_state": sync_state,
            "current_user": user,
            "recs_ready": recommendations_ready(user),
            "env_locked": {
                "PLEX_URL": settings.is_locked("PLEX_URL"),
                "PLEX_TOKEN": settings.is_locked("PLEX_TOKEN"),
                "TMDB_API_KEY": settings.is_locked("TMDB_API_KEY"),
                "OVERSEERR_URL": settings.is_locked("OVERSEERR_URL"),
                "OVERSEERR_API_KEY": settings.is_locked("OVERSEERR_API_KEY"),
                "TAUTULLI_URL": settings.is_locked("TAUTULLI_URL"),
                "TAUTULLI_API_KEY": settings.is_locked("TAUTULLI_API_KEY"),
                "AUTO_SYNC_HOURS": settings.is_locked("AUTO_SYNC_HOURS"),
                "AUTO_RECOMMENDATIONS_HOURS": settings.is_locked("AUTO_RECOMMENDATIONS_HOURS"),
                "HEALTHCHECK_INTERVAL_HOURS": settings.is_locked("HEALTHCHECK_INTERVAL_HOURS"),
                "SESSION_HTTPS_ONLY": settings.is_locked("SESSION_HTTPS_ONLY"),
            },
            "low_bandwidth": is_low_bandwidth(request),
            "first_name": first_name_of(user),
            "show_first_visit_tour": needs_first_visit_tour(user),
        }
    )

def _is_valid_http_url(url: str) -> bool:
    """Return True if url is an http:// or https:// URL with a non-empty host."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


@app.post("/api/settings/save")
def save_settings(
    request: Request,
    background_tasks: BackgroundTasks,
    plex_url: Optional[str] = Form(None),
    plex_token: Optional[str] = Form(None),
    tmdb_api_key: Optional[str] = Form(None),
    overseerr_url: Optional[str] = Form(None),
    overseerr_api_key: Optional[str] = Form(None),
    tautulli_url: Optional[str] = Form(None),
    tautulli_api_key: Optional[str] = Form(None),
    auto_sync_hours: Optional[str] = Form(None),
    auto_recommendations_hours: Optional[str] = Form(None),
    healthcheck_interval_hours: Optional[str] = Form(None),
    session_https_only: Optional[str] = Form(None),
    security_section: Optional[str] = Form(None),
):
    user = get_current_user(request)
    if not user:
        # Session expired or not logged in: send back to login instead of a dead-end 403.
        return RedirectResponse(url="/login?next=/settings", status_code=303)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Administrator access required.")

    for label, raw_url in (("PLEX_URL", plex_url), ("OVERSEERR_URL", overseerr_url), ("TAUTULLI_URL", tautulli_url)):
        if raw_url is not None and raw_url.strip() and not _is_valid_http_url(raw_url.strip()):
            raise HTTPException(status_code=400, detail=f"Invalid URL for {label}: must start with http:// or https://")

    pending_updates = {}
    changed = []
    if plex_url and plex_url.strip() != settings.plex_url:
        pending_updates["PLEX_URL"] = plex_url.strip()
        changed.append("PLEX_URL")
    if plex_token is not None and plex_token.strip() and plex_token.strip() != (settings.plex_token or ""):
        pending_updates["PLEX_TOKEN"] = plex_token.strip()
        changed.append("PLEX_TOKEN")
    if tmdb_api_key is not None and tmdb_api_key.strip() != (settings.tmdb_api_key or ""):
        pending_updates["TMDB_API_KEY"] = tmdb_api_key.strip()
        changed.append("TMDB_API_KEY")
    if overseerr_url is not None and overseerr_url.strip() != settings.overseerr_url:
        pending_updates["OVERSEERR_URL"] = overseerr_url.strip()
        changed.append("OVERSEERR_URL")
    if overseerr_api_key is not None and overseerr_api_key.strip() != (settings.overseerr_api_key or ""):
        pending_updates["OVERSEERR_API_KEY"] = overseerr_api_key.strip()
        changed.append("OVERSEERR_API_KEY")
    if tautulli_url is not None and tautulli_url.strip() != settings.tautulli_url:
        pending_updates["TAUTULLI_URL"] = tautulli_url.strip()
        changed.append("TAUTULLI_URL")
    if tautulli_api_key is not None and tautulli_api_key.strip() != (settings.tautulli_api_key or ""):
        pending_updates["TAUTULLI_API_KEY"] = tautulli_api_key.strip()
        changed.append("TAUTULLI_API_KEY")
    if auto_sync_hours is not None and auto_sync_hours.strip().isdigit():
        val = int(auto_sync_hours.strip())
        if val > 0 and val != settings.auto_sync_hours:
            pending_updates["AUTO_SYNC_HOURS"] = str(val)
            settings.auto_sync_hours = val
            changed.append(f"AUTO_SYNC_HOURS={val}h")
            if scheduler.running:
                try:
                    scheduler.reschedule_job("plex_sync_job", trigger="interval", hours=val)
                except Exception as e:
                    logger.warning(f"Failed to reschedule sync job: {e}")
    if auto_recommendations_hours is not None and auto_recommendations_hours.strip().isdigit():
        val = int(auto_recommendations_hours.strip())
        if val > 0 and val != settings.auto_recommendations_hours:
            pending_updates["AUTO_RECOMMENDATIONS_HOURS"] = str(val)
            settings.auto_recommendations_hours = val
            changed.append(f"AUTO_RECOMMENDATIONS_HOURS={val}h")
            if scheduler.running:
                try:
                    scheduler.reschedule_job("plex_recommendations_job", trigger="interval", hours=val)
                except Exception as e:
                    logger.warning(f"Failed to reschedule recommendations job: {e}")
    if healthcheck_interval_hours is not None and healthcheck_interval_hours.strip().isdigit():
        val = int(healthcheck_interval_hours.strip())
        if val > 0 and val != settings.healthcheck_interval_hours:
            pending_updates["HEALTHCHECK_INTERVAL_HOURS"] = str(val)
            settings.healthcheck_interval_hours = val
            changed.append(f"HEALTHCHECK_INTERVAL_HOURS={val}h")
            if scheduler.running:
                try:
                    scheduler.reschedule_job("plex_healthcheck_job", trigger="interval", hours=val)
                except Exception as e:
                    logger.warning(f"Failed to reschedule healthcheck job: {e}")

    # Checkbox: absent from form data means unchecked. Only reconcile when this
    # request actually submitted the security section (hidden marker field),
    # so partial wizard step submissions (which omit this section) never
    # accidentally flip https_only off.
    if security_section is not None:
        https_only_val = session_https_only is not None and session_https_only.strip().lower() in ("on", "true", "1", "yes")
        if https_only_val != settings.session_https_only:
            pending_updates["SESSION_HTTPS_ONLY"] = "true" if https_only_val else "false"
            settings.session_https_only = https_only_val
            changed.append(f"SESSION_HTTPS_ONLY={https_only_val}")

    if pending_updates:
        settings.save_settings_batch(pending_updates)

    integration_keys = {"TMDB_API_KEY", "OVERSEERR_URL", "OVERSEERR_API_KEY", "TAUTULLI_URL", "TAUTULLI_API_KEY"}
    if any(k in integration_keys for k in changed):
        background_tasks.add_task(job_manager.run_healthcheck, trigger="manual")

    logger.info(
        "Admin '%s' saved configuration changes: %s",
        user.get("username") or user.get("user_key"),
        ", ".join(changed) if changed else "no value changes"
    )
    return RedirectResponse(url="/settings?saved=true", status_code=303)

@app.get("/settings/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/settings/jobs", status_code=303)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Administrator access required.")
    jobs = get_job_history(limit=200)
    configured_jobs = get_scheduled_jobs_info()
    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "settings": settings,
            "jobs": jobs,
            "configured_jobs": configured_jobs,
            "sync_state": sync_state,
            "current_user": user,
            "recs_ready": recommendations_ready(user),
            "low_bandwidth": is_low_bandwidth(request),
            "first_name": first_name_of(user),
            "show_first_visit_tour": needs_first_visit_tour(user),
        }
    )

@app.post("/api/jobs/run")
def api_run_job(
    request: Request,
    background_tasks: BackgroundTasks,
    job_type: str = Form(...),
    user_key: Optional[str] = Form(None)
):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    if job_type not in JOB_DEFINITIONS:
        return JSONResponse({"error": f"Unknown job type: {job_type}"}, status_code=400)

    if job_manager.is_running(job_type):
        return JSONResponse({
            "status": "already_running",
            "message": f"Job '{job_type}' is already in progress.",
        }, status_code=409)

    if job_type == "sync":
        background_tasks.add_task(job_manager.run_sync, trigger="manual", user_key=user_key)
    elif job_type == "recommendations":
        background_tasks.add_task(job_manager.run_recommendations, trigger="manual", user_key=user_key)
    elif job_type == "truncate_logs":
        background_tasks.add_task(job_manager.run_truncate_logs, trigger="manual", retention_days=7)
    elif job_type == "poster_cleanup":
        background_tasks.add_task(job_manager.run_poster_cleanup, trigger="manual")
    elif job_type == "healthcheck":
        background_tasks.add_task(job_manager.run_healthcheck, trigger="manual")

    return JSONResponse({"status": "started", "job_type": job_type})

@app.get("/api/jobs/status")
def api_jobs_status(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not user.get("is_admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse({
        "configured_jobs": get_scheduled_jobs_info(),
        "running_jobs": job_manager.running_jobs,
    })

@app.post("/api/jobs/clear")
def api_clear_jobs(request: Request):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    clear_job_history()
    return RedirectResponse(url="/settings/jobs", status_code=303)

@app.get("/settings/system", response_class=HTMLResponse)
def system_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/settings/system", status_code=303)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return templates.TemplateResponse(
        request=request,
        name="system.html",
        context={
            "settings": settings,
            "db_stats": get_database_stats(),
            "user_data": get_user_data_summary(),
            "sync_state": sync_state,
            "current_user": user,
            "recs_ready": recommendations_ready(user),
            "low_bandwidth": is_low_bandwidth(request),
            "first_name": first_name_of(user),
            "show_first_visit_tour": needs_first_visit_tour(user),
        }
    )

@app.post("/api/system/clear-user")
def api_clear_user_data(request: Request, user_key: str = Form(...)):
    admin = get_current_user(request)
    if not admin or not admin.get("is_admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    clear_user_data(user_key)
    logger.info("Admin '%s' wiped all watch data and cached recommendations for user '%s'.", admin.get("username"), user_key)
    return RedirectResponse(url="/settings/system?cleared=1", status_code=303)


@app.get("/settings/logs", response_class=HTMLResponse)
def logs_page(request: Request, level: Optional[str] = None, q: Optional[str] = None):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login?next=/settings/logs", status_code=303)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Administrator access required.")
    logs = log_handler.get_logs(limit=250, level=level, search=q)
    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={
            "settings": settings,
            "logs": logs,
            "selected_level": (level or "ALL").upper(),
            "search_query": q or "",
            "sync_state": sync_state,
            "current_user": user,
            "recs_ready": recommendations_ready(user),
            "low_bandwidth": is_low_bandwidth(request),
            "first_name": first_name_of(user),
            "show_first_visit_tour": needs_first_visit_tour(user),
        }
    )


@app.get("/api/logs/export")
def api_export_logs(request: Request, format: str = "text"):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Administrator access required.")
    if format == "json":
        return JSONResponse(log_handler.get_logs(limit=1000))
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        content=log_handler.export_text(limit=1000),
        headers={"Content-Disposition": "attachment; filename=mindarr_logs.txt"}
    )


@app.post("/api/logs/truncate")
def api_truncate_logs(request: Request, days: int = Form(7)):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    count = log_handler.truncate(days=days)
    logger.info("Admin '%s' triggered manual log truncation (>%s days). Pruned %s records.", user.get("username"), days, count)
    return RedirectResponse(url=f"/settings/logs?truncated={count}", status_code=303)


@app.post("/api/logs/clear")
def api_clear_logs(request: Request):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    logger.info("Admin '%s' wiped all system logs.", user.get("username"))
    log_handler.clear()
    return RedirectResponse(url="/settings/logs?cleared=1", status_code=303)

@app.get("/welcome", response_class=HTMLResponse)
def welcome_page(request: Request):
    """Admin first-run setup wizard: Plex -> TMDb -> optional integrations -> initial sync."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not user.get("is_admin"):
        return RedirectResponse(url="/", status_code=303)

    plex_connected, plex_msg = (False, "")
    if settings.plex_token:
        plex_connected, plex_msg = verify_plex_connection()

    overseerr_connected, overseerr_msg = overseerr.test_connection()
    tautulli_connected, tautulli_msg = tautulli.test_connection()

    return templates.TemplateResponse(
        request=request,
        name="welcome.html",
        context={
            "settings": settings,
            "current_user": user,
            "sync_state": sync_state,
            "recs_ready": recommendations_ready(user),
            "low_bandwidth": is_low_bandwidth(request),
            "first_name": first_name_of(user),
            "show_first_visit_tour": False,
            "plex_connected": plex_connected,
            "plex_msg": plex_msg,
            "overseerr_connected": overseerr_connected,
            "overseerr_msg": overseerr_msg,
            "tautulli_connected": tautulli_connected,
            "tautulli_msg": tautulli_msg,
            "env_locked": {
                "PLEX_URL": settings.is_locked("PLEX_URL"),
                "TMDB_API_KEY": settings.is_locked("TMDB_API_KEY"),
                "OVERSEERR_URL": settings.is_locked("OVERSEERR_URL"),
                "OVERSEERR_API_KEY": settings.is_locked("OVERSEERR_API_KEY"),
                "TAUTULLI_URL": settings.is_locked("TAUTULLI_URL"),
                "TAUTULLI_API_KEY": settings.is_locked("TAUTULLI_API_KEY"),
            },
        }
    )


@app.post("/welcome/complete")
def welcome_complete(request: Request):
    """Mark the current user as onboarded.

    Used by the admin setup wizard (finish/skip) and by the shared-user
    first-visit welcome tour modal (dismiss/finish) to release the relevant
    one-time UI and, for admins, the /welcome redirect gate.
    """
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    mark_onboarded(user["user_key"])
    logger.info(
        "User '%s' completed onboarding (%s).",
        user.get("username") or user.get("user_key"),
        "admin wizard" if user.get("is_admin") else "first-visit tour",
    )
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/plex/test")
def test_plex_route(request: Request):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    success, message = verify_plex_connection()
    return JSONResponse({"success": success, "message": message})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if get_current_user(request):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "settings": settings,
            "needs_admin": not admin_exists(),
            "low_bandwidth": is_low_bandwidth(request),
        }
    )

@app.get("/logout")
def logout(request: Request):
    user = get_current_user(request)
    if user:
        logger.info("User '%s' (%s) signed out.", user.get("username") or user.get("title"), user.get("user_key"))
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

_AUTH_RATE_LOCK = threading.Lock()
_AUTH_RATE_BUCKETS: dict = {}


def _check_auth_rate_limit(request: Request, bucket: str, max_requests: int, window_sec: int = 60) -> bool:
    """Sliding-window per-IP rate limiter for auth endpoints. Returns True if allowed."""
    forwarded = request.headers.get("x-forwarded-for")
    client_ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else "unknown")
    )
    key = f"{bucket}:{client_ip}"
    now = time.monotonic()
    with _AUTH_RATE_LOCK:
        dq = _AUTH_RATE_BUCKETS.get(key)
        if dq is None:
            dq = deque()
            _AUTH_RATE_BUCKETS[key] = dq
        while dq and now - dq[0] >= window_sec:
            dq.popleft()
        if len(dq) >= max_requests:
            return False
        dq.append(now)
    return True


@app.post("/api/auth/start")
def start_auth(request: Request):
    if not _check_auth_rate_limit(request, "auth_start", max_requests=15):
        return JSONResponse({"error": "rate_limited", "message": "Too many sign-in attempts. Please wait a minute."}, status_code=429)
    try:
        pin_data = create_plex_pin()
        request.session["pending_pin_id"] = int(pin_data["id"])
        return JSONResponse(pin_data)
    except Exception as e:
        logger.error("Failed to initiate Plex OAuth PIN: %s", e)
        return JSONResponse({"error": "Failed to contact Plex authentication service."}, status_code=500)


@app.post("/api/auth/poll")
def poll_auth(request: Request, pin_id: int = Form(...)):
    """Poll the OAuth PIN; on claim, establish identity, access, and session."""
    if not _check_auth_rate_limit(request, "auth_poll", max_requests=60):
        return JSONResponse({"error": "rate_limited", "message": "Too many auth poll requests."}, status_code=429)

    pending_pin_id = request.session.get("pending_pin_id")
    if pending_pin_id is None or int(pending_pin_id) != int(pin_id):
        logger.warning("Rejected /api/auth/poll with unbound or mismatched pin_id=%s", pin_id)
        return JSONResponse({"error": "invalid_pin_session", "message": "Authentication session expired. Please try again."}, status_code=403)

    token = check_plex_pin(pin_id)
    if not token:
        return JSONResponse({"claimed": False})

    account = get_plex_account(token)
    if not account:
        request.session.pop("pending_pin_id", None)
        return JSONResponse({"claimed": True, "authorized": False,
                             "message": "Could not verify your Plex account."})

    first_user = not admin_exists()
    if first_user:
        # First user becomes the Administrator/owner; their token is the server token.
        settings.save_setting("PLEX_TOKEN", token)
        machine_id = get_server_machine_id(token=token)
        if machine_id:
            settings.save_setting("PLEX_MACHINE_ID", machine_id)
        create_or_update_user({
            **account,
            "plex_token": token,
            "is_admin": True,
        })
        request.session.clear()
        request.session["user_key"] = account["user_key"]
        previous_seen = touch_last_seen(account["user_key"])
        if previous_seen:
            request.session["previous_last_seen"] = previous_seen
        logger.info(
            "Initial setup: user '%s' (%s) registered as Administrator and server token owner.",
            account.get("username") or account.get("title"),
            account.get("user_key")
        )
        return JSONResponse({"claimed": True, "authorized": True, "is_admin": True,
                             "redirect": "/settings"})

    # Subsequent users: authorize against the owner's shared-user list.
    admin = get_admin()
    allowed = check_user_access(
        admin_token=admin.get("plex_token") or settings.plex_token,
        machine_id=settings.plex_machine_id,
        user_id=account["user_key"],
        owner_id=admin.get("user_key"),
    )
    if not allowed:
        request.session.pop("pending_pin_id", None)
        logger.warning(
            "Sign-in denied for user '%s' (%s): not authorized on linked server '%s'.",
            account.get("username") or account.get("title"),
            account.get("user_key"),
            settings.plex_machine_id
        )
        return JSONResponse({"claimed": True, "authorized": False,
                             "message": "You do not have access to this Plex server."})

    # Shared user OAuth tokens are only needed at login; never persist them at rest.
    create_or_update_user({**account, "plex_token": None, "is_admin": False})
    request.session.clear()
    request.session["user_key"] = account["user_key"]
    previous_seen = touch_last_seen(account["user_key"])
    if previous_seen:
        request.session["previous_last_seen"] = previous_seen
    logger.info(
        "User '%s' (%s) signed in successfully.",
        account.get("username") or account.get("title"),
        account.get("user_key")
    )
    return JSONResponse({"claimed": True, "authorized": True, "is_admin": False,
                         "redirect": "/"})

def run_sync_task(user_key: Optional[str] = None):
    job_manager.run_sync(trigger="manual", user_key=user_key)

@app.post("/api/sync")
def trigger_sync(request: Request, background_tasks: BackgroundTasks):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Atomically claim the sync slot so concurrent/rapid clicks can't start
    # overlapping jobs. Only one sync runs at a time (shared library scan).
    with sync_lock:
        if sync_state["is_syncing"]:
            active_user = sync_state.get("user_key")
            mine = active_user == user["user_key"]
            return JSONResponse(
                {
                    "status": "already_syncing",
                    "message": "A sync is already in progress for your account."
                    if mine
                    else "A sync is already in progress. Please wait for it to finish.",
                    "active_user": active_user,
                },
                status_code=409,
            )
        sync_state["is_syncing"] = True
        sync_state["user_key"] = user["user_key"]
        sync_state["status"] = "Starting sync..."
        sync_state["progress"] = 0.0
        sync_state["error"] = None

    background_tasks.add_task(run_sync_task, user["user_key"])
    return JSONResponse({"status": "started"})

@app.get("/api/sync/status")
def get_sync_status(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    stats = get_stats(user["user_key"])
    return JSONResponse({
        "is_syncing": sync_state["is_syncing"],
        "status": sync_state["status"],
        "progress": sync_state["progress"],
        "error": sync_state["error"],
        "stats": stats
    })

@app.post("/api/overseerr/request")
def request_overseerr(
    request: Request,
    tmdb_id: int = Form(...),
    media_type: str = Form("movie"),
    is_4k: bool = Form(False),
    seasons: Optional[str] = Form(None)
):
    """Trigger a media request (defaults to Season 1 for TV shows), attributed to current user."""
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    parsed_seasons = None
    if seasons:
        if seasons.lower() == "all":
            parsed_seasons = "all"
        else:
            try:
                parsed_seasons = [int(s.strip()) for s in seasons.split(",") if s.strip().isdigit()]
            except Exception:
                parsed_seasons = [1]
    elif media_type in ("show", "tv"):
        parsed_seasons = [1]

    # Server-wide duplicate check: prevent requesting if already in Overseerr queue
    media_info = overseerr.get_media_info(tmdb_id=tmdb_id, media_type=media_type)
    if media_info.get("exists") and media_info.get("status_code") in (2, 3, 4, 5):
        status_name = media_info.get("status_name", "PENDING")
        if status_name == "AVAILABLE":
            dup_msg = "This title is already available on the server."
        elif status_name == "PROCESSING":
            dup_msg = "This title is already downloading in the server queue."
        elif status_name == "PARTIALLY_AVAILABLE":
            dup_msg = "This title is already partially available on the server."
        else:
            dup_msg = "This title has already been requested on the server."

        logger.info(
            "User '%s' attempted to request %s TMDb %s, but it is already in queue (%s).",
            user.get("username") or user.get("user_key"),
            media_type,
            tmdb_id,
            status_name
        )
        return JSONResponse({"success": False, "already_requested": True, "message": dup_msg})

    overseerr_user_id = overseerr.resolve_user_id(
        plex_user_key=user.get("user_key"),
        email=user.get("email"),
        username=user.get("username")
    )

    success, message = overseerr.request_media(
        tmdb_id=tmdb_id,
        media_type=media_type,
        is_4k=is_4k,
        seasons=parsed_seasons,
        user_id=overseerr_user_id
    )

    if success:
        overseerr.mark_queued(tmdb_id=tmdb_id, media_type=media_type, status_code=2)
        clear_user_recommendations_cache(user["user_key"])

    logger.info(
        "User '%s' submitted Overseerr %s request (TMDb: %s, overseerr_user_id=%s, result=%s): %s",
        user.get("username") or user.get("user_key"),
        media_type,
        tmdb_id,
        overseerr_user_id or "admin_fallback",
        "success" if success else "failed",
        message
    )
    return JSONResponse({
        "success": success,
        "message": message,
        "overseerr_user_id": overseerr_user_id,
        "seasons": parsed_seasons,
    })

@app.get("/api/overseerr/test")
def test_overseerr_route(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not user.get("is_admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    success, message = overseerr.test_connection()
    return JSONResponse({"success": success, "message": message})

@app.get("/api/tautulli/test")
def test_tautulli_route(request: Request):
    user = get_current_user(request)
    if not user or not user.get("is_admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    success, message = tautulli.test_connection()
    server_mismatch = False
    if success and not tautulli.monitors_server(settings.plex_machine_id):
        server_mismatch = True
        message += " — but it is not monitoring the linked Plex server; its history will be ignored."
    return JSONResponse({"success": success, "server_mismatch": server_mismatch, "message": message})
