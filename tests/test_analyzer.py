import pytest
from datetime import datetime, timedelta
from plex_recommender.config import settings
from plex_recommender.db import init_db, upsert_user_media, create_or_update_user
from plex_recommender.analyzer import TasteAnalyzer, calculate_time_weight

USER = "u1"


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_analyzer.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    create_or_update_user({"user_key": USER, "username": "alice", "email": "a@x.com", "is_admin": True})
    yield

def test_time_decay_formula():
    now = datetime.now()
    today_weight = calculate_time_weight(now, now)
    six_months_ago = calculate_time_weight(now - timedelta(days=180), now)
    two_years_ago = calculate_time_weight(now - timedelta(days=730), now)

    assert today_weight > six_months_ago
    assert six_months_ago > two_years_ago
    assert today_weight <= 2.0
    assert two_years_ago >= 0.6

def test_taste_analyzer_generates_rankings():
    # Insert test items
    now_iso = datetime.now().isoformat()
    upsert_user_media(USER, {
        "item_id": "movie_1",
        "media_type": "movie",
        "title": "Interstellar",
        "year": 2014,
        "genres": ["Science Fiction", "Drama"],
        "directors": ["Christopher Nolan"],
        "actors": ["Matthew McConaughey", "Anne Hathaway"],
        "view_count": 3,
        "last_viewed_at": now_iso
    })
    upsert_user_media(USER, {
        "item_id": "movie_2",
        "media_type": "movie",
        "title": "Arrival",
        "year": 2016,
        "genres": ["Science Fiction", "Mystery"],
        "directors": ["Denis Villeneuve"],
        "actors": ["Amy Adams"],
        "view_count": 1,
        "last_viewed_at": now_iso
    })
    upsert_user_media(USER, {
        "item_id": "movie_3",
        "media_type": "movie",
        "title": "The Godfather",
        "year": 1972,
        "genres": ["Crime", "Drama"],
        "directors": ["Francis Ford Coppola"],
        "actors": ["Marlon Brando", "Al Pacino"],
        "view_count": 1,
        "last_viewed_at": "2020-01-01T00:00:00"
    })

    analyzer = TasteAnalyzer()
    profile = analyzer.analyze(USER)

    assert profile["has_data"] is True
    assert profile["total_items_analyzed"] == 3
    
    # Science Fiction should be #1 due to recency and multiple titles
    genres = profile["top_genres"]
    assert genres[0]["genre"] == "Science Fiction"
    assert genres[0]["score"] == 100.0

    # Top movies (most-watched ranking)
    top_movies = profile["top_movies"]
    assert top_movies[0]["item_id"] == "movie_1"  # highest view_count
    assert top_movies[0]["view_count"] == 3

    # Directors/actors no longer surfaced in the profile output
    assert "top_directors" not in profile
    assert "top_actors" not in profile

    # Decades
    decades = {d["decade"]: d["count"] for d in profile["decades"]}
    assert decades.get("2010s") == 2
    assert decades.get("1970s") == 1


def test_analyze_surfaces_top_shows():
    now_iso = datetime.now().isoformat()
    upsert_user_media(USER, {
        "item_id": "show_1", "media_type": "show", "title": "Big Binge",
        "year": 2020, "genres": ["Drama"], "view_count": 20, "last_viewed_at": now_iso,
    })
    upsert_user_media(USER, {
        "item_id": "show_2", "media_type": "show", "title": "Casual Watch",
        "year": 2018, "genres": ["Comedy"], "view_count": 3, "last_viewed_at": now_iso,
    })
    profile = TasteAnalyzer().analyze(USER)
    top_shows = profile["top_shows"]
    assert [s["item_id"] for s in top_shows] == ["show_1", "show_2"]


def test_analyze_handles_timezone_aware_dates():
    # Plex/Tautulli sync stores tz-aware ISO timestamps; analyzer must not crash.
    upsert_user_media(USER, {
        "item_id": "tz_1",
        "media_type": "movie",
        "title": "TZ Movie",
        "year": 2023,
        "genres": ["Sci-Fi"],
        "view_count": 1,
        "last_viewed_at": "2024-01-01T12:00:00+00:00",
    })
    profile = TasteAnalyzer().analyze(USER)
    assert profile["has_data"] is True
    assert profile["total_items_analyzed"] == 1


def test_analyze_produces_taste_summary():
    now_iso = datetime.now().isoformat()
    upsert_user_media(USER, {
        "item_id": "m1", "media_type": "movie", "title": "Sci Movie",
        "year": 2021, "genres": ["Science Fiction", "Action"],
        "directors": ["Denis Villeneuve"], "actors": ["Actor A", "Actor B"],
        "view_count": 2, "last_viewed_at": now_iso,
    })
    upsert_user_media(USER, {
        "item_id": "m2", "media_type": "movie", "title": "Another Sci",
        "year": 2019, "genres": ["Science Fiction"],
        "actors": ["Actor A"], "view_count": 1, "last_viewed_at": now_iso,
    })
    profile = TasteAnalyzer().analyze(USER)
    summary = profile["summary"]
    assert summary["headline"]
    assert len(summary["highlights"]) >= 2
    # Each highlight must carry a cited metric
    for h in summary["highlights"]:
        assert h["text"] and h["metric"]
    # Format percentages present and sane
    assert summary["movie_pct"] + summary["show_pct"] == 100
    # Cites the leading genre
    assert any("Science Fiction" in h["metric"] or "Science Fiction" in h["text"]
               for h in summary["highlights"])


def test_keyword_scoring_ranks_shared_themes():
    now_iso = datetime.now().isoformat()
    upsert_user_media(USER, {
        "item_id": "k1", "media_type": "movie", "title": "Loop One",
        "year": 2020, "genres": ["Sci-Fi"], "view_count": 2, "last_viewed_at": now_iso,
        "keywords": [{"id": 1, "name": "time loop"}, {"id": 2, "name": "heist"}],
    })
    upsert_user_media(USER, {
        "item_id": "k2", "media_type": "movie", "title": "Loop Two",
        "year": 2021, "genres": ["Sci-Fi"], "view_count": 1, "last_viewed_at": now_iso,
        "keywords": [{"id": 1, "name": "time loop"}],
    })
    profile = TasteAnalyzer().analyze(USER)
    top_keywords = profile["top_keywords"]
    assert top_keywords, "expected keyword taste dimension"
    # "time loop" appears in both -> should lead with full score + id preserved.
    assert top_keywords[0]["keyword"] == "time loop"
    assert top_keywords[0]["score"] == 100.0
    assert top_keywords[0]["id"] == 1
    assert top_keywords[0]["count"] == 2

    # The narrative summary surfaces recurring themes (count >= 2).
    assert any("time loop" in h["text"] or "time loop" in h["metric"]
               for h in profile["summary"]["highlights"])


def test_high_user_rating_boosts_but_low_does_not_penalize():
    now_iso = datetime.now().isoformat()
    # Two genres watched equally often; one carries a high per-user rating.
    upsert_user_media(USER, {
        "item_id": "r1", "media_type": "movie", "title": "Loved",
        "year": 2020, "genres": ["Fantasy"], "view_count": 1,
        "last_viewed_at": now_iso, "user_rating": 9.5,
    })
    upsert_user_media(USER, {
        "item_id": "r2", "media_type": "movie", "title": "Meh",
        "year": 2020, "genres": ["Western"], "view_count": 1,
        "last_viewed_at": now_iso, "user_rating": 2.0,
    })
    profile = TasteAnalyzer().analyze(USER)
    scores = {g["genre"]: g["score"] for g in profile["top_genres"]}
    # High-rated genre outranks low-rated one...
    assert scores["Fantasy"] > scores["Western"]
    # ...but the low-rated genre is still present (never penalized to negative).
    assert scores["Western"] > 0
