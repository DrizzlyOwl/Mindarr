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
