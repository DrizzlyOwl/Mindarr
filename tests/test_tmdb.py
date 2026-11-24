import pytest
from unittest.mock import patch, MagicMock
from plex_recommender.discovery.tmdb import TMDbClient, MOVIE_GENRES, TV_GENRES

def test_genre_mapping():
    client = TMDbClient(api_key="test_key")
    
    movie_ids = client.map_genres(["Sci-Fi", "Mystery", "NonExistentGenre"], media_type="movie")
    assert MOVIE_GENRES["sci-fi"] in movie_ids
    assert MOVIE_GENRES["mystery"] in movie_ids
    assert len(movie_ids) == 2

    tv_ids = client.map_genres(["Animation", "Crime"], media_type="tv")
    assert TV_GENRES["animation"] in tv_ids
    assert TV_GENRES["crime"] in tv_ids

def test_item_formatting():
    client = TMDbClient(api_key="test_key")
    raw = {
        "id": 12345,
        "title": "Coherence",
        "release_date": "2013-09-19",
        "vote_average": 7.2,
        "vote_count": 1400,
        "overview": "Eight friends at a dinner party...",
        "poster_path": "/abc123xyz.jpg",
        "genre_ids": [878, 9648]
    }
    item = client.format_item(raw, media_type="movie")
    assert item["tmdb_id"] == "12345"
    assert item["title"] == "Coherence"
    assert item["year"] == 2013
    assert item["rating"] == 7.2
    assert "Sci-Fi" in item["genres"]
    assert "Mystery" in item["genres"]
    assert "image.tmdb.org" in item["poster_url"]


def test_get_keywords_movie(monkeypatch):
    client = TMDbClient(api_key="test_key")
    monkeypatch.setattr(client, "_request", lambda ep, params=None: {
        "keywords": [{"id": 1, "name": "heist"}, {"id": 2, "name": "time loop"}]
    })
    kws = client.get_keywords(12345, media_type="movie")
    assert {"id": 1, "name": "heist"} in kws
    assert len(kws) == 2


def test_get_keywords_tv_uses_results(monkeypatch):
    client = TMDbClient(api_key="test_key")
    monkeypatch.setattr(client, "_request", lambda ep, params=None: {
        "results": [{"id": 9, "name": "dystopia"}]
    })
    kws = client.get_keywords(555, media_type="show")
    assert kws == [{"id": 9, "name": "dystopia"}]


def test_get_keywords_non_fatal(monkeypatch):
    client = TMDbClient(api_key="test_key")
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(client, "_request", boom)
    assert client.get_keywords(1, media_type="movie") == []


def test_discover_movies_with_keywords(monkeypatch):
    client = TMDbClient(api_key="test_key")
    captured = {}
    def fake_request(ep, params=None):
        captured["endpoint"] = ep
        captured["params"] = params
        return {"results": [{"id": 1}]}
    monkeypatch.setattr(client, "_request", fake_request)
    client.discover_movies(with_genres=[28], with_keywords=[101, 202])
    assert captured["params"]["with_keywords"] == "101|202"


def test_get_similar_and_recommendations(monkeypatch):
    client = TMDbClient(api_key="test_key")
    monkeypatch.setattr(client, "_request", lambda ep, params=None: {"results": [{"id": 7}]})
    assert client.get_similar(1, media_type="movie") == [{"id": 7}]
    assert client.get_tmdb_recommendations(1, media_type="show") == [{"id": 7}]


def test_genre_alias_matching():
    from plex_recommender.discovery.tmdb import candidate_matches_genre

    cand_scifi = {"genre_ids": [878], "genres": ["Sci-Fi"]}
    assert candidate_matches_genre(cand_scifi, "Science Fiction") is True
    assert candidate_matches_genre(cand_scifi, "Sci-Fi") is True
    assert candidate_matches_genre(cand_scifi, "Comedy") is False

    cand_action_tv = {"genre_ids": [10759], "genres": ["Action & Adventure"]}
    assert candidate_matches_genre(cand_action_tv, "Action") is True
    assert candidate_matches_genre(cand_action_tv, "Adventure") is True
    assert candidate_matches_genre(cand_action_tv, "Drama") is False


def test_batch_get_external_ids(monkeypatch):
    client = TMDbClient(api_key="test_key")

    def fake_get_ext(item_id, media_type="movie"):
        return {"imdb_id": f"tt{item_id}", "tvdb_id": None}

    monkeypatch.setattr(client, "get_external_ids", fake_get_ext)
    items = [(101, "movie"), (202, "movie"), (303, "show")]
    batch = client.batch_get_external_ids(items, max_workers=2)

    assert len(batch) == 3
    assert batch["101"]["imdb_id"] == "tt101"
    assert batch["202"]["imdb_id"] == "tt202"
    assert batch["303"]["imdb_id"] == "tt303"


def test_genres_align_and_canonicalization():
    from plex_recommender.discovery.tmdb import to_canonical_genre, genres_align

    assert to_canonical_genre("science fiction") == "Sci-Fi"
    assert to_canonical_genre("Sci-Fi") == "Sci-Fi"
    assert to_canonical_genre("action & adventure") == "Action & Adventure"
    assert to_canonical_genre("anime") == "Animation"
    assert to_canonical_genre("suspense") == "Thriller"
    assert to_canonical_genre("kids") == "Family"

    assert genres_align("Science Fiction", "Sci-Fi") is True
    assert genres_align("Action", "Action & Adventure") is True
    assert genres_align("Anime", "Animation") is True
    assert genres_align("Suspense", "Thriller") is True
    assert genres_align("Comedy", "Drama") is False


def test_get_tmdb_categories():
    from plex_recommender.discovery.tmdb import get_tmdb_categories

    movie_cats = get_tmdb_categories("movie")
    tv_cats = get_tmdb_categories("show")
    all_cats = get_tmdb_categories("all")

    assert "Romance" in movie_cats
    assert "Romance" not in tv_cats
    assert "Action & Adventure" in tv_cats
    assert "Action & Adventure" not in movie_cats

    # Non-TMDb genres must not appear
    for non_tmdb in ("Sport", "Mini-Series", "Indie", "Short", "Biography"):
        assert non_tmdb not in movie_cats
        assert non_tmdb not in tv_cats
        assert non_tmdb not in all_cats


def test_romance_does_not_match_crime_drama():
    from plex_recommender.discovery.tmdb import candidate_matches_genre

    crime_drama_show = {
        "genre_ids": [80, 18],  # Crime (80), Drama (18)
        "genres": ["Crime", "Drama"]
    }

    assert candidate_matches_genre(crime_drama_show, "Crime") is True
    assert candidate_matches_genre(crime_drama_show, "Drama") is True
    assert candidate_matches_genre(crime_drama_show, "Romance") is False


def test_tmdb_get_web_url():
    from plex_recommender.discovery.tmdb import TMDbClient
    client = TMDbClient()

    # Shows must link to /tv/
    assert client.get_web_url(246, media_type="show") == "https://www.themoviedb.org/tv/246"
    assert client.get_web_url("246", media_type="tv") == "https://www.themoviedb.org/tv/246"
    assert client.get_web_url(246, media_type="episode") == "https://www.themoviedb.org/tv/246"

    # Movies must link to /movie/
    assert client.get_web_url(27205, media_type="movie") == "https://www.themoviedb.org/movie/27205"


def test_is_configured(monkeypatch):
    import plex_recommender.discovery.tmdb as tmdb_module
    assert TMDbClient(api_key="test_key").is_configured() is True

    monkeypatch.setattr(tmdb_module.settings, "tmdb_api_key", None)
    assert TMDbClient(api_key=None).is_configured() is False


def test_api_key_reads_live_from_settings(monkeypatch):
    """Regression test: api_key must not be frozen at construction time, so a
    key change in Settings takes effect without a process restart."""
    import plex_recommender.discovery.tmdb as tmdb_module

    monkeypatch.setattr(tmdb_module.settings, "tmdb_api_key", "old_key")
    client = TMDbClient()
    assert client.api_key == "old_key"

    monkeypatch.setattr(tmdb_module.settings, "tmdb_api_key", "new_key")
    assert client.api_key == "new_key"


def test_explicit_api_key_override_takes_precedence(monkeypatch):
    import plex_recommender.discovery.tmdb as tmdb_module

    monkeypatch.setattr(tmdb_module.settings, "tmdb_api_key", "settings_key")
    client = TMDbClient(api_key="explicit_key")
    assert client.api_key == "explicit_key"


def test_test_connection_not_configured(monkeypatch):
    import plex_recommender.discovery.tmdb as tmdb_module
    monkeypatch.setattr(tmdb_module.settings, "tmdb_api_key", None)

    client = TMDbClient(api_key=None)
    success, msg = client.test_connection()
    assert success is False
    assert "not configured" in msg.lower()


@patch("requests.Session.get")
def test_test_connection_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"success": True}
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    client = TMDbClient(api_key="test_key")
    success, msg = client.test_connection()
    assert success is True
    assert "Connected" in msg


@patch("requests.Session.get")
def test_test_connection_unauthorized(mock_get):
    import requests

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    http_err = requests.exceptions.HTTPError(response=mock_resp)
    mock_resp.raise_for_status.side_effect = http_err
    mock_get.return_value = mock_resp

    client = TMDbClient(api_key="bad_key")
    success, msg = client.test_connection()
    assert success is False
    assert "invalid" in msg.lower()


def test_get_details_movie_parses_trailer_credits_and_providers(monkeypatch):
    client = TMDbClient(api_key="test_key")
    calls = []

    def fake_request(ep, params=None):
        calls.append((ep, params))
        return {
            "id": 603,
            "title": "The Matrix",
            "tagline": "Welcome to the Real World.",
            "overview": "A computer hacker learns...",
            "release_date": "1999-03-31",
            "vote_average": 8.2,
            "vote_count": 25000,
            "runtime": 136,
            "status": "Released",
            "genres": [{"id": 28, "name": "Action"}, {"id": 878, "name": "Science Fiction"}],
            "poster_path": "/matrix.jpg",
            "backdrop_path": "/matrix_bg.jpg",
            "original_language": "en",
            "external_ids": {"imdb_id": "tt0133093"},
            "credits": {
                "crew": [
                    {"job": "Director", "name": "Lana Wachowski"},
                    {"job": "Director", "name": "Lilly Wachowski"},
                ],
                "cast": [
                    {"name": "Keanu Reeves", "character": "Neo", "profile_path": "/keanu.jpg"},
                ],
            },
            "videos": {
                "results": [
                    {"site": "YouTube", "type": "Teaser", "official": True, "key": "teaser1", "name": "Teaser"},
                    {"site": "YouTube", "type": "Trailer", "official": True, "key": "trailer_main", "name": "Official Trailer"},
                ],
            },
            "watch/providers": {
                "results": {
                    "US": {
                        "flatrate": [{"provider_name": "Max", "logo_path": "/max.png"}]
                    }
                }
            },
        }

    monkeypatch.setattr(client, "_request", fake_request)
    details = client.get_details(603, media_type="movie")
    assert details is not None
    assert details["title"] == "The Matrix"
    assert details["year"] == 1999
    assert details["runtime"] == 136
    assert details["genres"] == ["Action", "Sci-Fi"]
    assert details["directors"] == ["Lana Wachowski", "Lilly Wachowski"]
    assert details["cast"][0]["name"] == "Keanu Reeves"
    assert details["trailer"]["key"] == "trailer_main"
    assert details["watch_providers"][0]["name"] == "Max"
    assert details["imdb_url"] == "https://www.imdb.com/title/tt0133093/"

    # Second call served from in-memory TTL cache
    cached = client.get_details(603, media_type="movie")
    assert cached["title"] == "The Matrix"
    assert len(calls) == 1


def test_get_details_tv_filters_specials_and_parses_seasons(monkeypatch):
    client = TMDbClient(api_key="test_key")
    monkeypatch.setattr(client, "_request", lambda ep, params=None: {
        "id": 1396,
        "name": "Breaking Bad",
        "overview": "Chemistry teacher...",
        "first_air_date": "2008-01-20",
        "vote_average": 8.9,
        "vote_count": 14000,
        "episode_run_time": [45],
        "genres": [{"id": 18, "name": "Drama"}],
        "created_by": [{"name": "Vince Gilligan"}],
        "seasons": [
            {"season_number": 0, "name": "Specials", "episode_count": 9},
            {"season_number": 2, "name": "Season 2", "episode_count": 13},
            {"season_number": 1, "name": "Season 1", "episode_count": 7},
        ],
        "number_of_seasons": 5,
        "number_of_episodes": 62,
    })

    details = client.get_details(1396, media_type="show")
    assert details is not None
    assert details["media_type"] == "show"
    assert details["directors"] == ["Vince Gilligan"]
    assert [s["season_number"] for s in details["seasons"]] == [1, 2]
    assert details["number_of_seasons"] == 5


def test_get_details_non_fatal_on_error(monkeypatch):
    client = TMDbClient(api_key="test_key")
    monkeypatch.setattr(client, "_request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert client.get_details(123, media_type="movie") is None


