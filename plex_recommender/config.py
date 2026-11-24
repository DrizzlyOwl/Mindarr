import os
import re
import secrets
import threading
from pathlib import Path
from typing import Optional, Dict
from dotenv import load_dotenv

_env_lock = threading.Lock()
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sanitize_env_value(value: str) -> str:
    """Strip CR, LF, and NUL bytes to prevent .env key/line injection."""
    return str(value).replace("\r", "").replace("\n", "").replace("\x00", "")

def get_config_dir() -> Path:
    """Determine config directory, preferring /config (Docker) or ./config (local)."""
    env_config = os.getenv("CONFIG_DIR")
    if env_config:
        p = Path(env_config)
    elif Path("/config").exists() and os.access("/config", os.W_OK):
        p = Path("/config")
    else:
        # Default to local ./config
        p = Path(__file__).resolve().parent.parent / "config"
    p.mkdir(parents=True, exist_ok=True)
    return p

# Snapshot which keys were provided by the real OS environment (e.g. Docker
# `environment:` / `-e` vars) BEFORE loading the .env file. Any key present here
# is considered externally managed and should be locked in the UI. A key is only
# treated as env-managed if it has a non-empty value (empty compose vars like
# `TAUTULLI_URL=` are placeholders and should remain editable).
ENV_MANAGED_KEYS = frozenset(k for k, v in os.environ.items() if v.strip())
OS_ENV_SNAPSHOT = {k: os.environ[k] for k in ENV_MANAGED_KEYS}

# Load environment from config directory if exists
CONFIG_DIR = get_config_dir()
ENV_FILE = CONFIG_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(dotenv_path=ENV_FILE)
else:
    load_dotenv()
# Ensure OS environment variables take precedence over .env file
for k in ENV_MANAGED_KEYS:
    if k in OS_ENV_SNAPSHOT:
        os.environ[k] = OS_ENV_SNAPSHOT[k]


def is_env_managed(key: str) -> bool:
    """Return True if a setting was supplied via the OS environment (e.g. Docker)."""
    return key in ENV_MANAGED_KEYS


def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    """Parse a truthy env string ('1', 'true', 'yes', 'on') into a bool."""
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")

class Settings:
    def __init__(self):
        self.config_dir: Path = get_config_dir()
        self.db_path: Path = Path(os.getenv("DB_PATH") or str(self.config_dir / "data.db"))
        self.plex_url: str = (os.getenv("PLEX_URL") or "").rstrip("/")
        self.plex_token: Optional[str] = os.getenv("PLEX_TOKEN") or None
        self.tmdb_api_key: Optional[str] = os.getenv("TMDB_API_KEY") or None
        self.overseerr_url: str = (os.getenv("OVERSEERR_URL") or "").rstrip("/")
        self.overseerr_api_key: Optional[str] = os.getenv("OVERSEERR_API_KEY") or None
        self.tautulli_url: str = (os.getenv("TAUTULLI_URL") or "").rstrip("/")
        self.tautulli_api_key: Optional[str] = os.getenv("TAUTULLI_API_KEY") or None
        self.plex_machine_id: Optional[str] = os.getenv("PLEX_MACHINE_ID") or None
        self.session_secret: str = self._ensure_session_secret()
        self.session_https_only: bool = _parse_bool(os.getenv("SESSION_HTTPS_ONLY"), default=False)
        self.host: str = os.getenv("HOST") or "0.0.0.0"
        self.port: int = int(os.getenv("PORT") or "8080")
        self.auto_sync_hours: int = int(os.getenv("AUTO_SYNC_HOURS") or "24")
        self.auto_recommendations_hours: int = int(os.getenv("AUTO_RECOMMENDATIONS_HOURS") or "24")
        self.healthcheck_interval_hours: int = int(os.getenv("HEALTHCHECK_INTERVAL_HOURS") or "1")

    def _ensure_session_secret(self) -> str:
        """Return a persistent session secret, generating one on first run."""
        secret = os.getenv("SESSION_SECRET")
        if not secret:
            secret = secrets.token_urlsafe(48)
            self.save_setting("SESSION_SECRET", secret)
        return secret

    def reload(self):
        """Reload configuration from disk."""
        if ENV_FILE.exists():
            load_dotenv(dotenv_path=ENV_FILE, override=True)
        for k in ENV_MANAGED_KEYS:
            if k in OS_ENV_SNAPSHOT:
                os.environ[k] = OS_ENV_SNAPSHOT[k]
        self.plex_url = (os.getenv("PLEX_URL") or "").rstrip("/")
        self.plex_token = os.getenv("PLEX_TOKEN") or None
        self.tmdb_api_key = os.getenv("TMDB_API_KEY") or None
        self.overseerr_url = (os.getenv("OVERSEERR_URL") or "").rstrip("/")
        self.overseerr_api_key = os.getenv("OVERSEERR_API_KEY") or None
        self.tautulli_url = (os.getenv("TAUTULLI_URL") or "").rstrip("/")
        self.tautulli_api_key = os.getenv("TAUTULLI_API_KEY") or None
        self.plex_machine_id = os.getenv("PLEX_MACHINE_ID") or None
        self.session_https_only = _parse_bool(os.getenv("SESSION_HTTPS_ONLY"), default=False)
        self.auto_sync_hours = int(os.getenv("AUTO_SYNC_HOURS") or "24")
        self.auto_recommendations_hours = int(os.getenv("AUTO_RECOMMENDATIONS_HOURS") or "24")
        self.healthcheck_interval_hours = int(os.getenv("HEALTHCHECK_INTERVAL_HOURS") or "1")
        self.db_path = Path(os.getenv("DB_PATH") or str(self.config_dir / "data.db"))

    def is_locked(self, key: str) -> bool:
        """True if this setting is managed by an OS/Docker env var (not editable)."""
        return is_env_managed(key)

    def save_settings_batch(self, updates: Dict[str, str]) -> None:
        """Persist multiple key/value pairs to config .env in a single atomic write + reload.

        No-op for keys locked by the OS environment (Docker-managed).
        """
        cleaned: Dict[str, str] = {}
        for key, value in updates.items():
            if not _ENV_KEY_RE.match(key):
                raise ValueError(f"Invalid setting key: {key!r}")
            if self.is_locked(key):
                continue
            cleaned[key] = _sanitize_env_value(value)

        if not cleaned:
            return

        with _env_lock:
            for key, val in cleaned.items():
                os.environ[key] = val

            lines = []
            if ENV_FILE.exists():
                with open(ENV_FILE, "r") as f:
                    lines = f.readlines()

            seen_keys = set()
            new_lines = []
            for line in lines:
                stripped = line.strip()
                matched_key = None
                for key in cleaned:
                    if stripped.startswith(f"{key}="):
                        matched_key = key
                        break
                if matched_key is not None:
                    if matched_key not in seen_keys:
                        new_lines.append(f"{matched_key}={cleaned[matched_key]}\n")
                        seen_keys.add(matched_key)
                else:
                    new_lines.append(line)

            for key, val in cleaned.items():
                if key not in seen_keys:
                    new_lines.append(f"{key}={val}\n")

            with open(ENV_FILE, "w") as f:
                f.writelines(new_lines)
            try:
                os.chmod(ENV_FILE, 0o600)
            except OSError:
                pass

            self.reload()

    def save_setting(self, key: str, value: str):
        """Save key/value pair to config .env file.

        No-op for keys locked by the OS environment (Docker-managed).
        """
        self.save_settings_batch({key: value})

settings = Settings()
