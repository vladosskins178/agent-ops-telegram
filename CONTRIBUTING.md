# Contributing

Thanks for helping. The most useful bug reports describe a message that went missing or
arrived twice: what was sent, what the agent's screen showed, `aot doctor --json`
(tokens are redacted) and the relevant lines of `aot logs`.

## Setup

```bash
git clone https://github.com/vladosskins178/agent-ops-telegram
cd agent-ops-telegram
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## Before you open a pull request

```bash
ruff check . && ruff format --check .
mypy
pytest                  # Telegram and tmux are faked; tests/test_tmux_real.py also runs if tmux is installed
```

The fake Bot API server lives in `tests/fakes.py`; extend it rather than calling the
real Telegram API from tests. Tests that use real tmux must use their own socket
(`tmux -L ...`) so they never touch the developer's sessions.

## Guidelines

- **Never lose a message.** Anything that changes the receive or deliver path needs a
  test that restarts the relay at the interesting point and checks that every message
  still arrives once, in order.
- **Secrets.** Nothing may print or log a token. Route new log or error text through the
  logger (which redacts) or `redact()`, and assert in tests that the token is absent.
- **No shell.** Text from Telegram must never reach a shell or `send-keys` as text; it
  goes through tmux buffers.
- **Dependencies.** Standard library only (plus `tomli` on Python 3.10). Ask in an issue
  before adding one.
- New doctor checks need a stable id (`area.name`), a status that matches the impact
  (`fail` only when messages are or will be lost), a `fix` a person can act on, a test,
  and a row in the README troubleshooting table if it is a common failure.

## Releasing

1. Update `version` in `pyproject.toml`, `src/agent_ops_telegram/__init__.py` and
   `.claude-plugin/plugin.json`.
2. Move the "Unreleased" notes in `CHANGELOG.md` under the new version.
3. Tag `vX.Y.Z` and push the tag.
