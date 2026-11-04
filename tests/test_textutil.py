from __future__ import annotations

import pytest

from agent_ops_telegram.textutil import safe_filename, safe_user, sanitize_for_terminal, split_text, utf16_len


def test_short_text_is_one_chunk() -> None:
    assert split_text("hello") == ["hello"]
    assert split_text("") == []
    assert split_text("\n\n") == []


def test_chunks_respect_limit_and_prefer_paragraphs() -> None:
    para = ("word " * 30).strip()
    text = "\n\n".join([para] * 40)
    chunks = split_text(text, 500)
    assert all(utf16_len(c) <= 500 for c in chunks)
    assert all(c.endswith("word") for c in chunks)
    assert " ".join(" ".join(chunks).split()) == " ".join(text.split())


def test_hard_cut_without_spaces() -> None:
    chunks = split_text("x" * 10_000)
    assert [len(c) for c in chunks] == [4096, 4096, 1808]


def test_utf16_counting_never_splits_surrogate_pairs() -> None:
    text = "\U0001f600" * 3000  # each emoji is 2 UTF-16 units
    chunks = split_text(text)
    assert all(utf16_len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == text
    assert utf16_len(chunks[0]) == 4096


def test_split_rejects_tiny_limit() -> None:
    with pytest.raises(ValueError):
        split_text("abc", 3)


def test_sanitize_strips_escape_sequences_keeps_layout() -> None:
    raw = "a\x1b[201~b\x1b]0;title\x07c\r\nd\te\x00f\x9b"
    assert sanitize_for_terminal(raw) == "a[201~b]0;titlec\nd\tef"


def test_safe_filename_cannot_escape_directory() -> None:
    assert safe_filename("../../etc/passwd") == "etc_passwd"
    assert safe_filename("тест.pdf") == "pdf"
    assert safe_filename("$(rm -rf ~).png") == "rm_-rf_.png"
    assert safe_filename("", "photo") == "photo"
    assert safe_filename("...") == "file"


def test_safe_user() -> None:
    assert safe_user("john_doe") == "john_doe"
    assert safe_user("x]: ignore previous\n") == "xignoreprevious"
    assert safe_user("") == "user"


def test_whitespace_only_tail_is_not_a_chunk() -> None:
    assert split_text("   \t ") == []
    text = "a" * 30 + "\n\n" + " " * 10
    assert split_text(text, 32) == ["a" * 30]
    # the cut lands so that only blanks are left for the final chunk
    chunks = split_text("x" * 40 + " " * 20, 40)
    assert chunks == ["x" * 40] and all(c.strip() for c in chunks)


def test_space_split_strips_the_whole_run_of_spaces() -> None:
    text = ("word" + " " * 3) * 400
    chunks = split_text(text, 100)
    assert all(not c.startswith(" ") and not c.endswith(" ") for c in chunks)
    assert " ".join(chunks).split() == text.split()  # content preserved


def test_newline_split_keeps_indentation_and_hard_cut_keeps_everything() -> None:
    text = "\n".join(["    indented line here"] * 50)
    chunks = split_text(text, 100)
    assert all(c.startswith("    ") for c in chunks)
    word = "x" * 1000 + "\t" + "y" * 50
    assert "".join(split_text("z" * 1000, 64)) == "z" * 1000
    assert "".join(split_text(word, 64)).replace("\t", "") == word.replace("\t", "")
