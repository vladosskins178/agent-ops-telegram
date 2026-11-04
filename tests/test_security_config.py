from __future__ import annotations

import logging
from pathlib import Path

import pytest

from agent_ops_telegram.config import ConfigError, load_config
from agent_ops_telegram.security import InsecureFileError, RedactingFilter, check_private_file, parse_env_file, redact, register_secret

from .conftest import ConfigFactory
from .fakes import TOKEN


def test_redact_tokens_anywhere() -> None:
    assert TOKEN not in redact(f"GET https://api.telegram.org/bot{TOKEN}/getMe failed")
    assert redact("plain text 12:34") == "plain text 12:34"


def test_redact_registered_secret() -> None:
    register_secret("hunter2-very-secret")
    assert redact("password hunter2-very-secret!") == "password <redacted>!"


def test_logging_filter_redacts_args_and_exceptions(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("aot.redact-test")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="aot.redact-test"):
        logger.info("url %s", f"https://x/bot{TOKEN}/getMe")
        try:
            raise RuntimeError(f"boom {TOKEN}")
        except RuntimeError:
            logger.exception("failed")
    assert TOKEN not in caplog.text
    assert "<redacted" in caplog.text


def test_logging_filter_keeps_the_traceback(tmp_path: Path) -> None:
    import io

    from agent_ops_telegram.logs import setup_logging

    stream = io.StringIO()
    logger = setup_logging("tb-test", None, stream=stream)

    def inner() -> None:
        raise ValueError(f"bad token {TOKEN} in url")

    try:
        inner()
    except ValueError:
        logger.exception("request failed")
    text = stream.getvalue()
    assert TOKEN not in text
    assert "Traceback (most recent call last)" in text and "in inner" in text  # frames are kept
    assert "ValueError: bad token <redacted" in text


def test_private_file_checks(tmp_path: Path) -> None:
    f = tmp_path / "x.env"
    f.write_text("A=1\n")
    f.chmod(0o600)
    check_private_file(f)
    f.chmod(0o640)
    with pytest.raises(InsecureFileError):
        check_private_file(f)
    check_private_file(f, allow_group_read=True)
    f.chmod(0o644)
    with pytest.raises(InsecureFileError, match="chmod 600"):
        check_private_file(f, allow_group_read=True)


def test_parse_env_file(tmp_path: Path) -> None:
    f = tmp_path / "s.env"
    f.write_text("# comment\nexport A='one'\nB=\"two\"\r\nC=three=3\n\nnoequals\n")
    f.chmod(0o600)
    assert parse_env_file(f) == {"A": "one", "B": "two", "C": "three=3"}


def test_parse_env_file_inline_comments(tmp_path: Path) -> None:
    f = tmp_path / "s.env"
    f.write_text(
        "A=one # the first\nB='two # kept' # quoted values keep their hash\nC=\"three\"\t# tab before the comment\nD=four#five\nE='six'\n"
    )
    f.chmod(0o600)
    assert parse_env_file(f) == {"A": "one", "B": "two # kept", "C": "three", "D": "four#five", "E": "six"}


def test_config_loads_with_defaults(make_config: ConfigFactory) -> None:
    cfg = make_config()
    bot = cfg.bot(None)
    assert bot.name == "agent"
    assert bot.token_env == "AOT_TOKEN_AGENT"
    assert bot.token() == TOKEN
    assert bot.allowed_chats == [42]
    assert bot.alert_chat == 42
    assert bot.session == "agent"
    assert bot.unit_name == "aot-relay@agent.service"


def test_world_readable_config_refused(make_config: ConfigFactory) -> None:
    with pytest.raises(ConfigError, match="chmod 600"):
        make_config(mode=0o644)


def test_world_readable_env_file_refused(make_config: ConfigFactory, tmp_path: Path) -> None:
    cfg = make_config()
    (tmp_path / "secrets.env").chmod(0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        cfg.bot(None).token()


def test_token_from_environment_wins(make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config()
    monkeypatch.setenv("AOT_TOKEN_AGENT", "999:" + "B" * 35)
    assert cfg.bot(None).token() == "999:" + "B" * 35


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("allowed_chats = []", "allowed_chats is empty"),
        ('allowed_chats = ["42"]', "integer ids"),
        ("default_chat = 7", "default_chat must be one of"),
        ("poll_timeout = 99", "poll_timeout"),
        ("typo_setting = 1", "unknown setting 'typo_setting'"),
        ("ack_reaction = 5", "must be a string"),
        ('blocked_patterns = ["("]', "bad regex"),
        ('prompt_template = "{nope}"', "prompt_template is invalid"),
        ('command_prefix = "aot"', "must start with '/'"),
    ],
)
def test_config_validation(make_config: ConfigFactory, extra: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        make_config(extra)


def test_missing_config_mentions_init(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="aot init"):
        load_config(tmp_path / "nope.toml")


def test_several_bots_need_a_name(make_config: ConfigFactory, tmp_path: Path, ftmux: object) -> None:
    cfg = make_config('[bots.second]\nallowed_chats = [7]\ntmux_target = "second"')
    assert sorted(cfg.bots) == ["agent", "second"]
    with pytest.raises(ConfigError, match="pass --bot"):
        cfg.bot(None)
    assert cfg.bot("second").token_env == "AOT_TOKEN_SECOND"
    with pytest.raises(ConfigError, match="no bot named"):
        cfg.bot("third")


def test_two_bots_cannot_share_a_session(make_config: ConfigFactory) -> None:
    with pytest.raises(ConfigError, match="same tmux session"):
        make_config('[bots.second]\nallowed_chats = [7]\ntmux_target = "agent:1"')


def test_api_base_must_be_url(make_config: ConfigFactory) -> None:
    with pytest.raises(ConfigError, match="api_base"):
        make_config(defaults_extra='api_base = "api.telegram.org"')


def test_unreadable_config_is_a_config_error(make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config()

    def boom(self: Path, *a: object, **k: object) -> str:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(cfg.path)


def test_redact_token_inside_url_without_registration() -> None:
    other = "987654321:" + "Q" * 35  # never registered
    assert other not in redact(f"curl https://api.telegram.org/bot{other}/getMe")


def test_session_names_tmux_would_rename_are_refused(make_config: ConfigFactory) -> None:
    with pytest.raises(ConfigError, match="session name"):
        make_config('tmux_target = "my.app"')


def test_write_private_refuses_special_files(tmp_path: Path) -> None:
    import os

    from agent_ops_telegram.security import write_private

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(OSError, match="not a regular file"):
        write_private(fifo, "x")
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "target")
    with pytest.raises(OSError, match="not a regular file"):
        write_private(link, "x")
    assert not (tmp_path / "target").exists()
