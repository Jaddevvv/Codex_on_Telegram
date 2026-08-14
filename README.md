# Codex on Telegram

Control the Codex CLI from one private Telegram chat. The bridge runs the Codex app server locally, keeps conversation state, streams useful progress into a single edited Telegram message, and removes that temporary progress message when the turn finishes.

The project uses Telegram long polling, Python's standard library, and your existing Codex CLI login. It does not require a webhook, public server, Telegram framework, or separate OpenAI API key.

## Features

- Send coding tasks to Codex from Telegram.
- See live progress in one message that is edited as work advances.
- Automatically delete the progress message after the final answer or error.
- Select a Codex model with `/model`.
- Select supported reasoning effort with `/think`.
- Choose approval and sandbox behavior with `/permissions`.
- Compact the active context with `/compact`.
- Set, inspect, pause, complete, or clear a durable thread goal with `/goal`.
- Inspect task, context, and subscription-limit status with `/status`.
- Stop an active turn, start a new conversation, or resume a recent conversation.
- Restrict access to one Telegram chat ID.
- Split long responses into Telegram-safe chunks.

## Requirements

- Linux or macOS, or Windows through WSL
- Python 3.10+
- Codex CLI installed and signed in
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Quick start

```bash
git clone https://github.com/Jaddevvv/Codex_on_Telegram.git
cd Codex_on_Telegram
./setup.sh
```

Edit `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:your_real_bot_token
ALLOWED_CHAT_ID=123456789
CODEX_WORKSPACE=/home/me/code
CODEX_DEFAULT_EFFORT=medium
```

Find your private chat ID by messaging the bot and opening `https://api.telegram.org/bot<TOKEN>/getUpdates`. Use the numeric `message.chat.id` value.

Start the bridge:

```bash
./run.sh
```

## Telegram commands

| Command | Action |
| --- | --- |
| `/start`, `/help` | Show the command reference. |
| `/model` | List numbered models; reply with a number to select one. |
| `/model MODEL_ID` | Select a model for subsequent turns. |
| `/think` | List numbered reasoning levels; reply with a number to select one. |
| `/think LEVEL` | Select a reasoning level for subsequent turns. |
| `/permissions` | List the three numbered Codex permission choices; reply with a number to select one. |
| `/permissions 1` | Ask for approval. |
| `/permissions 2` | Approve for me (the default). |
| `/permissions 3` | Full Access. |
| `/compact` | Compact the current Codex context. |
| `/goal` | Show the current durable goal. |
| `/goal OBJECTIVE` | Set or replace the current durable goal. |
| `/goal paused`, `/goal complete` | Change the current goal status. |
| `/goal clear` | Remove the current durable goal. |
| `/status`, `/debug` | Show the model, reasoning level, active task, context use, and rate limits. |
| `/stop` | Interrupt the active turn. |
| `/new` | Start a fresh Codex conversation. |
| `/resume` | List recent conversations. |
| `/resume NUMBER` | Resume a listed conversation. |

If a numbered menu is active, the next message is used as that selection. Otherwise, any other text starts a Codex turn. Only one turn runs at a time.

## Configuration

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Token issued by BotFather. |
| `ALLOWED_CHAT_ID` | Yes | — | The only Telegram chat allowed to control Codex. |
| `CODEX_BIN` | No | `/root/.local/bin/codex` | Path to the Codex executable. |
| `CODEX_WORKSPACE` | No | `~/codex-workspace` | Working directory and writable root provided to Codex. |
| `CODEX_DEFAULT_EFFORT` | No | `medium` | Preferred reasoning effort when supported by the model. |
| `CODEX_PERMISSION_MODE` | No | `approve` | Initial mode: `1` (ask), `2` (approve for me), or `3` (full access). |
| `CODEX_EVENT_LOG` | No | `/root/codex-telegram/events.log` | Private app-server event log path. |

Restart the bridge after changing `.env`.

## Continuous operation with systemd

Edit the two paths in `systemd/codex-telegram.service`, then install it as a user service:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/codex-telegram.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-telegram
```

Inspect or restart it with:

```bash
systemctl --user status codex-telegram
journalctl --user -u codex-telegram -f
systemctl --user restart codex-telegram
```

## Security

This bridge can allow Codex to edit files and run commands in `CODEX_WORKSPACE`.

- Keep `.env`, the bot token, Codex authentication, and event logs out of Git.
- Use a private one-to-one Telegram chat.
- Run the service as an unprivileged OS user.
- Limit `CODEX_WORKSPACE` to the projects the bot should access.
- Keep the default approve-for-me mode scoped to `CODEX_WORKSPACE`; choose full access only for a deliberate, temporary need.
- Revoke the BotFather token immediately if it is exposed.
- The app-server integration accepts supported approval requests for the active session, so treat Telegram access as remote shell-adjacent access.

## Development

Run the dependency-free unit tests:

```bash
python3 -m unittest discover -s tests -v
```
