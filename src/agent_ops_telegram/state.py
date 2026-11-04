"""Durable relay state: update offset, spool queue, health file and the single-poller lock."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import logging
import math
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from .security import create_private_temp

log = logging.getLogger("aot.state")


def as_int(value: Any, default: int = 0) -> int:
    """``value`` as an int, or ``default`` for anything that is not a plain number
    (hand-edited or damaged state files must never crash the relay)."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def as_float(value: Any, default: float = 0.0) -> float:
    """``value`` as a finite float, or ``default`` (see :func:`as_int`)."""
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + fsync + rename, so a crash never leaves a half-written file."""
    ensure_private_dir(path.parent)
    tmp, fd = create_private_temp(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and missing parents) with mode 0700.

    Only directories created here are chmod-ed (``mkdir`` gives missing parents the
    default mode). A directory that already exists was
    chosen by the user (e.g. ``inbox_dir``) and keeps its mode; ``aot doctor`` warns when
    it is group- or world-writable."""
    created: list[Path] = []
    probe = path
    while not probe.exists() and not probe.is_symlink() and probe != probe.parent:
        created.append(probe)
        probe = probe.parent
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in created:
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
    return path


@functools.lru_cache(maxsize=1)
def boot_id() -> str:
    """This boot's id, or ``""``: monotonic clock readings only compare within one boot.

    Linux exposes a random id per boot; on macOS and the BSDs the boot time from
    ``sysctl kern.boottime`` serves the same purpose.
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"boottime:{out}" if out else ""


class CorruptOffset(Exception):
    """The offset file exists but cannot be used; it has been moved aside."""


@dataclass
class QueueItem:
    update_id: int
    path: Path
    data: dict[str, Any]
    # Position in the queue (strictly increasing, see BotState.next_seq); -1 for a record
    # written by an older version, which predates every numbered one.
    seq: int = -1

    @property
    def ident(self) -> str:
        """Identifies this record (not just its file name) in the delivered list."""
        return f"{self.path.name}|{self.update_id}|{self.data.get('received')!r}"

    def save(self) -> None:
        atomic_write(self.path, json.dumps(self.data, ensure_ascii=False))


class BotState:
    """Files under ``<state_dir>/<bot>/``: ``offset``, ``queue/*.json``, ``health.json``."""

    def __init__(self, root: Path, clock: Callable[[], float] | None = None) -> None:
        self.root = root
        # Wall clock, only for the "received" stamp shown as a message's age; the queue
        # order comes from the sequence number, never from the clock.
        self.clock = clock or time.time
        # Called with the file name whenever a corrupt queue file is moved aside.
        self.on_corrupt: Callable[[str], None] | None = None
        self.queue_dir = root / "queue"
        self.offset_file = root / "offset"
        self.health_file = root / "health.json"
        self.commands_file = root / "commands.json"
        self.seq_file = root / "seq"
        self.delivered_file = root / "delivered.json"
        self._next_seq: int | None = None
        self._delivered: set[str] | None = None
        self._keys: dict[tuple[str, int, int], list[Path]] | None = None

    def ensure(self) -> None:
        ensure_private_dir(self.root)
        ensure_private_dir(self.queue_dir)

    def sweep_tmp(self) -> None:
        """Remove temp files a killed process may have left behind by ``atomic_write``."""
        for directory in (self.root, self.queue_dir):
            for tmp in directory.glob(".*.tmp"):
                with contextlib.suppress(OSError):
                    tmp.unlink()

    # A ``.done`` record only has to outlive the updates Telegram may still hold (24 hours).
    DONE_TTL = 7 * 86400.0

    def sweep_done(self, now: float, max_age: float = DONE_TTL) -> int:
        """Remove ``.done`` records (delivered, but their file could not be deleted at the
        time) older than ``max_age`` seconds. Returns how many were removed."""
        removed = 0
        if not self.queue_dir.exists():
            return 0
        for path in self.queue_dir.glob("*.done"):
            try:
                if now - path.stat().st_mtime > max_age:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            self._keys = None
        return removed

    def has_records(self) -> bool:
        """Whether the queue folder holds any record (queued, set aside or done)."""
        return bool(self._queue_files())

    # -------------------------------------------------------------- offset

    def load_offset(self) -> int | None:
        """The saved offset, or ``None`` when there is none yet (see :meth:`load_offset_record`)."""
        return self.load_offset_record()[0]

    def load_offset_record(self) -> tuple[int | None, str]:
        """``(offset, bot_id)``: the saved offset and the id of the bot it belongs to
        (``""`` when unknown, e.g. a plain number written by an older version), or
        ``(None, "")`` when there is no offset yet.

        Raises :class:`CorruptOffset` (after moving the file aside as ``offset.bad``) when
        the file exists but holds neither a number nor an offset record."""
        try:
            text = self.offset_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None, ""
        except (OSError, ValueError) as exc:
            raise self._quarantine_offset(str(exc)) from exc
        bot_id = ""
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            raw = data.get("offset") if isinstance(data, dict) else None
            if not isinstance(raw, int) or isinstance(raw, bool):
                raise self._quarantine_offset(f"not an offset record: {text[:40]!r}")
            value = raw
            owner = data.get("bot_id") if isinstance(data, dict) else None
            bot_id = str(owner) if isinstance(owner, (str, int)) and not isinstance(owner, bool) else ""
        else:
            try:
                value = int(text)
            except ValueError:
                raise self._quarantine_offset(f"not a number: {text[:40]!r}") from None
        if value < 0:
            raise self._quarantine_offset(f"negative value {value}")
        return value, bot_id

    def _quarantine_offset(self, why: str) -> CorruptOffset:
        bad = self.offset_file.with_name("offset.bad")
        try:
            self.offset_file.replace(bad)
            where = f"moved it to {bad}"
        except OSError as exc:
            where = f"could not move it aside: {exc}"
        log.warning("offset file %s is corrupt (%s); %s", self.offset_file, why, where)
        return CorruptOffset(why)

    def save_offset(self, offset: int, bot_id: str = "") -> None:
        """Save the offset together with the id of the bot it belongs to, so a token switch
        is noticed even when health.json is gone."""
        atomic_write(self.offset_file, json.dumps({"offset": offset, "bot_id": bot_id}) + "\n")

    def highest_queued(self) -> int | None:
        """The largest update id in the queue (including set-aside files), if any."""
        best: int | None = None
        for path in self._queue_files():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            update = data.get("update") if isinstance(data, dict) else None
            uid = as_int(update.get("update_id"), -1) if isinstance(update, dict) else -1
            if uid < 0 and isinstance(data, dict) and "seq" not in data:
                # Older versions named files by update id. An unreadable file is skipped: its
                # name may be a queue position, and too high an offset would skip updates.
                uid = as_int(path.name.split(".")[0], -1)
            if uid >= 0 and (best is None or uid > best):
                best = uid
        return best

    def _queue_files(self) -> list[Path]:
        """Every record in the queue folder (queued, set aside or marked done)."""
        if not self.queue_dir.exists():
            return []
        return [p for p in sorted(self.queue_dir.glob("*.*")) if not p.name.startswith(".")]

    # -------------------------------------------------------------- commands

    # Telegram keeps updates for 24 hours, so a replay can only bring back commands younger
    # than that. An older mark is ignored: after a week without updates Telegram may restart
    # update ids at a random (possibly lower) value.
    COMMAND_MARK_TTL = 86400.0

    def last_command(self, now: float) -> int:
        """Update id of the last relay command run (0 if none, or if the mark is stale)."""
        try:
            data = json.loads(self.commands_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict) or now - as_float(data.get("at")) > self.COMMAND_MARK_TTL:
            return 0
        return max(as_int(data.get("update_id")), 0)

    def mark_command(self, update_id: int, now: float) -> None:
        """Record that the command of ``update_id`` is about to run (raises OSError)."""
        atomic_write(self.commands_file, json.dumps({"update_id": update_id, "at": now}))

    def forget_commands(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.commands_file.unlink()

    # -------------------------------------------------------------- queue

    def _ensure_seq(self) -> int:
        """The next sequence number, initialised on first use from the ``seq`` file and the
        queue itself (one past the largest number any queue file uses), so numbering
        continues even if the ``seq`` file was lost."""
        if self._next_seq is None:
            try:
                stored = as_int(self.seq_file.read_text(encoding="utf-8").strip(), 0)
            except OSError:
                stored = 0
            floor = max((as_int(p.name.split(".")[0], -1) for p in self._queue_files()), default=-1) + 1
            self._next_seq = max(stored, floor, 0)
        return self._next_seq

    def next_seq(self) -> int:
        """Allocate the next queue position. Strictly increasing and persisted before it is
        used (raises OSError if it cannot be saved), so the queue order never depends on
        the wall clock, which NTP may step backwards."""
        seq = self._ensure_seq()
        atomic_write(self.seq_file, f"{seq + 1}\n")
        self._next_seq = seq + 1
        return seq

    def last_seq(self) -> int:
        """The newest queue position handed out so far (-1 if none): a command covers the
        messages up to it."""
        return self._ensure_seq() - 1

    def enqueue(self, update: dict[str, Any], bot_id: str = "") -> QueueItem | None:
        """Store ``update`` at the end of the queue (raises OSError if it cannot be stored).
        Returns ``None`` when it was delivered already (see :meth:`handled`).

        The same update fetched again after a crash is stored once. Update ids alone do not
        identify it (Telegram restarts them after a week without updates), so the key is
        the bot, the update id and the message date."""
        existing = self.find_queued(update, bot_id)
        if existing is not None:
            return existing
        if self.handled(update, bot_id):
            return None
        uid = int(update["update_id"])
        seq = self.next_seq()
        data: dict[str, Any] = {"seq": seq, "update": update, "received": self.clock()}
        if bot_id:
            data["bot_id"] = bot_id
        item = QueueItem(uid, self.queue_dir / f"{seq:015d}.json", data, seq)
        item.save()
        self._dedupe_keys()
        self._add_key(item, item.path)
        return item

    def find_queued(self, update: dict[str, Any], bot_id: str = "") -> QueueItem | None:
        """The queue record of ``update`` (same bot, update id and message date), if any.
        A record or query without a bot id matches any bot: the queue is archived whenever
        the bot changes, so what is queued belongs to the current one."""
        for path in self._matches(update, bot_id):
            if path.suffix == ".json":
                existing = self._read(path)
                if existing is not None and existing.ident not in self._delivered_idents():
                    return existing
        return None

    def handled(self, update: dict[str, Any], bot_id: str = "") -> bool:
        """Whether ``update`` was delivered but its record is still around: a ``.done``
        file, or a queue file listed in ``delivered.json``."""
        for path in self._matches(update, bot_id):
            if path.suffix == ".done":
                return True
            existing = self._read(path)
            if existing is not None and existing.ident in self._delivered_idents():
                return True
        return False

    def _matches(self, update: dict[str, Any], bot_id: str) -> list[Path]:
        uid = as_int(update.get("update_id"), -1)
        date = _update_date(update)
        return [
            path
            for (owner, other_uid, other_date), paths in self._dedupe_keys().items()
            if other_uid == uid and other_date == date and (owner == bot_id or not owner or not bot_id)
            for path in paths
            if path.exists()
        ]

    def _dedupe_keys(self) -> dict[tuple[str, int, int], list[Path]]:
        """(bot, update id, message date) -> queue and ``.done`` records with that key."""
        if self._keys is None:
            self._keys = {}
            if self.queue_dir.exists():
                for path in sorted([*self.queue_dir.glob("*.json"), *self.queue_dir.glob("*.done")]):
                    item = self._read(path)
                    if item is not None:
                        self._add_key(item, path)
        return self._keys

    def _add_key(self, item: QueueItem, path: Path) -> None:
        if self._keys is not None:
            key = (str(item.data.get("bot_id") or ""), item.update_id, _update_date(item.data["update"]))
            self._keys.setdefault(key, []).append(path)

    def _read(self, path: Path) -> QueueItem | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("update"), dict):
            return None  # valid JSON but not a queue record (e.g. "null" or a list)
        raw_seq = data.get("seq")
        seq = raw_seq if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) and raw_seq >= 0 else -1
        uid = as_int(data["update"].get("update_id"), -1)
        if uid < 0:
            if seq >= 0:
                return None
            uid = as_int(path.stem, -1)  # older versions named the file by update id
            if uid < 0:
                return None
        return QueueItem(uid, path, data, seq)

    def pending(self, *, readonly: bool = False) -> list[QueueItem]:
        """Queued messages in the order they were queued: by sequence number. Records from
        older versions (no number) come first, ordered by their "received" stamp as before.

        Unless ``readonly`` (``aot doctor`` / ``aot status``), unreadable records are moved
        aside and stale entries are pruned from ``delivered.json``."""
        if not self.queue_dir.exists():
            return []
        delivered = self._delivered_idents()
        items = []
        names: set[str] = set()
        for path in sorted(self.queue_dir.glob("*.json")):
            names.add(path.name)
            item = self._read(path)
            if item is None and readonly:
                continue
            if item is None:
                # unreadable record: move it aside instead of blocking the queue forever
                bad = path.with_suffix(".bad")
                try:
                    path.rename(bad)
                except OSError as exc:
                    log.warning("queue file %s is unreadable and could not be moved aside: %s", path, exc)
                    continue
                log.warning("queue file %s was unreadable (corrupt?); moved it to %s", path.name, bad.name)
                if self.on_corrupt is not None:
                    try:
                        self.on_corrupt(bad.name)
                    except Exception:  # best effort: never block the queue on a notification
                        log.exception("could not report the corrupt queue file")
                continue
            if item.ident in delivered:
                continue  # delivered, but its file could not be removed
            items.append(item)
        stale = {d for d in delivered if d.split("|", 1)[0] not in names}
        if stale and not readonly:
            delivered -= stale
            self._save_delivered()
        items.sort(key=lambda i: (1, float(i.seq), "") if i.seq >= 0 else (0, as_float(i.data.get("received")), i.path.name))
        return items

    def archive_queue(self, label: str) -> tuple[int, Path]:
        """Move every queue file (including set-aside ones) to ``queue.<label>/``, where it
        is kept but never delivered. Returns the number of messages moved and the folder."""
        archive = self.root / f"queue.{label}"
        moved = 0
        if not self.queue_dir.exists():
            return 0, archive
        for path in sorted(self.queue_dir.glob("*.*")):
            if path.name.startswith("."):
                continue
            ensure_private_dir(archive)
            path.replace(archive / path.name)
            moved += path.suffix == ".json"
        return moved, archive

    def queue_len(self) -> int:
        if not self.queue_dir.exists():
            return 0
        names = {p.name for p in self.queue_dir.glob("*.json")}
        done = {d.split("|", 1)[0] for d in self._delivered_idents()}
        return len(names - done)

    def oldest_queued(self) -> float | None:
        stamps = [as_float(i.data.get("received"), -1.0) for i in self.pending(readonly=True)]
        return min((t for t in stamps if t >= 0), default=None)

    def done(self, item: QueueItem) -> None:
        """Remove a handled message from the queue. Never raises: when the file cannot be
        deleted (read-only or broken file system) it is renamed to ``.done``, or else
        recorded as delivered, so it is never typed again either way."""
        try:
            item.path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            error = exc
        try:
            done = item.path.with_suffix(".done")
            item.path.rename(done)
            self._add_key(item, done)
            log.warning("could not remove queue file %s (%s); renamed it to .done", item.path.name, error)
            return
        except OSError:
            pass
        self._delivered_idents().add(item.ident)
        saved = self._save_delivered()
        log.error(
            "could not remove queue file %s (%s); %s",
            item.path.name,
            error,
            "recorded it as delivered" if saved else "it is marked delivered until the relay restarts",
        )

    def _delivered_idents(self) -> set[str]:
        if self._delivered is None:
            try:
                data = json.loads(self.delivered_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = []
            self._delivered = {str(d) for d in data} if isinstance(data, list) else set()
        return self._delivered

    def _save_delivered(self) -> bool:
        idents = self._delivered_idents()
        try:
            if idents:
                atomic_write(self.delivered_file, json.dumps(sorted(idents)))
            else:
                with contextlib.suppress(FileNotFoundError):
                    self.delivered_file.unlink()
        except OSError as exc:
            log.warning("could not save the delivered list: %s", exc)
            return False
        return True

    def set_aside(self, item: QueueItem) -> None:
        """Move a queue file that keeps failing out of the way (kept as ``.bad`` for inspection)."""
        with contextlib.suppress(OSError):
            item.path.rename(item.path.with_suffix(".bad"))

    def clear_queue(self, upto: int | None = None) -> int:
        """Delete queued messages (only those queued at or before position ``upto`` if given)."""
        n = 0
        for item in self.pending():
            if upto is not None and item.seq > upto:
                continue
            self.done(item)
            n += 1
        return n

    # -------------------------------------------------------------- health

    def read_health(self) -> dict[str, Any]:
        try:
            data = json.loads(self.health_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_health(self, **values: Any) -> None:
        data = self.read_health()
        data.update(values)
        atomic_write(self.health_file, json.dumps(data, ensure_ascii=False, indent=1))


def _update_date(update: dict[str, Any]) -> int:
    """The date of the message an update carries (0 if none)."""
    for key in ("message", "edited_message", "channel_post", "edited_channel_post", "business_message"):
        msg = update.get(key)
        if isinstance(msg, dict):
            return as_int(msg.get("date"))
    return 0


# ------------------------------------------------------------------ lock


def state_lock_path(bot_state_dir: Path) -> Path:
    """Lock on the bot's state directory: two relays for the same bot name never share
    its offset and queue, even with different tokens."""
    return bot_state_dir / "relay.lock"


def lock_path(state_dir: Path, token: str) -> Path:
    """Lock file keyed by a hash of the token: two configs for the same bot share it."""
    digest = hashlib.sha256(token.encode()).hexdigest()[:16]
    return state_dir / "locks" / f"bot-{digest}.lock"


class LockBusy(Exception):
    def __init__(self, pid: int | None, what: str = "polls this bot token") -> None:
        super().__init__(f"another relay (pid {pid or '?'}) already {what}")
        self.pid = pid


class PollerLock:
    """An exclusive ``flock`` held for the relay's lifetime. The kernel drops it when the
    process dies, so a crash never leaves a stale lock behind."""

    def __init__(self, path: Path, what: str = "polls this bot token") -> None:
        self.path = path
        self.what = what
        self._fh: TextIO | None = None

    def acquire(self, attempts: int = 10, pause: float = 0.2) -> None:
        """Take the lock. A few short retries ride out ``lock_holder`` probes from
        ``aot doctor`` / ``aot status``, which hold a shared lock for a moment."""
        ensure_private_dir(self.path.parent)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)  # lock files from older versions were created with the umask
        fh = os.fdopen(fd, "a+", encoding="utf-8")  # held open on purpose
        for attempt in range(max(attempts, 1)):
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == max(attempts, 1) - 1:
                    fh.seek(0)
                    pid = _parse_pid(fh.read())
                    fh.close()
                    raise LockBusy(pid, self.what) from None
                time.sleep(pause)
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def _parse_pid(text: str) -> int | None:
    try:
        return int(text.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def lock_holder(path: Path) -> int | None:
    """PID holding the lock, or ``None`` when nobody polls. Does not keep the lock."""
    if not path.exists():
        return None
    try:
        fh = open(path, encoding="utf-8")  # noqa: SIM115
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.seek(0)
            return _parse_pid(fh.read()) or -1
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return None
    finally:
        fh.close()
