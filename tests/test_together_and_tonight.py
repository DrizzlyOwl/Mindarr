import base64
import json
import itsdangerous
import pytest
from fastapi.testclient import TestClient

from plex_recommender.config import settings
from plex_recommender.db import init_db
from plex_recommender.db.users import create_or_update_user, mark_onboarded
from plex_recommender.db.watch import upsert_user_media
from plex_recommender.db.media import upsert_media_item
from plex_recommender.db.engagement import add_to_watchlist
from plex_recommender.db.recommendations import set_setting
from plex_recommender.recommender import ContentRecommender, blend_taste_profiles
from plex_recommender.discovery.tmdb import TMDbClient
from plex_recommender.web.app import app

U1 = "user_alpha"
U2 = "user_beta"


@pytest.fixture(autouse=True)
def setup_together_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_together.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    yield


def _login(client: TestClient, user_key: str, *, is_admin: bool = False, username: str = "alpha", title: str = "Alpha"):
    create_or_update_user({
        "user_key": user_key,
        "username": username,
        "email": f"{username}@example.com",
        "title": title,
        "is_admin": is_admin,
    })
    mark_onboarded(user_key)
    signer = itsdangerous.TimestampSigner(settings.session_secret)
    cookie = signer.sign(base64.b64encode(json.dumps({"user_key": user_key}).encode())).decode()
    client.cookies.set("session", cookie)


def test_blend_taste_profiles_prioritizes_shared_genres():
    p1 = {
        "has_data": True,
        "top_genres": [
            {"genre": "Horror", "score": 100.0, "count": 10},
            {"genre": "Sci-Fi", "score": 80.0, "count": 8},
        ],
        "top_keywords": [{"keyword": "time loop", "id": 10, "score": 90.0, "count": 3}],
        "decades": [{"decade": "2010s", "count": 5}],
    }
    p2 = {
        "has_data": True,
        "top_genres": [
            {"genre": "Romance", "score": 100.0, "count": 10},
            {"genre": "Sci-Fi", "score": 80.0, "count": 8},
        ],
        "top_keywords": [{"keyword": "time loop", "id": 10, "score": 70.0, "count": 2}],
        "decades": [{"decade": "2010s", "count": 4}],
    }
    blended = blend_taste_profiles([p1, p2])
    assert blended["has_data"] is True
    assert blended["group_size"] == 2
    assert "Sci-Fi" in blended["shared_genres"]
    # Sci-Fi is shared by both (80 * 1.0 = 80) > single-user Horror/Romance (50 * 0.75 = 37.5)
    assert blended["top_genres"][0]["genre"] == "Sci-Fi"
    assert blended["top_genres"][0]["shared_by_all"] is True


def test_group_recommendations_excludes_union_of_both_users_seen(monkeypatch):
    create_or_update_user({"user_key": U1, "username": "u1", "email": "u1@x.com", "title": "U1", "is_admin": True})
    create_or_update_user({"user_key": U2, "username": "u2", "email": "u2@x.com", "title": "U2", "is_admin": False})

    # U1 saw Movie A (tmdb 101); U2 saw Movie B (tmdb 202); neither saw Movie C (tmdb 303)
    upsert_user_media(U1, {
        "item_id": "m1", "media_type": "movie", "title": "Movie A", "year": 2020,
        "genres": ["Sci-Fi"], "tmdb_id": "101", "view_count": 1, "last_viewed_at": "2026-01-01T00:00:00",
    })
    upsert_user_media(U2, {
        "item_id": "m2", "media_type": "movie", "title": "Movie B", "year": 2021,
        "genres": ["Sci-Fi"], "tmdb_id": "202", "view_count": 1, "last_viewed_at": "2026-01-01T00:00:00",
    })

    fake_tmdb = TMDbClient(api_key="test_key")
    monkeypatch.setattr(fake_tmdb, "discover_movies", lambda **kw: [
        {"id": 101, "title": "Movie A", "release_date": "2020-01-01", "vote_average": 8.5, "vote_count": 1000, "genre_ids": [878], "original_language": "en"},
        {"id": 202, "title": "Movie B", "release_date": "2021-01-01", "vote_average": 8.4, "vote_count": 1000, "genre_ids": [878], "original_language": "en"},
        {"id": 303, "title": "Movie C", "release_date": "2022-01-01", "vote_average": 8.1, "vote_count": 900, "genre_ids": [878], "original_language": "en"},
    ])
    monkeypatch.setattr(fake_tmdb, "get_similar", lambda *a, **k: [])
    monkeypatch.setattr(fake_tmdb, "get_tmdb_recommendations", lambda *a, **k: [])
    monkeypatch.setattr(fake_tmdb, "batch_get_external_ids", lambda items: {
        str(tid): {"imdb_id": f"tt{tid}", "tvdb_id": None} for tid, _ in items
    })

    rec = ContentRecommender(tmdb_client=fake_tmdb)
    res = rec.get_group_recommendations([U1, U2], media_type="movie", force_refresh=True)
    assert res["success"] is True
    assert res["group_size"] == 2
    returned_ids = [r["tmdb_id"] for r in res["recommendations"]]
    assert returned_ids == ["303"]
    assert "Unseen by all 2 viewers" in res["recommendations"][0]["rationale"]


def test_tonight_picker_watchlist_and_mood_queries(monkeypatch):
    create_or_update_user({"user_key": U1, "username": "u1", "email": "u1@x.com", "title": "U1", "is_admin": False})
    upsert_user_media(U1, {
        "item_id": "m1", "media_type": "movie", "title": "Seed", "year": 2020,
        "genres": ["Sci-Fi"], "tmdb_id": "101", "view_count": 1, "last_viewed_at": "2026-01-01T00:00:00",
    })
    # Seed Plex library for Movie 500
    upsert_media_item({"item_id": "900", "media_type": "movie", "title": "Short Gem", "year": 2021, "tmdb_id": "500", "genres": ["Sci-Fi"]})
    add_to_watchlist(U1, "500", "movie", "Short Gem", 2021, rating=8.0, genres=["Sci-Fi"])
    add_to_watchlist(U1, "501", "show", "Long Show", 2022, rating=8.2, genres=["Drama"])

    fake_tmdb = TMDbClient(api_key="test_key")
    captured_kwargs = {}

    def fake_discover_movies(**kw):
        captured_kwargs.update(kw)
        return [
            {"id": 700, "title": "Quick Film", "release_date": "2023-01-01", "vote_average": 7.6, "vote_count": 500, "genre_ids": [878], "original_language": "en"},
            {"id": 701, "title": "Second Quick Film", "release_date": "2024-01-01", "vote_average": 7.5, "vote_count": 400, "genre_ids": [878], "original_language": "en"},
        ]

    monkeypatch.setattr(fake_tmdb, "discover_movies", fake_discover_movies)
    monkeypatch.setattr(fake_tmdb, "batch_get_external_ids", lambda items: {
        str(tid): {"imdb_id": f"tt{tid}", "tvdb_id": None} for tid, _ in items
    })

    rec = ContentRecommender(tmdb_client=fake_tmdb)

    # Watchlist source with only_available=True returns only Short Gem (on Plex)
    wl_res = rec.get_tonight_picks(U1, source="watchlist", only_available=True)
    assert wl_res["success"] is True
    assert [p["tmdb_id"] for p in wl_res["picks"]] == ["500"]

    # Quick Watch mood sets with_runtime_lte=105 and excludes already-shown IDs on re-roll
    qw_res = rec.get_tonight_picks(U1, source="discover", mood="quick_watch", exclude_ids={"700"})
    assert qw_res["success"] is True
    assert captured_kwargs.get("with_runtime_lte") == 105
    assert [p["tmdb_id"] for p in qw_res["picks"]] == ["701"]


def test_together_peers_shows_real_names_and_watch_code_resolution():
    create_or_update_user({"user_key": U2, "username": "secret_bob", "email": "bob@x.com", "title": "Bob Secret", "is_admin": False})
    upsert_user_media(U2, {
        "item_id": "m2", "media_type": "movie", "title": "Movie B", "year": 2021,
        "genres": ["Drama"], "tmdb_id": "202", "view_count": 1, "last_viewed_at": "2026-01-01T00:00:00",
    })

    with TestClient(app) as client:
        # Peers always show real Plex display names
        _login(client, U1, is_admin=False, username="alice", title="Alice")
        upsert_user_media(U1, {
            "item_id": "m1", "media_type": "movie", "title": "Movie A", "year": 2020,
            "genres": ["Drama"], "tmdb_id": "101", "view_count": 1, "last_viewed_at": "2026-01-01T00:00:00",
        })

        r_peers = client.get("/api/together/peers")
        assert r_peers.status_code == 200
        d = r_peers.json()
        assert d["my_watch_code"].startswith("MIND-")
        assert len(d["peers"]) == 1
        assert d["peers"][0]["is_anonymized"] is False
        assert d["peers"][0]["display_name"] == "Bob Secret"

        # Self code rejected; invalid code returns 404
        r_self = client.post("/api/together/resolve-code", data={"code": d["my_watch_code"]})
        assert r_self.status_code == 400
        r_bad = client.post("/api/together/resolve-code", data={"code": "MIND-0000-0000"})
        assert r_bad.status_code == 404

    # U2 gets their own code; U1 resolves it
    with TestClient(app) as c2:
        _login(c2, U2, is_admin=False, username="secret_bob", title="Bob Secret")
        u2_code = c2.get("/api/together/peers").json()["my_watch_code"]

    with TestClient(app) as c1:
        _login(c1, U1, is_admin=False, username="alice", title="Alice")
        r_ok = c1.post("/api/together/resolve-code", data={"code": u2_code})
        assert r_ok.status_code == 200
        assert r_ok.json()["peer"]["user_key"] == U2
        assert r_ok.json()["peer"]["display_name"] == "Bob Secret"


def test_api_together_and_tonight_endpoints(monkeypatch):
    from plex_recommender.web import app as webapp
    create_or_update_user({"user_key": U2, "username": "bob", "email": "bob@x.com", "title": "Bob", "is_admin": False})
    upsert_user_media(U2, {
        "item_id": "m2", "media_type": "movie", "title": "Movie B", "year": 2021,
        "genres": ["Sci-Fi"], "tmdb_id": "202", "view_count": 1, "last_viewed_at": "2026-01-01T00:00:00",
    })

    monkeypatch.setattr(webapp.tmdb, "is_configured", lambda: True)
    monkeypatch.setattr(webapp.recommender, "get_group_recommendations", lambda **kw: {
        "success": True,
        "is_complete": True,
        "group_size": len(kw["user_keys"]),
        "shared_genres": ["Sci-Fi"],
        "recommendations": [
            {
                "tmdb_id": "888",
                "media_type": "movie",
                "title": "Shared Pick",
                "year": 2023,
                "rating": 8.1,
                "vote_count": 500,
                "genres": ["Sci-Fi"],
                "summary": "Great group movie.",
                "match_score": 92,
                "rationale": "Unseen by all 2 viewers • Matches top genre: Sci-Fi",
                "poster_url": None,
                "original_language": "en",
                "language_name": "English",
                "available_on_plex": False,
                "overseerr_requested": False,
            }
        ],
        "page": 1,
        "total_pages": 1,
        "unseen_count": 1,
        "from_cache": False,
    })
    monkeypatch.setattr(webapp.recommender, "get_tonight_picks", lambda **kw: {
        "success": True,
        "picks": [
            {
                "tmdb_id": "777",
                "media_type": "movie",
                "title": "Tonight Finalist",
                "year": 2024,
                "rating": 8.0,
                "genres": ["Sci-Fi"],
                "summary": "Pick me.",
                "poster_url": None,
                "available_on_plex": True,
                "plex_url": "https://app.plex.tv/mock",
                "overseerr_requested": False,
                "match_score": 90,
                "rationale": "Best Match",
            }
        ],
        "total_pool": 1,
        "mood": kw["mood"],
        "mood_label": "Best Match",
        "source": kw["source"],
    })

    with TestClient(app) as client:
        _login(client, U1, is_admin=True, username="alice", title="Alice")
        upsert_user_media(U1, {
            "item_id": "m1", "media_type": "movie", "title": "Movie A", "year": 2020,
            "genres": ["Sci-Fi"], "tmdb_id": "101", "view_count": 1, "last_viewed_at": "2026-01-01T00:00:00",
        })

        # Without peers -> needs_peers=True
        r_empty = client.get("/api/recommendations/together")
        assert r_empty.status_code == 200
        assert r_empty.json()["needs_peers"] is True

        # With peer -> renders blended card HTML
        r_tog = client.get(f"/api/recommendations/together?peers={U2}")
        assert r_tog.status_code == 200
        d_tog = r_tog.json()
        assert d_tog["has_results"] is True
        assert d_tog["group_size"] == 2
        assert "Shared Pick" in d_tog["html"]

        # Tonight Picker validation & response
        assert client.get("/api/recommendations/tonight?mood=invalid_mood").status_code == 400
        assert client.get("/api/recommendations/tonight?source=together").status_code == 400
        r_ton = client.get(f"/api/recommendations/tonight?source=together&peers={U2}&mood=best")
        assert r_ton.status_code == 200
        assert r_ton.json()["picks"][0]["title"] == "Tonight Finalist"

        # Page shell renders Watch Together tab & Tonight Picker modal
        r_page = client.get("/recommendations")
        assert r_page.status_code == 200
        assert 'id="tab-view-together"' in r_page.text
        assert 'id="tonight-modal"' in r_page.text
        assert 'id="together-panel"' in r_page.text
