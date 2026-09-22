import pytest
from plex_recommender.config import settings
from plex_recommender.db import init_db
from plex_recommender.health import record_health, get_health, get_all_health, get_unhealthy, clear_health


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_health.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    yield


def test_get_health_defaults_to_none_when_never_checked():
    status = get_health("tmdb")
    assert status == {"ok": None, "message": None, "checked_at": None}


def test_record_and_get_health_ok():
    record_health("tmdb", True, "Connected to TMDb")
    status = get_health("tmdb")
    assert status["ok"] is True
    assert status["message"] == "Connected to TMDb"
    assert status["checked_at"] is not None


def test_record_and_get_health_failure():
    record_health("overseerr", False, "Unauthorized")
    status = get_health("overseerr")
    assert status["ok"] is False
    assert status["message"] == "Unauthorized"


def test_get_all_health_returns_all_three_integrations():
    record_health("tmdb", True, "ok")
    all_health = get_all_health()
    assert set(all_health.keys()) == {"tmdb", "overseerr", "tautulli"}
    assert all_health["tmdb"]["ok"] is True
    assert all_health["overseerr"]["ok"] is None


def test_get_unhealthy_filters_to_failing_only():
    record_health("tmdb", True, "ok")
    record_health("overseerr", False, "down")
    unhealthy = get_unhealthy()
    assert set(unhealthy.keys()) == {"overseerr"}


def test_clear_health_resets_to_unknown():
    record_health("tautulli", False, "mismatch")
    clear_health("tautulli")
    status = get_health("tautulli")
    assert status["ok"] is None


def test_invalid_integration_name_raises():
    with pytest.raises(ValueError):
        record_health("bogus", True, "x")
    with pytest.raises(ValueError):
        get_health("bogus")
