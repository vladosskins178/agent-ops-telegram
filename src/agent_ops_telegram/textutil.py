"""Text helpers: Telegram-sized chunks, terminal-safe sanitising, safe file names."""

from __future__ import annotations

import re
import unicodedata

TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024

# C0 controls except tab and newline, DEL, and C1 controls. ESC (0x1b) is the dangerous
# one: pasted into a terminal it can end a bracketed paste or send key sequences.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def utf16_len(text: str) -> int:
    """Length as Telegram counts it (UTF-16 code units)."""
    return len(text.encode("utf-16-le")) // 2


def split_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` UTF-16 units.

    Prefers paragraph breaks, then line breaks, then spaces, so code blocks and lists
    stay readable. Never splits a surrogate pair. Empty input gives no chunks.

    Content is preserved: no non-whitespace character is dropped, added or reordered. Only
    whitespace at a chunk boundary is removed (Telegram trims it anyway): the line breaks
    or the run of spaces where the text was split, and trailing whitespace of a chunk.
    Indentation after a line-break split is kept. Where no whitespace was found the text
    is cut hard and nothing is removed, so ``"".join(chunks) == text`` for text without
    whitespace.
    """
    if limit < 16:
        raise ValueError("limit too small")
    text = text.strip("\n")
    chunks: list[str] = []
    while text:
        if utf16_len(text) <= limit:
            if text.strip():  # a whitespace-only tail is not a message (Telegram rejects it)
                chunks.append(text.rstrip())
            break
        # largest prefix (in code points) that fits
        cut, units = 0, 0
        for ch in text:
            w = 2 if ord(ch) > 0xFFFF else 1
            if units + w > limit:
                break
            units += w
            cut += 1
        window = text[:cut]
        split_at, sep_used = -1, ""
        for sep in ("\n\n", "\n", " "):
            pos = window.rfind(sep)
            if pos >= limit // 3:
                split_at, sep_used = pos, sep
                break
        if split_at <= 0:  # hard cut inside a word: keep every character
            if window.strip():
                chunks.append(window)
            text = text[cut:]
            continue
        if sep_used == " ":  # the whole run of spaces is the separator
            head, text = window[:split_at], text[split_at + 1 :].lstrip(" ")
        else:  # keep the indentation of the next line, drop only the line breaks
            head, text = window[:split_at], text[split_at:].lstrip("\n")
        if head.strip():
            chunks.append(head.rstrip())
    return chunks


def sanitize_for_terminal(text: str) -> str:
    """Remove control characters (keeping tab and newline) and normalise line endings."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_RE.sub("", text)


def one_line(text: str, limit: int = 200) -> str:
    flat = " ".join(sanitize_for_terminal(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


_NAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, fallback: str = "file") -> str:
    """A file name that cannot escape its directory or confuse a shell."""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = name.replace("/", "_").replace("\\", "_")
    name = _NAME_SAFE.sub("_", name).strip("._-")
    if not name:
        name = fallback
    return name[-80:]


_USER_SAFE = re.compile(r"[^\w.-]+")


def safe_user(name: str) -> str:
    cleaned = _USER_SAFE.sub("", sanitize_for_terminal(name))[:32]
    return cleaned or "user"
