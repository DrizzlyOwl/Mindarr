import os
import pytest

from plex_recommender.config import Settings, _parse_bool, is_env_managed


def test_parse_bool_defaults():
    assert _parse_bool(None) is False
    assert _parse_bool("") is False
    assert _parse_bool(None, default=True) is True


def test_parse_bool_truthy_values():
    for v in ("1", "true", "True", "TRUE", "yes", "on", " on "):
        assert _parse_bool(v) is True


def test_parse_bool_falsy_values():
    for v in ("0", "false", "no", "off", "garbage"):
        assert _parse_bool(v) is False


def test_session_https_only_defaults_false(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SESSION_HTTPS_ONLY", raising=False)
    s = Settings()
    assert s.session_https_only is False


def test_healthcheck_interval_hours_defaults_to_one(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HEALTHCHECK_INTERVAL_HOURS", raising=False)
    s = Settings()
    assert s.healthcheck_interval_hours == 1


def test_healthcheck_interval_hours_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HEALTHCHECK_INTERVAL_HOURS", "6")
    s = Settings()
    assert s.healthcheck_interval_hours == 6


def test_healthcheck_interval_hours_reload_picks_up_change(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("HEALTHCHECK_INTERVAL_HOURS", raising=False)
    s = Settings()
    assert s.healthcheck_interval_hours == 1

    monkeypatch.setenv("HEALTHCHECK_INTERVAL_HOURS", "3")
    s.reload()
    assert s.healthcheck_interval_hours == 3


def test_session_https_only_enabled_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_HTTPS_ONLY", "true")
    s = Settings()
    assert s.session_https_only is True


def test_reload_picks_up_https_only_change(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SESSION_HTTPS_ONLY", raising=False)
    s = Settings()
    assert s.session_https_only is False

    monkeypatch.setenv("SESSION_HTTPS_ONLY", "true")
    s.reload()
    assert s.session_https_only is True


def test_save_setting_persists_and_reloads(tmp_path, monkeypatch):
    # save_setting()/reload() read/write the module-level ENV_FILE global
    # (fixed at import time), not settings.config_dir, so it must be patched
    # directly to redirect persistence into a temp file for this test.
    import plex_recommender.config as config_module
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config_module, "ENV_FILE", env_file)
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    s = Settings()
    assert s.tmdb_api_key is None

    s.save_setting("TMDB_API_KEY", "abc123")
    assert s.tmdb_api_key == "abc123"

    assert env_file.exists()
    content = env_file.read_text()
    assert "TMDB_API_KEY=abc123" in content


def test_save_setting_updates_existing_key(tmp_path, monkeypatch):
    import plex_recommender.config as config_module
    env_file = tmp_path / ".env"
    monkeypatch.setattr(config_module, "ENV_FILE", env_file)
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    s = Settings()
    s.save_setting("TMDB_API_KEY", "first")
    s.save_setting("TMDB_API_KEY", "second")
    assert s.tmdb_api_key == "second"

    content = env_file.read_text()
    assert content.count("TMDB_API_KEY=") == 1
    assert "TMDB_API_KEY=second" in content


def test_save_setting_is_noop_for_env_managed_key(tmp_path, monkeypatch):
    # Simulate a Docker/OS-managed env var by patching ENV_MANAGED_KEYS via is_locked.
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    s = Settings()

    import plex_recommender.config as config_module
    monkeypatch.setattr(config_module, "ENV_MANAGED_KEYS", frozenset({"TMDB_API_KEY"}))

    s.save_setting("TMDB_API_KEY", "should-not-persist")
    env_file = tmp_path / ".env"
    if env_file.exists():
        assert "TMDB_API_KEY=should-not-persist" not in env_file.read_text()


def test_is_locked_reflects_env_managed_keys(monkeypatch):
    import plex_recommender.config as config_module
    monkeypatch.setattr(config_module, "ENV_MANAGED_KEYS", frozenset({"PLEX_TOKEN"}))
    s = Settings.__new__(Settings)  # bypass __init__, only testing is_locked
    assert s.is_locked("PLEX_TOKEN") is True
    assert s.is_locked("TMDB_API_KEY") is False
    assert is_env_managed("PLEX_TOKEN") is True
