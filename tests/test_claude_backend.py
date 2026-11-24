"""Offline tests for claude_backend: no Claude Code CLI is started.

The CLI side is simulated by feeding stream-json messages (as observed from
Claude Code 2.1.331) to the session's message handler. qa/claude_backend_live.py
runs the same flows against the real CLI.
"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_CHAT_ID", "123")
os.environ.setdefault(
    "CODEX_EVENT_LOG",
    os.path.join(tempfile.gettempdir(), "codex-telegram-tests.log"),
)

import bot
import claude_backend
from claude_backend import ClaudeCodeSession


class FakeStdin:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(json.loads(data.decode()))

    async def drain(self):
        return None

    def is_closing(self):
        return False

    def close(self):
        return None


def fake_process():
    return SimpleNamespace(returncode=None, stdin=FakeStdin(), pid=4242)


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.session = ClaudeCodeSession(cwd=tempfile.mkdtemp())

    def test_provides_everything_bot_requires(self):
        missing = [name for name in bot.BACKEND_REQUIRED if not hasattr(self.session, name)]
        self.assertEqual(missing, [])
        self.assertEqual(self.session.display_name, "Claude")

    def test_optional_commands_present_or_absent(self):
        supported = {"refresh_models", "supported_efforts", "set_permission_mode", "compact_thread"}
        for command, attribute in bot.OPTIONAL_COMMAND_ATTRIBUTES.items():
            self.assertEqual(hasattr(self.session, attribute), attribute in supported, command)
        for name in ("models", "permission_mode", "permission_summary"):
            self.assertTrue(hasattr(self.session, name), name)

    def test_defaults(self):
        self.assertEqual(self.session.current_model, claude_backend.CLAUDE_DEFAULT_MODEL)
        self.assertEqual(self.session.current_effort, claude_backend.CLAUDE_DEFAULT_EFFORT)
        self.assertIn(self.session.permission_mode, bot.PERMISSION_MODES)
        self.assertIsNone(self.session.process)

    def test_permission_choices_match_bot_numbers(self):
        for key, mode in claude_backend.PERMISSION_MODES.items():
            self.assertEqual(mode["number"], bot.PERMISSION_MODES[key]["number"])
        self.assertEqual(
            claude_backend.normalize_permission_choice("3"), ("full", "bypassPermissions")
        )
        self.assertEqual(claude_backend.normalize_permission_choice("plan"), ("ask", "plan"))
        self.assertEqual(claude_backend.normalize_permission_choice("nope"), (None, None))

    def test_models_have_bot_shape(self):
        models = self.session._convert_models(claude_backend.FALLBACK_MODELS)
        self.assertEqual(models[0]["model"], claude_backend.CLAUDE_DEFAULT_MODEL)
        for model in models:
            self.assertTrue(model["model"])
            efforts = [effort["reasoningEffort"] for effort in model["supportedReasoningEfforts"]]
            self.assertIn(model["defaultReasoningEffort"], efforts)

    def test_tool_descriptions(self):
        self.assertEqual(claude_backend.describe_tool_use("Bash", {"command": "ls -la"}), "Running: ls -la")
        self.assertEqual(claude_backend.describe_tool_use("Read", {"file_path": "/a/b"}), "Reading /a/b")
        plan = claude_backend.describe_tool_use(
            "TodoWrite", {"todos": [{"content": "x", "status": "completed"}, {"content": "y"}]}
        )
        self.assertEqual(plan, "Plan:\n[x] x\n[ ] y")
        self.assertEqual(claude_backend.project_slug("/root/a_b.c"), "-root-a-b-c")

    def test_environment_drops_parent_session(self):
        with patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x"}):
            environment = claude_backend.claude_environment()
        self.assertNotIn("CLAUDECODE", environment)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", environment)
        self.assertEqual(environment["CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS"], "1")


class TurnAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = ClaudeCodeSession(cwd=tempfile.mkdtemp(), idle_timeout=0)
        await self.session.new_thread()
        self.session.needs_auto_thread_name = False
        self.session.process = fake_process()
        self.session._ensure_process = AsyncMock()

    def written(self, kind="user"):
        return [message for message in self.session.process.stdin.messages if message["type"] == kind]

    async def wait_written(self, count, kind="user"):
        for _ in range(200):
            if len(self.written(kind)) >= count:
                return self.written(kind)
            await asyncio.sleep(0)
        self.fail(f"expected {count} {kind} messages, got {self.written(kind)}")

    async def feed(self, *messages):
        for message in messages:
            await self.session._handle_message(message)

    def result(self, uuids, text, **extra):
        message = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "user_message_uuids": uuids,
            "total_cost_usd": 0.01,
            "usage": {"iterations": [{"input_tokens": 10, "cache_read_input_tokens": 90}]},
            "modelUsage": {"m": {"contextWindow": 1000}},
        }
        message.update(extra)
        return message

    async def test_run_returns_result_text_and_progress(self):
        task = asyncio.create_task(self.session.run("hi"))
        (prompt,) = await self.wait_written(1)
        self.assertTrue(self.session.active_turn_id)
        await self.feed(
            {"type": "command_lifecycle", "command_uuid": prompt["uuid"], "state": "started"},
            {
                "type": "assistant",
                "parent_tool_use_id": None,
                "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a\nb"}]},
            },
            self.result([prompt["uuid"]], "PONG"),
        )
        self.assertEqual(await task, "PONG")
        self.assertIsNone(self.session.active_turn_id)
        lines = []
        while not self.session.progress_updates.empty():
            lines.append(self.session.progress_updates.get_nowait())
        self.assertIn("Running: ls", lines)
        self.assertIn("Bash completed\na\nb", lines)
        self.assertIn("100 / 1,000", self.session.context_status())

    async def test_steer_folded_into_turn(self):
        with self.assertRaises(RuntimeError):
            await self.session.steer("idle")
        task = asyncio.create_task(self.session.run("long task"))
        (prompt,) = await self.wait_written(1)
        await self.session.steer("also this")
        steer = self.written()[1]
        self.assertEqual(steer["message"]["content"], "also this")
        await self.feed(self.result([prompt["uuid"], steer["uuid"]], "done BANANA"))
        self.assertEqual(await task, "done BANANA")

    async def test_steer_run_as_following_turn(self):
        task = asyncio.create_task(self.session.run("poem"))
        (prompt,) = await self.wait_written(1)
        await self.session.steer("add KIWI")
        steer = self.written()[1]
        await self.feed(self.result([prompt["uuid"]], "the poem"))
        self.assertFalse(task.done())
        await self.feed(
            {"type": "system", "subtype": "session_state_changed", "state": "running"},
            self.result([steer["uuid"]], "KIWI"),
        )
        self.assertEqual(await task, "the poem\n\nKIWI")

    async def test_interrupt_returns_stopped_text(self):
        self.assertFalse(await self.session.interrupt())
        task = asyncio.create_task(self.session.run("sleep"))
        (prompt,) = await self.wait_written(1)
        interrupt = asyncio.create_task(self.session.interrupt())
        (request,) = await self.wait_written(1, "control_request")
        self.assertEqual(request["request"], {"subtype": "interrupt", "cancel_queued": True})
        await self.feed(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": request["request_id"], "response": {}},
            }
        )
        self.assertTrue(await interrupt)
        await self.feed(
            self.result([prompt["uuid"]], None, subtype="error_during_execution", is_error=True),
            {"type": "system", "subtype": "session_state_changed", "state": "idle"},
        )
        self.assertIn("Stopped", await task)

    async def test_error_result_raises(self):
        task = asyncio.create_task(self.session.run("hi"))
        (prompt,) = await self.wait_written(1)
        await self.feed(self.result([prompt["uuid"]], "API Error: 500", is_error=True))
        with self.assertRaisesRegex(RuntimeError, "API Error: 500"):
            await task

    async def test_process_exit_fails_run_with_resume_hint(self):
        task = asyncio.create_task(self.session.run("hi"))
        await self.wait_written(1)
        self.session._finish_after_exit(1)
        with self.assertRaisesRegex(RuntimeError, "resumes the conversation"):
            await task

    async def test_turn_started_by_cli_goes_to_callback(self):
        received = []

        async def on_output(text):
            received.append(text)

        self.session.on_background_output = on_output
        self.session._uuid_results_seen = True
        await self.feed(
            {"type": "system", "subtype": "session_state_changed", "state": "running"},
        )
        self.assertEqual(self.session.active_turn_id, "background")
        await self.feed(
            self.result([], "FINISHED"),
            {"type": "system", "subtype": "session_state_changed", "state": "idle"},
        )
        await asyncio.sleep(0)
        self.assertEqual(received, ["FINISHED"])
        self.assertIsNone(self.session.active_turn_id)

    async def test_cli_permission_prompt_is_refused(self):
        await self.feed(
            {
                "type": "control_request",
                "request_id": "r1",
                "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}},
            }
        )
        (response,) = self.written("control_response")
        self.assertEqual(response["response"]["response"]["behavior"], "deny")


if __name__ == "__main__":
    unittest.main()
