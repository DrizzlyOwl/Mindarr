import pytest
from unittest.mock import MagicMock
from plex_recommender.config import settings
from plex_recommender.db import (
    init_db,
    upsert_user_media,
    create_or_update_user,
    set_setting,
    get_setting,
    get_cached_recommendations,
    set_cached_recommendations,
    clear_recommendations_cache
)
from plex_recommender.recommender import ContentRecommender

USER = "u1"


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_cache.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    create_or_update_user({"user_key": USER, "username": "alice", "email": "a@x.com", "is_admin": True})
    yield

def test_cache_crud_and_sync_version():
    cache_key = "movie:Sci-Fi:en:7.0:10"
    sync_v1 = "2026-09-07T12:00:00Z"
    sync_v2 = "2026-09-07T13:00:00Z"

    # Initially empty
    assert get_cached_recommendations(cache_key, sync_v1) is None

    # Set cache with sync_v1
    data = {"success": True, "recommendations": [{"title": "Cached Sci-Fi Movie"}]}
    set_cached_recommendations(cache_key, sync_v1, data)

    # Retrieval with same sync version hits cache
    cached = get_cached_recommendations(cache_key, sync_v1)
    assert cached is not None
    assert cached["from_cache"] is True
    assert cached["sync_version"] == sync_v1
    assert cached["recommendations"][0]["title"] == "Cached Sci-Fi Movie"

    # Retrieval with updated sync version misses cache (stale)
    assert get_cached_recommendations(cache_key, sync_v2) is None

    # Clear cache explicitly
    clear_recommendations_cache()
    assert get_cached_recommendations(cache_key, sync_v1) is None

def test_recommender_uses_cache_and_force_refresh():
    set_setting("last_sync_time", "2026-09-07T14:00:00Z")
    upsert_user_media(USER, {
        "item_id": "movie_1",
        "media_type": "movie",
        "title": "Existing Movie",
        "year": 2020,
        "genres": ["Sci-Fi"]
    })

    mock_tmdb = MagicMock()
    mock_tmdb.map_genres.return_value = [878]
    mock_tmdb.discover_movies.return_value = [
        {"id": 999, "title": "New Unseen Movie", "release_date": "2024-01-01", "vote_average": 7.8, "vote_count": 500, "genre_ids": [878], "poster_path": None, "overview": "..."}
    ]
    mock_tmdb.discover_tv.return_value = []
    mock_tmdb.format_item.return_value = {
        "tmdb_id": "999",
        "media_type": "movie",
        "title": "New Unseen Movie",
        "year": 2024,
        "rating": 7.8,
        "vote_count": 500,
        "summary": "...",
        "poster_url": None,
        "genres": ["Sci-Fi"]
    }
    mock_tmdb.get_external_ids.return_value = {"imdb_id": "tt9999999", "tvdb_id": None}

    engine = ContentRecommender(tmdb_client=mock_tmdb)

    # First call: cache miss, calls TMDb
    res1 = engine.get_recommendations(user_key=USER, media_type="movie", min_rating=7.0, limit=5)
    assert res1["success"] is True
    assert res1["from_cache"] is False
    assert mock_tmdb.discover_movies.call_count > 0

    call_count_after_first = mock_tmdb.discover_movies.call_count

    # Second call: cache hit, TMDb is NOT called again
    res2 = engine.get_recommendations(user_key=USER, media_type="movie", min_rating=7.0, limit=5)
    assert res2["success"] is True
    assert res2["from_cache"] is True
    assert mock_tmdb.discover_movies.call_count == call_count_after_first

    # Third call with force_refresh=True: bypasses cache and calls TMDb
    res3 = engine.get_recommendations(user_key=USER, media_type="movie", min_rating=7.0, limit=5, force_refresh=True)
    assert res3["success"] is True
    assert res3["from_cache"] is False
    assert mock_tmdb.discover_movies.call_count > call_count_after_first

    # Fourth call after Plex sync updates last_sync_time: cache is stale, calls TMDb again
    set_setting("last_sync_time", "2026-09-07T15:00:00Z")
    clear_recommendations_cache()  # As done in sync_plex_data()
    res4 = engine.get_recommendations(user_key=USER, media_type="movie", min_rating=7.0, limit=5)
    assert res4["success"] is True
    assert res4["from_cache"] is False
