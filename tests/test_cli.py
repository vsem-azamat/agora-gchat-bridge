import asyncio
import json
from types import SimpleNamespace

from gchat_bridge import cli
from gchat_bridge.config import Config
from gchat_bridge.errors import AuthError, ConfigError, TransportError
from tests.fakes import SPACE, FakeChat

CONFIG = Config(space=SPACE, token_file=None, poll_seconds=0.01)


def hello_then_wait(after=None):
    async def reader(bridge):
        bridge.handle_hub_line(json.dumps({"type": "hello", "version": 3, "bridge": "b"}))
        await (after or asyncio.Event()).wait()

    return reader


async def test_configuration_errors_exit_2():
    for error in (ConfigError("no space"), AuthError("revoked")):
        chat = FakeChat()
        chat.space_error = error
        code = await asyncio.wait_for(
            cli.run(CONFIG, chat, stdin_reader=hello_then_wait(), write=lambda line: None), 2
        )
        assert code == cli.EXIT_CONFIG


async def test_space_that_cannot_be_read_exits_2():
    from tests.fakes import api_error

    chat = FakeChat()
    chat.space_error = api_error(404, "not found")
    code = await asyncio.wait_for(
        cli.run(CONFIG, chat, stdin_reader=hello_then_wait(), write=lambda line: None), 2
    )
    assert code == cli.EXIT_CONFIG


async def test_backoff_doubles_to_the_cap_and_resets_after_steady_work(monkeypatch):
    chat = FakeChat()
    chat.space_error = TransportError("down")
    pauses: list[float] = []
    clock = {"now": 0.0}
    # cli's own clock only: asyncio's loop reads time.monotonic too
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

    original = chat.get_space

    async def get_space(space):
        if len(pauses) == 8:  # the ninth run lasts a minute before it fails
            clock["now"] += cli.STEADY_SECONDS
        return await original(space)

    chat.get_space = get_space

    async def record(closed, seconds):
        pauses.append(seconds)
        if len(pauses) == 9:
            closed.set()

    monkeypatch.setattr(cli, "pause", record)
    code = await asyncio.wait_for(
        cli.run(CONFIG, chat, stdin_reader=hello_then_wait(), write=lambda line: None), 2
    )
    assert code == 0
    assert pauses == [1, 2, 4, 8, 16, 32, 60, 60, 1]


async def test_stdin_closing_stops_the_bridge():
    chat = FakeChat()
    lines: list[dict] = []

    async def reader(bridge):
        bridge.handle_hub_line(json.dumps({"type": "hello", "version": 3, "bridge": "b"}))
        await asyncio.sleep(0.05)
        bridge.closed.set()

    code = await asyncio.wait_for(cli.run(CONFIG, chat, stdin_reader=reader, write=lines.append), 2)
    assert code == 0 and lines[0]["type"] == "hello"
