from unittest.mock import patch
from plex_recommender import auth


def _shared_users():
    return [
        {"id": "200", "username": "bob", "email": "bob@x.com", "servers": ["MACHINE123"]},
        {"id": "300", "username": "carol", "email": "carol@x.com", "servers": ["OTHER"]},
    ]


def test_owner_always_allowed():
    with patch.object(auth, "get_shared_users", return_value=[]):
        assert auth.check_user_access("admintok", "MACHINE123", user_id="100", owner_id="100") is True


def test_shared_user_with_matching_machine_allowed():
    with patch.object(auth, "get_shared_users", return_value=_shared_users()):
        assert auth.check_user_access("admintok", "MACHINE123", user_id="200", owner_id="100") is True


def test_shared_user_without_matching_machine_denied():
    with patch.object(auth, "get_shared_users", return_value=_shared_users()):
        assert auth.check_user_access("admintok", "MACHINE123", user_id="300", owner_id="100") is False


def test_unknown_user_denied():
    with patch.object(auth, "get_shared_users", return_value=_shared_users()):
        assert auth.check_user_access("admintok", "MACHINE123", user_id="999", owner_id="100") is False


def test_no_machine_id_denies_non_owner():
    with patch.object(auth, "get_shared_users", return_value=_shared_users()):
        assert auth.check_user_access("admintok", None, user_id="200", owner_id="100") is False
