import time
import logging
import requests
import xml.etree.ElementTree as ET
from typing import Optional, Dict, Any, Tuple, List
from plexapi.server import PlexServer
from plex_recommender import __version__
from plex_recommender.config import settings
from plex_recommender.http_client import make_session

logger = logging.getLogger(__name__)

PLEX_CLIENT_ID = "mindarr-app"
APP_PRODUCT = "Mindarr"

def create_plex_pin() -> Dict[str, Any]:
    """
    Initiate Plex OAuth PIN flow.
    Returns dictionary with pin id, code, and direct browser auth URL.
    """
    url = "https://plex.tv/api/v2/pins?strong=true"
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": APP_PRODUCT,
        "X-Plex-Client-Identifier": PLEX_CLIENT_ID,
        "X-Plex-Version": __version__
    }
    resp = requests.post(url, headers=headers, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    
    pin_id = data["id"]
    code = data["code"]
    auth_url = f"https://app.plex.tv/auth#?clientID={PLEX_CLIENT_ID}&code={code}"
    
    return {
        "id": pin_id,
        "code": code,
        "auth_url": auth_url,
        "expires_in": data.get("expiresIn", 1800)
    }

def check_plex_pin(pin_id: int) -> Optional[str]:
    """Check if the PIN has been claimed. Returns token if claimed, else None."""
    url = f"https://plex.tv/api/v2/pins/{pin_id}"
    headers = {
        "Accept": "application/json",
        "X-Plex-Client-Identifier": PLEX_CLIENT_ID
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("authToken")
    except Exception as e:
        logger.warning(f"Error checking Plex PIN: {e}")
    return None

def poll_plex_pin(pin_id: int, timeout_sec: int = 180, interval_sec: int = 2) -> Optional[str]:
    """Poll Plex PIN until claimed or timed out."""
    start = time.time()
    while time.time() - start < timeout_sec:
        token = check_plex_pin(pin_id)
        if token:
            return token
        time.sleep(interval_sec)
    return None

def verify_plex_connection(baseurl: Optional[str] = None, token: Optional[str] = None) -> Tuple[bool, str]:
    """
    Test connection to the Plex Media Server.
    Returns (success_bool, message_str).
    """
    url = (baseurl or settings.plex_url).rstrip("/")
    tok = token or settings.plex_token
    if not tok:
        return False, "No Plex token provided."

    # Use a session with SSL verification scoped to LAN hosts (local Plex).
    session = make_session(url)

    try:
        plex = PlexServer(url, tok, session=session, timeout=10)
        server_name = plex.friendlyName or plex.machineIdentifier
        version = plex.version
        return True, f"Connected to '{server_name}' (Plex {version})"
    except Exception as e:
        logger.error(f"Plex connection error: {e}")
        return False, str(e)


def get_plex_account(token: str) -> Optional[Dict[str, Any]]:
    """Fetch the plex.tv account identity for a user token.

    Returns None if the token is invalid/revoked (validates the token).
    """
    url = "https://plex.tv/users/account.json"
    headers = {
        "Accept": "application/json",
        "X-Plex-Token": token,
        "X-Plex-Client-Identifier": PLEX_CLIENT_ID,
        "X-Plex-Product": APP_PRODUCT,
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        user = resp.json().get("user", {})
        if not user.get("id"):
            return None
        return {
            "user_key": str(user.get("id")),
            "plex_uuid": user.get("uuid"),
            "username": user.get("username"),
            "title": user.get("title"),
            "email": user.get("email"),
            "thumb": user.get("thumb"),
        }
    except Exception as e:
        logger.warning(f"Failed to fetch plex.tv account: {e}")
        return None


def get_server_machine_id(baseurl: Optional[str] = None, token: Optional[str] = None) -> Optional[str]:
    """Return the machineIdentifier of the configured Plex server."""
    url = (baseurl or settings.plex_url).rstrip("/")
    tok = token or settings.plex_token
    if not tok:
        return None
    session = make_session(url)
    try:
        plex = PlexServer(url, tok, session=session, timeout=10)
        return plex.machineIdentifier
    except Exception as e:
        logger.error(f"Could not resolve server machine id: {e}")
        return None


def get_shared_users(admin_token: str) -> List[Dict[str, Any]]:
    """Return the owner's shared users from plex.tv (XML /api/users).

    Each entry: {id, username, email, servers: [machineIdentifier, ...]}.
    """
    url = "https://plex.tv/api/users"
    headers = {
        "X-Plex-Token": admin_token,
        "X-Plex-Client-Identifier": PLEX_CLIENT_ID,
    }
    users: List[Dict[str, Any]] = []
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return users
        root = ET.fromstring(resp.text)
        for u in root.findall("User"):
            servers = [s.get("machineIdentifier") for s in u.findall("Server")]
            users.append({
                "id": u.get("id"),
                "username": u.get("username"),
                "email": u.get("email"),
                "title": u.get("title"),
                "thumb": u.get("thumb"),
                "servers": [s for s in servers if s],
            })
    except Exception as e:
        logger.warning(f"Failed to fetch shared users: {e}")
    return users


def discover_shared_users(
    admin_token: Optional[str] = None,
    machine_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Discover users shared with this server from plex.tv."""
    tok = admin_token or settings.plex_token
    if not tok:
        return []
    mid = machine_id or settings.plex_machine_id
    raw_users = get_shared_users(tok)
    discovered = []
    for u in raw_users:
        if not mid or mid in u.get("servers", []):
            discovered.append({
                "user_key": str(u.get("id")),
                "username": u.get("username"),
                "email": u.get("email"),
                "title": u.get("title") or u.get("username"),
                "thumb": u.get("thumb"),
            })
    return discovered


def check_user_access(admin_token: str, machine_id: Optional[str], user_id: str, owner_id: Optional[str] = None) -> bool:
    """Authorize a user for this server.

    The server owner is always allowed. Any other user is allowed only if they
    appear in the owner's shared-user list (plex.tv /api/users) with access to
    the configured server's machineIdentifier.
    """
    if owner_id and str(user_id) == str(owner_id):
        return True
    if not machine_id:
        return False
    for u in get_shared_users(admin_token):
        if str(u.get("id")) == str(user_id):
            return machine_id in u.get("servers", [])
    return False
