import pytest
from unittest.mock import MagicMock
from plex_recommender.config import settings
from plex_recommender.db import init_db, upsert_user_media, create_or_update_user
from plex_recommender.recommender import ContentRecommender

USER = "u1"


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_dedupe.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    create_or_update_user({"user_key": USER, "username": "alice", "email": "a@x.com", "is_admin": True})
    yield

def test_unseen_filter_strictly_excludes_watched():
    # User has watched Inception and Dark
    upsert_user_media(USER, {
        "item_id": "movie_1",
        "media_type": "movie",
        "title": "Inception",
        "year": 2010,
        "genres": ["Sci-Fi", "Action"],
        "imdb_id": "tt1375666",
        "tmdb_id": "27205"
    })
    upsert_user_media(USER, {
        "item_id": "show_1",
        "media_type": "show",
        "title": "Dark",
        "year": 2017,
        "genres": ["Sci-Fi", "Mystery"],
        "imdb_id": "tt5753856",
        "tmdb_id": "70523"
    })

    # Mock TMDb client returning 3 candidates:
    # 1. Inception (seen via imdb_id & tmdb_id)
    # 2. Dark (seen via tmdb_id & title)
    # 3. Primer (completely unseen!)
    mock_tmdb = MagicMock()
    mock_tmdb.map_genres.return_value = [878]
    mock_tmdb.discover_movies.return_value = [
        {"id": 27205, "title": "Inception", "release_date": "2010-07-16", "vote_average": 8.4, "vote_count": 35000, "genre_ids": [878], "poster_path": "/inception.jpg", "overview": "..."},
        {"id": 8990, "title": "Primer", "release_date": "2004-10-08", "vote_average": 7.0, "vote_count": 2100, "genre_ids": [878], "poster_path": "/primer.jpg", "overview": "..."}
    ]
    mock_tmdb.discover_tv.return_value = []
    
    def mock_format_item(raw, media_type):
        return {
            "tmdb_id": str(raw["id"]),
            "media_type": media_type,
            "title": raw["title"],
            "year": int(raw["release_date"][:4]),
            "rating": raw["vote_average"],
            "vote_count": raw["vote_count"],
            "summary": raw["overview"],
            "poster_url": f"https://image.tmdb.org/t/p/w500{raw['poster_path']}",
            "genres": ["Sci-Fi"]
        }
    mock_tmdb.format_item.side_effect = mock_format_item

    def mock_get_ext(item_id, media_type):
        if item_id == 27205:
            return {"imdb_id": "tt1375666", "tvdb_id": None}
        elif item_id == 8990:
            return {"imdb_id": "tt0390384", "tvdb_id": None}
        return {"imdb_id": None, "tvdb_id": None}
    mock_tmdb.get_external_ids.side_effect = mock_get_ext

    rec_engine = ContentRecommender(tmdb_client=mock_tmdb)
    result = rec_engine.get_recommendations(user_key=USER, media_type="movie", min_rating=6.5)

    assert result["success"] is True
    recs = result["recommendations"]
    
    # Inception must be excluded!
    rec_titles = [r["title"] for r in recs]
    assert "Inception" not in rec_titles
    assert "Primer" in rec_titles
    assert recs[0]["imdb_id"] == "tt0390384"
    assert recs[0]["imdb_url"] == "https://www.imdb.com/title/tt0390384/"
    
    # Verify decision factors for tooltip
    df = recs[0]["decision_factors"]
    assert "matched_genres" in df
    assert "score_points" in df
    assert df["matched_genres"][0]["name"] == "Sci-Fi"

def test_language_filter_application():
    upsert_user_media(USER, {
        "item_id": "movie_test",
        "media_type": "movie",
        "title": "Existing Watched Movie",
        "year": 2019,
        "genres": ["Sci-Fi"]
    })
    mock_tmdb = MagicMock()
    mock_tmdb.map_genres.return_value = [878]
    mock_tmdb.discover_movies.return_value = [
        {"id": 1, "title": "English Film", "release_date": "2020-01-01", "vote_average": 7.5, "vote_count": 500, "genre_ids": [878], "poster_path": None, "overview": "..."},
        {"id": 2, "title": "Japanese Anime", "release_date": "2020-01-01", "vote_average": 8.0, "vote_count": 800, "genre_ids": [878], "poster_path": None, "overview": "..."}
    ]
    mock_tmdb.discover_tv.return_value = []
    
    def mock_format_item(raw, media_type):
        lang = "ja" if "Japanese" in raw["title"] else "en"
        return {
            "tmdb_id": str(raw["id"]),
            "media_type": media_type,
            "title": raw["title"],
            "year": 2020,
            "rating": raw["vote_average"],
            "vote_count": raw["vote_count"],
            "summary": raw["overview"],
            "poster_url": None,
            "genres": ["Sci-Fi"],
            "original_language": lang,
            "language_name": "Japanese" if lang == "ja" else "English"
        }
    mock_tmdb.format_item.side_effect = mock_format_item
    mock_tmdb.get_external_ids.return_value = {"imdb_id": None, "tvdb_id": None}

    rec_engine = ContentRecommender(tmdb_client=mock_tmdb)
    result = rec_engine.get_recommendations(user_key=USER, media_type="movie", language="ja")

    assert result["success"] is True
    recs = result["recommendations"]
    assert len(recs) == 1
    assert recs[0]["title"] == "Japanese Anime"
    assert recs[0]["original_language"] == "ja"

def test_default_language_is_english():
    upsert_user_media(USER, {
        "item_id": "movie_test_lang",
        "media_type": "movie",
        "title": "Existing Watched Movie",
        "year": 2019,
        "genres": ["Sci-Fi"]
    })
    mock_tmdb = MagicMock()
    mock_tmdb.map_genres.return_value = [878]
    mock_tmdb.discover_movies.return_value = [
        {"id": 1, "title": "English Film", "release_date": "2020-01-01", "vote_average": 7.5, "vote_count": 500, "genre_ids": [878], "poster_path": None, "overview": "..."},
        {"id": 2, "title": "Japanese Anime", "release_date": "2020-01-01", "vote_average": 8.0, "vote_count": 800, "genre_ids": [878], "poster_path": None, "overview": "..."}
    ]
    mock_tmdb.discover_tv.return_value = []
    
    def mock_format_item(raw, media_type):
        lang = "ja" if "Japanese" in raw["title"] else "en"
        return {
            "tmdb_id": str(raw["id"]),
            "media_type": media_type,
            "title": raw["title"],
            "year": 2020,
            "rating": raw["vote_average"],
            "vote_count": raw["vote_count"],
            "summary": raw["overview"],
            "poster_url": None,
            "genres": ["Sci-Fi"],
            "original_language": lang,
            "language_name": "Japanese" if lang == "ja" else "English"
        }
    mock_tmdb.format_item.side_effect = mock_format_item
    mock_tmdb.get_external_ids.return_value = {"imdb_id": None, "tvdb_id": None}

    rec_engine = ContentRecommender(tmdb_client=mock_tmdb)
    
    # Without passing language: should default to English
    result_default = rec_engine.get_recommendations(user_key=USER, media_type="movie")
    assert result_default["success"] is True
    recs_default = result_default["recommendations"]
    assert len(recs_default) == 1
    assert recs_default[0]["title"] == "English Film"
    assert recs_default[0]["original_language"] == "en"

    # Passing language="all": both should be returned
    result_all = rec_engine.get_recommendations(user_key=USER, media_type="movie", language="all", force_refresh=True)
    assert result_all["success"] is True
    assert len(result_all["recommendations"]) == 2

