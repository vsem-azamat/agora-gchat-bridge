"""The token file and OAuth access tokens.

`gchat-bridge login` runs Google's installed-app flow on a loopback port and
writes the refresh token, the OAuth client and the account's Chat user id to
the token file (mode 0600). The bridge reads it at start and refreshes access
tokens itself; secrets stay in memory and are never logged.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from gchat_bridge.errors import AuthError, ConfigError, TransportError

TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/directory.readonly",
)
EXPIRY_MARGIN_SECONDS = 60.0  # refresh this long before Google's expiry
REFRESH_TIMEOUT_SECONDS = 15.0


@dataclass
class TokenFile:
    client_id: str
    client_secret: str
    refresh_token: str
    user: str  # the account's Chat user, users/{id}
    email: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "user": self.user,
            "email": self.email,
        }


def load_token_file(path: Path) -> TokenFile:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"no token file at {path}; run `gchat-bridge login`") from None
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read the token file {path} ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"the token file {path} is not a JSON object")
    fields = {}
    for key in ("client_id", "client_secret", "refresh_token", "user"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"the token file {path} has no {key}; run `gchat-bridge login`")
        fields[key] = value
    if not fields["user"].startswith("users/"):
        raise ConfigError(f"the token file {path} has a malformed user")
    email = data.get("email") if isinstance(data.get("email"), str) else ""
    return TokenFile(**fields, email=email)


def save_token_file(path: Path, token: TokenFile) -> None:
    """Write the token file atomically with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(token.to_json(), fh)
    os.replace(tmp, path)


class AccessTokens:
    """Access tokens from the refresh token, refreshed when they expire or
    when Google refuses one (`invalidate`)."""

    def __init__(self, token: TokenFile, http: httpx.AsyncClient, path: Path | None = None):
        self._token = token
        self._http = http
        self._path = path
        self._access: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def user(self) -> str:
        return self._token.user

    def invalidate(self) -> None:
        self._access = None

    async def get(self) -> str:
        async with self._lock:
            if self._access is None or time.monotonic() >= self._expires_at:
                await self._refresh()
            assert self._access is not None
            return self._access

    async def _refresh(self) -> None:
        try:
            resp = await self._http.post(
                TOKEN_URI,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._token.refresh_token,
                    "client_id": self._token.client_id,
                    "client_secret": self._token.client_secret,
                },
                timeout=REFRESH_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"token refresh: {type(exc).__name__}") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransportError(f"token refresh: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or not isinstance(data, dict):
            error = data.get("error") if isinstance(data, dict) else None
            raise AuthError(
                f"Google refused the refresh token ({error or resp.status_code}); "
                "run `gchat-bridge login` again"
            )
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise AuthError("Google's token answer has no access token")
        expires_in = data.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, int | float) else 3600.0
        self._access = access
        self._expires_at = time.monotonic() + max(0.0, lifetime - EXPIRY_MARGIN_SECONDS)
        rotated = data.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != self._token.refresh_token:
            self._token.refresh_token = rotated
            if self._path is not None:
                save_token_file(self._path, self._token)
