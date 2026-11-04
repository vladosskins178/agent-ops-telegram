# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-04

First public release.

### Added

- `aot relay`: long-polls `getUpdates`, filters by `allowed_chats` / `allowed_users`,
  writes each message to an fsynced on-disk queue before saving the offset (a disk error
  keeps the offset, so nothing is confirmed that was not stored), and types it into a
  tmux pane through `load-buffer` + bracketed `paste-buffer` (no shell). Exact session
  matching, control-character stripping, chunked paste (a failure halfway is reported,
  never re-typed), long messages saved to a file, Enter retried only when the text is
  still in the input line and the cursor has not moved, copy mode left before typing,
  in-order delivery, confirmation dialogs detected right before pasting and before
  Enter (the message's own text is ignored), missing sessions started with
  `start_command` without the bot token in their environment, a boot grace period for
  fresh sessions, photos/documents/audio/voice/video downloaded to a private inbox
  (retried on transient errors), reply context, forwarded text marked line by line, an
  acknowledgement reaction, SIGTERM deferred while typing, and per-update / per-item
  error isolation.
- Phone commands: `/aot status|screen|keys|queue|flush|drop|start` (flush and drop only
  touch messages sent before the command).
- Single poller per token: `flock` keyed by a token hash (exit code 3 when taken), 409
  Conflict detection and alerting, duplicate-process detection limited to the same user
  and config.
- `aot send`: text chunked at 4096 UTF-16 units on paragraph/line/word boundaries,
  photos, documents and albums, plain text by default with Markdown/HTML fallback,
  retries with backoff for network errors, 5xx and 429, `sent` only on `ok: true`,
  stdin and heredoc input, allow-listed chats only.
- `aot doctor` (text, `--json`, `--strict`; `--fix` removes a webhook): token, webhook,
  pending updates, single poller, recent 409, heartbeat, service and timer, tmux session
  and open prompts, queue age, disk and inbox size, groups allowed without
  `allowed_users`.
- `aot watchdog`: restarts dead or stuck relays (or stops a stuck one so systemd restarts
  it when `systemctl restart` is not allowed), stops duplicate pollers, starts missing
  agent sessions, alerts on Telegram with a cooldown; `restart_command` for setups
  without systemd.
- `aot status`, `aot logs` (file or `--journal`).
- `aot init` wizard (interactive or `--yes`, `--allowed-user`), validated before
  anything is written; refuses reused tokens and non-regular files. `aot units` writes a
  templated `aot-relay@.service` plus a watchdog timer (system or user scope) with
  `KillMode=process`, an explicit `PATH`, escaped `%`/`$`, and refuses to overwrite
  masked or linked units.
- `aot instructions`, `aot skill install|print`, an agent skill
  (`skills/agent-ops-telegram/SKILL.md`) and a Claude Code plugin with a marketplace
  manifest.
- Token redaction in logs, errors and screen output (tracebacks are kept, only the
  secrets inside them are masked); config and secrets files that others can read are
  refused; env files accept `export`, quotes and inline ` # comments` on unquoted values.
- `aot send --reply-to` works for text, photos, documents and albums.
- A missing token, a broken config or a token Telegram rejects (401) at startup makes
  `aot relay` exit with 78, which the relay unit lists in `RestartPreventExitStatus`.
- `aot status` / `aot doctor` show `unknown (<reason>)` when systemctl cannot be queried.

### Security

- Forwarded messages (any `forward_*` field or `is_automatic_forward`) and messages sent
  via an inline bot are never run as `/aot` commands or answered as `/start`; they are
  typed in as marked data.
- `/aot@name` runs only when `name` is this bot's username; `getMe` is retried with a
  backoff until it is known. Commands addressed to another bot, or sent while the name
  is unknown, are dropped and logged, never typed in.
- The allow-list is checked again when a queued message is delivered, so a message
  from a chat or user removed from the config is dropped.
- State, config and lock files are written through temp files with random names,
  created with `O_CREAT|O_EXCL|O_NOFOLLOW` and mode 600. Directories aot creates are
  mode 700; an existing `inbox_dir` keeps its mode and `aot doctor` warns when it is
  group- or world-writable.
- Queue files that are not valid records (corrupt JSON or the wrong shape) are moved
  aside as `.bad`, logged and reported on Telegram. A corrupt offset file is moved to
  `offset.bad` and the relay fetches every update Telegram still holds instead of
  skipping the backlog.
- Attachment and long-message file names are `<timestamp>-<chat>-<msg>-<hex6>-<name>`;
  an existing file is never overwritten.
- `aot init` lists every chat that wrote during the wait (type, id, @username, name,
  sender) and preselects nothing unless exactly one private chat wrote. It shell-quotes
  the working directory in its "Next steps".
- Unit files: `--user` must be a POSIX user name, and values with newlines or control
  characters are refused instead of being written into the unit.
- The watchdog only signals a relay after checking that the PID is this bot's relay
  (command line, user, config) and, for a stuck relay, not newer than the stale
  heartbeat. It sends SIGTERM, waits 10 s, and sends SIGKILL only if the same process
  is still alive; duplicate pollers are re-checked right before they are signalled.
- `aot doctor` warns when another bot or config uses the same token.

[0.1.0]: https://github.com/vladosskins178/agent-ops-telegram/releases/tag/v0.1.0
