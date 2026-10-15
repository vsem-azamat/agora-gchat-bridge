import asyncio
import json

import pytest

from gchat_bridge.bridge import MAX_MESSAGE_UTF16, PROTOCOL_VERSION, Bridge, Cursor
from gchat_bridge.errors import ConfigError, TransportError
from gchat_bridge.render import format_time
from tests.fakes import ME, SPACE, T0, FakeChat, api_error

AGENT = {"name": "secretary", "kind": "agent", "display": "secretary", "sigil": "key"}


class Harness:
    def __init__(self, chat: FakeChat, **kwargs) -> None:
        self.chat = chat
        self.lines: list[dict] = []
        self.bridge = Bridge(
            chat, SPACE, self.lines.append, poll_seconds=0.01, now=lambda: T0, **kwargs
        )
        self.task: asyncio.Task | None = None

    def hub(self, line: dict) -> None:
        self.bridge.handle_hub_line(json.dumps(line))

    def hello(self, cursor: str | None = None) -> None:
        line = {"type": "hello", "version": 3, "bridge": "gchat-test", "room": "gchat-test"}
        if cursor is not None:
            line["cursor"] = cursor
        self.hub(line)

    def out(self, out_id: int, text: str, author=None, **extra) -> None:
        self.hub({"type": "out", "id": out_id, "author": author or AGENT, "text": text, **extra})

    def start(self) -> None:
        self.task = asyncio.ensure_future(self.bridge.serve())

    async def stop(self) -> None:
        self.bridge.closed.set()
        if self.task is not None:
            await asyncio.wait_for(self.task, 5)

    def of(self, kind: str) -> list[dict]:
        return [line for line in self.lines if line.get("type") == kind]

    def ins(self) -> list[dict]:
        return [line for line in self.of("in") if "id" in line]

    async def until(self, check, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not check():
            if self.task is not None and self.task.done():
                self.task.result()
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"timed out; lines: {self.lines}")
            await asyncio.sleep(0.01)


@pytest.fixture
async def chat():
    return FakeChat()


async def started(chat: FakeChat, cursor: str | None = None, **kwargs) -> Harness:
    h = Harness(chat, **kwargs)
    h.hello(cursor)
    h.start()
    await h.until(lambda: h.bridge.cursor is not None and h.bridge._primed)
    return h


# --- hello and the cursor ----------------------------------------------------


async def test_hello_declares_personas_then_threads_for_a_threaded_space(chat):
    h = await started(chat)
    hellos = h.of("hello")
    assert hellos[0] == {
        "type": "hello",
        "version": PROTOCOL_VERSION,
        "caps": {"personas": "prefix"},
    }
    assert hellos[-1]["caps"] == {"personas": "prefix", "threads": "thread"}
    await h.stop()


async def test_unthreaded_space_declares_no_threads():
    h = await started(FakeChat(threaded=False))
    assert all("threads" not in line["caps"] for line in h.of("hello"))
    await h.stop()


async def test_empty_cursor_starts_after_newest_message_and_reports_it(chat):
    old = chat.add("history")
    h = await started(chat)
    assert h.ins() == []
    assert h.of("in")[0] == {"type": "in", "cursor": f"{old['createTime']}|m1"}
    await h.stop()


async def test_empty_space_starts_now(chat):
    h = await started(chat)
    assert h.of("in")[0] == {"type": "in", "cursor": f"{format_time(T0)}|"}
    chat.add("first")
    await h.until(lambda: h.ins())
    assert h.ins()[0]["text"] == "first"
    await h.stop()


async def test_cursor_from_hello_reads_what_came_after_it(chat):
    first = chat.add("seen before")
    chat.add("new")
    h = await started(chat, cursor=f"{first['createTime']}|m1")
    await h.until(lambda: h.ins())
    assert [line["text"] for line in h.ins()] == ["new"]
    assert h.ins()[0]["cursor"].endswith("|m2")
    await h.stop()


async def test_a_message_that_shows_late_is_reported_without_moving_the_cursor_back(chat):
    h = await started(chat)
    late = chat.add("late")
    chat.hidden.add(late["name"])
    chat.add("on time")
    await h.until(lambda: len(h.ins()) == 1)
    chat.hidden.clear()
    await h.until(lambda: len(h.ins()) == 2)
    assert h.ins()[1]["text"] == "late"
    assert h.ins()[1]["cursor"] == h.ins()[0]["cursor"]
    await h.stop()


def test_cursor_round_trip():
    cursor = Cursor(T0, "abc.def")
    assert Cursor.parse(str(cursor)) == cursor
    assert Cursor.parse("nonsense") is None
    assert Cursor.parse(f"{format_time(T0)}|") == Cursor(T0, "")


# --- inbound ------------------------------------------------------------------


async def test_incoming_message_line(chat):
    h = await started(chat)
    chat.add("hello there")
    await h.until(lambda: h.ins())
    line = h.ins()[0]
    assert line["id"] == "m1"
    assert line["author"] == {"id": "200", "name": "Ada", "self": False}
    assert line["text"] == "hello there"
    assert line["addressed"] is False
    assert line["at"] == "2026-10-10T18:00:01Z"
    assert "thread" not in line and "reply_to" not in line
    await h.stop()


async def test_mention_of_the_account_addresses_the_message(chat):
    h = await started(chat)
    mention = {"type": "USER_MENTION", "userMention": {"user": {"name": ME}}}
    chat.add("@Operator look", annotations=[mention])
    chat.add("<users/300> see this")
    await h.until(lambda: len(h.ins()) == 2)
    assert h.ins()[0]["addressed"] is True
    assert h.ins()[1]["addressed"] is False
    await h.stop()


async def test_user_markup_becomes_names(chat):
    h = await started(chat)
    mention = {"type": "USER_MENTION", "userMention": {"user": {"name": "users/300"}}}
    chat.add("<users/300> see <https://example.com|this>", annotations=[mention])
    await h.until(lambda: h.ins())
    assert h.ins()[0]["text"] == "@Grace see this (https://example.com)"
    await h.stop()


async def test_direct_message_is_addressed():
    chat = FakeChat(threaded=False, space_type="DIRECT_MESSAGE")
    h = await started(chat)
    chat.add("ping")
    chat.add("my own", sender=ME)
    await h.until(lambda: len(h.ins()) == 2)
    assert [line["addressed"] for line in h.ins()] == [True, False]
    await h.stop()


async def test_operator_message_is_self_and_prefixed_one_is_an_assistant(chat):
    h = await started(chat)
    chat.add("from my phone", sender=ME)
    chat.add("🤖 other: from another client", sender=ME)
    await h.until(lambda: len(h.ins()) == 2)
    assert h.ins()[0]["author"] == {"id": "100", "name": "Operator", "self": True}
    assert h.ins()[1]["author"] == {"id": "assistant", "name": "assistant (🤖)", "self": False}
    assert h.ins()[1]["addressed"] is False
    await h.stop()


async def test_attachments_and_cards_become_placeholders(chat):
    h = await started(chat)
    chat.add("", attachment=[{"contentName": "a.png", "contentType": "image/png"}])
    chat.add("", cardsV2=[{}], fallbackText="Build passed")
    await h.until(lambda: len(h.ins()) == 2)
    assert [line["text"] for line in h.ins()] == ["[image a.png]", "[card] Build passed"]
    await h.stop()


async def test_reply_in_a_thread_is_keyed_by_its_root(chat):
    h = await started(chat)
    chat.add("Release plan\nmore")
    chat.add("sounds good", thread_of="m1")
    await h.until(lambda: len(h.ins()) == 2)
    root, reply = h.ins()
    assert "thread" not in root
    assert reply["thread"] == {"key": "m1", "title": "Release plan"}
    assert reply["reply_to"] == "m1"
    assert reply["addressed"] is False
    await h.stop()


async def test_reply_to_a_root_from_before_the_bridge_reads_the_root(chat):
    chat.add("Old topic")
    h = await started(chat)
    chat.add("bump", thread_of="m1")
    await h.until(lambda: h.ins())
    assert h.ins()[0]["thread"] == {"key": "m1", "title": "Old topic"}
    await h.stop()


async def test_reply_in_the_operators_thread_is_addressed(chat):
    h = await started(chat)
    chat.add("my question", sender=ME)
    chat.add("answer", thread_of="m1")
    await h.until(lambda: len(h.ins()) == 2)
    assert h.ins()[1]["addressed"] is True
    await h.stop()


async def test_quoted_message_is_the_reply(chat):
    h = await started(chat)
    chat.add("root")
    chat.add("inner", thread_of="m1")
    chat.add("quoting", thread_of="m1", quotedMessageMetadata={"name": f"{SPACE}/messages/m2"})
    await h.until(lambda: len(h.ins()) == 3)
    assert h.ins()[2]["reply_to"] == "m2"
    assert h.ins()[2]["thread"]["key"] == "m1"
    await h.stop()


# --- outbound -----------------------------------------------------------------


async def test_agent_message_goes_out_with_prefix_and_name(chat):
    h = await started(chat)
    h.out(318, "hi all")
    await h.until(lambda: h.of("sent"))
    assert chat.texts() == ["🤖 secretary: hi all"]
    assert h.of("sent") == [{"type": "sent", "id": 318, "ext_id": "m1"}]
    assert chat.creates[0]["message_id"].startswith("client-ag-")
    assert chat.creates[0]["message_id"].endswith("-318-0")
    await h.stop()


async def test_operator_message_carries_the_prefix_alone(chat):
    h = await started(chat)
    h.out(5, "from the web app", author={"name": "azamat", "kind": "human"})
    await h.until(lambda: h.of("sent"))
    assert chat.texts() == ["🤖 from the web app"]
    await h.stop()


async def test_own_message_only_moves_the_cursor_after_sent(chat):
    h = await started(chat)
    h.out(7, "hello")
    await h.until(lambda: h.of("sent") and h.lines[-1].get("type") == "in")
    sent_at = h.lines.index(h.of("sent")[0])
    cursor_lines = [i for i, line in enumerate(h.lines) if line.get("cursor", "").endswith("|m1")]
    assert cursor_lines and cursor_lines[0] > sent_at
    assert h.ins() == []
    await h.stop()


async def test_long_message_is_split_into_parts_in_one_thread(chat):
    h = await started(chat)
    chat.add("root")
    await h.until(lambda: h.ins())
    text = ("word " * (MAX_MESSAGE_UTF16 // 5)) + "\n" + ("tail " * 50)
    h.out(9, text, thread="m1", reply_to="m1")
    await h.until(lambda: h.of("sent"))
    assert len(chat.creates) == 2
    assert all(c["text"].startswith("🤖 secretary: ") for c in chat.creates)
    assert {c["thread"] for c in chat.creates} == {chat.by_id("m1")["thread"]["name"]}
    assert [c["message_id"][-2:] for c in chat.creates] == ["-0", "-1"]
    await h.stop()


async def test_reply_without_thread_key_goes_into_the_replied_messages_thread(chat):
    h = await started(chat)
    chat.add("root")
    chat.add("inner", thread_of="m1")
    await h.until(lambda: len(h.ins()) == 2)
    h.out(10, "answer", reply_to="m2")
    await h.until(lambda: h.of("sent"))
    assert chat.creates[0]["thread"] == chat.by_id("m1")["thread"]["name"]
    await h.stop()


async def test_reply_in_an_unthreaded_space_goes_out_plain():
    chat = FakeChat(threaded=False)
    h = await started(chat)
    chat.add("root")
    await h.until(lambda: h.ins())
    h.out(11, "answer", reply_to="m1")
    await h.until(lambda: h.of("sent"))
    assert chat.creates[0]["thread"] is None
    await h.stop()


async def test_refused_send_answers_failed(chat):
    h = await started(chat)
    chat.fail_create.append(api_error(403, "The caller does not have permission"))
    h.out(12, "nope")
    await h.until(lambda: h.of("failed"))
    failed = h.of("failed")[0]
    assert failed["id"] == 12 and "permission" in failed["error"]
    await h.stop()


async def test_empty_message_answers_failed(chat):
    h = await started(chat)
    h.out(13, "  \n")
    await h.until(lambda: h.of("failed"))
    assert h.of("failed")[0]["error"] == "empty message"
    assert chat.creates == []
    await h.stop()


async def test_out_id_handed_twice_is_sent_once(chat):
    h = await started(chat)
    h.out(14, "once")
    h.out(14, "once")
    await h.until(lambda: h.of("sent"))
    await asyncio.sleep(0.05)
    assert chat.texts() == ["🤖 secretary: once"]
    await h.stop()


async def test_lost_answer_is_claimed_after_reconnect_not_sent_twice(chat):
    h = await started(chat)
    chat.lose_answer = 1
    h.out(15, "maybe sent")
    with pytest.raises(TransportError):
        await asyncio.wait_for(h.task, 2)
    assert h.of("sent") == []
    h.start()  # the reconnect
    await h.until(lambda: h.of("sent"))
    assert chat.texts() == ["🤖 secretary: maybe sent"]
    assert h.of("sent") == [{"type": "sent", "id": 15, "ext_id": "m1"}]
    await h.stop()


async def test_message_handed_again_after_a_restart_is_claimed(chat):
    first = await started(chat)
    first.out(16, "hello")
    await first.until(lambda: first.of("sent"))
    await first.stop()
    second = Harness(chat)
    second.hello(first.of("in")[-1]["cursor"])
    second.out(16, "hello")  # the earlier process died before `sent` reached the hub
    second.start()
    await second.until(lambda: second.of("sent"))
    assert chat.texts() == ["🤖 secretary: hello"]
    assert second.of("sent") == [{"type": "sent", "id": 16, "ext_id": "m1"}]
    await second.stop()


async def test_client_id_taken_by_another_text_sends_under_a_new_id(chat):
    h = await started(chat)
    taken = f"{h.bridge.client_prefix}17-0"
    chat.add("🤖 old: from an earlier hub", sender=ME, client_id=taken)
    await h.until(lambda: chat.messages and h.bridge.cursor.id == "m1")
    h.out(17, "new message")
    await h.until(lambda: h.of("sent"))
    assert chat.texts()[-1] == "🤖 secretary: new message"
    assert chat.creates[-1]["message_id"].startswith(taken + "-")
    assert h.of("sent")[0]["ext_id"] == "m2"
    await h.stop()


# --- startup errors -----------------------------------------------------------


async def test_space_that_cannot_be_read_is_a_configuration_error(chat):
    chat.space_error = api_error(404, "Requested entity was not found")
    h = Harness(chat)
    h.hello()
    with pytest.raises(ConfigError):
        await asyncio.wait_for(h.bridge.serve(), 2)


async def test_space_read_failures_become_fatal_after_several_attempts(chat):
    chat.space_error = api_error(400, "bad")
    h = Harness(chat)
    h.hello()
    for _ in range(5):
        with pytest.raises(TransportError):
            await h.bridge.serve()
    with pytest.raises(ConfigError):
        await h.bridge.serve()


async def test_stopping_before_hello_returns(chat):
    h = Harness(chat)
    h.start()
    await h.stop()
    assert h.lines == []
