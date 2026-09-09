import sqlite3
from pathlib import Path
import pytest

from plex_recommender.config import settings

PROD_DB_PATH = Path("config/data.db")


def cleanup_alice_from_db(db_path: Path):
    """Ensure a@x.com / Alice / u1 is completely removed from the given database."""
    if not db_path or not Path(db_path).exists():
        return
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
        if not cur.fetchone():
            conn.close()
            return

        cur.execute(
            "SELECT user_key FROM users WHERE email = 'a@x.com' OR username = 'alice' OR title = 'Alice' OR user_key = 'u1'"
        )
        user_keys = {r["user_key"] for r in cur.fetchall()}
        user_keys.add("u1")

        for uk in user_keys:
            for table in ("user_media", "watch_events", "seen_identifiers"):
                try:
                    cur.execute(f"DELETE FROM {table} WHERE user_key = ?", (str(uk),))
                except Exception:
                    pass
            try:
                cur.execute("DELETE FROM recommendations_cache WHERE cache_key LIKE ?", (f"{uk}:%",))
            except Exception:
                pass
            try:
                cur.execute("DELETE FROM users WHERE user_key = ?", (str(uk),))
            except Exception:
                pass

        try:
            cur.execute("DELETE FROM users WHERE email = 'a@x.com' OR username = 'alice' OR title = 'Alice'")
        except Exception:
            pass

        conn.commit()
        conn.close()
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def session_isolate_db(tmp_path_factory):
    """Protect the production database by ensuring settings.db_path defaults to a temp db."""
    test_dir = tmp_path_factory.mktemp("session_db")
    session_db = test_dir / "session.db"
    orig_db = settings.db_path
    settings.db_path = session_db
    yield
    cleanup_alice_from_db(session_db)
    cleanup_alice_from_db(PROD_DB_PATH)
    cleanup_alice_from_db(orig_db)
    settings.db_path = orig_db


@pytest.fixture(autouse=True)
def clean_alice_after_test():
    """Ensure Alice (a@x.com) is purged from all test and production databases after each test."""
    yield
    cleanup_alice_from_db(settings.db_path)
    cleanup_alice_from_db(PROD_DB_PATH)


def pytest_sessionfinish(session, exitstatus):
    """Final safeguard ensuring Alice (a@x.com) is never left in any database after the test session."""
    cleanup_alice_from_db(settings.db_path)
    cleanup_alice_from_db(PROD_DB_PATH)
