"""``aot doctor``: every failure mode we have hit, as a check with a fix."""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import services
from .config import BotConfig, ConfigError
from .procs import all_relays, relay_pids
from .security import InsecureFileError, check_private_file
from .state import BotState, lock_holder, lock_path
from .telegram import TelegramAPI, TelegramError
from .tmux import Tmux, TmuxError

OK, INFO, WARN, FAIL = "ok", "info", "warn", "fail"


@dataclass
class Check:
    id: str
    status: str
    summary: str
    fix: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _age(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def _bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"  # pragma: no cover


def _dir_size(path: Path) -> int:
    total = 0
    if path.exists():
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return total


def _token_shared(bot: BotConfig, token: str) -> list[str]:
    """Other bots that poll the same token: in this config, or in another config a running
    relay uses. The single-poller lock lives under ``state_dir``, so it only covers configs
    that share a ``state_dir``; Telegram would split the messages between the others."""
    from .config import load_config

    found: list[str] = []
    configs: dict[Path, str] = {}
    if bot.config_path is not None:
        configs[bot.config_path.resolve()] = ""
    for pid, _name, path in all_relays():
        if path is not None:
            configs.setdefault(path.resolve(), f" (running relay pid {pid})")
    mine = bot.config_path.resolve() if bot.config_path is not None else None
    for path, note in configs.items():
        try:
            cfg = load_config(path)
        except (ConfigError, OSError):
            continue
        for other in cfg.bots.values():
            if path == mine and other.name == bot.name:
                continue
            try:
                if other.token() != token:
                    continue
            except ConfigError:
                continue
            where = "this config" if path == mine else str(path)
            if other.state_dir.resolve() != bot.state_dir.resolve():
                found.append(
                    f"bot '{other.name}' in {where}{note} uses the same token with a different state_dir, "
                    "so the single-poller lock does not stop both from polling (messages get split)"
                )
            else:
                found.append(f"bot '{other.name}' in {where}{note} uses the same token; only one of them can run")
    return found


def run_doctor(
    bot: BotConfig,
    *,
    offline: bool = False,
    fix: bool = False,
    api: TelegramAPI | None = None,
    tmux: Tmux | None = None,
    now: float | None = None,
) -> list[Check]:
    now = time.time() if now is None else now
    checks: list[Check] = []
    add = checks.append
    state = BotState(bot.bot_state_dir)
    health = state.read_health()

    # ------------------------------------------------------------ secrets
    if bot.env_file is not None and bot.env_file.exists():
        try:
            check_private_file(bot.env_file)
            add(Check("secrets.env_file", OK, f"{bot.env_file} is private"))
        except InsecureFileError as exc:
            add(Check("secrets.env_file", FAIL, str(exc), f"chmod 600 {bot.env_file}"))
    token = ""
    try:
        token = bot.token()
        add(Check("telegram.token_present", OK, f"{bot.token_env} is set (value not shown)"))
    except ConfigError as exc:
        add(
            Check(
                "telegram.token_present",
                FAIL,
                str(exc),
                f"put {bot.token_env}=<token from @BotFather> in {bot.env_file or 'the environment'}",
            )
        )

    # ------------------------------------------------------------ Telegram
    if token and not offline:
        api = api or TelegramAPI(token, bot.api_base, retries=1, timeout=15)
        try:
            me = api.get_me()
            add(Check("telegram.token_valid", OK, f"token accepted: @{me.get('username', '?')}"))
            try:
                hook = api.webhook_info()
                url = str(hook.get("url") or "")
                pending = int(hook.get("pending_update_count") or 0)
                if url and fix:
                    api.delete_webhook()
                    add(Check("telegram.webhook", OK, "a webhook was set; deleted it (pending updates kept)"))
                elif url:
                    add(
                        Check(
                            "telegram.webhook",
                            FAIL,
                            "a webhook is set, so getUpdates (and this relay) gets 409 Conflict",
                            f"aot doctor --bot {bot.name} --fix  (deletes the webhook; only if nothing else needs it)",
                        )
                    )
                else:
                    add(Check("telegram.webhook", OK, "no webhook set"))
                last_poll = float(health.get("last_poll") or 0)
                if pending and last_poll and now - last_poll > bot.heartbeat_stale:
                    add(
                        Check(
                            "telegram.pending",
                            FAIL,
                            f"{pending} update(s) waiting at Telegram and the relay has not polled for {_age(now - last_poll)}",
                            "the relay is stuck or down: aot watchdog (or restart the relay service)",
                        )
                    )
                else:
                    add(Check("telegram.pending", INFO, f"{pending} update(s) waiting at Telegram"))
            except TelegramError as exc:
                add(Check("telegram.webhook", WARN, f"could not read webhook info: {exc}"))
        except TelegramError as exc:
            if exc.code == 401:
                add(
                    Check(
                        "telegram.token_valid",
                        FAIL,
                        "Telegram rejected the token (401 Unauthorized)",
                        "get the current token from @BotFather (/token)",
                    )
                )
            elif exc.code == 404:
                add(Check("telegram.token_valid", FAIL, "Telegram does not know this token (404)", "copy the token again from @BotFather"))
            else:
                add(
                    Check(
                        "telegram.token_valid", WARN, f"could not reach Telegram: {exc}", "check DNS / outbound HTTPS to api.telegram.org"
                    )
                )

    # ------------------------------------------------------------ single poller
    pids = relay_pids(bot.name, bot.config_path)
    holder = lock_holder(lock_path(bot.state_dir, token)) if token else None
    if len(pids) > 1:
        add(
            Check(
                "poller.single",
                FAIL,
                f"{len(pids)} relay processes for this bot (pids {', '.join(map(str, pids))}); Telegram splits messages between them",
                f"keep one: aot watchdog --bot {bot.name} (stops the extra ones) or kill {' '.join(map(str, pids[1:]))}",
            )
        )
    elif pids or holder:
        who = holder if holder and holder > 0 else (pids[0] if pids else "unknown")
        add(Check("poller.single", OK, f"one relay polls this bot (pid {who})"))
    else:
        add(Check("poller.single", FAIL, "no relay is running for this bot; messages are not being picked up", _start_fix(bot)))
    if token:
        for summary in _token_shared(bot, token):
            add(
                Check(
                    "poller.shared_token",
                    WARN,
                    summary,
                    "use one bot token per relay: give the other bot its own token from @BotFather, or remove it",
                )
            )
    last_conflict = float(health.get("last_conflict") or 0)
    if last_conflict and now - last_conflict < 3600:
        add(
            Check(
                "poller.conflict",
                FAIL,
                f"Telegram reported 409 Conflict {_age(now - last_conflict)} ago: something else reads this bot's updates",
                "stop the other poller: another server, an old script, a bot framework or a webhook using the same token",
            )
        )
    elif last_conflict:
        add(Check("poller.conflict", INFO, f"last 409 Conflict was {_age(now - last_conflict)} ago"))

    groups = [c for c in bot.allowed_chats if c < 0]
    if groups and not bot.allowed_users:
        add(
            Check(
                "access.groups",
                WARN,
                f"group chat(s) {', '.join(map(str, groups))} allowed without allowed_users: every member can type into the agent",
                "set allowed_users = [<your user id>] for this bot",
            )
        )

    # ------------------------------------------------------------ relay health
    running = bool(pids or holder)
    beat = float(health.get("heartbeat") or 0)
    if running and beat:
        stale = now - beat > max(bot.heartbeat_stale, bot.poll_timeout * 3)
        add(
            Check(
                "relay.heartbeat",
                FAIL if stale else OK,
                f"last heartbeat {_age(now - beat)} ago" + (" (alive but stuck)" if stale else ""),
                f"aot watchdog --bot {bot.name}, then aot logs --bot {bot.name}" if stale else "",
            )
        )
    elif running:
        add(Check("relay.heartbeat", WARN, "relay is running but has not written a heartbeat yet"))
    unit = services.unit_state(bot)
    if unit is not None:
        ctl = services.manage_cmd(bot.systemd)
        if unit == "active":
            add(Check("relay.service", OK, f"{bot.unit_name} is active"))
        elif unit.startswith("unknown"):
            add(Check("relay.service", WARN, f"could not query {bot.unit_name}: {unit}", f"{ctl} status {bot.unit_name}"))
        elif not services.unit_loaded(bot):
            add(
                Check(
                    "relay.service",
                    FAIL,
                    f"{bot.unit_name}: no such unit installed",
                    f"{'sudo ' if bot.systemd == 'system' and os.geteuid() != 0 else ''}aot"
                    f"{f' --config {bot.config_path}' if bot.config_path else ''} units --write, "
                    f"then {ctl} daemon-reload && {ctl} enable --now {bot.unit_name}",
                )
            )
        else:
            add(Check("relay.service", FAIL, f"{bot.unit_name} is {unit}", f"{ctl} enable --now {bot.unit_name}"))
        timer = services.timer_state(bot.systemd)
        if timer == "active":
            add(Check("watchdog.timer", OK, f"{services.WATCHDOG_TIMER} is active"))
        else:
            add(
                Check(
                    "watchdog.timer",
                    WARN,
                    f"{services.WATCHDOG_TIMER} is {timer}; a hung relay will not be restarted",
                    f"{ctl} enable --now {services.WATCHDOG_TIMER}",
                )
            )
    last_error = str(health.get("last_error") or "")
    if last_error and now - float(health.get("last_error_at") or 0) < 600:
        add(Check("relay.last_error", WARN, f"recent error: {last_error}"))

    # ------------------------------------------------------------ tmux
    tmux = tmux or Tmux(bot.tmux_target, socket=bot.tmux_socket, binary=bot.tmux_bin)
    if not tmux.available():
        add(Check("tmux.binary", FAIL, f"'{bot.tmux_bin}' not found", "install tmux (apt install tmux / brew install tmux)"))
    elif tmux.has_session():
        add(Check("tmux.session", OK, f"session '{bot.session}' exists"))
        try:
            tail = "\n".join(tmux.tail(bot.blocked_scan_lines))
            if any(re.search(p, tail, re.IGNORECASE) for p in bot.blocked_patterns):
                add(
                    Check(
                        "tmux.prompt",
                        WARN,
                        "the agent is waiting on a confirmation prompt; new messages stay queued until it is answered",
                        f"answer it in the terminal or from Telegram: {bot.command_prefix} keys 1",
                    )
                )
        except TmuxError as exc:
            add(Check("tmux.pane", FAIL, f"cannot read pane {bot.tmux_target}: {exc}", "check tmux_target (session:window.pane)"))
    else:
        if bot.start_command:
            add(Check("tmux.session", WARN, f"session '{bot.session}' does not exist; the relay starts it on the next message", ""))
        else:
            add(
                Check(
                    "tmux.session",
                    FAIL,
                    f"session '{bot.session}' does not exist; messages will queue up",
                    f"tmux new -d -s {bot.session} '<your agent command>'  (or set start_command)",
                )
            )

    # ------------------------------------------------------------ queue / activity
    queued = state.queue_len()
    oldest = state.oldest_queued()
    if queued and oldest and now - oldest > 600:
        add(
            Check(
                "queue",
                WARN,
                f"{queued} message(s) queued, oldest {_age(now - oldest)} old",
                f"{bot.command_prefix} status in Telegram, or aot logs --bot {bot.name}",
            )
        )
    else:
        add(Check("queue", OK if not queued else INFO, f"{queued} message(s) queued"))
    last_update = float(health.get("last_update") or 0)
    if last_update:
        add(Check("activity.last_message", INFO, f"last allowed message {_age(now - last_update)} ago"))

    # ------------------------------------------------------------ disk
    probe = bot.state_dir if bot.state_dir.exists() else Path.home()
    try:
        free = shutil.disk_usage(probe).free
        status = FAIL if free < 200 * 1024**2 else WARN if free < 1024**3 else OK
        add(
            Check(
                "disk.free",
                status,
                f"{_bytes(free)} free on the state disk",
                "free disk space; a full disk stops the queue and the agent" if status != OK else "",
            )
        )
    except OSError as exc:
        add(Check("disk.free", WARN, f"could not check disk space: {exc}"))
    for label, directory in (("inbox", bot.inbox), ("state", bot.bot_state_dir)):
        try:
            mode = directory.stat().st_mode & 0o777
        except OSError:
            continue
        if mode & 0o022:
            add(
                Check(
                    f"perm.{label}_dir",
                    WARN,
                    f"{directory} has mode {mode:04o}; other users can add or replace files the agent reads",
                    f"chmod 700 {directory}",
                )
            )
    inbox = _dir_size(bot.inbox)
    if inbox > 1024**3:
        add(Check("disk.inbox", WARN, f"inbox holds {_bytes(inbox)}", f"delete old files in {bot.inbox}"))
    return checks


def _start_fix(bot: BotConfig) -> str:
    if bot.systemd == "none":
        return f"aot relay --bot {bot.name}  (run it under a supervisor)"
    return f"{services.manage_cmd(bot.systemd)} enable --now {bot.unit_name}"


def summarize(checks: list[Check]) -> dict[str, Any]:
    return {
        "ok": not any(c.status == FAIL for c in checks),
        "fail": sum(c.status == FAIL for c in checks),
        "warn": sum(c.status == WARN for c in checks),
    }
