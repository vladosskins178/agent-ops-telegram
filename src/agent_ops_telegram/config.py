"""Configuration: one TOML file, one ``[bots.<name>]`` table per bot (one relay per bot)."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

from .security import InsecureFileError, check_private_file, parse_env_file, register_secret

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

DEFAULT_TEMPLATE = "[TG @{user} chat_id={chat_id} msg_id={msg_id}{files}{reply}]: {text}"

# Screens that must not receive pasted text: a pasted message would answer the dialog.
# Matched case-insensitively against the last ``blocked_scan_lines`` lines of the pane.
DEFAULT_BLOCKED = [
    r"Do you want to (proceed|make this edit|create|allow|overwrite)",
    r"Would you like to (run|make|apply) ",
    r"\[y/N\]|\[Y/n\]|\(y/n\)",
    r"Press Enter to continue",
    r"How is (Claude|the agent) doing this session",
    r"Do you trust the files in this folder",
]


class ConfigError(Exception):
    """The configuration is missing, malformed or unsafe."""


def default_config_path() -> Path:
    env = os.environ.get("AOT_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    user = Path(base, "agent-ops-telegram", "config.toml").expanduser()
    system = Path("/etc/agent-ops-telegram/config.toml")
    if not user.exists() and system.exists():
        return system
    return user


@dataclass
class BotConfig:
    name: str
    token_env: str = ""
    allowed_chats: list[int] = field(default_factory=list)
    allowed_users: list[int] = field(default_factory=list)
    default_chat: int = 0
    tmux_target: str = ""
    tmux_socket: str = ""
    tmux_bin: str = "tmux"
    start_command: str = ""
    start_grace: int = 20
    workdir: str = ""
    prompt_template: str = DEFAULT_TEMPLATE
    poll_timeout: int = 25
    paste_chunk: int = 1000
    long_message_chars: int = 6000
    submit_delay: float = 0.4
    submit_retries: int = 3
    submit_check_lines: int = 4
    bracketed_paste: bool = True
    blocked_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED))
    blocked_scan_lines: int = 12
    ack_reaction: str = "\N{EYES}"
    remote_commands: bool = True
    command_prefix: str = "/aot"
    inbox_dir: str = ""
    max_download_mb: int = 20
    skip_backlog: bool = True
    heartbeat_stale: int = 120
    service: str = ""
    restart_command: list[str] = field(default_factory=list)
    notify_cooldown: int = 900
    # filled in by load_config
    state_dir: Path = field(default=Path(), repr=False)
    env_file: Path | None = field(default=None, repr=False)
    api_base: str = "https://api.telegram.org"
    systemd: str = "system"
    config_path: Path | None = field(default=None, repr=False)

    @property
    def session(self) -> str:
        return self.tmux_target.split(":", 1)[0]

    @property
    def bot_state_dir(self) -> Path:
        return self.state_dir / self.name

    @property
    def inbox(self) -> Path:
        return Path(self.inbox_dir).expanduser() if self.inbox_dir else self.bot_state_dir / "inbox"

    @property
    def unit_name(self) -> str:
        return self.service or f"aot-relay@{self.name}.service"

    @property
    def alert_chat(self) -> int:
        if self.default_chat:
            return self.default_chat
        return self.allowed_chats[0] if self.allowed_chats else 0

    def token(self) -> str:
        """Read the bot token from the environment or the env file. Never logged or printed."""
        if not self.token_env:
            raise ConfigError(f"bot '{self.name}': token_env is not set")
        value = os.environ.get(self.token_env, "")
        if not value and self.env_file is not None and self.env_file.exists():
            try:
                value = parse_env_file(self.env_file).get(self.token_env, "")
            except (OSError, InsecureFileError) as exc:
                raise ConfigError(str(exc)) from exc
        value = value.strip()
        if not value:
            where = f"the environment or {self.env_file}" if self.env_file else "the environment"
            raise ConfigError(f"bot '{self.name}': {self.token_env} is not set in {where}")
        register_secret(value)
        return value


@dataclass
class Config:
    path: Path
    bots: dict[str, BotConfig]
    state_dir: Path
    systemd: str
    env_file: Path | None = None

    def bot(self, name: str | None) -> BotConfig:
        if name:
            if name not in self.bots:
                known = ", ".join(sorted(self.bots)) or "none"
                raise ConfigError(f"no bot named '{name}' in {self.path} (configured: {known})")
            return self.bots[name]
        env_name = os.environ.get("AOT_BOT", "")
        if env_name:
            return self.bot(env_name)
        if len(self.bots) == 1:
            return next(iter(self.bots.values()))
        if not self.bots:
            raise ConfigError(f"no bots configured in {self.path}; run `aot init`")
        raise ConfigError(f"several bots configured ({', '.join(sorted(self.bots))}); pass --bot NAME or set AOT_BOT")


_DEFAULT_ONLY = {"state_dir", "env_file", "api_base", "systemd"}
_BOT_FIELDS = {f.name: f for f in fields(BotConfig) if f.name not in _DEFAULT_ONLY | {"name", "config_path"}}


def _check_type(where: str, key: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where}.{key} must be true or false")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}.{key} must be an integer")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}.{key} must be a number")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{where}.{key} must be a string")
        return value
    if isinstance(default, list):
        if not isinstance(value, list):
            raise ConfigError(f"{where}.{key} must be a list")
        return value
    return value  # pragma: no cover


def _bot_from_table(name: str, table: dict[str, Any], defaults: dict[str, Any]) -> BotConfig:
    where = f"bots.{name}"
    if not NAME_RE.match(name):
        raise ConfigError(f"bot name '{name}' must be lowercase letters, digits, '-' or '_' (max 32)")
    bot = BotConfig(name=name)
    merged = {k: v for k, v in defaults.items() if k in _BOT_FIELDS}
    merged.update(table)
    for key, value in merged.items():
        if key not in _BOT_FIELDS:
            raise ConfigError(f"{where}: unknown setting '{key}'")
        setattr(bot, key, _check_type(where, key, value, getattr(bot, key)))
    for key in ("allowed_chats", "allowed_users"):
        ids = getattr(bot, key)
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise ConfigError(f"{where}.{key} must be a list of integer ids")
    if not all(isinstance(s, str) for s in bot.blocked_patterns + bot.restart_command):
        raise ConfigError(f"{where}: blocked_patterns and restart_command must be lists of strings")
    for pattern in bot.blocked_patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"{where}.blocked_patterns: bad regex {pattern!r}: {exc}") from exc
    if not bot.allowed_chats:
        raise ConfigError(f"{where}.allowed_chats is empty; nobody could talk to the agent (and nobody else may)")
    if bot.default_chat and bot.default_chat not in bot.allowed_chats:
        raise ConfigError(f"{where}.default_chat must be one of allowed_chats")
    if not bot.tmux_target:
        raise ConfigError(f"{where}.tmux_target is required (the tmux session the agent runs in)")
    if not re.match(r"^[A-Za-z0-9_-]+$", bot.session):
        # tmux silently renames sessions containing '.' or ':', so they would never match
        raise ConfigError(f"{where}.tmux_target: session name '{bot.session}' may only contain letters, digits, '-' and '_'")
    if any(c == 0 for c in bot.allowed_chats):
        raise ConfigError(f"{where}.allowed_chats contains 0, which is not a chat id")
    for key in ("max_download_mb", "heartbeat_stale", "notify_cooldown", "start_grace", "long_message_chars"):
        if getattr(bot, key) < 0:
            raise ConfigError(f"{where}.{key} must not be negative")
    if not bot.token_env:
        bot.token_env = "AOT_TOKEN_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", bot.token_env):
        raise ConfigError(f"{where}.token_env must be an environment variable name")
    if not 1 <= bot.poll_timeout <= 50:
        raise ConfigError(f"{where}.poll_timeout must be between 1 and 50 seconds")
    if bot.paste_chunk < 100 or bot.long_message_chars < bot.paste_chunk:
        raise ConfigError(f"{where}: paste_chunk must be >= 100 and long_message_chars >= paste_chunk")
    if bot.submit_check_lines < 1 or bot.blocked_scan_lines < 1 or bot.submit_retries < 0:
        raise ConfigError(f"{where}: submit_check_lines and blocked_scan_lines must be >= 1, submit_retries >= 0")
    if not bot.command_prefix.startswith("/"):
        raise ConfigError(f"{where}.command_prefix must start with '/'")
    try:
        bot.prompt_template.format(user="", chat_id=0, msg_id=0, files="", reply="", text="", bot="")
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(f"{where}.prompt_template is invalid: {exc}") from exc
    return bot


def load_config(path: Path | None = None, *, any_owner: bool = False) -> Config:
    """Load and validate the config. ``any_owner`` relaxes the ownership check (used by
    root writing unit files for another user's config); permissions are still checked."""
    path = (path or default_config_path()).expanduser()
    if not path.exists():
        raise ConfigError(f"config file {path} not found; run `aot init` (or set AOT_CONFIG / --config)")
    try:
        check_private_file(path, allow_group_read=False, any_owner=any_owner)
    except (OSError, InsecureFileError) as exc:
        raise ConfigError(str(exc)) from exc
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}. It must be readable by the user aot runs as.") from exc

    unknown = set(data) - {"defaults", "bots"}
    if unknown:
        raise ConfigError(f"{path}: unknown top-level table(s): {', '.join(sorted(unknown))}")
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError("[defaults] must be a table")
    for key in defaults:
        if key not in _BOT_FIELDS and key not in _DEFAULT_ONLY:
            raise ConfigError(f"defaults: unknown setting '{key}'")

    state_dir = Path(str(defaults.get("state_dir", "~/.local/state/agent-ops-telegram"))).expanduser()
    env_raw = defaults.get("env_file", "")
    env_file = Path(str(env_raw)).expanduser() if env_raw else None
    if env_file is not None and not env_file.is_absolute():
        env_file = (path.parent / env_file).resolve()
    systemd = str(defaults.get("systemd", "system"))
    if systemd not in {"system", "user", "none"}:
        raise ConfigError("defaults.systemd must be 'system', 'user' or 'none'")
    api_base = str(defaults.get("api_base", "https://api.telegram.org")).rstrip("/")
    if not re.match(r"^https?://[^/\s]+", api_base):
        raise ConfigError("defaults.api_base must be an http(s) URL")

    bots_table = data.get("bots", {})
    if not isinstance(bots_table, dict):
        raise ConfigError("[bots] must be a table of [bots.<name>] tables")
    bots: dict[str, BotConfig] = {}
    for name, table in bots_table.items():
        if not isinstance(table, dict):
            raise ConfigError(f"bots.{name} must be a table")
        bot = _bot_from_table(name, table, defaults)
        bot.state_dir = state_dir
        bot.env_file = env_file
        bot.api_base = api_base
        bot.systemd = systemd
        bot.config_path = path
        bots[name] = bot

    tokens = [b.token_env for b in bots.values()]
    dupes = {t for t in tokens if tokens.count(t) > 1}
    if dupes:
        raise ConfigError(f"several bots share token_env {', '.join(sorted(dupes))}; one relay per bot token")
    targets = [(b.tmux_socket, b.session) for b in bots.values()]
    if len(set(targets)) != len(targets):
        raise ConfigError("two bots use the same tmux session; give each bot its own session")
    return Config(path=path, bots=bots, state_dir=state_dir, systemd=systemd, env_file=env_file)
