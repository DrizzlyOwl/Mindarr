"""Shared HTTP session factory with LAN-scoped SSL verification.

Local Plex servers are commonly reached over HTTPS with a self-signed
certificate on a LAN/private IP, so SSL verification must be disabled for
those hosts. Public hosts (e.g. plex.tv) should always be verified — blanket
`verify=False` on every session unnecessarily widens MITM exposure on paths
that don't need it (notably the plex.tv OAuth/token exchange).

This module centralizes that decision so callers don't duplicate
`session.verify = False` + `urllib3.disable_warnings(...)` boilerplate.
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import requests
import urllib3

logger = logging.getLogger(__name__)

_warnings_disabled = False


def _is_lan_host(host: str) -> bool:
    """Return True if host is a private/loopback/link-local IP or a .local hostname."""
    if not host:
        return False
    host = host.strip().lower()
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    # Not a literal IP — try resolving it, since Docker/LAN DNS names
    # (e.g. "plex.home") commonly resolve to private addresses.
    try:
        resolved = socket.gethostbyname(host)
        ip = ipaddress.ip_address(resolved)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except (socket.gaierror, ValueError, OSError):
        return False


def should_verify_ssl(url: str) -> bool:
    """Return whether SSL verification should be enabled for the given URL."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return True
    return not _is_lan_host(host)


def make_session(base_url: str) -> requests.Session:
    """Return a requests.Session with SSL verification scoped to the host.

    Verification is disabled only for LAN/private/loopback hosts (typical for
    a locally-hosted Plex Media Server). Public hosts keep verification on.
    """
    session = requests.Session()
    verify = should_verify_ssl(base_url)
    session.verify = verify
    if not verify:
        global _warnings_disabled
        if not _warnings_disabled:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            _warnings_disabled = True
        logger.debug(f"SSL verification disabled for LAN host: {base_url}")
    return session
