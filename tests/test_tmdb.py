import pytest
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

