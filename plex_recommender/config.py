import os
import secrets
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

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

# Load environment from config directory if exists
CONFIG_DIR = get_config_dir()
ENV_FILE = CONFIG_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(dotenv_path=ENV_FILE)
else:
    load_dotenv()


def is_env_managed(key: str) -> bool:
    """Return True if a setting was supplied via the OS environment (e.g. Docker)."""
    return key in ENV_MANAGED_KEYS

class Settings:
    def __init__(self):
        self.config_dir: Path = get_config_dir()
        self.db_path: Path = Path(os.getenv("DB_PATH", str(self.config_dir / "data.db")))
        self.plex_url: str = os.getenv("PLEX_URL", "https://10.255.10.30:32400").rstrip("/")
        self.plex_token: Optional[str] = os.getenv("PLEX_TOKEN")
        self.tmdb_api_key: Optional[str] = os.getenv("TMDB_API_KEY")
        self.overseerr_url: str = os.getenv("OVERSEERR_URL", "http://10.255.10.30:5055").rstrip("/")
        self.overseerr_api_key: Optional[str] = os.getenv("OVERSEERR_API_KEY")
        self.tautulli_url: str = os.getenv("TAUTULLI_URL", "").rstrip("/")
        self.tautulli_api_key: Optional[str] = os.getenv("TAUTULLI_API_KEY")
        self.plex_machine_id: Optional[str] = os.getenv("PLEX_MACHINE_ID")
        self.session_secret: str = self._ensure_session_secret()
        self.host: str = os.getenv("HOST", "0.0.0.0")
        self.port: int = int(os.getenv("PORT", "8080"))
        self.auto_sync_hours: int = int(os.getenv("AUTO_SYNC_HOURS", "24"))
        self.auto_recommendations_hours: int = int(os.getenv("AUTO_RECOMMENDATIONS_HOURS", "24"))

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
        self.plex_url = os.getenv("PLEX_URL", "https://10.255.10.30:32400").rstrip("/")
        self.plex_token = os.getenv("PLEX_TOKEN")
        self.tmdb_api_key = os.getenv("TMDB_API_KEY")
        self.overseerr_url = os.getenv("OVERSEERR_URL", "http://10.255.10.30:5055").rstrip("/")
        self.overseerr_api_key = os.getenv("OVERSEERR_API_KEY")
        self.tautulli_url = os.getenv("TAUTULLI_URL", "").rstrip("/")
        self.tautulli_api_key = os.getenv("TAUTULLI_API_KEY")
        self.plex_machine_id = os.getenv("PLEX_MACHINE_ID")
        self.auto_sync_hours = int(os.getenv("AUTO_SYNC_HOURS", "24"))
        self.auto_recommendations_hours = int(os.getenv("AUTO_RECOMMENDATIONS_HOURS", "24"))
        self.db_path = Path(os.getenv("DB_PATH", str(self.config_dir / "data.db")))

    def is_locked(self, key: str) -> bool:
        """True if this setting is managed by an OS/Docker env var (not editable)."""
        return is_env_managed(key)

    def save_setting(self, key: str, value: str):
        """Save key/value pair to config .env file.

        No-op for keys locked by the OS environment (Docker-managed).
        """
        if self.is_locked(key):
            return
        os.environ[key] = value
        lines = []
        if ENV_FILE.exists():
            with open(ENV_FILE, "r") as f:
                lines = f.readlines()

        key_found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(f"{key}="):
                new_lines.append(f"{key}={value}\n")
                key_found = True
            else:
                new_lines.append(line)
        if not key_found:
            new_lines.append(f"{key}={value}\n")

        with open(ENV_FILE, "w") as f:
            f.writelines(new_lines)

        self.reload()

settings = Settings()
