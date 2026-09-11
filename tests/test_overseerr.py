import pytest
from unittest.mock import patch, MagicMock
from plex_recommender.discovery.overseerr import OverseerrClient

def test_overseerr_headers():
    client = OverseerrClient(base_url="http://test:5055", api_key="my_secret_key")
    headers = client._get_headers()
    assert headers["X-Api-Key"] == "my_secret_key"
    assert headers["Accept"] == "application/json"

@patch("requests.get")
def test_overseerr_test_connection_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"version": "3.4.1"}
    mock_get.return_value = mock_resp

    client = OverseerrClient(base_url="http://10.255.10.30:5055", api_key="test_key")
    success, msg = client.test_connection()
    assert success is True
    assert "3.4.1" in msg

@patch("requests.post")
def test_overseerr_request_movie(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"id": 1, "status": 2}
    mock_post.return_value = mock_resp

    client = OverseerrClient(base_url="http://10.255.10.30:5055", api_key="test_key")
    success, msg = client.request_media(tmdb_id=27205, media_type="movie")
    assert success is True
    assert "Request submitted" in msg

    # Verify JSON payload
    call_kwargs = mock_post.call_args[1]
    payload = call_kwargs["json"]
    assert payload["mediaType"] == "movie"
    assert payload["mediaId"] == 27205
    assert payload["is4k"] is False

@patch("requests.post")
def test_overseerr_request_tv(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"id": 2, "status": 2}
    mock_post.return_value = mock_resp

    client = OverseerrClient(base_url="http://10.255.10.30:5055", api_key="test_key")
    success, msg = client.request_media(tmdb_id=70523, media_type="tv")
    assert success is True

    call_kwargs = mock_post.call_args[1]
    payload = call_kwargs["json"]
    assert payload["mediaType"] == "tv"
    assert payload["mediaId"] == 70523
    # Default TV request must be Season 1 only [1]
    assert payload["seasons"] == [1]
    assert "Season 1" in msg

@patch("requests.post")
def test_overseerr_request_tv_all_seasons(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"id": 2, "status": 2}
    mock_post.return_value = mock_resp

    client = OverseerrClient(base_url="http://10.255.10.30:5055", api_key="test_key")
    success, msg = client.request_media(tmdb_id=70523, media_type="tv", seasons="all")
    assert success is True

    call_kwargs = mock_post.call_args[1]
    payload = call_kwargs["json"]
    assert payload["seasons"] == "all"


@patch("requests.post")
def test_overseerr_request_with_user_id(mock_post):
    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json.return_value = {"id": 10, "status": 2}
    mock_post.return_value = mock_resp

    client = OverseerrClient(base_url="http://10.255.10.30:5055", api_key="test_key")
    success, msg = client.request_media(tmdb_id=550, media_type="movie", user_id=42)
    assert success is True

    call_kwargs = mock_post.call_args[1]
    payload = call_kwargs["json"]
    assert payload["userId"] == 42
    assert payload["mediaId"] == 550


@patch("requests.get")
def test_overseerr_resolve_user_id(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {"id": 1, "plexId": 111111, "email": "admin@plex.local", "username": "admin"},
            {"id": 5, "plexId": 222222, "email": "user2@plex.local", "username": "user2"},
            {"id": 8, "plexId": None, "email": "plexless@plex.local", "username": "plexless", "plexUsername": "plexless_user"},
        ]
    }
    mock_get.return_value = mock_resp

    client = OverseerrClient(base_url="http://10.255.10.30:5055", api_key="test_key")

    # Match by plexId
    assert client.resolve_user_id(plex_user_key="222222") == 5

    # Match by email
    assert client.resolve_user_id(plex_user_key="999999", email="admin@plex.local") == 1

    # Match by username / plexUsername
    assert client.resolve_user_id(plex_user_key="999999", username="plexless_user") == 8

    # Unmatched
    assert client.resolve_user_id(plex_user_key="999999", email="unknown@plex.local", username="unknown") is None


@patch("requests.get")
def test_overseerr_get_request_queue(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {
                "id": 1,
                "status": 1,
                "media": {"id": 101, "tmdbId": 27205, "mediaType": "movie", "status": 2}  # Pending
            },
            {
                "id": 2,
                "status": 2,
                "media": {"id": 102, "tmdbId": 70523, "mediaType": "tv", "status": 3}     # Processing (downloading)
            },
            {
                "id": 3,
                "status": 2,
                "media": {"id": 103, "tmdbId": 80000, "mediaType": "movie", "status": 5}  # Available (not in active queue)
            }
        ]
    }
    mock_get.return_value = mock_resp

    client = OverseerrClient(base_url="http://10.255.10.30:5055", api_key="test_key")
    queue = client.get_request_queue(force_refresh=True)

    assert "movie_27205" in queue
    assert queue["movie_27205"]["status_code"] == 2
    assert queue["movie_27205"]["status_name"] == "PENDING"

    # TV show should be indexed under both tv_ and show_
    assert "tv_70523" in queue
    assert "show_70523" in queue
    assert queue["tv_70523"]["status_code"] == 3
    assert queue["tv_70523"]["status_name"] == "PROCESSING"

    # Status 5 (Available) is not in the active request queue
    assert "movie_80000" not in queue

    # Test mark_queued adds immediately to in-memory queue
    client.mark_queued(tmdb_id=99999, media_type="movie", status_code=2)
    updated_queue = client.get_request_queue()
    assert "movie_99999" in updated_queue
    assert updated_queue["movie_99999"]["status_name"] == "PENDING"



