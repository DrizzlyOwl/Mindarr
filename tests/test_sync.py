import pytest
from unittest.mock import patch
from plex_recommender.config import settings
from plex_recommender.db import init_db
from plex_recommender.db.users import create_or_update_user
from plex_recommender.db.watch import has_user_history
from plex_recommender import sync

USER = "1"


class FakeAccount:
    def __init__(self, account_id, name):
        self.accountID = account_id
        self.name = name


# The admin (USER) maps to server-local account id 1.
FAKE_SYSTEM_ACCOUNTS = [FakeAccount(1, "alice"), FakeAccount(50, "other")]


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_sync.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    create_or_update_user({"user_key": USER, "username": "alice", "email": "a@x.com", "is_admin": True})
    yield


# --- Plex history (primary source) ---

def test_plex_history_skipped_without_token(monkeypatch):
    monkeypatch.setattr(settings, "plex_token", None)
    assert sync.sync_plex_user_history(USER) == 0
    assert has_user_history(USER) is False


def test_plex_history_records_events(monkeypatch):
    monkeypatch.setattr(settings, "plex_token", "tok")

    class FakeHistItem:
        ratingKey = "555"
        type = "movie"
        title = "Some Movie"
        viewedAt = None
        duration = 0

    class FakeMeta:
        year = 2020
        guids = []
        guid = "imdb://tt0111161"

    class FakePlex:
        def systemAccounts(self):
            return FAKE_SYSTEM_ACCOUNTS

        def history(self, maxresults=5000, accountID=None):
            assert accountID == 1
            return [FakeHistItem()]

        def fetchItem(self, key):
            return FakeMeta()

    with patch.object(sync, "get_plex_instance", return_value=FakePlex()):
        count = sync.sync_plex_user_history(USER)
    assert count == 1
    assert has_user_history(USER) is True


def test_plex_episode_rolls_up_to_show_with_genres(monkeypatch):
    from plex_recommender.db.watch import get_user_media_items
    monkeypatch.setattr(settings, "plex_token", "tok")

    class Tag:
        def __init__(self, tag):
            self.tag = tag

    class FakeEpisode:
        ratingKey = "999"
        type = "episode"
        title = "Pilot"
        viewedAt = None
        duration = 0
        grandparentRatingKey = "42"

    class FakeShow:
        year = 2015
        guids = []
        guid = "tvdb://12345"
        title = "Great Show"
        genres = [Tag("Comedy"), Tag("Drama")]
        directors = []
        roles = []

    class FakePlex:
        def systemAccounts(self):
            return FAKE_SYSTEM_ACCOUNTS

        def history(self, maxresults=5000, accountID=None):
            return [FakeEpisode()]

        def fetchItem(self, key):
            assert int(key) == 42  # rolled up to the show
            return FakeShow()

    with patch.object(sync, "get_plex_instance", return_value=FakePlex()):
        sync.sync_plex_user_history(USER)

    items = get_user_media_items(USER)
    shows = [i for i in items if i["media_type"] == "show"]
    assert len(shows) == 1
    assert shows[0]["title"] == "Great Show"
    assert set(shows[0]["genres"]) == {"Comedy", "Drama"}


def test_sync_library_metadata_uses_bare_ratingkey_item_id(monkeypatch):
    """Regression test: the library scan must NOT prefix item_id with
    movie_/show_/episode_, since watch-history sync always uses the bare
    ratingKey. A mismatch here re-splits media_items into duplicate rows."""
    from plex_recommender.db import get_connection

    monkeypatch.setattr(settings, "plex_token", "tok")

    class Tag:
        def __init__(self, tag):
            self.tag = tag

    class FakeMovie:
        ratingKey = "8001"
        title = "Bare Key Movie"
        year = 2022
        genres = [Tag("Action")]
        directors = []
        writers = []
        roles = []
        summary = ""
        userRating = None
        audienceRating = None
        rating = None
        viewCount = 1
        lastViewedAt = None
        guids = []
        guid = "imdb://tt8000000"
        originallyAvailableAt = None

    class FakeMovieSection:
        title = "Movies"
        type = "movie"
        def all(self):
            return [FakeMovie()]

    class FakeLibrary:
        def sections(self):
            return [FakeMovieSection()]

    class FakePlex:
        friendlyName = "Test Server"
        library = FakeLibrary()

    with patch.object(sync, "get_plex_instance", return_value=FakePlex()):
        sync.sync_library_metadata()

    conn = get_connection()
    rows = conn.execute("SELECT item_id FROM media_items WHERE title = 'Bare Key Movie'").fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["item_id"] == "8001"  # bare, no "movie_" prefix


def test_plex_history_tallies_view_count_across_multiple_events(monkeypatch):
    """Regression test: multiple watch events for the same movie/show must
    accumulate into view_count, not collapse to 1 (MAX-based upsert bug)."""
    from plex_recommender.db.watch import get_user_media_items
    monkeypatch.setattr(settings, "plex_token", "tok")

    class Tag:
        def __init__(self, tag):
            self.tag = tag

    class FakeMovieEvent:
        ratingKey = "777"
        type = "movie"
        title = "Rewatched Movie"
        viewedAt = None
        duration = 0

    class FakeEpisodeEvent:
        def __init__(self, rating_key):
            self.ratingKey = rating_key
            self.type = "episode"
            self.title = "Ep"
            self.viewedAt = None
            self.duration = 0
            self.grandparentRatingKey = "42"

    class FakeMovie:
        year = 2018
        guids = []
        guid = "imdb://tt9999999"
        title = "Rewatched Movie"
        genres = []
        directors = []
        roles = []

    class FakeShow:
        year = 2015
        guids = []
        guid = "tvdb://12345"
        title = "Binged Show"
        genres = [Tag("Comedy")]
        directors = []
        roles = []

    class FakePlex:
        def systemAccounts(self):
            return FAKE_SYSTEM_ACCOUNTS

        def history(self, maxresults=5000, accountID=None):
            return [
                FakeMovieEvent(), FakeMovieEvent(), FakeMovieEvent(),
                FakeEpisodeEvent("101"), FakeEpisodeEvent("102"),
            ]

        def fetchItem(self, key):
            if int(key) == 42:
                return FakeShow()
            return FakeMovie()

    with patch.object(sync, "get_plex_instance", return_value=FakePlex()):
        sync.sync_plex_user_history(USER)

    items = {i["item_id"]: i for i in get_user_media_items(USER)}
    assert items["777"]["view_count"] == 3
    assert items["42"]["view_count"] == 2


# --- Tautulli enrichment (optional secondary source) ---

def test_no_enrichment_when_tautulli_unconfigured():
    with patch.object(sync.tautulli, "is_configured", return_value=False):
        assert sync.sync_tautulli_user_history(USER) == 0
    assert has_user_history(USER) is False


def test_no_enrichment_when_no_tautulli_match():
    with patch.object(sync.tautulli, "is_configured", return_value=True), \
         patch.object(sync.tautulli, "monitors_server", return_value=True), \
         patch.object(sync.tautulli, "resolve_user_id", return_value=None):
        assert sync.sync_tautulli_user_history(USER) == 0
    assert has_user_history(USER) is False


def test_no_enrichment_when_tautulli_monitors_other_server():
    with patch.object(sync.tautulli, "is_configured", return_value=True), \
         patch.object(sync.tautulli, "monitors_server", return_value=False):
        assert sync.sync_tautulli_user_history(USER) == 0
    assert has_user_history(USER) is False


def test_no_enrichment_when_no_history():
    with patch.object(sync.tautulli, "is_configured", return_value=True), \
         patch.object(sync.tautulli, "monitors_server", return_value=True), \
         patch.object(sync.tautulli, "resolve_user_id", return_value="22"), \
         patch.object(sync.tautulli, "get_history", return_value=[]):
        assert sync.sync_tautulli_user_history(USER) == 0
    assert has_user_history(USER) is False


def test_tautulli_records_history_when_matched():
    from plex_recommender.db.watch import get_user_media_items
    history = [{"rating_key": "555", "full_title": "Some Movie", "media_type": "movie",
                "date": 1700000000, "year": 2020}]
    meta = {"imdb_id": "tt0111161", "tmdb_id": "278", "tvdb_id": None,
            "title": "Some Movie", "year": 2020, "genres": ["Drama"],
            "directors": ["Dir One"], "actors": ["Cast One", "Cast Two"]}
    with patch.object(sync.tautulli, "is_configured", return_value=True), \
         patch.object(sync.tautulli, "monitors_server", return_value=True), \
         patch.object(sync.tautulli, "resolve_user_id", return_value="22"), \
         patch.object(sync.tautulli, "get_history", return_value=history), \
         patch.object(sync.tautulli, "get_metadata", return_value=meta):
        count = sync.sync_tautulli_user_history(USER)
    assert count == 1
    assert has_user_history(USER) is True
    item = [i for i in get_user_media_items(USER) if i["item_id"] == "555"][0]
    assert item["directors"] == ["Dir One"]
    assert item["actors"] == ["Cast One", "Cast Two"]


# --- Combined sync ---

def test_combined_uses_plex_even_without_tautulli(monkeypatch):
    monkeypatch.setattr(settings, "plex_token", "tok")
    with patch.object(sync, "sync_plex_user_history", return_value=3) as plex_fn, \
         patch.object(sync.tautulli, "is_configured", return_value=False):
        result = sync.sync_user_history(USER)
    plex_fn.assert_called_once()
    assert result["plex_events"] == 3
    assert result["tautulli_events"] == 0
    assert result["total"] == 3


def test_combined_adds_plex_and_tautulli(monkeypatch):
    with patch.object(sync, "sync_plex_user_history", return_value=2), \
         patch.object(sync, "sync_tautulli_user_history", return_value=5):
        result = sync.sync_user_history(USER)
    assert result["plex_events"] == 2
    assert result["tautulli_events"] == 5
    assert result["total"] == 7


def test_watch_source_breakdown_tracks_plex_and_tautulli(monkeypatch):
    from plex_recommender.db.watch import get_watch_source_breakdown
    # Plex path
    monkeypatch.setattr(settings, "plex_token", "tok")

    class Hist:
        ratingKey = "10"; type = "movie"; title = "M"; viewedAt = None; duration = 0

    class Meta:
        year = 2020; guids = []; guid = ""; title = "M"; genres = []; directors = []; roles = []

    class FakePlex:
        def systemAccounts(self):
            return FAKE_SYSTEM_ACCOUNTS
        def history(self, maxresults=5000, accountID=None):
            return [Hist()]
        def fetchItem(self, key):
            return Meta()

    with patch.object(sync, "get_plex_instance", return_value=FakePlex()):
        sync.sync_plex_user_history(USER)

    # Tautulli path
    history = [{"rating_key": "20", "full_title": "T", "media_type": "movie",
                "date": 1700000000, "year": 2020}]
    meta = {"imdb_id": None, "tmdb_id": None, "tvdb_id": None, "title": "T",
            "year": 2020, "genres": [], "directors": [], "actors": []}
    with patch.object(sync.tautulli, "is_configured", return_value=True), \
         patch.object(sync.tautulli, "monitors_server", return_value=True), \
         patch.object(sync.tautulli, "resolve_user_id", return_value="22"), \
         patch.object(sync.tautulli, "get_history", return_value=history), \
         patch.object(sync.tautulli, "get_metadata", return_value=meta):
        sync.sync_tautulli_user_history(USER)

    bd = get_watch_source_breakdown(USER)
    assert bd["plex"] == 1
    assert bd["tautulli"] == 1
    assert bd["total"] == 2


def test_resolve_plex_account_id_owner_is_one():
    class FakePlex:
        def systemAccounts(self):
            return FAKE_SYSTEM_ACCOUNTS
    aid = sync._resolve_plex_account_id(FakePlex(), {"is_admin": True, "username": "alice"})
    assert aid == 1


def test_resolve_plex_account_id_by_username():
    class FakePlex:
        def systemAccounts(self):
            return [FakeAccount(1, "owner"), FakeAccount(77, "bob")]
    aid = sync._resolve_plex_account_id(FakePlex(), {"is_admin": False, "username": "bob"})
    assert aid == 77


def test_resolve_plex_account_id_no_match_returns_none():
    class FakePlex:
        def systemAccounts(self):
            return [FakeAccount(1, "owner")]
    aid = sync._resolve_plex_account_id(FakePlex(), {"is_admin": False, "username": "nobody", "email": "no@x.com"})
    assert aid is None


def test_plex_history_skipped_when_account_unmatched(monkeypatch):
    monkeypatch.setattr(settings, "plex_token", "tok")

    class FakePlex:
        def systemAccounts(self):
            return [FakeAccount(1, "owner")]
        def history(self, maxresults=5000, accountID=None):
            raise AssertionError("history should not be called when unmatched")

    # make USER a non-admin with no matching account name
    create_or_update_user({"user_key": USER, "username": "zzz", "email": "z@x.com", "is_admin": False})
    with patch.object(sync, "get_plex_instance", return_value=FakePlex()):
        assert sync.sync_plex_user_history(USER) == 0


def test_upsert_media_items_batch():
    from plex_recommender.db import get_connection
    from plex_recommender.db.media import upsert_media_items_batch
    items = [
        {"item_id": f"batch_{i}", "media_type": "movie", "title": f"Batch Film {i}", "year": 2020 + i}
        for i in range(10)
    ]
    upsert_media_items_batch(items, batch_size=3)
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM media_items WHERE item_id LIKE 'batch_%'").fetchone()[0]
    conn.close()
    assert count == 10


def test_sync_tautulli_metadata_caching():
    from unittest.mock import MagicMock
    # Multiple episodes from the same show rating_key (999)
    history = [
        {"rating_key": f"ep_{i}", "grandparent_rating_key": "999", "full_title": f"Show - Ep {i}",
         "media_type": "episode", "date": 1700000000 + i, "year": 2020}
        for i in range(5)
    ]
    mock_meta = {"imdb_id": None, "tmdb_id": None, "tvdb_id": None, "title": "Cached Show",
                 "year": 2020, "genres": ["Comedy"], "directors": [], "actors": []}
    mock_get_meta = MagicMock(return_value=mock_meta)

    with patch.object(sync.tautulli, "is_configured", return_value=True), \
         patch.object(sync.tautulli, "monitors_server", return_value=True), \
         patch.object(sync.tautulli, "resolve_user_id", return_value="22"), \
         patch.object(sync.tautulli, "get_history", return_value=history), \
         patch.object(sync.tautulli, "get_metadata", mock_get_meta):
        count = sync.sync_tautulli_user_history(USER)

    assert count == 5
    # Crucial assertion: get_metadata should have been called only ONCE for all 5 episodes due to cache!
    assert mock_get_meta.call_count == 1
