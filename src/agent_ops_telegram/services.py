"""systemd helpers (system or user scope) used by status, doctor and the watchdog."""

from __future__ import annotations

import shutil
import subprocess

from .config import BotConfig

WATCHDOG_TIMER = "aot-watchdog.timer"


def systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def systemctl(scope: str, *args: str, timeout: float = 30.0) -> tuple[int, str]:
    """Run ``systemctl [--user] args``; return (exit code, combined output). Never raises."""
    cmd = ["systemctl"]
    if scope == "user":
        cmd.append("--user")
    cmd += list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def unit_state(bot: BotConfig) -> str | None:
    """``active``, ``inactive``, ``failed``... or ``None`` when systemd is not used."""
    if bot.systemd == "none" or not systemctl_available():
        return None
    return _active_state(*systemctl(bot.systemd, "is-active", bot.unit_name))


# What `systemctl is-active` prints for a unit it could query.
_STATES = {"active", "inactive", "failed", "activating", "deactivating", "reloading", "refreshing", "maintenance"}


def _active_state(rc: int, out: str) -> str:
    """The unit state, or ``unknown (<short reason>)`` when systemctl itself failed
    (no D-Bus session for --user, permission denied, timeout...)."""
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if lines and lines[-1] in _STATES:
        return lines[-1]
    reason = lines[0] if lines else f"systemctl exit {rc}"
    reason = reason.removeprefix("Failed to ").rstrip(".")
    return f"unknown ({reason[:60]})"


def unit_loaded(bot: BotConfig) -> bool:
    """Whether systemd knows the unit file at all."""
    _, out = systemctl(bot.systemd, "show", "-p", "LoadState", "--value", bot.unit_name)
    return out.strip() not in {"not-found", ""}


def unit_exit_status(bot: BotConfig) -> int | None:
    """The exit status of the unit's last main process (``ExecMainStatus``), or ``None``
    when systemd is not used or the value cannot be read."""
    if bot.systemd == "none" or not systemctl_available():
        return None
    rc, out = systemctl(bot.systemd, "show", "-p", "ExecMainStatus", "--value", bot.unit_name)
    value = out.strip()
    return int(value) if rc == 0 and value.isdigit() else None


def manage_cmd(scope: str) -> str:
    """``systemctl``, ``sudo systemctl`` or ``systemctl --user`` as the reader needs to type it."""
    import os

    if scope == "user":
        return "systemctl --user"
    return "systemctl" if os.geteuid() == 0 else "sudo systemctl"


def unit_enabled(bot: BotConfig) -> bool:
    if bot.systemd == "none" or not systemctl_available():
        return False
    rc, _ = systemctl(bot.systemd, "is-enabled", "--quiet", bot.unit_name)
    return rc == 0


def timer_state(scope: str) -> str | None:
    if scope == "none" or not systemctl_available():
        return None
    return _active_state(*systemctl(scope, "is-active", WATCHDOG_TIMER))


def restart_relay(bot: BotConfig) -> tuple[bool, str]:
    if bot.restart_command:
        try:
            proc = subprocess.run(bot.restart_command, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    if bot.systemd == "none" or not systemctl_available():
        return False, "no systemd and no restart_command configured"
    rc, out = systemctl(bot.systemd, "restart", bot.unit_name, timeout=60)
    return rc == 0, out
