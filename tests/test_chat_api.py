import json

import httpx
import pytest

from gchat_bridge.auth import AccessTokens, TokenFile, load_token_file, save_token_file
from gchat_bridge.chat_api import ChatApi
from gchat_bridge.errors import ApiError, AuthError, ConfigError, TransportError

SPACE = "spaces/AAAAexample"


def token() -> TokenFile:
    return TokenFile("cid", "csecret", "refresh-1", "users/100", "ada@example.com")


class Google:
    """A scripted fake of Google's token and Chat endpoints."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.chat: list[httpx.Response] = []  # answers to Chat calls, in order
        self.token_answers: list[httpx.Response] = []
        self.issued = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            if self.token_answers:
                return self.token_answers.pop(0)
            self.issued += 1
            return httpx.Response(
                200, json={"access_token": f"at-{self.issued}", "expires_in": 3599}
            )
        if not self.chat:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        answer = self.chat.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def chat_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host != "oauth2.googleapis.com"]


@pytest.fixture
async def google():
    return Google()


@pytest.fixture
async def api(google):
    http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    yield ChatApi(AccessTokens(token(), http), http, backoff_base=0.001)
    await http.aclose()


def error(status: int, message: str, google_status: str = "") -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": message, "status": google_status}})


async def test_create_sends_client_id_and_thread(api, google):
    google.chat.append(httpx.Response(200, json={"name": f"{SPACE}/messages/m1"}))
    msg, new = await api.create_message(
        SPACE, "hi", message_id="client-ag-1", thread=f"{SPACE}/threads/t1"
    )
    assert new and msg["name"].endswith("m1")
    request = google.chat_requests()[0]
    assert request.url.params["messageId"] == "client-ag-1"
    assert request.url.params["messageReplyOption"] == "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    assert json.loads(request.content) == {"text": "hi", "thread": {"name": f"{SPACE}/threads/t1"}}
    assert request.headers["Authorization"] == "Bearer at-1"


async def test_create_conflict_returns_the_existing_message(api, google):
    google.chat += [
        error(409, "already exists", "ALREADY_EXISTS"),
        httpx.Response(200, json={"name": f"{SPACE}/messages/m1", "text": "hi"}),
    ]
    msg, new = await api.create_message(SPACE, "hi", message_id="client-ag-1")
    assert not new and msg["text"] == "hi"
    assert google.chat_requests()[1].url.path == f"/v1/{SPACE}/messages/client-ag-1"


async def test_server_errors_are_retried_then_a_transport_error(api, google):
    google.chat += [error(503, "busy")] * 4
    with pytest.raises(TransportError):
        await api.get_space(SPACE)
    assert len(google.chat_requests()) == 4


async def test_retry_succeeds(api, google):
    google.chat += [error(429, "slow down"), httpx.Response(200, json={"name": SPACE})]
    assert (await api.get_space(SPACE))["name"] == SPACE


async def test_network_error_is_a_transport_error(api, google):
    google.chat.append(httpx.ConnectError("refused"))
    with pytest.raises(TransportError):
        await api.get_space(SPACE)


async def test_client_error_is_an_api_error_with_googles_message(api, google):
    google.chat.append(error(403, "The caller does not have permission", "PERMISSION_DENIED"))
    with pytest.raises(ApiError) as caught:
        await api.get_space(SPACE)
    assert caught.value.status == 403
    assert "permission" in str(caught.value)


async def test_refused_access_token_is_refreshed_once(api, google):
    google.chat += [error(401, "expired"), httpx.Response(200, json={"name": SPACE})]
    await api.get_space(SPACE)
    assert [r.headers["Authorization"] for r in google.chat_requests()] == [
        "Bearer at-1",
        "Bearer at-2",
    ]


async def test_access_token_refused_twice_is_an_auth_error(api, google):
    google.chat += [error(401, "no"), error(401, "no")]
    with pytest.raises(AuthError):
        await api.get_space(SPACE)


async def test_revoked_refresh_token_is_an_auth_error(api, google):
    google.token_answers.append(httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(AuthError, match="invalid_grant"):
        await api.get_space(SPACE)


async def test_list_messages_filters_and_orders(api, google):
    google.chat.append(
        httpx.Response(200, json={"messages": [{"name": "x"}], "nextPageToken": "p2"})
    )
    messages, token_ = await api.list_messages(
        SPACE, after="2026-10-10T18:00:00.000000Z", thread=f"{SPACE}/threads/t1", page_size=1
    )
    assert messages == [{"name": "x"}] and token_ == "p2"
    params = google.chat_requests()[0].url.params
    assert params["orderBy"] == "createTime asc"
    assert params["pageSize"] == "1"
    assert params["filter"] == (
        f'createTime > "2026-10-10T18:00:00.000000Z" AND thread.name = {SPACE}/threads/t1'
    )


async def test_get_message_missing_is_none(api, google):
    google.chat.append(error(404, "not found"))
    assert await api.get_message(f"{SPACE}/messages/x") is None


async def test_unknown_person_is_none(api, google):
    google.chat.append(error(404, "not found"))
    assert await api.get_person("users/5") is None
    assert google.chat_requests()[0].url.path == "/v1/people/5"


async def test_rotated_refresh_token_is_saved(tmp_path, google):
    path = tmp_path / "token.json"
    save_token_file(path, token())
    google.token_answers.append(
        httpx.Response(200, json={"access_token": "at", "expires_in": 3599, "refresh_token": "r2"})
    )
    google.chat.append(httpx.Response(200, json={"name": SPACE}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(google.handler)) as http:
        await ChatApi(AccessTokens(load_token_file(path), http, path), http).get_space(SPACE)
    assert load_token_file(path).refresh_token == "r2"
    assert path.stat().st_mode & 0o777 == 0o600


def test_token_file_errors(tmp_path):
    with pytest.raises(ConfigError, match="gchat-bridge login"):
        load_token_file(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"client_id": "x"}))
    with pytest.raises(ConfigError, match="client_secret"):
        load_token_file(bad)


async def test_members_are_read_across_pages(api, google):
    google.chat += [
        httpx.Response(200, json={"memberships": [{"name": "a"}], "nextPageToken": "p2"}),
        httpx.Response(200, json={"memberships": [{"name": "b"}]}),
    ]
    assert [m["name"] for m in await api.list_members(SPACE)] == ["a", "b"]
    assert google.chat_requests()[1].url.params["pageToken"] == "p2"


async def test_person_asks_for_names_and_photos(api, google):
    google.chat.append(httpx.Response(200, json={"names": []}))
    await api.get_person("users/5")
    assert google.chat_requests()[0].url.params["personFields"] == "names,photos"


async def test_download(api, google):
    google.chat += [httpx.Response(200, content=b"img"), error(403, "no"), error(503, "busy")]
    assert await api.download("https://example.com/p=s160") == b"img"
    with pytest.raises(ApiError):
        await api.download("https://example.com/p=s160")
    with pytest.raises(TransportError):
        await api.download("https://example.com/p=s160")
    assert "Authorization" not in google.chat_requests()[0].headers


async def test_download_over_the_cap_is_refused(api, google):
    from gchat_bridge.chat_api import MAX_DOWNLOAD_BYTES

    google.chat.append(httpx.Response(200, content=b"0" * (MAX_DOWNLOAD_BYTES + 1)))
    with pytest.raises(ApiError):
        await api.download("https://example.com/p=s160")


async def test_download_rate_limited_is_a_transport_error(api, google):
    google.chat.append(error(429, "slow down"))
    with pytest.raises(TransportError):
        await api.download("https://example.com/p=s160")
