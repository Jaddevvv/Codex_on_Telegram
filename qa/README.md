# Telegram message-flow QA

Run from the repository root with Python 3:

```sh
python3 -m unittest qa.test_telegram_flow -v
QA_BOT_PATH=/root/codex-telegram/bot.py python3 -m unittest qa.test_telegram_flow -v
python3 -m unittest qa.test_attachments_steering -v
```

The first command checks the checkout; the second checks the bridge file at that path. Set `QA_BOT_PATH` to any compatible bridge file. A nonzero exit means a check failed. The tests replace Telegram and Codex with local fakes: they do not send messages, start Codex, read bot credentials, or change real conversations.

`qa.test_attachments_steering` drives the same loop with a fake agent backend: a files-only drop is saved and acknowledged once without starting a run, the next prompt lists every pending file by absolute path, `/new` switches to a fresh conversation folder, captions on albums run once for all files, text sent during a run steers it (or is queued when steering fails), `/stop` drops queued messages, outbox files are sent back, and `/resume` restores a conversation folder. It also checks that `CodexAppServer` and the fake provide every name in `BACKEND_REQUIRED`.

The flow suite checks early `/new` acknowledgement during a stalled startup, completion and topic title reset, isolation between topics during a slow startup, named conversations, and ignored topic lifecycle events. The half-second timeout is an offline event-loop regression threshold, not a promise about real network latency.

These checks cannot measure Telegram delivery, real Codex startup time, service health, topic permissions, or live message ordering. For a live check, use a dedicated test topic and time `/new` through its acknowledgement and completion, then send a prompt and check the response and title. Do not run live smoke tests in an active user's conversation.
