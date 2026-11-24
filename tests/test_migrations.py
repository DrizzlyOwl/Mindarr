import sqlite3
import pytest

from plex_recommender.config import settings
from plex_recommender.db import init_db, get_connection
from plex_recommender.db.migrations import (
    _migrate_add_columns,
    _migrate_discard_global,
    _reconcile_watch_events_migration,
    _migrate_scrub_non_admin_tokens,
)


def test_migrate_add_columns_upgrades_legacy_schema(tmp_path):
    legacy_db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy_db))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE watch_events (id INTEGER PRIMARY KEY, event_key TEXT, user_key TEXT, item_id TEXT);
        CREATE TABLE media_items (item_id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE user_media (user_key TEXT, item_id TEXT, PRIMARY KEY (user_key, item_id));
        CREATE TABLE users (user_key TEXT PRIMARY KEY, username TEXT);
    """)

    _migrate_add_columns(conn)
    # Idempotent on second call
    _migrate_add_columns(conn)

    watch_cols = {r[1] for r in conn.execute("PRAGMA table_info(watch_events)").fetchall()}
    media_cols = {r[1] for r in conn.execute("PRAGMA table_info(media_items)").fetchall()}
    um_cols = {r[1] for r in conn.execute("PRAGMA table_info(user_media)").fetchall()}
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    conn.close()

    assert "source" in watch_cols
    assert "keywords" in media_cols
    assert "user_rating" in um_cols
    assert "onboarded_at" in user_cols
    assert "last_seen_at" in user_cols


def test_migrate_discard_global_removes_null_user_rows_only(tmp_path, monkeypatch):
    db_path = tmp_path / "mig_global.db"
    monkeypatch.setattr(settings, "db_path", db_path)
    init_db()

    conn = get_connection()
    conn.execute("INSERT INTO seen_identifiers (user_key, id_type, id_value, title) VALUES (NULL, 'tmdb', '100', 'Old Global')")
    conn.execute("INSERT INTO seen_identifiers (user_key, id_type, id_value, title) VALUES ('u_keep', 'tmdb', '200', 'User Scoped')")
    conn.execute("INSERT INTO watch_events (event_key, user_key, item_id, title) VALUES ('g1', NULL, '1', 'Global Event')")
    conn.execute("INSERT INTO watch_events (event_key, user_key, item_id, title) VALUES ('u1', 'u_keep', '2', 'User Event')")
    conn.execute("INSERT INTO recommendations_cache (cache_key, sync_version, response_json) VALUES ('k1', 'v1', '{}')")
    conn.commit()

    _migrate_discard_global(conn)

    seen_rows = conn.execute("SELECT user_key, id_value FROM seen_identifiers").fetchall()
    event_rows = conn.execute("SELECT user_key, item_id FROM watch_events").fetchall()
    cache_count = conn.execute("SELECT COUNT(*) FROM recommendations_cache").fetchone()[0]
    conn.close()

    assert len(seen_rows) == 1
    assert seen_rows[0]["user_key"] == "u_keep"
    assert len(event_rows) == 1
    assert event_rows[0]["user_key"] == "u_keep"
    assert cache_count == 0


def test_reconcile_watch_events_migration_merges_within_window_and_keeps_distinct(tmp_path, monkeypatch):
    db_path = tmp_path / "mig_reconcile.db"
    monkeypatch.setattr(settings, "db_path", db_path)
    init_db()

    conn = get_connection()
    # Session 1: Plex + Tautulli within 1 hour -> should merge into Plex row with duration & composite title
    conn.execute("""
        INSERT INTO watch_events (event_key, user_key, item_id, title, media_type, viewed_at, duration_watched, source)
        VALUES ('p1', 'u_rec', 'ep_10', 'Pilot', 'episode', '2026-03-01T20:00:00+00:00', 0, 'plex')
    """)
    conn.execute("""
        INSERT INTO watch_events (event_key, user_key, item_id, title, media_type, viewed_at, duration_watched, source)
        VALUES ('t1', 'u_rec', 'ep_10', 'Great Show - Pilot', 'episode', '2026-03-01T20:30:00+00:00', 2700, 'tautulli')
    """)
    # Session 2: Tautulli watch 2 days later (> 4h window) -> must NOT be deleted
    conn.execute("""
        INSERT INTO watch_events (event_key, user_key, item_id, title, media_type, viewed_at, duration_watched, source)
        VALUES ('t2', 'u_rec', 'ep_10', 'Great Show - Pilot', 'episode', '2026-03-03T20:00:00+00:00', 2700, 'tautulli')
    """)
    conn.commit()

    _reconcile_watch_events_migration(conn)

    rows = conn.execute("SELECT event_key, title, duration_watched, source FROM watch_events ORDER BY viewed_at ASC").fetchall()
    conn.close()

    assert len(rows) == 2
    assert rows[0]["event_key"] == "p1"
    assert rows[0]["source"] == "plex"
    assert rows[0]["duration_watched"] == 2700
    assert rows[0]["title"] == "Great Show - Pilot"
    assert rows[1]["event_key"] == "t2"


def test_migrate_scrub_non_admin_tokens_preserves_admin_only(tmp_path, monkeypatch):
    db_path = tmp_path / "mig_tokens.db"
    monkeypatch.setattr(settings, "db_path", db_path)
    init_db()

    conn = get_connection()
    conn.execute("INSERT INTO users (user_key, username, plex_token, is_admin) VALUES ('adm', 'admin', 'adm-tok', 1)")
    conn.execute("INSERT INTO users (user_key, username, plex_token, is_admin) VALUES ('usr', 'bob', 'usr-tok', 0)")
    conn.commit()

    _migrate_scrub_non_admin_tokens(conn)

    adm_tok = conn.execute("SELECT plex_token FROM users WHERE user_key = 'adm'").fetchone()["plex_token"]
    usr_tok = conn.execute("SELECT plex_token FROM users WHERE user_key = 'usr'").fetchone()["plex_token"]
    conn.close()

    assert adm_tok == "adm-tok"
    assert usr_tok is None
