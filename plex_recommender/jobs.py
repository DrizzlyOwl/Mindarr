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
    truncate_system_logs,
)
from plex_recommender.sync import (
    sync_plex_data,
    sync_user_history,
    discover_and_register_shared_users,
)
from plex_recommender.recommender import recommender
from plex_recommender.poster_cache import sweep_expired_posters, POSTER_TTL_DAYS

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
    "truncate_logs": {
        "id": "truncate_logs",
        "name": "Truncate Logs",
        "description": "Purges system log events older than the 7-day retention limit.",
        "default_trigger": "scheduled",
    },
    "poster_cleanup": {
        "id": "poster_cleanup",
        "name": "Poster Cache Cleanup",
        "description": f"Deletes cached TMDb posters not accessed within {POSTER_TTL_DAYS} days.",
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

        logger.info(
            "Job 'Plex & Tautulli Sync' (ID: %s) started [trigger: %s, target: %s]",
            job_id, trigger, f"user {user_key}" if user_key else "all users"
        )

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
                logger.info("Job 'Plex & Tautulli Sync' (ID: %s): Syncing watch history for user %s...", job_id, user_key)
                sync_plex_data(user_key=user_key, progress_callback=_update_progress)
                detail = f"Manual sync completed for user {user_key}."
            else:
                _update_progress("Scanning library metadata...", 0.1)
                logger.info("Job 'Plex & Tautulli Sync' (ID: %s): Scanning Plex library catalog metadata...", job_id)
                sync_plex_data(progress_callback=_update_progress)

                _update_progress("Discovering shared users...", 0.5)
                logger.info("Job 'Plex & Tautulli Sync' (ID: %s): Discovering and registering shared Plex users...", job_id)
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
            logger.info("Job 'Plex & Tautulli Sync' (ID: %s) completed successfully: %s", job_id, detail)

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

        logger.info(
            "Job 'User Recommendations' (ID: %s) started [trigger: %s, target: %s]",
            job_id, trigger, f"user {user_key}" if user_key else "all users"
        )

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
                logger.warning("Job 'User Recommendations' (ID: %s) skipped: %s", job_id, err)
                finish_job(job_id, "failed", err)
                return {"success": False, "job_id": job_id, "error": err}

            if user_key:
                target_users = [u for u in [get_user(user_key)] if u]
            else:
                target_users = get_all_users()

            users_with_history = [u for u in target_users if has_user_history(u["user_key"])]
            if not users_with_history:
                msg = "No users with watch history found. Recommendations require watch history."
                logger.info("Job 'User Recommendations' (ID: %s): %s", job_id, msg)
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
                        limit=12,
                        page=1,
                        min_rating=7.0,
                        genre_filter=None,
                        language="en",
                        only_available=False,
                        include_kids=False,
                        force_refresh=True,
                        stage="full",
                    )
                    success_count += 1
                except Exception as e:
                    logger.warning(f"Failed pre-computing recommendations for user {uk}: {e}")

            _update_progress("Recommendations pre-computation complete!", 1.0)
            detail = f"Precomputed recommendations for {success_count} user(s)."
            finish_job(job_id, "success", detail)
            logger.info("Job 'User Recommendations' (ID: %s) completed successfully: %s", job_id, detail)
            return {"success": True, "job_id": job_id, "detail": detail}
        except Exception as e:
            logger.error(f"Recommendations job error: {e}")
            finish_job(job_id, "failed", str(e))
            return {"success": False, "job_id": job_id, "error": str(e)}
        finally:
            with self._lock:
                self.running_jobs.pop("recommendations", None)

    def run_truncate_logs(
        self,
        trigger: str = "scheduled",
        progress_callback: Optional[Callable[[str, float], None]] = None,
        retention_days: int = 7,
    ) -> Dict[str, Any]:
        """Truncate system logs older than retention period."""
        with self._lock:
            if "truncate_logs" in self.running_jobs:
                return {
                    "success": False,
                    "error": "Log truncation job is already in progress.",
                    "already_running": True,
                }
            self.running_jobs["truncate_logs"] = {
                "job_type": "truncate_logs",
                "trigger": trigger,
                "started_at": datetime.now().isoformat(),
                "status": "Starting log truncation...",
                "progress": 0.0,
                "error": None,
                "job_id": None,
            }

        job_id = start_job("truncate_logs", trigger=trigger)
        with self._lock:
            if "truncate_logs" in self.running_jobs:
                self.running_jobs["truncate_logs"]["job_id"] = job_id

        logger.info(
            "Job 'Truncate Logs' (ID: %s) started [trigger: %s, retention: %s days]",
            job_id, trigger, retention_days
        )

        def _update_progress(msg: str, pct: float):
            with self._lock:
                if "truncate_logs" in self.running_jobs:
                    self.running_jobs["truncate_logs"]["status"] = msg
                    self.running_jobs["truncate_logs"]["progress"] = pct
            if progress_callback:
                progress_callback(msg, pct)

        try:
            _update_progress(f"Scanning system logs for records older than {retention_days} days...", 0.3)
            deleted_count = truncate_system_logs(days=retention_days)
            _update_progress(f"Pruned {deleted_count} log entries.", 1.0)
            detail = f"Pruned {deleted_count} log record(s) older than {retention_days} days."
            finish_job(job_id, "success", detail)
            logger.info("Job 'Truncate Logs' (ID: %s) completed successfully: %s", job_id, detail)
            return {"success": True, "job_id": job_id, "detail": detail, "deleted_count": deleted_count}
        except Exception as e:
            logger.error(f"Log truncation job error: {e}")
            finish_job(job_id, "failed", str(e))
            return {"success": False, "job_id": job_id, "error": str(e)}
        finally:
            with self._lock:
                self.running_jobs.pop("truncate_logs", None)

    def run_poster_cleanup(
        self,
        trigger: str = "scheduled",
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> Dict[str, Any]:
        """Delete cached TMDb posters not accessed within the TTL window."""
        with self._lock:
            if "poster_cleanup" in self.running_jobs:
                return {
                    "success": False,
                    "error": "Poster cleanup job is already in progress.",
                    "already_running": True,
                }
            self.running_jobs["poster_cleanup"] = {
                "job_type": "poster_cleanup",
                "trigger": trigger,
                "started_at": datetime.now().isoformat(),
                "status": "Scanning poster cache...",
                "progress": 0.0,
                "error": None,
                "job_id": None,
            }

        job_id = start_job("poster_cleanup", trigger=trigger)
        with self._lock:
            if "poster_cleanup" in self.running_jobs:
                self.running_jobs["poster_cleanup"]["job_id"] = job_id

        logger.info(
            "Job 'Poster Cache Cleanup' (ID: %s) started [trigger: %s, ttl: %s days]",
            job_id, trigger, POSTER_TTL_DAYS
        )

        def _update_progress(msg: str, pct: float):
            with self._lock:
                if "poster_cleanup" in self.running_jobs:
                    self.running_jobs["poster_cleanup"]["status"] = msg
                    self.running_jobs["poster_cleanup"]["progress"] = pct
            if progress_callback:
                progress_callback(msg, pct)

        try:
            _update_progress(f"Scanning cached posters older than {POSTER_TTL_DAYS} days...", 0.3)
            result = sweep_expired_posters(ttl_days=POSTER_TTL_DAYS)
            detail = (
                f"Deleted {result['deleted']} expired poster(s) "
                f"({result['bytes_freed']} bytes freed), kept {result['kept']}."
            )
            _update_progress(detail, 1.0)
            finish_job(job_id, "success", detail)
            logger.info("Job 'Poster Cache Cleanup' (ID: %s) completed successfully: %s", job_id, detail)
            return {"success": True, "job_id": job_id, "detail": detail, **result}
        except Exception as e:
            logger.error(f"Poster cleanup job error: {e}")
            finish_job(job_id, "failed", str(e))
            return {"success": False, "job_id": job_id, "error": str(e)}
        finally:
            with self._lock:
                self.running_jobs.pop("poster_cleanup", None)


job_manager = JobManager()
