"""A small Telegram Bot API client on top of urllib (no third-party dependencies)."""

from __future__ import annotations

import contextlib
import http.client
import json
import mimetypes
import os
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .security import redact, register_secret

USER_AGENT = f"agent-ops-telegram/{__version__}"


class TelegramError(Exception):
    """The Bot API answered ``ok: false`` or could not be reached."""

    def __init__(self, description: str, code: int = 0, retry_after: float = 0.0) -> None:
        super().__init__(redact(description))
        self.description = redact(description)
        self.code = code
        self.retry_after = retry_after

    @property
    def network(self) -> bool:
        return self.code == 0

    @property
    def conflict(self) -> bool:
        return self.code == 409

    @property
    def retryable(self) -> bool:
        return self.code == 0 or self.code == 429 or self.code >= 500


def _multipart(fields: dict[str, Any], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = "aot" + secrets.token_hex(16)
    parts: list[bytes] = []
    for key, value in fields.items():
        if value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(value)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode() + text.encode() + b"\r\n")
    for key, path in files.items():
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        safe_name = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{safe_name}"\r\nContent-Type: {ctype}\r\n\r\n'
        parts.append(head.encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _add_reply(params: dict[str, Any], reply_to: int | None) -> None:
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}


class TelegramAPI:
    """Bot API calls with retries for network errors, 5xx and 429 (``retry_after``)."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.telegram.org",
        *,
        retries: int = 4,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        max_backoff: float = 30.0,
    ) -> None:
        if not token:
            raise ValueError("empty bot token")
        register_secret(token)
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.timeout = timeout
        self.sleep = sleep
        self.max_backoff = max_backoff

    def __repr__(self) -> str:
        return f"TelegramAPI(base_url={self.base_url!r})"

    def _url(self, method: str) -> str:
        return f"{self.base_url}/bot{self._token}/{method}"

    def _once(self, method: str, params: dict[str, Any], files: dict[str, Path] | None, timeout: float) -> Any:
        if files:
            body, ctype = _multipart(params, files)
        else:
            body, ctype = json.dumps({k: v for k, v in params.items() if v is not None}).encode(), "application/json"
        try:
            req = urllib.request.Request(self._url(method), data=body, method="POST")
        except ValueError:  # the message would contain the URL, and with it the token
            raise TelegramError(f"invalid API URL for {method}; check api_base", 400) from None
        req.add_header("Content-Type", ctype)
        req.add_header("User-Agent", USER_AGENT)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read() or b""
                data = json.loads(raw)
            except (ValueError, OSError, http.client.HTTPException):
                data = None
            if not isinstance(data, dict):
                raise TelegramError(f"HTTP {exc.code} from Telegram on {method}", exc.code) from None
            extra = data.get("parameters")
            params_: dict[str, Any] = extra if isinstance(extra, dict) else {}
            raise TelegramError(
                str(data.get("description") or f"HTTP {exc.code}"),
                _number(data.get("error_code"), exc.code),
                max(float(_number(params_.get("retry_after"), 0, real=True)), 0.0),
            ) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            raise TelegramError(f"network error on {method}: {reason}") from None
        except ValueError as exc:  # e.g. a malformed api_base; the message would contain the URL
            raise TelegramError(f"invalid request for {method}: {type(exc).__name__}", 400) from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise TelegramError(f"invalid JSON from Telegram on {method}", 502) from None
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description", "unknown error") if isinstance(data, dict) else "unknown error"
            code = _number(data.get("error_code"), 400) if isinstance(data, dict) else 400
            raise TelegramError(str(desc), code)
        return data.get("result")

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        files: dict[str, Path] | None = None,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> Any:
        """Call ``method``; return ``result`` when Telegram says ``ok: true``, raise :class:`TelegramError` otherwise."""
        attempts = 1 + (self.retries if retries is None else retries)
        delay = 1.0
        for attempt in range(attempts):
            try:
                return self._once(method, params or {}, files, timeout or self.timeout)
            except TelegramError as exc:
                if not exc.retryable or attempt == attempts - 1:
                    raise
                wait = exc.retry_after if exc.code == 429 and exc.retry_after else delay
                self.sleep(min(wait, self.max_backoff * 2 if exc.code == 429 else self.max_backoff))
                delay = min(delay * 2, self.max_backoff)
        raise AssertionError("unreachable")  # pragma: no cover

    # --------------------------------------------------------------- helpers

    def get_me(self) -> dict[str, Any]:
        result: dict[str, Any] = self.call("getMe", retries=1, timeout=15)
        return result

    def webhook_info(self) -> dict[str, Any]:
        result: dict[str, Any] = self.call("getWebhookInfo", retries=1, timeout=15)
        return result

    def get_updates(self, offset: int, timeout: int, *, limit: int = 100) -> list[dict[str, Any]]:
        result = self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "limit": limit, "allowed_updates": ["message"]},
            timeout=timeout + 15,
            retries=0,
        )
        return list(result or [])

    def send_message(
        self, chat_id: int, text: str, *, parse_mode: str | None = None, reply_to: int | None = None, retries: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "link_preview_options": {"is_disabled": True}}
        _add_reply(params, reply_to)
        result: dict[str, Any] = self.call("sendMessage", params, retries=retries)
        return result

    def send_file(
        self,
        method: str,
        field: str,
        chat_id: int,
        path: Path,
        *,
        caption: str = "",
        parse_mode: str | None = None,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": chat_id, "caption": caption or None, "parse_mode": parse_mode if caption else None}
        _add_reply(params, reply_to)
        result: dict[str, Any] = self.call(method, params, files={field: path}, timeout=120)
        return result

    def send_media_group(
        self,
        chat_id: int,
        kind: str,
        paths: Sequence[Path],
        *,
        caption: str = "",
        parse_mode: str | None = None,
        reply_to: int | None = None,
    ) -> list[Any]:
        media: list[dict[str, Any]] = []
        files: dict[str, Path] = {}
        for i, path in enumerate(paths):
            item: dict[str, Any] = {"type": kind, "media": f"attach://file{i}"}
            if i == 0 and caption:
                item["caption"] = caption
                if parse_mode:
                    item["parse_mode"] = parse_mode
            media.append(item)
            files[f"file{i}"] = path
        params: dict[str, Any] = {"chat_id": chat_id, "media": media}
        _add_reply(params, reply_to)
        result = self.call("sendMediaGroup", params, files=files, timeout=180)
        return list(result or [])

    def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.call(
            "setMessageReaction",
            {"chat_id": chat_id, "message_id": message_id, "reaction": [{"type": "emoji", "emoji": emoji}]},
            retries=0,
            timeout=10,
        )

    def delete_webhook(self) -> None:
        self.call("deleteWebhook", {"drop_pending_updates": False}, retries=1)

    def download(self, file_id: str, dest: Path, max_bytes: int, progress: Callable[[], None] | None = None) -> Path:
        """Download a file sent to the bot into ``dest`` (created with mode 0600).
        ``progress`` is called after every block, so a caller can keep a heartbeat."""
        info = self.call("getFile", {"file_id": file_id}, timeout=30)
        if not isinstance(info, dict):
            raise TelegramError("unexpected getFile answer", 502)
        size = int(info.get("file_size") or 0)
        if size and size > max_bytes:
            raise TelegramError(f"file is {size} bytes, over the {max_bytes} byte limit", 413)
        file_path = str(info.get("file_path") or "")
        if not file_path:
            raise TelegramError("Telegram returned no file_path (bots can only download files up to 20 MB)", 400)
        url = f"{self.base_url}/file/bot{self._token}/{file_path}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        except ValueError:
            raise TelegramError("invalid file URL; check api_base", 400) from None
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Reserve the name first: an existing file (or symlink) at ``dest`` is never replaced.
        os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600))
        tmp = dest.with_name(dest.name + ".part")
        done = False
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                fd = _open_private(tmp)
                total = 0
                with fd as fh:
                    while True:
                        block = resp.read(65536)
                        if not block:
                            break
                        total += len(block)
                        if total > max_bytes:
                            raise TelegramError(f"file is over the {max_bytes} byte limit", 413)
                        fh.write(block)
                        if progress is not None:
                            progress()
            tmp.replace(dest)
            done = True
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"HTTP {exc.code} downloading a file", exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
            raise TelegramError(f"network error downloading a file: {getattr(exc, 'reason', None) or type(exc).__name__}") from None
        finally:
            if tmp.exists():
                tmp.unlink()
            if not done:
                with contextlib.suppress(OSError):
                    dest.unlink()  # our empty reservation
        return dest


def _number(value: Any, default: int, *, real: bool = False) -> Any:
    """``error_code`` / ``retry_after`` from an error body, or ``default`` when the field is
    missing or not a plain number (a proxy or a broken server must not crash the caller)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        number = float(value)
    except ValueError:
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    if real:
        return number
    return int(number) if number else default


def _open_private(path: Path) -> Any:
    """Create ``path`` with mode 600; never follow a symlink planted at that name."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    return os.fdopen(fd, "wb")
