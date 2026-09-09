import logging
import threading
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable

from plex_recommender.config import settings
from plex_recommender.db import (
    start_job,
    finish_job,
    get_all_users,
    get_user,
    has_user_history,
)
from plex_recommender.sync import (
    sync_plex_data,
    sync_user_history,
    discover_and_register_shared_users,
)
from plex_recommender.recommender import recommender

logger = logging.getLogger("plex_recommender.jobs")

JOB_DEFINITIONS = {
    "sync": {
        "id": "sync",
        "name": "Plex & Tautulli Sync",
        "description": "Scans library metadata, discovers shared users, and syncs watch history.",
        "default_trigger": "scheduled",
    },
    "recommendations": {
        "id": "recommendations",
        "name": "User Recommendations",
        "description": "Pre-computes and caches TMDb discovery recommendations for all users with watch history.",
        "default_trigger": "scheduled",
    },
}


class JobManager:
    """Coordinates background job execution, status reporting, and concurrency."""

    def __init__(self):
        self._lock = threading.Lock()
        self.sync_state = {
            "is_syncing": False,
            "status": "Idle",
            "progress": 0.0,
            "error": None,
            "user_key": None,
        }
        self.running_jobs: Dict[str, Dict[str, Any]] = {}

    def is_running(self, job_type: str) -> bool:
        with self._lock:
            if job_type == "sync":
                return "sync" in self.running_jobs or self.sync_state.get("is_syncing", False)
            return job_type in self.running_jobs

    def get_job_state(self, job_type: str) -> Dict[str, Any]:
        with self._lock:
            info = self.running_jobs.get(job_type)
            if info:
                return {**info, "is_running": True}
            if job_type == "sync" and self.sync_state.get("is_syncing", False):
                return {
                    "job_type": "sync",
                    "is_running": True,
                    "status": self.sync_state.get("status", "Syncing..."),
                    "progress": self.sync_state.get("progress", 0.0),
                    "error": self.sync_state.get("error"),
                    "job_id": None,
                }
            return {
                "is_running": False,
                "status": "Idle",
                "progress": 0.0,
                "error": None,
                "job_id": None,
            }

    def get_all_job_states(self) -> Dict[str, Any]:
        states = {}
        for j_id, defn in JOB_DEFINITIONS.items():
            st = self.get_job_state(j_id)
            states[j_id] = {
                **defn,
                **st,
            }
        return states

    def run_sync(
        self,
        trigger: str = "manual",
        user_key: Optional[str] = None,
        progress_callback: Optional[Callable[[str, float], None]] = None,
        chain_recommendations: bool = False,
    ) -> Dict[str, Any]:
        """Execute full or user sync."""
        with self._lock:
            if "sync" in self.running_jobs:
                return {
                    "success": False,
                    "status": "already_running",
                    "message": "A sync job is already in progress.",
                }
            self.running_jobs["sync"] = {
                "job_type": "sync",
                "trigger": trigger,
                "user_key": user_key,
                "started_at": datetime.now().isoformat(),
                "status": "Starting sync...",
                "progress": 0.0,
                "error": None,
            }
            self.sync_state["is_syncing"] = True
            self.sync_state["user_key"] = user_key
            self.sync_state["status"] = "Starting sync..."
            self.sync_state["progress"] = 0.0
            self.sync_state["error"] = None

        db_job_type = "scheduled_sync" if trigger == "scheduled" else "manual_sync"
        job_id = start_job(db_job_type, trigger, user_key)
        self.running_jobs["sync"]["job_id"] = job_id

        def _update_progress(msg: str, prog: float):
            with self._lock:
                if "sync" in self.running_jobs:
                    self.running_jobs["sync"]["status"] = msg
                    self.running_jobs["sync"]["progress"] = round(prog * 100, 1)
                self.sync_state["status"] = msg
                self.sync_state["progress"] = round(prog * 100, 1)
            if progress_callback:
                progress_callback(msg, prog)

        try:
            if user_key:
                sync_plex_data(user_key=user_key, progress_callback=_update_progress)
                detail = f"Manual sync completed for user {user_key}."
            else:
                _update_progress("Scanning library metadata...", 0.1)
                sync_plex_data(progress_callback=_update_progress)

                _update_progress("Discovering shared users...", 0.5)
                discover_and_register_shared_users()

                users = get_all_users()
                total = len(users)
                user_count = 0
                for idx, u in enumerate(users):
                    uk = u["user_key"]
                    uname = u.get("username") or u.get("title") or uk
                    pct = 0.5 + (0.45 * (idx / max(total, 1)))
                    _update_progress(f"Syncing history for {uname} ({idx + 1}/{total})...", pct)
                    try:
                        sync_user_history(uk)
                        user_count += 1
                    except Exception as e:
                        logger.warning(f"Error syncing history for user {uk}: {e}")

                detail = f"Synced library metadata and {user_count} user(s)."

            with self._lock:
                self.sync_state["status"] = "Sync completed!"
                self.sync_state["progress"] = 100.0
            finish_job(job_id, "success", detail)

            if chain_recommendations:
                logger.info("Chaining user recommendations pre-fetch after sync...")
                threading.Thread(
                    target=self.run_recommendations,
                    kwargs={"trigger": "chained"},
                    daemon=True,
                ).start()

            return {"success": True, "job_id": job_id, "detail": detail}
        except Exception as e:
            logger.error(f"Sync job error: {e}")
            with self._lock:
                self.sync_state["status"] = "Sync failed"
                self.sync_state["error"] = str(e)
            finish_job(job_id, "failed", str(e))
            return {"success": False, "job_id": job_id, "error": str(e)}
        finally:
            with self._lock:
                self.running_jobs.pop("sync", None)
                self.sync_state["is_syncing"] = False
                self.sync_state["user_key"] = None

    def run_recommendations(
        self,
        trigger: str = "manual",
        user_key: Optional[str] = None,
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> Dict[str, Any]:
        """Pre-compute and cache recommendations for all users with watch history."""
        with self._lock:
            if "recommendations" in self.running_jobs:
                return {
                    "success": False,
                    "status": "already_running",
                    "message": "A recommendations pre-fetch job is already in progress.",
                }
            self.running_jobs["recommendations"] = {
                "job_type": "recommendations",
                "trigger": trigger,
                "user_key": user_key,
                "started_at": datetime.now().isoformat(),
                "status": "Starting recommendations pre-fetch...",
                "progress": 0.0,
                "error": None,
            }

        job_id = start_job("recommendations_sync", trigger, user_key)
        self.running_jobs["recommendations"]["job_id"] = job_id

        def _update_progress(msg: str, prog: float):
            with self._lock:
                if "recommendations" in self.running_jobs:
                    self.running_jobs["recommendations"]["status"] = msg
                    self.running_jobs["recommendations"]["progress"] = round(prog * 100, 1)
            if progress_callback:
                progress_callback(msg, prog)

        try:
            if not settings.tmdb_api_key:
                err = "TMDb API key is not configured. Cannot generate recommendations."
                finish_job(job_id, "failed", err)
                return {"success": False, "job_id": job_id, "error": err}

            if user_key:
                target_users = [u for u in [get_user(user_key)] if u]
            else:
                target_users = get_all_users()

            users_with_history = [u for u in target_users if has_user_history(u["user_key"])]
            if not users_with_history:
                msg = "No users with watch history found. Recommendations require watch history."
                finish_job(job_id, "success", msg)
                return {"success": True, "job_id": job_id, "detail": msg}

            total = len(users_with_history)
            success_count = 0
            _update_progress(f"Starting pre-computation for {total} user(s)...", 0.05)

            for idx, u in enumerate(users_with_history):
                uk = u["user_key"]
                uname = u.get("username") or u.get("title") or uk
                pct = 0.05 + (0.90 * (idx / max(total, 1)))
                _update_progress(f"Generating recommendations for {uname} ({idx + 1}/{total})...", pct)

                try:
                    recommender.get_recommendations(
                        user_key=uk,
                        media_type="all",
                        limit=18,
                        page=1,
                        min_rating=7.0,
                        genre_filter=None,
                        language="en",
                        only_available=False,
                        include_kids=False,
                        force_refresh=True,
                    )
                    success_count += 1
                except Exception as e:
                    logger.warning(f"Failed pre-computing recommendations for user {uk}: {e}")

            _update_progress("Recommendations pre-computation complete!", 1.0)
            detail = f"Precomputed recommendations for {success_count} user(s)."
            finish_job(job_id, "success", detail)
            return {"success": True, "job_id": job_id, "detail": detail}
        except Exception as e:
            logger.error(f"Recommendations job error: {e}")
            finish_job(job_id, "failed", str(e))
            return {"success": False, "job_id": job_id, "error": str(e)}
        finally:
            with self._lock:
                self.running_jobs.pop("recommendations", None)


job_manager = JobManager()
