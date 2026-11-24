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
    test_db = tmp_path / "test_security.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    yield


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
