"""A small async client for the Google Chat and People APIs (user auth).

The retry and backoff rules and the client-assigned message id on sends are
adapted from google-chat-mcp (https://github.com/mmedum/google-chat-mcp,
Apache-2.0).
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Mapping
from typing import Any

import httpx

from gchat_bridge.auth import AccessTokens
from gchat_bridge.errors import ApiError, AuthError, TransportError

CHAT_BASE = "https://chat.googleapis.com/v1"
PEOPLE_BASE = "https://people.googleapis.com/v1"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RETRIES = 3
MAX_BACKOFF_SECONDS = 30.0
PAGE_SIZE = 100  # Chat's largest page of messages
MAX_DOWNLOAD_BYTES = 1024 * 1024


class ChatApi:
    def __init__(
        self,
        tokens: AccessTokens,
        http: httpx.AsyncClient,
        *,
        chat_base: str = CHAT_BASE,
        people_base: str = PEOPLE_BASE,
        backoff_base: float = 0.5,
    ) -> None:
        self._tokens = tokens
        self._http = http
        self._chat = chat_base
        self._people = people_base
        self._backoff_base = backoff_base

    @property
    def user(self) -> str:
        """The account's own Chat user, users/{id}."""
        return self._tokens.user

    # --- spaces -----------------------------------------------------------

    async def get_space(self, space: str) -> dict[str, Any]:
        return await self._request("GET", f"{self._chat}/{space}")

    async def list_spaces(self) -> list[dict[str, Any]]:
        spaces: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params = {"pageSize": "1000"}
            if token:
                params["pageToken"] = token
            data = await self._request("GET", f"{self._chat}/spaces", params=params)
            spaces.extend(s for s in data.get("spaces") or [] if isinstance(s, dict))
            token = data.get("nextPageToken") or None
            if not token:
                return spaces

    async def list_members(self, space: str) -> list[dict[str, Any]]:
        members: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params = {"pageSize": "1000"}
            if token:
                params["pageToken"] = token
            data = await self._request("GET", f"{self._chat}/{space}/members", params=params)
            members.extend(m for m in data.get("memberships") or [] if isinstance(m, dict))
            token = data.get("nextPageToken") or None
            if not token:
                return members

    # --- messages ---------------------------------------------------------

    async def list_messages(
        self,
        space: str,
        *,
        after: str | None = None,
        newest_first: bool = False,
        thread: str | None = None,
        page_size: int = PAGE_SIZE,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of messages created after `after` (RFC 3339), oldest first
        unless `newest_first`; returns the messages and the next page token."""
        params: dict[str, str] = {
            "pageSize": str(page_size),
            "orderBy": "createTime desc" if newest_first else "createTime asc",
        }
        filters = []
        if after:
            filters.append(f'createTime > "{after}"')
        if thread:
            filters.append(f"thread.name = {thread}")
        if filters:
            params["filter"] = " AND ".join(filters)
        if page_token:
            params["pageToken"] = page_token
        data = await self._request("GET", f"{self._chat}/{space}/messages", params=params)
        messages = [m for m in data.get("messages") or [] if isinstance(m, dict)]
        return messages, data.get("nextPageToken") or None

    async def get_message(self, name: str) -> dict[str, Any] | None:
        """One message by resource name; None when it does not exist."""
        try:
            return await self._request("GET", f"{self._chat}/{name}")
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise

    async def create_message(
        self, space: str, text: str, *, message_id: str, thread: str | None = None
    ) -> tuple[dict[str, Any], bool]:
        """Post text with a client-assigned message id; returns the message and
        whether it is new. A message that already has that id (an earlier
        attempt got through) is returned as it is, with False."""
        params = {"messageId": message_id}
        body: dict[str, Any] = {"text": text}
        if thread:
            body["thread"] = {"name": thread}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        try:
            message = await self._request(
                "POST", f"{self._chat}/{space}/messages", params=params, json=body
            )
            return message, True
        except ApiError as exc:
            if exc.status != 409:
                raise
        existing = await self.get_message(f"{space}/messages/{message_id}")
        if existing is None:
            raise ApiError(409, "the message id is taken but the message cannot be read")
        return existing, False

    # --- people -----------------------------------------------------------

    async def get_person(self, user: str) -> dict[str, Any] | None:
        """Name and photo of a Chat user (users/{id}) from the People API; None
        when the directory does not know them."""
        resource = "people/" + user.removeprefix("users/")
        try:
            return await self._request(
                "GET", f"{self._people}/{resource}", params={"personFields": "names,photos"}
            )
        except ApiError as exc:
            if exc.status in (403, 404):
                return None
            raise

    async def download(self, url: str) -> bytes:
        """A public image (a profile photo), at most MAX_DOWNLOAD_BYTES."""
        try:
            resp = await self._http.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            raise TransportError(f"download: {type(exc).__name__}") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransportError(f"download: HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise ApiError(resp.status_code, "download refused")
        if len(resp.content) > MAX_DOWNLOAD_BYTES:
            raise ApiError(413, "download too large")
        return resp.content

    # --- transport --------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            access = await self._tokens.get()
            try:
                resp = await self._http.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except httpx.HTTPError as exc:
                raise TransportError(f"{method} {_path(url)}: {type(exc).__name__}") from exc
            if 200 <= resp.status_code < 300:
                try:
                    data = resp.json() if resp.content else {}
                except ValueError as exc:
                    raise TransportError(f"{method} {_path(url)}: answer is not JSON") from exc
                return data if isinstance(data, dict) else {}
            if resp.status_code == 401 and not refreshed:
                refreshed = True
                self._tokens.invalidate()
                continue
            if resp.status_code == 401:
                raise AuthError("Google refused the access token")
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt <= MAX_RETRIES:
                await asyncio.sleep(self._backoff(attempt, resp))
                continue
            message, status = _error_payload(resp)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise TransportError(f"{method} {_path(url)}: HTTP {resp.status_code} {message}")
            raise ApiError(resp.status_code, message, status)

    def _backoff(self, attempt: int, resp: httpx.Response) -> float:
        """Exponential with jitter; `Retry-After` is honoured within bounds."""
        base = self._backoff_base * (2 ** (attempt - 1))
        floor = base + random.uniform(0, 0.25 * base)
        try:
            seconds = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            return min(floor, MAX_BACKOFF_SECONDS)
        if not math.isfinite(seconds):
            return min(floor, MAX_BACKOFF_SECONDS)
        return min(max(seconds, floor), MAX_BACKOFF_SECONDS)


def _path(url: str) -> str:
    """The API path of a URL, for logs (no host, no query)."""
    return "/" + url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]


def _error_payload(resp: httpx.Response) -> tuple[str, str | None]:
    """(message, status) of a Google error body, the message cut to 300 characters."""
    try:
        payload = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}", None
    err = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(err, dict):
        return f"HTTP {resp.status_code}", None
    message = " ".join(str(err.get("message") or "").split())[:300]
    status = err.get("status") if isinstance(err.get("status"), str) else None
    return message or f"HTTP {resp.status_code}", status
