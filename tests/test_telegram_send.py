from __future__ import annotations

from pathlib import Path

import pytest

from agent_ops_telegram.send import send_files, send_text
from agent_ops_telegram.telegram import TelegramAPI, TelegramError

from .conftest import Clock
from .fakes import TOKEN, FakeTelegram

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def api(tg: FakeTelegram, clock: Clock, **kw: object) -> TelegramAPI:
    return TelegramAPI(TOKEN, tg.base_url, sleep=clock.sleep, **kw)  # type: ignore[arg-type]


def test_get_me_and_bad_token(tg: FakeTelegram, clock: Clock) -> None:
    assert api(tg, clock).get_me()["username"] == "fake_bot"
    bad = TelegramAPI("1:" + "x" * 35, tg.base_url, sleep=clock.sleep)
    with pytest.raises(TelegramError) as info:
        bad.get_me()
    assert info.value.code == 401
    assert "x" * 35 not in str(info.value)


def test_retries_5xx_then_succeeds(tg: FakeTelegram, clock: Clock) -> None:
    tg.fail("sendMessage", 502, "Bad Gateway", times=2)
    res = send_text(api(tg, clock), 42, "hi")
    assert len(res.message_ids) == 1
    assert clock.slept == [1.0, 2.0]  # exponential backoff


def test_429_waits_retry_after(tg: FakeTelegram, clock: Clock) -> None:
    tg.fail("sendMessage", 429, "Too Many Requests: retry after 7", retry_after=7)
    send_text(api(tg, clock), 42, "hi")
    assert clock.slept == [7.0]


def test_4xx_is_not_retried(tg: FakeTelegram, clock: Clock) -> None:
    tg.fail("sendMessage", 400, "Bad Request: chat not found")
    with pytest.raises(TelegramError, match="chat not found"):
        send_text(api(tg, clock), 42, "hi")
    assert clock.slept == []
    assert tg.methods().count("sendMessage") == 1


def test_gives_up_after_retries(tg: FakeTelegram, clock: Clock) -> None:
    tg.fail("sendMessage", 500, "Internal", times=10)
    with pytest.raises(TelegramError):
        send_text(api(tg, clock, retries=2), 42, "hi")
    assert tg.methods().count("sendMessage") == 3


def test_network_error_is_retryable(clock: Clock) -> None:
    dead = TelegramAPI(TOKEN, "http://127.0.0.1:9", sleep=clock.sleep, retries=1, timeout=2)
    with pytest.raises(TelegramError) as info:
        dead.get_updates(0, 1)  # getUpdates never retries on its own
    assert info.value.network and info.value.retryable
    assert TOKEN not in str(info.value)


def test_long_text_is_chunked_and_plain(tg: FakeTelegram, clock: Clock) -> None:
    text = "line with $HOME and `ticks` and *stars*_\n" * 300
    res = send_text(api(tg, clock), 42, text)
    assert len(res.message_ids) == len(tg.sent) == 4
    assert all(len(m["text"]) <= 4096 for m in tg.sent)
    assert all("parse_mode" not in m for m in tg.sent)
    assert "$HOME" in tg.sent[0]["text"]


def test_markdown_parse_error_falls_back_to_plain(tg: FakeTelegram, clock: Clock) -> None:
    res = send_text(api(tg, clock), 42, "BAD *markdown", parse_mode="MarkdownV2")
    assert res.message_ids
    assert "plain text" in res.notes[0]
    assert tg.sent[-1].get("parse_mode") is None


def test_fake_server_rejects_blank_text_like_telegram(tg: FakeTelegram, clock: Clock) -> None:
    with pytest.raises(TelegramError, match="text is empty"):
        api(tg, clock).send_message(42, " \n\t ")
    # split_text never produces such a chunk, so a trailing blank block sends cleanly
    res = send_text(api(tg, clock), 42, "x" * 4096 + "\n\n" + " " * 50)
    assert len(res.message_ids) == 1 and tg.sent[0]["text"] == "x" * 4096


def test_send_photo_document_and_album(tg: FakeTelegram, clock: Clock, tmp_path: Path) -> None:
    shots = []
    for i in range(12):
        p = tmp_path / f"shot{i}.png"
        p.write_bytes(PNG)
        shots.append(p)
    a = api(tg, clock)
    send_files(a, 42, "photo", shots[:1], caption="one")
    send_files(a, 42, "photo", shots, caption="many")
    doc = tmp_path / "report.txt"
    doc.write_text("x")
    send_files(a, 42, "document", [doc])
    methods = [u["method"] for u in tg.uploads]
    assert methods == ["sendPhoto", "sendMediaGroup", "sendMediaGroup", "sendDocument"]
    assert tg.uploads[0]["params"]["caption"] == "one"
    first_album = tg.uploads[1]["params"]["media"]
    assert len(first_album) == 10 and first_album[0]["caption"] == "many"
    assert len(tg.uploads[2]["params"]["media"]) == 2
    assert "caption" not in tg.uploads[2]["params"]["media"][0]


def test_non_image_photo_goes_as_document(tg: FakeTelegram, clock: Clock, tmp_path: Path) -> None:
    f = tmp_path / "data.csv"
    f.write_text("a,b")
    res = send_files(api(tg, clock), 42, "photo", [f])
    assert tg.uploads[0]["method"] == "sendDocument"
    assert res.notes


def test_long_caption_sent_as_text(tg: FakeTelegram, clock: Clock, tmp_path: Path) -> None:
    p = tmp_path / "a.png"
    p.write_bytes(PNG)
    send_files(api(tg, clock), 42, "photo", [p], caption="c" * 2000)
    assert "caption" not in tg.uploads[0]["params"]
    assert tg.sent[0]["text"] == "c" * 2000


def test_missing_file(tg: FakeTelegram, clock: Clock, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        send_files(api(tg, clock), 42, "photo", [tmp_path / "nope.png"])


def test_download_respects_limit(tg: FakeTelegram, clock: Clock, tmp_path: Path) -> None:
    tg.add_file("F1", "photos/file_1.jpg", b"x" * 1000)
    a = api(tg, clock)
    dest = a.download("F1", tmp_path / "in" / "a.jpg", 10_000)
    assert dest.read_bytes() == b"x" * 1000
    assert (dest.stat().st_mode & 0o777) == 0o600
    with pytest.raises(TelegramError, match="limit"):
        a.download("F1", tmp_path / "in" / "b.jpg", 10)
    assert not (tmp_path / "in" / "b.jpg").exists()


def test_truncated_response_is_retried(tg: FakeTelegram, clock: Clock) -> None:
    tg.truncate_next = 1
    res = send_text(api(tg, clock), 42, "after a broken response")
    assert len(res.message_ids) == 1
    assert clock.slept == [1.0]


def test_truncated_poll_is_a_telegram_error(tg: FakeTelegram, clock: Clock) -> None:
    tg.truncate_next = 1
    with pytest.raises(TelegramError) as info:
        api(tg, clock).get_updates(0, 1)
    assert info.value.network


def test_bad_base_url_does_not_leak_token(clock: Clock) -> None:
    bad = TelegramAPI(TOKEN, "api.telegram.org", sleep=clock.sleep, retries=0)
    with pytest.raises(TelegramError) as info:
        bad.get_me()
    assert TOKEN not in str(info.value)


def test_split_keeps_indentation() -> None:
    from agent_ops_telegram.textutil import split_text

    text = "x" * 3000 + "\n    indented " + "y" * 2000
    chunks = split_text(text)
    assert chunks[1].startswith("    indented y")


def test_photo_fallback_to_document_keeps_parse_mode(tg: FakeTelegram, clock: Clock, tmp_path: Path) -> None:
    p = tmp_path / "a.png"
    p.write_bytes(PNG)
    tg.fail("sendPhoto", 400, "Bad Request: PHOTO_INVALID_DIMENSIONS")
    send_files(api(tg, clock), 42, "photo", [p], caption="<b>x</b>", parse_mode="HTML")
    doc = tg.uploads[-1]
    assert doc["method"] == "sendDocument" and doc["params"]["parse_mode"] == "HTML"


@pytest.mark.parametrize(
    ("status", "body", "code", "retry"),
    [
        (400, {"ok": False, "error_code": "bad", "description": "x", "parameters": {"retry_after": "soon"}}, 400, 0.0),
        (429, {"ok": False, "error_code": None, "description": "x", "parameters": {"retry_after": [3]}}, 429, 0.0),
        (429, {"ok": False, "error_code": "429", "description": "x", "parameters": {"retry_after": "2"}}, 429, 2.0),
        (200, {"ok": False, "error_code": {"n": 1}, "description": "x"}, 400, 0.0),
        (200, {"ok": False, "error_code": "nan", "description": "x"}, 400, 0.0),
    ],
)
def test_garbage_error_fields_do_not_crash(
    tg: FakeTelegram, clock: Clock, monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, object], code: int, retry: float
) -> None:
    monkeypatch.setattr(tg, "handle", lambda method, params, files: (status, body))
    with pytest.raises(TelegramError) as info:
        api(tg, clock, retries=0).get_me()
    assert info.value.code == code and info.value.retry_after == retry
