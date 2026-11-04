"""Outbound messages for ``aot send``: chunked text, photos, documents and albums."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .telegram import TelegramAPI, TelegramError
from .textutil import TELEGRAM_CAPTION_LIMIT, TELEGRAM_TEXT_LIMIT, split_text, utf16_len

PHOTO_MAX_BYTES = 10 * 1024 * 1024
UPLOAD_MAX_BYTES = 50 * 1024 * 1024
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


@dataclass
class SendResult:
    message_ids: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _is_parse_error(exc: TelegramError) -> bool:
    return exc.code == 400 and "parse" in exc.description.lower() and "entit" in exc.description.lower()


def _ids(result: Any) -> list[int]:
    items = result if isinstance(result, list) else [result]
    return [int(r["message_id"]) for r in items if isinstance(r, dict) and "message_id" in r]


def send_text(api: TelegramAPI, chat_id: int, text: str, *, parse_mode: str | None = None, reply_to: int | None = None) -> SendResult:
    """Send ``text`` in as many 4096-unit messages as needed.

    With ``parse_mode``, a chunk Telegram cannot parse is re-sent as plain text instead
    of failing (a stray ``_`` or ``*`` is the usual cause).
    """
    out = SendResult()
    chunks = split_text(text, TELEGRAM_TEXT_LIMIT)
    if not chunks:
        raise ValueError("nothing to send: the message is empty")
    for i, chunk in enumerate(chunks):
        try:
            out.message_ids += _ids(api.send_message(chat_id, chunk, parse_mode=parse_mode, reply_to=reply_to if i == 0 else None))
        except TelegramError as exc:
            if parse_mode and _is_parse_error(exc):
                out.notes.append(f"part {i + 1}: {parse_mode} did not parse, sent as plain text")
                out.message_ids += _ids(api.send_message(chat_id, chunk, reply_to=reply_to if i == 0 else None))
            else:
                raise
    return out


def _check_files(paths: Sequence[Path]) -> None:
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"{path}: no such file")
        if path.stat().st_size > UPLOAD_MAX_BYTES:
            raise ValueError(f"{path}: bigger than the 50 MB Bot API upload limit")


def send_files(
    api: TelegramAPI,
    chat_id: int,
    kind: str,
    paths: Sequence[Path],
    *,
    caption: str = "",
    parse_mode: str | None = None,
    reply_to: int | None = None,
) -> SendResult:
    """Send photos or documents. One file goes as a single message, 2-10 as an album,
    more as several albums. A caption over 1024 units is sent as a separate text.
    ``reply_to`` makes the first message (or album) a reply."""
    if kind not in {"photo", "document"}:
        raise ValueError("kind must be photo or document")
    _check_files(paths)
    out = SendResult()
    text_after = ""
    if utf16_len(caption) > TELEGRAM_CAPTION_LIMIT:
        text_after, caption = caption, ""
    paths = list(paths)
    if kind == "photo":
        too_big = [p for p in paths if p.stat().st_size > PHOTO_MAX_BYTES or p.suffix.lower() not in PHOTO_EXTS]
        if too_big:
            out.notes.append(f"{len(too_big)} file(s) are not photos Telegram accepts (or over 10 MB); sending as documents")
            kind = "document"
    batches = [paths[i : i + 10] for i in range(0, len(paths), 10)]
    method, field_ = ("sendPhoto", "photo") if kind == "photo" else ("sendDocument", "document")
    for n, batch in enumerate(batches):
        cap = caption if n == 0 else ""
        reply = reply_to if n == 0 else None
        try:
            if len(batch) == 1:
                out.message_ids += _ids(
                    api.send_file(method, field_, chat_id, batch[0], caption=cap, parse_mode=parse_mode, reply_to=reply)
                )
            else:
                out.message_ids += _ids(api.send_media_group(chat_id, kind, batch, caption=cap, parse_mode=parse_mode, reply_to=reply))
        except TelegramError as exc:
            if parse_mode and cap and _is_parse_error(exc):
                out.notes.append("caption did not parse, sent as plain text")
                if len(batch) == 1:
                    out.message_ids += _ids(api.send_file(method, field_, chat_id, batch[0], caption=cap, reply_to=reply))
                else:
                    out.message_ids += _ids(api.send_media_group(chat_id, kind, batch, caption=cap, reply_to=reply))
            elif kind == "photo" and exc.code == 400 and "photo" in exc.description.lower():
                out.notes.append(f"Telegram refused a photo ({exc.description}); sent as documents")
                more = send_files(api, chat_id, "document", batch, caption=cap, parse_mode=parse_mode, reply_to=reply)
                out.notes += more.notes
                out.message_ids += more.message_ids
            else:
                raise
    if text_after:
        out.message_ids += send_text(api, chat_id, text_after, parse_mode=parse_mode).message_ids
    return out
