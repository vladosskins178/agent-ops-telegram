"""The relay: long-poll Telegram, spool every allowed message to disk, then paste it into tmux.

Ordering is what makes it lossless. For each batch of updates the relay first writes each
message to ``queue/<seq>.json`` (fsynced; ``seq`` is a persisted, strictly increasing
queue position), then saves the next offset, and only then tries to deliver. A crash at
any point either re-fetches the update (Telegram has not been told it was handled) or
finds it in the queue. Delivery removes the queue file only after the text was pasted
and submitted. If the tmux session is gone, the screen shows a
confirmation dialog, or tmux fails, the message stays queued and the user is told once.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import BotConfig
from .security import redact
from .state import BotState, CorruptOffset, PollerLock, QueueItem, as_int, boot_id, ensure_private_dir, lock_path, state_lock_path
from .telegram import TelegramAPI, TelegramError
from .textutil import TELEGRAM_TEXT_LIMIT, one_line, safe_filename, safe_user, sanitize_for_terminal, split_text
from .tmux import ALLOWED_KEYS, Tmux, TmuxError

ATTACHMENT_KINDS = ("photo", "document", "audio", "voice", "video", "video_note", "animation")
# A message carrying any of these was written by someone else (forwarded, channel autoforward)
# or composed by an inline bot: it is data for the agent, never a relay command.
FORWARD_KEYS = (
    "forward_origin",
    "forward_date",
    "forward_from",
    "forward_from_chat",
    "forward_from_message_id",
    "forward_sender_name",
    "forward_signature",
    "is_automatic_forward",
)
# Everything a plain text message may carry besides its text: metadata only. A message
# with any other key (a caption, media, a poll, a contact, or a field added to the Bot API
# later) is data for the agent, never a relay command.
PLAIN_TEXT_KEYS = frozenset(
    {
        "message_id",
        "message_thread_id",
        "direct_messages_topic",
        "from",
        "sender_chat",
        "sender_boost_count",
        "sender_business_bot",
        "sender_tag",
        "date",
        "business_connection_id",
        "chat",
        "is_topic_message",
        "is_from_offline",
        "is_paid_post",
        "reply_to_message",
        "external_reply",
        "quote",
        "reply_to_story",
        "reply_to_checklist_task_id",
        "edit_date",
        "has_protected_content",
        "author_signature",
        "paid_star_count",
        "text",
        "entities",
        "link_preview_options",
        "effect_id",
        "reply_markup",
    }
)
_WS = re.compile(r"\s+")


class FatalRelayError(Exception):
    pass


class _Blocked(Exception):
    """A confirmation dialog is open; nothing was typed."""


class _RetryLater(Exception):
    """A transient failure (e.g. a download); keep the message queued and try again."""


class Relay:
    def __init__(
        self,
        bot: BotConfig,
        api: TelegramAPI,
        tmux: Tmux,
        *,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        config_path: Path | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bot = bot
        self.api = api
        self.tmux = tmux
        self.log = logger or logging.getLogger(f"aot.{bot.name}")
        self.clock = clock  # wall clock: timestamps for people and health.json
        self.monotonic = monotonic  # deadlines and durations, immune to clock steps
        self.boot_id = boot_id()
        self.sleep = sleep
        self.config_path = config_path
        self.state = BotState(bot.bot_state_dir, clock=clock)
        self.state.on_corrupt = self._corrupt_queue_file
        self.tmux.scrub_env.add(bot.token_env)
        self.offset = 0
        self.started = clock()
        self._notified: dict[str, float] = {}
        self._session_started_mono: float | None = None  # when this relay last started the session
        self._session_seen: float | None = None  # tmux creation stamp of the session last seen
        self._session_ready_at = 0.0  # monotonic time from which that session counts as booted
        self._item_errors: dict[str, int] = {}  # delivery failures, if the queue file cannot record them
        self._error_streak = 0
        self._last_beat = -1e18  # monotonic
        self.username = ""
        self._identify_tries = 0
        self._identify_at = 0.0  # monotonic
        self.bot_id = ""
        self._known_bot_id = ""  # bot id the saved state belongs to (offset record or health.json)
        self._bot_changed = False
        # The saved state names a different bot id than getMe (not just suspected from evidence).
        self._switch_confirmed = False
        # An offset exists but nothing records which bot it belongs to (older version, or
        # health.json lost): treated as a suspected switch until checked (_resolve_owner).
        self._owner_unknown = False
        self._resolve_at = 0.0  # monotonic
        self.stop_requested = False
        self.busy = False
        self._blocked_patterns = [re.compile(p, re.IGNORECASE) for p in bot.blocked_patterns]

    # ================================================================ lifecycle

    def startup(self) -> None:
        try:
            self.state.ensure()
            ensure_private_dir(self.bot.inbox)
        except OSError as exc:
            self._state_write_failed("create its state folders", exc)
        self.state.sweep_tmp()
        with contextlib.suppress(OSError):
            if removed := self.state.sweep_done(self.clock()):
                self.log.info("removed %d delivered queue record(s) older than 7 days", removed)
        health = self.state.read_health()
        ran_before = str(health.get("bot_id") or "")
        # Offset and health.json both lost, but the queue holds records: not a first start.
        queue_left = self.state.has_records()
        corrupt = False
        try:
            saved, saved_bot = self.state.load_offset_record()
        except CorruptOffset:
            saved, saved_bot, corrupt = None, "", True
        # Read before getMe: the bot id the saved offset belongs to (the offset record, else
        # health.json). ``_identify`` only records a new id once the state was reset for it.
        self._known_bot_id = saved_bot or ran_before
        self._owner_unknown = (saved is not None or corrupt) and not self._known_bot_id
        self._identify()
        switched = self._bot_changed
        if switched:
            # The old offset, queue and command mark belong to the other bot. A failed reset
            # is retried from cycle(), which does not poll until it succeeds.
            self._reset_for_new_bot()
        try:
            hook = self.api.webhook_info()
            if hook.get("url"):
                self.log.error("a webhook is set for this bot, so getUpdates cannot work; run `aot doctor --bot %s --fix`", self.bot.name)
        except TelegramError as exc:
            self._raise_if_rejected(exc)
            self.log.warning("could not read webhook info: %s", exc)
        if switched:
            pass  # offset 0: keep the new bot's backlog, it holds the owner's real messages
        elif saved is not None:
            self.offset = saved
            self.log.info("resuming at offset %d, %d message(s) queued", saved, self.state.queue_len())
        elif corrupt:
            # Not a first start: skipping the backlog now could drop messages sent while the
            # relay was down. Resume from the offset the last heartbeat recorded (or just past
            # the newest queued update); commands in a replayed range are never run twice.
            queued = self.state.highest_queued()
            candidates = [max(as_int(health.get("offset")), 0)] + ([queued + 1] if queued is not None else [])
            self.offset = max(candidates)
            self.log.warning("the offset file was corrupt; resuming at offset %d (from health.json and the queue)", self.offset)
            self._save_offset_or_report()
        elif ran_before or queue_left:
            # The offset file is gone but the relay ran before: not a first start, so keep
            # whatever Telegram still holds (offset 0 only returns unconfirmed updates; what
            # is already queued is not queued twice).
            self.log.warning("no saved offset; fetching every update Telegram still holds")
        elif self.bot.skip_backlog:
            try:
                last = self.api.get_updates(-1, 0, limit=1)
                self.offset = int(last[-1]["update_id"]) + 1 if last else 0
            except TelegramError as exc:
                self._raise_if_rejected(exc)
                self.log.warning("could not skip the backlog: %s", exc)
            self._save_offset_or_report()
            self.log.info("first start: skipped old updates, offset %d", self.offset)
        if self._owner_unknown and self.bot_id:
            self._resolve_owner()
            if self._bot_changed:
                self._reset_for_new_bot()
        values: dict[str, Any] = {
            "pid": os.getpid(),
            "started": self.started,
            "started_mono": self.monotonic(),
            "boot_id": self.boot_id,
            "offset": self.offset,
            "last_error": "",
            "fatal": "",
            "fatal_reason": "",
        }
        if self.bot_id and not self._bot_changed and not self._owner_unknown:
            values["bot_id"] = self._known_bot_id = self.bot_id
        try:
            self.state.write_health(**values)
        except OSError as exc:
            self._state_write_failed("write health.json", exc)
        self.log.info("relay for bot '%s' up; agent pane tmux:%s", self.bot.name, self.bot.tmux_target)

    def _save_offset_or_report(self) -> None:
        try:
            self.state.save_offset(self.offset, self._known_bot_id)
        except OSError as exc:
            self._state_write_failed("save the update offset", exc)

    def _state_write_failed(self, what: str, exc: OSError) -> None:
        """A state write failed at startup (disk full, read-only file system): keep running
        and deliver what is queued, but say so in the log and on Telegram."""
        self.log.error("could not %s: %s; running on, delivering what is already queued", what, exc)
        self.notify(
            "disk",
            f"The relay '{self.bot.name}' could not save its state ({what}: {exc.strerror or exc}). It keeps running and "
            f"delivers what it can; new messages may not be stored. Free disk space or fix {self.bot.bot_state_dir} on the server.",
        )

    def _raise_if_rejected(self, exc: TelegramError) -> None:
        """A rejected (401) or malformed (404) token does not fix itself: stop the relay
        (the CLI exits 78, which systemd and the watchdog do not restart)."""
        if exc.code == 401:
            raise FatalRelayError("Telegram rejected the bot token (401 Unauthorized)") from exc
        if exc.code == 404:
            raise FatalRelayError("Telegram answered 404 Not Found: the bot token is malformed (or api_base is wrong)") from exc

    def _switch_possible(self) -> bool:
        """getMe has not answered yet and the saved state may belong to another bot.

        Polling now could confirm away the new bot's pending updates with the old offset,
        so :meth:`cycle` only delivers the queue until the identity is known. A token whose
        numeric prefix (the bot id) matches the saved bot id cannot be a switch."""
        if self._owner_unknown:
            return True
        if self.bot_id or not self._known_bot_id:
            return False
        try:
            prefix = self.bot.token().split(":", 1)[0]
        except Exception:  # the token was read at startup; never fail on a re-read here
            prefix = ""
        return prefix != self._known_bot_id

    def _resolve_owner(self) -> None:
        """Nothing records which bot the saved offset belongs to. Peek at the updates
        Telegram holds for this bot (offset 0 confirms nothing). One below the saved offset
        that we would have queued but did not is evidence of a token switch (the old
        queue is archived). Otherwise nothing proves either way, so the relay continues
        from offset 0 for this bot: a new bot with lower update ids loses nothing, and what
        is already queued or delivered is recognised by (bot, update id, date) and not
        typed again; commands are guarded by the command mark."""
        try:
            held = self.api.get_updates(0, 0)
        except TelegramError as exc:
            self._raise_if_rejected(exc)
            self._resolve_at = self.monotonic() + 10.0
            self.log.warning("could not check the saved offset against this bot: %s; not polling yet", exc)
            return
        self._owner_unknown = False
        for update in held:
            uid = update.get("update_id") if isinstance(update, dict) else None
            if isinstance(uid, int) and not isinstance(uid, bool) and uid < self.offset and self._would_queue(update):
                self.log.warning("the saved offset %d does not record its bot and is past update %d of this bot", self.offset, uid)
                self._bot_changed = True
                self._switch_confirmed = False
                return
        if self.offset:
            self.log.warning("the saved offset %d does not record its bot; continuing from offset 0 for bot %s", self.offset, self.bot_id)
        self._known_bot_id = self.bot_id
        self.offset = 0
        self._save_offset_or_report()
        self._safe_health(bot_id=self.bot_id, offset=0)

    def _would_queue(self, update: Any) -> bool:
        """Whether ``update`` is a message :meth:`receive` would queue that is neither in
        the queue nor recorded as delivered (allowed chat, not a relay command or /start)."""
        msg = update.get("message") if isinstance(update, dict) else None
        if not isinstance(msg, dict):
            return False
        try:
            if not self._allowed(msg):
                return False
        except (TypeError, ValueError, AttributeError):
            return False
        text = str(msg.get("text") or msg.get("caption") or "")
        if _is_plain_text(msg) and (
            self._foreign_target(text) is not None
            or (self.bot.remote_commands and self._is_command(text))
            or text.strip().split("@")[0] == "/start"
        ):
            return False
        return self.state.find_queued(update) is None and not self.state.handled(update)

    def _reset_for_new_bot(self) -> bool:
        """The token now belongs to a different bot: start from offset 0 (the new bot's
        backlog is the owner's real messages), drop the old command mark and archive the
        old bot's queue, whose update ids mean nothing for the new bot. The new bot id is
        recorded last, so a crash in between detects the switch again on the next start."""
        old = self._known_bot_id
        confirmed = self._switch_confirmed
        try:
            moved, archive = 0, self.state.root
            if confirmed:  # a suspected switch keeps the mark and the queue: it may be the same bot
                self.state.forget_commands()
                moved, archive = self.state.archive_queue("bot" + (re.sub(r"[^0-9A-Za-z_-]", "_", old)[:40] or "unknown"))
            self.state.save_offset(0, self.bot_id)
        except OSError as exc:
            self.log.error("could not reset the state for the new bot: %s; not polling until it works", exc)
            return False
        self.offset = 0
        self._bot_changed = self._switch_confirmed = False
        archived = f" {moved} queued message(s) for the old bot were not delivered; they are kept in {archive}." if moved else ""
        if confirmed:
            self.log.warning("this bot name now uses a different bot (id %s, was %s); starting from offset 0", self.bot_id, old)
            self.notify(
                "new-bot",
                f"Note: the relay '{self.bot.name}' now runs with a different bot token (bot id {self.bot_id}, was {old or 'not recorded'}). "
                f"It started from update offset 0, so messages already sent to the new bot are delivered.{archived}",
            )
        else:
            self.log.warning("the saved offset did not record its bot and Telegram holds older messages; starting from offset 0")
            self.notify(
                "new-bot",
                f"Note: the relay '{self.bot.name}' could not tell which bot its saved state belonged to (bot id {self.bot_id} now), "
                "and Telegram still held messages it had not queued, so it may run with a different bot token. "
                "It started from update offset 0 so no message is lost; a message delivered just before the restart may be typed again.",
            )
        self._known_bot_id = self.bot_id
        self._safe_health(bot_id=self.bot_id, offset=0)
        return True

    def _identify(self) -> dict[str, Any] | None:
        """Ask Telegram who we are (``getMe``). Until the username is known, commands
        addressed as ``/aot@name`` are never run, so failures are retried with a backoff
        from :meth:`cycle`. A rejected token (401/404) is fatal.

        When the bot id differs from the one the saved state belongs to, ``_bot_changed``
        is set and the new id is not written to health.json yet: the caller resets the
        offset first, so a crash in between cannot hide the switch on the next start."""
        self._identify_tries += 1
        try:
            me = self.api.get_me()
        except TelegramError as exc:
            self._raise_if_rejected(exc)
            delay = min(5.0 * 2 ** (self._identify_tries - 1), 300.0)
            self._identify_at = self.monotonic() + delay
            self.log.warning("could not read the bot identity: %s; retrying in %d s", exc, delay)
            return None
        self.username = str(me.get("username") or "")
        bot_id = str(me.get("id") or "")
        if bot_id:
            self.bot_id = bot_id
            if self._owner_unknown:
                pass  # _resolve_owner checks the saved offset against this bot
            elif self._known_bot_id and self._known_bot_id != bot_id:
                self._bot_changed = self._switch_confirmed = True
            else:
                self._known_bot_id = bot_id
                self._safe_health(bot_id=bot_id)
        if self.username:
            self._identify_tries = 0
            self.log.info("bot identity: @%s", self.username)
        else:
            self._identify_at = self.monotonic() + 300.0
        return me

    def run(self, stop: Callable[[], bool] = lambda: False) -> None:
        lock = PollerLock(lock_path(self.bot.state_dir, self.api_token_for_lock()))
        # Also lock the state directory: two relays for the same bot name with different
        # tokens would otherwise share (and both deliver) one queue.
        state_lock = PollerLock(state_lock_path(self.bot.bot_state_dir), "runs for this bot's state directory")
        lock.acquire()
        try:
            state_lock.acquire()
            try:
                self.startup()
                while not stop() and not self.stop_requested:
                    self.cycle()
            finally:
                state_lock.release()
        finally:
            lock.release()

    def api_token_for_lock(self) -> str:
        return self.bot.token()

    # ================================================================ one cycle

    def cycle(self) -> None:
        if not self.username and self.monotonic() >= self._identify_at:
            self._identify()
        if self._owner_unknown and self.bot_id and self.monotonic() >= self._resolve_at:
            self._resolve_owner()
        if self._bot_changed:
            self._reset_for_new_bot()
        if self._bot_changed or self._switch_possible():
            # Identity unknown (or the reset failed): never poll with an offset that may
            # belong to another bot. The queue (unless it is the old bot's, or its owner is
            # still being checked) still goes out: delivering before the check would make
            # the held copies of those messages look like another bot's.
            if not self._bot_changed and not self._owner_unknown:
                self.deliver_pending()
            self._heartbeat(poll_ok=False)
            if self._bot_changed:
                wait = 10.0
            else:
                wait = min((self._resolve_at if self.bot_id else self._identify_at) - self.monotonic(), float(self.bot.poll_timeout))
            self.sleep(max(wait, 1.0))
            return
        queued = self.state.queue_len()
        timeout = 2 if queued else self.bot.poll_timeout
        try:
            updates = self.api.get_updates(self.offset, timeout)
        except TelegramError as exc:
            self._poll_failed(exc)
            self.deliver_pending()
            self._heartbeat(poll_ok=False)
            return
        self._error_streak = 0
        # (chat, text, update id, queue position of the last message queued before the command)
        commands: list[tuple[int, str, int, int]] = []
        offset_saved = True
        if updates:
            spool_failed = False
            for update in updates:
                if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                    self.log.error("skipping a malformed update from the API")
                    continue
                try:
                    command = self.receive(update)
                    if command is not None:
                        commands.append((command[0], command[1], int(update["update_id"]), self.state.last_seq()))
                except OSError as exc:
                    # Disk full or I/O error: the message is not safely on disk, so do NOT
                    # move the offset past it. Telegram keeps it and we try again later.
                    self.log.error("could not queue update %s (%s); will fetch it again", update.get("update_id"), exc)
                    self._safe_health(last_error=f"queue write failed: {exc.strerror or exc}", last_error_at=self.clock())
                    self.notify(
                        "disk",
                        f"The relay '{self.bot.name}' could not store a new message ({exc.strerror or exc}). "
                        "Telegram keeps it and the relay retries; free disk space on the server.",
                    )
                    spool_failed = True
                    break
                except (ValueError, KeyError, TypeError, AttributeError):  # malformed update: skip it, never wedge
                    self.log.exception("could not handle update %s; skipped it", update.get("update_id"))
                self.offset = max(self.offset, int(update["update_id"]) + 1)
            try:
                self.state.save_offset(self.offset, self._known_bot_id)
            except OSError as exc:
                offset_saved = False
                self.log.error("could not save the offset: %s; commands in this batch are not run", exc)
                for chat_id, _text, _uid, _earlier in commands:
                    self._reply(
                        chat_id, "The server could not save its state (disk full?), so your command was not run. Send it again later."
                    )
            if spool_failed:
                self.sleep(10)
        # Commands run after the offset is saved, so a crash never repeats `/aot keys C-c`;
        # the command mark covers a replay after the offset file was lost. Each command runs
        # in the order it was sent: the messages queued before it are typed first, except
        # for flush and drop, which act on those messages themselves.
        done_upto = self.state.last_command(self.clock()) if commands and offset_saved else 0
        for chat_id, text, update_id, upto in commands if offset_saved else []:
            if update_id <= done_upto:
                self.log.warning("update %d is a command that already ran; not running it again", update_id)
                continue
            try:
                self.state.mark_command(update_id, self.clock())
            except OSError as exc:
                self.log.error("could not record command %d: %s; not running it", update_id, exc)
                self._reply(chat_id, "The server could not save its state (disk full?), so your command was not run. Send it again later.")
                continue
            if _subcommand(text) not in {"flush", "drop"}:
                self.deliver_pending(upto=upto)
            try:
                self.handle_command(chat_id, text, upto)
            except Exception:
                self.log.exception("command failed")
                self._reply(chat_id, "That command failed; see `aot logs` on the server.")
        self.deliver_pending()
        self._heartbeat(poll_ok=True)

    def _safe_health(self, **values: Any) -> None:
        try:
            self.state.write_health(**values)
        except OSError as exc:
            self.log.warning("could not write health.json: %s", exc)

    def _beat(self) -> None:
        """Heartbeat during long work (downloads, several deliveries), at most every 10 s."""
        mono = self.monotonic()
        if mono - self._last_beat >= 10:
            self._last_beat = mono
            self._safe_health(heartbeat=self.clock(), heartbeat_mono=mono, boot_id=self.boot_id)

    def _heartbeat(self, *, poll_ok: bool) -> None:
        self._last_beat = mono = self.monotonic()
        values: dict[str, Any] = {
            "pid": os.getpid(),
            "offset": self.offset,
            "queue": self.state.queue_len(),
            "heartbeat": self.clock(),
            "heartbeat_mono": mono,
            "boot_id": self.boot_id,
        }
        if poll_ok:
            values["last_poll"] = self.clock()
        self._safe_health(**values)

    def _poll_failed(self, exc: TelegramError) -> None:
        self._error_streak += 1
        now = self.clock()
        if exc.conflict:
            self.log.error("409 Conflict: another process polls this bot token or a webhook is set. Messages get split between pollers.")
            self._safe_health(last_conflict=now, last_error=exc.description)
            self.notify(
                "conflict",
                "Warning: another program is reading messages for this bot (Telegram returned 409 Conflict). "
                "Messages may go missing until only one relay polls this token. Run `aot doctor` on the server.",
            )
            self.sleep(min(5 * self._error_streak, 30))
            return
        self._safe_health(last_error=exc.description, last_error_at=now)
        self._raise_if_rejected(exc)
        self.log.warning("getUpdates failed (%s); retrying", exc)
        self.sleep(min(2**self._error_streak, 30) if exc.code != 429 else max(exc.retry_after, 1))

    # ================================================================ receive

    def _allowed(self, msg: dict[str, Any]) -> bool:
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        user_id = int((msg.get("from") or {}).get("id") or 0)
        if chat_id not in self.bot.allowed_chats:
            return False
        return not (self.bot.allowed_users and user_id not in self.bot.allowed_users)

    def receive(self, update: dict[str, Any]) -> tuple[int, str] | None:
        """Spool an allowed message. Returns ``(chat_id, text)`` for a relay command,
        which the caller runs after saving the offset."""
        msg = update.get("message")
        if not isinstance(msg, dict):
            return None
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        if not self._allowed(msg):
            user_id = (msg.get("from") or {}).get("id")
            self.log.warning("ignored update %s from chat %s user %s (not in the allow-list)", update.get("update_id"), chat_id, user_id)
            return None
        text = str(msg.get("text") or msg.get("caption") or "")
        plain = _is_plain_text(msg)
        target = self._foreign_target(text) if plain else None
        if target is not None:
            # "/aot@other_bot ..." in a group is for another bot; while our own username is
            # unknown we cannot tell, so it is neither run nor typed into the agent.
            self.log.info("dropped update %s: command addressed to @%s, not to this bot", update.get("update_id"), target or "?")
            if not self.username:
                self.notify(
                    "unidentified",
                    "Bot identity not confirmed yet (Telegram getMe is failing), so commands addressed "
                    f"as {self.bot.command_prefix}@name were not run. Retry in a minute.",
                    chat_id,
                )
            return None
        if self.bot.remote_commands and plain and self._is_command(text):
            self._safe_health(last_update=self.clock())
            return chat_id, text
        if plain and text.strip().split("@")[0] == "/start":
            self._reply(chat_id, f"Connected. Messages you send here are typed into the agent session '{self.bot.session}'.")
            return None
        self.state.enqueue(update, self.bot_id)  # raises OSError if it could not be stored: the caller keeps the offset
        self._safe_health(last_update=self.clock())
        self.log.info("queued message %s (%d chars)", msg.get("message_id"), len(text))
        return None

    def _is_command(self, text: str) -> bool:
        first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
        name, _, target = first.partition("@")
        if target and (not self.username or target.lower() != self.username.lower()):
            return False  # addressed to another bot (or to us while we do not know our name)
        return name.lower() == self.bot.command_prefix.lower()

    def _foreign_target(self, text: str) -> str | None:
        """For ``/aot@name`` or ``/start@name`` not addressed to this bot (or sent while
        the username is unknown), the ``name``; otherwise ``None``."""
        first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
        name, at, target = first.partition("@")
        if not at or name.lower() not in {self.bot.command_prefix.lower(), "/start"}:
            return None
        if self.username and target.lower() == self.username.lower():
            return None
        return target

    # ================================================================ deliver

    def deliver_pending(self, *, force: bool = False, upto: int | None = None) -> int:
        """Deliver queued messages in order. With ``upto`` (the queue position of the last
        message queued when a command arrived), stop at the first message queued after
        that command. ``force`` skips the dialog check for this run only."""
        delivered = 0
        for item in self.state.pending():
            if upto is not None and item.seq > upto:
                break  # sent after the command: keep order
            try:
                ok = self.deliver(item, force=force)
            except Exception:
                self.log.exception("delivering queued update %s failed", item.update_id)
                errors = max(as_int(item.data.get("errors")), self._item_errors.get(item.ident, 0), 0) + 1
                if errors < 3:
                    item.data["errors"] = self._item_errors[item.ident] = errors
                    self._save_item(item)
                    break
                self._item_errors.pop(item.ident, None)
                self.state.set_aside(item)
                self.notify(
                    "bad-item", f"One queued message could not be delivered after {errors} attempts and was set aside (see `aot logs`)."
                )
                continue
            if not ok:
                break  # keep order: never deliver message N+1 before message N
            self._item_errors.pop(item.ident, None)
            delivered += 1
            self._beat()
        return delivered

    def _save_item(self, item: QueueItem) -> None:
        """Save progress notes on a queue record (download attempts, the built prompt, error
        counts); on a full or read-only disk they are only kept in memory."""
        try:
            item.save()
        except OSError as exc:
            self.log.warning("could not update queue file %s: %s", item.path.name, exc)

    def deliver(self, item: QueueItem, *, force: bool = False) -> bool:
        if self.stop_requested:
            return False
        msg = item.data["update"].get("message") or {}
        if not isinstance(msg, dict) or not self._allowed(msg):
            # The allow-list may have changed since the message was queued (config edit +
            # restart): what is no longer allowed is dropped, never typed.
            self.log.warning(
                "dropped queued update %s from chat %s user %s: no longer in the allow-list",
                item.update_id,
                (msg.get("chat") or {}).get("id") if isinstance(msg, dict) else "?",
                (msg.get("from") or {}).get("id") if isinstance(msg, dict) else "?",
            )
            self.state.done(item)
            return True
        chat_id = int((msg.get("chat") or {}).get("id") or self.bot.alert_chat)
        if not self.tmux.has_session():
            self._session_missing(chat_id)
            return False
        if self._booting():
            return False  # the agent CLI is still booting
        if "prompt" not in item.data:
            attempts = max(as_int(item.data.get("download_attempts")), 0)
            try:
                prompt = self.build_prompt(msg, retry_downloads=attempts < 3)
            except _RetryLater as exc:
                item.data["download_attempts"] = attempts + 1
                self._save_item(item)
                self.log.warning("attachment download failed (%s); will retry", exc)
                return False
            if prompt is None:
                self.state.done(item)
                return True
            item.data["prompt"] = prompt
            self._save_item(item)
        # Check for a dialog right before typing (downloads above can take a while).
        if not force and self._report_blocked(chat_id):
            return False
        self.busy = True
        try:
            self.inject(str(item.data["prompt"]), force=force)
            self.state.done(item)  # still inside "busy", so SIGTERM cannot land in between
        except _Blocked:
            self._report_blocked(chat_id)
            return False
        except TmuxError as exc:
            if not exc.partial:
                self.log.error("could not paste into tmux:%s: %s", self.bot.tmux_target, exc)
                self.notify(
                    "tmux-error", f"Could not type your message into the agent session ({exc}). It is queued; I will retry.", chat_id
                )
                return False
            # Part of the text may already be in the agent's input: retrying would type it twice.
            self.log.error("tmux failed halfway through message %s: %s; not retrying", msg.get("message_id"), exc)
            self.state.done(item)
            self._reply(
                chat_id,
                f"Your message may have been typed only partly (tmux error: {exc}). Check with `{self.bot.command_prefix} screen` and resend if needed.",
            )
            return True
        finally:
            self.busy = False
        self._notified.pop("blocked", None)
        self.log.info("delivered message %s to tmux:%s", msg.get("message_id"), self.bot.tmux_target)
        if self.bot.ack_reaction and msg.get("message_id"):
            try:
                self.api.set_reaction(chat_id, int(msg["message_id"]), self.bot.ack_reaction)
            except TelegramError as exc:
                self.log.debug("reaction failed: %s", exc)
        if self.stop_requested:  # SIGTERM arrived while typing; stop now that the item is done
            raise SystemExit(0)
        return True

    def _booting(self) -> bool:
        """Whether the agent session is younger than ``start_grace``, measured on the
        monotonic clock. tmux reports the creation time as wall-clock time, so its age is
        read once per session (clamped to 0..start_grace); a later clock step cannot
        shorten or stretch the grace."""
        grace = float(self.bot.start_grace)
        mono = self.monotonic()
        created = self.tmux.session_created() or 0.0
        if created != self._session_seen:
            self._session_seen = created
            age = min(max(self.clock() - created, 0.0), grace) if created else grace
            self._session_ready_at = mono + grace - age
        ready = self._session_ready_at
        if self._session_started_mono is not None:
            ready = max(ready, self._session_started_mono + grace)
        return mono < ready

    def _corrupt_queue_file(self, name: str) -> None:
        self.notify(
            "corrupt-" + name,
            f"A queued message could not be read (corrupt file) and was set aside as {self.state.queue_dir / name}. "
            "It was not delivered; check it on the server.",
        )

    def _report_blocked(self, chat_id: int) -> bool:
        screen = self.blocked_screen()
        if screen is None:
            return False
        self.notify(
            "blocked",
            "The agent is waiting on a prompt in the terminal, so your message is queued "
            f"(pasting now would answer that prompt). Last lines:\n\n{screen}\n\n"
            f"Answer it with `{self.bot.command_prefix} keys 1` (or Enter, Escape, y, n...), "
            f"or send `{self.bot.command_prefix} flush` to paste anyway.",
            chat_id,
        )
        return True

    def _session_missing(self, chat_id: int) -> None:
        now = self.monotonic()
        if not self.bot.start_command:
            self.notify(
                "no-session",
                f"The agent session '{self.bot.session}' is not running. Your message is queued and will be "
                f"delivered once it exists (on the server: tmux new -s {self.bot.session}).",
                chat_id,
            )
            return
        if self._session_started_mono is not None and now - self._session_started_mono < max(self.bot.start_grace * 3, 60):
            return
        env = {"AOT_BOT": self.bot.name}
        if self.config_path:
            env["AOT_CONFIG"] = str(self.config_path)
        try:
            self.tmux.new_session(self.bot.start_command, workdir=self.bot.workdir, env=env)
            self._session_started_mono = now
            self.log.warning("tmux session '%s' was missing; started it", self.bot.session)
            self.notify(
                "started-session",
                f"The agent session '{self.bot.session}' was not running, so I started it. "
                "Your message is queued and will be typed in once the agent is up.",
                chat_id,
            )
        except TmuxError as exc:
            self._session_started_mono = now
            self.log.error("could not start tmux session '%s': %s", self.bot.session, exc)
            self.notify("start-failed", f"The agent session '{self.bot.session}' is not running and starting it failed: {exc}", chat_id)

    def blocked_screen(self, typed: str = "") -> str | None:
        """Return the pane tail when it shows a confirmation dialog, else ``None``.

        While typing a message, lines that are just that message (``typed``) are ignored,
        so a message that *mentions* "(y/n)" is not mistaken for a dialog. Earlier
        messages are deliberately not ignored: a real dialog can quote them."""
        if not self._blocked_patterns:
            return None
        try:
            rows = self.tmux.tail(self.bot.blocked_scan_lines)
        except TmuxError:
            return None
        ours = [_norm(typed)] if typed else []
        screen = [r for r in rows if not _is_our_text(r, ours)]
        tail = "\n".join(screen)
        if any(p.search(tail) for p in self._blocked_patterns):
            return redact("\n".join(rows))[-1500:]
        return None

    # ================================================================ prompt

    def build_prompt(self, msg: dict[str, Any], *, retry_downloads: bool = False) -> str | None:
        """The text to type for ``msg``, or ``None`` if it is not forwarded. With
        ``retry_downloads``, a transient download error raises ``_RetryLater`` instead of
        becoming a "[photo not downloaded]" note."""
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        msg_id = int(msg.get("message_id") or 0)
        sender = msg.get("from") or {}
        user = safe_user(str(sender.get("username") or sender.get("first_name") or "user"))
        text = sanitize_for_terminal(str(msg.get("text") or msg.get("caption") or "")).strip()
        notes: list[str] = []
        files: list[str] = []
        for kind in ATTACHMENT_KINDS:
            obj = msg.get(kind)
            if not obj:
                continue
            if kind == "photo" and isinstance(obj, list):
                obj = obj[-1]  # largest size
            if isinstance(obj, dict) and obj.get("file_id"):
                path, note = self._download(kind, obj, msg_id, retry=retry_downloads, chat_id=chat_id)
                if path:
                    files.append(path)
                if note:
                    notes.append(note)
        if msg.get("sticker"):
            notes.append(sanitize_for_terminal(f"[sticker {msg['sticker'].get('emoji', '')}]".replace(" ]", "]")))
        if not text and not files and not notes:
            if (msg.get("chat") or {}).get("type") == "private":  # stay quiet about joins, pins... in groups
                self._reply(chat_id, "This kind of message is not forwarded to the agent; send text, photos or files.")
            return None
        marker = "[forwarded]" if _is_forwarded(msg) else ("[via inline bot]" if msg.get("via_bot") else "")
        if marker:
            # Every line is marked, so forwarded text cannot pass for a prompt header of its own.
            text = marker + "\n" + "\n".join("| " + line for line in text.split("\n")) if text else marker
        if notes:
            text = (text + " " + " ".join(notes)).strip()
        if not text:
            text = "(no caption)"
        if len(text) > self.bot.long_message_chars:
            long_path = self.bot.inbox / f"{_file_stem(chat_id, msg_id)}-message.txt"
            fd = os.open(long_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            text = f"(long message, {len(text)} characters, saved to {long_path}; read the file) {one_line(text, 300)}"
        reply = ""
        reply_to = msg.get("reply_to_message")
        if isinstance(reply_to, dict) and reply_to.get("message_id"):
            quoted = (
                one_line(str(reply_to.get("text") or reply_to.get("caption") or ""), 160)
                .replace("[", "(")
                .replace("]", ")")
                .replace('"', "'")
            )
            reply = f" reply_to={reply_to['message_id']}" + (f' "{quoted}"' if quoted else "")
        file_part = "".join(f" file={f}" for f in files)
        return self.bot.prompt_template.format(
            bot=self.bot.name, user=user, chat_id=chat_id, msg_id=msg_id, files=file_part, reply=reply, text=text
        )

    def _download(
        self, kind: str, obj: dict[str, Any], msg_id: int, *, retry: bool = False, chat_id: int = 0
    ) -> tuple[str | None, str | None]:
        name = str(obj.get("file_name") or "")
        if not name:
            ext = {"photo": ".jpg", "voice": ".ogg", "video_note": ".mp4", "animation": ".mp4", "video": ".mp4", "audio": ".mp3"}.get(
                kind, ".bin"
            )
            name = f"{kind}{ext}"
        dest = self.bot.inbox / f"{_file_stem(chat_id, msg_id)}-{safe_filename(name, kind)}"
        try:
            self.api.download(str(obj["file_id"]), dest, self.bot.max_download_mb * 1024 * 1024, progress=self._beat)
        except TelegramError as exc:
            if retry and exc.retryable:
                raise _RetryLater(exc.description) from exc
            self.log.warning("could not download %s from message %d: %s", kind, msg_id, exc)
            return None, sanitize_for_terminal(f"[{kind} not downloaded: {exc.description}]")
        except OSError as exc:
            self.log.warning("could not save %s from message %d: %s", kind, msg_id, exc)
            return None, f"[{kind} not saved: {exc.strerror or exc}]"
        self.log.info("saved %s from message %d to %s", kind, msg_id, dest)
        note = f"[{kind}]" if kind in {"voice", "audio", "video", "video_note"} else None
        return str(dest), note

    # ================================================================ inject

    def inject(self, prompt: str, *, force: bool = False) -> None:
        """Paste ``prompt`` and press Enter. If Enter was swallowed (the end of the text is
        still at the bottom of the pane and the cursor has not moved), press it again.

        Raises ``_Blocked`` (nothing typed) when a dialog is open, and :class:`TmuxError`
        with ``partial=False`` when nothing was typed (safe to retry) or ``partial=True``
        when some text may already be in the input."""
        if self.tmux.in_mode():
            # Copy mode (someone scrolled the pane) would swallow every Enter.
            self.tmux.exit_mode()
            if self.tmux.in_mode():
                raise TmuxError("the pane is in copy mode and did not leave it")
        if not force and self.blocked_screen(prompt) is not None:
            raise _Blocked()
        self.tmux.paste(prompt, chunk=self.bot.paste_chunk, bracketed=self.bot.bracketed_paste)
        try:
            self.sleep(self.bot.submit_delay)
            if not force and self.blocked_screen(prompt) is not None:
                raise TmuxError("a confirmation prompt appeared while typing; Enter was not pressed", partial=True)
            before = self.tmux.cursor() if self.bot.submit_retries else None
            self.tmux.press_enter()
        except TmuxError as exc:
            exc.partial = True
            raise
        probe = _probe(prompt)
        try:
            for attempt in range(self.bot.submit_retries):
                self.sleep(0.8 + 0.7 * attempt)
                if not _still_in_input(self.tmux.tail(self.bot.submit_check_lines), probe):
                    return
                now = self.tmux.cursor()
                if before is not None and now is not None and now != before:
                    return  # the program reacted to Enter; the text on screen is its echo
                if self.blocked_screen(prompt) is not None:
                    return  # a dialog opened: an extra Enter would answer it
                if self.tmux.in_mode():
                    self.tmux.exit_mode()
                self.log.info("text still in the input line; pressing Enter again (%d)", attempt + 1)
                self.tmux.press_enter()
        except TmuxError as exc:
            self.log.warning("could not check that the message was submitted: %s", exc)

    # ================================================================ commands

    def handle_command(self, chat_id: int, text: str, scope: int | None = None) -> None:
        """Run a relay command. ``scope`` is the queue position of the last message queued
        when the command arrived: flush and drop only touch messages sent before it."""
        parts = text.split()
        sub = parts[1].lower() if len(parts) > 1 else "help"
        args = parts[2:]
        p = self.bot.command_prefix
        self.log.info("command %s %s from chat %d", p, sub, chat_id)
        try:
            if sub in {"ping", "status"}:
                session = "running" if self.tmux.has_session() else "NOT running"
                blocked = " It is waiting on a prompt." if self.tmux.has_session() and self.blocked_screen() else ""
                up = _human_age(self.clock() - self.started)
                self._reply(chat_id, f"Relay up {up}. Queue: {self.state.queue_len()}. Session '{self.bot.session}': {session}.{blocked}")
            elif sub == "screen":
                n = min(max(int(args[0]) if args and re.fullmatch(r"[0-9]{1,3}", args[0]) else 30, 1), 80)
                rows = self.tmux.tail(n) if self.tmux.has_session() else ["(session not running)"]
                self._reply(chat_id, redact("\n".join(rows)) or "(empty screen)")
            elif sub == "keys":
                if not args:
                    self._reply(chat_id, f"Usage: {p} keys KEY [KEY...]. Allowed: {', '.join(sorted(ALLOWED_KEYS))}")
                    return
                self.tmux.send_keys(args)
                self._reply(chat_id, f"Sent: {' '.join(args)}")
            elif sub == "flush":
                n = self.deliver_pending(force=True, upto=scope)
                self._reply(chat_id, f"Delivered {n} queued message(s); {self.state.queue_len()} left.")
            elif sub == "queue":
                items = self.state.pending()
                lines = [f"{len(items)} queued message(s)."]
                for item in items[:10]:
                    m = item.data["update"].get("message") or {}
                    lines.append(f"- {m.get('message_id')}: {one_line(str(m.get('text') or m.get('caption') or '(attachment)'), 60)}")
                self._reply(chat_id, "\n".join(lines))
            elif sub == "drop":
                self._reply(chat_id, f"Dropped {self.state.clear_queue(scope)} queued message(s).")
            elif sub == "start":
                if self.tmux.has_session():
                    self._reply(chat_id, f"Session '{self.bot.session}' is already running.")
                elif not self.bot.start_command:
                    self._reply(chat_id, "No start_command is configured for this bot.")
                else:
                    self._session_started_mono = None
                    self._session_missing(chat_id)
            else:
                self._reply(
                    chat_id,
                    f"{p} status - relay, queue and session state\n"
                    f"{p} screen [N] - last N lines of the agent's terminal\n"
                    f"{p} keys KEY... - press keys (Enter, Escape, 1, 2, y, n, C-c...)\n"
                    f"{p} queue - list queued messages\n"
                    f"{p} flush - paste queued messages even if a prompt is open\n"
                    f"{p} drop - delete queued messages\n"
                    f"{p} start - start the agent session (needs start_command)",
                )
        except TmuxError as exc:
            self._reply(chat_id, f"tmux error: {exc}")

    # ================================================================ outbound

    def _reply(self, chat_id: int, text: str) -> None:
        for chunk in split_text(text, TELEGRAM_TEXT_LIMIT) or ["(empty)"]:
            try:
                self.api.send_message(chat_id, chunk, retries=1)
            except TelegramError as exc:
                self.log.warning("could not send a reply: %s", exc)
                return

    def notify(self, key: str, text: str, chat_id: int | None = None) -> None:
        """Tell the user something, at most once per ``notify_cooldown`` seconds per key
        (measured on the monotonic clock)."""
        now = self.monotonic()
        if now - self._notified.get(key, -1e18) < self.bot.notify_cooldown:
            return
        self._notified[key] = now
        target = chat_id or self.bot.alert_chat
        if target:
            self._reply(target, text)


def _file_stem(chat_id: int, msg_id: int) -> str:
    """Unique per chat and message, plus a random part: files in the inbox are never overwritten."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{chat_id}-{msg_id}-{secrets.token_hex(3)}"


def _subcommand(text: str) -> str:
    parts = text.split()
    return parts[1].lower() if len(parts) > 1 else "help"


def _is_plain_text(msg: dict[str, Any]) -> bool:
    """A message that is nothing but text typed by the sender (see PLAIN_TEXT_KEYS)."""
    return isinstance(msg.get("text"), str) and all(k in PLAIN_TEXT_KEYS for k in msg)


def _is_forwarded(msg: dict[str, Any]) -> bool:
    return any(msg.get(k) for k in FORWARD_KEYS)


def _norm(text: str) -> str:
    return _WS.sub("", text)


def _is_our_text(row: str, ours: list[str]) -> bool:
    """Whether a pane row is (a wrapped piece of) text we typed, maybe behind a prompt or box border."""
    core = _norm(row).strip("│|>❯›»*-─╭╮╰╯")
    return len(core) >= 4 and any(core in o for o in ours)


def _probe(prompt: str) -> str:
    tail = _WS.sub("", prompt)[-24:]
    return tail


def _still_in_input(rows: list[str], probe: str) -> bool:
    joined = _WS.sub("", "".join(rows))
    if "[Pastedtext#" in joined:
        return True
    return bool(probe) and probe in joined


def _human_age(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def install_signal_handlers(relay: Relay | None = None) -> None:
    """SIGTERM stops the relay at once, except while it is typing a message: then it
    finishes that message first, so a restart never leaves half a prompt behind."""

    def _stop(signum: int, _frame: object) -> None:
        if relay is not None:
            relay.stop_requested = True
            if relay.busy:
                return
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
