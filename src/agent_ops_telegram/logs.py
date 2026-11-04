"""Logging for the relay (stderr for journald plus a small rotating file) and ``aot logs``."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import TextIO

from .security import RedactingFilter
from .state import ensure_private_dir

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def log_file(bot_state_dir: Path) -> Path:
    return bot_state_dir / "relay.log"


class _PrivateRotatingHandler(logging.handlers.RotatingFileHandler):
    """Rotating log whose new files are created with mode 600 regardless of the umask."""

    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


def setup_logging(name: str, bot_state_dir: Path | None, *, stream: TextIO | None = None, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(f"aot.{name}")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stderr)]
    file_error: OSError | None = None
    if bot_state_dir is not None:
        try:
            ensure_private_dir(bot_state_dir)
            path = log_file(bot_state_dir)
            if not path.exists():
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
            handlers.append(_PrivateRotatingHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
        except OSError as exc:  # read-only or full file system: stderr (journald) still works
            file_error = exc
    for handler in handlers:
        handler.setFormatter(logging.Formatter(FORMAT))
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
    if file_error is not None:
        logger.warning("could not open the log file in %s (%s); logging to stderr only", bot_state_dir, file_error)
    return logger


def tail_file(path: Path, lines: int, out: TextIO, *, follow: bool = False, poll: float = 0.5) -> None:
    """Print the last ``lines`` lines of ``path``; with ``follow``, keep printing new lines."""
    fh = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115 - reopened on rotation
    try:
        for line in deque(fh, maxlen=lines):
            out.write(line)
        out.flush()
        if not follow:
            return
        inode = os.fstat(fh.fileno()).st_ino
        while True:
            line = fh.readline()
            if line:
                out.write(line)
                out.flush()
                continue
            try:
                if path.stat().st_ino != inode:  # rotated: continue with the new file
                    fh.close()
                    fh = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
                    inode = os.fstat(fh.fileno()).st_ino
                    continue
            except OSError:
                pass
            time.sleep(poll)
    finally:
        fh.close()
