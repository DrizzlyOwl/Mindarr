import pytest
from plex_recommender.http_client import should_verify_ssl, make_session, _is_lan_host


def test_private_ips_are_lan():
    assert _is_lan_host("192.168.1.5") is True
    assert _is_lan_host("10.255.10.30") is True
    assert _is_lan_host("172.16.0.1") is True
    assert _is_lan_host("127.0.0.1") is True


def test_public_ips_are_not_lan():
    assert _is_lan_host("8.8.8.8") is False
    assert _is_lan_host("1.1.1.1") is False


def test_localhost_and_dot_local_are_lan():
    assert _is_lan_host("localhost") is True
    assert _is_lan_host("myserver.local") is True
    assert _is_lan_host("MyServer.LOCAL") is True


def test_should_verify_ssl_disabled_for_lan_url():
    assert should_verify_ssl("https://192.168.1.5:32400") is False
    assert should_verify_ssl("https://10.255.10.30:32400") is False
    assert should_verify_ssl("http://localhost:8080") is False


def test_should_verify_ssl_enabled_for_public_url():
    assert should_verify_ssl("https://plex.tv") is True
    assert should_verify_ssl("https://app.plex.tv/auth") is True


def test_make_session_sets_verify_flag():
    lan_session = make_session("https://192.168.1.5:32400")
    assert lan_session.verify is False

    public_session = make_session("https://plex.tv")
    assert public_session.verify is True


def test_should_verify_ssl_handles_unresolvable_host_gracefully():
    # Unresolvable/garbage hostnames should not raise; DNS failure -> not LAN -> verify True
    assert should_verify_ssl("https://this-host-should-not-exist.invalid") is True
