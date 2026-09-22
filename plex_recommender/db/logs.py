"""system_logs table: DB-backed application log storage + logging.Handler."""

import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from plex_recommender.db import get_connection


def insert_system_log(level: str, logger_name: str, message: str, epoch: Optional[float] = None):
    """Insert a log entry into system_logs table."""
    if epoch is None:
        epoch = datetime.now(timezone.utc).timestamp()
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO system_logs (timestamp, epoch, level, logger_name, message)
        VALUES (CURRENT_TIMESTAMP, ?, ?, ?, ?)
        """, (epoch, level, logger_name, message))
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_system_logs(
    limit: int = 250,
    level: Optional[str] = None,
    search: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Retrieve system logs sorted by most recent timestamp first (epoch DESC)."""
    conn = get_connection()
    cur = conn.cursor()

    query = "SELECT id, timestamp, epoch, level, logger_name, message FROM system_logs WHERE 1=1"
    params: List[Any] = []

    if level and level.upper() != "ALL":
        query += " AND level = ?"
        params.append(level.upper())

    if search and search.strip():
        query += " AND (message LIKE ? OR logger_name LIKE ?)"
        term = f"%{search.strip()}%"
        params.extend([term, term])

    query += " ORDER BY epoch DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))

    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def truncate_system_logs(days: int = 7) -> int:
    """Delete logs older than retention days (default 7 days). Returns number of pruned rows."""
    cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM system_logs WHERE epoch < ?", (cutoff,))
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count


def clear_all_system_logs():
    """Wipe all system logs."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM system_logs")
    conn.commit()
    conn.close()


class SQLiteLogHandler(logging.Handler):
    """Thread-safe logging handler that persists log records into the system_logs SQLite table."""

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            epoch = record.created
            level = record.levelname
            name = record.name
            if name.startswith("sqlite") or name.startswith("plex_recommender.db"):
                return
            insert_system_log(level=level, logger_name=name, message=msg, epoch=epoch)
        except Exception:
            self.handleError(record)


# Export alias
LogHandler = SQLiteLogHandler
