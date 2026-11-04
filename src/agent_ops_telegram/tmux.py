"""tmux control: exact-match targets, paste through buffers (no shell), pane capture."""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence

# Key names `/aot keys` may send. Anything else is refused, so a phone message can never
# type arbitrary text through send-keys (text goes through the paste path instead).
ALLOWED_KEYS = frozenset(
    {"Enter", "Escape", "Tab", "BTab", "Up", "Down", "Left", "Right", "BSpace", "Space", "C-c", "C-d", "y", "n", "PageUp", "PageDown"}
    | {str(i) for i in range(10)}
)


class TmuxError(Exception):
    def __init__(self, message: str, *, partial: bool = False) -> None:
        super().__init__(message)
        self.partial = partial  # True when some text may already have reached the pane


_counter = itertools.count()


class Tmux:
    def __init__(
        self,
        target: str,
        *,
        socket: str = "",
        binary: str = "tmux",
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 10.0,
    ) -> None:
        self.target = target
        self.session = target.split(":", 1)[0]
        self.socket = socket
        self.binary = binary
        self.sleep = sleep
        self.timeout = timeout
        self.scrub_env: set[str] = set()

    # ------------------------------------------------------------------ plumbing

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _cmd(self, args: Sequence[str]) -> list[str]:
        base = [self.binary]
        if self.socket:
            base += ["-L", self.socket]
        return base + list(args)

    def run(
        self, args: Sequence[str], *, input_text: str | None = None, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                self._cmd(args),
                input=input_text,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TmuxError(f"tmux binary '{self.binary}' not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise TmuxError(f"tmux {args[0]} timed out after {self.timeout:.0f}s") from exc
        if check and proc.returncode != 0:
            raise TmuxError(f"tmux {args[0]} failed: {proc.stderr.strip() or proc.returncode}")
        return proc

    @property
    def _session_t(self) -> str:
        # "=name" asks tmux for an exact match. Without it, "-t app" would happily match
        # a session called "app2" when "app" is gone, and deliver prompts to the wrong agent.
        return f"={self.session}"

    @property
    def _pane_t(self) -> str:
        rest = self.target.split(":", 1)[1] if ":" in self.target else ""
        return f"={self.session}:{rest}"

    # ------------------------------------------------------------------ queries

    def has_session(self) -> bool:
        try:
            return self.run(["has-session", "-t", self._session_t], check=False).returncode == 0
        except TmuxError:
            return False

    def capture(self, lines: int = 40) -> str:
        proc = self.run(["capture-pane", "-p", "-J", "-t", self._pane_t, "-S", f"-{max(lines, 1)}"])
        return proc.stdout

    def tail(self, lines: int) -> list[str]:
        text = self.capture(max(lines, 1) + 20)
        rows = [r.rstrip() for r in text.splitlines()]
        while rows and not rows[-1].strip():
            rows.pop()
        return rows[-lines:]

    def session_created(self) -> float | None:
        """Unix time the session was created (to give a fresh agent time to boot)."""
        try:
            return float(self.run(["display-message", "-p", "-t", self._session_t, "#{session_created}"]).stdout.strip())
        except (TmuxError, ValueError):
            return None

    def in_mode(self) -> bool:
        """Whether the pane is in copy mode (or another mode) where keys do not reach the program."""
        try:
            return self.run(["display-message", "-p", "-t", self._pane_t, "#{pane_in_mode}"]).stdout.strip() == "1"
        except TmuxError:
            return False

    def exit_mode(self) -> None:
        self.run(["send-keys", "-t", self._pane_t, "-X", "cancel"], check=False)

    def cursor(self) -> tuple[int, int] | None:
        """Cursor position in the pane, or ``None`` if tmux cannot tell."""
        try:
            out = self.run(["display-message", "-p", "-t", self._pane_t, "#{cursor_x} #{cursor_y}"]).stdout.split()
            return int(out[0]), int(out[1])
        except (TmuxError, ValueError, IndexError):
            return None

    # ------------------------------------------------------------------ actions

    def new_session(self, command: str, *, workdir: str = "", env: dict[str, str] | None = None) -> None:
        args = ["new-session", "-d", "-s", self.session]
        if workdir:
            args += ["-c", os.path.expanduser(workdir)]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        if command:
            args.append(command)
        # A tmux server started here inherits our environment: keep bot tokens out of it,
        # so the agent (and anything it runs) never sees them.
        clean = {k: v for k, v in os.environ.items() if not k.startswith("AOT_TOKEN_") and k not in self.scrub_env}
        self.run(args, env=clean)

    def paste(self, text: str, *, chunk: int = 1000, bracketed: bool = True, chunk_pause: float = 0.05) -> None:
        """Paste ``text`` into the pane via tmux buffers. No shell is involved, so ``$``,
        quotes and backticks arrive as typed. Long text goes in several pastes.

        All buffers are loaded before the first paste, so a failure while loading types
        nothing. A failure after the first paste raises ``TmuxError(partial=True)``."""
        tag = f"aot-{os.getpid()}-{next(_counter)}"
        buffers: list[str] = []
        pasted = 0
        try:
            for n, i in enumerate(range(0, len(text), chunk)):
                buf = f"{tag}-{n}"
                buffers.append(buf)
                self.run(["load-buffer", "-b", buf, "-"], input_text=text[i : i + chunk])
            for buf in buffers:
                args = ["paste-buffer", "-d", "-b", buf, "-t", self._pane_t]
                if bracketed:
                    args.insert(1, "-p")
                try:
                    self.run(args)
                except TmuxError as exc:
                    raise TmuxError(str(exc), partial=pasted > 0) from exc
                pasted += 1
                if pasted < len(buffers):
                    self.sleep(chunk_pause)
        finally:
            for buf in buffers[pasted:]:  # pasted buffers were deleted by paste-buffer -d
                try:
                    self.run(["delete-buffer", "-b", buf], check=False)
                except TmuxError:
                    pass

    def send_keys(self, keys: Sequence[str]) -> None:
        bad = [k for k in keys if k not in ALLOWED_KEYS]
        if bad:
            raise TmuxError(f"key(s) not allowed: {', '.join(bad)}")
        if keys:
            self.run(["send-keys", "-t", self._pane_t, *keys])

    def press_enter(self) -> None:
        self.run(["send-keys", "-t", self._pane_t, "Enter"])
