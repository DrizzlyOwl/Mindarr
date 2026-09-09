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

