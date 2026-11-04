# Configuration reference

`aot` reads one TOML file. It looks in this order: `--config PATH`, `$AOT_CONFIG`,
`$XDG_CONFIG_HOME/agent-ops-telegram/config.toml` (default `~/.config/...`), then
`/etc/agent-ops-telegram/config.toml`.
`aot init` writes it for you; edit it by hand to tune the settings below.

The file must not be readable or writable by other users (`chmod 600`), and must be
owned by you or root: it holds the allow-list that decides who can type into your
agent. `aot` refuses to start otherwise. Unknown keys are errors, so typos do not pass
silently.

```toml
[defaults]                 # applies to every bot; bot tables override
env_file = "~/.config/agent-ops-telegram/secrets.env"
state_dir = "~/.local/state/agent-ops-telegram"
systemd = "user"

[bots.app]                 # one table per bot, name: lowercase letters, digits, - and _
token_env = "AOT_TOKEN_APP"
allowed_chats = [123456789]
tmux_target = "app"
start_command = "claude --continue"
workdir = "~/app"
```

## `[defaults]` only

| Key | Default | Meaning |
| --- | --- | --- |
| `env_file` | none | `KEY=VALUE` file with bot tokens. Must be mode 600 (no group or other access). A relative path is relative to the config file. |
| `state_dir` | `~/.local/state/agent-ops-telegram` | Queue, offsets, health, inbox, logs and lock files; one subdirectory per bot. |
| `systemd` | `system` when omitted (`aot init` writes `user` unless run as root) | `system`, `user` or `none`. Used by doctor, status, the watchdog and `aot units`. |
| `api_base` | `https://api.telegram.org` | Advanced: a Bot API server URL, for a self-hosted Bot API server or tests. |

Any bot setting below may also be put in `[defaults]` to apply to all bots.

## Bot settings

### Identity and access

| Key | Default | Meaning |
| --- | --- | --- |
| `token_env` | `AOT_TOKEN_<NAME>` | Name of the variable holding the token. Read from the process environment first, then `env_file`. The token never goes in this file. |
| `allowed_chats` | required | Chat ids whose messages reach the agent. Private chats have your user id; groups have negative ids. |
| `allowed_users` | `[]` | If set, the sender's user id must also be in this list. Use it for groups. |
| `default_chat` | first of `allowed_chats` | Where `aot send` and alerts go by default. |

### The agent's tmux session

| Key | Default | Meaning |
| --- | --- | --- |
| `tmux_target` | required | `session`, `session:window` or `session:window.pane`. The session is matched exactly; its name may only use letters, digits, `-` and `_` (tmux renames sessions containing `.` or `:`). |
| `tmux_socket` | `""` | tmux server socket name (`tmux -L NAME`), if the agent runs on a non-default server. |
| `tmux_bin` | `tmux` | Path to the tmux binary. |
| `start_command` | `""` | Started in a new detached session when the session is missing (by the relay on a message, by the watchdog on its next run). Empty: never start it, only queue and tell you. Runs with the service's `PATH` and without `AOT_TOKEN_*` variables. For Claude Code, `claude --continue || claude` also works in a folder with no earlier conversation. |
| `workdir` | `""` | Working directory for `start_command`. |
| `start_grace` | `20` | Seconds after a session was created (by aot or anyone else) before typing into it, so the agent can boot. |

### Delivery

| Key | Default | Meaning |
| --- | --- | --- |
| `prompt_template` | `[TG @{user} chat_id={chat_id} msg_id={msg_id}{files}{reply}]: {text}` | Fields: `bot`, `user`, `chat_id`, `msg_id`, `files` (` file=/path` per attachment), `reply` (` reply_to=ID "quote"`), `text`. |
| `bracketed_paste` | `true` | Paste with `tmux paste-buffer -p`, so multi-line text is one paste for programs that support bracketed paste. |
| `paste_chunk` | `1000` | Characters per paste; longer messages are pasted in several chunks before one Enter. |
| `long_message_chars` | `6000` | Longer messages are saved to a file in the inbox and the prompt points at it. |
| `submit_delay` | `0.4` | Seconds between paste and Enter. |
| `submit_retries` | `3` | How often to press Enter again when the text is still in the input line. `0` disables the check. |
| `submit_check_lines` | `4` | How many bottom lines of the pane count as the input area (at least 1). Enter is pressed again only if the end of the text is still there and the cursor has not moved. |
| `blocked_patterns` | Claude Code / `(y/n)` prompts | Regexes (case-insensitive) for confirmation dialogs. While one matches the last `blocked_scan_lines` lines, messages stay queued. `[]` disables. |
| `blocked_scan_lines` | `12` | Lines scanned for `blocked_patterns`. |
| `ack_reaction` | eyes emoji | Reaction set on your message once it was typed in. `""` disables. |
| `inbox_dir` | `<state_dir>/<bot>/inbox` | Where attachments are saved. aot creates a missing directory with mode 700; an existing one keeps its mode, and `aot doctor` warns if it is group- or world-writable. |
| `max_download_mb` | `20` | Size cap for downloads (the Bot API itself stops at 20 MB). |
| `skip_backlog` | `true` | On the very first start (no saved offset) ignore messages sent before the relay existed. |

### Phone commands, health and alerts

| Key | Default | Meaning |
| --- | --- | --- |
| `remote_commands` | `true` | Handle `/aot status`, `screen`, `keys`, `queue`, `flush`, `drop`, `start` in the relay. |
| `command_prefix` | `/aot` | Prefix for those commands. `/aot@name` runs only when `name` is this bot (from `getMe`, retried with a backoff until known); addressed to another bot, or while the name is unknown, it is dropped and logged, never typed in. |
| `poll_timeout` | `25` | Long-poll seconds (1-50). |
| `heartbeat_stale` | `120` | A running relay without a heartbeat for this long (and at least 3 poll timeouts) counts as stuck. |
| `notify_cooldown` | `900` | Seconds before the same warning (session missing, prompt open, 409, watchdog action) is sent again. |
| `service` | `aot-relay@<name>.service` | systemd unit the watchdog checks and restarts. |
| `restart_command` | `[]` | Command (argv list) the watchdog runs to restart the relay instead of `systemctl restart`, e.g. for supervisord. |

## Files under `state_dir/<bot>/`

| Path | Content |
| --- | --- |
| `offset` | `{"offset": N, "bot_id": "..."}`: the next Telegram update id to fetch and the id of the bot it belongs to. A switch of token is detected from this record (or, failing that, `health.json`): when `getMe` reports a different bot id, the relay forgets the command mark, archives the old bot's queue, starts from offset 0 (keeping the new bot's backlog, whatever `skip_backlog` says) and tells the owner. Until `getMe` confirms which bot the token belongs to, the relay does not poll at all, so the old offset can never confirm away the new bot's messages. When nothing records the bot (a plain number from an older version, and no `health.json`), the relay peeks at the updates Telegram holds: if one below the saved offset is a message it would have queued but did not, it treats that as a suspected switch (offset 0, owner told; queue and command mark kept); otherwise it also continues from offset 0, and what is already queued or recorded as delivered is recognised by bot, update id and message date, not typed twice. A corrupt file is moved to `offset.bad` and logged; the relay then resumes at the offset in `health.json` (or just past the newest queued update) instead of skipping the backlog. If the offset and `health.json` are both lost but the queue still holds records, the relay is not treated as new and keeps the backlog. |
| `commands.json` | Update id of the last `/aot` command run, so a replayed batch never runs a command twice (ignored after 24 hours). Removed only on a confirmed token switch. |
| `seq` | The next queue position. Persisted before use, so queue order never depends on the clock or on Telegram's update ids; rebuilt from the queue file names if lost. |
| `queue/<seq>.json` | Messages received but not yet typed in, one per update, named by their 15-digit zero-padded queue position (`<seq>`) and delivered in that order (files from older versions, named by update id, go first). The allow-list is checked again before typing; messages from chats or users no longer allowed are dropped and logged. Unreadable ones are renamed to `.bad`, logged and reported to `default_chat`. |
| `queue/<seq>.done` | A delivered message whose file could not be deleted (read-only or broken file system). Never typed again; a held copy fetched again from Telegram is recognised and skipped. Removed on startup once older than 7 days. |
| `delivered.json` | Fallback when a delivered message's file could neither be deleted nor renamed to `.done`: the records listed here are never typed again. Entries are dropped once their file is gone. `aot doctor` and `aot status` only read it. |
| `queue.bot<old id>/` | After a confirmed token switch, the old bot's queued messages, moved here and not delivered (the owner is told how many and where). |
| `health.json` | pid, bot id, start time, offset, heartbeat (wall clock, plus `heartbeat_mono`/`started_mono` and `boot_id`, which the watchdog prefers within one boot so a clock step never looks like a stuck relay), last poll, last allowed message, last error, last 409, and `fatal` plus `fatal_reason` (`token_rejected` or `config_error`) when the relay exited with 78 (written only if no other relay holds `relay.lock`, so a failed second start never marks a running relay as stopped). |
| `inbox/` | Downloaded attachments and saved long messages, named `<timestamp>-<chat>-<msg>-<hex6>-<name>`. Not cleaned automatically. |
| `relay.log` | Log, rotated at 1 MB, 3 backups. Tokens are redacted. If it cannot be opened (read-only or full file system), the relay logs to stderr (journald) only and keeps running. |
| `watchdog.json` | When each watchdog alert was last sent. |
| `token_rejected`, `config_error` | Present while the relay is stopped with exit 78 for that reason and the watchdog has sent its one alert about it. |
| `relay.lock` | Held by the running relay, so two relays for the same bot name never share this directory, even with different tokens. |

`state_dir/locks/bot-<hash>.lock` is the single-poller lock; the hash is of the token, so
two configs for the same bot that share a `state_dir` also share the lock. Relays with
different state directories are still caught by Telegram's 409 Conflict, which the relay
reports and `aot doctor` shows. `aot doctor` also warns (`poller.shared_token`) when
another bot in the same config, or in the config of another running relay, uses the same
token.

## Without systemd

Set `systemd = "none"`, run `aot relay --bot NAME` under your supervisor of choice, and
call the watchdog from cron. Give it a `restart_command` so it can restart the relay:

```cron
*/2 * * * * AOT_CONFIG=/home/me/.config/agent-ops-telegram/config.toml /home/me/.local/bin/aot watchdog
```
