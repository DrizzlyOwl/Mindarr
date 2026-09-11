from unittest.mock import patch, MagicMock
from plex_recommender.discovery.tautulli import TautulliClient


def _resp(json_data, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data
    return m


def test_not_configured_returns_empty():
    client = TautulliClient(base_url="", api_key="")
    assert client.is_configured() is False
    assert client.get_users() == []
    ok, _ = client.test_connection()
    assert ok is False


def test_test_connection_success():
    client = TautulliClient(base_url="http://tautulli:8181", api_key="key")
    payload = {"response": {"result": "success", "data": {"pms_name": "HomeServer"}}}
    with patch("plex_recommender.discovery.tautulli.requests.get", return_value=_resp(payload)):
        ok, msg = client.test_connection()
    assert ok is True
    assert "HomeServer" in msg


def test_resolve_user_id_by_email_then_username():
    client = TautulliClient(base_url="http://tautulli:8181", api_key="key")
    users_payload = {"response": {"result": "success", "data": [
        {"user_id": 11, "username": "bob", "email": "bob@x.com"},
        {"user_id": 22, "username": "carol", "email": "carol@x.com"},
    ]}}
    with patch("plex_recommender.discovery.tautulli.requests.get", return_value=_resp(users_payload)):
        assert client.resolve_user_id(email="carol@x.com") == "22"
        assert client.resolve_user_id(username="bob") == "11"
        assert client.resolve_user_id(email="none@x.com") is None


def test_get_history_parses_rows():
    client = TautulliClient(base_url="http://tautulli:8181", api_key="key")
    hist_payload = {"response": {"result": "success", "data": {"data": [
        {"rating_key": "555", "full_title": "Some Movie", "media_type": "movie", "date": 1700000000},
    ]}}}
    with patch("plex_recommender.discovery.tautulli.requests.get", return_value=_resp(hist_payload)):
        rows = client.get_history("22")
    assert len(rows) == 1
    assert rows[0]["rating_key"] == "555"


def test_get_metadata_parses_cast_and_genres():
    client = TautulliClient(base_url="http://tautulli:8181", api_key="key")
    payload = {"response": {"result": "success", "data": {
        "title": "Great Show", "year": 2015,
        "genres": ["Comedy", "Drama"],
        "directors": ["Jane Doe"],
        "actors": ["Actor One", "Actor Two"],
        "imdb_id": "tt0000001",
    }}}
    with patch("plex_recommender.discovery.tautulli.requests.get", return_value=_resp(payload)):
        meta = client.get_metadata("42")
    assert meta["title"] == "Great Show"
    assert meta["genres"] == ["Comedy", "Drama"]
    assert meta["directors"] == ["Jane Doe"]
    assert meta["actors"] == ["Actor One", "Actor Two"]
    assert meta["imdb_id"] == "tt0000001"


def test_get_home_stats_parses_groups():
    client = TautulliClient(base_url="http://tautulli:8181", api_key="key")
    payload = {"response": {"result": "success", "data": [
        {"stat_id": "popular_movies", "rows": [{"title": "Top Film", "users_watched": 4}]},
    ]}}
    with patch("plex_recommender.discovery.tautulli.requests.get", return_value=_resp(payload)):
        stats = client.get_home_stats()
    assert len(stats) == 1
    assert stats[0]["stat_id"] == "popular_movies"


def test_get_history_without_user_id():
    client = TautulliClient(base_url="http://tautulli:8181", api_key="key")
    payload = {"response": {"result": "success", "data": {"data": [
        {"rating_key": "123", "title": "Server Wide Show"},
    ]}}}
    with patch("plex_recommender.discovery.tautulli.requests.get", return_value=_resp(payload)):
        rows = client.get_history()
    assert len(rows) == 1
    assert rows[0]["title"] == "Server Wide Show"


def test_community_merges_popular_stats_and_resolves_viewer_count():
    from plex_recommender.community import CommunityService

    service = CommunityService()
    # Mock Tautulli home stats where top_tv has empty string users_watched,
    # but popular_tv has users_watched: 4
    home_stats = [
        {
            "stat_id": "top_tv",
            "rows": [
                {"title": "The Rookie", "year": 2018, "rating_key": 1001, "total_plays": 20, "users_watched": ""}
            ]
        },
        {
            "stat_id": "popular_tv",
            "rows": [
                {"title": "The Rookie", "year": 2018, "rating_key": 1001, "total_plays": 20, "users_watched": 4}
            ]
        }
    ]

    with patch("plex_recommender.discovery.tautulli.tautulli.is_configured", return_value=True), \
         patch("plex_recommender.discovery.tautulli.tautulli.get_users", return_value=[{"user_id": "1"}, {"user_id": "2"}]), \
         patch("plex_recommender.community.get_all_users", return_value=[]), \
         patch("plex_recommender.discovery.tautulli.tautulli.get_home_stats", return_value=home_stats), \
         patch("plex_recommender.discovery.tautulli.tautulli.get_history", return_value=[]):

        current_user = {"user_key": "user_test", "username": "testuser"}
        data = service._build_from_tautulli(
            current_user=current_user,
            seen_index={"imdb": set(), "tmdb": set(), "tvdb": set(), "title_year": set()},
            user_item_ids=set(),
            local_user_stats={},
            media_meta={},
            local_user_counts={}
        )

        unseen = data["unseen_recommendations"]
        assert len(unseen) == 1
        rookie = unseen[0]
        assert rookie["title"] == "The Rookie"
        # Must reflect 4 viewers (from popular_tv), NOT 1 viewer (from top_tv)
        assert rookie["users_watched"] == 4
        assert rookie["total_plays"] == 20

