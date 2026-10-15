"""The bridge: Agora's JSON lines on one side, one Google Chat space on the other.

Inbound: after `hello`, read the space after the hub's cursor, then read again
every `poll_seconds`, each read looking back a little before the cursor for
messages that became visible late, and every `reconcile_seconds` further back.
Messages already reported in this process are skipped by id; the hub skips
repeats by `ext_id` anyway.

Outbound: each `out` line becomes a send job of one or more parts with the
prefix and the agent's name in front. Every part carries a client-assigned
message id derived from the bridge and the hub's message id, so a part sent
twice (a retry after a lost answer, a message the hub hands again after a
restart) is refused by Chat as already existing and claimed instead. `sent` is
written before any read can report the new message, because reads and sends
hold the same lock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from gchat_bridge.config import DEFAULT_PREFIX, MAX_PREFIX_UTF16
from gchat_bridge.errors import ApiError, ConfigError, TransportError
from gchat_bridge.render import (
    format_time,
    mentions,
    message_id,
    message_text,
    parse_time,
    thread_title,
    user_id,
)
from gchat_bridge.text import (
    MAX_PERSONA_UTF16,
    PERSONA_SEPARATOR,
    persona,
    short_reason,
    split_message,
    utf16_len,
    visible,
)

log = logging.getLogger("gchat_bridge")

PROTOCOL_VERSION = 3  # the Agora bridge protocol version this bridge speaks, sent in `hello`
CAPS = {"personas": "prefix"}  # agents are named in the text after the prefix
THREADS_CAP = "thread"  # Chat threads are Agora threads, keyed by their root message
THREADED_STATES = ("THREADED_MESSAGES", "GROUPED_MESSAGES")
ASSISTANT_ID = "assistant"
# Chat takes 4096 characters of text; leave room for the longest prefix,
# persona and separator (counted in UTF-16 units, the stricter measure).
MAX_TEXT_UTF16 = 4096
MAX_MESSAGE_UTF16 = (
    MAX_TEXT_UTF16 - MAX_PREFIX_UTF16 - MAX_PERSONA_UTF16 - utf16_len(PERSONA_SEPARATOR)
)
CLIENT_ID_PREFIX = "client-ag-"  # Chat requires client-assigned ids to start with client-
CLIENT_ID_HASH_CHARS = 8  # hex digits of the bridge's hash, and of a part's text after a clash
MAX_ERROR_CHARS = 480  # of a `failed` line's reason; the hub keeps 500
POLL_LOOKBACK = timedelta(seconds=10)
RECONCILE_LOOKBACK = timedelta(minutes=2)
SEEN_LIMIT = 5000  # message ids remembered as reported
CACHE_LIMIT = 2000  # thread roots and names remembered
NAME_TTL_SECONDS = 24 * 3600.0
STARTUP_ATTEMPTS = 6
READ_ERROR_PAUSE_SECONDS = 5.0


class Api(Protocol):
    """What the bridge needs of Chat (`chat_api.ChatApi`)."""

    @property
    def user(self) -> str: ...

    async def get_space(self, space: str) -> dict[str, Any]: ...

    async def list_messages(
        self,
        space: str,
        *,
        after: str | None = None,
        newest_first: bool = False,
        thread: str | None = None,
        page_size: int = 100,
        page_token: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]: ...

    async def get_message(self, name: str) -> dict[str, Any] | None: ...

    async def create_message(
        self, space: str, text: str, *, message_id: str, thread: str | None = None
    ) -> tuple[dict[str, Any], bool]: ...

    async def get_person(self, user: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True, order=True)
class Cursor:
    """A position in the space: a message's creation time and id."""

    at: datetime
    id: str = ""

    def __str__(self) -> str:
        return f"{format_time(self.at)}|{self.id}"

    @classmethod
    def parse(cls, value: Any) -> Cursor | None:
        if not isinstance(value, str) or "|" not in value:
            return None
        at, _, msg = value.partition("|")
        parsed = parse_time(at)
        return cls(parsed, msg) if parsed else None


@dataclass
class Root:
    """A thread's root message, as far as the bridge needs it."""

    id: str
    thread: str  # spaces/S/threads/T
    sender: str
    marked: bool  # the bridge's or an assistant's message
    title: str


@dataclass
class SendJob:
    out_id: int
    chunks: list[str]
    persona: str = ""
    root: str | None = None  # the message whose thread every part goes into
    thread: str | None = None  # that thread, or the one the first part started
    done: int = 0
    ids: list[str] = field(default_factory=list)


def _remember(ids: OrderedDict[str, None], msg_id: str) -> None:
    """Add an id to a bounded set that forgets the oldest first."""
    ids[msg_id] = None
    ids.move_to_end(msg_id)
    while len(ids) > SEEN_LIMIT:
        ids.popitem(last=False)


def _raise_lost_access(exc: ApiError) -> None:
    """A space the account may no longer read is a configuration error."""
    if exc.status in (403, 404):
        raise ConfigError(f"cannot read the space: {short_reason(exc)}") from exc


def _parse_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _ext(value: Any) -> str | None:
    """A message id from an `out` line (a string or a number), or None."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    return text if re.fullmatch(r"[A-Za-z0-9._-]{1,200}", text) else None


def _text_hash(text: str) -> str:
    """A short hash of a part's text, for a client id that stays the same
    when the part is sent again."""
    return hashlib.sha256(text.encode()).hexdigest()[:CLIENT_ID_HASH_CHARS]


def _same_text(a: str, b: str) -> bool:
    return " ".join(visible(a).split()) == " ".join(visible(b).split())


class Bridge:
    def __init__(
        self,
        api: Api,
        space: str,
        write: Callable[[dict[str, Any]], None],
        *,
        prefix: str = DEFAULT_PREFIX,
        poll_seconds: float = 5.0,
        reconcile_seconds: float = 60.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.api = api
        self.space = space
        self.prefix = prefix
        self._write = write
        self.poll_seconds = poll_seconds
        self.reconcile_seconds = reconcile_seconds
        self._now = now
        self.caps: dict[str, str] = dict(CAPS)
        self.hello: dict[str, Any] | None = None
        self.hello_seen = asyncio.Event()
        self.closed = asyncio.Event()
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.cursor: Cursor | None = None
        self.space_type = ""
        self.threaded = False
        self._resolved = False
        self._primed = False
        self._seen: OrderedDict[str, None] = OrderedDict()
        # Messages the hub has: reported here, sent here, or read before the
        # hub's cursor (an earlier process reported them). History before an
        # empty cursor is seen but not the hub's.
        self._known: OrderedDict[str, None] = OrderedDict()
        self._history_known = False
        self._roots: OrderedDict[str, Root] = OrderedDict()  # by root message id
        self._thread_roots: OrderedDict[str, str] = OrderedDict()  # thread name → root id
        self._names: dict[str, tuple[str, float]] = {}
        self._accepted: set[int] = set()  # out ids taken in this process
        self._job: SendJob | None = None
        self._lock = asyncio.Lock()
        self._in_flight: asyncio.Future | None = None
        self._startup_failures = 0
        self._last_reconcile = 0.0

    # --- hub side ---------------------------------------------------------

    def emit(self, line: dict[str, Any]) -> None:
        self._write(line)

    def _emit_hello(self) -> None:
        self.emit({"type": "hello", "version": PROTOCOL_VERSION, "caps": self.caps})

    @property
    def client_prefix(self) -> str:
        """The start of the client-assigned ids of this bridge's messages."""
        name = str((self.hello or {}).get("bridge") or "")
        digest = hashlib.sha256(f"{name}\n{self.space}".encode()).hexdigest()
        digest = digest[:CLIENT_ID_HASH_CHARS]
        return f"{CLIENT_ID_PREFIX}{digest}-"

    def handle_hub_line(self, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("hub line is not JSON; skipped")
            return
        if not isinstance(line, dict):
            return
        kind = line.get("type")
        if kind == "hello":
            if self.hello is None:
                self.hello = line
                self.cursor = Cursor.parse(line.get("cursor"))
                self._history_known = self.cursor is not None
                if line.get("cursor") and self.cursor is None:
                    log.warning("hub cursor not understood; starting after the newest message")
                version = line.get("version")
                if version != PROTOCOL_VERSION:
                    log.warning("hub speaks protocol %s, this bridge %s", version, PROTOCOL_VERSION)
                log.info("hello: bridge %s, cursor %s", line.get("bridge"), self.cursor)
                self._emit_hello()
            self.hello_seen.set()
        elif kind == "out":
            out_id = _parse_id(line.get("id"))
            if out_id is None:
                log.warning("out line without a numeric id; skipped")
                return
            if out_id in self._accepted:
                log.info("message %s handed again; already taken", out_id)
                return
            self._accepted.add(out_id)
            self.outbox.put_nowait(line)

    # --- small caches -----------------------------------------------------

    def _mark_seen(self, msg_id: str, *, known: bool = True) -> None:
        """Remember a message as read, and as the hub's when `known`."""
        _remember(self._seen, msg_id)
        if known:
            _remember(self._known, msg_id)

    def _remember_root(self, root: Root) -> None:
        self._roots[root.id] = root
        self._thread_roots[root.thread] = root.id
        while len(self._roots) > CACHE_LIMIT:
            self._roots.popitem(last=False)
        while len(self._thread_roots) > CACHE_LIMIT:
            self._thread_roots.popitem(last=False)

    async def _name(self, user: str) -> str:
        cached = self._names.get(user)
        if cached and time.monotonic() - cached[1] < NAME_TTL_SECONDS:
            return cached[0]
        name = ""
        try:
            person = await self.api.get_person(user)
        except ApiError as exc:
            log.warning("name of %s not read: %s", user_id(user), short_reason(exc, 120))
            person = None
        for entry in (person or {}).get("names") or []:
            if isinstance(entry, dict) and isinstance(entry.get("displayName"), str):
                name = entry["displayName"].strip()
                if name:
                    break
        name = name or f"user {user_id(user)}"
        self._names[user] = (name, time.monotonic())
        return name

    async def _names_for(self, msg: dict[str, Any]) -> dict[str, str]:
        """Display names of the users a message's text mentions."""
        return {user: await self._name(user) for user in mentions(msg) if user != "users/all"}

    # --- reading ------------------------------------------------------------

    def _is_own(self, msg: dict[str, Any]) -> bool:
        """Sent by this bridge (its client-assigned id)."""
        client = msg.get("clientAssignedMessageId")
        return isinstance(client, str) and client.startswith(self.client_prefix)

    def _is_marked(self, msg: dict[str, Any]) -> bool:
        """The account's own message carrying the prefix: an assistant's."""
        if (msg.get("sender") or {}).get("name") != self.api.user:
            return False
        text = msg.get("text") if isinstance(msg.get("text"), str) else ""
        return visible(text).lstrip().startswith(visible(self.prefix).strip())

    def _root_of(self, msg: dict[str, Any], msg_id: str) -> Root | None:
        thread = (msg.get("thread") or {}).get("name")
        if not isinstance(thread, str) or not thread:
            return None
        sender = (msg.get("sender") or {}).get("name") or ""
        return Root(
            id=msg_id,
            thread=thread,
            sender=sender,
            marked=self._is_own(msg) or self._is_marked(msg),
            title="",
        )

    async def _thread_root(self, thread: str) -> Root | None:
        """The root of a thread: known, or its first message read from Chat."""
        root_id = self._thread_roots.get(thread)
        if root_id is not None and root_id in self._roots:
            return self._roots[root_id]
        messages, _ = await self.api.list_messages(self.space, thread=thread, page_size=1)
        if not messages or not isinstance(messages[0].get("name"), str):
            return None
        first = messages[0]
        root = self._root_of(first, message_id(first["name"]))
        if root is None:
            return None
        root.title = thread_title(first, await self._names_for(first))
        self._remember_root(root)
        return root

    async def _line_for(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_id = message_id(msg["name"])
        sender = msg.get("sender") or {}
        sender_name = sender.get("name") if isinstance(sender.get("name"), str) else ""
        names = await self._names_for(msg)
        own_account = sender_name == self.api.user
        marked = self._is_marked(msg)
        if marked:
            author: dict[str, Any] = {
                "id": ASSISTANT_ID,
                "name": f"assistant ({visible(self.prefix).strip()})",
                "self": False,
            }
        elif sender.get("type") == "BOT":
            display = sender.get("displayName")
            author = {
                "id": user_id(sender_name),
                "name": display if isinstance(display, str) and display else "app",
                "self": False,
            }
        else:
            author = {
                "id": user_id(sender_name),
                "name": await self._name(sender_name) if sender_name else "Unknown",
                "self": own_account,
            }
        line: dict[str, Any] = {
            "type": "in",
            "id": msg_id,
            "author": author,
            "text": message_text(msg, names),
        }
        at = parse_time(msg.get("createTime"))
        if at:
            line["at"] = format_time(at).replace(".000000Z", "Z")
        addressed = False
        if not own_account and not marked:
            if self.space_type == "DIRECT_MESSAGE":
                addressed = True
            elif mentions(msg) & {self.api.user, "users/all"}:
                addressed = True
        quoted = (msg.get("quotedMessageMetadata") or {}).get("name")
        if isinstance(quoted, str) and quoted.startswith(f"{self.space}/messages/"):
            line["reply_to"] = message_id(quoted)
        root = self._root_of(msg, msg_id)
        if self.threaded and msg.get("threadReply") and root is not None:
            try:
                thread_root = await self._thread_root(root.thread)
            except ApiError as exc:
                # Without its root the reply comes in as the room's, rather than
                # holding up every message after it.
                log.warning("root of %s not read: %s", msg_id, short_reason(exc, 120))
                thread_root = None
            if thread_root is not None and thread_root.id != msg_id:
                # The hub joins a new thread key to the thread of the message the
                # line replies to: a quoted message it may not have would start
                # a new thread instead.
                if line.get("reply_to") not in self._known:
                    line["reply_to"] = thread_root.id
                line["thread"] = {"key": thread_root.id, "title": thread_root.title}
                own_root = thread_root.sender == self.api.user and not thread_root.marked
                if own_root and not (own_account or marked):
                    addressed = True
        elif root is not None and not msg.get("threadReply"):
            root.title = thread_title(msg, names)
            self._remember_root(root)
        line["addressed"] = addressed
        return line

    async def read(self, lookback: timedelta) -> int:
        """Read the space from a little before the cursor and emit what is new;
        returns the count."""
        async with self._lock:
            return await self._read_locked(lookback)

    async def _read_locked(self, lookback: timedelta) -> int:
        assert self.cursor is not None
        after = format_time(self.cursor.at - lookback)
        emitted = 0
        token: str | None = None
        while True:
            messages, token = await self.api.list_messages(
                self.space, after=after, page_token=token
            )
            for msg in messages:
                name = msg.get("name")
                at = parse_time(msg.get("createTime"))
                if not isinstance(name, str) or at is None:
                    continue
                msg_id = message_id(name)
                if msg_id in self._seen:
                    continue
                if not self._primed and (at < self.cursor.at or msg_id == self.cursor.id):
                    self._mark_seen(msg_id, known=self._history_known)
                    continue  # before the start
                # Build the line first: when that fails (a name or a thread root
                # that cannot be read), the message stays unseen and the cursor
                # stays before it, so the next read reports it.
                line = None if self._is_own(msg) else await self._line_for(msg)
                self._mark_seen(msg_id)
                position = Cursor(at, msg_id)
                advanced = position > self.cursor
                if advanced:
                    self.cursor = position
                if line is None:
                    root = self._root_of(msg, msg_id)
                    if root is not None and not msg.get("threadReply"):
                        self._remember_root(root)
                    if advanced:
                        self.emit({"type": "in", "cursor": str(self.cursor)})
                    continue
                line["cursor"] = str(self.cursor)
                self.emit(line)
                emitted += 1
            if not token:
                break
        self._primed = True
        if emitted:
            log.info("read %d new message(s), cursor %s", emitted, self.cursor)
        return emitted

    # --- sending ------------------------------------------------------------

    def _part_text(self, job: SendJob, part: int) -> str:
        return (
            self.prefix
            + (job.persona + PERSONA_SEPARATOR if job.persona else "")
            + (job.chunks[part])
        )

    async def _thread_for(self, root_id: str) -> str | None:
        """The thread a reply to `root_id` goes into, None when it is unknown."""
        root = self._roots.get(root_id)
        if root is not None:
            return root.thread
        msg = await self.api.get_message(f"{self.space}/messages/{root_id}")
        if msg is None:
            return None
        root = self._root_of(msg, root_id)
        if root is None:
            return None
        if not msg.get("threadReply"):  # a reply names its thread, but is no root
            root.title = thread_title(msg, await self._names_for(msg))
            self._remember_root(root)
        return root.thread

    async def _process(self, line: dict[str, Any]) -> None:
        """Run one `out` line to an answer (the caller holds the lock)."""
        out_id = _parse_id(line.get("id"))
        text = line.get("text") if isinstance(line.get("text"), str) else ""
        if not visible(text).strip():
            self.emit({"type": "failed", "id": out_id, "error": "empty message"})
            return
        root = _ext(line.get("thread")) or _ext(line.get("reply_to"))
        self._job = SendJob(
            out_id=out_id,
            chunks=split_message(text, MAX_MESSAGE_UTF16),
            persona=persona(line.get("author")),
            root=root,
        )
        await self._run_job(self._job)

    async def _run_job(self, job: SendJob) -> None:
        """Send what is left of a job and answer it. A connection failure leaves
        the job in place, to be sent again after the reconnect: its parts'
        client-assigned ids make that safe."""
        if job.thread is None and job.root is not None and self.threaded:
            try:
                job.thread = await self._thread_for(job.root)
            except ApiError as exc:
                log.warning("thread of message %s not found: %s", job.out_id, short_reason(exc))
        while job.done < len(job.chunks):
            thread = job.thread
            text = self._part_text(job, job.done)
            client_id = f"{self.client_prefix}{job.out_id}-{job.done}"
            try:
                msg, new = await self.api.create_message(
                    self.space, text, message_id=client_id, thread=thread
                )
                if not new and not _same_text(str(msg.get("text") or ""), text):
                    # The id was used by another message (the hub's ids started
                    # over): this part was not sent yet.
                    log.warning("message %s: client id taken by another message", job.out_id)
                    msg, new = await self.api.create_message(
                        self.space,
                        text,
                        message_id=f"{client_id}-{_text_hash(text)}",
                        thread=thread,
                    )
            except ApiError as exc:
                self._finish_failed(job, short_reason(exc))
                return
            name = msg.get("name")
            if self.threaded and job.thread is None:
                # Every further part goes into the thread the first one started.
                started = (msg.get("thread") or {}).get("name")
                job.thread = started if isinstance(started, str) and started else None
            if isinstance(name, str):
                job.ids.append(message_id(name))
                _remember(self._known, message_id(name))
                root = self._root_of(msg, message_id(name))
                if root is not None and not msg.get("threadReply"):
                    root.title = thread_title(msg, {})
                    self._remember_root(root)
            if not new:
                log.info("message %s: part %d already in the space", job.out_id, job.done + 1)
            job.done += 1
        self._finish_sent(job)

    def _finish_sent(self, job: SendJob) -> None:
        line: dict[str, Any] = {"type": "sent", "id": job.out_id}
        if job.ids:
            line["ext_id"] = job.ids[0]
        self.emit(line)
        self._job = None
        log.info("message %s sent as %s", job.out_id, job.ids[0] if job.ids else "?")

    def _finish_failed(self, job: SendJob, reason: str) -> None:
        if job.done:
            reason = f"sent in part: {job.done} of {len(job.chunks)} ({reason})"
        self.emit({"type": "failed", "id": job.out_id, "error": reason[:MAX_ERROR_CHARS]})
        self._job = None
        log.warning("message %s not sent (%d of %d parts)", job.out_id, job.done, len(job.chunks))

    async def _process_locked(self, line: dict[str, Any]) -> None:
        async with self._lock:
            await self._process(line)
            # Report own messages and anything that came in meanwhile now,
            # rather than at the next poll.
            try:
                await self._read_locked(POLL_LOOKBACK)
            except ApiError as exc:
                _raise_lost_access(exc)
                log.warning("read after a send failed: %s", short_reason(exc, 120))

    # --- loops ------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            lookback = POLL_LOOKBACK
            if time.monotonic() - self._last_reconcile >= self.reconcile_seconds:
                lookback = RECONCILE_LOOKBACK
                self._last_reconcile = time.monotonic()
            try:
                await self.read(lookback)
            except ApiError as exc:
                _raise_lost_access(exc)
                log.warning("read failed: %s", short_reason(exc, 120))
                await asyncio.sleep(READ_ERROR_PAUSE_SECONDS)

    async def _outbound_loop(self) -> None:
        while True:
            line = await self.outbox.get()
            # Shielded: stopping must not cut a send between Chat and `sent`.
            self._in_flight = asyncio.ensure_future(self._process_locked(line))
            await asyncio.shield(self._in_flight)

    # --- lifecycle --------------------------------------------------------

    async def _resolve_space(self) -> None:
        try:
            space = await self.api.get_space(self.space)
        except ApiError as exc:
            if exc.status in (403, 404):
                raise ConfigError(f"cannot read the space: {short_reason(exc)}") from exc
            self._startup_failures += 1
            if self._startup_failures >= STARTUP_ATTEMPTS:
                raise ConfigError(f"cannot read the space: {short_reason(exc)}") from exc
            raise TransportError(short_reason(exc)) from exc
        self._startup_failures = 0
        self.space_type = str(space.get("spaceType") or space.get("type") or "")
        self.threaded = space.get("spaceThreadingState") in THREADED_STATES
        self._resolved = True
        log.info("space type %s, threaded %s", self.space_type, self.threaded)
        if self.threaded and "threads" not in self.caps:
            # The hub keeps the latest caps; it holds replies in threads for a
            # few seconds after `hello` until they are declared.
            self.caps = {**CAPS, "threads": THREADS_CAP}
            self._emit_hello()

    async def _start_cursor(self) -> None:
        """Without a cursor, start after the newest message (no history import)
        and report that position at once, so a restart keeps it."""
        messages, _ = await self.api.list_messages(self.space, newest_first=True, page_size=1)
        newest = messages[0] if messages else None
        at = parse_time((newest or {}).get("createTime"))
        if newest and at and isinstance(newest.get("name"), str):
            self.cursor = Cursor(at, message_id(newest["name"]))
        else:
            self.cursor = Cursor(self._now())
        self._primed = False
        self.emit({"type": "in", "cursor": str(self.cursor)})
        log.info("no cursor: starting at %s", self.cursor)

    async def _startup(self) -> None:
        if not self._resolved:
            await self._resolve_space()
        async with self._lock:
            if self.cursor is None:
                await self._start_cursor()
            if self._job is not None:
                await self._run_job(self._job)
            while not self.outbox.empty():
                await self._process(self.outbox.get_nowait())
            await self._read_locked(RECONCILE_LOOKBACK)
        self._last_reconcile = time.monotonic()

    async def _settle_in_flight(self, timeout: float | None) -> None:
        task, self._in_flight = self._in_flight, None
        if task is None:
            return
        if not task.done():
            await asyncio.wait({task}, timeout=timeout)
        if not task.done():
            task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        if isinstance(results[0], BaseException) and not isinstance(
            results[0], asyncio.CancelledError
        ):
            log.info("previous send ended with %s", type(results[0]).__name__)

    async def serve(self) -> None:
        """Run until stdin closes; transport errors propagate so the caller
        retries with backoff."""
        await self._settle_in_flight(None)
        hello = asyncio.ensure_future(self.hello_seen.wait())
        closed = asyncio.ensure_future(self.closed.wait())
        await asyncio.wait({hello, closed}, return_when=asyncio.FIRST_COMPLETED)
        hello.cancel()
        if self.closed.is_set():
            closed.cancel()
            return
        try:
            await self._startup()
        except BaseException:
            closed.cancel()
            raise
        tasks = {
            asyncio.ensure_future(self._poll_loop()),
            asyncio.ensure_future(self._outbound_loop()),
        }
        try:
            done, _ = await asyncio.wait(tasks | {closed}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not closed and task.exception() is not None:
                    raise task.exception()
        finally:
            for task in tasks | {closed}:
                task.cancel()
            await asyncio.gather(*tasks, closed, return_exceptions=True)
            if self.closed.is_set():
                # The hub kills the bridge 5 s after SIGTERM; finish the send first.
                await self._settle_in_flight(4.0)
