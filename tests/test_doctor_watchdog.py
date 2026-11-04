from __future__ import annotations

import signal
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_ops_telegram import doctor, services, watchdog
from agent_ops_telegram.config import BotConfig, Config
from agent_ops_telegram.doctor import FAIL, OK, WARN, run_doctor
from agent_ops_telegram.procs import is_relay_argv
from agent_ops_telegram.state import BotState, PollerLock, lock_path
from agent_ops_telegram.watchdog import watchdog_bot

from .conftest import ConfigFactory
from .fakes import TOKEN, FakeTelegram, FakeTmux


def by_id(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.id: c for c in checks}


@pytest.fixture
def running_lock(make_config: ConfigFactory, tmp_path: Path) -> Iterator[Config]:
    cfg = make_config()
    lock = PollerLock(lock_path(cfg.bot(None).state_dir, TOKEN))
    lock.acquire()
    yield cfg
    lock.release()


def test_doctor_healthy(running_lock: Config, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    bot = running_lock.bot(None)
    ftmux.add_session("agent")
    BotState(bot.bot_state_dir).write_health(heartbeat=1000.0, last_poll=1000.0, last_update=900.0)
    checks = by_id(run_doctor(bot, now=1010.0))
    assert checks["telegram.token_valid"].status == OK
    assert "@fake_bot" in checks["telegram.token_valid"].summary
    assert checks["telegram.webhook"].status == OK
    assert checks["poller.single"].status == OK
    assert checks["relay.heartbeat"].status == OK
    assert checks["tmux.session"].status == OK
    assert checks["queue"].status == OK
    assert doctor.summarize(list(checks.values()))["ok"]


def test_doctor_finds_webhook_and_fixes_it(running_lock: Config, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    tg.webhook_url = "https://example.org/hook"
    bot = running_lock.bot(None)
    checks = by_id(run_doctor(bot))
    assert checks["telegram.webhook"].status == FAIL
    assert "--fix" in checks["telegram.webhook"].fix
    checks = by_id(run_doctor(bot, fix=True))
    assert checks["telegram.webhook"].status == OK
    assert tg.webhook_url == ""


def test_doctor_bad_token(make_config: ConfigFactory, tg: FakeTelegram) -> None:
    bot = make_config(token="1:" + "z" * 35).bot(None)
    checks = by_id(run_doctor(bot))
    assert checks["telegram.token_valid"].status == FAIL
    assert "BotFather" in checks["telegram.token_valid"].fix


def test_doctor_missing_token(make_config: ConfigFactory, tmp_path: Path) -> None:
    bot = make_config().bot(None)
    (tmp_path / "secrets.env").write_text("OTHER=1\n")
    checks = by_id(run_doctor(bot))
    assert checks["telegram.token_present"].status == FAIL
    assert "telegram.token_valid" not in checks


def test_doctor_no_relay_and_no_session(make_config: ConfigFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    bot = make_config().bot(None)
    checks = by_id(run_doctor(bot))
    assert checks["poller.single"].status == FAIL
    assert checks["tmux.session"].status == FAIL
    assert "tmux new" in checks["tmux.session"].fix


def test_doctor_session_missing_but_start_command(make_config: ConfigFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    bot = make_config('start_command = "codex"').bot(None)
    assert by_id(run_doctor(bot, offline=True))["tmux.session"].status == WARN


def test_doctor_duplicate_pollers(running_lock: Config, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram) -> None:
    monkeypatch.setattr(doctor, "relay_pids", lambda name, config=None: [101, 202])
    check = by_id(run_doctor(running_lock.bot(None), offline=True))["poller.single"]
    assert check.status == FAIL and "101, 202" in check.summary


def test_doctor_recent_conflict_and_stuck_relay(running_lock: Config, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    bot = running_lock.bot(None)
    tg.add_message("waiting at telegram")
    BotState(bot.bot_state_dir).write_health(heartbeat=1000.0, last_poll=1000.0, last_conflict=1500.0)
    checks = by_id(run_doctor(bot, now=1800.0))
    assert checks["poller.conflict"].status == FAIL
    assert checks["relay.heartbeat"].status == FAIL and "stuck" in checks["relay.heartbeat"].summary
    assert checks["telegram.pending"].status == FAIL


def test_doctor_blocked_prompt_and_old_queue(running_lock: Config, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    bot = running_lock.bot(None)
    ftmux.add_session("agent", screen=["Do you want to proceed?", "1. Yes"])
    state = BotState(bot.bot_state_dir)
    state.ensure()
    item = state.enqueue({"update_id": 5, "message": {"text": "x"}})
    assert item is not None
    item.data["received"] = 100.0
    item.save()
    checks = by_id(run_doctor(bot, offline=True, now=10_000.0))
    assert checks["tmux.prompt"].status == WARN
    assert checks["queue"].status == WARN


def test_doctor_low_disk(running_lock: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    class Usage:
        free = 50 * 1024**2

    monkeypatch.setattr("agent_ops_telegram.doctor.shutil.disk_usage", lambda p: Usage())
    assert by_id(run_doctor(running_lock.bot(None), offline=True))["disk.free"].status == FAIL


def test_doctor_service_state(running_lock: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = running_lock.bot(None)
    bot.systemd = "system"
    monkeypatch.setattr(services, "systemctl_available", lambda: True)
    monkeypatch.setattr(services, "systemctl", lambda scope, *a, **k: (3, "inactive"))
    checks = by_id(run_doctor(bot, offline=True))
    assert checks["relay.service"].status == FAIL
    assert "enable --now aot-relay@agent.service" in checks["relay.service"].fix
    assert checks["watchdog.timer"].status == WARN


def test_doctor_never_prints_token(running_lock: Config, tg: FakeTelegram) -> None:
    text = repr(run_doctor(running_lock.bot(None)))
    assert TOKEN not in text


def test_is_relay_argv() -> None:
    assert is_relay_argv(["/usr/bin/python3", "/opt/venv/bin/aot", "relay", "--bot", "app"], "app")
    assert is_relay_argv(["python3", "-u", "-m", "agent_ops_telegram", "relay", "--bot=app"], "app")
    assert is_relay_argv(["/usr/bin/python3", "/home/me/.local/bin/aot", "--config", "c.toml", "relay", "--bot", "app"], "app")
    # wrappers are not the relay itself, so the watchdog never kills them
    assert not is_relay_argv(["sudo", "aot", "relay", "--bot", "app"], "app")
    assert not is_relay_argv(["timeout", "1d", "/opt/venv/bin/aot", "relay", "--bot", "app"], "app")
    assert not is_relay_argv(["uv", "run", "aot", "relay", "--bot", "app"], "app")
    assert not is_relay_argv(["/venv/bin/aot", "send", "relay", "restarted", "--bot", "app"], "app")
    assert not is_relay_argv(["/opt/venv/bin/aot", "relay", "--bot", "app2"], "app")
    assert not is_relay_argv(["/opt/venv/bin/aot", "send", "--bot", "app"], "app")
    assert not is_relay_argv(["vim", "relay", "--bot", "app"], "app")


# ------------------------------------------------------------------ watchdog


def test_watchdog_healthy_does_nothing(running_lock: Config, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    bot = running_lock.bot(None)
    ftmux.add_session("agent")
    BotState(bot.bot_state_dir).write_health(heartbeat=1000.0, started=0.0)
    assert watchdog_bot(bot, now=1010.0) == []
    assert tg.sent == []


def test_watchdog_restarts_stuck_relay_and_alerts_once(
    running_lock: Config, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    bot = running_lock.bot(None)
    ftmux.add_session("agent")
    calls: list[str] = []

    def restart(b: BotConfig) -> tuple[bool, str]:
        calls.append(b.name)
        return True, ""

    monkeypatch.setattr(services, "restart_relay", restart)
    BotState(bot.bot_state_dir).write_health(heartbeat=1000.0, started=0.0)
    actions = watchdog_bot(bot, now=2000.0)
    assert calls == ["agent"]
    assert "stuck" in actions[0]
    assert "Watchdog (agent)" in tg.sent[0]["text"]
    watchdog_bot(bot, now=2100.0)
    assert len(tg.sent) == 1  # same problem again within the cooldown: no second alert


def test_watchdog_kills_duplicates_keeps_lock_holder(
    running_lock: Config, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    import os

    bot = running_lock.bot(None)
    ftmux.add_session("agent")
    me = os.getpid()
    monkeypatch.setattr(watchdog, "relay_pids", lambda name, config=None: [999_991, me, 999_992])
    monkeypatch.setattr(watchdog, "lock_holder", lambda path: me)
    monkeypatch.setattr(watchdog, "start_epoch", lambda pid: 500.0)  # running without a lock for 500 s
    BotState(bot.bot_state_dir).write_health(heartbeat=1000.0, started=0.0)
    killed: list[tuple[int, int]] = []
    actions = watchdog_bot(bot, now=1001.0, kill=lambda pid, sig: killed.append((pid, sig)))
    assert killed == [(999_991, signal.SIGTERM), (999_992, signal.SIGTERM)]
    assert "kept" in actions[0]


def test_watchdog_leaves_a_relay_that_lost_the_lock_race_alone(
    running_lock: Config, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    """A relay started while another holds the lock sleeps ~30 s, then exits with 3: it
    holds no lock and is young, so it is not a duplicate to kill or report."""
    import os

    bot = running_lock.bot(None)
    ftmux.add_session("agent")
    me = os.getpid()
    monkeypatch.setattr(watchdog, "relay_pids", lambda name, config=None: [me, 999_991])
    monkeypatch.setattr(watchdog, "lock_holder", lambda path: me)
    monkeypatch.setattr(watchdog, "start_epoch", lambda pid: 990.0)  # started 11 s ago
    BotState(bot.bot_state_dir).write_health(heartbeat=1000.0, started=0.0)
    killed: list[tuple[int, int]] = []
    assert watchdog_bot(bot, now=1001.0, kill=lambda pid, sig: killed.append((pid, sig))) == []
    assert killed == [] and tg.sent == []


def test_watchdog_starts_missing_session(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    bot = make_config('start_command = "claude --continue"\nrestart_command = ["true"]').bot(None)
    actions = watchdog_bot(bot, config_path="/etc/aot.toml")
    assert any("restarted the relay" in a for a in actions)
    assert any("started the agent session" in a for a in actions)
    sess = ftmux.session("agent")
    assert sess["command"] == "claude --continue"
    assert "AOT_CONFIG=/etc/aot.toml" in sess["env"]


def test_watchdog_dry_run(make_config: ConfigFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    bot = make_config('start_command = "x"\nrestart_command = ["false"]').bot(None)
    actions = watchdog_bot(bot, dry_run=True)
    assert actions == ["would restart the relay: relay was not running", "would start tmux session 'agent'"]
    assert "agent" not in ftmux.state()["sessions"]
    assert tg.sent == []


def test_watchdog_restart_failure_reported(make_config: ConfigFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    ftmux.add_session("agent")
    bot = make_config('restart_command = ["false"]').bot(None)
    actions = watchdog_bot(bot)
    assert actions[0].startswith("could not restart the relay")
    assert "could not restart" in tg.sent[0]["text"]


def _fake_proc(root: Path, pid: int, argv: list[str], env: dict[str, str] | None = None, start: int = 100, ppid: int = 1) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in (env or {}).items()))
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(start)]
    (d / "stat").write_text(f"{pid} (aot) " + " ".join(fields))
    (d / "cwd").symlink_to(root)


def test_relay_pids_match_config_and_skip_wrappers(tmp_path: Path) -> None:
    from agent_ops_telegram.procs import ancestors, relay_pids

    proc = tmp_path / "proc"
    mine, other = tmp_path / "mine.toml", tmp_path / "other.toml"
    mine.write_text("")
    other.write_text("")
    _fake_proc(proc, 10, ["/venv/bin/aot", "relay", "--bot", "app"], {"AOT_CONFIG": str(mine)}, start=5, ppid=9)
    _fake_proc(proc, 9, ["sudo", "/venv/bin/aot", "relay", "--bot", "app"], start=4)
    _fake_proc(proc, 11, ["/venv/bin/aot", "relay", "--bot", "app"], {"AOT_CONFIG": str(other)}, start=6)
    _fake_proc(proc, 12, ["/venv/bin/aot", "--config", str(mine), "relay", "--bot", "app"], start=7)
    assert relay_pids("app", mine, proc_root=proc) == [10, 12]
    assert relay_pids("app", None, proc_root=proc) == [10, 11, 12]
    assert ancestors(10, proc_root=proc) == {9}


def test_watchdog_kills_stuck_relay_when_restart_is_not_allowed(
    running_lock: Config, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    import os

    bot = running_lock.bot(None)
    bot.systemd = "system"
    ftmux.add_session("agent")
    monkeypatch.setattr(services, "unit_state", lambda b: "active")
    monkeypatch.setattr(services, "restart_relay", lambda b: (False, "Interactive authentication required."))
    monkeypatch.setattr(watchdog, "relay_pids", lambda name, config=None: [os.getpid()])
    BotState(bot.bot_state_dir).write_health(heartbeat=1000.0, started=0.0)
    killed: list[tuple[int, int]] = []
    naps: list[float] = []
    actions = watchdog_bot(bot, now=2000.0, kill=lambda pid, sig: killed.append((pid, sig)), sleep=naps.append)
    # the lock holder in this test is this process; the fake kill leaves it alive, so after
    # the grace period it gets SIGKILL (it is still the same relay process)
    assert killed == [(os.getpid(), signal.SIGTERM), (os.getpid(), signal.SIGKILL)]
    assert abs(sum(naps) - 10.0) < 0.5
    assert "so systemd restarts it" in actions[0] and "killed it after 10 s" in actions[0]


def test_watchdog_never_kills_a_lock_pid_that_is_not_our_relay(
    running_lock: Config, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    import os

    bot = running_lock.bot(None)
    bot.systemd = "system"
    ftmux.add_session("agent")
    monkeypatch.setattr(services, "unit_state", lambda b: "active")
    monkeypatch.setattr(services, "restart_relay", lambda b: (False, "denied"))
    killed: list[int] = []
    # The lock names this process, but /proc does not show it as a relay for this bot.
    monkeypatch.setattr(watchdog, "relay_pids", lambda name, config=None: [])
    BotState(bot.bot_state_dir).write_health(heartbeat=1000.0, started=10.0)
    actions = watchdog_bot(bot, now=2000.0, kill=lambda pid, sig: killed.append(pid))
    assert killed == [] and "is not a relay process for this bot" in actions[0]
    # A relay with that PID, but it started long after the stale heartbeat's relay did.
    monkeypatch.setattr(watchdog, "relay_pids", lambda name, config=None: [os.getpid()])
    monkeypatch.setattr(watchdog, "start_epoch", lambda pid: 1900.0)
    actions = watchdog_bot(bot, now=2000.0, kill=lambda pid, sig: killed.append(pid))
    assert killed == [] and "started after the stale heartbeat" in actions[0]
    monkeypatch.setattr(watchdog, "start_epoch", lambda pid: 5.0)
    watchdog_bot(bot, now=2000.0, kill=lambda pid, sig: killed.append(pid), sleep=lambda s: None)
    assert killed == [os.getpid(), os.getpid()]  # SIGTERM, then SIGKILL after the grace period


def test_start_epoch_reads_proc(tmp_path: Path) -> None:
    import os

    from agent_ops_telegram.procs import start_epoch

    proc = tmp_path / "proc"
    _fake_proc(proc, 30, ["/venv/bin/aot", "relay", "--bot", "app"], start=500)
    (proc / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
    assert start_epoch(30, proc_root=proc) == 1000 + 500 / os.sysconf("SC_CLK_TCK")
    assert start_epoch(31, proc_root=proc) is None


def test_doctor_warns_about_open_groups(make_config: ConfigFactory) -> None:
    bot = make_config("allowed_chats = [42, -1001]").bot(None)
    assert by_id(run_doctor(bot, offline=True))["access.groups"].status == WARN
    bot = make_config("allowed_chats = [42, -1001]\nallowed_users = [42]").bot(None)
    assert "access.groups" not in by_id(run_doctor(bot, offline=True))


def test_watchdog_stuck_without_known_pid_does_not_crash(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    bot = make_config().bot(None)
    bot.systemd = "system"
    ftmux.add_session("agent")
    monkeypatch.setattr(services, "unit_state", lambda b: "active")
    monkeypatch.setattr(services, "restart_relay", lambda b: (False, "denied"))
    monkeypatch.setattr(watchdog, "lock_holder", lambda path: -1)  # lock held, pid unreadable
    monkeypatch.setattr(watchdog, "relay_pids", lambda name, config=None: [])
    BotState(bot.bot_state_dir).write_health(heartbeat=1000.0, started=0.0)
    actions = watchdog_bot(bot, now=2000.0, kill=lambda pid, sig: None)
    assert "no relay pid known" in actions[0]


def test_config_of_uses_the_process_default(tmp_path: Path) -> None:
    from agent_ops_telegram.procs import _config_of

    proc = tmp_path / "proc"
    _fake_proc(proc, 20, ["/venv/bin/aot", "relay", "--bot", "app"], {"HOME": "/home/u"})
    _fake_proc(proc, 21, ["/venv/bin/aot", "relay", "--bot", "app"], {"XDG_CONFIG_HOME": "/x"})
    assert _config_of(proc / "20", ["/venv/bin/aot", "relay"]) == "/home/u/.config/agent-ops-telegram/config.toml"
    assert _config_of(proc / "21", ["/venv/bin/aot", "relay"]) == "/x/agent-ops-telegram/config.toml"


def test_doctor_warns_when_another_config_shares_the_token(running_lock: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bot = running_lock.bot(None)
    other = tmp_path / "other" / "config.toml"
    other.parent.mkdir()
    other.write_text(
        "[defaults]\n"
        f'env_file = "{tmp_path / "secrets.env"}"\n'
        f'state_dir = "{tmp_path / "other-state"}"\n'
        'systemd = "none"\n'
        '[bots.copy]\ntoken_env = "AOT_TOKEN_AGENT"\nallowed_chats = [42]\ntmux_target = "x"\n'
    )
    other.chmod(0o600)
    monkeypatch.setattr(doctor, "all_relays", lambda: [(4242, "copy", other)])
    checks = by_id(run_doctor(bot, offline=True))
    shared = checks["poller.shared_token"]
    assert shared.status == WARN and "different state_dir" in shared.summary and "4242" in shared.summary
    monkeypatch.setattr(doctor, "all_relays", lambda: [])
    assert "poller.shared_token" not in by_id(run_doctor(bot, offline=True))


def test_doctor_warns_when_two_bots_in_one_config_share_a_token(make_config: ConfigFactory, tmp_path: Path) -> None:
    cfg = make_config('[bots.twin]\ntoken_env = "AOT_TOKEN_TWIN"\nallowed_chats = [42]\ntmux_target = "twin"\n')
    with open(tmp_path / "secrets.env", "a") as fh:
        fh.write(f"AOT_TOKEN_TWIN={TOKEN}\n")
    checks = by_id(run_doctor(cfg.bot("agent"), offline=True))
    assert "bot 'twin' in this config" in checks["poller.shared_token"].summary


def test_all_relays_lists_every_bot(tmp_path: Path) -> None:
    from agent_ops_telegram.procs import all_relays

    proc = tmp_path / "proc"
    conf = tmp_path / "c.toml"
    _fake_proc(proc, 10, ["/venv/bin/aot", "relay", "--bot", "app"], {"AOT_CONFIG": str(conf)})
    _fake_proc(proc, 11, ["/venv/bin/aot", "--config", "rel.toml", "relay", "--bot=web"])
    _fake_proc(proc, 12, ["/venv/bin/aot", "send", "hi"])
    assert all_relays(proc_root=proc) == [(10, "app", conf), (11, "web", proc / "rel.toml")]


def test_doctor_warns_about_a_writable_inbox(make_config: ConfigFactory, tmp_path: Path) -> None:
    shared = tmp_path / "shared-inbox"
    shared.mkdir()
    shared.chmod(0o777)
    bot = make_config(f'inbox_dir = "{shared}"').bot(None)
    checks = by_id(run_doctor(bot, offline=True))
    assert checks["perm.inbox_dir"].status == WARN and "chmod 700" in checks["perm.inbox_dir"].fix
    shared.chmod(0o750)
    assert "perm.inbox_dir" not in by_id(run_doctor(bot, offline=True))


def test_watchdog_sigkill_only_when_still_the_same_process(
    running_lock: Config, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    import os

    bot = running_lock.bot(None)
    bot.systemd = "system"
    ftmux.add_session("agent")
    monkeypatch.setattr(services, "unit_state", lambda b: "active")
    monkeypatch.setattr(services, "restart_relay", lambda b: (False, "denied"))
    monkeypatch.setattr(watchdog, "relay_pids", lambda name, config=None: [os.getpid()])
    epochs = iter([5.0, 5.0, 999.0])  # verify, record, then: the PID now belongs to a new process
    monkeypatch.setattr(watchdog, "start_epoch", lambda pid: next(epochs, 999.0))
    BotState(bot.bot_state_dir).write_health(heartbeat=1000.0, started=10.0)
    killed: list[tuple[int, int]] = []
    actions = watchdog_bot(bot, now=2000.0, kill=lambda pid, sig: killed.append((pid, sig)), sleep=lambda s: None)
    assert killed == [(os.getpid(), signal.SIGTERM)]
    assert "stopped it" in actions[0]


def test_watchdog_rechecks_duplicates_right_before_killing(
    running_lock: Config, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    import os

    bot = running_lock.bot(None)
    ftmux.add_session("agent")
    me = os.getpid()
    listings = iter([[999_991, me, 999_992], [me, 999_992]])  # 999_991 exited meanwhile
    monkeypatch.setattr(watchdog, "relay_pids", lambda name, config=None: next(listings, [me, 999_992]))
    monkeypatch.setattr(watchdog, "lock_holder", lambda path: me)
    monkeypatch.setattr(watchdog, "start_epoch", lambda pid: 50.0)
    BotState(bot.bot_state_dir).write_health(heartbeat=1000.0, started=0.0)
    killed: list[tuple[int, int]] = []
    actions = watchdog_bot(bot, now=1001.0, kill=lambda pid, sig: killed.append((pid, sig)), sleep=lambda s: None)
    assert killed == [(999_992, signal.SIGTERM)]  # not alive afterwards: no SIGKILL
    assert "999992" in actions[0] and "999991" not in actions[0]


@pytest.mark.parametrize(
    ("rc", "out", "expected"),
    [
        (0, "active", "active"),
        (3, "inactive", "inactive"),
        (3, "failed", "failed"),
        (1, "Failed to connect to bus: No medium found", "unknown (connect to bus: No medium found)"),
        (127, "[Errno 2] No such file or directory: 'systemctl'", "unknown ([Errno 2] No such file or directory: 'systemctl')"),
        (1, "", "unknown (systemctl exit 1)"),
    ],
)
def test_unit_state_maps_systemctl_errors(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch, rc: int, out: str, expected: str
) -> None:
    bot = make_config().bot(None)
    bot.systemd = "user"
    monkeypatch.setattr(services, "systemctl_available", lambda: True)
    monkeypatch.setattr(services, "systemctl", lambda scope, *a, **k: (rc, out))
    assert services.unit_state(bot) == expected
    assert services.timer_state("user") == expected
    if expected.startswith("unknown"):
        checks = by_id(run_doctor(bot, offline=True))
        assert checks["relay.service"].status == WARN and expected in checks["relay.service"].summary


def test_watchdog_does_not_restart_a_relay_that_exited_78(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    bot = make_config().bot(None)
    bot.systemd = "system"
    ftmux.add_session("agent")
    restarts: list[str] = []
    status = {"unit": "failed", "exit": 78}
    BotState(bot.bot_state_dir).write_health(fatal="Telegram rejected the bot token (401 Unauthorized)", fatal_reason="token_rejected")
    monkeypatch.setattr(services, "unit_state", lambda b: status["unit"])
    monkeypatch.setattr(services, "unit_enabled", lambda b: True)
    monkeypatch.setattr(services, "unit_exit_status", lambda b: status["exit"])

    def restart(b: BotConfig) -> tuple[bool, str]:
        restarts.append(b.name)
        return True, ""

    monkeypatch.setattr(services, "restart_relay", restart)
    for minute in range(3):  # the timer fires every couple of minutes
        notes: list[str] = []
        actions = watchdog_bot(bot, now=1000.0 + 120 * minute + 10**6, notes=notes)
        assert actions == [] and "run `aot doctor`" in notes[0]
    assert restarts == []
    assert len(tg.sent) == 1 and "rejected the bot token" in tg.sent[0]["text"]
    # the token is fixed and the relay runs again: a later rejection is reported again
    status.update(unit="active", exit=0)
    BotState(bot.bot_state_dir).write_health(fatal="", fatal_reason="")  # the relay clears the note on start
    watchdog_bot(bot, now=2 * 10**6)
    status.update(unit="failed", exit=78)
    BotState(bot.bot_state_dir).write_health(fatal="Telegram rejected the bot token (401 Unauthorized)", fatal_reason="token_rejected")
    watchdog_bot(bot, now=3 * 10**6)
    assert len(tg.sent) == 2 and "rejected the bot token" in tg.sent[1]["text"]
    BotState(bot.bot_state_dir).write_health(fatal="", fatal_reason="")
    # any other failure is still restarted
    status.update(exit=1)
    assert "restarted the relay" in watchdog_bot(bot, now=4 * 10**6)[0]


def test_watchdog_respects_fatal_note_without_systemd(make_config: ConfigFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    bot = make_config('restart_command = ["false"]').bot(None)
    ftmux.add_session("agent")
    BotState(bot.bot_state_dir).write_health(fatal="Telegram rejected the bot token (401 Unauthorized)")
    assert watchdog_bot(bot, now=10**6) == [] and watchdog_bot(bot, now=10**6 + 120) == []
    assert len(tg.sent) == 1


def test_watchdog_reports_a_stopped_relay_it_cannot_restart(make_config: ConfigFactory, tg: FakeTelegram, ftmux: FakeTmux) -> None:
    bot = make_config().bot(None)  # systemd = "none", no restart_command
    ftmux.add_session("agent")
    notes: list[str] = []
    assert watchdog_bot(bot, now=10**6, notes=notes) == []
    assert notes == ["relay not running (no restart method configured)"] and tg.sent == []


def test_watchdog_survives_garbage_in_its_state_files(
    running_lock: Config, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    bot = running_lock.bot(None)
    ftmux.add_session("agent")
    state = BotState(bot.bot_state_dir)
    state.write_health(heartbeat="soon", started=[1])
    assert watchdog_bot(bot, now=10**6) == []
    state.write_health(heartbeat=1000.0, started=0.0)
    (state.root / "watchdog.json").write_text('{"x": "never", "y": null}')
    monkeypatch.setattr(services, "restart_relay", lambda b: (True, ""))
    assert "stuck" in watchdog_bot(bot, now=10**6)[0]
    assert len(tg.sent) == 1


def test_unit_exit_status_parses_systemctl(make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = make_config().bot(None)
    bot.systemd = "user"
    monkeypatch.setattr(services, "systemctl_available", lambda: True)
    for out, expected in [((0, "78\n"), 78), ((0, "0"), 0), ((1, "Failed to connect to bus"), None), ((0, "[not set]"), None)]:
        monkeypatch.setattr(services, "systemctl", lambda *a, _o=out, **k: _o)
        assert services.unit_exit_status(bot) == expected


def test_watchdog_tells_a_config_error_from_a_rejected_token(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    bot = make_config('restart_command = ["false"]').bot(None)
    ftmux.add_session("agent")
    restarts: list[str] = []

    def restart(b: BotConfig) -> tuple[bool, str]:
        restarts.append(b.name)
        return True, ""

    monkeypatch.setattr(services, "restart_relay", restart)
    state = BotState(bot.bot_state_dir)
    state.write_health(fatal="AOT_TOKEN_AGENT is not set in /x/secrets.env", fatal_reason="config_error")
    for minute in range(3):
        notes: list[str] = []
        assert watchdog_bot(bot, now=10**6 + 120 * minute, notes=notes) == []
        assert "configuration error" in notes[0] and "AOT_TOKEN_AGENT is not set" in notes[0]
    assert restarts == []  # restart_command is not re-run for a config error
    assert len(tg.sent) == 1 and "configuration error" in tg.sent[0]["text"] and "rejected" not in tg.sent[0]["text"]
    assert (state.root / "config_error").exists() and not (state.root / "token_rejected").exists()
    state.write_health(fatal="Telegram rejected the bot token (401 Unauthorized)", fatal_reason="token_rejected")
    watchdog_bot(bot, now=2 * 10**6)
    assert len(tg.sent) == 2 and "rejected the bot token" in tg.sent[1]["text"]


def test_watchdog_exit_78_without_a_note_is_a_config_error(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch, tg: FakeTelegram, ftmux: FakeTmux
) -> None:
    bot = make_config().bot(None)
    bot.systemd = "system"
    ftmux.add_session("agent")
    monkeypatch.setattr(services, "unit_state", lambda b: "failed")
    monkeypatch.setattr(services, "unit_enabled", lambda b: True)
    monkeypatch.setattr(services, "unit_exit_status", lambda b: 78)
    monkeypatch.setattr(services, "restart_relay", lambda b: pytest.fail("must not restart"))
    notes: list[str] = []
    assert watchdog_bot(bot, now=10**6, notes=notes) == []
    assert "configuration error" in notes[0] and len(tg.sent) == 1
