import pytest
from plex_recommender.config import settings
from plex_recommender.db import (
    init_db,
    upsert_user_media,
    upsert_media_item,
    record_watch_event,
    is_seen,
    get_user_seen_index,
    get_user_media_items,
    get_stats,
    normalize_title,
    create_or_update_user,
    delete_user,
    get_user,
    get_watch_events,
    get_top_watched,
    get_connection,
    _migrate_normalize_media_item_ids,
)

USER = "u1"


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_data.db"
    monkeypatch.setattr(settings, "db_path", test_db)
    init_db()
    create_or_update_user({"user_key": USER, "username": "alice", "email": "a@x.com", "is_admin": True})
    yield


def test_normalize_title():
    assert normalize_title("The Matrix (1999)!") == "the matrix 1999"
    assert normalize_title("Spider-Man: Into the Spider-Verse") == "spiderman into the spiderverse"


def test_upsert_and_is_seen():
    item = {
        "item_id": "movie_101",
        "media_type": "movie",
        "title": "Inception",
        "year": 2010,
        "genres": ["Action", "Sci-Fi"],
        "directors": ["Christopher Nolan"],
        "imdb_id": "tt1375666",
        "tmdb_id": "27205",
        "tvdb_id": "121361",
        "view_count": 3
    }
    upsert_user_media(USER, item)

    assert is_seen(USER, imdb_id="tt1375666") is True
    assert is_seen(USER, imdb_id="TT1375666") is True  # Case insensitive
    assert is_seen(USER, imdb_id="tt9999999") is False

    assert is_seen(USER, tmdb_id="27205") is True
    assert is_seen(USER, tmdb_id=27205) is True

    assert is_seen(USER, tvdb_id="121361") is True

    assert is_seen(USER, title="Inception", year=2010) is True
    assert is_seen(USER, title="Inception") is True
    assert is_seen(USER, title="Interstellar", year=2014) is False


def test_seen_index_is_per_user():
    item = {
        "item_id": "movie_102",
        "media_type": "movie",
        "title": "Blade Runner 2049",
        "year": 2017,
        "imdb_id": "tt1856101",
        "tmdb_id": "335984"
    }
    upsert_user_media(USER, item)

    seen = get_user_seen_index(USER)
    assert "tt1856101" in seen["imdb"]
    assert "335984" in seen["tmdb"]

    # A different user has not seen it
    create_or_update_user({"user_key": "u2", "username": "bob", "email": "b@x.com"})
    other = get_user_seen_index("u2")
    assert "tt1856101" not in other["imdb"]
    assert is_seen("u2", imdb_id="tt1856101") is False


def test_keywords_stored_and_preserved_on_empty():
    item = {
        "item_id": "movie_200",
        "media_type": "movie",
        "title": "Primer",
        "year": 2004,
        "tmdb_id": "14337",
        "keywords": [{"id": 1, "name": "time travel"}],
    }
    upsert_user_media(USER, item)
    loaded = {i["item_id"]: i for i in get_user_media_items(USER)}["movie_200"]
    assert loaded["keywords"] == [{"id": 1, "name": "time travel"}]

    # A later upsert with empty keywords must not wipe existing ones.
    upsert_media_item({**item, "keywords": []})
    loaded = {i["item_id"]: i for i in get_user_media_items(USER)}["movie_200"]
    assert loaded["keywords"] == [{"id": 1, "name": "time travel"}]


def test_per_user_rating_override_and_preserve():
    item = {
        "item_id": "movie_201",
        "media_type": "movie",
        "title": "Arrival",
        "year": 2016,
        "tmdb_id": "329865",
        "audience_rating": 7.0,
        "user_rating": 9.5,
    }
    upsert_user_media(USER, item)
    loaded = {i["item_id"]: i for i in get_user_media_items(USER)}["movie_201"]
    # Per-user rating is authoritative over shared media rating.
    assert loaded["user_rating"] == 9.5

    # A subsequent history sync without a rating must not clobber it.
    upsert_user_media(USER, {**item, "user_rating": None})
    loaded = {i["item_id"]: i for i in get_user_media_items(USER)}["movie_201"]
    assert loaded["user_rating"] == 9.5


def test_get_top_watched_orders_and_filters():
    upsert_user_media(USER, {
        "item_id": "movie_301", "media_type": "movie", "title": "Low Watch",
        "year": 2015, "tmdb_id": "1", "view_count": 1
    })
    upsert_user_media(USER, {
        "item_id": "movie_302", "media_type": "movie", "title": "High Watch",
        "year": 2018, "tmdb_id": "2", "view_count": 5
    })
    upsert_user_media(USER, {
        "item_id": "show_301", "media_type": "show", "title": "Binged Show",
        "year": 2019, "tmdb_id": "4", "view_count": 42
    })

    top_movies = get_top_watched(USER, "movie", limit=10)
    assert [m["item_id"] for m in top_movies] == ["movie_302", "movie_301"]
    assert all(m["view_count"] > 0 for m in top_movies)

    top_movies_limited = get_top_watched(USER, "movie", limit=1)
    assert len(top_movies_limited) == 1
    assert top_movies_limited[0]["item_id"] == "movie_302"

    top_shows = get_top_watched(USER, "show", limit=10)
    assert len(top_shows) == 1
    assert top_shows[0]["item_id"] == "show_301"
    assert top_shows[0]["view_count"] == 42

    # Items with zero view_count are excluded from the ranking entirely.
    assert get_top_watched("no_such_user", "movie", limit=10) == []


def test_migration_merges_prefixed_row_into_existing_bare_twin():
    """Regression test: library-scan-prefixed media_items rows (movie_/show_/episode_)
    must merge into their bare-ratingKey twin used by user_media, not stay split."""
    conn = get_connection()
    cur = conn.cursor()
    # Simulate the pre-fix state: a rich library-scanned row (prefixed) and a
    # thinner history-sync row (bare) for the same physical movie.
    upsert_media_item({
        "item_id": "movie_500",
        "media_type": "movie",
        "title": "Rich Title",
        "year": 2020,
        "genres": ["Action", "Sci-Fi"],
        "directors": ["Some Director"],
        "actors": ["Some Actor"],
        "imdb_id": "tt5000000",
        "tmdb_id": "5000",
        "view_count": 42,
    })
    upsert_media_item({
        "item_id": "500",
        "media_type": "movie",
        "title": "Rich Title",
        "year": 2020,
        "genres": [],
        "directors": [],
        "actors": [],
        "imdb_id": None,
        "tmdb_id": None,
        "view_count": 3,
    })
    conn.close()

    conn = get_connection()
    _migrate_normalize_media_item_ids(conn)
    conn.close()

    conn = get_connection()
    rows = conn.execute("SELECT * FROM media_items WHERE item_id IN ('500', 'movie_500')").fetchall()
    conn.close()

    assert len(rows) == 1
    merged = dict(rows[0])
    assert merged["item_id"] == "500"
    assert merged["view_count"] == 42  # MAX of both
    assert "Action" in merged["genres"]  # richer metadata preserved
    assert merged["imdb_id"] == "tt5000000"


def test_migration_renames_prefixed_row_with_no_bare_twin():
    """Episode rows (and unwatched library items) have no bare twin; they must be
    renamed in place, not lost."""
    conn = get_connection()
    upsert_media_item({
        "item_id": "episode_777",
        "media_type": "episode",
        "title": "Some Episode",
        "year": 2021,
        "genres": [],
        "view_count": 0,
    })
    conn.close()

    conn = get_connection()
    _migrate_normalize_media_item_ids(conn)
    conn.close()

    conn = get_connection()
    rows = conn.execute("SELECT * FROM media_items WHERE item_id IN ('777', 'episode_777')").fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["item_id"] == "777"
    assert rows[0]["title"] == "Some Episode"


def test_migration_is_idempotent():
    conn = get_connection()
    upsert_media_item({"item_id": "show_900", "media_type": "show", "title": "Idempotent Show", "view_count": 5})
    conn.close()

    conn = get_connection()
    _migrate_normalize_media_item_ids(conn)
    conn.close()

    # Running it again should be a safe no-op (nothing left to migrate).
    conn = get_connection()
    _migrate_normalize_media_item_ids(conn)
    conn.close()

    conn = get_connection()
    rows = conn.execute("SELECT * FROM media_items WHERE item_id IN ('900', 'show_900')").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["item_id"] == "900"


def test_migration_preserves_user_media_join_after_merge():
    """The per-user watch data (always bare-keyed) must still resolve correctly
    against the merged media_items row after migration."""
    upsert_media_item({
        "item_id": "movie_600",
        "media_type": "movie",
        "title": "Joined Movie",
        "year": 2019,
        "genres": ["Drama"],
        "view_count": 10,
    })
    upsert_user_media(USER, {"item_id": "600", "media_type": "movie", "title": "Joined Movie", "view_count": 4})

    conn = get_connection()
    _migrate_normalize_media_item_ids(conn)
    conn.close()

    items = {i["item_id"]: i for i in get_user_media_items(USER)}
    assert "600" in items
    assert items["600"]["genres"] == ["Drama"]
    assert items["600"]["view_count"] == 4  # per-user tally, unaffected by merge


def test_stats_reporting():
    upsert_user_media(USER, {
        "item_id": "movie_1",
        "media_type": "movie",
        "title": "Dune",
        "year": 2021
    })
    upsert_user_media(USER, {
        "item_id": "show_1",
        "media_type": "show",
        "title": "Severance",
        "year": 2022
    })
    record_watch_event({
        "user_key": USER,
        "item_id": "movie_1",
        "title": "Dune",
        "media_type": "movie",
        "viewed_at": "2026-01-01T12:00:00Z"
    })

    stats = get_stats(USER)
    assert stats["movies_count"] == 1
    assert stats["shows_count"] == 1
    assert stats["events_count"] == 1
    assert stats["seen_count"] >= 2


def test_upsert_preserves_genres_when_incoming_empty():
    from plex_recommender.db import get_user_media_items
    # First upsert with genres (e.g. from a library scan / show metadata)
    upsert_user_media(USER, {
        "item_id": "show_1", "media_type": "show", "title": "Show",
        "year": 2015, "genres": ["Comedy", "Drama"], "tmdb_id": "111",
    })
    # Later rollup upsert where metadata fetch returned no genres
    upsert_user_media(USER, {
        "item_id": "show_1", "media_type": "show", "title": "Show",
        "year": 2015, "genres": [], "tmdb_id": "111",
    })
    item = [i for i in get_user_media_items(USER) if i["item_id"] == "show_1"][0]
    assert set(item["genres"]) == {"Comedy", "Drama"}


def test_database_stats_and_clear_user_data():
    from plex_recommender.db import (
        get_database_stats, get_user_data_summary, clear_user_data,
        record_watch_event, has_user_history,
    )
    upsert_user_media(USER, {
        "item_id": "s1", "media_type": "show", "title": "Show",
        "year": 2021, "genres": ["Comedy"], "tmdb_id": "111",
    })
    record_watch_event({"user_key": USER, "item_id": "s1", "viewed_at": "2026-01-01T00:00:00", "source": "plex"})

    stats = get_database_stats()
    assert stats["size_bytes"] > 0
    assert stats["tables"]["user_media"] >= 1
    assert stats["total_rows"] >= 1

    summary = get_user_data_summary()
    me = [u for u in summary if u["user_key"] == USER][0]
    assert me["media_count"] >= 1
    assert me["event_count"] >= 1
    assert me["seen_count"] >= 1

    deleted = clear_user_data(USER)
    assert deleted["user_media"] >= 1
    assert has_user_history(USER) is False


def test_watch_event_reconciliation_plex_primary():
    from plex_recommender.db import get_watch_events

    # 1. Ingest Plex event (short title, 0 duration, end time)
    record_watch_event({
        "user_key": USER,
        "item_id": "41426",
        "title": "A Tale of Two Bandits",
        "media_type": "episode",
        "viewed_at": "2026-09-06T20:55:18",
        "duration_watched": 0,
        "source": "plex",
    })

    # 2. Ingest Tautulli event for same session (~19 mins earlier, full title, real duration)
    record_watch_event({
        "user_key": USER,
        "item_id": "41426",
        "title": "Brooklyn Nine-Nine - A Tale of Two Bandits",
        "media_type": "episode",
        "viewed_at": "2026-09-06T19:36:05+00:00",
        "duration_watched": 1276,
        "source": "tautulli",
    })

    events = get_watch_events(USER)
    # Exactly one reconciled event should exist
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "plex"  # Plex Primary
    assert ev["title"] == "Brooklyn Nine-Nine - A Tale of Two Bandits"  # Full title enriched
    assert ev["duration_watched"] == 1276  # Duration enriched


def test_watch_event_distinct_sessions_not_deduped():
    from plex_recommender.db import get_watch_events

    # Ingest watch session on day 1
    record_watch_event({
        "user_key": USER,
        "item_id": "100",
        "title": "Movie Rewatch",
        "media_type": "movie",
        "viewed_at": "2026-09-01T20:00:00",
        "duration_watched": 7200,
        "source": "plex",
    })

    # Ingest rewatch session 5 days later
    record_watch_event({
        "user_key": USER,
        "item_id": "100",
        "title": "Movie Rewatch",
        "media_type": "movie",
        "viewed_at": "2026-09-06T20:00:00",
        "duration_watched": 7200,
        "source": "plex",
    })

    events = get_watch_events(USER)
    assert len(events) == 2


def test_delete_user_cleans_all_records():
    # Given user with watch events, media, and seen identifiers
    upsert_user_media(USER, {
        "item_id": "movie_del",
        "media_type": "movie",
        "title": "To Delete",
        "year": 2020,
    })
    record_watch_event({
        "user_key": USER,
        "item_id": "movie_del",
        "title": "To Delete",
        "media_type": "movie",
        "viewed_at": "2026-09-01T20:00:00",
        "source": "plex",
    })
    assert get_user(USER) is not None
    assert len(get_user_media_items(USER)) > 0
    assert len(get_watch_events(USER)) > 0

    res = delete_user(USER)
    assert res["users"] == 1
    assert get_user(USER) is None
    assert len(get_user_media_items(USER)) == 0
    assert len(get_watch_events(USER)) == 0


def test_user_dismissals_crud():
    from plex_recommender.db import dismiss_item, undismiss_item, get_user_dismissals

    dismiss_item(USER, tmdb_id="12345", media_type="movie", title="The Matrix", year=1999, reason="already_watched")
    dismiss_item(USER, tmdb_id="67890", media_type="show", title="Bad Show", year=2020, reason="not_interested")

    items = get_user_dismissals(USER)
    assert len(items) == 2
    assert items[0]["tmdb_id"] == "67890"  # newest first
    assert items[0]["reason"] == "not_interested"
    assert items[1]["tmdb_id"] == "12345"

    undismiss_item(USER, tmdb_id="12345")
    items_after = get_user_dismissals(USER)
    assert len(items_after) == 1
    assert items_after[0]["tmdb_id"] == "67890"


def test_system_logs_retention_and_truncation():
    import logging
    import time
    from plex_recommender.db import (
        insert_system_log,
        get_system_logs,
        truncate_system_logs,
        clear_all_system_logs,
        SQLiteLogHandler
    )

    now = time.time()
    ten_days_ago = now - (10 * 86400)
    eight_days_ago = now - (8 * 86400)
    three_days_ago = now - (3 * 86400)

    # Insert test logs at different timestamps
    insert_system_log(level="INFO", logger_name="test.sync", message="10 days old log", epoch=ten_days_ago)
    insert_system_log(level="WARNING", logger_name="test.recs", message="8 days old warning", epoch=eight_days_ago)
    insert_system_log(level="ERROR", logger_name="test.overseerr", message="3 days old error", epoch=three_days_ago)
    insert_system_log(level="INFO", logger_name="test.web", message="Fresh info log", epoch=now)

    # Verify all 4 logs exist, ordered newest first
    all_logs = get_system_logs(limit=10)
    assert len(all_logs) == 4
    assert all_logs[0]["message"] == "Fresh info log"
    assert all_logs[1]["message"] == "3 days old error"
    assert all_logs[2]["message"] == "8 days old warning"
    assert all_logs[3]["message"] == "10 days old log"

    # Filter by level
    errors = get_system_logs(level="ERROR")
    assert len(errors) == 1
    assert errors[0]["message"] == "3 days old error"

    # Filter by search
    searched = get_system_logs(search="warning")
    assert len(searched) == 1
    assert searched[0]["message"] == "8 days old warning"

    # Truncate logs older than 7 days
    pruned = truncate_system_logs(days=7)
    assert pruned == 2

    # Verify only records <= 7 days remain
    remaining = get_system_logs(limit=10)
    assert len(remaining) == 2
    assert [r["message"] for r in remaining] == ["Fresh info log", "3 days old error"]

    # Test SQLiteLogHandler
    logger = logging.getLogger("test_sqlite_handler")
    handler = SQLiteLogHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info("Log emitted via handler")

    handled = get_system_logs(search="emitted via handler")
    assert len(handled) == 1
    assert handled[0]["level"] == "INFO"

    # Test clear all
    clear_all_system_logs()
    assert len(get_system_logs()) == 0


def test_user_votes_crud_and_migration():
    from plex_recommender.db import (
        record_vote,
        remove_vote,
        get_user_votes,
        get_user_vote_items,
        dismiss_item,
        get_connection,
        _migrate_hidden_cards_to_votes,
        clear_user_data,
        is_seen
    )

    uk = "vote_test_user"
    clear_user_data(uk)

    # 1. Upvote (+1)
    record_vote(uk, tmdb_id="101", media_type="movie", title="Interstellar", year=2014, vote=1)
    votes = get_user_votes(uk)
    assert votes.get("101") == 1
    items = get_user_vote_items(uk)
    assert len(items) == 1
    assert items[0]["title"] == "Interstellar"
    assert items[0]["vote"] == 1

    # 2. Downvote (-1)
    record_vote(uk, tmdb_id="102", media_type="show", title="Terrible Show", year=2022, vote=-1)
    votes = get_user_votes(uk)
    assert votes.get("102") == -1
    # Downvote also excludes in seen_identifiers
    assert is_seen(uk, tmdb_id="102") is True

    # 3. Filter by vote polarity
    up_only = get_user_vote_items(uk, vote=1)
    down_only = get_user_vote_items(uk, vote=-1)
    assert len(up_only) == 1
    assert up_only[0]["tmdb_id"] == "101"
    assert len(down_only) == 1
    assert down_only[0]["tmdb_id"] == "102"

    # 4. Remove vote
    remove_vote(uk, "102")
    votes_after = get_user_votes(uk)
    assert "102" not in votes_after
    assert is_seen(uk, tmdb_id="102") is False

    # 5. Test Option B migration from legacy 'not_interested' dismissals
    dismiss_item(uk, tmdb_id="999", media_type="movie", title="Legacy Dismissed", year=2018, reason="not_interested")
    conn = get_connection()
    # Force delete from user_votes to simulate legacy database pre-migration
    conn.execute("DELETE FROM user_votes WHERE user_key = ? AND tmdb_id = '999'", (uk,))
    conn.commit()

    _migrate_hidden_cards_to_votes(conn)
    conn.close()

    migrated_votes = get_user_votes(uk)
    assert migrated_votes.get("999") == -1

    # 6. Clear user data clears votes
    res = clear_user_data(uk)
    assert res.get("user_votes", 0) >= 1
    assert len(get_user_votes(uk)) == 0


def test_get_user_action_counts():
    from plex_recommender.db import (
        get_user_action_counts,
        record_vote,
        dismiss_item,
        clear_user_data
    )

    uk = "action_counts_user"
    clear_user_data(uk)

    initial_counts = get_user_action_counts(uk)
    assert initial_counts["dismissals_count"] == 0
    assert initial_counts["upvotes_count"] == 0
    assert initial_counts["downvotes_count"] == 0
    assert initial_counts["seen_count"] == 0

    # 1 dismissal
    dismiss_item(uk, tmdb_id="111", media_type="movie", title="Dismissed Movie", year=2020, reason="already_watched")
    # 1 upvote
    record_vote(uk, tmdb_id="222", media_type="movie", title="Upvoted Movie", year=2021, vote=1)
    # 1 downvote (which also creates a dismissal and seen_identifier)
    record_vote(uk, tmdb_id="333", media_type="show", title="Downvoted Show", year=2022, vote=-1)

    counts = get_user_action_counts(uk)
    assert counts["dismissals_count"] == 2
    assert counts["upvotes_count"] == 1
    assert counts["downvotes_count"] == 1
    assert counts["seen_count"] >= 2

    clear_user_data(uk)
    cleared = get_user_action_counts(uk)
    assert cleared["dismissals_count"] == 0
    assert cleared["upvotes_count"] == 0
    assert cleared["downvotes_count"] == 0
    assert cleared["seen_count"] == 0




