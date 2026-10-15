"""Chat message resources to the parts of Agora `in` lines."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from gchat_bridge.text import visible

MAX_TITLE_CHARS = 80  # a thread's title, from its root's text
_FRACTION = re.compile(r"(\.\d{1,6})\d*")
USER_MARKUP = re.compile(r"<(users/[A-Za-z0-9_-]+)>")
LINK_MARKUP = re.compile(r"<(https?://[^|>\s]+)\|([^>]*)>")


def message_id(name: str) -> str:
    """The id of a message within its space: spaces/S/messages/ID → ID."""
    return name.rsplit("/messages/", 1)[-1]


def user_id(user: str) -> str:
    """The numeric part of users/{id}."""
    return user.removeprefix("users/")


def parse_time(value: Any) -> datetime | None:
    """An RFC 3339 time from the API (up to nanoseconds) as an aware datetime,
    to the microsecond."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(_FRACTION.sub(r"\1", value, count=1))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def format_time(value: datetime) -> str:
    """RFC 3339 in UTC with microseconds, as the API's filters take it."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _attachment(att: dict[str, Any]) -> str:
    name = att.get("contentName") or ""
    kind = str(att.get("contentType") or "")
    if att.get("driveDataRef") and not kind:
        return f"[Drive file {name}]" if name else "[Drive file]"
    label = "file"
    for prefix, word in (("image/", "image"), ("video/", "video"), ("audio/", "audio")):
        if kind.startswith(prefix):
            label = word
    return f"[{label} {name}]" if name else f"[{label}]"


def placeholders(msg: dict[str, Any]) -> list[str]:
    """Short placeholders for what is not text: attachments, GIFs, cards."""
    parts = [_attachment(a) for a in msg.get("attachment") or [] if isinstance(a, dict)]
    parts += ["[GIF]" for g in msg.get("attachedGifs") or [] if isinstance(g, dict)]
    if (msg.get("cardsV2") or msg.get("cards")) and not msg.get("text"):
        fallback = msg.get("fallbackText")
        parts.append(f"[card] {fallback}" if isinstance(fallback, str) and fallback else "[card]")
    return parts


def message_text(msg: dict[str, Any], names: dict[str, str]) -> str:
    """The text of a message for Agora: placeholders first, then the text with
    user markup as @names and link markup as `text (url)`."""
    text = msg.get("text") if isinstance(msg.get("text"), str) else ""

    def user(match: re.Match[str]) -> str:
        return "@" + names.get(match.group(1), user_id(match.group(1)))

    text = USER_MARKUP.sub(user, text)
    text = LINK_MARKUP.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)
    return " ".join([*placeholders(msg), text]).strip()


def mentions(msg: dict[str, Any]) -> set[str]:
    """The users a message mentions (users/{id}; users/all for @all)."""
    found = set()
    for ann in msg.get("annotations") or []:
        if not isinstance(ann, dict) or ann.get("type") != "USER_MENTION":
            continue
        user = (ann.get("userMention") or {}).get("user") or {}
        if isinstance(user.get("name"), str):
            found.add(user["name"])
    return found


def thread_title(msg: dict[str, Any], names: dict[str, str]) -> str:
    """A thread's title: the first line of its root's text, cut short."""
    text = visible(message_text(msg, names)).strip()
    first = text.split("\n", 1)[0].strip()
    if len(first) > MAX_TITLE_CHARS:
        first = first[: MAX_TITLE_CHARS - 1].rstrip() + "…"
    return first
