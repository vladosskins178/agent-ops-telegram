"""``aot watchdog``: run every couple of minutes (systemd timer or cron) to repair what
``Restart=always`` cannot see: a relay that is alive but stuck, duplicate pollers, and an
agent tmux session that did not come back after a reboot."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import time
from collections.abc import Callable

from . import services
from .config import BotConfig, ConfigError
from .procs import ancestors, pid_alive, relay_pids, start_epoch
from .security import redact
from .state import BotState, as_float, atomic_write, boot_id, lock_holder, lock_path, state_lock_path
from .telegram import TelegramAPI, TelegramError
from .textutil import one_line, split_text
from .tmux import Tmux, TmuxError

log = logging.getLogger("aot.watchdog")

# The relay exits with this status (sysexits EX_CONFIG) when Telegram rejects the token or
# the config cannot be used; health.json's ``fatal_reason`` says which.
EXIT_CONFIG = 78
TOKEN_REJECTED = "relay stopped: Telegram rejected the bot token (exit 78); not restarting it - run `aot doctor`"
CONFIG_ERROR = "relay stopped: configuration error (exit 78); not restarting it - fix the config, run `aot doctor`, then start the relay"
FATAL_REASONS = ("token_rejected", "config_error")
# A relay that lost the race for the lock sleeps ~30 s and exits with 3. Only a process
# without a lock that has lived longer than this is treated as a duplicate.
DUPLICATE_GRACE = 90.0


def fatal_reason(health: dict[str, object]) -> str:
    """Why the relay exited with 78. Older versions wrote ``fatal`` only for a rejected
    token; an exit 78 with no note at all means the config could not even be loaded."""
    reason = health.get("fatal_reason")
    if reason in FATAL_REASONS:
        return str(reason)
    return "token_rejected" if health.get("fatal") else "config_error"


def _fatal_message(reason: str, health: dict[str, object]) -> str:
    if reason == "token_rejected":
        return TOKEN_REJECTED
    detail = one_line(redact(str(health.get("fatal") or "")), 160)
    return f"{CONFIG_ERROR} ({detail})" if detail else CONFIG_ERROR


def health_age(health: dict[str, object], key: str, now: float, mono_now: float, boot: str) -> float | None:
    """Seconds since the relay wrote ``key`` (``heartbeat`` or ``started``), or ``None``
    when unknown. Within the same boot the monotonic stamp (``<key>_mono``) is used, so a
    wall-clock step cannot make a fresh heartbeat look stale. Otherwise the wall-clock
    stamp is used, and an age below zero (written before the clock stepped back) is unknown."""
    mono = as_float(health.get(f"{key}_mono"), -1.0)
    if boot and health.get("boot_id") == boot and mono >= 0:
        age = mono_now - mono
    else:
        wall = as_float(health.get(key))
        if wall <= 0:
            return None
        age = now - wall
    return age if age >= 0 else None


def watchdog_bot(
    bot: BotConfig,
    *,
    dry_run: bool = False,
    tmux: Tmux | None = None,
    api: TelegramAPI | None = None,
    now: float | None = None,
    mono_now: float | None = None,
    kill: Callable[[int, int], None] = os.kill,
    config_path: str = "",
    sleep: Callable[[float], None] = time.sleep,
    term_grace: float = 10.0,
    notes: list[str] | None = None,
) -> list[str]:
    """Check one bot and repair it. Returns the actions taken (or that would be taken).

    Problems the watchdog cannot act on and does not alert about (e.g. a relay that is
    not running with no way configured to restart it) are appended to ``notes``."""
    now = time.time() if now is None else now
    mono_now = time.monotonic() if mono_now is None else mono_now
    state = BotState(bot.bot_state_dir)
    health = state.read_health()
    actions: list[str] = []
    try:
        token = bot.token()
    except ConfigError as exc:
        return [f"cannot check: {exc}"]

    # 1. duplicate pollers on this machine
    pids = relay_pids(bot.name, bot.config_path)
    holders = [lock_holder(lock_path(bot.state_dir, token)), lock_holder(state_lock_path(bot.bot_state_dir))]
    holder = holders[0] or holders[1]
    keep = holder if holder in pids else (pids[0] if pids else 0)

    def duplicate(pid: int) -> bool:
        """Holds a lock it should not, or has run without one for too long. A relay that
        just lost the lock race holds neither and exits on its own (code 3)."""
        if pid in holders:
            return True
        born = start_epoch(pid)
        return born is not None and now - born > DUPLICATE_GRACE

    extras = [p for p in pids if p not in ancestors(keep) | {keep} and duplicate(p)] if keep else []
    if extras and dry_run:
        actions.append(f"would stop duplicate relay process(es) {', '.join(map(str, extras))} (keeping {keep})")
    elif extras:
        born = {pid: start_epoch(pid) for pid in extras}
        # Re-check right before signalling: a duplicate may have exited and its PID been reused.
        fresh = relay_pids(bot.name, bot.config_path)
        extras = [p for p in extras if p in fresh and start_epoch(p) == born[p]]
        if extras:
            hard = _terminate(
                extras, lambda p: p in relay_pids(bot.name, bot.config_path) and start_epoch(p) == born[p], kill, sleep, term_grace
            )
            note = f"; killed {', '.join(map(str, hard))} after {term_grace:g} s" if hard else ""
            actions.append(f"stopped duplicate relay process(es) {', '.join(map(str, extras))} (kept {keep}){note}")

    # 2. relay down or stuck
    unit = services.unit_state(bot)
    running = bool(pids) or bool(holder)
    threshold = max(bot.heartbeat_stale, bot.poll_timeout * 3)
    boot = boot_id()
    beat_age = health_age(health, "heartbeat", now, mono_now, boot)
    started_age = health_age(health, "started", now, mono_now, boot)
    started = as_float(health.get("started"))
    stuck = running and beat_age is not None and beat_age > threshold and (started_age is None or started_age > threshold)
    reason = ""
    fatal = ""  # "token_rejected" or "config_error": the relay exited with 78
    if unit is not None and unit != "active" and services.unit_enabled(bot):
        reason = f"{bot.unit_name} was {unit}"
        if unit != "activating" and (services.unit_exit_status(bot) == EXIT_CONFIG or (not running and bool(health.get("fatal")))):
            fatal = fatal_reason(health)
    elif unit is None and not running and (bot.restart_command or bot.systemd != "none"):
        reason = "relay was not running"
        if health.get("fatal"):
            fatal = fatal_reason(health)
    elif unit is None and not running and notes is not None:
        notes.append("relay not running (no restart method configured)")
    elif stuck:
        reason = f"relay was stuck (no heartbeat for {int(beat_age or 0)} s)"
    if fatal:
        # Restarting cannot help until someone fixes the token or config: say so once, then
        # stay quiet (and never run restart_command).
        reason = ""
        flag = state.root / fatal
        message = _fatal_message(fatal, health)
        if notes is not None:
            notes.append(message)
        if not dry_run and not flag.exists():
            log.warning("%s: %s", bot.name, message)
            if _alert(bot, state, [message], token, api, now, force=True):
                try:
                    atomic_write(flag, f"{now}\n")
                except OSError as exc:
                    log.warning("%s: could not record the token alert: %s", bot.name, exc)
    elif not dry_run:
        for name in FATAL_REASONS:
            with contextlib.suppress(OSError):
                (state.root / name).unlink()
    if reason:
        if dry_run:
            actions.append(f"would restart the relay: {reason}")
        else:
            ok, out = services.restart_relay(bot)
            if ok:
                actions.append(f"restarted the relay: {reason}")
            elif stuck and (holder or pids) and bot.systemd != "none":
                # Not allowed to restart a system unit (no polkit rights as User=), but the
                # stuck relay is our own process: stop it and let Restart=always bring it back.
                victim = holder if holder and holder > 0 else (pids[0] if pids else 0)
                try:
                    if not victim:
                        raise ProcessLookupError("no relay pid known")
                    why_not = _not_our_relay(victim, pids, started)
                    if why_not:
                        raise ProcessLookupError(f"pid {victim} {why_not}; not killing it")
                    victim_born = start_epoch(victim)
                    hard = _terminate(
                        [victim],
                        lambda p: p in relay_pids(bot.name, bot.config_path) and start_epoch(p) == victim_born,
                        kill,
                        sleep,
                        term_grace,
                    )
                    how = f"killed it after {term_grace:g} s without exiting" if hard else "stopped it"
                    actions.append(f"{how}: the stuck relay (pid {victim}), so systemd restarts it: {reason}")
                except (ProcessLookupError, PermissionError) as exc:
                    actions.append(f"could not restart the relay ({reason}): {out[:160]}; kill failed: {exc}")
            else:
                actions.append(f"could not restart the relay ({reason}): {out[:200]}")

    # 3. agent session missing (typically after a reboot)
    tmux = tmux or Tmux(bot.tmux_target, socket=bot.tmux_socket, binary=bot.tmux_bin)
    tmux.scrub_env.add(bot.token_env)
    if bot.start_command and tmux.available() and not tmux.has_session():
        if dry_run:
            actions.append(f"would start tmux session '{bot.session}'")
        else:
            env = {"AOT_BOT": bot.name}
            if config_path:
                env["AOT_CONFIG"] = config_path
            try:
                tmux.new_session(bot.start_command, workdir=bot.workdir, env=env)
                actions.append(f"started the agent session '{bot.session}' (it was not running)")
            except TmuxError as exc:
                actions.append(f"could not start the agent session '{bot.session}': {exc}")

    if actions and not dry_run:
        for action in actions:
            log.warning("%s: %s", bot.name, action)
        _alert(bot, state, actions, token, api, now)
    return actions


def _terminate(
    pids: list[int], still_ours: Callable[[int], bool], kill: Callable[[int, int], None], sleep: Callable[[float], None], grace: float
) -> list[int]:
    """SIGTERM ``pids``, wait up to ``grace`` seconds, then SIGKILL the ones still alive
    that ``still_ours`` confirms are the same process. Returns the PIDs killed hard.

    The relay finishes a message it is typing before it exits on SIGTERM; a relay hung in
    a system call never exits, which is why the SIGKILL is needed."""
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            kill(pid, signal.SIGTERM)
    waited = 0.0
    while waited < grace and any(pid_alive(p) for p in pids):
        sleep(0.2)
        waited += 0.2
    hard: list[int] = []
    for pid in pids:
        if pid_alive(pid) and still_ours(pid):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                kill(pid, signal.SIGKILL)
                hard.append(pid)
    return hard


def _not_our_relay(pid: int, pids: list[int], started: float) -> str:
    """Why ``pid`` must not be killed as this bot's stuck relay ("" when it may).

    The PID comes from the lock file, so check it is still a relay for this bot and config
    (command line, same user) and that it is the process that wrote the stale heartbeat,
    not a newer one that reused the PID or just took over."""
    if pid not in pids:
        return "is not a relay process for this bot"
    began = start_epoch(pid)
    if started and began is not None and began > started + 60:
        return "started after the stale heartbeat was written"
    return ""


def _alert(
    bot: BotConfig, state: BotState, actions: list[str], token: str, api: TelegramAPI | None, now: float, *, force: bool = False
) -> bool:
    """Tell the owner what the watchdog did; the same text at most once per notify_cooldown
    (``force`` skips the cooldown). Returns whether the alert was sent."""
    path = state.root / "watchdog.json"
    try:
        sent: dict[str, float] = json.loads(path.read_text())
    except (OSError, ValueError):
        sent = {}
    text = "Watchdog (" + bot.name + "):\n" + "\n".join(f"- {a}" for a in actions)
    key = "\n".join(sorted(a.split(" (")[0] for a in actions))
    if not isinstance(sent, dict):
        sent = {}
    if (not force and now - as_float(sent.get(key)) < bot.notify_cooldown) or not bot.alert_chat:
        return False
    try:
        api = api or TelegramAPI(token, bot.api_base, retries=2, timeout=20)
        for chunk in split_text(text):
            api.send_message(bot.alert_chat, chunk)
        sent[key] = now
        sent = {k: v for k, v in sent.items() if now - as_float(v) < 7 * 86400}
        state.ensure()
        atomic_write(path, json.dumps(sent))
    except TelegramError as exc:
        log.warning("%s: could not send the alert: %s", bot.name, exc)
        return False
    except OSError as exc:
        log.warning("%s: could not record the alert: %s", bot.name, exc)
    return True
