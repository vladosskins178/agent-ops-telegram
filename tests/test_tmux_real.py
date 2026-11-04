"""End-to-end with a real tmux server on a private socket (skipped when tmux is missing).

The tests use ``tmux -L aot-test-<pid>``, a separate server, so they never see or touch
your own tmux sessions."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_ops_telegram.relay import Relay
from agent_ops_telegram.telegram import TelegramAPI
from agent_ops_telegram.tmux import Tmux

from .conftest import Clock, ConfigFactory
from .fakes import FakeTelegram

pytestmark = [pytest.mark.tmux, pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")]

READER = "import sys\nfor line in sys.stdin:\n    open(sys.argv[1], 'a').write(line)\n"


@pytest.fixture
def socket() -> Iterator[str]:
    name = f"aot-test-{os.getpid()}"
    yield name
    subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True, check=False)
    sock = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / name
    if sock.is_socket():
        sock.unlink()


def wait_for(path: Path, needle: str, timeout: float = 5.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and needle in path.read_text():
            return path.read_text()
        time.sleep(0.05)
    return path.read_text() if path.exists() else ""


def test_exact_session_match(socket: str) -> None:
    t2 = Tmux("app2", socket=socket)
    t2.new_session("sleep 30")
    assert t2.has_session()
    assert not Tmux("app", socket=socket).has_session()  # plain "-t app" would match app2


def test_relay_types_into_real_tmux(socket: str, make_config: ConfigFactory, tg: FakeTelegram, clock: Clock, tmp_path: Path) -> None:
    out = tmp_path / "received.txt"
    script = tmp_path / "reader.py"
    script.write_text(READER)
    cfg = make_config(f'tmux_bin = "tmux"\ntmux_socket = "{socket}"\nsubmit_retries = 0\nblocked_patterns = []')
    bot = cfg.bot(None)
    tmux = Tmux(bot.tmux_target, socket=socket)
    tmux.new_session(f"{sys.executable} {script} {out}")
    relay = Relay(bot, TelegramAPI(bot.token(), bot.api_base), tmux, logger=logging.getLogger("t"), clock=clock, sleep=time.sleep)
    relay.startup()
    tg.add_message("costs $5 `whoami` $(id) 'q' \"d\" \\n end")
    relay.cycle()
    text = wait_for(out, "end")
    assert "]: costs $5 `whoami` $(id) 'q' \"d\" \\n end\n" in text
    assert "[TG @owner chat_id=42" in text
    screen = "\n".join(tmux.tail(5))
    assert "costs $5" in screen
