"""Queue order, identity and disk-failure edge cases: clock steps, reused update ids,
token switches without health.json, failed deletes and read-only state directories."""

from __future__ import annotations

import errno
import json
import time
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_ops_telegram import state as state_mod
from agent_ops_telegram.relay import Relay

from .conftest import Clock
from .fakes import FakeTelegram, FakeTmux

RelayFactory = Callable[..., Relay]


def texts(ftmux: FakeTmux) -> list[str]:
    return [s.split("]: ", 1)[1] for s in ftmux.session("agent").get("submitted", [])]


def _update(update_id: int, text: str, date: int = 1_700_000_000, **extra: Any) -> dict[str, Any]:
    msg = {"message_id": 7, "date": date, "chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": text, **extra}
    return {"update_id": update_id, "message": msg}


@pytest.fixture
def wall(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Clock:
    """Make every wall-clock read in the state module follow the test clock."""
    monkeypatch.setattr(state_mod, "time", types.SimpleNamespace(time=clock, sleep=time.sleep))
    return clock


# ---------------------------------------------------------------- 1. order by sequence


def test_queue_order_survives_a_clock_step_back(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux, wall: Clock) -> None:
    relay = make_relay()
    relay.startup()
    tg.add_message("first")
    relay.cycle()  # session down: queued
    wall.step(-3600)  # NTP steps the clock back an hour
    tg.add_message("second")
    relay.cycle()
    ftmux.add_session("agent")
    relay.cycle()
    assert texts(ftmux) == ["first", "second"]


def test_flush_scope_survives_a_clock_step_back(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux, wall: Clock) -> None:
    ftmux.add_session("agent", screen=["Continue? (y/n)"])
    relay = make_relay()
    relay.startup()
    tg.add_message("before")
    relay.cycle()  # a dialog is open: queued
    wall.step(-3600)
    tg.add_message("/aot flush")
    tg.add_message("after")
    relay.cycle()
    assert texts(ftmux) == ["before"]
    assert "Delivered 1 queued message(s); 1 left." in [m["text"] for m in tg.sent]


def test_sequence_continues_after_a_restart(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    relay = make_relay()
    relay.startup()
    tg.add_message("one")
    relay.cycle()
    (relay.state.root / "seq").unlink()  # even without the counter file, numbering continues
    again = make_relay()
    again.startup()
    tg.add_message("two")
    again.cycle()
    seqs = [i.seq for i in again.state.pending()]
    assert len(seqs) == 2 and seqs[0] < seqs[1]
    ftmux.add_session("agent")
    again.cycle()
    assert texts(ftmux) == ["one", "two"]


def test_legacy_queue_files_are_kept_in_order(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    relay = make_relay()
    relay.startup()
    for uid, text, received in [(9000, "older", 10.0), (500, "newer", 20.0)]:
        path = relay.state.queue_dir / f"{uid:015d}.json"
        path.write_text(json.dumps({"update": _update(uid, text), "received": received}))
    tg.add_message("newest")
    relay.cycle()
    ftmux.add_session("agent")
    relay.cycle()
    assert texts(ftmux) == ["older", "newer", "newest"]


# ---------------------------------------------------------------- 2. switch without health.json


def test_token_switch_detected_without_health_json(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    """An offset (from an older version: a plain number) but no health.json: nothing says
    which bot it belongs to, so the relay must not poll with it before checking."""
    ftmux.add_session("agent")
    relay = make_relay()
    relay.state.ensure()
    relay.state.offset_file.write_text("999999\n")
    tg.next_update_id = 5000
    tg.add_message("sent to the new bot")
    relay.startup()
    relay.cycle()
    assert texts(ftmux) == ["sent to the new bot"]
    assert json.loads(relay.state.offset_file.read_text())["bot_id"] == "1"


def test_unknown_owner_waits_for_getme_before_polling(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux, clock: Clock) -> None:
    ftmux.add_session("agent")
    relay = make_relay()
    relay.state.ensure()
    relay.state.offset_file.write_text("999999\n")
    tg.next_update_id = 5000
    tg.add_message("sent to the new bot")
    tg.fail("getMe", 502, "Bad Gateway", times=2)
    relay.startup()
    relay.cycle()
    assert "getUpdates" not in tg.methods()
    clock.now += relay._identify_at - clock.monotonic()
    relay.cycle()
    relay.cycle()
    assert texts(ftmux) == ["sent to the new bot"]


def test_unknown_owner_same_bot_keeps_offset_and_queue(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    relay = make_relay()
    relay.state.ensure()
    relay.state.offset_file.write_text(f"{tg.next_update_id}\n")
    relay.state.enqueue(_update(tg.next_update_id - 1, "queued earlier"))
    tg.add_message("new")
    ftmux.add_session("agent")
    relay.startup()
    relay.cycle()
    assert texts(ftmux) == ["queued earlier", "new"]
    assert not any("different bot" in m["text"] for m in tg.sent)
    assert json.loads(relay.state.offset_file.read_text()) == {"offset": tg.next_update_id, "bot_id": "1"}


def test_offset_file_records_the_bot_id(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    relay = make_relay()
    relay.startup()
    tg.add_message("x")
    relay.cycle()
    assert json.loads(relay.state.offset_file.read_text()) == {"offset": tg.next_update_id, "bot_id": "1"}
    relay.state.health_file.unlink()
    relay.state.offset_file.write_text(json.dumps({"offset": 999999, "bot_id": "777"}))
    tg.next_update_id = 5000
    tg.add_message("for the new bot")
    ftmux.add_session("agent")
    again = make_relay()
    again.startup()
    again.cycle()
    assert texts(ftmux)[-1] == "for the new bot"


# ---------------------------------------------------------------- 3. failed delete after paste


def test_failed_queue_delete_after_paste_is_not_retyped(
    make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ftmux.add_session("agent")
    relay = make_relay()
    relay.startup()
    queue_dir = relay.state.queue_dir
    real_unlink, real_rename = Path.unlink, Path.rename

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.parent == queue_dir:
            raise OSError(errno.EROFS, "Read-only file system")
        real_unlink(self, missing_ok)

    def rename(self: Path, target: Any) -> Path:
        if self.parent == queue_dir:
            raise OSError(errno.EROFS, "Read-only file system")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "rename", rename)
    tg.add_message("type me once")
    with caplog.at_level("WARNING"):
        relay.cycle()
        relay.cycle()
        assert relay.deliver_pending() == 0
    assert texts(ftmux) == ["type me once"]
    assert relay.state.queue_len() == 0
    assert "could not remove" in caplog.text
    (path,) = list(queue_dir.glob("*.json"))
    assert "errors" not in json.loads(path.read_text())
    again = make_relay()  # a restart still knows it was delivered
    again.startup()
    again.cycle()
    assert texts(ftmux) == ["type me once"]


def test_failed_delete_falls_back_to_done_suffix(
    make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    ftmux.add_session("agent")
    relay = make_relay()
    relay.startup()
    queue_dir = relay.state.queue_dir
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.parent == queue_dir:
            raise OSError(errno.EPERM, "Operation not permitted")
        real_unlink(self, missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    tg.add_message("once")
    relay.cycle()
    relay.cycle()
    assert texts(ftmux) == ["once"] and len(list(queue_dir.glob("*.done"))) == 1


# ---------------------------------------------------------------- 4. read-only / full disk


def _fail_writes(monkeypatch: pytest.MonkeyPatch, code: int = errno.ENOSPC) -> None:
    def broken(path: Path, text: str) -> None:
        raise OSError(code, "No space left on device" if code == errno.ENOSPC else "Read-only file system")

    monkeypatch.setattr(state_mod, "atomic_write", broken)


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EROFS])
def test_startup_survives_a_full_or_read_only_disk(
    make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    relay = make_relay()
    relay.startup()
    tg.add_message("queued before the disk filled up")
    relay.cycle()
    _fail_writes(monkeypatch, code)
    again = make_relay()
    again.startup()
    ftmux.add_session("agent")
    again.cycle()
    assert texts(ftmux) == ["queued before the disk filled up"]
    assert any("could not save" in m["text"] for m in tg.sent)


def test_first_start_survives_a_read_only_disk(
    make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay = make_relay()
    relay.state.ensure()
    _fail_writes(monkeypatch, errno.EROFS)
    relay.startup()  # skip_backlog wants to save the offset: must not crash
    relay.cycle()


# ---------------------------------------------------------------- 5. start grace on a monotonic clock


def test_start_grace_survives_a_clock_step_back(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux, clock: Clock) -> None:
    ftmux.add_session("agent", created=clock.now)
    relay = make_relay("start_grace = 15")
    relay.startup()
    tg.add_message("early")
    relay.cycle()
    assert texts(ftmux) == []
    clock.step(-3600)
    clock.now += 20
    relay.cycle()
    assert texts(ftmux) == ["early"]


def test_start_grace_ignores_a_clock_step_forward(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux, clock: Clock) -> None:
    ftmux.add_session("agent", created=clock.now)
    relay = make_relay("start_grace = 15")
    relay.startup()
    tg.add_message("early")
    relay.cycle()
    clock.step(3600)
    relay.cycle()
    assert texts(ftmux) == []


# ---------------------------------------------------------------- 6. reused update ids


def test_reused_update_id_is_not_dropped(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    relay = make_relay()
    relay.startup()
    relay.receive(_update(7, "old", date=1_700_000_000))
    relay.receive(_update(7, "new, same id a week later", date=1_700_900_000))
    relay.receive(_update(7, "old", date=1_700_000_000))  # a real re-fetch: stored once
    assert relay.state.queue_len() == 2
    ftmux.add_session("agent")
    relay.deliver_pending()
    assert texts(ftmux) == ["old", "new, same id a week later"]


# ---------------------------------------------------------------- 7. drop / flush act on the snapshot


def test_drop_does_not_deliver_earlier_messages_first(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    ftmux.add_session("agent")
    relay = make_relay()
    relay.startup()
    tg.add_message("never mind this")
    tg.add_message("/aot drop")
    relay.cycle()
    assert texts(ftmux) == []
    assert "Dropped 1 queued message(s)." in [m["text"] for m in tg.sent]


def test_flush_sends_no_blocked_notice(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    ftmux.add_session("agent", screen=["Continue? (y/n)"])
    relay = make_relay()
    relay.startup()
    tg.add_message("paste anyway")
    tg.add_message("/aot flush")
    relay.cycle()
    assert texts(ftmux) == ["paste anyway"]
    assert not any("waiting on a prompt" in m["text"] for m in tg.sent)


# ---------------------------------------------------------------- 8. only plain text is a command


@pytest.mark.parametrize(
    "extra",
    [
        {"caption": "/aot keys C-c", "sticker": {"file_id": "S", "emoji": "x"}},
        {"text": "/aot keys C-c", "contact": {"phone_number": "1", "first_name": "a"}},
        {"text": "/aot keys C-c", "some_future_content": {"x": 1}},
        {"caption": "/aot keys C-c", "paid_media": {"star_count": 1}},
    ],
)
def test_only_plain_text_is_a_command(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux, extra: dict[str, Any]) -> None:
    ftmux.add_session("agent")
    relay = make_relay()
    relay.startup()
    tg.add_message(**extra)
    relay.cycle()
    assert ftmux.session("agent")["keys"] == []


def test_plain_text_with_entities_is_still_a_command(make_relay: RelayFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    ftmux.add_session("agent")
    relay = make_relay()
    relay.startup()
    tg.add_message("/aot keys y", entities=[{"type": "bot_command", "offset": 0, "length": 4}], link_preview_options={"is_disabled": True})
    relay.cycle()
    assert ftmux.session("agent")["keys"] == ["y"]
