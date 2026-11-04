# Notes for coding agents

This file is for agents working **on this repository**. To use agent-ops-telegram from
an agent session, load [`skills/agent-ops-telegram/SKILL.md`](skills/agent-ops-telegram/SKILL.md)
instead, or append `aot instructions --bot NAME` to the project's `CLAUDE.md` /
`AGENTS.md`.

## Layout

- `src/agent_ops_telegram/relay.py` receive, queue and deliver. The order of operations
  in `cycle()` (spool, then offset, then deliver) is what makes the relay lossless; keep it.
- `state.py` atomic writes, the queue, `health.json` and the single-poller `flock`.
- `tmux.py` every tmux call. Targets use `=session` for exact matching. Text only goes
  through `paste()`; `send_keys()` accepts the `ALLOWED_KEYS` names only.
- `telegram.py` a urllib Bot API client with retries; `send.py` chunking, albums and
  parse-mode fallback for `aot send`.
- `doctor.py`, `watchdog.py`, `services.py` (systemctl), `procs.py` (/proc scan),
  `units.py` (systemd templates), `init_wizard.py`, `config.py`, `security.py`
  (redaction, file permission checks), `cli.py`.
- `tests/fakes.py` a fake Bot API server and a fake tmux binary used by most tests.

## Checks before you finish

```bash
ruff check . && ruff format --check .
mypy
pytest
```

Never run the tests or the relay against a real bot token or your own tmux server.
