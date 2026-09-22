import time
import pytest
from plex_recommender.config import settings
from plex_recommender.db import init_db
from plex_recommender.db.jobs import start_job, finish_job, get_job_history, clear_job_history


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_jobs.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    yield


def test_job_lifecycle_records_status_and_duration():
    job_id = start_job("manual_sync", "manual", user_key="u1")
    running = get_job_history()
    assert len(running) == 1
    assert running[0]["status"] == "running"
    assert running[0]["duration_ms"] is None

    time.sleep(0.02)
    finish_job(job_id, "success", "done")

    jobs = get_job_history()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["status"] == "success"
    assert job["detail"] == "done"
    assert job["trigger"] == "manual"
    assert job["user_key"] == "u1"
    assert job["duration_ms"] is not None and job["duration_ms"] >= 0
    assert job["finished_at"] is not None


def test_failed_job_records_error():
    job_id = start_job("scheduled_sync", "scheduled")
    finish_job(job_id, "failed", "boom")
    job = get_job_history()[0]
    assert job["status"] == "failed"
    assert job["detail"] == "boom"


def test_history_ordered_and_clearable():
    a = start_job("manual_sync", "manual")
    finish_job(a, "success")
    b = start_job("scheduled_sync", "scheduled")
    finish_job(b, "success")

    jobs = get_job_history()
    assert len(jobs) == 2
    # Most recent first
    assert jobs[0]["id"] >= jobs[1]["id"]

    clear_job_history()
    assert get_job_history() == []


def test_upsert_discovered_user_and_preserves_token():
    from plex_recommender.db.users import upsert_discovered_user, get_user, create_or_update_user
    # Insert newly discovered user
    upsert_discovered_user({
        "user_key": "u100",
        "username": "bob",
        "email": "bob@example.com",
        "title": "Bob",
    })
    u = get_user("u100")
    assert u is not None
    assert u["username"] == "bob"
    assert u["plex_token"] is None
    assert u["last_login"] is None
    assert u["is_admin"] == 0

    # User logs in and gets a token
    create_or_update_user({
        "user_key": "u100",
        "username": "bob",
        "plex_token": "secret-token",
        "is_admin": False,
    })
    u_after_login = get_user("u100")
    assert u_after_login["plex_token"] == "secret-token"

    # Subsequent discovery does NOT wipe their token or login
    upsert_discovered_user({
        "user_key": "u100",
        "username": "bob_renamed",
    })
    u_final = get_user("u100")
    assert u_final["username"] == "bob_renamed"
    assert u_final["plex_token"] == "secret-token"


def test_job_manager_recommendations_prefetches_and_caches(monkeypatch):
    from plex_recommender.jobs import JobManager
    from plex_recommender.db.recommendations import get_cached_recommendations, set_setting
    from plex_recommender.db.users import create_or_update_user
    from plex_recommender.db.watch import upsert_user_media
    monkeypatch.setattr(settings, "tmdb_api_key", "test_tmdb_key")

    create_or_update_user({
        "user_key": "u_rec",
        "username": "rec_user",
        "email": "rec@example.com",
    })
    upsert_user_media("u_rec", {
        "item_id": "item1",
        "media_type": "movie",
        "title": "Inception",
        "year": 2010,
        "genres": ["Action", "Sci-Fi"],
        "tmdb_id": "27205",
        "view_count": 1,
    })
    set_setting("last_sync_time", "2026-01-01T00:00:00")

    jm = JobManager()

    # Mock recommender.get_recommendations
    called_keys = []
    def fake_get_recommendations(**kwargs):
        called_keys.append(kwargs.get("user_key"))
        return {"success": True, "recommendations": []}

    from plex_recommender.jobs import recommender
    monkeypatch.setattr(recommender, "get_recommendations", fake_get_recommendations)

    res = jm.run_recommendations(trigger="manual")
    assert res["success"] is True
    assert "u_rec" in called_keys

    # Check job history was recorded
    history = get_job_history()
    rec_jobs = [j for j in history if j["job_type"] == "recommendations_sync"]
    assert len(rec_jobs) == 1
    assert rec_jobs[0]["status"] == "success"
    assert "1 user(s)" in rec_jobs[0]["detail"]


def test_job_manager_concurrency_blocks_duplicate_job():
    from plex_recommender.jobs import JobManager
    jm = JobManager()
    jm.running_jobs["recommendations"] = {
        "job_type": "recommendations",
        "trigger": "manual",
        "user_key": None,
        "status": "Running...",
        "progress": 50.0,
    }

    res = jm.run_recommendations(trigger="manual")
    assert res["success"] is False
    assert res["status"] == "already_running"
    assert jm.is_running("recommendations") is True


def test_job_manager_truncate_logs(monkeypatch):
    from plex_recommender.jobs import JobManager, JOB_DEFINITIONS
    assert "truncate_logs" in JOB_DEFINITIONS

    jm = JobManager()
    res = jm.run_truncate_logs(trigger="manual", retention_days=7)
    assert res["success"] is True
    assert "deleted_count" in res

    history = get_job_history()
    log_jobs = [j for j in history if j["job_type"] == "truncate_logs"]
    assert len(log_jobs) == 1
    assert log_jobs[0]["status"] == "success"
    assert "older than 7 days" in log_jobs[0]["detail"]


def test_healthcheck_job_all_healthy(monkeypatch):
    from plex_recommender.jobs import JobManager, JOB_DEFINITIONS, tmdb, overseerr, tautulli
    from plex_recommender.health import get_all_health
    assert "healthcheck" in JOB_DEFINITIONS

    monkeypatch.setattr(tmdb, "test_connection", lambda: (True, "Connected to TMDb"))
    monkeypatch.setattr(overseerr, "is_configured", lambda: True)
    monkeypatch.setattr(overseerr, "test_connection", lambda: (True, "Connected to Overseerr v1.0"))
    monkeypatch.setattr(tautulli, "is_configured", lambda: True)
    monkeypatch.setattr(tautulli, "test_connection", lambda: (True, "Connected to Tautulli"))
    monkeypatch.setattr(tautulli, "monitors_server", lambda machine_id: True)

    jm = JobManager()
    res = jm.run_healthcheck(trigger="manual")
    assert res["success"] is True

    health = get_all_health()
    assert health["tmdb"]["ok"] is True
    assert health["overseerr"]["ok"] is True
    assert health["tautulli"]["ok"] is True

    history = get_job_history()
    hc_jobs = [j for j in history if j["job_type"] == "healthcheck"]
    assert len(hc_jobs) == 1
    assert hc_jobs[0]["status"] == "success"


def test_healthcheck_job_tmdb_failure_flags_unhealthy(monkeypatch):
    from plex_recommender.jobs import JobManager, tmdb, overseerr, tautulli
    from plex_recommender.health import get_unhealthy

    monkeypatch.setattr(tmdb, "test_connection", lambda: (False, "TMDb authentication failed: invalid API key."))
    monkeypatch.setattr(overseerr, "is_configured", lambda: False)
    monkeypatch.setattr(tautulli, "is_configured", lambda: False)

    jm = JobManager()
    res = jm.run_healthcheck(trigger="manual")
    assert res["success"] is True  # the job itself completes; it just records a failing integration

    unhealthy = get_unhealthy()
    assert "tmdb" in unhealthy
    assert "invalid API key" in unhealthy["tmdb"]["message"]


def test_healthcheck_job_skips_unconfigured_optional_integrations(monkeypatch):
    from plex_recommender.jobs import JobManager, tmdb, overseerr, tautulli
    from plex_recommender.health import get_all_health

    monkeypatch.setattr(tmdb, "test_connection", lambda: (True, "Connected to TMDb"))
    monkeypatch.setattr(overseerr, "is_configured", lambda: False)
    monkeypatch.setattr(tautulli, "is_configured", lambda: False)

    jm = JobManager()
    res = jm.run_healthcheck(trigger="manual")
    assert res["success"] is True
    assert "not configured" in res["detail"]

    health = get_all_health()
    # Never checked (or cleared) -> ok is None, not False; must not appear as "unhealthy"
    assert health["overseerr"]["ok"] is None
    assert health["tautulli"]["ok"] is None


def test_healthcheck_job_tautulli_server_mismatch_flagged_unhealthy(monkeypatch):
    from plex_recommender.jobs import JobManager, tmdb, overseerr, tautulli
    from plex_recommender.health import get_health

    monkeypatch.setattr(tmdb, "test_connection", lambda: (True, "Connected to TMDb"))
    monkeypatch.setattr(overseerr, "is_configured", lambda: False)
    monkeypatch.setattr(tautulli, "is_configured", lambda: True)
    monkeypatch.setattr(tautulli, "test_connection", lambda: (True, "Connected to Tautulli"))
    monkeypatch.setattr(tautulli, "monitors_server", lambda machine_id: False)

    jm = JobManager()
    res = jm.run_healthcheck(trigger="manual")
    assert res["success"] is True

    status = get_health("tautulli")
    assert status["ok"] is False
    assert "different Plex server" in status["message"]


def test_healthcheck_job_concurrency_blocks_duplicate():
    from plex_recommender.jobs import JobManager
    jm = JobManager()
    jm.running_jobs["healthcheck"] = {
        "job_type": "healthcheck",
        "trigger": "manual",
        "status": "Running...",
        "progress": 50.0,
    }
    res = jm.run_healthcheck(trigger="manual")
    assert res["success"] is False
    assert res["already_running"] is True


