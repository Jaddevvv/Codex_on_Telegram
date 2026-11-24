import asyncio
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_CHAT_ID", "123")
os.environ.setdefault(
    "CODEX_EVENT_LOG",
    os.path.join(tempfile.gettempdir(), "codex-telegram-tests.log"),
)

import bot


class FormattingTests(unittest.TestCase):
    def test_normalized_key_ignores_case_and_punctuation(self):
        self.assertEqual(bot.normalized_key("Context-Window_Tokens"), "contextwindowtokens")

    def test_find_value_searches_nested_collections(self):
        data = {"outer": [{"contextWindowTokens": 200_000}]}
        self.assertEqual(bot.find_value(data, ["context_window_tokens"]), 200_000)

    def test_telegram_html_renders_bold_and_escapes_raw_html(self):
        rendered = bot.telegram_html("**Bold** and <script>alert(1)</script>")

        self.assertEqual(
            rendered,
            "<b>Bold</b> and &lt;script&gt;alert(1)&lt;/script&gt;",
        )

    def test_telegram_html_removes_openai_citation_markers(self):
        rendered = bot.telegram_html(
            "Answer. citeturn1search0turn2search12\n\nNext paragraph."
        )

        self.assertEqual(rendered, "Answer.\n\nNext paragraph.")

    def test_telegram_html_turns_known_citations_into_links(self):
        rendered = bot.telegram_html(
            "See this. citeturn2search12",
            {
                "turn2search12": {
                    "title": "Official simulator",
                    "url": "https://example.com/simulator?a=1&b=2",
                }
            },
        )

        self.assertEqual(
            rendered,
            'See this. <a href="https://example.com/simulator?a=1&amp;b=2">Official simulator</a>',
        )

    def test_format_duration_uses_largest_exact_unit(self):
        self.assertEqual(bot.format_duration(60), "1h")
        self.assertEqual(bot.format_duration(1440), "1d")

    def test_permission_choices_use_the_three_cli_numbers(self):
        self.assertEqual(bot.normalize_permission_mode("1"), "ask")
        self.assertEqual(bot.normalize_permission_mode("2"), "approve")
        self.assertEqual(bot.normalize_permission_mode("3"), "full")

    def test_numbered_choice_returns_the_selected_item(self):
        self.assertEqual(bot.numbered_choice("2", ["low", "medium", "high"]), "medium")
        self.assertIsNone(bot.numbered_choice("4", ["low", "medium", "high"]))

    def test_telegram_session_key_separates_topics_and_keeps_regular_chat(self):
        regular = {"chat": {"id": 123}}
        topic_a = {"chat": {"id": 123}, "message_thread_id": 456}
        topic_b = {"chat": {"id": 123}, "message_thread_id": 789}
        general_topic = {"chat": {"id": 123}, "is_topic_message": True}

        self.assertEqual(bot.telegram_session_key(regular), (123, None))
        self.assertEqual(bot.telegram_session_key(topic_a), (123, 456))
        self.assertNotEqual(
            bot.telegram_session_key(topic_a),
            bot.telegram_session_key(topic_b),
        )
        self.assertEqual(bot.telegram_session_key(general_topic), (123, 1))

    def test_format_goal_handles_empty_goal(self):
        self.assertIn("No goal is set", bot.format_goal(None))

    def test_fast_mode_is_off_by_default_and_sets_service_tier_when_enabled(self):
        server = bot.CodexAppServer()

        self.assertFalse(server.fast_mode)
        self.assertIsNone(server.turn_start_params("hello")["serviceTier"])

        server.fast_mode = True

        self.assertEqual(
            server.turn_start_params("hello")["serviceTier"],
            "fast",
        )


class TelegramMessageTests(unittest.IsolatedAsyncioTestCase):
    def test_changed_attachment_files_returns_only_new_or_modified_files(self):
        with tempfile.TemporaryDirectory() as workspace:
            attachment_folder = bot.Path(workspace) / "outbox"
            attachment_folder.mkdir()
            existing = attachment_folder / "existing.txt"
            existing.write_text("before")

            before = bot.attachment_file_snapshot(attachment_folder)
            existing.write_text("after")
            new_file = attachment_folder / "new.docx"
            new_file.write_bytes(b"document")
            changed = bot.changed_attachment_files(before, attachment_folder)

        self.assertEqual(changed, [existing, new_file])
        self.assertEqual(bot.attachment_file_snapshot(None), {})

    def test_conversation_folders_are_scoped_per_topic_and_unique(self):
        now = bot.datetime(2026, 11, 24, 13, 5, 1)
        with tempfile.TemporaryDirectory() as workspace:
            with patch.object(bot, "WORKSPACE", workspace):
                regular = bot.new_conversation_folder((-100123, None), now=now)
                topic = bot.new_conversation_folder((-100123, 456), "Research plan!", now=now)
                again = bot.new_conversation_folder((-100123, 456), "Research plan!", now=now)

        root = bot.Path(workspace) / "telegram_sessions"
        self.assertEqual(regular, root / "chat-100123" / "20261124-130501")
        self.assertEqual(topic, root / "topic-456" / "20261124-130501-research-plan")
        self.assertEqual(again, root / "topic-456" / "20261124-130501-research-plan-2")
        self.assertFalse(regular.exists())
        self.assertEqual(bot.outbox_folder(topic), topic / "outbox")

    def test_thread_conversation_state_round_trips(self):
        with tempfile.TemporaryDirectory() as directory:
            state = bot.Path(directory) / "state.json"
            with patch.object(bot, "CONVERSATION_STATE_FILE", state):
                self.assertIsNone(bot.thread_conversation_record("t1"))
                bot.remember_thread_conversation("t1", "/w/telegram_sessions/topic-1/a", "/w")
                record = bot.thread_conversation_record("t1")

        self.assertEqual(record["folder"], "/w/telegram_sessions/topic-1/a")
        self.assertEqual(record["cwd"], "/w")

    def test_telegram_upload_document_streams_any_file_as_multipart(self):
        class FakeResponse:
            status = 200

            def read(self):
                return b'{"ok":true,"result":{"message_id":7}}'

        class FakeConnection:
            def __init__(self):
                self.target = None
                self.headers = {}
                self.sent = []

            def putrequest(self, method, target):
                self.target = (method, target)

            def putheader(self, name, value):
                self.headers[name] = value

            def endheaders(self):
                pass

            def send(self, data):
                self.sent.append(data)

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as workspace:
            path = bot.Path(workspace) / "report.docx"
            path.write_bytes(b"word document bytes")
            connection = FakeConnection()
            with patch.object(bot.http.client, "HTTPSConnection", return_value=connection):
                result = bot.telegram_upload_document(
                    123,
                    path,
                    "Generated report",
                    message_thread_id=456,
                )

        body = b"".join(connection.sent)
        self.assertEqual(result["message_id"], 7)
        self.assertEqual(connection.target[0], "POST")
        self.assertTrue(connection.target[1].endswith("/sendDocument"))
        self.assertIn(b'name="document"; filename="report.docx"', body)
        self.assertIn(b"Generated report", body)
        self.assertIn(b'name="message_thread_id"\r\n\r\n456', body)
        self.assertIn(b"word document bytes", body)

    def test_attachment_specs_support_documents_photos_and_media(self):
        specs = bot.attachment_specs(
            {
                "document": {"file_id": "doc-id", "file_name": "report.csv"},
                "photo": [
                    {"file_id": "small-photo", "width": 320, "height": 240},
                    {"file_id": "large-photo", "width": 1280, "height": 960},
                ],
                "voice": {"file_id": "voice-id"},
            }
        )

        self.assertEqual(
            specs,
            [
                {"file_id": "doc-id", "name": "report.csv", "kind": "document"},
                {"file_id": "large-photo", "name": "photo.jpg", "kind": "photo"},
                {"file_id": "voice-id", "name": "voice.ogg", "kind": "voice"},
            ],
        )

    def test_prompt_with_attachments_lists_exact_absolute_paths(self):
        first = bot.Path("/w/telegram_sessions/topic-1/a/fresh report.csv")
        second = bot.Path("/w/telegram_sessions/topic-1/a/notes.docx")

        prompt = bot.prompt_with_attachments("Analyze this", [first, second])

        self.assertTrue(prompt.startswith("Analyze this\n\n"))
        self.assertIn("2 files", prompt)
        self.assertIn(f"- fresh report.csv: {first}", prompt)
        self.assertIn(f"- notes.docx: {second}", prompt)
        self.assertNotIn("minute", prompt)
        self.assertEqual(bot.prompt_with_attachments("Just text", []), "Just text")

    def test_readable_attachment_names_keep_letters_and_stay_inside_folder(self):
        self.assertEqual(bot.readable_attachment_name("../Rapport été.docx", "x"), "Rapport été.docx")
        self.assertEqual(bot.readable_attachment_name("a/b\\c<d>.txt", "x"), "c_d_.txt")
        self.assertEqual(bot.readable_attachment_name("..", "fallback"), "fallback")

    async def test_save_attachments_creates_attachment_folder_and_downloads_files(self):
        message = {
            "message_id": 42,
            "document": {"file_id": "doc-id", "file_name": "../report.csv"},
        }

        with tempfile.TemporaryDirectory() as workspace:
            folder = bot.Path(workspace) / "telegram_sessions" / "topic-1" / "conv"

            def fake_download(file_path, destination):
                self.assertEqual(file_path, "documents/report.csv")
                destination.write_bytes(b"a,b\n1,2\n")

            async def fake_to_thread(function, *args, **kwargs):
                return function(*args, **kwargs)

            with (
                patch.object(bot, "WORKSPACE", workspace),
                patch.object(
                    bot,
                    "telegram",
                    new=AsyncMock(return_value={"file_path": "documents/report.csv"}),
                ) as telegram_request,
                patch.object(bot, "telegram_file_download", side_effect=fake_download),
                patch.object(
                    bot.asyncio,
                    "to_thread",
                    new=fake_to_thread,
                ),
            ):
                saved = await bot.save_attachments(message, folder)
                again = await bot.save_attachments(message, folder)

            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0], folder / "report.csv")
            self.assertEqual(saved[0].read_bytes(), b"a,b\n1,2\n")
            self.assertEqual(again[0], folder / "report (2).csv")
            telegram_request.assert_awaited_with("getFile", {"file_id": "doc-id"})

    def test_split_message_hard_splits_without_separators(self):
        text = "x" * 10_001

        chunks = bot.split_message(text, limit=4000)

        self.assertEqual([len(chunk) for chunk in chunks], [4000, 4000, 2001])
        self.assertEqual("".join(chunks), text)

    def test_split_message_rejects_invalid_limit(self):
        with self.assertRaises(ValueError):
            bot.split_message("text", limit=0)

    async def test_send_message_splits_long_text(self):
        with patch.object(bot, "telegram", new=AsyncMock(side_effect=[{"message_id": 1}, {"message_id": 2}])) as request:
            sent = await bot.send_message(123, "x" * 4001)

        self.assertEqual([message["message_id"] for message in sent], [1, 2])
        self.assertEqual(len(request.await_args_list[0].args[1]["text"]), 4000)
        self.assertEqual(request.await_args_list[0].args[1]["parse_mode"], "HTML")
        self.assertEqual(request.await_args_list[1].args[1]["text"], "x")

    async def test_send_message_routes_to_topic(self):
        with patch.object(bot, "telegram", new=AsyncMock(return_value={"message_id": 1})) as request:
            await bot.send_message(123, "hello", message_thread_id=456)

        self.assertEqual(
            request.await_args.args,
            ("sendMessage", {
                "chat_id": 123,
                "text": "hello",
                "parse_mode": "HTML",
                "message_thread_id": 456,
            }),
        )

    async def test_typing_indicator_routes_to_topic(self):
        called = asyncio.Event()

        async def fake_telegram(method, values):
            self.assertEqual(method, "sendChatAction")
            self.assertEqual(values["message_thread_id"], 456)
            called.set()

        with patch.object(bot, "telegram", new=fake_telegram):
            task = asyncio.create_task(bot.typing_loop(123, 456))
            await asyncio.wait_for(called.wait(), timeout=1)
            task.cancel()
            await task

    async def test_edit_message_uses_telegram_edit_endpoint(self):
        with patch.object(bot, "telegram", new=AsyncMock(return_value={})) as request:
            await bot.edit_message(123, 456, "**progress** citeturn2search12")

        request.assert_awaited_once_with(
            "editMessageText",
            {
                "chat_id": 123,
                "message_id": 456,
                "text": "<b>progress</b>",
                "parse_mode": "HTML",
            },
        )

    async def test_delete_message_uses_telegram_delete_endpoint(self):
        with patch.object(bot, "telegram", new=AsyncMock(return_value=True)) as request:
            await bot.delete_message(123, 456)

        request.assert_awaited_once_with(
            "deleteMessage",
            {"chat_id": 123, "message_id": 456},
        )

    async def test_cleanup_progress_message_awaits_cancellation_and_deletes(self):
        progress_task = asyncio.create_task(asyncio.sleep(60))
        with patch.object(bot, "delete_message", new=AsyncMock()) as delete:
            await bot.cleanup_progress_message(progress_task, 123, 456)

        self.assertTrue(progress_task.done())
        delete.assert_awaited_once_with(123, 456)

    async def test_cleanup_progress_message_is_bounded(self):
        async def hanging_delete(chat_id, message_id):
            await asyncio.sleep(60)

        with (
            patch.object(bot, "delete_message", new=hanging_delete),
            patch.object(bot, "TELEGRAM_CLEANUP_TIMEOUT", 0.01),
        ):
            deleted = await bot.cleanup_progress_message(None, 123, 456)

        self.assertFalse(deleted)

    async def test_completed_turn_stops_typing_before_final_response(self):
        events = []

        async def fake_typing(chat_id):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("typing-stopped")
                raise

        async def fake_run(prompt, title_prompt=None):
            await asyncio.sleep(0)
            return "completed response"

        async def fake_send(chat_id, text, citation_sources=None):
            events.append(text)
            return [{"message_id": 1}]

        codex = SimpleNamespace(
            progress_updates=asyncio.Queue(),
            citation_sources={},
            run=fake_run,
        )
        with (
            patch.object(bot, "typing_loop", new=fake_typing),
            patch.object(bot, "send_message", new=fake_send),
        ):
            await bot.run_prompt_with_progress(codex, "finish this", 123)

        self.assertEqual(events, ["typing-stopped", "completed response"])

    async def test_final_response_is_sent_when_progress_cleanup_hangs(self):
        events = []

        async def fake_typing(chat_id):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        async def fake_run(prompt, title_prompt=None):
            codex.progress_updates.put_nowait("started work")
            await asyncio.sleep(0.9)
            return "completed response"

        async def fake_send(chat_id, text, citation_sources=None):
            events.append(text)
            return [{"message_id": 99}]

        async def hanging_delete(chat_id, message_id):
            await asyncio.sleep(60)

        codex = SimpleNamespace(
            progress_updates=asyncio.Queue(),
            citation_sources={},
            run=fake_run,
        )
        with (
            patch.object(bot, "typing_loop", new=fake_typing),
            patch.object(bot, "send_message", new=fake_send),
            patch.object(bot, "delete_message", new=hanging_delete),
            patch.object(bot, "TELEGRAM_CLEANUP_TIMEOUT", 0.01),
        ):
            await bot.run_prompt_with_progress(codex, "finish this", 123)

        self.assertEqual(events[-1], "completed response")

    async def test_files_created_in_attachment_are_sent_after_final_response(self):
        events = []
        documents = []

        async def fake_typing(chat_id):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        async def fake_run(prompt, title_prompt=None):
            events.append(prompt)
            output = bot.Path(workspace) / "outbox" / "result.docx"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"generated document")
            return "created the report"

        async def fake_send_message(chat_id, text, citation_sources=None):
            events.append(text)
            return [{"message_id": 1}]

        async def fake_send_document(chat_id, path, caption=None):
            documents.append((chat_id, path, caption))

        codex = SimpleNamespace(
            progress_updates=asyncio.Queue(),
            citation_sources={},
            run=fake_run,
        )
        with tempfile.TemporaryDirectory() as workspace:
            with (
                patch.object(bot, "WORKSPACE", workspace),
                patch.object(bot, "typing_loop", new=fake_typing),
                patch.object(bot, "send_message", new=fake_send_message),
                patch.object(bot, "send_document", new=fake_send_document),
            ):
                await bot.run_prompt_with_progress(
                    codex,
                    "make a report",
                    123,
                    output_folder=bot.Path(workspace) / "outbox",
                )

        self.assertIn(bot.outbound_file_prompt(bot.Path(workspace) / "outbox"), events[0])
        self.assertEqual(events[1], "created the report")
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0][0], 123)
        self.assertEqual(documents[0][1].name, "result.docx")
        self.assertEqual(documents[0][2], "Generated file: result.docx")

    async def test_thread_turn_routes_reply_and_generated_file_to_topic(self):
        messages = []
        documents = []
        prompts = []

        async def fake_typing(chat_id, message_thread_id=None):
            self.assertEqual(message_thread_id, 456)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise

        async def fake_run(prompt, title_prompt=None):
            prompts.append(prompt)
            output = bot.Path(workspace) / "telegram_sessions" / "topic-456" / "c" / "outbox" / "topic-result.txt"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("topic file")
            return "topic response"

        async def fake_send_message(chat_id, text, citation_sources=None, message_thread_id=None):
            messages.append((chat_id, text, message_thread_id))
            return [{"message_id": 1}]

        async def fake_send_document(chat_id, path, caption=None, message_thread_id=None):
            documents.append((chat_id, path, caption, message_thread_id))

        codex = SimpleNamespace(
            last_thread_name=None,
            progress_updates=asyncio.Queue(),
            citation_sources={},
            run=fake_run,
        )
        with tempfile.TemporaryDirectory() as workspace:
            with (
                patch.object(bot, "WORKSPACE", workspace),
                patch.object(bot, "typing_loop", new=fake_typing),
                patch.object(bot, "send_message", new=fake_send_message),
                patch.object(bot, "send_document", new=fake_send_document),
            ):
                await bot.run_prompt_with_progress(
                    codex,
                    "do work",
                    123,
                    message_thread_id=456,
                    output_folder=bot.Path(workspace) / "telegram_sessions" / "topic-456" / "c" / "outbox",
                )

        self.assertIn("telegram_sessions/topic-456/c/outbox/", prompts[0])
        self.assertEqual(messages, [(123, "topic response", 456)])
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0][0], 123)
        self.assertEqual(documents[0][1].name, "topic-result.txt")
        self.assertEqual(documents[0][3], 456)


class CodexStatusTests(unittest.TestCase):
    def test_usage_is_a_status_alias(self):
        self.assertEqual(bot.STATUS_COMMANDS, {"/status", "/debug", "/usage"})
    def test_memory_snapshot_reads_cgroup_limits_and_events(self):
        with tempfile.TemporaryDirectory() as directory:
            cgroup = bot.Path(directory)
            (cgroup / "memory.current").write_text("123456789\n")
            (cgroup / "memory.high").write_text("1073741824\n")
            (cgroup / "memory.max").write_text("1610612736\n")
            (cgroup / "memory.swap.max").write_text("0\n")
            (cgroup / "memory.events").write_text("high 4\nmax 2\noom 1\noom_kill 1\n")

            snapshot = bot.read_memory_snapshot(cgroup)

        self.assertEqual(snapshot["memory.current"], 123456789)
        self.assertEqual(snapshot["memory.max"], 1610612736)
        self.assertEqual(snapshot["memory.swap.max"], 0)
        self.assertEqual(snapshot["events"]["oom_kill"], 1)
        self.assertIn("118 MiB", bot.format_bytes(snapshot["memory.current"]))

    def test_context_status_reports_used_and_remaining_percent(self):
        server = bot.CodexAppServer()
        server.token_usage = {
            "modelContextWindow": 200_000,
            "lastTokenUsage": {"totalTokens": 50_000},
        }

        status = server.context_status()

        self.assertIn("25.0%", status)
        self.assertIn("75.0%", status)


class CodexThreadCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_model_effort_and_permissions_are_selected(self):
        server = bot.CodexAppServer()
        server.request = AsyncMock(
            return_value={
                "data": [
                    {
                        "model": bot.DEFAULT_MODEL,
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low"},
                            {"reasoningEffort": bot.DEFAULT_REASONING_EFFORT},
                        ],
                    },
                    {"model": "other-model", "isDefault": True},
                ]
            }
        )

        await server.refresh_models()

        self.assertEqual(server.current_model, bot.DEFAULT_MODEL)
        self.assertEqual(server.current_effort, bot.DEFAULT_REASONING_EFFORT)
        self.assertEqual(server.permission_mode, "full")
        self.assertEqual(
            server.thread_permission_params(),
            {"approvalPolicy": "never", "sandbox": "danger-full-access"},
        )

    async def test_full_permission_mode_updates_thread_settings(self):
        server = bot.CodexAppServer()
        server.thread_id = "thread-1"
        server.request = AsyncMock(return_value={})

        await server.set_permission_mode("full")

        server.request.assert_awaited_once_with(
            "thread/settings/update",
            {
                "permissions": None,
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            },
        )
        self.assertEqual(server.permission_summary(), "3. Full Access")

    async def test_compact_and_goal_use_current_thread(self):
        server = bot.CodexAppServer()
        server.thread_id = "thread-1"
        server.request = AsyncMock(
            side_effect=[{}, {"goal": {"objective": "ship", "status": "active"}}, {"goal": {"objective": "ship"}}, {"cleared": True}]
        )

        await server.compact_thread()
        goal = await server.get_goal()
        await server.set_goal("ship")
        await server.clear_goal()

        self.assertEqual(goal["objective"], "ship")
        self.assertEqual(
            [call.args[0] for call in server.request.await_args_list],
            ["thread/compact/start", "thread/goal/get", "thread/goal/set", "thread/goal/clear"],
        )

    async def test_steer_targets_the_active_main_turn(self):
        server = bot.CodexAppServer()
        server.thread_id = "thread-1"
        server.request = AsyncMock(return_value={"turnId": "turn-9"})

        with self.assertRaises(RuntimeError):
            await server.steer("too early")
        server.active_turn_id = "turn-title"
        server.active_turn_thread_id = "title-thread"
        with self.assertRaises(RuntimeError):
            await server.steer("not the title turn")
        server.request.assert_not_awaited()

        server.active_turn_id = "turn-9"
        server.active_turn_thread_id = "thread-1"
        self.assertEqual(await server.steer("also do X"), "turn-9")
        server.request.assert_awaited_once_with(
            "turn/steer",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "also do X"}],
                "expectedTurnId": "turn-9",
            },
        )

    async def test_new_thread_uses_cwd_and_reuses_untouched_thread(self):
        server = bot.CodexAppServer()
        server.current_model = "m"
        server.request = AsyncMock(
            side_effect=[{"thread": {"id": "t1"}}, {"thread": {"id": "t2"}}]
        )

        await server.new_thread()
        await server.new_thread("Named")  # untouched, same cwd: reused
        self.assertEqual((server.thread_id, server.pending_thread_name), ("t1", "Named"))
        await server.new_thread("Named", cwd="/w/telegram_sessions/topic-1/c")

        self.assertEqual(server.request.await_count, 2)
        self.assertEqual(server.request.await_args_list[0].args[1]["cwd"], bot.WORKSPACE)
        self.assertEqual(
            server.request.await_args_list[1].args[1]["cwd"], "/w/telegram_sessions/topic-1/c"
        )
        self.assertEqual(server.thread_id, "t2")
        self.assertEqual(server.turn_start_params("hi")["cwd"], "/w/telegram_sessions/topic-1/c")


if __name__ == "__main__":
    unittest.main()
