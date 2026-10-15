"""An agent's text to Chat's: Markdown to Chat's formatting, @names to mentions.

Code spans and blocks are left as they are; Chat shows them the same way.
"""

from __future__ import annotations

import re

_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.DOTALL)
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_BOLD_MARK = "\x00"  # stands for Chat's bold `*` while italics are converted


def _outside_code(text: str, convert) -> str:
    parts: list[str] = []
    last = 0
    for match in _CODE.finditer(text):
        parts.append(convert(text[last : match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(convert(text[last:]))
    return "".join(parts)


def _markdown(text: str) -> str:
    text = _HEADING.sub(lambda m: f"{_BOLD_MARK}{m.group(1)}{_BOLD_MARK}", text)
    text = _BOLD.sub(lambda m: f"{_BOLD_MARK}{m.group(1)}{_BOLD_MARK}", text)
    text = _ITALIC.sub(r"_\1_", text)
    text = _STRIKE.sub(r"~\1~", text)
    text = _LINK.sub(r"<\2|\1>", text)
    return text.replace(_BOLD_MARK, "*")


def chat_markup(text: str) -> str:
    """Markdown as Chat writes it: `**b**` and headings as `*b*`, `*i*` as
    `_i_`, `~~s~~` as `~s~`, `[text](url)` as `<url|text>`."""
    return _outside_code(text, _markdown)


def mention_markup(text: str, members: dict[str, str]) -> str:
    """`@Name` as Chat's `<users/ID>` for the members of the space, given as
    name → users/ID (names compared without case). Longer names go first, so
    `@Ada Lovelace` wins over `@Ada`; a name must end at a word boundary."""
    if not members:
        return text
    by_lower = {name.casefold(): user for name, user in members.items() if name.strip()}
    names = sorted(by_lower, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![\w@])@(" + "|".join(re.escape(n) for n in names) + r")(?!\w)", re.IGNORECASE
    )

    def convert(chunk: str) -> str:
        return pattern.sub(lambda m: f"<{by_lower[m.group(1).casefold()]}>", chunk)

    return _outside_code(text, convert)


def member_names(people: dict[str, str]) -> dict[str, str]:
    """Names to mention members by, from users/ID → display name: each full
    name, and each first name that only one member has."""
    names: dict[str, str] = {}
    firsts: dict[str, set[str]] = {}
    for user, name in people.items():
        name = " ".join(name.split())
        if not name:
            continue
        names[name] = user
        first = name.split(" ", 1)[0]
        firsts.setdefault(first.casefold(), set()).add(user)
    full = {n.casefold() for n in names}
    for user, name in people.items():
        first = " ".join(name.split()).split(" ", 1)[0] if name.strip() else ""
        if first and len(firsts[first.casefold()]) == 1 and first.casefold() not in full:
            names[first] = user
    # A full name two members share names neither.
    counts: dict[str, int] = {}
    for name in people.values():
        key = " ".join(name.split()).casefold()
        counts[key] = counts.get(key, 0) + 1
    return {n: u for n, u in names.items() if counts.get(n.casefold(), 1) <= 1}
