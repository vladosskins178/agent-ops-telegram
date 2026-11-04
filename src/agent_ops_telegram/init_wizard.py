"""``aot init``: write the config, the secrets file and the systemd units for one bot."""

from __future__ import annotations

import getpass
import json
import os
import re
import shlex
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from . import units
from .config import NAME_RE
from .security import InsecureFileError, parse_env_file, register_secret, write_private
from .telegram import TelegramAPI, TelegramError
from .textutil import one_line

DETECT_SETTLE = 8.0  # seconds to keep listening for other chats after the first message
TOKEN_SHAPE = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{30,}$")


class InitError(Exception):
    pass


@dataclass
class InitOptions:
    config: Path
    name: str = ""
    token: str = ""
    chats: list[int] = field(default_factory=list)
    users: list[int] = field(default_factory=list)
    tmux: str = ""
    start_command: str | None = None
    workdir: str = ""
    systemd: str = ""
    units_dir: Path | None = None
    write_units: bool = True
    env_file: Path | None = None
    state_dir: str = ""
    api_base: str = "https://api.telegram.org"
    offline: bool = False
    yes: bool = False
    detect_wait: float = 120.0


def _q(value: str) -> str:
    """A TOML basic string. JSON escapes are valid TOML; DEL must be escaped too."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def bot_table(
    name: str, token_env: str, chats: list[int], tmux: str, start_command: str, workdir: str, users: list[int] | None = None
) -> str:
    lines = [
        f"[bots.{name}]",
        f"token_env = {_q(token_env)}",
        f"allowed_chats = [{', '.join(str(c) for c in chats)}]",
    ]
    if users:
        lines.append(f"allowed_users = [{', '.join(str(u) for u in users)}]")
    lines += [f"default_chat = {chats[0]}", f"tmux_target = {_q(tmux)}"]
    if start_command:
        lines.append(f"start_command = {_q(start_command)}")
    if workdir:
        lines.append(f"workdir = {_q(workdir)}")
    return "\n".join(lines) + "\n"


def config_header(env_file: Path, state_dir: str, systemd: str, api_base: str) -> str:
    text = (
        "# agent-ops-telegram configuration. Keep this file private (chmod 600):\n"
        "# allowed_chats decides who can type into your agent.\n"
        "# Reference: https://github.com/vladosskins178/agent-ops-telegram/blob/main/docs/config.md\n"
        "\n"
        "[defaults]\n"
        f"env_file = {_q(str(env_file))}\n"
        f"state_dir = {_q(state_dir)}\n"
        f"systemd = {_q(systemd)}\n"
    )
    if api_base != "https://api.telegram.org":
        text += f"api_base = {_q(api_base)}\n"
    return text + "\n"


def upsert_env(path: Path, key: str, value: str) -> None:
    existing = parse_env_file(path) if path.exists() else {}
    lines = []
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
                continue
            lines.append(raw)
    elif not existing:
        lines.append("# Secrets for agent-ops-telegram. Mode 600; never commit this file.")
    lines.append(f"{key}={value}")
    write_private(path, "\n".join(lines) + "\n")


@dataclass
class SeenChat:
    chat_id: int
    kind: str
    username: str
    name: str
    sender: int
    sender_label: str

    @property
    def label(self) -> str:
        who = " ".join(p for p in (f"@{self.username}" if self.username else "", f'"{self.name}"' if self.name else "") if p)
        return f"{self.kind} chat {self.chat_id}" + (f" {who}" if who else "")


def _seen_chat(msg: dict[str, object]) -> SeenChat | None:
    chat = msg.get("chat")
    sender = msg.get("from")
    if not isinstance(chat, dict) or not chat.get("id"):
        return None
    sender = sender if isinstance(sender, dict) else {}
    name = " ".join(str(chat.get(k) or "") for k in ("first_name", "last_name")).strip() or str(chat.get("title") or "")
    sender_name = str(sender.get("username") or sender.get("first_name") or "")
    return SeenChat(
        chat_id=int(chat["id"]),
        kind=one_line(str(chat.get("type") or "chat"), 12),
        username=one_line(str(chat.get("username") or ""), 40),
        name=one_line(name, 40),
        sender=int(sender.get("id") or 0),
        sender_label=one_line(sender_name, 40),
    )


def detect_chats(
    api: TelegramAPI,
    out: TextIO,
    wait: float,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    settle: float | None = None,
) -> list[SeenChat]:
    """Wait for messages to the bot and return every chat that wrote during the window.

    After the first message, keep listening for ``settle`` more seconds, so that a stranger
    who writes at the same moment is shown next to you instead of being offered alone.
    Updates that were already waiting are skipped (the relay skips them on its first start
    too); the new messages are not confirmed, so the relay still receives them."""
    settle = DETECT_SETTLE if settle is None else settle
    started = clock()
    deadline = started + wait
    seen: dict[int, SeenChat] = {}
    offset = 0
    try:
        old = api.get_updates(-1, 0, limit=1)
        offset = int(old[-1]["update_id"]) + 1 if old else 0
    except TelegramError:
        pass
    while clock() < deadline:
        t0 = clock()
        try:
            # Once something arrived, poll without waiting: the same unconfirmed updates come back.
            timeout = 0 if seen else int(max(1, min(20, deadline - clock())))
            updates = api.get_updates(offset, timeout)
        except TelegramError as exc:
            if exc.conflict:
                raise InitError("another program is polling this bot (409 Conflict); stop it or pass --chat ID") from exc
            raise InitError(f"getUpdates failed: {exc}") from exc
        for upd in updates:
            msg = upd.get("message") or {}
            # Only messages sent while we wait: an old message from a stranger must not be offered.
            if float(msg.get("date") or 0) < started - 30:
                if not seen:
                    offset = max(offset, int(upd.get("update_id", 0)) + 1)
                continue
            chat = _seen_chat(msg)
            if chat is not None and chat.chat_id not in seen:
                if not seen:
                    deadline = min(deadline, clock() + settle)
                seen[chat.chat_id] = chat
        if seen or clock() - t0 < 1:
            sleep(1)  # never spin if the server answers at once
    return sorted(seen.values(), key=lambda c: c.chat_id)


def _ask(prompt: str, default: str, inp: Callable[[str], str]) -> str:
    answer = inp(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return answer or default


def run_init(opts: InitOptions, out: TextIO = sys.stdout, inp: Callable[[str], str] | None = None) -> int:
    if inp is None:
        inp = input  # looked up at call time
    interactive = not opts.yes
    cfg = opts.config.expanduser()
    if cfg.exists() and (cfg.is_symlink() or not cfg.is_file()):
        raise InitError(f"{cfg} is not a regular file; choose another --config path")
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        out.write(
            f"Note: you are running aot init through sudo. The config goes to {cfg} and the relay will "
            f"run as root, typing into root's tmux sessions. Run it as {os.environ['SUDO_USER']} without sudo "
            "unless the agent really runs as root.\n"
        )
    existing_text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    existing_env: Path | None = None
    existing_scope = ""
    existing_token_envs: set[str] = set()
    if existing_text:
        from .config import ConfigError, load_config

        try:
            current = load_config(cfg)
        except ConfigError as exc:
            raise InitError(f"{cfg} exists but cannot be loaded ({exc}); fix it before adding a bot") from exc
        existing_env, existing_scope = current.env_file, current.systemd
        existing_token_envs = {b.token_env for b in current.bots.values()}
    existing_names = set(re.findall(r"^\[bots\.([a-z0-9_-]+)\]", existing_text, re.MULTILINE))

    name = opts.name or (_ask("Bot name (short id used for the service and tmux session)", "agent", inp) if interactive else "agent")
    if not NAME_RE.match(name):
        raise InitError("bot name must be lowercase letters, digits, '-' or '_'")
    if name in existing_names:
        raise InitError(f"bot '{name}' already exists in {cfg}; edit the file or choose another name")

    token = opts.token.strip()
    if not token and interactive:
        out.write("Create a bot with @BotFather (/newbot) and paste its token. Input is hidden.\n")
        token = getpass.getpass("Bot token: ").strip()
    if not token:
        raise InitError("no token given; pass --token-stdin or --token-env VAR")
    if not TOKEN_SHAPE.match(token):
        raise InitError("that does not look like a bot token (expected 123456789:AA...)")
    register_secret(token)

    if existing_text:
        if existing_env is None:
            raise InitError(f"{cfg} has no env_file in [defaults], so there is nowhere to store the token; add one first")
        if opts.env_file and opts.env_file.expanduser().resolve() != existing_env.resolve():
            raise InitError(f"{cfg} already uses env_file {existing_env}; drop --env-file or use the same path")
    env_file = existing_env or opts.env_file or cfg.parent / "secrets.env"
    if env_file.exists() and (env_file.is_symlink() or not env_file.is_file()):
        raise InitError(f"{env_file} is not a regular file")
    token_env = "AOT_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
    try:
        existing_secrets = parse_env_file(env_file) if env_file.exists() else {}
    except (OSError, InsecureFileError) as exc:
        raise InitError(str(exc)) from exc
    for key, value in existing_secrets.items():
        if value.strip() == token and key != token_env:
            raise InitError(f"this token is already configured as {key}; one relay per bot token (two would split messages)")
    if token_env in existing_token_envs:
        raise InitError(f"bot name '{name}' maps to {token_env}, which another bot already uses; choose a different name")

    api = TelegramAPI(token, opts.api_base, retries=1, timeout=20)
    username = "?"
    if not opts.offline:
        try:
            username = str(api.get_me().get("username", "?"))
        except TelegramError as exc:
            raise InitError(f"Telegram did not accept the token: {exc}") from exc
        out.write(f"Token OK: @{username}\n")

    chats = list(opts.chats)
    users = list(opts.users)
    if not chats:
        if opts.offline or not interactive:
            raise InitError("pass --chat ID (your Telegram user id); find it by messaging the bot and running `aot init` interactively")
        out.write(f"Now open Telegram and send any message to @{username}. Waiting up to {int(opts.detect_wait)} s...\n")
        out.flush()
        found = detect_chats(api, out, opts.detect_wait)
        if not found:
            raise InitError("no message arrived; run again, or pass --chat ID")
        out.write(f"{len(found)} chat(s) wrote to @{username}:\n")
        for chat in found:
            sender = f"user {chat.sender}" + (f" ({chat.sender_label})" if chat.sender_label else "")
            out.write(f"  - {chat.label}, sent by {sender}\n")
        only_you = len(found) == 1 and found[0].kind == "private"
        if not only_you:
            out.write(
                "More than one chat (or a group) wrote during the wait. Allow only the chat you recognize as yours:\n"
                "everyone you allow can type into your agent.\n"
            )
        out.flush()
        for chat in found:
            default = "y" if only_you else "n"
            if _ask(f"Allow {chat.label} to send prompts to the agent? (y/n)", default, inp).lower().startswith("y"):
                chats.append(chat.chat_id)
                if chat.chat_id < 0 and chat.sender and not users:
                    answer = _ask(
                        "In a group every member could type into the agent. Only accept messages from user id (empty: everyone)",
                        str(chat.sender),
                        inp,
                    )
                    if answer.strip():
                        try:
                            users.append(int(answer))
                        except ValueError as exc:
                            raise InitError(f"not a user id: {answer}") from exc
        if not chats:
            raise InitError("no chat allowed; run again, or pass --chat ID")
    if any(c < 0 for c in chats) and not users:
        out.write(
            "Warning: a group chat is allowed without --allowed-user. Every member of that group will be able to type into your agent.\n"
        )

    tmux = opts.tmux or (_ask("tmux session the agent runs in", name, inp) if interactive else name)
    start_command = opts.start_command
    if start_command is None:
        start_command = (
            _ask("Command that starts the agent if the session is missing (empty: never start it)", "claude --continue || claude", inp)
            if interactive
            else ""
        )
    workdir = opts.workdir or (_ask("Working directory for the agent", os.getcwd(), inp) if interactive and start_command else "")
    systemd = opts.systemd or existing_scope
    if not systemd:
        systemd = "system" if os.geteuid() == 0 else "user"
        if interactive:
            systemd = _ask(
                "Run the services as system units (/etc/systemd/system) or user units, or none? (system/user/none)", systemd, inp
            )
    if systemd not in {"system", "user", "none"}:
        raise InitError("--systemd must be system, user or none")

    state_dir = opts.state_dir or (
        "/var/lib/agent-ops-telegram" if os.geteuid() == 0 and str(cfg).startswith("/etc/") else "~/.local/state/agent-ops-telegram"
    )

    if existing_text:
        text = existing_text.rstrip("\n") + "\n\n" + bot_table(name, token_env, chats, tmux, start_command, workdir, users)
    else:
        text = config_header(env_file, state_dir, systemd, opts.api_base) + bot_table(
            name, token_env, chats, tmux, start_command, workdir, users
        )

    # Validate the new config before touching anything: a bad edit must not break the bots
    # that already work.
    from .config import ConfigError, load_config

    if cfg.is_symlink():
        raise InitError(f"{cfg} is a symlink; choose another --config path")
    candidate = cfg.with_name(f".{cfg.name}.new")
    try:
        if candidate.is_symlink() or candidate.exists():
            candidate.unlink()
        write_private(candidate, text)
        try:
            loaded = load_config(candidate)
        except ConfigError as exc:
            raise InitError(f"the resulting config would be invalid: {exc}") from exc
        finally:
            candidate.unlink(missing_ok=True)
        upsert_env(env_file, token_env, token)
        write_private(cfg, text)
    except OSError as exc:
        raise InitError(f"could not write the configuration: {exc}") from exc
    out.write(f"Wrote {cfg} (mode 600)\nStored the token in {env_file} as {token_env} (mode 600)\n")
    scope = loaded.systemd
    start_hint = ""
    written_ok = False
    if opts.write_units and scope != "none":
        directory = opts.units_dir or units.unit_dir(scope)
        user = getpass.getuser()
        try:
            written = units.write_units(directory, units.all_units(cfg, scope, user=user))
            written_ok = True
            out.write("Wrote " + ", ".join(str(p) for p in written) + "\n")
            if directory != units.unit_dir(scope):
                out.write(f"(These are not in {units.unit_dir(scope)}; copy them there before the commands below.)\n")
        except PermissionError:
            written_ok = True  # the enable commands apply once root has written them
            out.write(
                f"Could not write units to {directory} (permission denied). Write them as root:\n"
                f"  sudo {shlex.quote(units.aot_command())} --config {shlex.quote(str(cfg))} units --write --scope {scope} --user {shlex.quote(user)}\n"
            )
        except (OSError, units.UnitError) as exc:
            out.write(f"Did not write the unit files: {exc}\n")
        if written_ok:
            start_hint = units.enable_hint(scope, [name])
    units_failed = opts.write_units and scope != "none" and not written_ok
    target_file = shlex.quote(workdir.rstrip("/") + "/CLAUDE.md") if workdir else "<the agent's project folder>/CLAUDE.md"
    from .config import default_config_path

    default_cfg = os.environ.pop("AOT_CONFIG", None)
    try:
        cfg_flag = "" if cfg.resolve() == default_config_path().resolve() else f" --config {shlex.quote(str(cfg))}"
    finally:
        if default_cfg is not None:
            os.environ["AOT_CONFIG"] = default_cfg
    out.write(
        "\nNext steps:\n"
        f"1. Tell the agent how to answer (before it starts, so it reads it; restart an agent that is already running):\n"
        f"   aot{cfg_flag} instructions --bot {name} >> {target_file}   (or AGENTS.md)\n"
    )
    if start_hint:
        out.write("2. Start the relay and the watchdog:\n" + "".join(f"   {line}\n" for line in start_hint.splitlines()))
    elif units_failed:
        out.write(
            f"2. Fix the unit-file problem above, run `aot{cfg_flag} units --write`, then enable aot-relay@{name}.service and aot-watchdog.timer.\n"
        )
    elif scope != "none":
        out.write(
            f"2. Write the unit files with `aot{cfg_flag} units --write`, then enable aot-relay@{name}.service and aot-watchdog.timer.\n"
        )
    else:
        out.write(
            f"2. Run `aot{cfg_flag} relay --bot {name}` under your own supervisor, and `aot{cfg_flag} watchdog` from cron every\n"
            "   2 minutes (give the bot a restart_command so the watchdog can restart the relay; see docs/config.md).\n"
        )
    if start_command and start_hint:
        out.write(f"3. The agent session '{tmux}' is started on the first message (or by the watchdog within 2 minutes).\n")
    elif start_command:
        out.write(f"3. The agent session '{tmux}' is started on the first message.\n")
    else:
        out.write(f"3. Start the agent: tmux new -s {tmux} '<agent command>'\n")
    out.write(f"4. Check everything: aot{cfg_flag} doctor --bot {name}, then message the bot from your phone.\n")
    return 0
