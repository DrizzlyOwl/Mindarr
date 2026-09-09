import logging
import threading
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

from plex_recommender.config import settings
from plex_recommender.db import (
    init_db, get_stats, set_setting, get_setting, clear_recommendations_cache,
    admin_exists, get_admin, get_user, get_all_users, create_or_update_user,
    has_user_history, start_job, finish_job, get_job_history, clear_job_history,
    get_database_stats, get_user_data_summary, clear_user_data,
    get_enriched_watch_events, get_watch_source_breakdown,
)
from plex_recommender.auth import (
    create_plex_pin, check_plex_pin, verify_plex_connection,
    get_plex_account, get_server_machine_id, check_user_access,
)
from plex_recommender.sync import sync_plex_data, sync_user_history
from plex_recommender.analyzer import analyzer
from plex_recommender.recommender import recommender
from plex_recommender.community import community_service
from plex_recommender.discovery.overseerr import overseerr
from plex_recommender.discovery.tautulli import tautulli
from plex_recommender.jobs import job_manager, JOB_DEFINITIONS

logger = logging.getLogger("plex_recommender.web")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

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
    if not settings.tmdb_api_key:
        logger.info("Skipping scheduled recommendations: No TMDb API key.")
        return
    logger.info("Starting scheduled recommendations pre-fetch...")
    job_manager.run_recommendations(trigger="scheduled")


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

    return jobs_info


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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
        scheduler.start()
        logger.info(
            f"Started background scheduler (sync every {settings.auto_sync_hours}h, recs every {settings.auto_recommendations_hours}h)."
        )
    yield
    if scheduler.running:
        scheduler.shutdown()

app = FastAPI(title="Mindarr", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    max_age=SESSION_MAX_AGE,
    same_site="lax",
)


def get_current_user(request: Request) -> Optional[dict]:
    """Resolve the logged-in user from the session, or None."""
    user_key = request.session.get("user_key")
    if not user_key:
        return None
    return get_user(user_key)


def recommendations_ready(user: Optional[dict]) -> bool:
    """True once the user's initial watch history sync has produced data."""
    if not user:
        return False
    return has_user_history(user["user_key"])


# Mount static if directory exists
static_dir = BASE_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

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
            "low_bandwidth": is_low_bandwidth(request)
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
            "low_bandwidth": is_low_bandwidth(request)
        }
    )

@app.get("/sources", response_class=HTMLResponse)
@app.get("/sources/", response_class=HTMLResponse)
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
            "low_bandwidth": is_low_bandwidth(request)
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
    force_refresh: bool = False
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

    # Surface only the setup-blocking errors up front; data errors are handled
    # by the async endpoint and rendered client-side.
    error_msg = None
    if not settings.tmdb_api_key:
        error_msg = "TMDb API Key is not configured. Please ask your administrator to add it in Settings."
    elif not profile.get("has_data"):
        error_msg = "No watch history available for your account. Recommendations require Tautulli history."

    return templates.TemplateResponse(
        request=request,
        name="recommendations.html",
        context={
            "results": {"success": False, "recommendations": []},
            "profile": profile,
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
            "has_overseerr": bool(settings.overseerr_url and settings.overseerr_api_key),
            "low_bandwidth": is_low_bandwidth(request),
            "force_refresh": force_refresh
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
    force_refresh: bool = False
):
    """Compute recommendations and return rendered card HTML + cache metadata.

    Called in the background by the recommendations page so the initial load
    isn't blocked on TMDb/Plex discovery.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    profile = analyzer.analyze(user["user_key"])
    selected_lang = language or "en"
    results = {"success": False, "recommendations": []}
    error_msg = None

    if not settings.tmdb_api_key:
        error_msg = "TMDb API Key is not configured. Please ask your administrator to add it in Settings."
    elif not profile.get("has_data"):
        error_msg = "No watch history available for your account. Recommendations require Tautulli history."
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
                force_refresh=force_refresh
            )
        except Exception as e:
            logger.error(f"Recommendation generation error: {e}")
            error_msg = str(e)

    html = templates.get_template("_recommendation_cards.html").render(
        {
            "request": request,
            "results": results,
            "error_msg": error_msg,
            "has_overseerr": bool(settings.overseerr_url and settings.overseerr_api_key),
            "low_bandwidth": is_low_bandwidth(request),
        }
    )

    return JSONResponse({
        "html": html,
        "has_results": bool(results.get("recommendations")),
        "from_cache": bool(results.get("from_cache")),
        "page": results.get("page", 1),
        "total_pages": results.get("total_pages", 1),
        "unseen_count": results.get("unseen_count", 0),
        "error": error_msg,
    })


@app.post("/api/recommendations/clear-cache")
def api_clear_recommendations_cache(request: Request):
    if not get_current_user(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    clear_recommendations_cache()
    return {"success": True, "message": "Recommendations cache cleared"}

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
            },
            "low_bandwidth": is_low_bandwidth(request)
        }
    )

@app.post("/api/settings/save")
def save_settings(
    request: Request,
    plex_url: Optional[str] = Form(None),
    plex_token: Optional[str] = Form(None),
    tmdb_api_key: Optional[str] = Form(None),
    overseerr_url: Optional[str] = Form(None),
    overseerr_api_key: Optional[str] = Form(None),
    tautulli_url: Optional[str] = Form(None),
    tautulli_api_key: Optional[str] = Form(None),
    auto_sync_hours: Optional[str] = Form(None),
    auto_recommendations_hours: Optional[str] = Form(None),
):
    user = get_current_user(request)
    if not user:
        # Session expired or not logged in: send back to login instead of a dead-end 403.
        return RedirectResponse(url="/login?next=/settings", status_code=303)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Administrator access required.")
    if plex_url:
        settings.save_setting("PLEX_URL", plex_url.strip())
    if plex_token is not None:
        settings.save_setting("PLEX_TOKEN", plex_token.strip())
    if tmdb_api_key is not None:
        settings.save_setting("TMDB_API_KEY", tmdb_api_key.strip())
    if overseerr_url is not None:
        settings.save_setting("OVERSEERR_URL", overseerr_url.strip())
    if overseerr_api_key is not None:
        settings.save_setting("OVERSEERR_API_KEY", overseerr_api_key.strip())
    if tautulli_url is not None:
        settings.save_setting("TAUTULLI_URL", tautulli_url.strip())
    if tautulli_api_key is not None:
        settings.save_setting("TAUTULLI_API_KEY", tautulli_api_key.strip())
    if auto_sync_hours is not None and auto_sync_hours.strip().isdigit():
        val = int(auto_sync_hours.strip())
        if val > 0:
            settings.save_setting("AUTO_SYNC_HOURS", str(val))
            settings.auto_sync_hours = val
            if scheduler.running:
                try:
                    scheduler.reschedule_job("plex_sync_job", trigger="interval", hours=val)
                except Exception as e:
                    logger.warning(f"Failed to reschedule sync job: {e}")
    if auto_recommendations_hours is not None and auto_recommendations_hours.strip().isdigit():
        val = int(auto_recommendations_hours.strip())
        if val > 0:
            settings.save_setting("AUTO_RECOMMENDATIONS_HOURS", str(val))
            settings.auto_recommendations_hours = val
            if scheduler.running:
                try:
                    scheduler.reschedule_job("plex_recommendations_job", trigger="interval", hours=val)
                except Exception as e:
                    logger.warning(f"Failed to reschedule recommendations job: {e}")
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

    return JSONResponse({"status": "started", "job_type": job_type})

@app.get("/api/jobs/status")
def api_jobs_status(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
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
        }
    )

@app.post("/api/system/clear-user")
def api_clear_user_data(request: Request, user_key: str = Form(...)):
    admin = get_current_user(request)
    if not admin or not admin.get("is_admin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    clear_user_data(user_key)
    return RedirectResponse(url="/settings/system?cleared=1", status_code=303)

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
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

@app.post("/api/auth/start")
def start_auth():
    try:
        pin_data = create_plex_pin()
        return JSONResponse(pin_data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/auth/poll")
def poll_auth(request: Request, pin_id: int = Form(...)):
    """Poll the OAuth PIN; on claim, establish identity, access, and session."""
    token = check_plex_pin(pin_id)
    if not token:
        return JSONResponse({"claimed": False})

    account = get_plex_account(token)
    if not account:
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
        request.session["user_key"] = account["user_key"]
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
        return JSONResponse({"claimed": True, "authorized": False,
                             "message": "You do not have access to this Plex server."})

    create_or_update_user({**account, "plex_token": token, "is_admin": False})
    request.session["user_key"] = account["user_key"]
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
    stats = get_stats(user["user_key"]) if user else get_stats()
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
    """Trigger a media request (defaults to Season 1 for TV shows)."""
    if not get_current_user(request):
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

    success, message = overseerr.request_media(
        tmdb_id=tmdb_id,
        media_type=media_type,
        is_4k=is_4k,
        seasons=parsed_seasons
    )
    return JSONResponse({"success": success, "message": message})

@app.get("/api/overseerr/test")
def test_overseerr_route(request: Request):
    if not get_current_user(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
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
