"""job_history table: background job start/finish tracking."""

from datetime import datetime
from typing import Optional, Dict, Any, List

from plex_recommender.db import get_connection


def start_job(job_type: str, trigger: str, user_key: Optional[str] = None) -> int:
    """Record the start of a background job. Returns the job id."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO job_history (job_type, trigger, user_key, status, started_at)
        VALUES (?, ?, ?, 'running', ?)
        """,
        (job_type, trigger, user_key, datetime.now().isoformat()),
    )
    job_id = cur.lastrowid
    conn.commit()
    conn.close()
    return job_id


def finish_job(job_id: int, status: str, detail: Optional[str] = None):
    """Mark a job finished, computing its duration from started_at."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT started_at FROM job_history WHERE id = ?", (job_id,))
    row = cur.fetchone()
    finished = datetime.now()
    duration_ms = None
    if row and row["started_at"]:
        try:
            started = datetime.fromisoformat(row["started_at"])
            duration_ms = int((finished - started).total_seconds() * 1000)
        except (ValueError, TypeError):
            duration_ms = None
    cur.execute(
        """
        UPDATE job_history
        SET status = ?, detail = ?, finished_at = ?, duration_ms = ?
        WHERE id = ?
        """,
        (status, (detail or "")[:2000], finished.isoformat(), duration_ms, job_id),
    )
    conn.commit()
    conn.close()


def get_job_history(limit: int = 100) -> List[Dict[str, Any]]:
    """Return recent job history, most recent first."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM job_history ORDER BY started_at DESC, id DESC LIMIT ?",
        (int(limit),),
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_job_history():
    """Delete all job history records."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM job_history")
    conn.commit()
    conn.close()
