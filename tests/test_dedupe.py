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


def test_genre_filter_strictly_excludes_unmatched_genres_and_unmatched_seeds():
    # User watched a Drama show
    upsert_user_media(USER, {
        "item_id": "drama_1",
        "media_type": "show",
        "title": "Serious Drama",
        "year": 2018,
        "genres": ["Drama"],
        "tmdb_id": "1111",
        "user_rating": 9.0
    })

    mock_tmdb = MagicMock()
    mock_tmdb.map_genres.return_value = [35]  # Comedy TMDb ID
    mock_tmdb.discover_movies.return_value = [
        {"id": 10, "title": "Super Funny Movie", "release_date": "2021-01-01", "vote_average": 7.5, "vote_count": 500, "genre_ids": [35], "poster_path": None, "overview": "..."},
        {"id": 20, "title": "Serious Drama Movie", "release_date": "2021-01-01", "vote_average": 8.0, "vote_count": 500, "genre_ids": [18], "poster_path": None, "overview": "..."}
    ]
    mock_tmdb.discover_tv.return_value = []
    # Seed recommendations for Serious Drama return another drama
    mock_tmdb.get_tmdb_recommendations.return_value = [
        {"id": 30, "title": "Another Drama Seed", "release_date": "2021-01-01", "vote_average": 8.5, "vote_count": 500, "genre_ids": [18], "poster_path": None, "overview": "..."}
    ]
    mock_tmdb.get_similar.return_value = []

    def mock_format_item(raw, media_type):
        gid = raw.get("genre_ids", [35])[0]
        gname = "Comedy" if gid == 35 else "Drama"
        return {
            "tmdb_id": str(raw["id"]),
            "media_type": media_type,
            "title": raw["title"],
            "year": 2021,
            "rating": raw["vote_average"],
            "vote_count": raw["vote_count"],
            "summary": raw["overview"],
            "poster_url": None,
            "genres": [gname],
            "genre_ids": [gid],
            "original_language": "en",
            "language_name": "English"
        }
    mock_tmdb.format_item.side_effect = mock_format_item
    mock_tmdb.get_external_ids.return_value = {"imdb_id": None, "tvdb_id": None}

    rec_engine = ContentRecommender(tmdb_client=mock_tmdb)
    result = rec_engine.get_recommendations(user_key=USER, media_type="movie", genre_filter="Comedy")

    assert result["success"] is True
    recs = result["recommendations"]
    assert len(recs) == 1
    assert recs[0]["title"] == "Super Funny Movie"
    assert recs[0]["genres"] == ["Comedy"]


def test_progressive_stages_and_instant_pagination():
    upsert_user_media(USER, {
        "item_id": "seed_1",
        "media_type": "movie",
        "title": "Watched SciFi",
        "year": 2020,
        "genres": ["Sci-Fi"],
        "tmdb_id": "900",
        "user_rating": 8.0
    })

    mock_tmdb = MagicMock()
    mock_tmdb.map_genres.return_value = [878]
    # Return 25 items to test pagination across 2 pages
    items = []
    for i in range(1, 26):
        items.append({
            "id": i + 1000,
            "title": f"SciFi Movie {i}",
            "release_date": "2022-01-01",
            "vote_average": 7.5,
            "vote_count": 300,
            "genre_ids": [878],
            "poster_path": None,
            "overview": "..."
        })
    mock_tmdb.discover_movies.return_value = items
    mock_tmdb.discover_tv.return_value = []
    mock_tmdb.get_tmdb_recommendations.return_value = []
    mock_tmdb.get_similar.return_value = []

    def mock_format_item(raw, media_type):
        return {
            "tmdb_id": str(raw["id"]),
            "media_type": media_type,
            "title": raw["title"],
            "year": 2022,
            "rating": raw["vote_average"],
            "vote_count": raw["vote_count"],
            "summary": raw["overview"],
            "poster_url": None,
            "genres": ["Sci-Fi"],
            "genre_ids": [878],
            "original_language": "en",
            "language_name": "English"
        }
    mock_tmdb.format_item.side_effect = mock_format_item
    mock_tmdb.get_external_ids.return_value = {"imdb_id": None, "tvdb_id": None}

    rec_engine = ContentRecommender(tmdb_client=mock_tmdb)

    # Stage 'fast': returns top 6 items, marked not complete
    res_fast = rec_engine.get_recommendations(user_key=USER, media_type="movie", limit=12, page=1, stage="fast")
    assert res_fast["success"] is True
    assert res_fast["is_complete"] is False
    assert len(res_fast["recommendations"]) == 6

    # Stage 'full': completes discovery, caches candidate pool, returns page 1 (12 items)
    res_full = rec_engine.get_recommendations(user_key=USER, media_type="movie", limit=12, page=1, stage="full")
    assert res_full["success"] is True
    assert res_full["is_complete"] is True
    assert len(res_full["recommendations"]) == 12
    initial_discover_calls = mock_tmdb.discover_movies.call_count

    # Pagination: page 2 hits cached pool without querying TMDb
    res_page2 = rec_engine.get_recommendations(user_key=USER, media_type="movie", limit=12, page=2, stage="full")
    assert res_page2["success"] is True
    assert res_page2["from_cache"] is True
    assert res_page2["page"] == 2
    assert len(res_page2["recommendations"]) == 12
    assert mock_tmdb.discover_movies.call_count == initial_discover_calls


def test_genre_alignment_and_prioritization_on_candidate():
    # User watched "Science Fiction" in Plex
    upsert_user_media(USER, {
        "item_id": "seed_sf",
        "media_type": "movie",
        "title": "Watched SciFi Movie",
        "year": 2021,
        "genres": ["Science Fiction"],
        "tmdb_id": "5001",
        "user_rating": 8.5
    })

    mock_tmdb = MagicMock()
    mock_tmdb.map_genres.return_value = [878]
    # TMDb returns a candidate with genres ["Drama", "Sci-Fi"]
    mock_tmdb.discover_movies.return_value = [
        {
            "id": 9001,
            "title": "Deep Space Drama",
            "release_date": "2023-01-01",
            "vote_average": 7.9,
            "vote_count": 800,
            "genre_ids": [18, 878],
            "poster_path": None,
            "overview": "..."
        }
    ]
    mock_tmdb.discover_tv.return_value = []
    mock_tmdb.get_tmdb_recommendations.return_value = []
    mock_tmdb.get_similar.return_value = []

    def mock_format_item(raw, media_type):
        return {
            "tmdb_id": str(raw["id"]),
            "media_type": media_type,
            "title": raw["title"],
            "year": 2023,
            "rating": raw["vote_average"],
            "vote_count": raw["vote_count"],
            "summary": raw["overview"],
            "poster_url": None,
            "genres": ["Drama", "Sci-Fi"],
            "genre_ids": [18, 878],
            "original_language": "en",
            "language_name": "English"
        }
    mock_tmdb.format_item.side_effect = mock_format_item
    mock_tmdb.get_external_ids.return_value = {"imdb_id": None, "tvdb_id": None}

    rec_engine = ContentRecommender(tmdb_client=mock_tmdb)

    # Filter by Plex genre name "Science Fiction"
    res = rec_engine.get_recommendations(user_key=USER, media_type="movie", genre_filter="Science Fiction", force_refresh=True)
    assert res["success"] is True
    assert len(res["recommendations"]) == 1
    cand = res["recommendations"][0]

    # Matched category Sci-Fi must be prioritized to the front (index 0) of genres
    assert cand["genres"][0] == "Sci-Fi"
    assert cand["matched_category"] == "Science Fiction"

    # Rationale must state that it matches category Sci-Fi
    assert "Matches category Sci-Fi" in cand["rationale"]

    # Decision factors matched_genres must successfully match "Science Fiction" affinity
    matched_genres = cand["decision_factors"]["matched_genres"]
    assert len(matched_genres) >= 1
    assert any(mg["name"] == "Sci-Fi" for mg in matched_genres)


def test_romance_filter_strictly_excludes_crime_drama():
    # User watched a Romance movie
    upsert_user_media(USER, {
        "item_id": "seed_rom",
        "media_type": "movie",
        "title": "Before Sunrise",
        "year": 1995,
        "genres": ["Romance", "Drama"],
        "tmdb_id": "76",
        "user_rating": 9.0
    })

    mock_tmdb = MagicMock()
    mock_tmdb.map_genres.return_value = [10749]
    mock_tmdb.discover_movies.return_value = [
        {
            "id": 100,
            "title": "Pride and Prejudice",
            "release_date": "2005-01-01",
            "vote_average": 8.1,
            "vote_count": 5000,
            "genre_ids": [10749, 18],  # Romance, Drama
            "poster_path": None,
            "overview": "..."
        },
        {
            "id": 200,
            "title": "The Sopranos Clone",
            "release_date": "2002-01-01",
            "vote_average": 9.2,
            "vote_count": 10000,
            "genre_ids": [80, 18],  # Crime, Drama (NO Romance!)
            "poster_path": None,
            "overview": "..."
        }
    ]
    mock_tmdb.discover_tv.return_value = []
    mock_tmdb.get_tmdb_recommendations.return_value = []
    mock_tmdb.get_similar.return_value = []

    def mock_format_item(raw, media_type):
        g_ids = raw.get("genre_ids", [])
        g_names = []
        if 10749 in g_ids:
            g_names.append("Romance")
        if 80 in g_ids:
            g_names.append("Crime")
        if 18 in g_ids:
            g_names.append("Drama")
        return {
            "tmdb_id": str(raw["id"]),
            "media_type": media_type,
            "title": raw["title"],
            "year": 2005,
            "rating": raw["vote_average"],
            "vote_count": raw["vote_count"],
            "summary": raw["overview"],
            "poster_url": None,
            "genres": g_names,
            "genre_ids": g_ids,
            "original_language": "en",
            "language_name": "English"
        }
    mock_tmdb.format_item.side_effect = mock_format_item
    mock_tmdb.get_external_ids.return_value = {"imdb_id": None, "tvdb_id": None}

    rec_engine = ContentRecommender(tmdb_client=mock_tmdb)
    res = rec_engine.get_recommendations(user_key=USER, media_type="movie", genre_filter="Romance", force_refresh=True)

    assert res["success"] is True
    rec_titles = [r["title"] for r in res["recommendations"]]
    assert "Pride and Prejudice" in rec_titles
    assert "The Sopranos Clone" not in rec_titles


