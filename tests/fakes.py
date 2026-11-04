"""Test doubles: an in-process fake Telegram Bot API server and a fake tmux binary."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmn"


def parse_multipart(body: bytes, ctype: str) -> tuple[dict[str, Any], dict[str, tuple[str, bytes]]]:
    boundary = re.search(r"boundary=(\S+)", ctype)
    assert boundary, ctype
    fields: dict[str, Any] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in body.split(b"--" + boundary.group(1).encode()):
        if not part.strip() or part.strip() == b"--":
            continue
        head, _, content = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
        content = content.removesuffix(b"\r\n")
        disp = head.decode()
        name = re.search(r'name="([^"]*)"', disp)
        filename = re.search(r'filename="([^"]*)"', disp)
        assert name
        if filename:
            files[name.group(1)] = (filename.group(1), content)
        else:
            raw = content.decode()
            try:
                fields[name.group(1)] = json.loads(raw) if raw[:1] in "[{" else raw
            except ValueError:
                fields[name.group(1)] = raw
    return fields, files


class FakeTelegram:
    """Implements the slice of the Bot API the relay and `aot send` use."""

    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.updates: list[dict[str, Any]] = []
        self.next_update_id = 1000
        self.next_message_id = 500
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sent: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []
        self.failures: dict[str, list[tuple[int, str, float]]] = {}
        self.webhook_url = ""
        self.conflict = False
        self.files: dict[str, tuple[str, bytes]] = {}
        self.truncate_next = 0
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------ test helpers

    def fail(self, method: str, code: int, description: str, retry_after: float = 0, times: int = 1) -> None:
        self.failures.setdefault(method, []).extend([(code, description, retry_after)] * times)

    def add_message(self, text: str = "", chat_id: int = 42, user_id: int = 42, username: str = "owner", **extra: Any) -> int:
        with self.lock:
            self.next_message_id += 1
            msg: dict[str, Any] = {
                "message_id": self.next_message_id,
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "private", "username": username},
                "from": {"id": user_id, "is_bot": False, "username": username},
            }
            if text:
                msg["text"] = text
            msg.update(extra)
            self.updates.append({"update_id": self.next_update_id, "message": msg})
            self.next_update_id += 1
            return int(msg["message_id"])

    def add_file(self, file_id: str, file_path: str, content: bytes) -> None:
        self.files[file_id] = (file_path, content)

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    # ------------------------------------------------------------ API

    def handle(self, method: str, params: dict[str, Any], files: dict[str, tuple[str, bytes]]) -> tuple[int, dict[str, Any]]:
        with self.lock:
            self.calls.append((method, params))
            queue = self.failures.get(method)
            if queue:
                code, desc, retry_after = queue.pop(0)
                body: dict[str, Any] = {"ok": False, "error_code": code, "description": desc}
                if retry_after:
                    body["parameters"] = {"retry_after": retry_after}
                return code, body
            if method == "getMe":
                return 200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "fake_bot"}}
            if method == "getWebhookInfo":
                return 200, {"ok": True, "result": {"url": self.webhook_url, "pending_update_count": len(self.updates)}}
            if method == "deleteWebhook":
                self.webhook_url = ""
                return 200, {"ok": True, "result": True}
            if method == "getUpdates":
                if self.conflict or self.webhook_url:
                    return 409, {"ok": False, "error_code": 409, "description": "Conflict: terminated by other getUpdates request"}
                offset = int(params.get("offset") or 0)
                if offset < 0:
                    result = self.updates[offset:]
                else:
                    self.updates = [u for u in self.updates if u["update_id"] >= offset]
                    result = list(self.updates)
                return 200, {"ok": True, "result": result[: int(params.get("limit") or 100)]}
            if method == "sendMessage":
                if not str(params.get("text") or "").strip():
                    return 400, {"ok": False, "error_code": 400, "description": "Bad Request: message text is empty"}
                if params.get("parse_mode") and "BAD" in str(params.get("text")):
                    return 400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities: unclosed tag"}
                self.next_message_id += 1
                self.sent.append(params)
                return 200, {"ok": True, "result": {"message_id": self.next_message_id, "text": params.get("text")}}
            if method in {"sendPhoto", "sendDocument"}:
                self.next_message_id += 1
                self.uploads.append({"method": method, "params": params, "files": {k: (n, len(c)) for k, (n, c) in files.items()}})
                return 200, {"ok": True, "result": {"message_id": self.next_message_id}}
            if method == "sendMediaGroup":
                self.uploads.append({"method": method, "params": params, "files": {k: (n, len(c)) for k, (n, c) in files.items()}})
                result = []
                for _ in params["media"]:
                    self.next_message_id += 1
                    result.append({"message_id": self.next_message_id})
                return 200, {"ok": True, "result": result}
            if method == "setMessageReaction":
                self.reactions.append(params)
                return 200, {"ok": True, "result": True}
            if method == "getFile":
                fid = params.get("file_id")
                if fid not in self.files:
                    return 400, {"ok": False, "error_code": 400, "description": "Bad Request: invalid file_id"}
                path, content = self.files[fid]
                return 200, {"ok": True, "result": {"file_id": fid, "file_path": path, "file_size": len(content)}}
            return 404, {"ok": False, "error_code": 404, "description": "Not Found"}

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, code: int, body: dict[str, Any] | bytes) -> None:
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                if fake.truncate_next > 0:  # promise more bytes than we send: IncompleteRead
                    fake.truncate_next -= 1
                    self.send_header("Content-Length", str(len(data) + 100))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data[: len(data) // 2])
                    self.close_connection = True
                    return
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                prefix = f"/file/bot{fake.token}/"
                if self.path.startswith(prefix):
                    want = self.path[len(prefix) :]
                    for path, content in fake.files.values():
                        if path == want:
                            self._send(200, content)
                            return
                self._send(404, b"not found")

            def do_POST(self) -> None:
                m = re.match(r"^/bot([^/]+)/(\w+)$", self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if not m:
                    self._send(404, {"ok": False, "error_code": 404, "description": "Not Found"})
                    return
                if m.group(1) != fake.token:
                    self._send(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                    return
                ctype = self.headers.get("Content-Type", "")
                files: dict[str, tuple[str, bytes]] = {}
                if ctype.startswith("multipart/"):
                    params, files = parse_multipart(body, ctype)
                else:
                    params = json.loads(body or b"{}")
                code, resp = fake.handle(m.group(2), params, files)
                self._send(code, resp)

        return Handler


FAKE_TMUX = r'''#!PYTHON
"""Fake tmux for tests: state in $FAKE_TMUX_STATE (JSON)."""
import json, os, sys

path = os.environ["FAKE_TMUX_STATE"]
try:
    with open(path) as fh:
        st = json.load(fh)
except FileNotFoundError:
    st = {"sessions": {}, "buffers": {}, "log": []}
argv = sys.argv[1:]
if argv[:1] == ["-L"]:
    st.setdefault("sockets", []).append(argv[1])
    argv = argv[2:]
st["log"].append(argv)
code = 0
out = ""
err = ""

def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default

def session_of(target):
    if target is None:
        return None
    t = target[1:] if target.startswith("=") else target
    return t.split(":", 1)[0]

cmd = argv[0]
fail = st.get("fail", {}).get(cmd)
after = st.get("fail_after", {}).get(cmd)
if after is not None:
    if after > 0:
        st["fail_after"][cmd] = after - 1
        fail = None
    else:
        fail = "simulated failure"
if fail:
    code, err = 1, fail
elif cmd == "has-session":
    code = 0 if session_of(opt("-t")) in st["sessions"] else 1
    if code:
        err = "can't find session"
elif cmd == "new-session":
    name = opt("-s")
    envs, rest, i = [], "", 1
    while i < len(argv):
        a = argv[i]
        if a in ("-s", "-c", "-e", "-x", "-y"):
            if a == "-e":
                envs.append(argv[i + 1])
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            rest = a
            i += 1
    st["sessions"][name] = {"screen": [], "input": "", "eat_enter": 0, "cwd": opt("-c", ""), "env": envs, "command": rest, "keys": [],
                            "inherited_tokens": sorted(k for k in os.environ if k.startswith("AOT_TOKEN_") or k == "MY_SECRET_TOKEN")}
elif cmd == "load-buffer":
    st["buffers"][opt("-b")] = sys.stdin.read()
elif cmd == "delete-buffer":
    st["buffers"].pop(opt("-b"), None)
elif cmd == "paste-buffer":
    s = st["sessions"].get(session_of(opt("-t")))
    if s is None:
        code, err = 1, "can't find pane"
    else:
        s["input"] += st["buffers"].get(opt("-b"), "")
        s.setdefault("bracketed", []).append("-p" in argv)
        if "-d" in argv:
            st["buffers"].pop(opt("-b"), None)
elif cmd == "send-keys":
    s = st["sessions"].get(session_of(opt("-t")))
    if s is None:
        code, err = 1, "can't find pane"
    else:
        keys = argv[argv.index("-t") + 2:]
        if keys[:1] == ["-X"]:
            s["in_mode"] = False
            keys = []
        if s.get("in_mode"):
            keys = []  # copy mode swallows keys
        for k in keys:
            if k == "Enter":
                if s.get("eat_enter", 0) > 0:
                    s["eat_enter"] -= 1
                elif s["input"]:
                    s.setdefault("submitted", []).append(s["input"])
                    s["screen"] += ["> " + line for line in s["input"].split("\n")] + ["", "working..."]
                    s["input"] = ""
            else:
                s["keys"].append(k)
elif cmd == "display-message":
    s = st["sessions"].get(session_of(opt("-t")))
    if s is None:
        code, err = 1, "can't find pane"
    else:
        if "session_created" in argv[-1]:
            out = "%d\n" % s.get("created", 0)
        elif "pane_in_mode" in argv[-1]:
            out = "%d\n" % (1 if s.get("in_mode") else 0)
        else:
            out = "%d %d\n" % (len(s["input"]) % 80, len(s["screen"]))
elif cmd == "capture-pane":
    s = st["sessions"].get(session_of(opt("-t")))
    if s is None:
        code, err = 1, "can't find pane"
    else:
        lines = list(s["screen"])
        lines += ["-" * 20] + ["> " + l for l in s["input"].split("\n")] + ["-" * 20, "  ? for shortcuts"]
        out = "\n".join(lines) + "\n"
else:
    code, err = 1, "unknown command " + cmd

with open(path, "w") as fh:
    json.dump(st, fh)
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(code)
'''


class FakeTmux:
    def __init__(self, directory: Path) -> None:
        self.bin = directory / "tmux"
        self.state_file = directory / "tmux-state.json"
        self.bin.write_text(FAKE_TMUX.replace("#!PYTHON", "#!" + sys.executable))
        self.bin.chmod(0o755)

    def state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"sessions": {}, "buffers": {}, "log": []}
        data: dict[str, Any] = json.loads(self.state_file.read_text())
        return data

    def save(self, st: dict[str, Any]) -> None:
        self.state_file.write_text(json.dumps(st))

    def add_session(
        self, name: str, screen: list[str] | None = None, eat_enter: int = 0, created: float = 0, in_mode: bool = False
    ) -> None:
        st = self.state()
        st["sessions"][name] = {
            "screen": screen or [],
            "input": "",
            "eat_enter": eat_enter,
            "keys": [],
            "env": [],
            "command": "",
            "created": created,
            "in_mode": in_mode,
        }
        self.save(st)

    def fail(self, command: str, after: int | None = None, message: str = "simulated failure") -> None:
        """Make ``command`` fail: always, or after ``after`` more successful calls."""
        st = self.state()
        if after is None:
            st.setdefault("fail", {})[command] = message
        else:
            st.setdefault("fail_after", {})[command] = after
        self.save(st)

    def heal(self) -> None:
        st = self.state()
        st["fail"], st["fail_after"] = {}, {}
        self.save(st)

    def session(self, name: str) -> dict[str, Any]:
        result: dict[str, Any] = self.state()["sessions"][name]
        return result

    def set_screen(self, name: str, screen: list[str]) -> None:
        st = self.state()
        st["sessions"][name]["screen"] = screen
        self.save(st)

    def commands(self) -> list[str]:
        return [argv[0] for argv in self.state()["log"]]
