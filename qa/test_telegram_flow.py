"""Offline message-flow QA. Set QA_BOT_PATH to test a specific bridge file."""
import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['TELEGRAM_BOT_TOKEN'] = 'offline-test-token'
os.environ['ALLOWED_CHAT_ID'] = '123'
os.environ['CODEX_EVENT_LOG'] = str(Path(tempfile.gettempdir()) / 'telegram-qa-events.log')
path = Path(os.environ.get('QA_BOT_PATH', Path(__file__).resolve().parents[1] / 'bot.py'))
spec = importlib.util.spec_from_file_location('telegram_qa_target', path)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


def update(n, text=None, topic=10, **extra):
    message = {'chat': {'id': 123}, 'message_thread_id': topic, 'is_topic_message': True, **extra}
    if text is not None:
        message['text'] = text
    return {'update_id': n, 'message': message}


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.updates = asyncio.Queue()
        self.messages = asyncio.Queue()
        self.start_gate = asyncio.Event()
        self.start_gate.set()
        self.renames = []
        self.instances = []
        owner = self

        class FakeCodex:
            def __init__(self):
                self.thread_id = 'fresh-thread'
                self.thread_has_completed_turn = False
                self.pending_thread_name = None
                self.current_model = 'test-model'
                self.current_effort = 'medium'
                owner.instances.append(self)
            async def start(self):
                await owner.start_gate.wait()
            async def close(self):
                pass
            def configure_new_thread_state(self, name):
                self.pending_thread_name = name
            async def new_thread(self, name=None, cwd=None):
                self.pending_thread_name = name
                self.cwd = cwd
            async def status_text(self):
                return 'status ready'

        async def telegram(method, values=None, timeout=70):
            if method == 'deleteWebhook':
                return True
            if method == 'getUpdates':
                return [await self.updates.get()]
            raise AssertionError(f'unexpected Telegram call: {method}')
        async def send(chat_id, text, citation_sources=None, message_thread_id=None):
            await self.messages.put((message_thread_id, text))
        async def rename(chat_id, topic, name):
            self.renames.append((topic, name))
        async def guard(sessions):
            await asyncio.Event().wait()
        self.patches = [patch.object(bot, 'telegram', telegram), patch.object(bot, 'send_message', send),
                        patch.object(bot, 'rename_forum_topic', rename), patch.object(bot, 'monitor_cgroup_oom', guard),
                        patch.object(bot, 'CodexAppServer', FakeCodex)]
        for p in self.patches:
            p.start()
        self.main = asyncio.create_task(bot.main())

    async def asyncTearDown(self):
        self.start_gate.set()
        self.main.cancel()
        try:
            await self.main
        except asyncio.CancelledError:
            pass
        # Allow any per-topic dispatch tasks to finish before removing mocks.
        await asyncio.sleep(0.02)
        for p in reversed(self.patches):
            p.stop()

    async def receive(self):
        return await asyncio.wait_for(self.messages.get(), 0.5)

    async def test_new_acknowledges_before_slow_start_and_completes(self):
        self.start_gate.clear()
        await self.updates.put(update(1, '/new'))
        topic, text = await self.receive()
        self.assertEqual(topic, 10)
        self.assertIn('Preparing', text)
        self.assertTrue(self.messages.empty())
        self.start_gate.set()
        topic, text = await self.receive()
        self.assertEqual(topic, 10)
        self.assertIn('Started a new conversation', text)
        self.assertEqual(self.renames, [(10, 'New conversation')])

    async def test_slow_topic_does_not_block_another_topic(self):
        self.start_gate.clear()
        await self.updates.put(update(1, '/new', 10))
        self.assertIn('Preparing', (await self.receive())[1])
        await self.updates.put(update(2, '/new', 20))
        topic, text = await self.receive()
        self.assertEqual(topic, 20)
        self.assertIn('Preparing', text)
        self.start_gate.set()
        results = [await self.receive(), await self.receive()]
        self.assertEqual({topic for topic, _ in results}, {10, 20})

    async def test_topic_lifecycle_update_is_silent(self):
        await self.updates.put(update(1, forum_topic_created={'name': 'Test'}))
        await self.updates.put(update(2, '/new'))
        self.assertIn('Preparing', (await self.receive())[1])
        self.assertIn('Started', (await self.receive())[1])
        self.assertTrue(self.messages.empty())

    async def test_named_new_resets_title(self):
        await self.updates.put(update(1, '/new Research plan'))
        await self.receive()
        self.assertIn('Started', (await self.receive())[1])
        self.assertEqual(self.renames, [(10, 'Research plan')])


if __name__ == '__main__':
    unittest.main(verbosity=2)
