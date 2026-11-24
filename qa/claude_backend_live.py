#!/usr/bin/env python3
"""Live checks for claude_backend.ClaudeCodeSession against the real Claude Code CLI.

This spends real (small) Claude usage, so it is a script, not a unittest:

    python3 qa/claude_backend_live.py [WORK_DIR]

Most checks use claude-haiku-5-5; one short turn uses the defaults
(claude-opus-5-5, medium effort). WORK_DIR (default: a new temporary folder)
becomes the session working directory. Exit code 0 means every check passed.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import claude_backend  # noqa: E402
from claude_backend import ClaudeCodeSession  # noqa: E402

HAIKU = "claude-haiku-5-5"
RESULTS = []


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


class ProgressRecorder:
    """Drain a session's progress queue the way bot.py's reporter does."""

    def __init__(self, session):
        self.session = session
        self.lines = []
        self.task = asyncio.create_task(self._drain())

    async def _drain(self):
        while True:
            line = await self.session.progress_updates.get()
            self.lines.append(line)
            log("   progress:", line.replace("\n", " | ")[:160])

    async def wait_for(self, predicate, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if any(predicate(line) for line in self.lines):
                return True
            await asyncio.sleep(0.2)
        return False

    def stop(self):
        self.task.cancel()


async def check(name, coroutine, timeout=240):
    log(f"=== {name}")
    started = time.time()
    try:
        detail = await asyncio.wait_for(coroutine, timeout)
        RESULTS.append((name, True, f"{time.time() - started:.1f}s {detail or ''}".strip()))
        log(f"PASS {name}: {detail or ''}")
    except Exception as error:
        traceback.print_exc()
        RESULTS.append((name, False, f"{type(error).__name__}: {error}"[:300]))
        log(f"FAIL {name}: {error}")


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def transcript_requests(session_id):
    path = next(claude_backend.CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"), None)
    expect(path is not None, f"no transcript for {session_id}")
    requests = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row.get("type") == "api-request":
            params = row.get("params") or {}
            requests.append((params.get("model"), (params.get("output_config") or {}).get("effort")))
    return requests


async def test_simple(work):
    session = ClaudeCodeSession(cwd=work / "simple", model=HAIKU, effort="low")
    await session.start()
    expect(session.process is None, "CLI must not start before the first turn")
    expect(session.cwd == str(work / "simple"), f"start() changed cwd to {session.cwd}")
    recorder = ProgressRecorder(session)
    try:
        response = await session.run("Reply with exactly: PONG", title_prompt="Say pong to me")
        expect("PONG" in response, f"unexpected response {response!r}")
        expect(session.last_thread_name, "no conversation title was set")
        expect(session.thread_id and session.process is not None, "no live process after the turn")
        status = await session.status_text()
        log("   status:\n" + status)
        expect("Context:" in status and "Subscription limits" in status, "status text incomplete")
        expect("not available" not in session.context_status(), "no context usage after a turn")
        requests = transcript_requests(session.thread_id)
        expect((HAIKU, "low") in requests, f"model/effort not applied: {requests}")
        return f"response={response!r} title={session.last_thread_name!r}"
    finally:
        recorder.stop()
        await session.close()


async def test_tool_progress(work):
    folder = work / "tool"
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("alpha.txt", "beta.txt", "gamma.txt"):
        (folder / name).write_text(name)
    session = ClaudeCodeSession(cwd=work, model=HAIKU, effort="low")
    await session.start()
    await session.new_thread("Tool check", cwd=str(folder))
    recorder = ProgressRecorder(session)
    try:
        response = await session.run(
            "Run `ls` with the Bash tool in the current directory and tell me how many "
            "entries it printed, as a digit."
        )
        expect("3" in response, f"unexpected response {response!r}")
        expect(any(line.startswith("Running: ls") for line in recorder.lines), "no Bash progress line")
        expect(any(line.startswith("Bash completed") for line in recorder.lines), "no Bash result line")
        expect(session.last_thread_name == "Tool check", f"pending name not applied: {session.last_thread_name}")
        return f"response={response!r}; {len(recorder.lines)} progress lines"
    finally:
        recorder.stop()
        await session.close()


async def test_steer(work):
    session = ClaudeCodeSession(cwd=work / "steer", model=HAIKU, effort="low")
    await session.start()
    recorder = ProgressRecorder(session)
    try:
        try:
            await session.steer("nothing is running")
        except RuntimeError:
            pass
        else:
            raise AssertionError("steer() must raise without an active turn")
        task = asyncio.create_task(
            session.run("Use the Bash tool to run `sleep 15`, then reply with just the word done.")
        )
        expect(
            await recorder.wait_for(lambda line: line.startswith("Running: sleep"), 60),
            "the sleep command never started",
        )
        await asyncio.sleep(2)
        await session.steer("Also say BANANA in your final reply.")
        response = await task
        expect("banana" in response.lower(), f"steer was not applied: {response!r}")
        reached = any("reached Claude" in line for line in recorder.lines)
        return f"response={response!r} folded_into_turn={reached}"
    finally:
        recorder.stop()
        await session.close()


async def test_steer_during_final_text(work):
    session = ClaudeCodeSession(cwd=work / "steer2", model=HAIKU, effort="low")
    await session.start()
    recorder = ProgressRecorder(session)
    try:
        task = asyncio.create_task(session.run("Write a 250-word poem about the sea. Do not use tools."))
        await asyncio.sleep(2.5)
        await session.steer("After the poem, add a last line with only the word KIWI.")
        response = await task
        expect("kiwi" in response.lower(), f"queued steer answer missing: {response[-300:]!r}")
        return f"{len(response)} chars, ends {response[-40:]!r}"
    finally:
        recorder.stop()
        await session.close()


async def test_interrupt_and_crash(work):
    session = ClaudeCodeSession(cwd=work / "interrupt", model=HAIKU, effort="low")
    await session.start()
    recorder = ProgressRecorder(session)
    try:
        expect(await session.interrupt() is False, "interrupt() must be False when idle")
        task = asyncio.create_task(
            session.run(
                "Run this exact command with the Bash tool in the foreground (do not use "
                "run_in_background): `for i in $(seq 1 60); do sleep 1; done; echo finished`. "
                "Then reply with the word done."
            )
        )
        expect(
            await recorder.wait_for(lambda line: line.startswith("Running: for i"), 60),
            "the long command never started",
        )
        pid = session.process.pid
        await asyncio.sleep(2)
        started = time.time()
        expect(await session.interrupt() is True, "interrupt() returned False during a turn")
        response = await asyncio.wait_for(task, 30)
        stop_seconds = time.time() - started
        expect("Stopped" in response, f"unexpected interrupted response {response!r}")
        expect(session.process is not None and session.process.pid == pid, "process did not survive")
        follow_up = await session.run("What exact command did I ask you to run? Answer in one line.")
        expect("seq" in follow_up, f"follow-up lost context: {follow_up!r}")

        # Crash: kill the CLI between turns; the next run resumes the session.
        thread_id = session.thread_id
        os.kill(session.process.pid, signal.SIGKILL)
        await asyncio.sleep(1)
        expect(session.process is None, "process exit was not noticed")
        after_crash = await session.run("Repeat the number 60 from that command, digits only.")
        expect("60" in after_crash, f"context lost after crash: {after_crash!r}")
        expect(session.thread_id == thread_id, "session id changed after crash restart")
        return f"stopped in {stop_seconds:.1f}s; follow_up={follow_up!r}; after_crash={after_crash!r}"
    finally:
        recorder.stop()
        await session.close()


async def test_resume_after_close(work):
    session = ClaudeCodeSession(cwd=work / "resume", model=HAIKU, effort="low")
    await session.start()
    try:
        await session.run("Remember the secret word MANGO. Reply with just OK.")
        renamed = await session.set_thread_name("Secret word test")
        expect(renamed is True, "set_thread_name after a turn must apply now")
        compacted = await session.compact_thread()
        log("   compact:", compacted)
        thread_id = session.thread_id
    finally:
        await session.close()
    expect(session.process is None, "close() left the process running")

    other = ClaudeCodeSession(cwd=work / "resume", model=HAIKU, effort="low")
    await other.start()
    try:
        threads = await other.list_threads()
        listed = next((thread for thread in threads if thread["id"] == thread_id), None)
        expect(listed is not None, f"session not listed: {[t['id'] for t in threads]}")
        expect(listed.get("name") == "Secret word test", f"name not listed: {listed}")
        resumed = await other.resume_thread(thread_id)
        confirmation = await other.resume_confirmation(resumed, listed)
        log("   confirmation:", confirmation.replace("\n", " | "))
        expect("Secret word test" in confirmation, "resume confirmation lacks the name")
        answer = await other.run("What is the secret word? One word.")
        expect("MANGO" in answer.upper(), f"resume lost context: {answer!r}")
        expect(other.thread_id == thread_id, "resume changed the session id")
        return f"answer={answer!r}; listed {len(threads)} sessions"
    finally:
        await other.close()


async def test_settings_switch(work):
    session = ClaudeCodeSession(cwd=work / "settings", model=HAIKU, effort="low")
    await session.start()
    try:
        await session.run("Reply with exactly: ONE")
        models = await session.refresh_models()
        ids = [model["id"] for model in models]
        expect("claude-opus-5-5" in ids and HAIKU in ids, f"model list incomplete: {ids}")
        expect("medium" in session.supported_efforts(), "no effort levels")
        pid = session.process.pid
        session.current_effort = "high"
        await session.set_permission_mode("ask")
        reply = await session.run("Reply with exactly: TWO")
        expect("TWO" in reply, f"unexpected {reply!r}")
        expect(session.process.pid == pid, "settings change restarted the process")
        expect(session._applied.get("permission") == "acceptEdits", f"permission not applied: {session._applied}")
        requests = transcript_requests(session.thread_id)
        expect(requests[-1] == (HAIKU, "high"), f"live effort change not applied: {requests}")
        return f"models={ids}; requests={requests}"
    finally:
        await session.close()


async def test_background_output(work):
    session = ClaudeCodeSession(cwd=work / "background", model=HAIKU, effort="low")
    received = []

    async def on_output(text):
        received.append(text)

    session.on_background_output = on_output
    await session.start()
    recorder = ProgressRecorder(session)
    try:
        response = await session.run(
            "First call the Bash tool with run_in_background set to true for the command "
            "`sleep 5; echo bgdone`. Only after that tool call has returned, reply 'started' "
            "without waiting for it. When you are later told it finished, reply with just "
            "the word FINISHED."
        )
        expect(
            any(line.startswith("Running (background)") for line in recorder.lines),
            f"the model did not start a background task: {response!r}",
        )
        end = time.time() + 120
        while not received and time.time() < end:
            await asyncio.sleep(0.5)
        expect(received, f"no background output (first response {response!r})")
        return f"run={response!r}; background={received[0][:80]!r}"
    finally:
        recorder.stop()
        await session.close()


async def test_idle_close_and_cwd_change(work):
    first = work / "idle-a"
    second = work / "idle-b"
    session = ClaudeCodeSession(cwd=first, model=HAIKU, effort="low", idle_timeout=3)
    await session.start()
    try:
        await session.run("Remember the number 4711. Reply with just OK.")
        end = time.time() + 20
        while session.process is not None and time.time() < end:
            await asyncio.sleep(0.5)
        expect(session.process is None, "idle process was not closed")
        session.cwd = str(second)
        answer = await session.run(
            "Which number did I ask you to remember, and what is the current working "
            "directory? Use the Bash tool to run `pwd`. Answer in one line."
        )
        expect("4711" in answer, f"context lost after idle close: {answer!r}")
        expect("idle-b" in answer, f"cwd change not applied: {answer!r}")
        return f"answer={answer!r}"
    finally:
        await session.close()


async def test_opus_defaults(work):
    session = ClaudeCodeSession(cwd=work / "opus")
    expect(session.current_model == "claude-opus-5-5", session.current_model)
    expect(session.current_effort == "medium", session.current_effort)
    expect(session.claude_permission_mode == "bypassPermissions", session.claude_permission_mode)
    await session.start()
    try:
        response = await session.run("Reply with exactly: OK", title_prompt="opus default check")
        expect("OK" in response, f"unexpected {response!r}")
        requests = transcript_requests(session.thread_id)
        expect(("claude-opus-5-5", "medium") in requests, f"defaults not applied: {requests}")
        return f"response={response!r}; requests={requests}"
    finally:
        await session.close()


async def main():
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="claude-backend-qa-"))
    work.mkdir(parents=True, exist_ok=True)
    log("work dir:", work, "cli:", claude_backend.CLAUDE_BIN)
    selected = set(os.environ.get("CLAUDE_QA_ONLY", "").split(",")) - {""}
    tests = [
        ("simple run", test_simple),
        ("tool call progress", test_tool_progress),
        ("steer during tool call", test_steer),
        ("steer during final text", test_steer_during_final_text),
        ("interrupt, follow-up, crash restart", test_interrupt_and_crash),
        ("resume after close", test_resume_after_close),
        ("live model/effort/permission change", test_settings_switch),
        ("background turn output", test_background_output),
        ("idle close, resume, cwd change", test_idle_close_and_cwd_change),
        ("opus 5.5 + medium defaults", test_opus_defaults),
    ]
    for name, test in tests:
        if selected and test.__name__ not in selected:
            continue
        await check(name, test(work))
    print("\nSummary:")
    for name, passed, detail in RESULTS:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}: {detail}")
    return 0 if RESULTS and all(passed for _, passed, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
