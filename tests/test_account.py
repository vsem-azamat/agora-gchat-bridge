import json
from types import SimpleNamespace

import httpx
import pytest

from gchat_bridge import account
from gchat_bridge.auth import SCOPE_FEATURES, SCOPES, scope_gaps

CHAT = "https://www.googleapis.com/auth/chat."
MEMBERS = CHAT + "memberships.readonly"
DIRECTORY = "https://www.googleapis.com/auth/directory.readonly"


def test_every_scope_asked_for_says_what_is_lost_without_it():
    assert set(SCOPE_FEATURES) == set(SCOPES)


def test_all_granted_is_no_gap():
    assert scope_gaps(list(SCOPES)) == ([], [])


def test_unknown_grant_is_no_gap():
    assert scope_gaps(None) == ([], [])


def test_missing_memberships_loses_mentions_only():
    fatal, lost = scope_gaps([s for s in SCOPES if s != MEMBERS])
    assert fatal == []
    assert [scope for scope, _ in lost] == [MEMBERS]
    assert "mention" in lost[0][1]


def test_missing_send_or_read_is_fatal():
    for scope in (CHAT + "messages.create", CHAT + "messages.readonly", CHAT + "spaces.readonly"):
        fatal, _ = scope_gaps([s for s in SCOPES if s != scope])
        assert [s for s, _ in fatal] == [scope]


def test_grant_as_one_string_is_split():
    granted = " ".join(s for s in SCOPES if s != DIRECTORY)
    fatal, lost = scope_gaps(granted)
    assert fatal == []
    assert [s for s, _ in lost] == [DIRECTORY]


class Flow:
    """A stand-in for google_auth_oauthlib's InstalledAppFlow."""

    granted: list[str] = list(SCOPES)

    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        return cls()

    def run_local_server(self, **kwargs):
        return SimpleNamespace(
            refresh_token="refresh-example",
            token="access-example",
            client_id="client-example",
            client_secret="secret-example",
            granted_scopes=Flow.granted,
        )


@pytest.fixture
def login(tmp_path, monkeypatch):
    import google_auth_oauthlib.flow

    monkeypatch.setattr(google_auth_oauthlib.flow, "InstalledAppFlow", Flow)
    me = {"resourceName": "people/123", "emailAddresses": [{"value": "ada@example.com"}]}
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: httpx.Response(200, json=me, request=httpx.Request("GET", "x")),
    )
    secret = tmp_path / "client.json"
    secret.write_text("{}")
    token = tmp_path / "token.json"
    args = SimpleNamespace(client_secret=str(secret), token=str(token), manual=False)

    def run(granted):
        Flow.granted = granted
        return account.login(args)

    return run, token


def test_login_without_memberships_saves_and_warns(login, capsys):
    run, token = login
    assert run([s for s in SCOPES if s != MEMBERS]) == 0
    assert json.loads(token.read_text())["user"] == "users/123"
    err = capsys.readouterr().err
    assert "chat.memberships.readonly" in err and "mention" in err


def test_login_without_send_fails_and_keeps_no_token(login, capsys):
    run, token = login
    assert run([s for s in SCOPES if s != CHAT + "messages.create"]) == 1
    assert not token.exists()
    assert "chat.messages.create" in capsys.readouterr().err


def test_login_with_every_scope_is_quiet(login, capsys):
    run, _ = login
    assert run(list(SCOPES)) == 0
    assert "warning" not in capsys.readouterr().err
