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
            attachment_folder = bot.Path(workspace) / "attachment"
            attachment_folder.mkdir()
            existing = attachment_folder / "existing.txt"
            existing.write_text("before")

            with patch.object(bot, "WORKSPACE", workspace):
                before = bot.attachment_file_snapshot()
                existing.write_text("after")
                new_file = attachment_folder / "new.docx"
                new_file.write_bytes(b"document")
                changed = bot.changed_attachment_files(before)

        self.assertEqual(changed, [existing, new_file])

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
                result = bot.telegram_upload_document(123, path, "Generated report")

        body = b"".join(connection.sent)
        self.assertEqual(result["message_id"], 7)
        self.assertEqual(connection.target[0], "POST")
        self.assertTrue(connection.target[1].endswith("/sendDocument"))
        self.assertIn(b'name="document"; filename="report.docx"', body)
        self.assertIn(b"Generated report", body)
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

    def test_prompt_with_attachments_names_fresh_files_and_ignores_older_files(self):
        with tempfile.TemporaryDirectory() as workspace:
            workspace_path = bot.Path(workspace)
            attachment_path = workspace_path / "attachment" / "fresh report.csv"

            with patch.object(bot, "WORKSPACE", workspace):
                prompt = bot.prompt_with_attachments("Analyze this", [attachment_path])

        self.assertIn("Analyze this", prompt)
        self.assertIn(
            "i have sent you attachments in the folder attachment that have been dropped less than a minute ago.",
            prompt,
        )
        self.assertIn("attachment/fresh report.csv", prompt)
        self.assertIn("ignore older files in that folder", prompt)

    async def test_save_attachments_creates_attachment_folder_and_downloads_files(self):
        message = {
            "message_id": 42,
            "document": {"file_id": "doc-id", "file_name": "../report.csv"},
        }

        with tempfile.TemporaryDirectory() as workspace:
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
                saved = await bot.save_attachments(message)

            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].parent, bot.Path(workspace) / "attachment")
            self.assertTrue(saved[0].name.endswith("_report.csv"))
            self.assertEqual(saved[0].read_bytes(), b"a,b\n1,2\n")
            telegram_request.assert_awaited_once_with("getFile", {"file_id": "doc-id"})

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

        async def fake_run(prompt):
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

        async def fake_run(prompt):
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

        async def fake_run(prompt):
            events.append(prompt)
            output = bot.Path(workspace) / "attachment" / "result.docx"
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
                await bot.run_prompt_with_progress(codex, "make a report", 123)

        self.assertIn(bot.OUTBOUND_FILE_PROMPT, events[0])
        self.assertEqual(events[1], "created the report")
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0][0], 123)
        self.assertEqual(documents[0][1].name, "result.docx")
        self.assertEqual(documents[0][2], "Generated file: result.docx")


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
                        "model": "gpt-5.6-luna",
                        "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
                    },
                    {"model": "other-model", "isDefault": True},
                ]
            }
        )

        await server.refresh_models()

        self.assertEqual(server.current_model, "gpt-5.6-luna")
        self.assertEqual(server.current_effort, "xhigh")
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


if __name__ == "__main__":
    unittest.main()
