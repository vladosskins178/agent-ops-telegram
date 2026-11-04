"""systemd unit files: a templated relay service (one instance per bot) and a watchdog timer."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from pathlib import Path

DOCS = "https://github.com/vladosskins178/agent-ops-telegram"
# POSIX portable user names (plus the trailing "$" Samba machine accounts use).
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}\$?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class UnitError(ValueError):
    """A value that cannot be written safely into a unit file."""


def check_user(user: str) -> str:
    if user and not USER_RE.match(user):
        raise UnitError(f"invalid user name {user!r} for User= (letters, digits, '_', '.', '-')")
    return user


def _check_value(value: str, what: str) -> str:
    """A newline would start a new directive in the unit file; refuse every control character."""
    if _CONTROL.search(value):
        raise UnitError(f"{what} contains a newline or control character, which a unit file cannot hold: {value!r}")
    return value


def aot_command() -> str:
    """How systemd should start aot: the script next to this interpreter, on PATH, or ``python -m``."""
    beside = Path(sys.executable).parent / "aot"
    if beside.exists():
        return str(beside)
    found = shutil.which("aot")
    if found:
        return str(Path(found).resolve())
    return f"{sys.executable} -m agent_ops_telegram"


def unit_dir(scope: str) -> Path:
    if scope == "user":
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(base) / "systemd" / "user"
    return Path("/etc/systemd/system")


def _user_line(scope: str, user: str) -> str:
    # Set User= even for root: systemd only sets HOME (which tmux and the agent need)
    # when User= is present.
    return f"User={user}\n" if scope == "system" and user else ""


def _home_of(user: str) -> Path:
    try:
        import pwd

        return Path(pwd.getpwnam(user).pw_dir) if user else Path.home()
    except (KeyError, ImportError):
        return Path.home()


def service_path(user: str = "", aot: str = "") -> str:
    """PATH for the services and, through them, for agent sessions they start.

    systemd's default PATH lacks ~/.local/bin, where pipx puts `aot` and where many
    agent CLIs install themselves, so we write an explicit one."""
    dirs: list[str] = []
    first = aot.split()[0] if aot else ""
    if first.startswith("/"):
        dirs.append(str(Path(first).parent))
    home = _home_of(user)
    dirs.append(str(home / ".local" / "bin"))
    mine = Path.home()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        # Writing units for another account: do not hand it directories in our own home.
        if home != mine and d.startswith(str(mine) + "/"):
            continue
        dirs.append(d)
    dirs += ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]
    seen: list[str] = []
    for d in dirs:
        if d and d.startswith("/") and d not in seen and not _CONTROL.search(d):
            seen.append(d)
    return ":".join(seen)


def _service_common(config: Path, scope: str, user: str, aot: str) -> str:
    return (
        f"{_user_line(scope, user)}"
        f"Environment={_env_quote('AOT_CONFIG=' + str(config))}\n"
        f"Environment={_env_quote('PATH=' + service_path(user, aot))}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "# The relay and the watchdog may start the agent's tmux server. With the default\n"
        "# KillMode=control-group, systemd would kill that server (and the agent) whenever\n"
        "# the relay restarts or the oneshot watchdog exits. Only kill our own process.\n"
        "KillMode=process\n"
        "# No PrivateTmp (tmux keeps its socket in /tmp/tmux-<uid>), no NoNewPrivileges and\n"
        "# no UMask: an agent session started from here inherits them. aot creates its own\n"
        "# files with private modes explicitly.\n"
    )


def _unit_head(description: str, scope: str) -> str:
    net = "After=network-online.target\nWants=network-online.target\n" if scope == "system" else ""
    return f"[Unit]\nDescription={description}\nDocumentation={DOCS}\n{net}"


def relay_unit(config: Path, scope: str, *, user: str = "", aot: str = "") -> str:
    aot = aot or aot_command()
    wanted = "default.target" if scope == "user" else "multi-user.target"
    return (
        _unit_head("agent-ops-telegram relay for bot %i", scope) + "StartLimitIntervalSec=0\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"{_service_common(config, scope, user, aot)}"
        f"ExecStart={_exec(aot)} relay --bot %i\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "# Exit 78: config or token missing, or the token was rejected (401). Restarting cannot fix that; wait for the user.\n"
        "RestartPreventExitStatus=78\n"
        "\n"
        "[Install]\n"
        f"WantedBy={wanted}\n"
    )


def watchdog_service(config: Path, scope: str, *, user: str = "", aot: str = "") -> str:
    aot = aot or aot_command()
    return (
        _unit_head("agent-ops-telegram watchdog (restarts stuck relays, removes duplicate pollers)", scope) + "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"{_service_common(config, scope, user, aot)}"
        f"ExecStart={_exec(aot)} watchdog\n"
    )


def watchdog_timer() -> str:
    return (
        "[Unit]\n"
        "Description=Run the agent-ops-telegram watchdog every 2 minutes\n"
        f"Documentation={DOCS}\n"
        "\n"
        "[Timer]\n"
        "OnBootSec=2min\n"
        "OnUnitActiveSec=2min\n"
        "AccuracySec=15s\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _env_quote(assignment: str) -> str:
    _check_value(assignment, "Environment= value")
    assignment = assignment.replace("%", "%%")  # systemd specifiers
    if any(c in assignment for c in ' "\\'):
        return '"' + assignment.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return assignment


def _exec(command: str) -> str:
    """ExecStart words: quote each part that has spaces, escape % specifiers."""
    _check_value(command, "ExecStart= command")
    module_form = " -m agent_ops_telegram"
    if command.endswith(module_form):
        parts = [command[: -len(module_form)], "-m", "agent_ops_telegram"]
    else:
        parts = [command]  # aot_command() returns one path, which may contain spaces
    out = []
    for part in parts:
        part = part.replace("%", "%%").replace("$", "$$")
        out.append('"' + part.replace("\\", "\\\\").replace('"', '\\"') + '"' if any(c in part for c in ' "\\') else part)
    return " ".join(out)


def all_units(config: Path, scope: str, *, user: str = "", aot: str = "") -> dict[str, str]:
    """Raises :class:`UnitError` for a bad user name or a value with control characters."""
    check_user(user)
    _check_value(str(config), "config path")
    return {
        "aot-relay@.service": relay_unit(config, scope, user=user, aot=aot),
        "aot-watchdog.service": watchdog_service(config, scope, user=user, aot=aot),
        "aot-watchdog.timer": watchdog_timer(),
    }


def write_units(directory: Path, units: dict[str, str]) -> list[Path]:
    """Write unit files atomically. Symlinks are refused: ``systemctl mask`` links a unit
    to /dev/null and ``systemctl link`` to a file elsewhere, and neither may be overwritten."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in units:
        path = directory / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise OSError(f"{path} is a symlink or not a regular file (masked or linked unit?); remove it first")
    written = []
    for name, text in units.items():
        path = directory / name
        tmp = directory / f".{name}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                os.fchmod(fh.fileno(), 0o644)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()
        written.append(path)
    return written


def enable_hint(scope: str, bots: list[str]) -> str:
    sudo = "" if scope == "user" or os.geteuid() == 0 else "sudo "
    flag = "--user " if scope == "user" else ""
    names = " ".join(shlex.quote(f"aot-relay@{b}.service") for b in bots)
    lines = [f"{sudo}systemctl {flag}daemon-reload", f"{sudo}systemctl {flag}enable --now {names} aot-watchdog.timer"]
    if scope == "user":
        lines.append("loginctl enable-linger $USER   # keep user services running after you log out")
    return "\n".join(lines)
