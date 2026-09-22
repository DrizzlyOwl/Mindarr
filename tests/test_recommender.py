import pytest
from unittest.mock import MagicMock
from plex_recommender.config import settings
from plex_recommender.db import init_db
from plex_recommender.db.engagement import record_vote
from plex_recommender.db.users import create_or_update_user
from plex_recommender.db.watch import upsert_user_media
from plex_recommender.discovery.tmdb import TMDbClient
from plex_recommender.recommender import (
    ContentRecommender,
    score_candidate,
    compute_genre_overlap,
    compute_keyword_overlap,
    passes_seen_filters,
    passes_pool_filters,
    build_rationale,
    build_seed_list,
    build_discover_tasks,
    KIDS_GENRE_IDS,
)

USER = "u1"


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_recommender.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    create_or_update_user({"user_key": USER, "username": "alice", "email": "a@x.com", "is_admin": True})
    yield


def make_candidate(**overrides):
    base = {
        "tmdb_id": "100",
        "media_type": "movie",
        "title": "Some Movie",
        "year": 2015,
        "rating": 7.5,
        "vote_count": 500,
        "genres": ["Sci-Fi"],
        "genre_ids": [878],
        "keywords": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------

def test_compute_genre_overlap_direct_match():
    top_genres = [{"genre": "Sci-Fi", "score": 90, "count": 10}]
    genre_affinity_map = {"sci-fi": top_genres[0]}
    overlap = compute_genre_overlap(["Sci-Fi", "Comedy"], genre_affinity_map, top_genres)
    assert len(overlap) == 1
    assert overlap[0]["name"] == "Sci-Fi"
    assert overlap[0]["affinity"] == "90%"


def test_compute_genre_overlap_via_alias():
    top_genres = [{"genre": "Sci-Fi", "score": 80, "count": 5}]
    genre_affinity_map = {"sci-fi": top_genres[0]}
    # "Science Fiction" isn't a direct key but aligns with Sci-Fi via genres_align
    overlap = compute_genre_overlap(["Science Fiction"], genre_affinity_map, top_genres)
    assert len(overlap) == 1
    assert overlap[0]["watched_count"] == 5


def test_compute_keyword_overlap():
    keyword_affinity_map = {"heist": {"score": 70, "count": 3}}
    overlap = compute_keyword_overlap([{"name": "Heist"}, {"name": "unrelated"}], keyword_affinity_map)
    assert len(overlap) == 1
    assert overlap[0]["name"] == "Heist"
    assert overlap[0]["affinity"] == "70%"


def test_passes_seen_filters_excludes_tmdb_id():
    cand = make_candidate(tmdb_id="42")
    assert passes_seen_filters(cand, seen_tmdb={"42"}, seen_titles=set()) is False


def test_passes_seen_filters_excludes_normalized_title_year():
    cand = make_candidate(title="Inception", year=2010)
    from plex_recommender.db._util import normalize_title
    key = f"{normalize_title('Inception')}::2010"
    assert passes_seen_filters(cand, seen_tmdb=set(), seen_titles={key}) is False


def test_passes_seen_filters_excludes_yearless_title_prefix_match():
    cand = make_candidate(title="Inception", year=None)
    from plex_recommender.db._util import normalize_title
    key = f"{normalize_title('Inception')}::2010"
    assert passes_seen_filters(cand, seen_tmdb=set(), seen_titles={key}) is False


def test_passes_seen_filters_allows_unseen():
    cand = make_candidate(tmdb_id="999", title="Brand New", year=2024)
    assert passes_seen_filters(cand, seen_tmdb={"1"}, seen_titles=set()) is True


def test_passes_pool_filters_genre_mismatch():
    cand = make_candidate(genres=["Comedy"], genre_ids=[35])
    assert passes_pool_filters(cand, genre_filter="Sci-Fi", include_kids=False, min_rating=0) is False


def test_passes_pool_filters_kids_excluded_by_default():
    cand = make_candidate(genre_ids=[10751])  # Family
    assert passes_pool_filters(cand, genre_filter=None, include_kids=False, min_rating=0) is False


def test_passes_pool_filters_kids_included_when_requested():
    cand = make_candidate(genre_ids=[10751])
    assert passes_pool_filters(cand, genre_filter=None, include_kids=True, min_rating=0) is True


def test_passes_pool_filters_rating_threshold():
    cand = make_candidate(rating=5.0)
    assert passes_pool_filters(cand, genre_filter=None, include_kids=True, min_rating=7.0) is False
    cand2 = make_candidate(rating=8.0)
    assert passes_pool_filters(cand2, genre_filter=None, include_kids=True, min_rating=7.0) is True


def test_score_candidate_base_score():
    cand = make_candidate(genres=[], genre_ids=[], rating=5.0)
    score_candidate(
        cand,
        top_genres=[],
        genre_affinity_map={},
        keyword_affinity_map={},
        favored_decades_map={},
        upvoted_ids=set(),
        upvoted_titles=set(),
        seed_origin={},
        genre_filter=None,
    )
    assert cand["match_score"] == 50


def test_score_candidate_genre_and_rating_bonus():
    top_genres = [{"genre": "Sci-Fi", "score": 90, "count": 10}]
    cand = make_candidate(genres=["Sci-Fi"], rating=8.5)
    score_candidate(
        cand,
        top_genres=top_genres,
        genre_affinity_map={"sci-fi": top_genres[0]},
        keyword_affinity_map={},
        favored_decades_map={},
        upvoted_ids=set(),
        upvoted_titles=set(),
        seed_origin={},
        genre_filter=None,
    )
    # 50 base + 15 genre bonus (1 overlap) + 15 rating bonus (>=8.0) = 80
    assert cand["match_score"] == 80


def test_score_candidate_caps_at_99():
    top_genres = [{"genre": "Sci-Fi", "score": 90, "count": 10}, {"genre": "Action", "score": 80, "count": 8}]
    cand = make_candidate(genres=["Sci-Fi", "Action"], rating=9.0, year=2020)
    score_candidate(
        cand,
        top_genres=top_genres,
        genre_affinity_map={"sci-fi": top_genres[0], "action": top_genres[1]},
        keyword_affinity_map={},
        favored_decades_map={"2020s": 5},
        upvoted_ids={"100"},
        upvoted_titles=set(),
        seed_origin={"100": "Some Watched Title"},
        genre_filter=None,
    )
    # 50 + 30 (genre cap) + 15 (rating) + 5 (decade) + 20 (upvote) + 10 (seed) = 130 -> capped 99
    assert cand["match_score"] == 99


def test_score_candidate_upvoted_seed_gets_higher_bonus_than_plain_seed():
    cand_a = make_candidate(tmdb_id="1", genres=[], rating=0)
    score_candidate(
        cand_a, top_genres=[], genre_affinity_map={}, keyword_affinity_map={},
        favored_decades_map={}, upvoted_ids=set(), upvoted_titles={"Seed Title"},
        seed_origin={"1": "Seed Title"}, genre_filter=None,
    )
    cand_b = make_candidate(tmdb_id="2", genres=[], rating=0)
    score_candidate(
        cand_b, top_genres=[], genre_affinity_map={}, keyword_affinity_map={},
        favored_decades_map={}, upvoted_ids=set(), upvoted_titles=set(),
        seed_origin={"2": "Seed Title"}, genre_filter=None,
    )
    assert cand_a["match_score"] > cand_b["match_score"]


def test_score_candidate_reorders_genres_by_matched_filter():
    cand = make_candidate(genres=["Comedy", "Sci-Fi"], genre_ids=[35, 878])
    score_candidate(
        cand, top_genres=[], genre_affinity_map={}, keyword_affinity_map={},
        favored_decades_map={}, upvoted_ids=set(), upvoted_titles=set(),
        seed_origin={}, genre_filter="Sci-Fi",
    )
    assert cand["genres"][0] == "Sci-Fi"
    assert cand["matched_category"] == "Sci-Fi"


def test_build_rationale_includes_seed_and_genre_and_cleans_temp_fields():
    cand = make_candidate()
    score_candidate(
        cand, top_genres=[{"genre": "Sci-Fi", "score": 90, "count": 10}],
        genre_affinity_map={"sci-fi": {"genre": "Sci-Fi", "score": 90, "count": 10}},
        keyword_affinity_map={}, favored_decades_map={}, upvoted_ids=set(),
        upvoted_titles=set(), seed_origin={"100": "Watched Thing"}, genre_filter=None,
    )
    rationale, decision_factors = build_rationale(cand, genre_filter=None, favored_decades_map={})
    assert "Because you watched Watched Thing" in rationale
    assert decision_factors["seed_title"] == "Watched Thing"
    for k in ("_seed_title", "_overlap_details", "_keyword_overlap", "_is_favored_decade", "_decade_str", "_score_points"):
        assert k not in cand


def test_build_seed_list_prioritizes_upvotes_and_filters_media_type():
    upvoted_items = [{"tmdb_id": "5", "media_type": "movie", "title": "Upvoted Movie"}]
    seen_items = [
        {"tmdb_id": "6", "media_type": "movie", "title": "Watched Movie", "user_rating": 9.0, "view_count": 2},
        {"tmdb_id": "7", "media_type": "show", "title": "Watched Show", "user_rating": 9.5, "view_count": 1},
    ]
    seeds = build_seed_list("u1", media_type="movie", genre_filter=None, upvoted_items=upvoted_items, seen_items=seen_items)
    # Show seed excluded by media_type filter; upvoted seed ranked first (rating=999)
    assert len(seeds) == 2
    assert seeds[0][2] == "5"
    assert seeds[0][0] == 999.0


def test_build_seed_list_filters_by_genre():
    seen_items = [
        {"tmdb_id": "1", "media_type": "movie", "title": "A", "genres": ["Comedy"], "user_rating": 8},
        {"tmdb_id": "2", "media_type": "movie", "title": "B", "genres": ["Sci-Fi"], "user_rating": 8},
    ]
    seeds = build_seed_list("u1", media_type="all", genre_filter="Sci-Fi", upvoted_items=[], seen_items=seen_items)
    assert len(seeds) == 1
    assert seeds[0][2] == "2"


def test_build_discover_tasks_skips_unsupported_tv_genre():
    tmdb = MagicMock()
    tmdb.map_genres.side_effect = lambda genres, media_type: [10749] if media_type == "movie" else []
    tasks = build_discover_tasks(
        tmdb, types_to_query=["movie", "show"], selected_genres=["Romance"],
        genre_filter="Romance", lang_code="en", query_keyword_ids=None, min_rating=7.0,
    )
    # TV has no genre id for Romance with genre_filter set -> show tasks skipped entirely
    assert all(t[2] == "movie" for t in tasks)
    assert len(tasks) == 2  # primary + popularity-sorted secondary movie query


# ---------------------------------------------------------------------------
# Integration tests via ContentRecommender.get_recommendations
# ---------------------------------------------------------------------------

def _seed_history(genres=("Sci-Fi", "Action")):
    upsert_user_media(USER, {
        "item_id": "movie_1",
        "media_type": "movie",
        "title": "Inception",
        "year": 2010,
        "genres": list(genres),
        "imdb_id": "tt1375666",
        "tmdb_id": "27205",
        "view_count": 3,
    })


def make_mock_tmdb():
    """A MagicMock TMDb client with a real `format_item` (needed to produce
    properly-shaped candidate dicts, since the recommender relies on its
    output structure, not just a mock return value)."""
    real = TMDbClient(api_key="test_key")
    mock_tmdb = MagicMock()
    mock_tmdb.format_item.side_effect = real.format_item
    mock_tmdb.get_web_url.side_effect = real.get_web_url
    mock_tmdb.discover_tv.return_value = []
    mock_tmdb.get_similar.return_value = []
    mock_tmdb.get_tmdb_recommendations.return_value = []
    mock_tmdb.batch_get_external_ids.return_value = {}
    return mock_tmdb


def test_get_recommendations_no_history_returns_error():
    recommender = ContentRecommender(tmdb_client=MagicMock())
    result = recommender.get_recommendations(USER)
    assert result["success"] is False
    assert "watch history" in result["error"].lower()


def test_get_recommendations_returns_unseen_candidate():
    _seed_history()
    mock_tmdb = make_mock_tmdb()
    mock_tmdb.map_genres.return_value = [878]
    mock_tmdb.discover_movies.return_value = [
        {"id": 8990, "title": "Primer", "release_date": "2004-10-08", "vote_average": 7.5,
         "vote_count": 2100, "genre_ids": [878], "poster_path": "/p.jpg", "overview": "..."}
    ]

    recommender = ContentRecommender(tmdb_client=mock_tmdb)
    result = recommender.get_recommendations(USER, min_rating=7.0, stage="full")

    assert result["success"] is True
    titles = [r["title"] for r in result["recommendations"]]
    assert "Primer" in titles
    assert "Inception" not in titles


def test_get_recommendations_excludes_seen_title():
    _seed_history()
    mock_tmdb = make_mock_tmdb()
    mock_tmdb.map_genres.return_value = [878]
    mock_tmdb.discover_movies.return_value = [
        {"id": 27205, "title": "Inception", "release_date": "2010-07-16", "vote_average": 8.4,
         "vote_count": 35000, "genre_ids": [878], "poster_path": "/i.jpg", "overview": "..."}
    ]

    recommender = ContentRecommender(tmdb_client=mock_tmdb)
    result = recommender.get_recommendations(USER, stage="full")

    assert result["recommendations"] == []
    assert result["exhaustion_reason"] == "exhausted"


def test_get_recommendations_fast_stage_limits_to_six():
    _seed_history()
    mock_tmdb = make_mock_tmdb()
    mock_tmdb.map_genres.return_value = [878]
    mock_tmdb.discover_movies.return_value = [
        {"id": 8990 + i, "title": f"Movie {i}", "release_date": "2015-01-01", "vote_average": 7.5,
         "vote_count": 500, "genre_ids": [878], "poster_path": "/p.jpg", "overview": "..."}
        for i in range(10)
    ]

    recommender = ContentRecommender(tmdb_client=mock_tmdb)
    result = recommender.get_recommendations(USER, limit=12, stage="fast")

    assert result["stage"] == "fast"
    assert len(result["recommendations"]) <= 6
    # Fast stage never calls seed expansion (get_similar/get_tmdb_recommendations)
    mock_tmdb.get_similar.assert_not_called()
    mock_tmdb.get_tmdb_recommendations.assert_not_called()


def test_get_recommendations_only_available_filters_without_counting_as_exhausted():
    _seed_history()
    mock_tmdb = make_mock_tmdb()
    mock_tmdb.map_genres.return_value = [878]
    mock_tmdb.discover_movies.return_value = [
        {"id": 8990, "title": "Primer", "release_date": "2004-10-08", "vote_average": 7.5,
         "vote_count": 2100, "genre_ids": [878], "poster_path": "/p.jpg", "overview": "..."}
    ]

    recommender = ContentRecommender(tmdb_client=mock_tmdb)
    # Not in the Plex library -> filtered by only_available, but this is not a
    # "seen" exclusion, so exhaustion_reason should be no_results, not exhausted.
    result = recommender.get_recommendations(USER, only_available=True, stage="full")

    assert result["recommendations"] == []
    assert result["exhaustion_reason"] == "no_results"


def test_get_recommendations_upvoted_item_ranks_first():
    _seed_history()
    record_vote(USER, tmdb_id="55555", media_type="movie", title="Loved Movie", vote=1)

    mock_tmdb = make_mock_tmdb()
    mock_tmdb.map_genres.return_value = [878]
    mock_tmdb.discover_movies.return_value = [
        {"id": 8990, "title": "Plain Movie", "release_date": "2015-01-01", "vote_average": 7.5,
         "vote_count": 500, "genre_ids": [878], "poster_path": "/p.jpg", "overview": "..."},
        {"id": 55555, "title": "Loved Movie", "release_date": "2015-01-01", "vote_average": 7.5,
         "vote_count": 500, "genre_ids": [878], "poster_path": "/p.jpg", "overview": "..."},
    ]

    recommender = ContentRecommender(tmdb_client=mock_tmdb)
    result = recommender.get_recommendations(USER, stage="full")

    assert result["recommendations"][0]["title"] == "Loved Movie"
    assert result["recommendations"][0]["is_upvoted"] is True


def test_get_recommendations_cache_hit_serves_pool(monkeypatch):
    _seed_history()
    mock_tmdb = make_mock_tmdb()
    mock_tmdb.map_genres.return_value = [878]
    mock_tmdb.discover_movies.return_value = [
        {"id": 8990, "title": "Primer", "release_date": "2004-10-08", "vote_average": 7.5,
         "vote_count": 2100, "genre_ids": [878], "poster_path": "/p.jpg", "overview": "..."}
    ]

    recommender = ContentRecommender(tmdb_client=mock_tmdb)
    first = recommender.get_recommendations(USER, stage="full")
    assert first["from_cache"] is False

    second = recommender.get_recommendations(USER, stage="full")
    assert second["from_cache"] is True
    assert second["recommendations"][0]["title"] == "Primer"
    # Discovery should not be re-invoked on a cache hit (2 calls from the first
    # run: primary + popularity-sorted movie queries; unchanged on cache hit).
    assert mock_tmdb.discover_movies.call_count == 2
