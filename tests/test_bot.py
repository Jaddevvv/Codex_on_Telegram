import asyncio
import os
import tempfile
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


class CodexStatusTests(unittest.TestCase):
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
