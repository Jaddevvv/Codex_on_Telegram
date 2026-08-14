# Codex on Telegram

Control the OpenAI Codex CLI from a private Telegram chat. Send a coding task from your phone; the bot runs Codex against your local projects and sends the result back.

This project is intentionally small:

- one Python file;
- no Python packages, database, webhook, public server, or Telegram framework;
- no OpenAI keys copied into this repository;
- one allowed Telegram chat ID;
- one command to run it.

It uses Telegram long polling, so it works behind home routers and firewalls without exposing an inbound port.

## What it can do

Anything the Codex CLI can do within the configured workspace and permission mode, including:

- explain or search a repository;
- edit files and implement features;
- run tests, linters, and local commands;
- diagnose errors;
- continue a conversation about the same project;
- switch safely between projects under one parent directory.

Example conversation:

```text
You: /cd my-web-app
Bot: Working directory: /home/me/code/my-web-app

You: Find why the login tests fail, fix the issue, and run the tests again.
Bot: Working on it…
Bot: Fixed the session cookie configuration and ran the login test suite...

You: Now add a regression test for the expired-cookie case.
Bot: Working on it…
```

Follow-up messages reuse the Codex session for that directory. `/new` starts a clean conversation without touching any files.

## Requirements

- Linux or macOS (Windows works through WSL)
- Python 3.10+
- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli), installed and signed in
- a Telegram account

The bot uses your existing Codex CLI authentication. Codex supports signing in with ChatGPT or an API key; see the [official authentication guide](https://learn.chatgpt.com/docs/auth). You do not put OpenAI credentials in `.env`.

## Quick start

### 1. Clone and enter the repository

```bash
git clone https://github.com/Jaddevvv/Codex_on_Telegram.git
cd Codex_on_Telegram
```

### 2. Install and sign in to Codex

If `codex` is not installed:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Then sign in and verify it:

```bash
codex login
codex login status
```

For a headless machine, use `codex login --device-auth`. The [official Codex authentication docs](https://learn.chatgpt.com/docs/auth#login-on-headless-devices) also describe API-key login and securely moving an auth cache.

### 3. Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Choose a display name and a username ending in `bot`.
4. Copy the token BotFather gives you. Treat it like a password.
5. Open your new bot and press **Start**, or send it any message.

### 4. Find your Telegram chat ID

After messaging the bot once, open this URL in a browser, replacing `<TOKEN>` with the BotFather token:

```text
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Find `message.chat.id` in the JSON response. For a private chat it looks like `123456789`. If the result is empty, send the bot another message and reload.

### 5. Configure the bot

```bash
./setup.sh
```

Edit `.env` and replace the two example values:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:your_real_bot_token
TELEGRAM_CHAT_ID=123456789
```

By default, Codex can only see this cloned repository. To let it work across your projects, set their common parent directory:

```dotenv
CODEX_WORKSPACE=/home/me/code
```

### 6. Start it

```bash
./run.sh
```

Leave that process running and message the bot. Stop it with `Ctrl+C`.

## Telegram commands

| Command | What it does |
| --- | --- |
| `/start` | Show the welcome message and command reference. |
| `/help` | Show commands and usage examples. |
| `/status` | Show workspace, directory, sandbox, model, session, and running-task status. |
| `/cwd` | Show the directory Codex currently works in. |
| `/cd <path>` | Change directory. Relative paths start at `CODEX_WORKSPACE`; paths cannot escape it. |
| `/new` | Forget the current conversation and start a fresh Codex session in this directory. |
| `/reset` | Alias for `/new`. |
| `/stop` | Terminate the task currently running. |
| `/id` | Show the authorized Telegram chat ID. |

All non-command text is sent to Codex as a task. Only one task runs at a time, which prevents two phone messages from editing the same checkout concurrently.

## Configuration

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Token issued by BotFather. |
| `TELEGRAM_CHAT_ID` | Yes | — | The only chat allowed to use the bot. |
| `CODEX_WORKSPACE` | No | Repository directory | Root directory available through `/cd`. |
| `CODEX_SANDBOX` | No | `workspace-write` | `workspace-write` lets Codex edit; `read-only` is inspection only. |
| `CODEX_MODEL` | No | Codex CLI default | Optional model override passed to `codex exec`. |
| `CODEX_BIN` | No | `codex` | Path or command name for the Codex executable. |
| `CODEX_TIMEOUT` | No | `1800` | Maximum seconds for one task. |

After changing `.env`, restart the bot.

## Run it continuously with systemd

The included user service keeps the bot running after logout and restarts it after a crash.

1. Edit `systemd/codex-telegram.service` and replace both `YOUR_USER` paths.
2. Install and start it:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/codex-telegram.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-telegram
```

Useful service commands:

```bash
systemctl --user status codex-telegram
journalctl --user -u codex-telegram -f
systemctl --user restart codex-telegram
systemctl --user stop codex-telegram
```

On some Linux servers, enable lingering so the user service starts before login:

```bash
loginctl enable-linger "$USER"
```

## Security model

This bot can edit and run code on your machine. Treat it like remote shell-adjacent access.

- Messages from every chat except `TELEGRAM_CHAT_ID` are ignored.
- `/cd` cannot leave `CODEX_WORKSPACE`, including through `..` or symlinks.
- The default `workspace-write` Codex sandbox limits file writes to the active workspace.
- `danger-full-access` is deliberately rejected by this project.
- `.env` is gitignored. Never commit the bot token or `~/.codex/auth.json`.
- Use a private one-to-one chat, not a group.
- Set `CODEX_SANDBOX=read-only` if you only want repository questions and reviews.
- Run the bot as an unprivileged OS user with access only to the projects it needs.

If the token is exposed, message BotFather and use `/revoke` immediately, then put the replacement token in `.env`.

## How it works

```text
Your Telegram message
        ↓
Telegram getUpdates long poll
        ↓
Chat-ID and workspace checks
        ↓
codex exec (or codex exec resume)
        ↓
Final Codex response split into Telegram-sized messages
```

The bridge invokes Codex in its documented [non-interactive mode](https://learn.chatgpt.com/docs/developer-commands?surface=cli#codex-exec). Prompts go through standard input rather than a shell, so message text is not interpreted as a shell command by the bridge. Codex itself may choose to run commands according to its sandbox and your project instructions.

Conversation IDs live in memory and are separated by directory. Restarting the bridge begins fresh bot-side conversations; Codex’s own local session history remains on the machine.

## Troubleshooting

### `Configuration error`

Run `./setup.sh`, open `.env`, and replace both example values. The chat ID must contain only an integer (negative IDs are valid for groups, though private chats are recommended).

### `Codex CLI not found`

Install Codex with the command in the quick start, then open a new terminal or set `CODEX_BIN` to the full executable path.

### Codex reports an authentication error

```bash
codex login status
codex login
```

When running through systemd, make sure the service runs as the same OS user that completed `codex login`.

### The bot does not reply

- Confirm the terminal running `./run.sh` has no error.
- Make sure you pressed **Start** in the bot chat.
- Re-check `TELEGRAM_CHAT_ID` using `getUpdates`.
- Make sure only one copy of the bot is running; Telegram long polling should have one consumer.
- Test the token by opening `https://api.telegram.org/bot<TOKEN>/getMe`.

### `Conflict: terminated by other getUpdates request`

Another bot process is using the same token. Stop the duplicate local process, service, container, or deployment.

### A task is stuck

Send `/stop`. Increase or reduce `CODEX_TIMEOUT` as needed. Run the same prompt directly with `codex` to diagnose project-specific approval, tool, or configuration issues.

### Codex cannot access another project

Set `CODEX_WORKSPACE` to a parent directory containing that project, restart the bot, then use `/cd project-name`.

## Development

Run the dependency-free unit tests:

```bash
python3 -m unittest discover -s tests -v
```

The test suite covers Telegram message splitting, Codex session-event parsing, and workspace path isolation. A live end-to-end test requires real Telegram and Codex credentials and is intentionally not part of CI.

## Why this is simpler

Many remote coding assistants add an SDK layer, database, Docker volumes, browser automation, GitHub credentials, and multiple user/session abstractions. Those features can be useful, but they also create setup and security work.

Codex on Telegram is aimed at one person controlling an already working local Codex installation. Telegram handles messaging, Codex handles coding, and this repository is only the small private bridge between them.

## License

[MIT](LICENSE)
