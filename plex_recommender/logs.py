import logging
import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from plex_recommender.db import (
    insert_system_log,
    get_system_logs,
    truncate_system_logs,
    clear_all_system_logs,
)


class LogHandler(logging.Handler):
    """
    Mindarr LogHandler.

    Acts as both a standard Python logging.Handler to capture log events into SQLite,
    and a management service for querying, filtering, truncating (7-day retention),
    and auditing system events (settings changes, job runs, dismissals, auth).
    """

    def __init__(self, level: int = logging.INFO):
        super().__init__(level=level)
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord):
        """Persist a LogRecord into the system_logs table."""
        try:
            name = record.name
            # Never recurse on internal database operations
            if name.startswith("sqlite") or name == "plex_recommender.db":
                return
            msg = self.format(record)
            insert_system_log(
                level=record.levelname,
                logger_name=name,
                message=msg,
                epoch=record.created
            )
        except Exception:
            self.handleError(record)

    def get_logs(
        self,
        limit: int = 250,
        level: Optional[str] = None,
        search: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve system logs sorted by most recent timestamp first (newest first)."""
        return get_system_logs(limit=limit, level=level, search=search)

    def truncate(self, days: int = 7) -> int:
        """Purge system logs older than the specified retention window (default 7 days)."""
        return truncate_system_logs(days=days)

    def clear(self) -> None:
        """Purge all system logs."""
        clear_all_system_logs()

    def log_event(
        self,
        level: str,
        logger_name: str,
        message: str,
        user: Optional[str] = None,
        epoch: Optional[float] = None
    ) -> None:
        """
        Record a structured operational or user event directly into the log store.
        If user is provided, prefixes the message with '[User: {user}]'.
        """
        formatted_msg = f"[User: {user}] {message}" if user else message
        insert_system_log(
            level=level.upper(),
            logger_name=logger_name,
            message=formatted_msg,
            epoch=epoch
        )

    def export_text(self, limit: int = 1000) -> str:
        """Export recent logs as plain text lines for download or auditing."""
        logs = self.get_logs(limit=limit)
        lines = []
        for l in logs:
            lines.append(f"[{l.get('timestamp')}] [{l.get('level')}] [{l.get('logger_name')}]: {l.get('message')}")
        return "\n".join(lines)


# Global singleton instance
log_handler = LogHandler()


def setup_logging():
    """Attach LogHandler to the plex_recommender root logger."""
    pkg_logger = logging.getLogger("plex_recommender")
    pkg_logger.setLevel(logging.INFO)
    if not any(isinstance(h, LogHandler) for h in pkg_logger.handlers):
        pkg_logger.addHandler(log_handler)

