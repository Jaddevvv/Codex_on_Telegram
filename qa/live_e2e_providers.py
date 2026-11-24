#!/usr/bin/env python3
"""Live end-to-end check: fake Telegram, REAL Codex and Claude backends via bot.main().

This spends a little real usage, so it is a script, not a unittest:

    python3 qa/live_e2e_providers.py [WORK_DIR]

Flow: drop a .txt file (Codex session, files-first folder) -> /claude ->
"what's in the file?" -> a `sleep 10` turn steered once -> /codex -> one-line
prompt. Claude runs claude-haiku-5-5 at low effort without AI titles; Codex
runs gpt-6.2-sol at low effort. Afterwards the Codex threads are archived and
the Claude transcripts of WORK_DIR are deleted. Exit code 0 means all passed.
"""

import asyncio
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
WORK = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="telegram-e2e-")).resolve()
WORK.mkdir(parents=True, exist_ok=True)
os.environ.update(
    TELEGRAM_BOT_TOKEN="offline-e2e",
    ALLOWED_CHAT_ID="123",
    CODEX_EVENT_LOG=str(WORK / "events.log"),
    CODEX_WORKSPACE=str(WORK / "workspace"),
    TELEGRAM_CONVERSATION_STATE=str(WORK / "conversation_state.json"),
    TELEGRAM_ATTACHMENT_BATCH_SECONDS="1",
    CODEX_DEFAULT_MODEL="gpt-6.2-sol",
    CODEX_DEFAULT_EFFORT="low",
    CLAUDE_DEFAULT_MODEL="claude-haiku-5-5",
    CLAUDE_DEFAULT_EFFORT="low",
    CLAUDE_AI_TITLES="0",
)
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("bot_live_e2e", ROOT / "bot.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

TOPIC = 10
SECRET = "PAPAYA-42"
codex_threads = set()
codex_backends = []
claude_sessions = []
results = []


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    log("PASS" if condition else "FAIL", name, detail if not condition else "")


class RecordingCodex(bot.CodexAppServer):
    def __init__(self):
        super().__init__()
        codex_backends.append(self)

    async def request(self, method, params=None):
        result = await super().request(method, params)
        if method == "thread/start" and isinstance(result, dict) and result.get("thread"):
            if not (params or {}).get("ephemeral"):
                codex_threads.add(result["thread"]["id"])
        return result


class RecordingClaude(bot.claude_backend.ClaudeCodeSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        claude_sessions.append(self)


async def main():
    updates = asyncio.Queue()
    outbox = asyncio.Queue()
    renames = []

    async def telegram(method, values=None, timeout=70):
        if method == "deleteWebhook":
            return True
        if method == "getUpdates":
            return [await updates.get()]
        if method == "getFile":
            return {"file_path": f"documents/{values['file_id']}"}
        if method in {"sendChatAction", "editMessageText", "deleteMessage"}:
            return True
        raise AssertionError(f"unexpected Telegram call {method}")

    def download(file_path, destination):
        destination.write_text(f"Project codename: {SECRET}.\nOwner: the QA harness.\n")

    async def send(chat_id, text, citation_sources=None, message_thread_id=None):
        log("BOT>", text.replace("\n", " | ")[:300])
        await outbox.put(text)
        return [{"message_id": 1}]

    async def send_document(chat_id, path, caption=None, message_thread_id=None):
        log("BOT> [document]", path)

    async def rename(chat_id, topic, name):
        renames.append(name)
        log("BOT> [topic renamed]", name)

    async def guard(sessions):
        await asyncio.Event().wait()

    bot.telegram = telegram
    bot.telegram_file_download = download
    bot.send_message = send
    bot.send_document = send_document
    bot.rename_forum_topic = rename
    bot.monitor_cgroup_oom = guard
    bot.CodexAppServer = RecordingCodex
    bot.claude_backend.ClaudeCodeSession = RecordingClaude

    counter = [0]

    async def user(text=None, document=None):
        counter[0] += 1
        message = {"message_id": counter[0], "chat": {"id": 123}, "message_thread_id": TOPIC,
                   "is_topic_message": True}
        if text is not None:
            message["text"] = text
        if document is not None:
            message["document"] = {"file_id": f"file-{counter[0]}", "file_name": document}
        log("USER>", text or f"[document {document}]")
        await updates.put({"update_id": counter[0], "message": message})

    noise = ("Codex is working", "Claude is working")

    async def expect(predicate, timeout):
        end = time.time() + timeout
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                raise TimeoutError("expected bot message did not arrive")
            text = await asyncio.wait_for(outbox.get(), remaining)
            if not text.startswith(noise) and predicate(text):
                return text

    main_task = asyncio.create_task(bot.main())
    try:
        await user(document="notes.txt")
        ack = await expect(lambda t: True, 180)
        check("files-only drop is acknowledged, no run", ack.startswith("📎 Saved 1 file"), ack)
        folder = Path(codex_backends[-1].cwd)
        check("files-first Codex thread works in the conversation folder",
              (folder / "notes.txt").is_file() and "telegram_sessions" in str(folder), folder)

        await user("/claude")
        switched = await expect(lambda t: True, 120)
        check("/claude switches and keeps folder + pending file",
              "Switched to Claude: claude-haiku-5-5" in switched and str(folder) in switched
              and "1 pending file" in switched, switched)
        check("old Codex app-server closed", codex_backends[0].process.returncode is not None)
        claude = claude_sessions[-1]
        check("Claude conversation cwd is the folder", claude.cwd == str(folder), claude.cwd)

        await user("What's in the file I sent? Answer in one short sentence.")
        answer = await expect(lambda t: True, 240)
        check("Claude read the pending file", SECRET in answer, answer)
        check("topic renamed after the Claude turn", bool(renames) and renames[-1] != "New conversation", renames)

        await user("Use the Bash tool to run `sleep 10`, then reply with the single word DONE.")
        end = time.time() + 60
        while not claude.active_turn_id and time.time() < end:
            await asyncio.sleep(0.2)
        await asyncio.sleep(4)
        await user("Also add the word BANANA at the end of your reply.")
        steer_ack = await expect(lambda t: True, 60)
        check("message during the run steers it", steer_ack.startswith("↪️ Added to the running task"), steer_ack)
        final = await expect(lambda t: True, 240)
        check("steered reply contains DONE and BANANA", "DONE" in final and "BANANA" in final, final)

        await user("/codex")
        back = await expect(lambda t: True, 180)
        check("/codex switches back", "Switched to Codex: gpt-6.2-sol (low thinking)" in back, back)
        check("Claude CLI closed", not claude._process_alive())
        codex = codex_backends[-1]
        check("Codex keeps the files-first folder", codex.cwd == str(folder), codex.cwd)

        await user("Reply with exactly: OK codex")
        reply = await expect(lambda t: True, 240)
        check("Codex answered", "OK" in reply, reply)

        await user("/status")
        status = await expect(lambda t: True, 60)
        check("/status shows provider", status.startswith("Provider: Codex (switch with /claude)"), status[:200])
    finally:
        live = next((b for b in reversed(codex_backends) if b.process and b.process.returncode is None), None)
        if live:
            for thread_id in sorted(codex_threads):
                try:
                    await live.request("thread/archive", {"threadId": thread_id})
                    log("archived Codex thread", thread_id)
                except Exception as error:
                    log("could not archive", thread_id, error)
        main_task.cancel()
        try:
            await main_task
        except asyncio.CancelledError:
            pass
        prefix = bot.claude_backend.project_slug(WORK)
        projects = bot.claude_backend.CLAUDE_PROJECTS_DIR
        if projects.is_dir():
            for directory in projects.iterdir():
                if directory.is_dir() and directory.name.startswith(prefix):
                    shutil.rmtree(directory)
                    log("deleted Claude transcripts", directory)


asyncio.run(main())
failed = [name for name, ok in results if not ok]
log(f"{len(results) - len(failed)}/{len(results)} checks passed", *(["FAILED:"] + failed if failed else []))
sys.exit(1 if failed else 0)
