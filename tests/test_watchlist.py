import base64
import json
import itsdangerous
import pytest
from fastapi.testclient import TestClient

from plex_recommender.config import settings
from plex_recommender.db import init_db
from plex_recommender.db.users import create_or_update_user, mark_onboarded, clear_user_data
from plex_recommender.db.media import upsert_media_item
from plex_recommender.db.watch import upsert_user_media
from plex_recommender.db.engagement import (
    add_to_watchlist,
    remove_from_watchlist,
    is_in_watchlist,
    get_user_watchlist,
    get_user_watchlist_ids,
    get_user_watchlist_count,
    dismiss_item,
)
from plex_recommender.web.app import app

USER = "wl_user"


@pytest.fixture(autouse=True)
def setup_watchlist_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_watchlist.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    yield


def _login(client: TestClient, user_key: str = USER):
    create_or_update_user({
        "user_key": user_key,
        "username": "wl_alice",
        "email": "wl@example.com",
        "title": "Alice",
        "is_admin": False,
    })
    mark_onboarded(user_key)
    signer = itsdangerous.TimestampSigner(settings.session_secret)
    cookie = signer.sign(base64.b64encode(json.dumps({"user_key": user_key}).encode())).decode()
    client.cookies.set("session", cookie)


def test_watchlist_db_crud_and_dismiss_cleanup():
    add_to_watchlist(
        user_key=USER,
        tmdb_id="603",
        media_type="movie",
        title="The Matrix",
        year=1999,
        rating=8.2,
        genres=["Action", "Sci-Fi"],
        summary="Simulation.",
    )
    assert is_in_watchlist(USER, "603") is True
    assert get_user_watchlist_count(USER) == 1
    assert get_user_watchlist_ids(USER) == {"603"}

    items = get_user_watchlist(USER)
    assert len(items) == 1
    assert items[0]["title"] == "The Matrix"
    assert items[0]["genres"] == ["Action", "Sci-Fi"]

    # Marking item as seen automatically removes it from watchlist
    dismiss_item(USER, "603", media_type="movie", title="The Matrix", year=1999, reason="already_watched")
    assert is_in_watchlist(USER, "603") is False
    assert get_user_watchlist_count(USER) == 0

    # Add two items and verify remove + clear_user_data
    add_to_watchlist(USER, "550", "movie", "Fight Club", 1999)
    add_to_watchlist(USER, "1396", "show", "Breaking Bad", 2008)
    assert remove_from_watchlist(USER, "550") is True
    assert get_user_watchlist_count(USER) == 1
    deleted = clear_user_data(USER)
    assert deleted["user_watchlist"] == 1
    assert get_user_watchlist_count(USER) == 0


def test_api_watchlist_toggle_and_list_enrichment(monkeypatch):
    from plex_recommender.web import app as webapp
    monkeypatch.setattr(webapp.settings, "plex_machine_id", "MID-123")
    monkeypatch.setattr(webapp.overseerr, "is_configured", lambda: False)

    # Seed an item on Plex library so watchlist live-checks On Plex availability
    upsert_media_item({
        "item_id": "9001",
        "media_type": "movie",
        "title": "Dune",
        "year": 2021,
        "tmdb_id": "438631",
        "genres": ["Sci-Fi"],
    })

    with TestClient(app) as client:
        _login(client)

        # Toggle ON (with untrusted poster_url stripped cleanly)
        r_add = client.post("/api/watchlist/toggle", data={
            "tmdb_id": "438631",
            "media_type": "movie",
            "title": "Dune",
            "year": "2021",
            "poster_url": "https://evil.example/x.jpg",
            "rating": "7.8",
            "genres": "Sci-Fi,Adventure",
            "summary": "Arrakis.",
        })
        assert r_add.status_code == 200
        d_add = r_add.json()
        assert d_add["in_watchlist"] is True
        assert d_add["watchlist_count"] == 1
        assert get_user_watchlist(USER)[0]["poster_url"] is None

        # Watchlist endpoint returns HTML with On Plex badge & Watch link
        r_list = client.get("/api/watchlist")
        assert r_list.status_code == 200
        d_list = r_list.json()
        assert d_list["has_results"] is True
        assert d_list["watchlist_count"] == 1
        assert "Dune" in d_list["html"]
        assert "On Plex" in d_list["html"]
        assert "Watch on Plex" in d_list["html"]

        # Filtering to TV shows excludes Dune
        r_tv = client.get("/api/watchlist?type=show")
        assert r_tv.status_code == 200
        assert r_tv.json()["has_results"] is False
        assert "Your Watchlist is Empty" in r_tv.json()["html"]

        # Toggle OFF
        r_rem = client.post("/api/watchlist/toggle", data={
            "tmdb_id": "438631",
            "media_type": "movie",
        })
        assert r_rem.status_code == 200
        assert r_rem.json()["in_watchlist"] is False
        assert r_rem.json()["watchlist_count"] == 0


def test_api_media_details_validation_and_enrichment(monkeypatch):
    from plex_recommender.web import app as webapp
    monkeypatch.setattr(webapp.tmdb, "is_configured", lambda: True)
    monkeypatch.setattr(webapp.overseerr, "is_configured", lambda: True)
    monkeypatch.setattr(webapp.overseerr, "get_media_info", lambda tmdb_id, media_type="movie": {
        "exists": True,
        "status_code": 4,
        "status_name": "PARTIALLY_AVAILABLE",
        "seasons": {1: {"status_code": 5, "status_name": "AVAILABLE"}},
    })
    monkeypatch.setattr(webapp.tmdb, "get_details", lambda item_id, media_type="movie": {
        "tmdb_id": str(item_id),
        "media_type": "show",
        "title": "Severance",
        "tagline": "We're all family here.",
        "overview": "Mark leads a team...",
        "year": 2022,
        "rating": 8.4,
        "vote_count": 2000,
        "genres": ["Drama", "Sci-Fi & Fantasy"],
        "poster_url": None,
        "backdrop_url": None,
        "directors": ["Dan Erickson"],
        "cast": [{"name": "Adam Scott", "character": "Mark Scout", "profile_url": None}],
        "trailer": {"key": "xEQP4VVuyrY", "name": "Trailer", "type": "Trailer"},
        "seasons": [{"season_number": 1, "name": "Season 1", "episode_count": 9}],
        "number_of_seasons": 1,
        "number_of_episodes": 9,
        "watch_providers": [],
    })

    with TestClient(app) as client:
        _login(client)
        add_to_watchlist(USER, "95396", "show", "Severance", 2022)

        # Reject non-numeric tmdb_id
        r_bad = client.get("/api/media/details?tmdb_id=abc123&media_type=show")
        assert r_bad.status_code == 400

        r_ok = client.get("/api/media/details?tmdb_id=95396&media_type=show")
        assert r_ok.status_code == 200
        details = r_ok.json()["details"]
        assert details["title"] == "Severance"
        assert details["in_watchlist"] is True
        assert details["trailer"]["key"] == "xEQP4VVuyrY"
        assert details["overseerr_info"]["seasons"]["1"]["status_name"] == "AVAILABLE"


def test_recommendations_page_renders_watchlist_tab_and_csp(monkeypatch):
    with TestClient(app) as client:
        _login(client)
        upsert_user_media(USER, {
            "item_id": "555", "media_type": "movie", "title": "Watched Film",
            "year": 2020, "genres": ["Sci-Fi"], "tmdb_id": "278",
        })
        add_to_watchlist(USER, "603", "movie", "The Matrix", 1999)

        resp = client.get("/recommendations")
        assert resp.status_code == 200
        assert 'id="tab-view-discover"' in resp.text
        assert 'id="tab-view-watchlist"' in resp.text
        assert 'id="media-detail-modal"' in resp.text

        csp = resp.headers.get("Content-Security-Policy", "")
        assert "frame-src https://www.youtube-nocookie.com https://www.youtube.com" in csp
        assert "frame-ancestors 'none'" in csp
