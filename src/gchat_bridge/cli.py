"""Entry point: wire stdin/stdout to the bridge and keep it connected."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from gchat_bridge import account
from gchat_bridge.auth import AccessTokens, load_token_file
from gchat_bridge.bridge import Api, Bridge
from gchat_bridge.chat_api import ChatApi
from gchat_bridge.config import Config, parse_args
from gchat_bridge.errors import ApiError, AuthError, ConfigError, TransportError
from gchat_bridge.text import short_reason

log = logging.getLogger("gchat_bridge")

EXIT_CONFIG = 2
BACKOFF_MAX = 60.0
STEADY_SECONDS = 60.0


def write_stdout(line: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def read_stdin(bridge: Bridge) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=2 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    try:
        while True:
            try:
                raw = await reader.readline()
            except ValueError:
                log.warning("hub line too long; skipped")
                continue
            if not raw:
                break
            bridge.handle_hub_line(raw.decode("utf-8", errors="replace"))
    finally:
        log.info("stdin closed; stopping")
        bridge.closed.set()


async def pause(closed: asyncio.Event, seconds: float) -> None:
    """Wait before a reconnect, or until the bridge is stopped."""
    try:
        await asyncio.wait_for(closed.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def run(
    config: Config,
    api: Api,
    *,
    stdin_reader: Callable[[Bridge], Awaitable[None]] = read_stdin,
    write: Callable[[dict[str, Any]], None] = write_stdout,
    backoff_start: float = 1.0,
) -> int:
    bridge = Bridge(
        api,
        config.space,
        write,
        prefix=config.prefix,
        poll_seconds=config.poll_seconds,
        reconcile_seconds=config.reconcile_seconds,
    )
    loop = asyncio.get_running_loop()
    signals = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, bridge.closed.set)
            signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    stdin_task = asyncio.ensure_future(stdin_reader(bridge))
    backoff = backoff_start
    try:
        while not bridge.closed.is_set():
            started = time.monotonic()
            try:
                await bridge.serve()
                if bridge.closed.is_set():
                    return 0
            except (ConfigError, AuthError) as exc:
                log.error("configuration error: %s", exc)
                return EXIT_CONFIG
            except (TransportError, ApiError) as exc:
                log.warning(
                    "Google Chat failed (%s); retrying in %.0f s", short_reason(exc, 120), backoff
                )
            if time.monotonic() - started >= STEADY_SECONDS:
                backoff = backoff_start
            await pause(bridge.closed, backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
        return 0
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
        stdin_task.cancel()
        await asyncio.gather(stdin_task, return_exceptions=True)


async def _main(config: Config) -> int:
    token = load_token_file(config.token_file)
    async with httpx.AsyncClient() as http:
        api = ChatApi(AccessTokens(token, http, config.token_file), http)
        return await run(config, api)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="gchat-bridge %(levelname)s %(message)s"
    )
    # httpx logs every request at INFO; a poll every few seconds would flood the hub's log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("login", "spaces"):
        return account.main(args[0], args[1:])
    try:
        config = parse_args(args)
        return asyncio.run(_main(config))
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_CONFIG


if __name__ == "__main__":
    sys.exit(main())
