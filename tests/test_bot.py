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

    def test_format_duration_uses_largest_exact_unit(self):
        self.assertEqual(bot.format_duration(60), "1h")
        self.assertEqual(bot.format_duration(1440), "1d")


class TelegramMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_splits_long_text(self):
        with patch.object(bot, "telegram", new=AsyncMock(side_effect=[{"message_id": 1}, {"message_id": 2}])) as request:
            sent = await bot.send_message(123, "x" * 4001)

        self.assertEqual([message["message_id"] for message in sent], [1, 2])
        self.assertEqual(len(request.await_args_list[0].args[1]["text"]), 4000)
        self.assertEqual(request.await_args_list[1].args[1]["text"], "x")

    async def test_edit_message_uses_telegram_edit_endpoint(self):
        with patch.object(bot, "telegram", new=AsyncMock(return_value={})) as request:
            await bot.edit_message(123, 456, "progress")

        request.assert_awaited_once_with(
            "editMessageText",
            {"chat_id": 123, "message_id": 456, "text": "progress"},
        )

    async def test_delete_message_uses_telegram_delete_endpoint(self):
        with patch.object(bot, "telegram", new=AsyncMock(return_value=True)) as request:
            await bot.delete_message(123, 456)

        request.assert_awaited_once_with(
            "deleteMessage",
            {"chat_id": 123, "message_id": 456},
        )


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


if __name__ == "__main__":
    unittest.main()
