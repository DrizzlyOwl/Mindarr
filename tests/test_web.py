import pytest
from starlette.testclient import TestClient
from plex_recommender.config import settings
from plex_recommender.db import init_db, create_or_update_user, upsert_user_media, record_watch_event
from plex_recommender.web.app import app

USER = "u1"


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_web.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    yield


def _login(client):
    """Seed an admin user and attach a valid session cookie."""
    create_or_update_user({
        "user_key": USER, "username": "alice", "email": "a@x.com",
        "title": "Alice", "is_admin": True,
    })
    import itsdangerous, json, base64
    signer = itsdangerous.TimestampSigner(settings.session_secret)
    cookie = signer.sign(base64.b64encode(json.dumps({"user_key": USER}).encode())).decode()
    client.cookies.set("session", cookie)


def test_unauthenticated_redirects_to_login():
    with TestClient(app) as client:
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

        resp_recs = client.get("/recommendations", follow_redirects=False)
        assert resp_recs.status_code == 303
        assert resp_recs.headers["location"] == "/login"

        resp_settings = client.get("/settings", follow_redirects=False)
        assert resp_settings.status_code == 303
        assert resp_settings.headers["location"] == "/login"


def test_login_page_renders():
    with TestClient(app) as client:
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "Sign in with Plex" in resp.text
        # Fresh setup: first user becomes admin
        assert "Administrator" in resp.text


def test_sync_status_requires_no_auth_but_returns_global():
    with TestClient(app) as client:
        resp = client.get("/api/sync/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "is_syncing" in data
        assert "stats" in data


def test_recommendations_gated_until_history_exists():
    with TestClient(app) as client:
        _login(client)
        # No watch history yet -> route redirects to dashboard
        resp = client.get("/recommendations", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/?recs_not_ready=1"

        # Nav item is disabled (no clickable link to /recommendations)
        dash = client.get("/")
        assert 'href="/recommendations"' not in dash.text
        assert "Recommendations Locked" in dash.text


def test_recommendations_accessible_after_history():
    with TestClient(app) as client:
        _login(client)
        upsert_user_media(USER, {
            "item_id": "555", "media_type": "movie", "title": "Some Movie",
            "year": 2020, "genres": ["Sci-Fi"], "tmdb_id": "278",
        })
        # Route is now reachable
        resp = client.get("/recommendations", follow_redirects=False)
        assert resp.status_code == 200
        # Page shell renders skeleton loaders; cards are fetched in background
        assert 'id="recs-skeleton"' in resp.text
        assert 'id="recs-results"' in resp.text
        # Nav item is clickable again
        dash = client.get("/")
        assert 'href="/recommendations"' in dash.text


def test_api_recommendations_returns_card_html():
    with TestClient(app) as client:
        _login(client)
        upsert_user_media(USER, {
            "item_id": "555", "media_type": "movie", "title": "Some Movie",
            "year": 2020, "genres": ["Sci-Fi"], "tmdb_id": "278",
        })
        resp = client.get("/api/recommendations?type=movie")
        assert resp.status_code == 200
        data = resp.json()
        assert "html" in data
        assert "has_results" in data
        assert "from_cache" in data


def test_api_recommendations_requires_auth():
    with TestClient(app) as client:
        resp = client.get("/api/recommendations")
        assert resp.status_code == 401



def test_settings_warns_on_tautulli_server_mismatch(monkeypatch):
    from plex_recommender.web import app as webapp
    from plex_recommender.config import settings as cfg
    monkeypatch.setattr(cfg, "plex_machine_id", "MACHINE-A")
    monkeypatch.setattr(webapp.verify_plex_connection, "__call__", lambda *a, **k: (True, "ok"), raising=False)
    monkeypatch.setattr(webapp.overseerr, "test_connection", lambda: (False, "n/a"))
    monkeypatch.setattr(webapp.tautulli, "test_connection", lambda: (True, "Connected to Tautulli"))
    monkeypatch.setattr(webapp.tautulli, "monitors_server", lambda mid: False)
    monkeypatch.setattr(cfg, "plex_token", None)  # skip real plex verify
    with TestClient(app) as client:
        _login(client)
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert "not monitoring the linked Plex server" in resp.text


def test_settings_no_warning_when_tautulli_matches(monkeypatch):
    from plex_recommender.web import app as webapp
    from plex_recommender.config import settings as cfg
    monkeypatch.setattr(cfg, "plex_machine_id", "MACHINE-A")
    monkeypatch.setattr(webapp.overseerr, "test_connection", lambda: (False, "n/a"))
    monkeypatch.setattr(webapp.tautulli, "test_connection", lambda: (True, "Connected to Tautulli"))
    monkeypatch.setattr(webapp.tautulli, "monitors_server", lambda mid: True)
    monkeypatch.setattr(cfg, "plex_token", None)
    with TestClient(app) as client:
        _login(client)
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert "not monitoring the linked Plex server" not in resp.text


def test_jobs_page_requires_login():
    with TestClient(app) as client:
        resp = client.get("/settings/jobs", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")


def test_jobs_page_renders_history():
    from plex_recommender.db import start_job, finish_job
    with TestClient(app) as client:
        _login(client)
        jid = start_job("manual_sync", "manual", user_key=USER)
        finish_job(jid, "success", "all good")
        resp = client.get("/settings/jobs")
        assert resp.status_code == 200
        assert "Background Job History" in resp.text
        assert "manual_sync" in resp.text
        assert "Success" in resp.text


def test_homepage_shows_taste_profile_card():
    with TestClient(app) as client:
        _login(client)
        upsert_user_media(USER, {
            "item_id": "m1", "media_type": "movie", "title": "Sci Movie",
            "year": 2021, "genres": ["Science Fiction"], "actors": ["Actor A"],
            "view_count": 1, "last_viewed_at": "2026-01-01T00:00:00",
        })
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Your Taste Profile" in resp.text
        assert "analyzed titles" in resp.text


def test_taste_profile_does_not_show_data_sources_accordion():
    from plex_recommender.db import upsert_user_media, record_watch_event
    with TestClient(app) as client:
        _login(client)
        upsert_user_media(USER, {
            "item_id": "s1", "media_type": "show", "title": "Show",
            "year": 2021, "genres": ["Comedy"], "actors": ["Actor A"],
            "view_count": 1, "last_viewed_at": "2026-01-01T00:00:00",
        })
        record_watch_event({"user_key": USER, "item_id": "s1", "viewed_at": "2026-01-01T00:00:00", "source": "tautulli"})
        record_watch_event({"user_key": USER, "item_id": "s1", "viewed_at": "2026-01-02T00:00:00", "source": "plex"})
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Data sources" not in resp.text
        assert "Viewing Trends & Taste Profile" in resp.text


def test_env_managed_setting_is_locked_and_not_overwritten(monkeypatch):
    from plex_recommender import config as cfg
    from plex_recommender.config import settings as cfg_settings
    # Simulate TAUTULLI_URL provided by Docker/OS env
    monkeypatch.setattr(cfg, "ENV_MANAGED_KEYS", frozenset({"TAUTULLI_URL"}))
    assert cfg_settings.is_locked("TAUTULLI_URL") is True
    assert cfg_settings.is_locked("TMDB_API_KEY") is False

    # save_setting must no-op for a locked key
    before = cfg_settings.tautulli_url
    cfg_settings.save_setting("TAUTULLI_URL", "http://should-not-apply:9999")
    assert cfg_settings.tautulli_url == before


def test_settings_page_marks_env_locked_fields(monkeypatch):
    from plex_recommender import config as cfg
    monkeypatch.setattr(cfg, "ENV_MANAGED_KEYS", frozenset({"TAUTULLI_URL"}))
    from plex_recommender.web import app as webapp
    monkeypatch.setattr(webapp.overseerr, "test_connection", lambda: (False, "n/a"))
    monkeypatch.setattr(webapp.tautulli, "test_connection", lambda: (False, "n/a"))
    monkeypatch.setattr(webapp.settings, "plex_token", None)
    with TestClient(app) as client:
        _login(client)
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert "Managed by env var TAUTULLI_URL" in resp.text
        assert "managed by environment variables" in resp.text


def test_os_env_vars_survive_reload_with_conflicting_env_file(tmp_path, monkeypatch):
    from plex_recommender import config as cfg
    env_file = tmp_path / ".env"
    env_file.write_text("TMDB_API_KEY=stale_key_from_file\nPLEX_URL=https://stale:32400\n")

    monkeypatch.setattr(cfg, "ENV_FILE", env_file)
    monkeypatch.setattr(cfg, "ENV_MANAGED_KEYS", frozenset({"TMDB_API_KEY", "PLEX_URL"}))
    monkeypatch.setattr(cfg, "OS_ENV_SNAPSHOT", {
        "TMDB_API_KEY": "docker_injected_tmdb_key",
        "PLEX_URL": "https://docker-plex:32400",
    })

    import os
    os.environ["TMDB_API_KEY"] = "docker_injected_tmdb_key"
    os.environ["PLEX_URL"] = "https://docker-plex:32400"

    cfg.settings.reload()

    assert cfg.settings.tmdb_api_key == "docker_injected_tmdb_key"
    assert cfg.settings.plex_url == "https://docker-plex:32400"
    assert os.environ["TMDB_API_KEY"] == "docker_injected_tmdb_key"
    assert os.environ["PLEX_URL"] == "https://docker-plex:32400"


def test_settings_page_displays_os_env_values(monkeypatch):
    from plex_recommender import config as cfg
    from plex_recommender.web import app as webapp

    monkeypatch.setattr(cfg, "ENV_MANAGED_KEYS", frozenset({"TMDB_API_KEY", "PLEX_URL"}))
    monkeypatch.setattr(cfg.settings, "tmdb_api_key", "docker_key_value")
    monkeypatch.setattr(cfg.settings, "plex_url", "https://docker-plex:32400")
    monkeypatch.setattr(webapp.settings, "tmdb_api_key", "docker_key_value")
    monkeypatch.setattr(webapp.settings, "plex_url", "https://docker-plex:32400")
    monkeypatch.setattr(webapp.overseerr, "test_connection", lambda: (False, "n/a"))
    monkeypatch.setattr(webapp.tautulli, "test_connection", lambda: (False, "n/a"))
    monkeypatch.setattr(webapp.settings, "plex_token", None)

    with TestClient(app) as client:
        _login(client)
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert "value=\"https://docker-plex:32400\"" in resp.text
        assert "value=\"docker_key_value\"" in resp.text
        assert "Managed by env var TMDB_API_KEY" in resp.text
        assert "Managed by env var PLEX_URL" in resp.text


def test_system_page_requires_admin():
    with TestClient(app) as client:
        resp = client.get("/settings/system", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login")


def test_system_page_shows_db_and_user_data():
    from plex_recommender.db import upsert_user_media
    with TestClient(app) as client:
        _login(client)
        upsert_user_media(USER, {
            "item_id": "s1", "media_type": "show", "title": "Show",
            "year": 2021, "genres": ["Comedy"], "view_count": 1,
            "last_viewed_at": "2026-01-01T00:00:00",
        })
        resp = client.get("/settings/system")
        assert resp.status_code == 200
        assert "Database Footprint" in resp.text
        assert "User Data" in resp.text
        assert "Watched Items" in resp.text
        assert "Last Seen" in resp.text


def test_clear_user_data_removes_watch_data():
    from plex_recommender.db import upsert_user_media, has_user_history, get_user
    with TestClient(app) as client:
        _login(client)
        upsert_user_media(USER, {
            "item_id": "s1", "media_type": "show", "title": "Show",
            "year": 2021, "genres": ["Comedy"], "view_count": 1,
            "last_viewed_at": "2026-01-01T00:00:00",
        })
        assert has_user_history(USER) is True
        resp = client.post("/api/system/clear-user", data={"user_key": USER}, follow_redirects=False)
        assert resp.status_code == 303
        # Data gone, but account retained
        assert has_user_history(USER) is False
        assert get_user(USER) is not None


def test_clear_user_data_forbidden_without_admin():
    with TestClient(app) as client:
        resp = client.post("/api/system/clear-user", data={"user_key": USER}, follow_redirects=False)
        assert resp.status_code == 403


def test_sync_blocked_when_already_active():
    from plex_recommender.web import app as webapp
    with TestClient(app) as client:
        _login(client)
        # Simulate an in-progress sync for this user
        webapp.sync_state["is_syncing"] = True
        webapp.sync_state["user_key"] = USER
        try:
            resp = client.post("/api/sync", follow_redirects=False)
            assert resp.status_code == 409
            data = resp.json()
            assert data["status"] == "already_syncing"
            assert "your account" in data["message"]
        finally:
            webapp.sync_state["is_syncing"] = False
            webapp.sync_state["user_key"] = None


def test_sync_blocked_when_another_user_active():
    from plex_recommender.web import app as webapp
    with TestClient(app) as client:
        _login(client)
        webapp.sync_state["is_syncing"] = True
        webapp.sync_state["user_key"] = "someone-else"
        try:
            resp = client.post("/api/sync", follow_redirects=False)
            assert resp.status_code == 409
            assert resp.json()["status"] == "already_syncing"
        finally:
            webapp.sync_state["is_syncing"] = False
            webapp.sync_state["user_key"] = None


def test_sync_requires_login():
    with TestClient(app) as client:
        resp = client.post("/api/sync", follow_redirects=False)
        assert resp.status_code == 401


def test_sources_unauthenticated_redirects_to_login():
    with TestClient(app) as client:
        resp = client.get("/sources/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

        resp_noslash = client.get("/sources", follow_redirects=False)
        assert resp_noslash.status_code == 303
        assert resp_noslash.headers["location"] == "/login"

        resp_hist = client.get("/history/", follow_redirects=False)
        assert resp_hist.status_code == 303
        assert resp_hist.headers["location"] == "/login"


def test_watch_history_page_renders():
    with TestClient(app) as client:
        _login(client)
        resp = client.get("/sources/")
        assert resp.status_code == 200
        assert "Watch History" in resp.text
        assert "Recent Plays Shown" in resp.text
        assert "Total Recorded Plays" in resp.text
        assert "Recent Watch Time" in resp.text
        assert "Data Provenance Key" not in resp.text
        assert "No Watch History Found" in resp.text

        # Also verify /history alias works identically
        resp_hist = client.get("/history/")
        assert resp_hist.status_code == 200
        assert "Watch History" in resp_hist.text


def test_watch_history_page_renders_events_and_sorting():
    with TestClient(app) as client:
        _login(client)

        # Event 1: Plex API, older
        record_watch_event({
            "user_key": USER,
            "item_id": "101",
            "title": "Older Plex Movie",
            "media_type": "movie",
            "viewed_at": "2024-01-10T14:00:00",
            "source": "plex",
            "duration_watched": 5400000,
        })

        # Event 2: Tautulli, newer
        record_watch_event({
            "user_key": USER,
            "item_id": "102",
            "title": "Newer Tautulli Show",
            "media_type": "episode",
            "viewed_at": "2024-02-15T20:30:00",
            "source": "tautulli",
            "duration_watched": 2700,
        })

        # Event 3: Another user's event (isolation test)
        record_watch_event({
            "user_key": "other-user",
            "item_id": "999",
            "title": "Secret Other User Movie",
            "media_type": "movie",
            "viewed_at": "2024-03-01T12:00:00",
            "source": "plex",
            "duration_watched": 3600,
        })

        resp = client.get("/sources/")
        assert resp.status_code == 200
        text = resp.text

        # Ensure other user's event is not visible
        assert "Secret Other User Movie" not in text

        # Both user events visible
        assert "Newer Tautulli Show" in text
        assert "Older Plex Movie" in text

        # Sort order: most recent first (Newer Tautulli Show before Older Plex Movie)
        idx_newer = text.find("Newer Tautulli Show")
        idx_older = text.find("Older Plex Movie")
        assert idx_newer != -1 and idx_older != -1
        assert idx_newer < idx_older, "Expected most recent event to appear first in the HTML"

        # Separate archive/live provenance badges are not displayed
        assert "Enriched archive record" not in text
        assert "Direct server playback log" not in text

        # Verify data binding and details action trigger
        assert "window.WATCH_EVENTS_DATA = [" in text
        assert 'onclick="openDetailDrawer(0)"' in text
        assert 'onclick="openDetailDrawer(1)"' in text
        # Verify details button specifically has the onclick action
        assert '<button type="button" onclick="openDetailDrawer(' in text
        # Verify detail drawer elements exist
        assert 'id="detail-drawer"' in text
        assert 'id="detail-drawer-backdrop"' in text


def test_sources_page_limits_to_100_items():
    with TestClient(app) as client:
        _login(client)

        for i in range(105):
            record_watch_event({
                "user_key": USER,
                "item_id": f"item_{i}",
                "title": f"Movie Item {i:03d}",
                "media_type": "movie",
                "viewed_at": f"2024-01-01T{i % 24:02d}:00:00",
                "source": "plex" if i % 2 == 0 else "tautulli",
                "duration_watched": 3600,
            })

        resp = client.get("/sources/")
        assert resp.status_code == 200
        # Exactly 100 rows rendered in table (0 to 99), 101st item not present
        assert 'data-index="99"' in resp.text
        assert 'data-index="100"' not in resp.text
        assert resp.text.count('<tr class="hover:bg-slate-800/40') == 100


def test_nav_submenu_contains_sources():
    with TestClient(app) as client:
        _login(client)
        resp = client.get("/")
        assert resp.status_code == 200
        assert 'href="/sources/"' in resp.text
        assert "Watch History" in resp.text
        assert 'href="/community/"' in resp.text
        assert "Community" in resp.text


def test_nav_bar_structure_and_user_menu():
    with TestClient(app) as client:
        _login(client)
        resp = client.get("/")
        assert resp.status_code == 200
        # Streamlined primary labels
        assert "Dashboard" in resp.text
        assert "Recommendations" in resp.text
        # Settings is moved out of main nav into the user dropdown
        assert 'id="user-menu-container"' in resp.text
        assert 'id="user-menu-dropdown"' in resp.text
        assert 'id="nav-sync-btn"' in resp.text
        assert 'id="mobile-menu"' in resp.text
        # User dropdown contains admin settings
        assert 'href="/settings"' in resp.text
        assert 'href="/settings/jobs"' in resp.text
        assert 'href="/settings/system"' in resp.text
        assert 'href="/logout"' in resp.text
        # Low bandwidth toggle is inside user menu & mobile menu
        assert 'id="low-bandwidth-toggle"' in resp.text


def test_nav_bar_non_admin_user_menu():
    with TestClient(app) as client:
        create_or_update_user({
            "user_key": "u_regular", "username": "bob", "email": "bob@x.com",
            "title": "Bob", "is_admin": False,
        })
        import itsdangerous, json, base64
        signer = itsdangerous.TimestampSigner(settings.session_secret)
        cookie = signer.sign(base64.b64encode(json.dumps({"user_key": "u_regular"}).encode())).decode()
        client.cookies.set("session", cookie)

        resp = client.get("/")
        assert resp.status_code == 200
        # Non-admin sees user menu, but not admin settings
        assert 'id="user-menu-dropdown"' in resp.text
        assert 'href="/settings"' not in resp.text
        assert 'href="/settings/jobs"' not in resp.text
        assert 'href="/settings/system"' not in resp.text
        assert 'href="/logout"' in resp.text


def test_community_unauthenticated_redirects_to_login():
    with TestClient(app) as client:
        resp = client.get("/community/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

        resp_noslash = client.get("/community", follow_redirects=False)
        assert resp_noslash.status_code == 303
        assert resp_noslash.headers["location"] == "/login"


def test_community_page_renders_single_user_state(monkeypatch):
    from unittest.mock import patch
    from plex_recommender.discovery.tautulli import tautulli
    with patch.object(tautulli, "is_configured", return_value=False):
        with TestClient(app) as client:
            _login(client)
            resp = client.get("/community/")
            assert resp.status_code == 200
            assert "Community &amp; Server Trends" in resp.text or "Community & Server Trends" in resp.text
            assert "Unlock Full Community Benchmarks" in resp.text


def test_community_page_privacy_and_peer_recommendations():
    from unittest.mock import patch
    from plex_recommender.community import community_service

    mock_stats = [
        {
            "stat_id": "popular_movies",
            "rows": [
                {
                    "title": "Community Hit Movie",
                    "year": 2024,
                    "users_watched": 4,
                    "total_plays": 8,
                    "rating_key": "999",
                }
            ],
        },
        {
            "stat_id": "popular_tv",
            "rows": [
                {
                    "title": "Community Hit Show",
                    "year": 2023,
                    "users_watched": 3,
                    "total_plays": 12,
                    "rating_key": "888",
                }
            ],
        },
    ]

    mock_history = [
        {
            "title": "Private Watch Show",
            "media_type": "episode",
            "year": 2024,
            "started": 1700000000,
            "duration": 1800,
            "user": "SecretFriend99",
            "email": "secret@example.com",
            "rating_key": "777",
        }
    ]

    with patch.object(community_service, "get_community_data") as mock_get:
        mock_get.return_value = {
            "is_single_user": False,
            "total_community_members": 25,
            "source": "tautulli",
            "benchmarks": {
                "user_plays": 10,
                "user_movie_pct": 60,
                "user_tv_pct": 40,
                "community_movie_pct": 55,
                "community_tv_pct": 45,
                "overlap_count": 1,
                "overlap_pct": 50,
            },
            "popular_movies": [
                {
                    "title": "Community Hit Movie",
                    "year": 2024,
                    "media_type": "movie",
                    "rating_key": "999",
                    "users_watched": 4,
                    "total_plays": 8,
                    "seen_by_user": False,
                    "genres": ["Action"],
                    "summary": "Great community film.",
                }
            ],
            "popular_tv": [
                {
                    "title": "Community Hit Show",
                    "year": 2023,
                    "media_type": "show",
                    "rating_key": "888",
                    "users_watched": 3,
                    "total_plays": 12,
                    "seen_by_user": True,
                    "genres": ["Drama"],
                    "summary": "Popular series.",
                }
            ],
            "unseen_recommendations": [
                {
                    "title": "Community Hit Movie",
                    "year": 2024,
                    "media_type": "movie",
                    "users_watched": 4,
                    "total_plays": 8,
                    "genres": ["Action"],
                    "summary": "Great community film.",
                }
            ],
            "recent_activity": [
                {
                    "title": "Private Watch Show",
                    "year": 2024,
                    "media_type": "episode",
                    "time_str": "2024-03-01 12:00",
                    "duration_formatted": "30m",
                    "is_me": False,
                    "attribution": "Watched by a community member",
                    "seen_by_user": False,
                }
            ],
        }

        with TestClient(app) as client:
            _login(client)
            resp = client.get("/community/")
            assert resp.status_code == 200
            text = resp.text

            # Community picks visible
            assert "Community Hit Movie" in text
            assert "Watched by Others, Unseen by You" in text
            assert "4 viewers" in text

            # Privacy Option B verification:
            # Other usernames and emails MUST NEVER appear in the rendered page
            assert "SecretFriend99" not in text
            assert "secret@example.com" not in text
            assert "Watched by a community member" in text


def test_jobs_page_renders_configured_tasks():
    with TestClient(app) as client:
        _login(client)
        resp = client.get("/settings/jobs")
        assert resp.status_code == 200
        assert "Active Background Tasks" in resp.text
        assert "Plex &amp; Tautulli Sync" in resp.text
        assert "User Recommendations" in resp.text
        assert "Run Now" in resp.text


def test_api_jobs_run_and_status():
    with TestClient(app) as client:
        _login(client)
        # Check initial status
        st_resp = client.get("/api/jobs/status")
        assert st_resp.status_code == 200
        data = st_resp.json()
        assert "configured_jobs" in data
        assert len(data["configured_jobs"]) >= 2

        # Trigger recommendations job
        resp = client.post("/api/jobs/run", data={"job_type": "recommendations"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

        # Invalid job type
        bad_resp = client.post("/api/jobs/run", data={"job_type": "unknown_job"})
        assert bad_resp.status_code == 400


def test_api_jobs_run_requires_admin():
    from plex_recommender.db import create_or_update_user
    create_or_update_user({
        "user_key": "reg_u", "username": "bob", "email": "b@x.com",
        "title": "Bob", "is_admin": False,
    })
    import itsdangerous, json, base64
    signer = itsdangerous.TimestampSigner(settings.session_secret)
    cookie = signer.sign(base64.b64encode(json.dumps({"user_key": "reg_u"}).encode())).decode()

    with TestClient(app) as client:
        client.cookies.set("session", cookie)
        resp = client.post("/api/jobs/run", data={"job_type": "sync"})
        assert resp.status_code == 403


def test_settings_saves_automation_schedules():
    with TestClient(app) as client:
        _login(client)
        resp = client.post(
            "/api/settings/save",
            data={
                "auto_sync_hours": "12",
                "auto_recommendations_hours": "6",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert settings.auto_sync_hours == 12
        assert settings.auto_recommendations_hours == 6


def test_header_displays_version_instead_of_truenas():
    from plex_recommender import __version__
    with TestClient(app) as client:
        _login(client)
        resp = client.get("/")
        assert resp.status_code == 200
        assert f"v{__version__}" in resp.text
        assert "TrueNAS Edition" not in resp.text


def test_api_overseerr_request_resolves_user_id(monkeypatch):
    from plex_recommender.web import app as webapp
    from unittest.mock import MagicMock

    mock_overseerr = MagicMock()
    mock_overseerr.get_media_info.return_value = {"exists": False, "status_code": 1}
    mock_overseerr.resolve_user_id.return_value = 42
    mock_overseerr.request_media.return_value = (True, "Request submitted!")
    monkeypatch.setattr(webapp, "overseerr", mock_overseerr)

    with TestClient(app) as client:
        _login(client)
        resp = client.post("/api/overseerr/request", data={"tmdb_id": "12345", "media_type": "movie"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["overseerr_user_id"] == 42
        mock_overseerr.resolve_user_id.assert_called_once_with(plex_user_key=USER, email="a@x.com", username="alice")
        mock_overseerr.request_media.assert_called_once_with(tmdb_id=12345, media_type="movie", is_4k=False, seasons=None, user_id=42)


def test_api_overseerr_request_blocks_duplicates(monkeypatch):
    from plex_recommender.web import app as webapp
    from unittest.mock import MagicMock

    mock_overseerr = MagicMock()
    mock_overseerr.get_media_info.return_value = {
        "exists": True,
        "status_code": 3,
        "status_name": "PROCESSING"
    }
    monkeypatch.setattr(webapp, "overseerr", mock_overseerr)

    with TestClient(app) as client:
        _login(client)
        resp = client.post("/api/overseerr/request", data={"tmdb_id": "12345", "media_type": "movie"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data.get("already_requested") is True
        assert "already downloading in the server queue" in data["message"]
        # request_media should NEVER be called when already queued
        mock_overseerr.request_media.assert_not_called()


def test_recommendation_card_renders_anonymous_queue_status():
    from plex_recommender.web.app import templates
    from starlette.requests import Request

    fake_scope = {"type": "http", "method": "GET", "path": "/"}
    req = Request(fake_scope)

    # 1. Processing / Downloading item
    card_html = templates.get_template("_recommendation_cards.html").render({
        "request": req,
        "results": {
            "recommendations": [
                {
                    "tmdb_id": "27205",
                    "media_type": "movie",
                    "title": "Inception",
                    "overseerr_requested": True,
                    "overseerr_status": "PROCESSING",
                    "overseerr_url": "http://overseerr/movie/27205",
                    "available_on_plex": False,
                }
            ]
        },
        "has_overseerr": True,
        "low_bandwidth": False
    })

    assert "Downloading in Overseerr" in card_html
    assert "Downloading" in card_html
    # Ensure no personal username is shown
    assert "alice" not in card_html

    # 2. Pending approval item
    card_html_pending = templates.get_template("_recommendation_cards.html").render({
        "request": req,
        "results": {
            "recommendations": [
                {
                    "tmdb_id": "70523",
                    "media_type": "show",
                    "title": "Dark",
                    "overseerr_requested": True,
                    "overseerr_status": "PENDING",
                    "overseerr_url": "http://overseerr/tv/70523",
                    "available_on_plex": False,
                }
            ]
        },
        "has_overseerr": True,
        "low_bandwidth": False
    })

    assert "Already in Request Queue" in card_html_pending
    assert "In Queue" in card_html_pending



def test_api_recommendations_dismiss_and_undismiss():
    from plex_recommender.db import get_user_dismissals
    with TestClient(app) as client:
        _login(client)

        # 1. Dismiss title
        resp = client.post(
            "/api/recommendations/dismiss",
            data={
                "tmdb_id": "9876",
                "media_type": "movie",
                "title": "Hidden Gem",
                "year": "2022",
                "reason": "already_watched",
            }
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # 2. Verify in dismissed list
        list_resp = client.get("/api/recommendations/dismissed")
        assert list_resp.status_code == 200
        items = list_resp.json()["dismissals"]
        assert len(items) == 1
        assert items[0]["tmdb_id"] == "9876"
        assert items[0]["reason"] == "already_watched"

        # 3. Undismiss title
        un_resp = client.post("/api/recommendations/undismiss", data={"tmdb_id": "9876"})
        assert un_resp.status_code == 200
        assert un_resp.json()["success"] is True

        # 4. Verify list empty
        assert len(get_user_dismissals(USER)) == 0


def test_settings_logs_page_and_actions():
    from plex_recommender.db import insert_system_log, get_system_logs
    import time

    now = time.time()
    insert_system_log(level="ERROR", logger_name="test.err", message="Critical error occurred", epoch=now)
    insert_system_log(level="INFO", logger_name="test.info", message="Regular info log", epoch=now - 100)

    with TestClient(app) as client:
        _login(client)

        # View logs page
        resp = client.get("/settings/logs")
        assert resp.status_code == 200
        assert "System Logs" in resp.text
        assert "Critical error occurred" in resp.text
        assert "Regular info log" in resp.text

        # Filter by level
        err_resp = client.get("/settings/logs?level=ERROR")
        assert err_resp.status_code == 200
        assert "Critical error occurred" in err_resp.text
        assert "Regular info log" not in err_resp.text

        # Truncate logs
        trunc_resp = client.post("/api/logs/truncate", data={"days": "7"}, follow_redirects=True)
        assert trunc_resp.status_code == 200

        # Clear logs
        clear_resp = client.post("/api/logs/clear", follow_redirects=True)
        assert clear_resp.status_code == 200
        assert len(get_system_logs()) == 0


def test_api_recommendations_vote_and_unvote():
    from plex_recommender.db import get_user_votes, get_user_dismissals, get_system_logs
    with TestClient(app) as client:
        _login(client)

        # 1. Upvote (+1)
        up_resp = client.post(
            "/api/recommendations/vote",
            data={
                "tmdb_id": "8080",
                "media_type": "movie",
                "title": "Super Sci-Fi",
                "year": "2023",
                "vote": "1",
            }
        )
        assert up_resp.status_code == 200
        assert up_resp.json()["vote"] == 1
        votes = get_user_votes(USER)
        assert votes.get("8080") == 1

        # Check audit log for upvote
        logs = get_system_logs(search="upvoted recommendation")
        assert len(logs) >= 1
        assert "Super Sci-Fi" in logs[0]["message"]

        # 2. Downvote (-1)
        down_resp = client.post(
            "/api/recommendations/vote",
            data={
                "tmdb_id": "9090",
                "media_type": "show",
                "title": "Bad Reality",
                "year": "2024",
                "vote": "-1",
            }
        )
        assert down_resp.status_code == 200
        assert down_resp.json()["vote"] == -1
        votes = get_user_votes(USER)
        assert votes.get("9090") == -1

        # Check dismissed endpoint contains both dismissals and votes
        d_resp = client.get("/api/recommendations/dismissed")
        assert d_resp.status_code == 200
        data = d_resp.json()
        assert "dismissals" in data
        assert "votes" in data
        assert any(v["tmdb_id"] == "8080" for v in data["votes"])

        # 3. Unvote
        unvote_resp = client.post("/api/recommendations/unvote", data={"tmdb_id": "8080"})
        assert unvote_resp.status_code == 200
        votes_after = get_user_votes(USER)
        assert "8080" not in votes_after


def test_api_pool_stats():
    from plex_recommender.db import clear_user_data, record_vote, dismiss_item

    with TestClient(app) as client:
        _login(client)
        clear_user_data(USER)

        resp = client.get("/api/recommendations/pool-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["dismissals_count"] == 0
        assert data["upvotes_count"] == 0
        assert data["downvotes_count"] == 0

        # Add an upvote and a dismissal
        record_vote(USER, tmdb_id="1111", media_type="movie", title="Good", vote=1)
        dismiss_item(USER, tmdb_id="2222", media_type="movie", title="Seen", reason="already_watched")

        resp2 = client.get("/api/recommendations/pool-stats")
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["upvotes_count"] == 1
        assert data2["dismissals_count"] == 1


def test_api_recommendations_exhaustion_rendering(monkeypatch):
    from unittest.mock import MagicMock
    from plex_recommender.web import app as webapp

    mock_rec = MagicMock()
    mock_rec.get_recommendations.return_value = {
        "success": True,
        "is_complete": True,
        "stage": "full",
        "total_candidates_scanned": 15,
        "unseen_count": 0,
        "recommendations": [],
        "page": 1,
        "total_pages": 1,
        "limit": 18,
        "query_genres": [],
        "from_cache": False,
        "exhaustion_reason": "exhausted",
    }
    monkeypatch.setattr(webapp, "recommender", mock_rec)
    monkeypatch.setattr(webapp.analyzer, "analyze", lambda uk: {"has_data": True})

    with TestClient(app) as client:
        _login(client)
        resp = client.get("/api/recommendations")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_results"] is False
        assert data["exhaustion_reason"] == "exhausted"
        assert "explored all available titles" in data["html"]
        assert "Review Tuned Preferences" in data["html"]
        assert "Reset Filters" in data["html"]






