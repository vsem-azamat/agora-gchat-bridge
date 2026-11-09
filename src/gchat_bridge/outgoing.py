"""An agent's text to Chat's: Markdown to Chat's formatting, @names to mentions.

Code spans and blocks are left as they are; Chat shows them the same way. A
fence at the start of a line with no closing fence runs to the end of the text;
a lone ``` inside a line is text.
"""

from __future__ import annotations

import re

_CODE = re.compile(r"```.*?```|^[ \t]*```.*\Z|`[^`\n]+`", re.DOTALL | re.MULTILINE)
_BOLD_ITALIC = re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
# A URL may hold one level of balanced parentheses, as Wikipedia's do.
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://(?:[^()\s]|\([^()\s]*\))+)\)")
# What a mention must not touch besides code: URLs and Chat's own <…> markup.
_NO_MENTION = re.compile(r"<[^<>\n]*>|https?://\S+")
# Where a mention may start: an @ that does not follow a word or another @.
_MENTION_AT = re.compile(r"(?<![\w@])@")
_WORD_CHAR = re.compile(r"\w")
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


def _link(match: re.Match[str]) -> str:
    """`[text](url)` as `<url|text>`; as written when Chat's markup cannot
    hold it (a `|` in the URL, a `>` in the text)."""
    text, url = match.group(1), match.group(2)
    if "|" in url or ">" in text or "<" in text:
        return match.group(0)
    return f"<{url}|{text}>"


def _markdown(text: str) -> str:
    text = _HEADING.sub(lambda m: f"{_BOLD_MARK}{m.group(1)}{_BOLD_MARK}", text)
    text = _BOLD_ITALIC.sub(lambda m: f"{_BOLD_MARK}_{m.group(1)}_{_BOLD_MARK}", text)
    text = _BOLD.sub(lambda m: f"{_BOLD_MARK}{m.group(1)}{_BOLD_MARK}", text)
    text = _ITALIC.sub(r"_\1_", text)
    text = _STRIKE.sub(r"~\1~", text)
    text = _LINK.sub(_link, text)
    return text.replace(_BOLD_MARK, "*")


def chat_markup(text: str) -> str:
    """Markdown as Chat writes it: `**b**` and headings as `*b*`, `*i*` as
    `_i_`, `***bi***` as `*_bi_*`, `~~s~~` as `~s~`, `[text](url)` as `<url|text>`."""
    return _outside_code(text, _markdown)


def _fold(name: str) -> str:
    """A name as mentions and the name table compare it: case folded, so
    `STRASSE` and `Straße` are the same name."""
    return name.casefold()


def mention_markup(text: str, members: dict[str, str]) -> str:
    """`@Name` as Chat's `<users/ID>` for the members of the space, given as
    name → users/ID (names compared case folded). Longer names go first, so
    `@Ada Lovelace` wins over `@Ada`; a name must end at a word boundary.
    Code, URLs and Chat's `<…>` markup are left alone."""
    by_fold = {_fold(name): user for name, user in members.items() if name.strip()}
    if not by_fold:
        return text
    # Folding never shortens text, so a name is at most this many characters.
    longest = max(len(name) for name in by_fold)

    def mention_at(chunk: str, start: int) -> tuple[str, int] | None:
        """The member mentioned by the name at `start` and where it ends."""
        for end in range(min(len(chunk), start + longest), start, -1):
            if end < len(chunk) and _WORD_CHAR.match(chunk, end):
                continue
            user = by_fold.get(_fold(chunk[start:end]))
            if user:
                return user, end
        return None

    def convert_plain(chunk: str) -> str:
        parts: list[str] = []
        last = 0
        for at in _MENTION_AT.finditer(chunk):
            if at.start() < last:
                continue
            found = mention_at(chunk, at.end())
            if found:
                parts.append(chunk[last : at.start()])
                parts.append(f"<{found[0]}>")
                last = found[1]
        parts.append(chunk[last:])
        return "".join(parts)

    def convert(chunk: str) -> str:
        parts: list[str] = []
        last = 0
        for skip in _NO_MENTION.finditer(chunk):
            parts.append(convert_plain(chunk[last : skip.start()]))
            parts.append(skip.group(0))
            last = skip.end()
        parts.append(convert_plain(chunk[last:]))
        return "".join(parts)

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
        firsts.setdefault(_fold(first), set()).add(user)
    full = {_fold(n) for n in names}
    for user, name in people.items():
        first = " ".join(name.split()).split(" ", 1)[0] if name.strip() else ""
        if first and len(firsts[_fold(first)]) == 1 and _fold(first) not in full:
            names[first] = user
    # A full name two members share names neither.
    counts: dict[str, int] = {}
    for name in people.values():
        key = _fold(" ".join(name.split()))
        counts[key] = counts.get(key, 0) + 1
    return {n: u for n, u in names.items() if counts.get(_fold(n), 1) <= 1}
