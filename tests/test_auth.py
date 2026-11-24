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


def test_create_and_check_plex_pin():
    from unittest.mock import MagicMock

    pin_resp = MagicMock()
    pin_resp.json.return_value = {"id": 42, "code": "ABCD", "expiresIn": 900}
    pin_resp.raise_for_status.return_value = None

    with patch("plex_recommender.auth.requests.post", return_value=pin_resp) as mock_post:
        res = auth.create_plex_pin()
    assert res["id"] == 42
    assert res["code"] == "ABCD"
    assert res["expires_in"] == 900
    assert "code=ABCD" in res["auth_url"]
    assert mock_post.called

    claimed_resp = MagicMock(status_code=200)
    claimed_resp.json.return_value = {"authToken": "tok-xyz"}
    with patch("plex_recommender.auth.requests.get", return_value=claimed_resp):
        assert auth.check_plex_pin(42) == "tok-xyz"

    unclaimed_resp = MagicMock(status_code=200)
    unclaimed_resp.json.return_value = {"authToken": None}
    with patch("plex_recommender.auth.requests.get", return_value=unclaimed_resp):
        assert auth.check_plex_pin(42) is None

    with patch("plex_recommender.auth.requests.get", side_effect=RuntimeError("net down")):
        assert auth.check_plex_pin(42) is None


def test_get_plex_account():
    from unittest.mock import MagicMock

    ok_resp = MagicMock(status_code=200)
    ok_resp.json.return_value = {
        "user": {
            "id": 12345,
            "uuid": "uuid-1",
            "username": "bob",
            "title": "Bob",
            "email": "bob@x.com",
            "thumb": "https://img/bob.png",
        }
    }
    with patch("plex_recommender.auth.requests.get", return_value=ok_resp):
        acct = auth.get_plex_account("tok")
    assert acct == {
        "user_key": "12345",
        "plex_uuid": "uuid-1",
        "username": "bob",
        "title": "Bob",
        "email": "bob@x.com",
        "thumb": "https://img/bob.png",
    }

    bad_resp = MagicMock(status_code=401)
    with patch("plex_recommender.auth.requests.get", return_value=bad_resp):
        assert auth.get_plex_account("bad") is None

    empty_resp = MagicMock(status_code=200)
    empty_resp.json.return_value = {"user": {}}
    with patch("plex_recommender.auth.requests.get", return_value=empty_resp):
        assert auth.get_plex_account("tok") is None


def test_get_shared_users_xml_and_discover_filtering():
    from unittest.mock import MagicMock

    xml_body = """
    <MediaContainer>
      <User id="200" username="bob" email="bob@x.com" title="Bob" thumb="/b.png">
        <Server machineIdentifier="MACHINE123" />
      </User>
      <User id="300" username="carol" email="carol@x.com" title="" thumb="/c.png">
        <Server machineIdentifier="OTHER_SERVER" />
      </User>
    </MediaContainer>
    """.strip()

    resp = MagicMock(status_code=200, text=xml_body)
    with patch("plex_recommender.auth.requests.get", return_value=resp):
        users = auth.get_shared_users("admintok")
        assert len(users) == 2
        assert users[0]["servers"] == ["MACHINE123"]

        discovered = auth.discover_shared_users(admin_token="admintok", machine_id="MACHINE123")
        assert len(discovered) == 1
        assert discovered[0]["user_key"] == "200"
        assert discovered[0]["username"] == "bob"

    bad_xml_resp = MagicMock(status_code=200, text="<not-valid-xml")
    with patch("plex_recommender.auth.requests.get", return_value=bad_xml_resp):
        assert auth.get_shared_users("admintok") == []


def test_verify_plex_connection_and_machine_id(monkeypatch):
    from unittest.mock import MagicMock

    monkeypatch.setattr(auth.settings, "plex_url", "")
    monkeypatch.setattr(auth.settings, "plex_token", None)

    ok, msg = auth.verify_plex_connection(baseurl="", token="tok")
    assert ok is False
    assert "No Plex URL" in msg

    ok2, msg2 = auth.verify_plex_connection(baseurl="http://plex.local:32400", token="")
    assert ok2 is False
    assert "No Plex token" in msg2

    fake_plex = MagicMock(friendlyName="HomePlex", machineIdentifier="MID-99", version="1.40.0")
    with patch("plex_recommender.auth.PlexServer", return_value=fake_plex):
        ok3, msg3 = auth.verify_plex_connection(baseurl="http://plex.local:32400", token="tok")
        assert ok3 is True
        assert "HomePlex" in msg3
        assert auth.get_server_machine_id(baseurl="http://plex.local:32400", token="tok") == "MID-99"

    with patch("plex_recommender.auth.PlexServer", side_effect=RuntimeError("timeout")):
        ok4, _ = auth.verify_plex_connection(baseurl="http://plex.local:32400", token="tok")
        assert ok4 is False
        assert auth.get_server_machine_id(baseurl="http://plex.local:32400", token="tok") is None
