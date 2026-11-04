"""Command-line entry point: ``aot <command>``."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .config import BotConfig, Config, ConfigError, default_config_path, load_config
from .security import redact

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
# sysexits EX_CONFIG. The relay unit has RestartPreventExitStatus=78: a relay that cannot
# start until someone fixes the config or the token is not restarted every 5 seconds.
EXIT_CONFIG = 78


def _out() -> TextIO:
    return sys.stdout


def _err(message: str) -> None:
    sys.stderr.write(f"aot: {redact(message)}\n")


def _config(args: argparse.Namespace) -> Config:
    return load_config(Path(args.config).expanduser() if args.config else None)


def _bots(cfg: Config, name: str | None) -> list[BotConfig]:
    if name:
        return [cfg.bot(name)]
    return list(cfg.bots.values())


def _age(ts: float, now: float) -> str:
    if not ts:
        return "-"
    s = max(int(now - ts), 0)
    if s < 120:
        return f"{s}s ago"
    if s < 7200:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


# ================================================================ relay


def cmd_relay(args: argparse.Namespace) -> int:
    from .logs import setup_logging
    from .relay import FatalRelayError, Relay, install_signal_handlers
    from .state import LockBusy
    from .telegram import TelegramAPI
    from .tmux import Tmux

    bot = None
    try:
        cfg = _config(args)
        bot = cfg.bot(args.bot)
        token = bot.token()
    except ConfigError as exc:
        if bot is not None:  # tell the watchdog not to restart it (and why)
            _write_fatal(bot.bot_state_dir, str(exc), "config_error")
        _err(f"{exc}\nThe relay cannot start until this is fixed; systemd will not restart it (exit {EXIT_CONFIG}).")
        _err("After fixing it, start it again (systemctl restart aot-relay@<bot>.service) and run `aot doctor`.")
        return EXIT_CONFIG
    logger = setup_logging(bot.name, bot.bot_state_dir)
    api = TelegramAPI(token, bot.api_base)
    tmux = Tmux(bot.tmux_target, socket=bot.tmux_socket, binary=bot.tmux_bin)
    relay = Relay(bot, api, tmux, logger=logger, config_path=cfg.path)
    install_signal_handlers(relay)
    try:
        relay.run()
    except LockBusy as exc:
        logger.error("%s; refusing to start a second poller (it would split messages). Stop the other one first.", exc)
        # Wait before exiting so systemd's restart loop stays slow; the next attempt takes
        # over as soon as the other poller is gone.
        time.sleep(30)
        return 3
    except FatalRelayError as exc:
        # A rejected token does not fix itself: exit 78 so systemd does not restart it. The
        # note in health.json tells the watchdog not to restart it either.
        _write_fatal(bot.bot_state_dir, str(exc), "token_rejected")
        logger.critical("%s. Put a valid token in %s, then restart the relay; exiting with %d.", exc, bot.token_env, EXIT_CONFIG)
        _err(f"{exc}\nFix {bot.token_env} (see `aot doctor --bot {bot.name}`), then start the relay again (exit {EXIT_CONFIG}).")
        return EXIT_CONFIG
    except (KeyboardInterrupt, SystemExit):
        logger.info("relay stopped")
    return EXIT_OK


def _write_fatal(bot_state_dir: Path, message: str, reason: str) -> None:
    """Record in health.json why the relay exited with 78: ``token_rejected`` or ``config_error``.

    Only when no other relay runs for this state directory (its lock is free): a running
    relay owns health.json, and a fatal note there would stop the watchdog from restarting it."""
    from .state import BotState, LockBusy, PollerLock, state_lock_path

    lock = PollerLock(state_lock_path(bot_state_dir), "runs for this bot's state directory")
    try:
        lock.acquire(attempts=1)
    except LockBusy as exc:
        _err(f"not recording this error in health.json: {exc}")
        return
    except OSError:
        return  # the state directory is not writable: health.json cannot be either
    try:
        BotState(bot_state_dir).write_health(fatal=message, fatal_reason=reason, fatal_at=time.time())
    except OSError:
        pass
    finally:
        lock.release()


# ================================================================ send


def _read_text(args: argparse.Namespace) -> str:
    parts: list[str] = list(args.text or [])
    if parts == ["-"] or (not parts and not sys.stdin.isatty() and not args.photo and not args.document):
        return sys.stdin.read()
    return " ".join(parts)


def cmd_send(args: argparse.Namespace) -> int:
    from .send import send_files, send_text
    from .telegram import TelegramAPI, TelegramError

    cfg = _config(args)
    bot = cfg.bot(args.bot)
    chat = args.chat if args.chat is not None else bot.alert_chat
    if chat not in bot.allowed_chats:
        _err(f"chat {chat} is not in allowed_chats for bot '{bot.name}'; aot only sends to allow-listed chats")
        return EXIT_USAGE
    text = _read_text(args)
    parse_mode = "MarkdownV2" if args.markdown else "HTML" if args.html else None
    if not text.strip() and not args.photo and not args.document:
        _err("nothing to send (give text, '-' for stdin, --photo or --document)")
        return EXIT_USAGE
    api = TelegramAPI(bot.token(), bot.api_base, retries=args.retries)
    ids: list[int] = []
    notes: list[str] = []
    try:
        caption_used = False
        reply_to = args.reply_to  # only the first message sent is the reply
        for kind, paths in (("photo", args.photo), ("document", args.document)):
            if paths:
                res = send_files(
                    api,
                    chat,
                    kind,
                    [Path(p) for p in paths],
                    caption="" if caption_used else text.strip(),
                    parse_mode=parse_mode,
                    reply_to=reply_to,
                )
                reply_to = None
                caption_used = caption_used or bool(text.strip())
                ids += res.message_ids
                notes += res.notes
        if not caption_used and text.strip():
            res = send_text(api, chat, text, parse_mode=parse_mode, reply_to=reply_to)
            ids += res.message_ids
            notes += res.notes
    except TelegramError as exc:
        _err(f"send failed: {exc.description}" + (f" (after {len(ids)} message(s) were sent)" if ids else ""))
        if args.json:
            json.dump({"ok": False, "error": exc.description, "message_ids": ids}, _out())
            _out().write("\n")
        return EXIT_FAIL
    except (OSError, ValueError) as exc:
        _err(str(exc))
        return EXIT_USAGE
    for note in notes:
        sys.stderr.write(f"aot: note: {note}\n")
    if args.json:
        json.dump({"ok": True, "message_ids": ids}, _out())
        _out().write("\n")
    else:
        _out().write("sent\n" if len(ids) <= 1 else f"sent ({len(ids)} messages)\n")
    return EXIT_OK


# ================================================================ doctor / status / logs / watchdog


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import FAIL, INFO, OK, WARN, run_doctor, summarize

    cfg = _config(args)
    report: list[dict[str, Any]] = []
    failed = False
    out = _out()
    color = out.isatty() and not os.environ.get("NO_COLOR")
    paint = {OK: "\033[32m", INFO: "\033[36m", WARN: "\033[33m", FAIL: "\033[31m"}
    for bot in _bots(cfg, args.bot):
        checks = run_doctor(bot, offline=args.offline, fix=args.fix)
        summary = summarize(checks)
        failed = failed or not summary["ok"] or (args.strict and summary["warn"] > 0)
        if args.json:
            report.append({"bot": bot.name, **summary, "checks": [c.to_dict() for c in checks]})
            continue
        out.write(f"aot doctor - bot '{bot.name}' (tmux {bot.tmux_target}, config {cfg.path})\n")
        for c in checks:
            tag = c.status.upper().ljust(4)
            if color:
                tag = f"{paint[c.status]}{tag}\033[0m"
            out.write(f"  {tag} {c.id:<24} {c.summary}\n")
            if c.fix and c.status in (WARN, FAIL):
                out.write(f"       {'':<24} fix: {c.fix}\n")
        verdict = "all good" if summary["ok"] and not summary["warn"] else f"{summary['fail']} failing, {summary['warn']} warning(s)"
        out.write(f"  -> {verdict}\n\n")
    if args.json:
        json.dump({"ok": not failed, "config": str(cfg.path), "bots": report}, out, indent=2)
        out.write("\n")
    return EXIT_FAIL if failed else EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    from .procs import relay_pids
    from .state import BotState
    from .tmux import Tmux

    cfg = _config(args)
    now = time.time()
    rows: list[dict[str, Any]] = []
    for bot in _bots(cfg, args.bot):
        health = BotState(bot.bot_state_dir).read_health()
        pids = relay_pids(bot.name, bot.config_path)
        tmux = Tmux(bot.tmux_target, socket=bot.tmux_socket, binary=bot.tmux_bin)
        rows.append(
            {
                "bot": bot.name,
                "relay": f"pid {pids[0]}" if len(pids) == 1 else (f"{len(pids)} PROCESSES" if pids else "stopped"),
                "service": _service_label(bot),
                "heartbeat": _age(float(health.get("heartbeat") or 0), now),
                "last_message": _age(float(health.get("last_update") or 0), now),
                "queue": BotState(bot.bot_state_dir).queue_len(),
                "tmux": f"{bot.session}: " + ("up" if tmux.has_session() else "MISSING"),
            }
        )
    out = _out()
    if args.json:
        json.dump(rows, out, indent=2)
        out.write("\n")
        return EXIT_OK
    headers = ["bot", "relay", "service", "heartbeat", "last_message", "queue", "tmux"]
    widths = {h: max(len(h), *(len(str(r[h])) for r in rows)) for h in headers} if rows else {h: len(h) for h in headers}
    out.write("  ".join(h.upper().ljust(widths[h]) for h in headers).rstrip() + "\n")
    for r in rows:
        out.write("  ".join(str(r[h]).ljust(widths[h]) for h in headers).rstrip() + "\n")
    return EXIT_OK


def _service_label(bot: BotConfig) -> str:
    from . import services

    state = services.unit_state(bot)
    if state is None:
        return "-"
    if state != "active" and not services.unit_loaded(bot):
        return "not installed"
    return state


def cmd_logs(args: argparse.Namespace) -> int:
    from .logs import log_file, tail_file

    cfg = _config(args)
    bot = cfg.bot(args.bot)
    if args.journal:
        cmd = ["journalctl"] + (["--user-unit"] if bot.systemd == "user" else ["-u"]) + [bot.unit_name, "-n", str(args.lines), "--no-pager"]
        if args.follow:
            cmd.append("-f")
        os.execvp(cmd[0], cmd)
    path = log_file(bot.bot_state_dir)
    if not path.exists():
        _err(f"no log yet at {path} (has the relay run?); try --journal")
        return EXIT_FAIL
    try:
        tail_file(path, args.lines, _out(), follow=args.follow)
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def cmd_watchdog(args: argparse.Namespace) -> int:
    import logging

    from .security import RedactingFilter
    from .watchdog import watchdog_bot

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())
    cfg = _config(args)
    acted = False
    for bot in _bots(cfg, args.bot):
        notes: list[str] = []
        try:
            actions = watchdog_bot(bot, dry_run=args.dry_run, config_path=str(cfg.path), notes=notes)
        except Exception as exc:  # one broken bot must not stop the checks for the others
            logging.getLogger("aot.watchdog").exception("%s: watchdog check failed", bot.name)
            actions = [f"check failed: {type(exc).__name__}: {exc}"]
        for action in actions:
            _out().write(f"{bot.name}: {action}\n")
        for note in notes if args.verbose else []:
            _out().write(f"{bot.name}: {note}\n")
        acted = acted or bool(actions) or bool(notes)
    if not acted and args.verbose:
        _out().write("all relays healthy\n")
    return EXIT_OK


# ================================================================ init / units / instructions / skill


def cmd_init(args: argparse.Namespace) -> int:
    from .init_wizard import InitError, InitOptions, run_init

    token = ""
    if args.token_stdin:
        token = sys.stdin.readline().strip()
    elif args.token_env:
        token = os.environ.get(args.token_env, "")
        if not token:
            _err(f"{args.token_env} is empty")
            return EXIT_USAGE
    opts = InitOptions(
        config=Path(args.config).expanduser() if args.config else default_config_path(),
        name=args.name or "",
        token=token,
        chats=list(args.chat or []),
        users=list(args.allowed_user or []),
        tmux=args.tmux or "",
        start_command=args.start_command,
        workdir=args.workdir or "",
        systemd=args.systemd or "",
        units_dir=Path(args.units_dir) if args.units_dir else None,
        write_units=not args.no_units,
        env_file=Path(args.env_file).expanduser() if args.env_file else None,
        state_dir=args.state_dir or "",
        api_base=args.api_base,
        offline=args.offline,
        yes=args.yes,
    )
    try:
        return run_init(opts, _out())
    except InitError as exc:
        _err(str(exc))
        return EXIT_FAIL
    except (EOFError, KeyboardInterrupt):
        _err("cancelled")
        return EXIT_FAIL


def cmd_units(args: argparse.Namespace) -> int:
    import getpass

    from . import units

    cfg = load_config(Path(args.config).expanduser() if args.config else None, any_owner=os.geteuid() == 0)
    scope = args.scope or cfg.systemd
    if scope == "none":
        _err("defaults.systemd is 'none'; pass --scope system or --scope user")
        return EXIT_USAGE
    user = args.user or os.environ.get("SUDO_USER") or getpass.getuser()
    try:
        units.check_user(user)
    except units.UnitError as exc:
        _err(str(exc))
        return EXIT_USAGE
    if scope == "system":
        try:
            import pwd

            owner = pwd.getpwuid(cfg.path.stat().st_uid).pw_name
            if owner != user:
                sys.stderr.write(
                    f"aot: warning: the services run as {user}, but {cfg.path} belongs to {owner}; {user} must own the config (chown)\n"
                )
        except (KeyError, OSError):
            pass
    try:
        files = units.all_units(cfg.path, scope, user=user)
    except units.UnitError as exc:
        _err(str(exc))
        return EXIT_USAGE
    if not args.write:
        for name, text in files.items():
            _out().write(f"# ---- {name}\n{text}\n")
        return EXIT_OK
    directory = Path(args.dir) if args.dir else units.unit_dir(scope)
    try:
        written = units.write_units(directory, files)
    except PermissionError:
        _err(f"permission denied writing to {directory}")
        return EXIT_FAIL
    except OSError as exc:
        _err(str(exc))
        return EXIT_FAIL
    note = (
        f"(These are not in {units.unit_dir(scope)}; copy them there before the commands below.)\n"
        if directory != units.unit_dir(scope)
        else ""
    )
    _out().write("Wrote " + ", ".join(map(str, written)) + "\n" + note + units.enable_hint(scope, sorted(cfg.bots)) + "\n")
    return EXIT_OK


def cmd_instructions(args: argparse.Namespace) -> int:
    from .instructions import agent_instructions

    name, prefix = args.bot or "", "/aot"
    try:
        cfg = _config(args)
        bot = cfg.bot(args.bot)
        name, prefix = bot.name, bot.command_prefix
    except ConfigError:
        if not name:
            raise
    _out().write(agent_instructions(name, prefix))
    return EXIT_OK


def _skill_source() -> Path:
    packaged = Path(str(resources.files("agent_ops_telegram").joinpath("skill/SKILL.md")))
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parents[2] / "skills" / "agent-ops-telegram" / "SKILL.md"


def cmd_skill(args: argparse.Namespace) -> int:
    src = _skill_source()
    if not src.exists():
        _err("SKILL.md is missing from this installation")
        return EXIT_FAIL
    text = src.read_text(encoding="utf-8")
    if args.action == "print":
        if not args.frontmatter and text.startswith("---\n"):
            text = text.split("\n---\n", 1)[1].lstrip("\n")
        _out().write(text)
        return EXIT_OK
    dest_root = Path(args.dir).expanduser() if args.dir else Path.home() / ".claude" / "skills"
    dest = dest_root / "agent-ops-telegram" / "SKILL.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    _out().write(f"Installed the agent-ops-telegram skill to {dest}\n")
    return EXIT_OK


# ================================================================ parser


def _non_negative(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aot",
        description="Run a coding agent in tmux 24/7 and talk to it from Telegram without losing messages.",
    )
    parser.add_argument("--version", action="version", version=f"aot {__version__}")
    parser.add_argument(
        "--config", help="config file (default: $AOT_CONFIG, ~/.config/agent-ops-telegram/config.toml, /etc/agent-ops-telegram/config.toml)"
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p = sub.add_parser("init", help="set up a bot: config, secrets file, systemd units")
    p.add_argument("--name", help="bot id (lowercase), also the default tmux session name")
    tok = p.add_mutually_exclusive_group()
    tok.add_argument("--token-stdin", action="store_true", help="read the bot token from the first line of stdin")
    tok.add_argument("--token-env", metavar="VAR", help="read the bot token from this environment variable")
    p.add_argument("--chat", type=int, action="append", help="allowed chat id (repeatable); skips auto-detection")
    p.add_argument(
        "--allowed-user",
        type=int,
        action="append",
        metavar="USER_ID",
        help="only accept messages from this user id (repeatable; use it for groups)",
    )
    p.add_argument("--tmux", help="tmux target (session or session:window.pane)")
    p.add_argument("--start-command", help="command that starts the agent when the session is missing ('' for never)")
    p.add_argument("--workdir", help="working directory for --start-command")
    p.add_argument("--systemd", choices=["system", "user", "none"], help="where the services run (default: system as root, else user)")
    p.add_argument("--units-dir", help="write unit files here instead of the systemd directory")
    p.add_argument("--no-units", action="store_true", help="do not write systemd unit files")
    p.add_argument("--env-file", help="secrets file (default: next to the config, secrets.env)")
    p.add_argument("--state-dir", help="where queue, offsets, inbox and logs live")
    p.add_argument("--api-base", default="https://api.telegram.org", help=argparse.SUPPRESS)
    p.add_argument("--offline", action="store_true", help="do not contact Telegram (needs --chat)")
    p.add_argument("--yes", "-y", action="store_true", help="non-interactive: use flags and defaults, ask nothing")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("relay", help="run the relay for one bot (what the systemd service runs)")
    p.add_argument("--bot", required=True)
    p.set_defaults(func=cmd_relay)

    p = sub.add_parser("send", help="send a message, photos or files to Telegram")
    p.add_argument("text", nargs="*", help="message text; '-' (or no text with piped stdin) reads stdin")
    p.add_argument("--bot", help="bot name (default: $AOT_BOT, or the only bot)")
    p.add_argument("--chat", type=int, help="chat id (default: the bot's default_chat); must be allow-listed")
    p.add_argument("--photo", action="append", metavar="PATH", help="send a photo (repeat for an album); text becomes the caption")
    p.add_argument("--document", action="append", metavar="PATH", help="send a file (repeat for an album)")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--markdown", action="store_true", help="parse as MarkdownV2 (falls back to plain text if it does not parse)")
    fmt.add_argument("--html", action="store_true", help="parse as HTML (falls back to plain text if it does not parse)")
    p.add_argument("--reply-to", type=int, metavar="MSG_ID", help="reply to this message id")
    p.add_argument("--retries", type=_non_negative, default=4, help="retries on network errors, 5xx and 429 (default 4)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("doctor", help="check token, webhook, single poller, tmux, heartbeat, queue, disk")
    p.add_argument("--bot", help="only this bot (default: all)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--offline", action="store_true", help="skip the Telegram API checks")
    p.add_argument("--fix", action="store_true", help="delete a webhook that blocks polling")
    p.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", help="one line per bot: relay, service, heartbeat, queue, tmux")
    p.add_argument("--bot")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("logs", help="show the relay log")
    p.add_argument("--bot")
    p.add_argument("-n", "--lines", type=int, default=50)
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--journal", action="store_true", help="read journald instead of the log file")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("watchdog", help="repair stuck or duplicate relays and missing sessions (run from a timer)")
    p.add_argument("--bot")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_watchdog)

    p = sub.add_parser("units", help="print or write the systemd unit files")
    p.add_argument("--write", action="store_true", help="write the files (default: print them)")
    p.add_argument("--dir", help="target directory (default: the systemd directory for the scope)")
    p.add_argument(
        "--scope", choices=["system", "user"], help="system units in /etc/systemd/system or user units (default: from the config)"
    )
    p.add_argument("--user", help="User= for system units: the account that owns the agent's tmux session (default: $SUDO_USER or you)")
    p.set_defaults(func=cmd_units)

    p = sub.add_parser("instructions", help="print the CLAUDE.md / AGENTS.md block that tells the agent how to reply")
    p.add_argument("--bot")
    p.set_defaults(func=cmd_instructions)

    p = sub.add_parser("skill", help="install or print the agent skill (SKILL.md)")
    p.add_argument("action", choices=["install", "print"])
    p.add_argument("--dir", help="skills folder (default: ~/.claude/skills)")
    p.add_argument("--frontmatter", action="store_true", help="print: keep the YAML header")
    p.set_defaults(func=cmd_skill)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code: int = args.func(args)
        return code
    except ConfigError as exc:
        _err(str(exc))
        return EXIT_USAGE
    except BrokenPipeError:
        return EXIT_OK
