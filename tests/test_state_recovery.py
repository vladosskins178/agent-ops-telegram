"""Recovery from lost or rewritten state: an offset with no known owner, clock steps,
a read-only log directory, read-only status checks and leftover ``.done`` files."""

from __future__ import annotations

import errno
import io
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_ops_telegram import cli
from agent_ops_telegram import state as state_mod
from agent_ops_telegram.config import Config
from agent_ops_telegram.relay import Relay
from agent_ops_telegram.state import BotState, PollerLock, state_lock_path
from agent_ops_telegram.watchdog import watchdog_bot

from .conftest import Clock, ConfigFactory
from .fakes import FakeTelegram, FakeTmux

RelayFactory = Callable[..., Relay]


def texts(ftmux: FakeTmux) -> list[str]:
    return [s.split("]: ", 1)[1] for s in ftmux.session("agent").get("submitted", [])]


def _update(update_id: int, text: str, date: int = 1_700_000_000, **extra: Any) -> dict[str, Any]:
    msg = {"message_id": 7, "date": date, "chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": text, **extra}
    return {"update_id": update_id, "message": msg}


# ---------------------------------------------------------------- 1. unknown owner, nothing held


def test_unknown_owner_with_nothing_held_starts_from_zero(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    """A plain offset far above the new bot's ids and no health.json: Telegram holds
    nothing yet, so nothing proves a switch, but keeping the old offset would hide every
    message the new bot ever gets."""
    ftmux.add_session("agent")
    relay = make_relay()
    relay.state.ensure()
    relay.state.offset_file.write_text("999999\n")
    relay.startup()
    relay.cycle()
    tg.add_message("first message to the new bot")  # update id 1000
    relay.cycle()
    assert texts(ftmux) == ["first message to the new bot"]


# ---------------------------------------------------------------- 2. no false switch


def test_lost_owner_record_does_not_rerun_a_command(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    """Same bot, but the offset was rewritten as a plain number and health.json is gone
    while Telegram still holds the last batch: the command in it must not run again."""
    ftmux.add_session("agent")
    relay = make_relay()
    relay.startup()
    tg.add_message("/aot keys C-c")
    tg.add_message("some text")
    relay.cycle()
    assert ftmux.session("agent")["keys"] == ["C-c"]
    offset = json.loads(relay.state.offset_file.read_text())["offset"]
    relay.state.offset_file.write_text(f"{offset}\n")
    relay.state.health_file.unlink()
    again = make_relay()
    again.startup()
    again.cycle()
    again.cycle()
    assert ftmux.session("agent")["keys"] == ["C-c"]


def test_held_updates_that_would_not_be_queued_are_no_evidence(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    """Held updates below the offset from chats outside the allow-list, /start and relay
    commands were never queued: they do not suggest another bot."""
    ftmux.add_session("agent")
    relay = make_relay()
    relay.state.ensure()
    tg.add_message("stranger", chat_id=99, user_id=99)
    tg.add_message("/start")
    tg.add_message("/aot status")
    relay.state.offset_file.write_text(f"{tg.next_update_id}\n")
    relay.state.mark_command(tg.next_update_id - 1, time.time())
    relay.startup()
    relay.cycle()
    assert not any("different bot" in m["text"] for m in tg.sent)
    assert relay.state.commands_file.exists()
    assert json.loads(relay.state.offset_file.read_text())["bot_id"] == "1"


def test_suspected_switch_keeps_the_command_mark(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    """Evidence alone (a held message we never queued) resets the offset, but only a
    confirmed bot id mismatch forgets which commands already ran."""
    ftmux.add_session("agent")
    relay = make_relay()
    relay.state.ensure()
    relay.state.offset_file.write_text("999999\n")
    relay.state.mark_command(5, time.time())
    relay.state.enqueue(_update(999990, "queued before the restart"))
    tg.add_message("held for the new bot")
    relay.startup()
    relay.cycle()
    assert texts(ftmux) == ["queued before the restart", "held for the new bot"]
    assert relay.state.commands_file.exists() and not list(relay.state.root.glob("queue.*"))
    (notice,) = [m["text"] for m in tg.sent if "could not tell which bot" in m["text"]]
    assert "offset 0" in notice


# ---------------------------------------------------------------- 3. clock steps


def test_identify_retry_survives_a_clock_step_back(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux, clock: Clock) -> None:
    ftmux.add_session("agent")
    relay = make_relay()
    relay.state.ensure()
    relay.state.offset_file.write_text("999999\n")
    tg.fail("getMe", 502, "Bad Gateway", times=3)
    relay.startup()
    clock.step(-3600)
    for _ in range(5):
        relay.cycle()
    assert "getUpdates" in tg.methods()


def test_resolve_retry_survives_a_clock_step_back(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux, clock: Clock) -> None:
    ftmux.add_session("agent")
    relay = make_relay()
    relay.state.ensure()
    relay.state.offset_file.write_text("999999\n")
    tg.fail("getUpdates", 502, "Bad Gateway", times=3)
    relay.startup()
    assert relay._owner_unknown
    clock.step(-3600)
    for _ in range(5):
        relay.cycle()
    assert not relay._owner_unknown


def test_notify_cooldown_survives_a_clock_step_back(make_relay: RelayFactory, tg: FakeTelegram, clock: Clock) -> None:
    relay = make_relay()
    relay.notify("k", "one")
    clock.step(-3600)
    clock.sleep(relay.bot.notify_cooldown + 1)
    relay.notify("k", "two")
    assert [m["text"] for m in tg.sent] == ["one", "two"]


def test_heartbeat_records_monotonic_time_and_boot(make_relay: RelayFactory, tg: FakeTelegram, clock: Clock) -> None:
    relay = make_relay()
    relay.startup()
    relay.cycle()
    health = relay.state.read_health()
    assert health["heartbeat_mono"] == clock.monotonic() and health["boot_id"] == state_mod.boot_id()


@pytest.fixture
def running_lock(make_config: ConfigFactory) -> Any:
    cfg = make_config()
    bot = cfg.bot(None)
    lock = PollerLock(state_lock_path(bot.bot_state_dir))
    lock.acquire()
    yield cfg
    lock.release()


def test_watchdog_ignores_a_wall_clock_step_forward(running_lock: Config, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    """The wall clock jumped an hour ahead, but the monotonic clock says the heartbeat is fresh."""
    bot = running_lock.bot(None)
    ftmux.add_session("agent")
    mono = time.monotonic()
    BotState(bot.bot_state_dir).write_health(
        heartbeat=1000.0, started=0.0, heartbeat_mono=mono - 5, started_mono=mono - 9000, boot_id=state_mod.boot_id()
    )
    assert watchdog_bot(bot, now=1000.0 + 3600, mono_now=mono) == []


def test_watchdog_treats_a_future_heartbeat_as_unknown(running_lock: Config, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    bot = running_lock.bot(None)
    ftmux.add_session("agent")
    BotState(bot.bot_state_dir).write_health(heartbeat=99_000.0, started=0.0)  # written before a step back
    assert watchdog_bot(bot, now=1000.0) == []


def test_watchdog_uses_wall_time_after_a_reboot(
    running_lock: Config, tg: FakeTelegram, ftmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_ops_telegram import services

    bot = running_lock.bot(None)
    ftmux.add_session("agent")
    monkeypatch.setattr(services, "restart_relay", lambda b: (True, ""))
    BotState(bot.bot_state_dir).write_health(heartbeat=1000.0, started=0.0, heartbeat_mono=1e9, started_mono=0.0, boot_id="another-boot")
    actions = watchdog_bot(bot, now=2000.0, mono_now=5.0)
    assert actions and "stuck" in actions[0]


# ---------------------------------------------------------------- 4. read-only log directory


def test_setup_logging_falls_back_to_stderr_on_a_read_only_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_ops_telegram import logs

    def erofs(path: Path) -> Path:
        raise OSError(errno.EROFS, "Read-only file system", str(path))

    monkeypatch.setattr(logs, "ensure_private_dir", erofs)
    stream = io.StringIO()
    logger = logs.setup_logging("agent", tmp_path / "ro", stream=stream)
    logger.info("still logging")
    assert [type(h) for h in logger.handlers] == [logging.StreamHandler]
    assert "Read-only file system" in stream.getvalue() and "still logging" in stream.getvalue()


def test_relay_starts_with_a_read_only_log_dir(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agent_ops_telegram import logs

    cfg = make_config()

    def erofs(path: Path) -> Path:
        raise OSError(errno.EROFS, "Read-only file system", str(path))

    monkeypatch.setattr(logs, "ensure_private_dir", erofs)
    ran: list[bool] = []
    monkeypatch.setattr(Relay, "run", lambda self, stop=None: ran.append(True))
    assert cli.main(["--config", str(cfg.path), "relay", "--bot", "agent"]) == cli.EXIT_OK
    assert ran == [True]
    assert "Read-only file system" in capsys.readouterr().err


# ---------------------------------------------------------------- 5. read-only status


def test_status_and_doctor_do_not_modify_the_queue(make_config: ConfigFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    from agent_ops_telegram.doctor import run_doctor

    cfg = make_config()
    state = BotState(cfg.bot(None).bot_state_dir)
    state.ensure()
    state.enqueue(_update(5, "kept"))
    (state.queue_dir / "000000000000099.json").write_text("{broken")
    state.delivered_file.write_text(json.dumps(["000000000000777.json|1|1.0"]))
    before = sorted(p.name for p in state.root.rglob("*"))
    delivered = state.delivered_file.read_text()
    run_doctor(cfg.bot(None))
    assert cli.main(["--config", str(cfg.path), "status"]) in (0, 1)
    fresh = BotState(cfg.bot(None).bot_state_dir)
    assert fresh.oldest_queued() is not None and fresh.queue_len() == 2
    assert sorted(p.name for p in state.root.rglob("*")) == before
    assert state.delivered_file.read_text() == delivered


# ---------------------------------------------------------------- 6. fatal note vs. a running relay


def test_fatal_note_not_written_while_another_relay_runs(
    make_config: ConfigFactory, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_config()
    bot = cfg.bot(None)
    (tmp_path / "secrets.env").write_text("# token removed\n")
    lock = PollerLock(state_lock_path(bot.bot_state_dir))
    lock.acquire()
    try:
        assert cli.main(["--config", str(cfg.path), "relay", "--bot", "agent"]) == cli.EXIT_CONFIG
    finally:
        lock.release()
    assert "fatal" not in BotState(bot.bot_state_dir).read_health()
    assert "AOT_TOKEN_AGENT is not set" in capsys.readouterr().err


# ---------------------------------------------------------------- 7. .done cleanup, lost offset + health


def test_old_done_files_are_removed_on_startup(make_relay: RelayFactory, clock: Clock) -> None:
    relay = make_relay()
    relay.state.ensure()
    old = relay.state.queue_dir / "000000000000001.done"
    new = relay.state.queue_dir / "000000000000002.done"
    for path in (old, new):
        path.write_text(json.dumps({"seq": 1, "update": _update(1, "x")}))
    os.utime(old, (clock.now - 8 * 86400, clock.now - 8 * 86400))
    os.utime(new, (clock.now - 86400, clock.now - 86400))
    relay.startup()
    assert not old.exists() and new.exists()


def test_done_records_are_not_queued_again(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    relay = make_relay()
    relay.state.ensure()
    update = _update(1000, "typed already")
    item = relay.state.enqueue(update)
    assert item is not None
    item.path.rename(item.path.with_suffix(".done"))
    fresh = BotState(relay.state.root)
    assert fresh.enqueue(update) is None and fresh.queue_len() == 0


def test_lost_offset_and_health_with_a_queue_keeps_the_backlog(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    relay = make_relay("skip_backlog = true")
    relay.state.ensure()
    relay.state.enqueue(_update(900, "queued before"))
    tg.add_message("sent while the relay was down")
    ftmux.add_session("agent")
    relay.startup()
    relay.cycle()
    assert texts(ftmux) == ["queued before", "sent while the relay was down"]
