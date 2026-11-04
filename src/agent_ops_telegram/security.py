"""Secret handling: token redaction, private-file checks and env-file parsing."""

from __future__ import annotations

import logging
import os
import re
import secrets
import stat
from pathlib import Path

# Telegram bot tokens look like "123456789:AAH...". Match them anywhere, including
# inside URLs such as https://api.telegram.org/bot<token>/getMe.
TOKEN_RE = re.compile(r"(?<![0-9])\d{5,16}:[A-Za-z0-9_-]{30,}")

_known_secrets: set[str] = set()


def register_secret(value: str) -> None:
    """Remember a secret so :func:`redact` masks it even if it does not look like a token."""
    if value and len(value) >= 8:
        _known_secrets.add(value)


def redact(text: str) -> str:
    """Mask bot tokens and registered secrets in ``text``."""
    for secret in sorted(_known_secrets, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, "<redacted>")
    return TOKEN_RE.sub("<redacted-token>", text)


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the fully formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        clean = redact(message)
        if clean != message or record.args:
            record.msg = clean
            record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            # Keep the whole traceback (it is what makes a bug report useful); only the
            # secrets inside it are masked.
            record.exc_text = redact(_FORMATTER.formatException(record.exc_info))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


_FORMATTER = logging.Formatter()


class InsecureFileError(Exception):
    """A config or secrets file has permissions that let other users read or change it."""


def check_private_file(path: Path, *, allow_group_read: bool = False, any_owner: bool = False) -> None:
    """Refuse files that other users could read or modify.

    Config files hold the allow-list (who may send prompts to your agent), env files hold
    the bot token. Both must be owned by the current user (or root) and must not be
    readable or writable by "other". Env files must not be group-accessible either.
    """
    st = path.stat()
    mode = stat.S_IMODE(st.st_mode)
    uid = os.geteuid() if hasattr(os, "geteuid") else st.st_uid
    if not any_owner and st.st_uid not in (uid, 0):
        raise InsecureFileError(f"{path} is owned by uid {st.st_uid}, not by you (uid {uid}). Fix: chown $(id -u) {path}")
    forbidden = 0o007 if allow_group_read else 0o077
    if allow_group_read:
        forbidden |= 0o020  # group-writable is never fine
    if mode & forbidden:
        raise InsecureFileError(f"{path} has mode {mode:04o}; other users can read or change it. Fix: chmod 600 {path}")


_INLINE_COMMENT = re.compile(r"\s+#.*$")


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE file (``export`` prefixes, comments and quotes allowed)."""
    check_private_file(path)
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip().removesuffix("\r")
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            rest = value[end + 1 :].strip() if end > 0 else ""
            if end > 0 and (not rest or rest.startswith("#")):
                value = value[1:end]
        else:
            value = _INLINE_COMMENT.sub("", value)  # `KEY=value  # note`, as a shell reads it
        if key:
            values[key] = value
    return values


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with mode 0600 from the start (no window where it is readable).

    Refuses to replace anything but a regular file: a mistyped path such as /dev/null must
    never be swapped for a config file."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise OSError(f"{path} is not a regular file; refusing to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp, fd = create_private_temp(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def create_private_temp(path: Path) -> tuple[Path, int]:
    """Create a new temp file next to ``path`` and return it with an open write fd.

    The name is random and the file is created with ``O_EXCL | O_NOFOLLOW`` and mode 0600,
    so a file or symlink someone planted under a predictable name is never written through."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _ in range(16):
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            fd = os.open(tmp, flags, 0o600)
        except FileExistsError:
            continue
        try:
            os.fchmod(fd, 0o600)  # the umask can only remove bits; make the mode explicit anyway
        except OSError:
            os.close(fd)
            tmp.unlink()
            raise
        return tmp, fd
    raise FileExistsError(f"could not create a temp file next to {path}")
