"""A fake of the Chat API the bridge talks to (`bridge.Api`), in memory."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from gchat_bridge.errors import ApiError, TransportError
from gchat_bridge.render import format_time, parse_time

SPACE = "spaces/AAAAexample"
ME = "users/100"
T0 = datetime(2026, 10, 10, 18, 0, 0, tzinfo=UTC)


class FakeChat:
    def __init__(self, *, threaded: bool = True, space_type: str = "SPACE") -> None:
        self.user = ME
        self.space = {
            "name": SPACE,
            "spaceType": space_type,
            "spaceThreadingState": "THREADED_MESSAGES" if threaded else "UNTHREADED_MESSAGES",
        }
        self.messages: list[dict[str, Any]] = []
        self.people = {ME: "Operator", "users/200": "Ada", "users/300": "Grace"}
        self.clock = T0
        self.counter = 0
        self.creates: list[dict[str, Any]] = []  # every create call
        self.fail_create: list[Exception] = []  # raised by the next creates, in order
        self.lose_answer = 0  # creates that succeed but raise TransportError
        self.space_error: Exception | None = None
        self.hidden: set[str] = set()  # names list_messages does not show yet
        self.list_errors: list[Exception] = []  # raised by the next list_messages calls
        self.person_errors: list[Exception] = []  # raised by the next get_person calls
        self.thread_list_errors: list[Exception] = []  # raised by thread lookups
        self.create_delay = 0.005  # a create yields, as a request would

    # --- test helpers -------------------------------------------------------

    def tick(self) -> str:
        self.clock += timedelta(seconds=1)
        return format_time(self.clock)

    def add(
        self,
        text: str,
        *,
        sender: str = "users/200",
        thread_of: str | None = None,
        client_id: str | None = None,
        at: datetime | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """A message written in Chat; `thread_of` is a message id to reply to."""
        self.counter += 1
        msg_id = f"m{self.counter}"
        if thread_of is not None:
            root = self.by_id(thread_of)
            thread = root["thread"]["name"]
            reply = True
        else:
            thread = f"{SPACE}/threads/t{self.counter}"
            reply = False
        msg = {
            "name": f"{SPACE}/messages/{msg_id}",
            "sender": {"name": sender, "type": "HUMAN"},
            "createTime": format_time(at) if at else self.tick(),
            "text": text,
            "thread": {"name": thread},
            "threadReply": reply and self.space["spaceThreadingState"] != "UNTHREADED_MESSAGES",
            **extra,
        }
        if client_id:
            msg["clientAssignedMessageId"] = client_id
        self.messages.append(msg)
        return msg

    def by_id(self, msg_id: str) -> dict[str, Any]:
        return next(m for m in self.messages if m["name"].endswith(f"/messages/{msg_id}"))

    def texts(self) -> list[str]:
        return [m["text"] for m in self.messages]

    # --- Api ----------------------------------------------------------------

    async def get_space(self, space: str) -> dict[str, Any]:
        if self.space_error:
            raise self.space_error
        return dict(self.space)

    async def list_messages(
        self,
        space: str,
        *,
        after: str | None = None,
        newest_first: bool = False,
        thread: str | None = None,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if thread and self.thread_list_errors:
            raise self.thread_list_errors.pop(0)
        if not thread and self.list_errors:
            raise self.list_errors.pop(0)
        found = [m for m in self.messages if m["name"] not in self.hidden]
        if after:
            bound = parse_time(after)
            found = [m for m in found if parse_time(m["createTime"]) > bound]
        if thread:
            found = [m for m in found if m["thread"]["name"] == thread]
        found.sort(key=lambda m: (parse_time(m["createTime"]), m["name"]), reverse=newest_first)
        start = int(page_token or 0)
        page = found[start : start + page_size]
        more = start + page_size < len(found)
        return [dict(m) for m in page], (str(start + page_size) if more else None)

    async def get_message(self, name: str) -> dict[str, Any] | None:
        for m in self.messages:
            if m["name"] == name or (
                m.get("clientAssignedMessageId")
                and name.endswith("/messages/" + m["clientAssignedMessageId"])
            ):
                return dict(m)
        return None

    async def create_message(
        self, space: str, text: str, *, message_id: str, thread: str | None = None
    ) -> tuple[dict[str, Any], bool]:
        self.creates.append({"text": text, "message_id": message_id, "thread": thread})
        if self.fail_create:
            raise self.fail_create.pop(0)
        existing = await self.get_message(f"{SPACE}/messages/{message_id}")
        if existing is not None:
            return existing, False
        self.counter += 1
        known = thread and any(m["thread"]["name"] == thread for m in self.messages)
        msg = {
            "name": f"{SPACE}/messages/m{self.counter}",
            "sender": {"name": ME, "type": "HUMAN"},
            "createTime": self.tick(),
            "text": text,
            "thread": {"name": thread if known else f"{SPACE}/threads/t{self.counter}"},
            "threadReply": bool(known),
            "clientAssignedMessageId": message_id,
        }
        self.messages.append(msg)
        # The message is in the space before its answer arrives.
        await asyncio.sleep(self.create_delay)
        if self.lose_answer:
            self.lose_answer -= 1
            raise TransportError("ReadTimeout")
        return dict(msg), True

    async def get_person(self, user: str) -> dict[str, Any] | None:
        if self.person_errors:
            raise self.person_errors.pop(0)
        name = self.people.get(user)
        return {"names": [{"displayName": name}]} if name else None


def api_error(status: int, message: str = "refused") -> ApiError:
    return ApiError(status, message)
