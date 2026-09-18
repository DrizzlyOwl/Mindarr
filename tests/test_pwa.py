import json

from starlette.testclient import TestClient

from plex_recommender import __version__
from plex_recommender.config import settings
from plex_recommender.db import init_db
from plex_recommender.web.app import app


def setup_module(module):
    init_db()


def test_manifest_json_served_with_correct_content_type():
    with TestClient(app) as client:
        resp = client.get("/manifest.json")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/manifest+json")
        data = json.loads(resp.text)
        assert data["name"] == "Mindarr"
        assert data["display"] == "standalone"
        assert data["start_url"] == "/"
        assert any(icon["sizes"] == "192x192" for icon in data["icons"])
        assert any(icon["sizes"] == "512x512" for icon in data["icons"])
        assert any(icon.get("purpose") == "maskable" for icon in data["icons"])


def test_manifest_webmanifest_alias():
    with TestClient(app) as client:
        resp = client.get("/manifest.webmanifest")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/manifest+json")


def test_service_worker_served_with_root_scope():
    with TestClient(app) as client:
        resp = client.get("/sw.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["content-type"]
        assert resp.headers["service-worker-allowed"] == "/"
        # Network-first strategy: the fetch handler must try the network before cache.
        assert "networkFirst" in resp.text
        assert "fetch(request)" in resp.text


def test_offline_page_renders():
    with TestClient(app) as client:
        resp = client.get("/offline")
        assert resp.status_code == 200
        assert "offline" in resp.text.lower()


def test_pwa_icons_are_served():
    with TestClient(app) as client:
        for name in (
            "icon.svg",
            "icon-192.png",
            "icon-512.png",
            "icon-512-maskable.png",
            "apple-touch-icon.png",
            "favicon-32.png",
            "favicon-16.png",
        ):
            resp = client.get(f"/static/icons/{name}")
            assert resp.status_code == 200, name


def test_pwa_routes_are_not_blocked_by_onboarding_gate():
    with TestClient(app) as client:
        for path in ("/manifest.json", "/manifest.webmanifest", "/sw.js", "/offline"):
            resp = client.get(path, follow_redirects=False)
            assert resp.status_code == 200, path


def test_login_page_includes_pwa_meta_tags():
    with TestClient(app) as client:
        resp = client.get("/login")
        assert resp.status_code == 200
        assert '<link rel="manifest" href="/manifest.json">' in resp.text
        assert 'name="theme-color" content="#0b0f19"' in resp.text
        assert "navigator.serviceWorker.register" in resp.text
