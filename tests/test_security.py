import base64
import json
import itsdangerous
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from plex_recommender.config import settings
from plex_recommender.db import init_db
from plex_recommender.db.users import create_or_update_user, mark_onboarded
from plex_recommender.web.app import app, _PUBLIC_API_ROUTES


@pytest.fixture(autouse=True)
def setup_security_db(tmp_path, monkeypatch):
    from plex_recommender.web import app as webapp
    test_db = tmp_path / "test_security.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    webapp._AUTH_RATE_BUCKETS.clear()
    init_db()
    yield
    webapp._AUTH_RATE_BUCKETS.clear()


def _sign_cookie(user_key: str) -> str:
    signer = itsdangerous.TimestampSigner(settings.session_secret)
    return signer.sign(base64.b64encode(json.dumps({"user_key": user_key}).encode())).decode()


def _seed_shared_user(client: TestClient, user_key: str = "shared_sec_u") -> str:
    create_or_update_user({
        "user_key": user_key,
        "username": "shareduser",
        "email": "shared@example.com",
        "title": "Shared User",
        "is_admin": False,
    })
    mark_onboarded(user_key)
    client.cookies.set("session", _sign_cookie(user_key))
    return user_key


def test_every_non_public_api_route_rejects_unauthenticated_requests():
    """OWASP A01: Every /api/ endpoint (except pre-login OAuth) must reject unauthenticated calls with 401."""
    api_routes = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/"):
            if route.path in _PUBLIC_API_ROUTES:
                continue
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                api_routes.append((method, route.path))

    assert len(api_routes) >= 20

    with TestClient(app) as client:
        for method, path in api_routes:
            resp = client.request(method, path, follow_redirects=False)
            assert resp.status_code == 401, f"{method} {path} returned {resp.status_code} instead of 401"
            assert resp.json().get("error") == "unauthorized"


def test_non_admin_cannot_access_admin_api_or_pages():
    """OWASP A01: Shared (non-admin) users must receive 403 on all admin-only endpoints."""
    admin_api = [
        ("POST", "/api/settings/save", {"auto_sync_hours": "12"}),
        ("POST", "/api/jobs/run", {"job_type": "sync"}),
        ("GET", "/api/jobs/status", None),
        ("POST", "/api/jobs/clear", None),
        ("POST", "/api/system/clear-user", {"user_key": "someone"}),
        ("GET", "/api/logs/export", None),
        ("POST", "/api/logs/truncate", {"days": "7"}),
        ("POST", "/api/logs/clear", None),
        ("GET", "/api/plex/test", None),
        ("GET", "/api/overseerr/test", None),
        ("GET", "/api/tautulli/test", None),
        ("POST", "/api/community/deanonymize", None),
    ]
    admin_pages = [
        "/settings",
        "/settings/jobs",
        "/settings/system",
        "/settings/logs",
    ]

    with TestClient(app) as client:
        _seed_shared_user(client)

        for method, path, data in admin_api:
            resp = client.request(method, path, data=data, follow_redirects=False)
            assert resp.status_code == 403, f"{method} {path} returned {resp.status_code} instead of 403"

        for page in admin_pages:
            resp = client.get(page, follow_redirects=False)
            assert resp.status_code == 403, f"GET {page} returned {resp.status_code} instead of 403"


def test_csrf_guard_blocks_sec_fetch_site_cross_site_and_mismatched_referer():
    """OWASP A01: State-changing requests with Sec-Fetch-Site=cross-site or mismatched Referer/Origin are blocked."""
    with TestClient(app) as client:
        _seed_shared_user(client)

        # Stripped Origin/Referer, but browser sends Sec-Fetch-Site: cross-site
        r_sfs = client.post(
            "/api/recommendations/clear-cache",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert r_sfs.status_code == 403
        assert r_sfs.json()["error"] == "cross_origin_forbidden"

        # No Origin, mismatched Referer
        r_ref = client.post(
            "/api/recommendations/clear-cache",
            headers={"Referer": "https://attacker.example/form.html", "Host": "testserver"},
        )
        assert r_ref.status_code == 403
        assert r_ref.json()["error"] == "cross_origin_forbidden"

        # Same-origin Sec-Fetch-Site and Referer succeed
        r_ok = client.post(
            "/api/recommendations/clear-cache",
            headers={
                "Sec-Fetch-Site": "same-origin",
                "Referer": "http://testserver/recommendations",
                "Host": "testserver",
            },
        )
        assert r_ok.status_code == 200


def test_env_and_db_file_permissions_are_owner_only(tmp_path, monkeypatch):
    """OWASP A02: Secrets file (.env) and SQLite DB must be chmod 0600 on POSIX."""
    import os
    import stat
    import plex_recommender.config as config_module

    env_file = tmp_path / ".env"
    db_file = tmp_path / "perm.db"
    monkeypatch.setattr(config_module, "ENV_FILE", env_file)
    monkeypatch.setattr(settings, "db_path", db_file)
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setattr(config_module, "ENV_MANAGED_KEYS", frozenset())

    settings.save_setting("TMDB_API_KEY", "secret_val")
    init_db()

    if os.name == "posix":
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(db_file.stat().st_mode) == 0o600


def test_shared_user_login_does_not_persist_plex_token(monkeypatch):
    """OWASP A02: Non-admin OAuth tokens are only checked at login and must not be stored."""
    from plex_recommender.web import app as webapp
    from plex_recommender.db.users import get_user

    create_or_update_user({
        "user_key": "admin_owner",
        "username": "owner",
        "email": "owner@example.com",
        "is_admin": True,
        "plex_token": "owner-tok",
    })
    monkeypatch.setattr(webapp.settings, "plex_machine_id", "MID-1")
    monkeypatch.setattr(webapp, "check_plex_pin", lambda pin_id: "shared-secret-tok")
    monkeypatch.setattr(webapp, "get_plex_account", lambda tok: {
        "user_key": "shared_99",
        "plex_uuid": "uuid-99",
        "username": "friend99",
        "title": "Friend 99",
        "email": "friend99@example.com",
        "thumb": None,
    })
    monkeypatch.setattr(webapp, "check_user_access", lambda **kw: True)

    monkeypatch.setattr(webapp, "create_plex_pin", lambda: {"id": 55, "code": "ABCD", "auth_url": "https://app.plex.tv/auth", "expires_in": 900})
    with TestClient(app) as client:
        assert client.post("/api/auth/start").status_code == 200
        resp = client.post("/api/auth/poll", data={"pin_id": "55"})
        assert resp.status_code == 200
        assert resp.json()["authorized"] is True

    stored = get_user("shared_99")
    assert stored is not None
    assert stored["is_admin"] == 0
    assert stored["plex_token"] is None


def test_recommendation_engagement_endpoints_validate_inputs():
    """OWASP A03: Reject malformed tmdb_id/media_type/reason and strip log-injection newlines from title."""
    from plex_recommender.db.engagement import get_user_vote_items

    with TestClient(app) as client:
        uk = _seed_shared_user(client)

        # Injection-like tmdb_id rejected with 400 across all 4 endpoints
        bad_id = "1');alert(1)//"
        assert client.post("/api/recommendations/vote", data={"tmdb_id": bad_id, "media_type": "movie", "vote": 1}).status_code == 400
        assert client.post("/api/recommendations/unvote", data={"tmdb_id": bad_id}).status_code == 400
        assert client.post("/api/recommendations/dismiss", data={"tmdb_id": bad_id, "media_type": "movie"}).status_code == 400
        assert client.post("/api/recommendations/undismiss", data={"tmdb_id": bad_id}).status_code == 400

        # Invalid media_type and reason rejected
        assert client.post("/api/recommendations/vote", data={"tmdb_id": "550", "media_type": "evil<script>", "vote": 1}).status_code == 400
        assert client.post("/api/recommendations/dismiss", data={"tmdb_id": "550", "media_type": "movie", "reason": "xss"}).status_code == 400

        # Newlines stripped from title
        r_ok = client.post(
            "/api/recommendations/vote",
            data={"tmdb_id": "550", "media_type": "movie", "title": "Fight\r\nClub", "vote": 1},
        )
        assert r_ok.status_code == 200
        items = get_user_vote_items(uk)
        assert items[0]["title"] == "Fight  Club"


def test_templates_escape_dynamic_innerhtml_sinks():
    """OWASP A03: Verify template innerHTML sinks use escapeHtml or safe DOM APIs."""
    from pathlib import Path

    tmpl_dir = Path("plex_recommender/web/templates")
    layout_src = (tmpl_dir / "layout.html").read_text()
    recs_src = (tmpl_dir / "recommendations.html").read_text()
    sources_src = (tmpl_dir / "sources.html").read_text()
    settings_src = (tmpl_dir / "settings.html").read_text()

    assert "window.escapeHtml = escapeHtml;" in layout_src
    assert "escapeHtml(message || 'Working...')" in layout_src
    assert "onclick=\"unvoteRecommendation('${item.tmdb_id}')\"" not in recs_src
    assert "onclick=\"undismissRecommendation('${item.tmdb_id}', this)\"" not in recs_src
    assert "escapeHtml(item.title" in recs_src
    assert "escapeHtml(g)" in sources_src
    assert "modal.replaceChildren(msgDiv)" in settings_src


def test_auth_poll_rejects_unbound_or_mismatched_pin_and_rate_limits(monkeypatch):
    """OWASP A04/A07: /api/auth/poll requires matching session-bound pending_pin_id and /api/auth/start is rate-limited."""
    from plex_recommender.web import app as webapp

    monkeypatch.setattr(webapp, "create_plex_pin", lambda: {"id": 777, "code": "XYZ", "auth_url": "https://app.plex.tv/auth", "expires_in": 900})
    monkeypatch.setattr(webapp, "check_plex_pin", lambda pin_id: "tok")

    with TestClient(app) as client:
        # Polling without calling /api/auth/start first is rejected
        r_missing = client.post("/api/auth/poll", data={"pin_id": "777"})
        assert r_missing.status_code == 403
        assert r_missing.json()["error"] == "invalid_pin_session"

        # Start auth binds pin_id=777 to session; polling pin_id=999 is rejected
        assert client.post("/api/auth/start").status_code == 200
        r_mismatch = client.post("/api/auth/poll", data={"pin_id": "999"})
        assert r_mismatch.status_code == 403

        # Remaining 14 /api/auth/start calls succeed, 16th returns 429
        for _ in range(14):
            assert client.post("/api/auth/start").status_code == 200
        r_limited = client.post("/api/auth/start")
        assert r_limited.status_code == 429
        assert r_limited.json()["error"] == "rate_limited"


def test_security_headers_and_openapi_disabled_and_errors_sanitized(monkeypatch):
    """OWASP A05: Baseline security headers present, /docs & /openapi.json disabled, internal errors not leaked."""
    from plex_recommender.web import app as webapp

    with TestClient(app) as client:
        resp = client.get("/login")
        assert resp.status_code == 200
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert resp.headers.get("Permissions-Policy") == "camera=(), microphone=(), geolocation=()"
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp

        # OpenAPI / Swagger endpoints disabled by default
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404

        # /api/auth/start does not leak raw exception strings
        monkeypatch.setattr(webapp, "create_plex_pin", lambda: (_ for _ in ()).throw(RuntimeError("internal-secret-host:32400 failed")))
        r_err = client.post("/api/auth/start")
        assert r_err.status_code == 500
        assert "internal-secret-host" not in r_err.text


def test_pyproject_and_requirements_sync_runtime_dependencies():
    """OWASP A06: Keep runtime requirements.txt aligned with pyproject.toml without test-only packages."""
    import re
    from pathlib import Path

    req_lines = [
        line.strip()
        for line in Path("requirements.txt").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    pyproject_text = Path("pyproject.toml").read_text()

    req_names = {re.split(r"[<>=\[]", line)[0].lower() for line in req_lines}
    assert "pytest" not in req_names
    assert "httpx" not in req_names
    assert "faker" in req_names

    for line in req_lines:
        assert f'"{line}"' in pyproject_text, f"{line!r} missing or out of sync in pyproject.toml"
