"""`gchat-bridge login` and `gchat-bridge spaces`."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

from gchat_bridge.auth import SCOPES, AccessTokens, TokenFile, load_token_file, save_token_file
from gchat_bridge.chat_api import PEOPLE_BASE, ChatApi
from gchat_bridge.config import add_common_options, expand, file_defaults
from gchat_bridge.errors import ApiError, AuthError, ConfigError, TransportError

CLIENT_SECRET_ENV = "GCHAT_BRIDGE_CLIENT_SECRET"
MANUAL_PORT = 8765  # --manual: a loopback address nothing listens on
PROFILE_TIMEOUT_SECONDS = 15.0


def _parser(command: str, argv: list[str]) -> argparse.Namespace:
    file_values = file_defaults(argv)
    parser = argparse.ArgumentParser(prog=f"gchat-bridge {command}")
    add_common_options(parser)
    if command == "login":
        parser.add_argument(
            "--client-secret",
            default=os.environ.get(CLIENT_SECRET_ENV),
            help="the OAuth client file (Desktop app) downloaded from Google Cloud Console",
        )
        parser.add_argument(
            "--manual",
            action="store_true",
            help="for a machine without a browser: print the URL, then read the address "
            "the browser was sent to (the page that failed to load) from standard input",
        )
    parser.set_defaults(**file_values)
    return parser.parse_args(argv)


def login(args: argparse.Namespace) -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not args.client_secret:
        print("error: --client-secret is required", file=sys.stderr)
        return 2
    secret = expand(args.client_secret)
    if not secret.is_file():
        print(f"error: {secret} is not a file", file=sys.stderr)
        return 2
    # Google reports `email`/`profile` back as userinfo.* URLs; the scopes
    # asked for already use those, this only quiets oauthlib's strict check.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), scopes=list(SCOPES))
    if args.manual:
        # Nothing listens on this port: the browser fails to load the page, and
        # its address carries the code.
        flow.redirect_uri = f"http://127.0.0.1:{MANUAL_PORT}/"
        url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        print(f"\nOpen this URL to authorize the bridge:\n\n  {url}\n")
        print("Then paste the address of the page that failed to load:")
        answer = sys.stdin.readline().strip()
        # oauthlib insists on https for the callback; it never leaves this process.
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
        flow.fetch_token(authorization_response=answer)
        creds = flow.credentials
    else:
        creds = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            authorization_prompt_message="\nOpen this URL to authorize the bridge:\n\n  {url}\n",
            success_message="agora-gchat-bridge received the code; you may close this window.",
        )
    if not creds.refresh_token:
        print("error: Google returned no refresh token", file=sys.stderr)
        return 1
    try:
        resp = httpx.get(
            f"{PEOPLE_BASE}/people/me",
            params={"personFields": "names,emailAddresses"},
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=PROFILE_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        me = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"error: cannot read your profile ({type(exc).__name__})", file=sys.stderr)
        return 1
    resource = str(me.get("resourceName") or "")
    if not resource.startswith("people/") or not resource[len("people/") :].isdigit():
        print("error: your profile has no numeric id", file=sys.stderr)
        return 1
    emails = [e.get("value") for e in me.get("emailAddresses") or [] if isinstance(e, dict)]
    token = TokenFile(
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        refresh_token=creds.refresh_token,
        user="users/" + resource[len("people/") :],
        email=next((e for e in emails if isinstance(e, str)), ""),
    )
    path = expand(args.token)
    save_token_file(path, token)
    print(f"Saved the token to {path} (mode 0600).")
    if token.email:
        print(f"Logged in as {token.email}.")
    return 0


async def _list_spaces(path: Path) -> int:
    token = load_token_file(path)
    async with httpx.AsyncClient() as http:
        api = ChatApi(AccessTokens(token, http, path), http)
        spaces = await api.list_spaces()
    for space in spaces:
        kind = space.get("spaceType") or space.get("type") or ""
        name = space.get("displayName") or "(no name)"
        print(f"{space.get('name')}\t{kind}\t{name}")
    return 0


def main(command: str, argv: list[str]) -> int:
    try:
        args = _parser(command, argv)
        if command == "login":
            return login(args)
        return asyncio.run(_list_spaces(expand(args.token)))
    except (ConfigError, AuthError, ApiError, TransportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
