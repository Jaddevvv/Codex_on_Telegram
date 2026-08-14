#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10 or newer is required." >&2
  exit 1
fi

if ! command -v codex >/dev/null 2>&1; then
  echo "Codex CLI was not found. Install it with:" >&2
  echo "  curl -fsSL https://chatgpt.com/codex/install.sh | sh" >&2
  exit 1
fi

if ! codex login status >/dev/null 2>&1; then
  echo "Codex is not signed in. Running 'codex login' now..."
  codex login
fi

if [[ ! -f "$project_dir/.env" ]]; then
  cp "$project_dir/.env.example" "$project_dir/.env"
  echo "Created $project_dir/.env"
fi

echo
echo "Next: edit .env and set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
echo "Then start the bot with: ./run.sh"
