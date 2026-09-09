import time
import pytest
from plex_recommender.config import settings
from plex_recommender.db import (
    init_db, start_job, finish_job, get_job_history, clear_job_history,
)


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
    from plex_recommender.db import upsert_discovered_user, get_user, create_or_update_user
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
    from plex_recommender.db import create_or_update_user, upsert_user_media, get_cached_recommendations, set_setting
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

