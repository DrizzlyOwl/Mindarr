"""Database package: connection factory + schema initialization.

Data-access functions live in the sibling submodules (media, watch, users,
recommendations, engagement, jobs, logs); import from those directly, e.g.
`from plex_recommender.db.watch import get_user_media_items`.
"""

import sqlite3
from plex_recommender.config import settings
from plex_recommender.db import migrations


def get_connection() -> sqlite3.Connection:
    """Returns SQLite connection with dict-like row factory."""
    db_path = settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize database tables and indexes."""
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS watch_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT UNIQUE,
        user_key TEXT,
        item_id TEXT,
        title TEXT,
        media_type TEXT,
        viewed_at TIMESTAMP,
        account_id TEXT,
        duration_watched INTEGER,
        source TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_key TEXT PRIMARY KEY,
        plex_uuid TEXT,
        username TEXT,
        email TEXT,
        title TEXT,
        thumb TEXT,
        plex_token TEXT,
        is_admin INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_login TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS media_items (
        item_id TEXT PRIMARY KEY,
        media_type TEXT,
        title TEXT,
        year INTEGER,
        release_date TEXT,
        genres TEXT,          -- JSON list of strings
        directors TEXT,       -- JSON list of strings
        writers TEXT,         -- JSON list of strings
        actors TEXT,          -- JSON list of strings
        summary TEXT,
        user_rating REAL,
        audience_rating REAL,
        critic_rating REAL,
        imdb_id TEXT,
        tmdb_id TEXT,
        tvdb_id TEXT,
        view_count INTEGER DEFAULT 0,
        last_viewed_at TIMESTAMP,
        raw_guids TEXT        -- JSON list of guid strings
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_media (
        user_key TEXT,
        item_id TEXT,
        view_count INTEGER DEFAULT 1,
        last_viewed_at TIMESTAMP,
        PRIMARY KEY (user_key, item_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS seen_identifiers (
        user_key TEXT,
        id_type TEXT,          -- 'imdb', 'tmdb', 'tvdb', 'title_year'
        id_value TEXT,
        title TEXT,
        year INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_key, id_type, id_value)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS recommendations_cache (
        cache_key TEXT PRIMARY KEY,
        sync_version TEXT,
        cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        response_json TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS job_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_type TEXT,           -- e.g. 'manual_sync', 'scheduled_sync'
        trigger TEXT,            -- 'manual' or 'scheduled'
        user_key TEXT,           -- who/what the job ran for (nullable)
        status TEXT,             -- 'running', 'success', 'failed'
        detail TEXT,             -- summary or error message
        started_at TIMESTAMP,
        finished_at TIMESTAMP,
        duration_ms INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_dismissals (
        user_key TEXT,
        tmdb_id TEXT,
        media_type TEXT,
        title TEXT,
        year INTEGER,
        reason TEXT,           -- 'already_watched' or 'not_interested'
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_key, tmdb_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_votes (
        user_key TEXT,
        tmdb_id TEXT,
        media_type TEXT,
        title TEXT,
        year INTEGER,
        vote INTEGER NOT NULL,          -- +1 for Thumbs Up, -1 for Thumbs Down
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_key, tmdb_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS system_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        epoch REAL,
        level TEXT,
        logger_name TEXT,
        message TEXT
    )
    """)

    # Indexes for fast lookup
    cur.execute("CREATE INDEX IF NOT EXISTS idx_watch_viewed_at ON watch_events(viewed_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_watch_user ON watch_events(user_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_media_type ON media_items(media_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_seen_lookup ON seen_identifiers(user_key, id_type, id_value)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_media ON user_media(user_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rec_cache_sync ON recommendations_cache(sync_version)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_job_started ON job_history(started_at DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_dismissals ON user_dismissals(user_key)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_votes ON user_votes(user_key, vote)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_epoch ON system_logs(epoch DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_level ON system_logs(level)")

    conn.commit()

    migrations.run_all(conn)

    conn.close()
