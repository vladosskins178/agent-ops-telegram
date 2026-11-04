"""Find relay processes on this machine (Linux /proc), to catch duplicate pollers.

Only processes of the same user that run ``aot relay --bot NAME`` with the same config
count. Wrappers such as ``sudo aot relay ...`` or ``timeout 1d aot relay ...`` are not
the relay itself and are never matched."""

from __future__ import annotations

import os
from pathlib import Path

PROC = Path("/proc")


def _argv(pid_dir: Path) -> list[str]:
    try:
        raw = (pid_dir / "cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def _aot_args(argv: list[str]) -> list[str] | None:
    """The arguments after the aot program itself, or None if this is not aot."""
    if not argv:
        return None
    first = Path(argv[0]).name
    if first in {"aot", "agent-ops-telegram"}:
        return argv[1:]
    if first.startswith("python"):
        rest = argv[1:]
        while rest and rest[0].startswith("-") and rest[0] not in {"-m"}:
            rest = rest[1:]  # interpreter flags such as -u or -B
        if rest[:2] == ["-m", "agent_ops_telegram"]:
            return rest[2:]
        if rest and Path(rest[0]).name in {"aot", "agent-ops-telegram"}:
            return rest[1:]
    return None


def relay_bot(argv: list[str]) -> str | None:
    """The bot name of an ``aot relay --bot NAME`` command line, else ``None``."""
    args = _aot_args(argv)
    if args is None:
        return None
    i = 0
    while i < len(args) and args[i].startswith("-"):  # global options before the subcommand
        i += 2 if args[i] == "--config" else 1
    if i >= len(args) or args[i] != "relay":
        return None
    tail = args[i + 1 :]
    for i, arg in enumerate(tail):
        if arg == "--bot" and i + 1 < len(tail):
            return tail[i + 1]
        if arg.startswith("--bot="):
            return arg.split("=", 1)[1]
    return None


def is_relay_argv(argv: list[str], bot: str) -> bool:
    return relay_bot(argv) == bot


def all_relays(proc_root: Path = PROC) -> list[tuple[int, str, Path | None]]:
    """``(pid, bot, config path)`` for every ``aot relay`` process of this user."""
    if not proc_root.exists():
        return []
    me, uid = os.getpid(), os.geteuid()
    found: list[tuple[int, str, Path | None]] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        argv = _argv(entry)
        bot = relay_bot(argv)
        if bot is None:
            continue
        config = _config_of(entry, argv)
        path: Path | None = None
        if config is not None:
            path = Path(config).expanduser()
            if not path.is_absolute():
                try:
                    path = (entry / "cwd").readlink() / path
                except OSError:
                    path = None
        found.append((int(entry.name), bot, path))
    return sorted(found)


def _config_of(pid_dir: Path, argv: list[str]) -> str | None:
    args = _aot_args(argv) or []
    for i, arg in enumerate(args):
        if arg == "--config" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
    try:
        env = dict(
            item.decode("utf-8", "replace").split("=", 1) for item in (pid_dir / "environ").read_bytes().split(b"\0") if b"=" in item
        )
    except OSError:
        return None
    if env.get("AOT_CONFIG"):
        return env["AOT_CONFIG"]
    # No explicit config: work out the default the way that process would.
    base = env.get("XDG_CONFIG_HOME") or (env["HOME"] + "/.config" if env.get("HOME") else "")
    if not base:
        return None
    user = Path(base) / "agent-ops-telegram" / "config.toml"
    system = Path("/etc/agent-ops-telegram/config.toml")
    return str(system if not user.exists() and system.exists() else user)


def _same_path(a: str, b: Path, cwd: Path | None) -> bool:
    p = Path(a).expanduser()
    if not p.is_absolute() and cwd is not None:
        p = cwd / p
    try:
        return p.resolve() == b.resolve()
    except OSError:
        return False


def relay_pids(bot: str, config: Path | None = None, proc_root: Path = PROC) -> list[int]:
    """PIDs of this user's ``aot relay --bot <bot>`` processes for ``config``, oldest first.

    With ``config=None`` the config is not compared."""
    if not proc_root.exists():
        return []
    me = os.getpid()
    uid = os.geteuid()
    found: list[tuple[int, int]] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        argv = _argv(entry)
        if not is_relay_argv(argv, bot):
            continue
        if config is not None:
            theirs = _config_of(entry, argv)
            try:
                cwd: Path | None = (entry / "cwd").readlink()
            except OSError:
                cwd = None
            if theirs is not None and not _same_path(theirs, config, cwd):
                continue
        found.append((_start_time(entry), int(entry.name)))
    return [pid for _, pid in sorted(found)]


def _stat_fields(pid_dir: Path) -> list[str]:
    try:
        return (pid_dir / "stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return []


def _start_time(pid_dir: Path) -> int:
    fields = _stat_fields(pid_dir)
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return 0


def start_epoch(pid: int, proc_root: Path = PROC) -> float | None:
    """When ``pid`` started (Unix time), from /proc; ``None`` if unknown."""
    ticks = _start_time(proc_root / str(pid))
    if not ticks:
        return None
    try:
        btime = next(int(line.split()[1]) for line in (proc_root / "stat").read_text().splitlines() if line.startswith("btime "))
        hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    except (OSError, StopIteration, ValueError, IndexError):
        return None
    return btime + ticks / hz


def ancestors(pid: int, proc_root: Path = PROC) -> set[int]:
    """Parent, grandparent... of ``pid`` (never kill these: they may be its supervisor)."""
    found: set[int] = set()
    current = pid
    while True:
        fields = _stat_fields(proc_root / str(current))
        try:
            parent = int(fields[1])
        except (IndexError, ValueError):
            break
        if parent <= 1 or parent in found or parent == pid:
            break
        found.add(parent)
        current = parent
    return found


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
