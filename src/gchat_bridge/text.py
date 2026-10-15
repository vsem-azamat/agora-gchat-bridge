"""Text helpers: lengths, visible characters, splitting and personas."""

from __future__ import annotations

import unicodedata
from typing import Any

MAX_PERSONA_UTF16 = 32  # Agora's longest agent name
PERSONA_SEPARATOR = ": "  # between the persona and the text


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def visible(text: str) -> str:
    """Text without control and format characters (line breaks and tabs stay)."""
    return "".join(
        ch for ch in text if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf")
    )


def cut_utf16(text: str, limit: int) -> str:
    """The longest start of `text` of at most `limit` UTF-16 units."""
    units = 0
    for i, ch in enumerate(text):
        units += utf16_len(ch)
        if units > limit:
            return text[:i]
    return text


def split_message(text: str, limit: int) -> list[str]:
    """Split text into pieces of at most `limit` UTF-16 units, preferring line
    breaks, then spaces."""
    chunks: list[str] = []
    rest = text
    while utf16_len(rest) > limit:
        cut = len(cut_utf16(rest, limit))
        window = rest[:cut]
        brk = max(window.rfind("\n"), window.rfind(" "))
        if brk > cut // 2:
            cut = brk + 1
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest.strip() or not chunks:
        chunks.append(rest)
    return chunks


def persona(author: Any) -> str:
    """The name an `out` line's author is shown under after the prefix: an
    agent's display name (else its name) on one line, cut to
    `MAX_PERSONA_UTF16`; "" for the operator (`kind` `human`) or an author
    without a usable name."""
    if not isinstance(author, dict) or author.get("kind") == "human":
        return ""
    for key in ("display", "name"):
        name = author.get(key)
        if not isinstance(name, str):
            continue
        name = " ".join(visible(name.replace(" ", " ").replace(" ", " ")).split())
        name = cut_utf16(name, MAX_PERSONA_UTF16).rstrip()
        if name:
            return name
    return ""


def short_reason(text: Any, limit: int = 200) -> str:
    """A one-line error reason for a `failed` line (never the message text)."""
    reason = " ".join(str(text).split())
    return reason[:limit] or "unknown error"
