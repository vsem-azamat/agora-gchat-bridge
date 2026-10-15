import pytest

from gchat_bridge.config import DEFAULT_PREFIX, parse_args
from gchat_bridge.errors import ConfigError
from gchat_bridge.render import message_text, parse_time, thread_title
from gchat_bridge.text import persona, split_message, utf16_len


def test_defaults():
    config = parse_args(["--space", "spaces/AAAAexample"])
    assert config.space == "spaces/AAAAexample"
    assert config.prefix == DEFAULT_PREFIX
    assert config.poll_seconds == 5.0
    assert config.token_file.name == "token.json"


@pytest.mark.parametrize("space", ["AAAAexample", "Team chat", "spaces/", "spaces/a b"])
def test_space_must_be_a_resource_name(space):
    with pytest.raises(ConfigError):
        parse_args(["--space", space])


def test_prefix_needs_visible_characters_and_a_bound():
    with pytest.raises(ConfigError):
        parse_args(["--space", "spaces/A", "--prefix", "​ "])
    with pytest.raises(ConfigError):
        parse_args(["--space", "spaces/A", "--prefix", "x" * 65])


def test_config_file_gives_defaults_and_command_line_wins(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('prefix = "[bot] "\npoll = 1\nclient_secret = "cs.json"\n')
    config = parse_args(["--space", "spaces/A", "--config", str(path)])
    assert config.prefix == "[bot] "
    assert config.poll_seconds == 2.0  # the minimum
    config = parse_args(["--space", "spaces/A", "--config", str(path), "--prefix", "> "])
    assert config.prefix == "> "


def test_config_file_rejects_unknown_keys(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('space = "spaces/A"\n')
    with pytest.raises(ConfigError, match="unknown key"):
        parse_args(["--space", "spaces/A", "--config", str(path)])


def test_split_respects_the_limit_and_prefers_line_breaks():
    text = "a" * 30 + "\n" + "b" * 30
    assert split_message(text, 40) == ["a" * 30, "b" * 30]
    parts = split_message("😀" * 50, 21)
    assert all(utf16_len(p) <= 21 for p in parts) and "".join(parts) == "😀" * 50


def test_persona():
    assert persona({"name": "secretary", "kind": "agent", "display": "Sec\nretary"}) == "Sec retary"
    assert persona({"name": "azamat", "kind": "human"}) == ""
    assert persona({"name": "x" * 40, "kind": "agent"}) == "x" * 32


def test_parse_time_takes_nanoseconds():
    assert parse_time("2026-10-10T18:00:00.123456789Z").microsecond == 123456
    assert parse_time("garbage") is None


def test_text_and_title():
    msg = {"text": "", "attachment": [{"contentName": "doc.pdf", "contentType": "application/pdf"}]}
    assert message_text(msg, {}) == "[file doc.pdf]"
    assert thread_title({"text": "x" * 100}, {}).endswith("…")
