from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path

import pytest

from agent_ops_telegram import cli
from agent_ops_telegram.config import load_config

from .conftest import ConfigFactory
from .fakes import TOKEN, FakeTelegram, FakeTmux


def run(
    argv: list[str], capsys: pytest.CaptureFixture[str], stdin: str | None = None, monkeypatch: pytest.MonkeyPatch | None = None
) -> tuple[int, str, str]:
    if stdin is not None:
        assert monkeypatch is not None
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = cli.main(argv)
    out, err = capsys.readouterr()
    assert TOKEN not in out and TOKEN not in err
    return code, out, err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert "aot 0.1.0" in capsys.readouterr().out


def test_send_text(make_config: ConfigFactory, tg: FakeTelegram, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_config()
    code, out, _ = run(["--config", str(cfg.path), "send", "build", "passed:", "$5"], capsys)
    assert code == 0 and out == "sent\n"
    assert tg.sent[0]["text"] == "build passed: $5"
    assert tg.sent[0]["chat_id"] == 42


def test_send_stdin_heredoc(
    make_config: ConfigFactory, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config()
    text = "Costs $5/month, `npm i` and $(date)\n" * 200
    code, out, _ = run(["--config", str(cfg.path), "send", "-"], capsys, stdin=text, monkeypatch=monkeypatch)
    assert code == 0 and out.startswith("sent (")
    assert "".join(m["text"] for m in tg.sent).replace("\n", "") == text.strip().replace("\n", "")


def test_send_json_and_photo(make_config: ConfigFactory, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    cfg = make_config()
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG....")
    code, out, _ = run(["--config", str(cfg.path), "send", "--json", "--photo", str(shot), "the menu"], capsys)
    assert code == 0
    assert json.loads(out)["ok"] is True
    assert tg.uploads[0]["params"]["caption"] == "the menu"
    assert tg.sent == []


def test_send_reply_to_with_photos_and_album(
    make_config: ConfigFactory, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    cfg = make_config()
    shots = []
    for i in range(3):
        shot = tmp_path / f"s{i}.png"
        shot.write_bytes(b"\x89PNG....")
        shots.append(str(shot))
    photo_args = [a for s in shots for a in ("--photo", s)]
    doc = tmp_path / "log.txt"
    doc.write_text("x")
    code, _, _ = run(["--config", str(cfg.path), "send", "--reply-to", "77", "--photo", shots[0], "one"], capsys)
    assert code == 0
    assert tg.uploads[-1]["params"]["reply_parameters"]["message_id"] == 77
    code, _, _ = run(["--config", str(cfg.path), "send", "album", "--reply-to", "78", *photo_args, "--document", str(doc)], capsys)
    assert code == 0
    album, document = tg.uploads[-2], tg.uploads[-1]
    assert album["method"] == "sendMediaGroup" and album["params"]["reply_parameters"]["message_id"] == 78
    assert "reply_parameters" not in document["params"]  # only the first message is the reply


def test_send_refuses_unlisted_chat(make_config: ConfigFactory, tg: FakeTelegram, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_config()
    code, _, err = run(["--config", str(cfg.path), "send", "--chat", "1234", "hi"], capsys)
    assert code == 2 and "allowed_chats" in err
    assert tg.sent == []


def test_send_failure_exit_code(make_config: ConfigFactory, tg: FakeTelegram, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_config()
    tg.fail("sendMessage", 403, "Forbidden: bot was blocked by the user")
    code, out, err = run(["--config", str(cfg.path), "send", "hi"], capsys)
    assert code == 1 and "blocked by the user" in err and "sent" not in out


def test_send_empty(make_config: ConfigFactory, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config()
    code, _, err = run(["--config", str(cfg.path), "send", "-"], capsys, stdin="  \n", monkeypatch=monkeypatch)
    assert code == 2 and "nothing to send" in err


def test_bad_config_is_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(["--config", str(tmp_path / "missing.toml"), "status"], capsys)
    assert code == 2 and "aot init" in err


def test_status_table_and_json(make_config: ConfigFactory, ftmux: FakeTmux, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_config()
    ftmux.add_session("agent")
    code, out, _ = run(["--config", str(cfg.path), "status"], capsys)
    assert code == 0
    assert out.splitlines()[0].split() == ["BOT", "RELAY", "SERVICE", "HEARTBEAT", "LAST_MESSAGE", "QUEUE", "TMUX"]
    assert "agent: up" in out and "stopped" in out
    code, out, _ = run(["--config", str(cfg.path), "status", "--json"], capsys)
    assert json.loads(out)[0]["queue"] == 0


def test_doctor_cli_json_exit_code(make_config: ConfigFactory, tg: FakeTelegram, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_config()
    code, out, _ = run(["--config", str(cfg.path), "doctor", "--json"], capsys)
    data = json.loads(out)
    assert code == 1 and data["ok"] is False  # no relay, no tmux session
    ids = {c["id"] for c in data["bots"][0]["checks"]}
    assert {"telegram.token_valid", "poller.single", "tmux.session", "disk.free"} <= ids


def test_doctor_cli_text(make_config: ConfigFactory, tg: FakeTelegram, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_config()
    _code, out, _ = run(["--config", str(cfg.path), "doctor", "--offline"], capsys)
    assert "FAIL poller.single" in out and "fix:" in out


def test_logs(make_config: ConfigFactory, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_config()
    code, _, err = run(["--config", str(cfg.path), "logs"], capsys)
    assert code == 1 and "no log yet" in err
    log = cfg.bot(None).bot_state_dir / "relay.log"
    log.parent.mkdir(parents=True)
    log.write_text("".join(f"line {i}\n" for i in range(100)))
    code, out, _ = run(["--config", str(cfg.path), "logs", "-n", "3"], capsys)
    assert out == "line 97\nline 98\nline 99\n"


def test_instructions(make_config: ConfigFactory, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_config()
    code, out, _ = run(["--config", str(cfg.path), "instructions"], capsys)
    assert code == 0
    assert "aot send --bot agent - <<'EOF'" in out
    assert "file=/path" in out


def test_skill_print_and_install(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, out, _ = run(["skill", "print"], capsys)
    assert code == 0 and not out.startswith("---") and "aot send" in out
    code, out, _ = run(["skill", "install", "--dir", str(tmp_path)], capsys)
    assert (tmp_path / "agent-ops-telegram" / "SKILL.md").read_text().startswith("---\nname: agent-ops-telegram")


def test_units_print_and_write(make_config: ConfigFactory, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    cfg = make_config()
    code, out, _ = run(["--config", str(cfg.path), "units", "--scope", "system", "--user", "alice"], capsys)
    assert "ExecStart=" in out and "relay --bot %i" in out and "User=alice" in out
    lines = out.splitlines()
    assert "KillMode=process" in lines  # restarting the relay must not kill the agent's tmux server
    assert not any(line.startswith(("PrivateTmp=", "NoNewPrivileges=", "UMask=")) for line in lines)
    assert any(line.startswith("Environment=PATH=") and ".local/bin" in line for line in lines)
    assert "Wants=network-online.target" in lines
    code, out, _ = run(["--config", str(cfg.path), "units", "--scope", "user", "--write", "--dir", str(tmp_path / "u")], capsys)
    assert code == 0
    names = sorted(p.name for p in (tmp_path / "u").iterdir())
    assert names == ["aot-relay@.service", "aot-watchdog.service", "aot-watchdog.timer"]
    relay = (tmp_path / "u" / "aot-relay@.service").read_text()
    assert "User=" not in relay and "WantedBy=default.target" in relay
    assert "network-online" not in relay  # no such target in the user manager
    assert "systemctl --user enable --now aot-relay@agent.service aot-watchdog.timer" in out


def test_watchdog_cli_dry_run(make_config: ConfigFactory, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_config('restart_command = ["true"]')
    code, out, _ = run(["--config", str(cfg.path), "watchdog", "--dry-run"], capsys)
    assert code == 0 and "agent: would restart the relay" in out


# ------------------------------------------------------------------ init


def init_args(tmp_path: Path, tg: FakeTelegram, *extra: str) -> list[str]:
    return [
        "--config",
        str(tmp_path / "conf" / "config.toml"),
        "init",
        "--yes",
        "--token-stdin",
        "--api-base",
        tg.base_url,
        "--units-dir",
        str(tmp_path / "units"),
        "--systemd",
        "user",
        "--state-dir",
        str(tmp_path / "st"),
        *extra,
    ]


def test_init_non_interactive(
    tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = init_args(
        tmp_path, tg, "--name", "app", "--chat", "42", "--chat", "43", "--start-command", "claude --continue", "--workdir", "/srv/app"
    )
    code, out, err = run(argv, capsys, stdin=TOKEN + "\n", monkeypatch=monkeypatch)
    assert code == 0, err
    assert "Token OK: @fake_bot" in out
    cfg_path = tmp_path / "conf" / "config.toml"
    env_path = tmp_path / "conf" / "secrets.env"
    assert stat.S_IMODE(cfg_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert TOKEN not in cfg_path.read_text()
    assert f"AOT_TOKEN_APP={TOKEN}" in env_path.read_text()
    cfg = load_config(cfg_path)
    bot = cfg.bot("app")
    assert bot.allowed_chats == [42, 43] and bot.start_command == "claude --continue" and bot.token() == TOKEN
    assert (tmp_path / "units" / "aot-relay@.service").exists()
    assert "systemctl --user enable --now aot-relay@app.service" in out

    # a second bot is appended, the first one is kept
    argv = init_args(tmp_path, tg, "--name", "web", "--chat", "42", "--start-command", "")
    code, out, err = run(argv, capsys, stdin="55555:" + "C" * 35 + "\n", monkeypatch=monkeypatch)
    assert code == 1 and "did not accept" in err  # the fake server only knows one token
    argv = init_args(tmp_path, tg, "--name", "web", "--chat", "42", "--start-command", "", "--offline")
    code, out, err = run(argv, capsys, stdin="55555:" + "C" * 35 + "\n", monkeypatch=monkeypatch)
    assert code == 0, err
    cfg = load_config(cfg_path)
    assert sorted(cfg.bots) == ["app", "web"]
    assert cfg.bot("web").token() == "55555:" + "C" * 35
    assert cfg.bot("app").token() == TOKEN


def test_init_rejects_bad_input(
    tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _, err = run(init_args(tmp_path, tg, "--name", "app", "--chat", "1"), capsys, stdin="not-a-token\n", monkeypatch=monkeypatch)
    assert code == 1 and "does not look like a bot token" in err
    code, _, err = run(init_args(tmp_path, tg, "--name", "Bad Name", "--chat", "1"), capsys, stdin=TOKEN + "\n", monkeypatch=monkeypatch)
    assert code == 1 and "lowercase" in err
    code, _, err = run(init_args(tmp_path, tg, "--name", "app"), capsys, stdin=TOKEN + "\n", monkeypatch=monkeypatch)
    assert code == 1 and "--chat" in err
    assert not (tmp_path / "conf" / "config.toml").exists()


def test_init_detects_chat(tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from agent_ops_telegram import init_wizard

    monkeypatch.setattr(init_wizard, "DETECT_SETTLE", 1.0)

    tg.add_message("an old message from a stranger", chat_id=666, username="stranger")
    threading.Timer(0.5, lambda: tg.add_message("hi bot", chat_id=777, username="me")).start()
    answers = iter(["app", "y", "", "", "user"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": TOKEN)
    argv = [a for a in init_args(tmp_path, tg) if a not in {"--yes", "--token-stdin"}]
    code, _out, err = run([*argv, "--start-command", ""], capsys)
    assert code == 0, err
    assert load_config(tmp_path / "conf" / "config.toml").bot("app").allowed_chats == [777]
    assert [u["message"]["chat"]["id"] for u in tg.updates] == [777]  # old update skipped, new one kept for the relay


def test_init_shows_every_chat_and_defaults_to_no(
    tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from agent_ops_telegram import init_wizard

    monkeypatch.setattr(init_wizard, "DETECT_SETTLE", 1.5)

    def both() -> None:
        tg.add_message("hi bot", chat_id=666, username="stranger", first_name="Eve")
        tg.add_message("it's me", chat_id=777, username="me")

    threading.Timer(0.3, both).start()
    prompts: list[str] = []

    def answer(prompt: str = "") -> str:
        prompts.append(prompt)
        return {0: "app"}.get(len(prompts) - 1, "")  # then just press Enter everywhere

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": TOKEN)
    argv = [a for a in init_args(tmp_path, tg) if a not in {"--yes", "--token-stdin"}]
    code, out, err = run([*argv, "--start-command", ""], capsys)
    # Two chats wrote: nothing is allowed by default, the stranger is never auto-accepted.
    assert code == 1 and "no chat allowed" in err
    assert "2 chat(s) wrote" in out and "private chat 666 @stranger" in out and "private chat 777 @me" in out
    assert all(p.endswith("[n]: ") for p in prompts if p.startswith("Allow"))
    assert not (tmp_path / "conf" / "config.toml").exists()


def test_detect_chats_keeps_listening_after_the_first_message() -> None:
    from typing import Any, cast

    from agent_ops_telegram.init_wizard import detect_chats
    from agent_ops_telegram.telegram import TelegramAPI

    t = {"now": 0.0}
    updates: list[dict[str, Any]] = [{"update_id": 1, "message": {"date": -1000, "chat": {"id": 666, "type": "private"}}}]
    offsets: list[int] = []

    class Api:
        def get_updates(self, offset: int, timeout: int, limit: int | None = None) -> list[dict[str, Any]]:
            offsets.append(offset)
            if offset == -1:
                return updates[-1:]
            return [u for u in updates if u["update_id"] >= offset]

    def msg(uid: int, chat: int, kind: str) -> dict[str, Any]:
        return {"update_id": uid, "message": {"date": t["now"], "chat": {"id": chat, "type": kind}, "from": {"id": 5}}}

    def sleep(s: float) -> None:
        t["now"] += s
        if t["now"] == 1.0:
            updates.append(msg(2, 777, "private"))
        if t["now"] == 3.0:
            updates.append(msg(3, -100, "group"))

    found = detect_chats(cast(TelegramAPI, Api()), io.StringIO(), 60, clock=lambda: t["now"], sleep=sleep, settle=5)
    assert [c.chat_id for c in found] == [-100, 777]  # the old 666 message is not offered
    assert t["now"] < 10  # stopped after the settle window, not the full 60 s
    assert set(offsets[1:]) == {2}  # nothing after the backlog was confirmed: the relay still gets both


def test_token_env_option(tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", TOKEN)
    argv = [a for a in init_args(tmp_path, tg, "--name", "app", "--chat", "1") if a != "--token-stdin"]
    i = argv.index("init")
    argv[i + 1 : i + 1] = ["--token-env", "MY_TOKEN"]
    code, _, err = run(argv, capsys)
    assert code == 0, err
    assert os.environ["MY_TOKEN"] == TOKEN


def test_init_refuses_reused_token_and_name_collision(
    tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = init_args(tmp_path, tg, "--name", "my-bot", "--chat", "42", "--start-command", "")
    assert run(ok, capsys, stdin=TOKEN + "\n", monkeypatch=monkeypatch)[0] == 0
    cfg_path = tmp_path / "conf" / "config.toml"
    before = cfg_path.read_text(), (tmp_path / "conf" / "secrets.env").read_text()
    code, _, err = run(
        init_args(tmp_path, tg, "--name", "other", "--chat", "42", "--start-command", ""),
        capsys,
        stdin=TOKEN + "\n",
        monkeypatch=monkeypatch,
    )
    assert code == 1 and "already configured as AOT_TOKEN_MY_BOT" in err
    second = "55555:" + "D" * 35 + "\n"
    code, _, err = run(
        init_args(tmp_path, tg, "--name", "my_bot", "--chat", "42", "--start-command", "", "--offline"),
        capsys,
        stdin=second,
        monkeypatch=monkeypatch,
    )
    assert code == 1 and "choose a different name" in err
    code, _, err = run(
        init_args(tmp_path, tg, "--name", "third", "--chat", "42", "--tmux", "my-bot", "--start-command", "", "--offline"),
        capsys,
        stdin=second,
        monkeypatch=monkeypatch,
    )
    assert code == 1 and "would be invalid" in err
    assert (cfg_path.read_text(), (tmp_path / "conf" / "secrets.env").read_text()) == before
    assert not list((tmp_path / "conf").glob(".config.toml.new"))


def test_init_start_command_with_control_chars(
    tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = init_args(tmp_path, tg, "--name", "app", "--chat", "42", "--start-command", 'run "x"\x7f\\y')
    code, _, err = run(argv, capsys, stdin=TOKEN + "\n", monkeypatch=monkeypatch)
    assert code == 0, err
    assert load_config(tmp_path / "conf" / "config.toml").bot("app").start_command == 'run "x"\x7f\\y'


def test_init_without_token_says_how(tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [a for a in init_args(tmp_path, tg, "--name", "app", "--chat", "1") if a != "--token-stdin"]
    code, _, err = run(argv, capsys)
    assert code == 1 and "--token-stdin" in err


def test_units_escape_percent_and_spaces(tmp_path: Path) -> None:
    from agent_ops_telegram import units

    text = units.relay_unit(tmp_path / "my dir" / "100%.toml", "system", user="bob", aot="/opt/my tools/aot")
    assert 'Environment="AOT_CONFIG=' in text and "100%%.toml" in text
    assert 'ExecStart="/opt/my tools/aot" relay --bot %i' in text
    assert "User=bob" in text


def test_init_env_file_must_match_existing_config(
    tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        run(
            init_args(tmp_path, tg, "--name", "one", "--chat", "42", "--start-command", ""),
            capsys,
            stdin=TOKEN + "\n",
            monkeypatch=monkeypatch,
        )[0]
        == 0
    )
    argv = init_args(
        tmp_path, tg, "--name", "two", "--chat", "42", "--start-command", "", "--offline", "--env-file", str(tmp_path / "elsewhere.env")
    )
    code, _, err = run(argv, capsys, stdin="55555:" + "E" * 35 + "\n", monkeypatch=monkeypatch)
    assert code == 1 and "already uses env_file" in err


def test_init_refuses_special_config_path(
    tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    fifo = tmp_path / "cfg.fifo"
    os.mkfifo(fifo)
    argv = init_args(tmp_path, tg, "--name", "one", "--chat", "42", "--offline")
    argv[1] = str(fifo)
    code, _, err = run(argv, capsys, stdin=TOKEN + "\n", monkeypatch=monkeypatch)
    assert code == 1 and "not a regular file" in err


def test_send_rejects_negative_retries(make_config: ConfigFactory, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_config()
    with pytest.raises(SystemExit) as info:
        cli.main(["--config", str(cfg.path), "send", "--retries", "-1", "hi"])
    assert info.value.code == 2


def test_status_reports_missing_unit(
    make_config: ConfigFactory, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_ops_telegram import services

    cfg = make_config(defaults_extra="")
    cfg.bot(None).systemd = "user"
    monkeypatch.setattr(services, "systemctl_available", lambda: True)
    monkeypatch.setattr(services, "systemctl", lambda scope, *a, **k: (3, "not-found" if "show" in a else "inactive"))
    monkeypatch.setattr(cli, "_config", lambda args: cfg)
    _, out, _ = run(["status"], capsys)
    assert "not installed" in out


def test_write_units_refuses_symlinks(tmp_path: Path) -> None:
    from agent_ops_telegram import units

    target = tmp_path / "victim"
    target.write_text("keep")
    target.chmod(0o600)
    d = tmp_path / "units"
    d.mkdir()
    (d / "aot-watchdog.timer").symlink_to(target)  # what `systemctl mask` leaves behind (to /dev/null)
    with pytest.raises(OSError, match="symlink"):
        units.write_units(d, units.all_units(tmp_path / "c.toml", "user"))
    assert target.read_text() == "keep" and (target.stat().st_mode & 0o777) == 0o600
    assert not (d / "aot-relay@.service").exists()


def test_init_declining_every_chat_is_a_clean_error(
    tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from agent_ops_telegram import init_wizard

    monkeypatch.setattr(init_wizard, "DETECT_SETTLE", 1.0)
    threading.Timer(0.3, lambda: tg.add_message("hi", chat_id=777)).start()
    answers = iter(["app", "n"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": TOKEN)
    argv = [a for a in init_args(tmp_path, tg) if a not in {"--yes", "--token-stdin"}]
    code, _, err = run(argv, capsys)
    assert code == 1 and "no chat allowed" in err


def test_init_group_with_allowed_user(
    tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = init_args(tmp_path, tg, "--name", "app", "--chat", "-1001", "--allowed-user", "42", "--start-command", "")
    code, out, err = run(argv, capsys, stdin=TOKEN + "\n", monkeypatch=monkeypatch)
    assert code == 0, err
    assert "Warning" not in out
    assert load_config(tmp_path / "conf" / "config.toml").bot("app").allowed_users == [42]
    assert "--config " in out  # non-default config path is carried into the next steps


def test_units_reject_bad_user_and_control_chars(make_config: ConfigFactory, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    from agent_ops_telegram import units

    cfg = make_config()
    for bad in ["bob\nExecStartPre=/bin/sh -c evil", "-root", "a b", "bob;id"]:
        code, out, err = run(["--config", str(cfg.path), "units", "--scope", "system", f"--user={bad}"], capsys)
        assert code == 2 and "invalid user name" in err and "ExecStartPre" not in out
    with pytest.raises(units.UnitError, match="control character"):
        units.all_units(tmp_path / "evil\nExecStartPre=/bin/id" / "c.toml", "system", user="bob")
    with pytest.raises(units.UnitError):
        units.relay_unit(tmp_path / "c.toml", "user", aot="/opt/aot\n/bin/id")
    assert units.check_user("svc-agent.1") == "svc-agent.1"
    text = units.relay_unit(tmp_path / "c.toml", "system", user="bob")
    assert "RestartPreventExitStatus=78" in text


def test_relay_without_token_exits_78(make_config: ConfigFactory, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    cfg = make_config()
    (tmp_path / "secrets.env").write_text("# token removed\n")
    code, _, err = run(["--config", str(cfg.path), "relay", "--bot", "agent"], capsys)
    assert code == cli.EXIT_CONFIG == 78
    assert "AOT_TOKEN_AGENT is not set" in err and "will not restart" in err
    health = json.loads((cfg.bot(None).bot_state_dir / "health.json").read_text())
    assert health["fatal_reason"] == "config_error" and "AOT_TOKEN_AGENT" in health["fatal"]


def test_relay_with_rejected_token_exits_78_without_waiting(
    make_config: ConfigFactory, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(token="123456:" + "r" * 35)  # the fake server only knows TOKEN: 401
    monkeypatch.setattr("time.sleep", lambda s: pytest.fail("must not sleep before exiting"))
    code, _, err = run(["--config", str(cfg.path), "relay", "--bot", "agent"], capsys)
    assert code == cli.EXIT_CONFIG
    assert "401 Unauthorized" in err and "AOT_TOKEN_AGENT" in err and "r" * 35 not in err
    health = json.loads((cfg.bot(None).bot_state_dir / "health.json").read_text())
    assert "401" in health["fatal"]  # the watchdog reads this and does not restart the relay
    assert health["fatal_reason"] == "token_rejected"


def test_watchdog_verbose_reports_a_relay_it_cannot_restart(make_config: ConfigFactory, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = make_config()
    code, out, _ = run(["--config", str(cfg.path), "watchdog", "-v"], capsys)
    assert code == 0 and "relay not running (no restart method configured)" in out and "healthy" not in out


def test_log_dirs_created_by_aot_are_private(tmp_path: Path) -> None:
    from agent_ops_telegram.logs import setup_logging

    root = tmp_path / "fresh-state"
    setup_logging("agent", root / "agent", stream=io.StringIO())
    assert stat.S_IMODE(root.stat().st_mode) == 0o700 and stat.S_IMODE((root / "agent").stat().st_mode) == 0o700


def test_init_next_steps_quote_the_workdir(
    tmp_path: Path, tg: FakeTelegram, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = init_args(tmp_path, tg, "--name", "app", "--chat", "42", "--start-command", "claude", "--workdir", "/srv/my app; rm -rf ~")
    code, out, err = run(argv, capsys, stdin=TOKEN + "\n", monkeypatch=monkeypatch)
    assert code == 0, err
    assert ">> '/srv/my app; rm -rf ~/CLAUDE.md'   (or AGENTS.md)" in out


def test_readme_init_sample_matches_the_real_output(tmp_path: Path, tg: FakeTelegram, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from agent_ops_telegram import init_wizard
    from agent_ops_telegram.config import default_config_path

    home = tmp_path / "you"
    (home / "project").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.chdir(home / "project")
    real_euid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real_euid or 1000)  # the sample shows a non-root run
    monkeypatch.setattr(init_wizard, "DETECT_SETTLE", 0.5)
    transcript = io.StringIO()
    answers = iter(["app", "", "", "", "", ""])

    def inp(prompt: str = "") -> str:
        answer = next(answers)
        transcript.write(prompt + answer + "\n")
        return answer

    def getpass(prompt: str = "") -> str:
        transcript.write(prompt + "\n")
        return TOKEN

    monkeypatch.setattr("getpass.getpass", getpass)
    threading.Timer(0.3, lambda: tg.add_message("hi", chat_id=123456789, user_id=123456789, username="you")).start()
    opts = init_wizard.InitOptions(config=default_config_path(), api_base=tg.base_url)
    assert init_wizard.run_init(opts, out=transcript, inp=inp) == 0
    printed = transcript.getvalue().replace(str(home), "/home/you").replace("@fake_bot", "@my_agent_bot")
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    sample = readme.split("$ aot init\n", 1)[1].split("```", 1)[0]
    assert [line.rstrip() for line in printed.splitlines()] == sample.rstrip("\n").splitlines()
