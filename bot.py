#!/usr/bin/env python3
"""A tiny, private Telegram bridge for the Codex CLI."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


LOG = logging.getLogger("codex-telegram")
TELEGRAM_MESSAGE_LIMIT = 4096


class ConfigError(ValueError):
    """Raised when startup configuration is invalid."""


@dataclass(frozen=True)
class Config:
    token: str
    chat_id: int
    workspace: pathlib.Path
    codex_bin: str = "codex"
    sandbox: str = "workspace-write"
    model: str | None = None
    timeout: int = 1800

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id_text = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or "replace_with" in token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is missing or still uses the example value")
        try:
            chat_id = int(chat_id_text)
        except ValueError as exc:
            raise ConfigError("TELEGRAM_CHAT_ID must be an integer") from exc

        project_dir = pathlib.Path(__file__).resolve().parent
        workspace = pathlib.Path(os.getenv("CODEX_WORKSPACE", project_dir)).expanduser().resolve()
        if not workspace.is_dir():
            raise ConfigError(f"CODEX_WORKSPACE is not a directory: {workspace}")

        sandbox = os.getenv("CODEX_SANDBOX", "workspace-write").strip()
        if sandbox not in {"read-only", "workspace-write"}:
            raise ConfigError("CODEX_SANDBOX must be read-only or workspace-write")

        try:
            timeout = int(os.getenv("CODEX_TIMEOUT", "1800"))
        except ValueError as exc:
            raise ConfigError("CODEX_TIMEOUT must be an integer number of seconds") from exc
        if timeout < 1:
            raise ConfigError("CODEX_TIMEOUT must be positive")

        return cls(
            token=token,
            chat_id=chat_id,
            workspace=workspace,
            codex_bin=os.getenv("CODEX_BIN", "codex").strip() or "codex",
            sandbox=sandbox,
            model=os.getenv("CODEX_MODEL", "").strip() or None,
            timeout=timeout,
        )


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split text into Telegram-safe chunks, preferring newline boundaries."""
    text = text.strip() or "(Codex returned no text.)"
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = text.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def extract_session_id(output: str) -> str | None:
    """Find a Codex thread/session ID in JSONL without depending on one event version."""
    preferred_keys = ("thread_id", "session_id")
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        pending: list[Any] = [event]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                for key in preferred_keys:
                    candidate = value.get(key)
                    if isinstance(candidate, str) and candidate:
                        return candidate
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
    return None


def safe_directory(root: pathlib.Path, requested: str) -> pathlib.Path:
    """Resolve a requested directory and keep it under the configured workspace."""
    candidate = pathlib.Path(requested).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Directory must stay inside CODEX_WORKSPACE") from exc
    if not candidate.is_dir():
        raise ValueError("Directory does not exist")
    return candidate


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **params: Any) -> Any:
        encoded = urllib.parse.urlencode(params).encode()
        request = urllib.request.Request(self.base_url + method, data=encoded)
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Telegram {method} failed ({exc.code}): {detail}") from exc
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {payload}")
        return payload.get("result")

    def send(self, chat_id: int, text: str) -> None:
        for chunk in split_message(text):
            self.call("sendMessage", chat_id=chat_id, text=chunk)


class CodexRunner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.current: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()

    def stop(self) -> bool:
        with self.lock:
            process = self.current
            if process is None or process.poll() is not None:
                return False
            process.terminate()
            return True

    def run(self, prompt: str, cwd: pathlib.Path, session_id: str | None) -> tuple[str, str | None]:
        output_file = tempfile.NamedTemporaryFile(prefix="codex-telegram-", delete=False)
        output_path = pathlib.Path(output_file.name)
        output_file.close()

        command = [
            self.config.codex_bin,
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            self.config.sandbox,
            "--cd",
            str(cwd),
            "--output-last-message",
            str(output_path),
        ]
        if self.config.model:
            command.extend(["--model", self.config.model])
        if session_id:
            command.extend(["resume", session_id, "-"])
        else:
            command.append("-")

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            with self.lock:
                self.current = process
            try:
                stdout, stderr = process.communicate(prompt, timeout=self.config.timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise RuntimeError(f"Codex timed out after {self.config.timeout} seconds")
            if process.returncode != 0:
                detail = stderr.strip() or stdout.strip() or f"exit code {process.returncode}"
                raise RuntimeError(detail[-3500:])
            answer = output_path.read_text(errors="replace").strip()
            return answer, extract_session_id(stdout) or session_id
        finally:
            with self.lock:
                self.current = None
            output_path.unlink(missing_ok=True)


class Bot:
    HELP = """Codex on Telegram

Send any normal message to give Codex a task in the current directory.

/status — show the current setup
/cwd — show the working directory
/cd <path> — switch directory (inside CODEX_WORKSPACE)
/new — start a fresh Codex conversation
/stop — stop the running task
/help — show this message
/id — show the current chat ID

Examples:
Explain this repository
Fix the failing tests and tell me what changed
/cd projects/my-app
Add a health-check endpoint and run the tests"""

    def __init__(self, config: Config, api: TelegramAPI, runner: CodexRunner) -> None:
        self.config = config
        self.api = api
        self.runner = runner
        self.cwd = config.workspace
        self.sessions: dict[pathlib.Path, str] = {}
        self.jobs: queue.Queue[str] = queue.Queue(maxsize=1)
        self.running = threading.Event()
        self.stopping = threading.Event()

    def send(self, text: str) -> None:
        self.api.send(self.config.chat_id, text)

    def handle(self, message: dict[str, Any]) -> None:
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text")
        if not isinstance(text, str):
            if chat_id == self.config.chat_id:
                self.send("Please send text messages; file and voice input are not enabled yet.")
            return
        if chat_id != self.config.chat_id:
            LOG.warning("Ignored message from unauthorized chat ID %s", chat_id)
            return

        command, _, argument = text.strip().partition(" ")
        command = command.split("@", 1)[0].lower()
        argument = argument.strip()
        if command in {"/start", "/help"}:
            self.send(self.HELP)
        elif command == "/id":
            self.send(f"This chat ID is: {chat_id}")
        elif command == "/cwd":
            self.send(str(self.cwd))
        elif command == "/status":
            session = "active" if self.cwd in self.sessions else "new"
            self.send(
                f"Workspace: {self.config.workspace}\n"
                f"Current directory: {self.cwd}\n"
                f"Sandbox: {self.config.sandbox}\n"
                f"Model: {self.config.model or 'Codex default'}\n"
                f"Conversation: {session}\n"
                f"Task running: {'yes' if self.running.is_set() else 'no'}"
            )
        elif command == "/cd":
            if not argument:
                self.send("Usage: /cd <path>")
                return
            try:
                self.cwd = safe_directory(self.config.workspace, argument)
            except ValueError as exc:
                self.send(f"Cannot change directory: {exc}")
                return
            self.send(f"Working directory: {self.cwd}")
        elif command in {"/new", "/reset"}:
            self.sessions.pop(self.cwd, None)
            self.send("Started a fresh Codex conversation in this directory.")
        elif command == "/stop":
            self.send("Stopping the current task..." if self.runner.stop() else "No task is running.")
        elif command.startswith("/"):
            self.send("Unknown command. Use /help to see the available commands.")
        else:
            self.enqueue(text.strip())

    def enqueue(self, prompt: str) -> None:
        if not prompt:
            return
        if self.running.is_set() or not self.jobs.empty():
            self.send("Codex is already working. Use /stop, then send the next task.")
            return
        self.jobs.put(prompt)
        self.send("Working on it…")

    def worker(self) -> None:
        while not self.stopping.is_set():
            try:
                prompt = self.jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            self.running.set()
            cwd = self.cwd
            try:
                answer, session_id = self.runner.run(prompt, cwd, self.sessions.get(cwd))
                if session_id:
                    self.sessions[cwd] = session_id
                self.send(answer)
            except Exception as exc:  # Keep the long-running bot alive after a failed task.
                LOG.exception("Codex task failed")
                self.send(f"Codex failed:\n{exc}")
            finally:
                self.running.clear()
                self.jobs.task_done()

    def run_forever(self) -> None:
        worker = threading.Thread(target=self.worker, name="codex-worker", daemon=True)
        worker.start()
        offset = 0
        LOG.info("Bot started for chat %s in %s", self.config.chat_id, self.config.workspace)
        self.api.call(
            "setMyCommands",
            commands=json.dumps(
                [
                    {"command": "help", "description": "Show commands and examples"},
                    {"command": "status", "description": "Show current setup"},
                    {"command": "cwd", "description": "Show working directory"},
                    {"command": "cd", "description": "Change working directory"},
                    {"command": "new", "description": "Start a fresh conversation"},
                    {"command": "stop", "description": "Stop the running task"},
                    {"command": "id", "description": "Show this chat ID"},
                ]
            ),
        )
        while not self.stopping.is_set():
            try:
                updates = self.api.call("getUpdates", offset=offset, timeout=30) or []
                for update in updates:
                    offset = max(offset, int(update["update_id"]) + 1)
                    message = update.get("message")
                    if isinstance(message, dict):
                        self.handle(message)
            except Exception:
                LOG.exception("Telegram polling failed; retrying")
                time.sleep(3)

    def stop(self) -> None:
        self.stopping.set()
        self.runner.stop()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
    except ConfigError as exc:
        LOG.error("Configuration error: %s", exc)
        return 2
    if shutil.which(config.codex_bin) is None:
        LOG.error("Codex CLI not found: %s. Run ./setup.sh", config.codex_bin)
        return 2

    bot = Bot(config, TelegramAPI(config.token), CodexRunner(config))

    def stop_handler(_signum: int, _frame: Any) -> None:
        bot.stop()

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    bot.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
