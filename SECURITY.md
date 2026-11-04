# Security

agent-ops-telegram turns Telegram messages into prompts for a coding agent that usually
has shell access. Please read the threat model before you deploy it.

## Threat model

- **Allow-listed accounts are trusted with the server.** Whoever can send messages from
  a chat in `allowed_chats` (and, if set, a user in `allowed_users`) can make the agent
  run commands. Protect those Telegram accounts with two-step verification, keep the
  list short, set `allowed_users` for groups, and run the agent as an unprivileged user.
- **Prompt injection.** Forwarded messages, files the agent opens and web pages it reads
  can contain instructions. The relay marks forwarded text as `[forwarded]` and the
  bundled instructions tell the agent to treat such content as data, but this reduces
  the risk; it does not remove it.
- **Sessions the relay starts do not inherit bot tokens**: `AOT_TOKEN_*` variables and
  the bot's `token_env` are removed from the environment of `tmux new-session`. This is
  defence in depth only: the agent runs as the same user, so it can still read the
  secrets file. Real isolation needs the agent under a separate user account.
- **Everyone else is ignored.** Messages from other chats are dropped without a reply,
  and their content is not logged.
- **Remote commands** (`/aot keys`, `/aot screen`...) are available to allow-listed chats
  only. `keys` accepts a fixed set of key names; `screen` output is redacted for tokens
  but can show anything else that is on the agent's screen. Disable them with
  `remote_commands = false`.

## What aot does with secrets

- Bot tokens come from the environment or an env file that must be mode 600; they are
  never written to the config, printed, or logged. Log lines, error messages, doctor
  output and `/aot screen` text pass through a redaction filter for Telegram tokens.
- The config file must not be readable or writable by other users, and must be owned by
  you or root, because it holds the allow-list.
- Queue files, downloads, saved long messages and logs are created with mode 600 in
  directories with mode 700. Attachment names are sanitised and stay inside the inbox.
- `aot` talks only to the Telegram Bot API (`api_base`, default
  `https://api.telegram.org`).

## Reporting a vulnerability

If you find a way to make `aot` deliver a message from a chat that is not allow-listed,
reveal a token, write outside its state directory, inbox and the paths you pass it, or
run commands it should not, please report it privately through GitHub's
[security advisory form](https://github.com/vladosskins178/agent-ops-telegram/security/advisories/new)
rather than a public issue. You will get a reply within a few days.
