"""Command line and the optional config file."""

from __future__ import annotations

import argparse
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gchat_bridge.errors import ConfigError
from gchat_bridge.text import utf16_len, visible

ROBOT = "\U0001f916"
DEFAULT_PREFIX = f"{ROBOT} "
MAX_PREFIX_UTF16 = 64  # the split limit leaves this much room below Chat's limit
DEFAULT_CONFIG_FILE = "~/.config/agora-gchat-bridge/config.toml"
DEFAULT_TOKEN_FILE = "~/.local/share/agora-gchat-bridge/token.json"
DEFAULT_POLL_SECONDS = 5.0
MIN_POLL_SECONDS = 2.0  # Chat's per-space read quota is generous, but not unlimited
DEFAULT_RECONCILE_SECONDS = 60.0
MIN_RECONCILE_SECONDS = 10.0
SPACE_NAME = re.compile(r"spaces/[A-Za-z0-9_-]{1,64}")

# Keys the config file may set, with their types; the space is per bridge and
# stays on the command line.
FILE_KEYS: dict[str, tuple[type, ...]] = {
    "token": (str,),
    "client_secret": (str,),
    "poll": (int, float),
    "reconcile": (int, float),
    "prefix": (str,),
}
LOGIN_KEYS = frozenset({"client_secret"})  # read by `gchat-bridge login` only


@dataclass(frozen=True)
class Config:
    space: str
    token_file: Path
    poll_seconds: float = DEFAULT_POLL_SECONDS
    reconcile_seconds: float = DEFAULT_RECONCILE_SECONDS
    prefix: str = DEFAULT_PREFIX


def parse_space(value: str) -> str:
    """A space's resource name only (`spaces/AAAA…`): a display name can be
    shared by several spaces or change."""
    value = value.strip()
    if not SPACE_NAME.fullmatch(value):
        raise ConfigError(
            "--space must be a space's resource name such as spaces/AAAAexample "
            "(find it with `gchat-bridge spaces`)"
        )
    return value


def parse_prefix(value: str) -> str:
    if not visible(value).strip():
        raise ConfigError("--prefix must have visible characters")
    if utf16_len(value) > MAX_PREFIX_UTF16:
        raise ConfigError(f"--prefix is longer than {MAX_PREFIX_UTF16} UTF-16 units")
    return value


def expand(value: str) -> Path:
    return Path(os.path.expanduser(value))


def load_config_file(path: Path, *, required: bool) -> dict[str, Any]:
    """Defaults from a TOML file; a missing default file is no error."""
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        if required:
            raise ConfigError(f"the config file {path} does not exist") from None
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the config file {path} is not valid TOML ({exc})") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read the config file {path} ({type(exc).__name__})") from exc
    values: dict[str, Any] = {}
    for key, value in data.items():
        types = FILE_KEYS.get(key)
        if types is None:
            raise ConfigError(f"unknown key in the config file: {key}")
        if isinstance(value, bool) or not isinstance(value, types):
            raise ConfigError(f"wrong type for {key} in the config file")
        values[key] = value
    return values


def file_defaults(argv: list[str] | None) -> dict[str, Any]:
    """The config file's values: `--config` names it, else the default file
    is read when it exists."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    known, rest = pre.parse_known_args(argv)
    config_path = known.config if known.config is not None else DEFAULT_CONFIG_FILE
    try:
        return load_config_file(expand(config_path), required=known.config is not None)
    except ConfigError:
        if "-h" not in rest and "--help" not in rest:
            raise
        return {}  # help works with a broken config file


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help="TOML file with defaults for the other options (read when it exists)",
    )
    parser.add_argument(
        "--token",
        default=DEFAULT_TOKEN_FILE,
        help="the token file written by `gchat-bridge login`",
    )


def parse_args(argv: list[str] | None = None) -> Config:
    file_values = file_defaults(argv)
    parser = argparse.ArgumentParser(
        prog="gchat-bridge",
        description="Agora bridge for one Google Chat space (JSON lines on stdin/stdout).",
        epilog="Other commands: `gchat-bridge login` writes the token file, "
        "`gchat-bridge spaces` lists your spaces with their resource names.",
    )
    parser.add_argument("--space", required=True, help="the space, e.g. spaces/AAAAexample")
    add_common_options(parser)
    parser.add_argument(
        "--poll",
        type=float,
        default=DEFAULT_POLL_SECONDS,
        help=f"seconds between reads of new messages (at least {MIN_POLL_SECONDS:.0f})",
    )
    parser.add_argument(
        "--reconcile",
        type=float,
        default=DEFAULT_RECONCILE_SECONDS,
        help="seconds between safety reads that look back for late messages",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help="text in front of every message the bridge sends (default: the robot emoji)",
    )
    parser.set_defaults(**{k: v for k, v in file_values.items() if k not in LOGIN_KEYS})
    args = parser.parse_args(argv)
    return Config(
        space=parse_space(args.space),
        token_file=expand(args.token),
        poll_seconds=max(MIN_POLL_SECONDS, float(args.poll)),
        reconcile_seconds=max(MIN_RECONCILE_SECONDS, float(args.reconcile)),
        prefix=parse_prefix(args.prefix),
    )
