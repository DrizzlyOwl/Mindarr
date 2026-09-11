import logging
import time
import pytest
from starlette.testclient import TestClient

from plex_recommender.config import settings
from plex_recommender.db import init_db, create_or_update_user
from plex_recommender.logs import LogHandler, log_handler
from plex_recommender.web.app import app

USER = "u_log_test"


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_logs.db"
    test_env = tmp_path / ".env"
    monkeypatch.setenv("DB_PATH", str(test_db))
    monkeypatch.setattr(settings, "db_path", test_db)
    from plex_recommender import config as cfg
    monkeypatch.setattr(cfg, "ENV_FILE", test_env)
    init_db()
    create_or_update_user({
        "user_key": USER,
        "username": "log_user",
        "email": "log@test.local",
        "is_admin": True,
    })
    yield


def _login(client):
    import itsdangerous, json, base64
    signer = itsdangerous.TimestampSigner(settings.session_secret)
    cookie = signer.sign(base64.b64encode(json.dumps({"user_key": USER}).encode())).decode()
    client.cookies.set("session", cookie)


def test_log_handler_emit_and_retrieve():
    handler = LogHandler(level=logging.DEBUG)
    test_logger = logging.getLogger("test_emit_isolated")
    test_logger.propagate = False
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    test_logger.info("Test info message 123")
    test_logger.warning("Test warning message 456")
    test_logger.error("Test error message 789")

    logs = handler.get_logs(limit=10)
    assert len(logs) >= 3

    # Check newest first
    assert logs[0]["message"] == "Test error message 789"
    assert logs[1]["message"] == "Test warning message 456"
    assert logs[2]["message"] == "Test info message 123"

    # Filter by level
    warn_logs = handler.get_logs(level="WARNING")
    assert any(l["message"] == "Test warning message 456" for l in warn_logs)
    assert not any(l["message"] == "Test error message 789" for l in warn_logs)

    # Search filter
    search_logs = handler.get_logs(search="123")
    assert len(search_logs) == 1
    assert search_logs[0]["message"] == "Test info message 123"


def test_log_handler_truncate_and_clear():
    handler = LogHandler()
    now = time.time()
    ten_days_ago = now - (10 * 86400)
    eight_days_ago = now - (8 * 86400)
    two_days_ago = now - (2 * 86400)

    handler.log_event(level="INFO", logger_name="old_proc", message="10-day log", epoch=ten_days_ago)
    handler.log_event(level="INFO", logger_name="old_proc", message="8-day log", epoch=eight_days_ago)
    handler.log_event(level="INFO", logger_name="new_proc", message="2-day log", epoch=two_days_ago)
    handler.log_event(level="INFO", logger_name="new_proc", message="Today log", epoch=now)

    assert len(handler.get_logs()) == 4

    pruned = handler.truncate(days=7)
    assert pruned == 2

    remaining = handler.get_logs()
    assert len(remaining) == 2
    messages = [r["message"] for r in remaining]
    assert "Today log" in messages
    assert "2-day log" in messages
    assert "10-day log" not in messages

    # Clear
    handler.clear()
    assert len(handler.get_logs()) == 0


def test_log_handler_export_text():
    handler = LogHandler()
    handler.log_event(level="INFO", logger_name="export.test", message="Export line 1")
    handler.log_event(level="ERROR", logger_name="export.test", message="Export line 2")

    text = handler.export_text()
    assert "Export line 1" in text
    assert "Export line 2" in text
    assert "[INFO]" in text
    assert "[ERROR]" in text


def test_user_activity_logging_in_web_routes(monkeypatch):
    from unittest.mock import MagicMock
    from plex_recommender.web import app as webapp

    mock_overseerr = MagicMock()
    mock_overseerr.resolve_user_id.return_value = 99
    mock_overseerr.request_media.return_value = (True, "Requested successfully")
    monkeypatch.setattr(webapp, "overseerr", mock_overseerr)

    with TestClient(app) as client:
        _login(client)

        # 1. Dismiss recommendation
        client.post(
            "/api/recommendations/dismiss",
            data={
                "tmdb_id": "5555",
                "media_type": "movie",
                "title": "Test Movie Dismiss",
                "reason": "already_watched",
            }
        )
        logs = log_handler.get_logs(search="dismissed recommendation")
        assert len(logs) >= 1
        assert "Test Movie Dismiss" in logs[0]["message"]
        assert "watched outside Plex" in logs[0]["message"]

        # 2. Undismiss recommendation
        client.post("/api/recommendations/undismiss", data={"tmdb_id": "5555"})
        un_logs = log_handler.get_logs(search="restored recommendation")
        assert len(un_logs) >= 1
        assert "5555" in un_logs[0]["message"]

        # 3. Settings save
        client.post(
            "/api/settings/save",
            data={"auto_sync_hours": "18"},
            follow_redirects=False
        )
        set_logs = log_handler.get_logs(search="saved configuration changes")
        assert len(set_logs) >= 1

        # 4. Overseerr request
        client.post("/api/overseerr/request", data={"tmdb_id": "777", "media_type": "movie"})
        ov_logs = log_handler.get_logs(search="Overseerr")
        assert len(ov_logs) >= 1
        assert "777" in ov_logs[0]["message"]

        # 5. Clear user data
        client.post("/api/system/clear-user", data={"user_key": "some_other_user"})
        clear_logs = log_handler.get_logs(search="wiped all watch data")
        assert len(clear_logs) >= 1

        # 6. Export logs endpoint
        export_resp = client.get("/api/logs/export")
        assert export_resp.status_code == 200
        assert "Overseerr" in export_resp.text

        json_export = client.get("/api/logs/export?format=json")
        assert json_export.status_code == 200
        assert isinstance(json_export.json(), list)

        # 7. Sign out
        logout_resp = client.get("/logout", follow_redirects=False)
        assert logout_resp.status_code == 303
        logout_logs = log_handler.get_logs(search="signed out")
        assert len(logout_logs) >= 1


def test_job_lifecycle_logging():
    from plex_recommender.jobs import JobManager
    jm = JobManager()

    # Truncate logs job records lifecycle in log store
    res = jm.run_truncate_logs(trigger="manual", retention_days=7)
    assert res["success"] is True

    start_logs = log_handler.get_logs(search="Job 'Truncate Logs'")
    assert len(start_logs) >= 2  # started and completed
