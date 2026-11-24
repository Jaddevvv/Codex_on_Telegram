"""Offline QA for file drops, per-conversation folders and steering.

Drives bot.main() with synthetic Telegram updates. Telegram and the agent
backend are replaced by local fakes; nothing is sent and no model is called.
Set QA_BOT_PATH to test a specific bridge file.
"""
import asyncio
import importlib.util
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

os.environ['TELEGRAM_BOT_TOKEN'] = 'offline-test-token'
os.environ['ALLOWED_CHAT_ID'] = '123'
os.environ['CODEX_EVENT_LOG'] = str(Path(tempfile.gettempdir()) / 'telegram-qa-events.log')
path = Path(os.environ.get('QA_BOT_PATH', Path(__file__).resolve().parents[1] / 'bot.py'))
spec = importlib.util.spec_from_file_location('telegram_qa_steer_target', path)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

TOPIC = 10


def text_update(n, text, topic=TOPIC):
    return {'update_id': n, 'message': {
        'message_id': n, 'chat': {'id': 123}, 'message_thread_id': topic,
        'is_topic_message': True, 'text': text}}


def doc_update(n, name, topic=TOPIC, caption=None, media_group_id=None):
    message = {'message_id': n, 'chat': {'id': 123}, 'message_thread_id': topic,
               'is_topic_message': True,
               'document': {'file_id': f'file-{n}', 'file_name': name}}
    if caption is not None:
        message['caption'] = caption
    if media_group_id is not None:
        message['media_group_id'] = media_group_id
    return {'update_id': n, 'message': message}


class SteeringFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / 'workspace'
        self.workspace.mkdir()
        self.updates = asyncio.Queue()
        self.messages = asyncio.Queue()
        self.documents = []
        self.renames = []
        self.backends = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.counter = 0
        owner = self

        class FakeBackend:
            display_name = 'FakeAgent'

            def __init__(self):
                self.thread_id = None
                self.cwd = bot.WORKSPACE
                self.pending_thread_name = None
                self.last_thread_name = None
                self.active_turn_id = None
                self.progress_updates = asyncio.Queue()
                self.citation_sources = {}
                self.resume_choices = []
                self.current_model = 'fake-model'
                self.current_effort = 'medium'
                self.runs = []
                self.steers = []
                self.new_threads = []
                self.steer_ok = True
                self.steer_hook = None
                owner.backends.append(self)

            async def start(self):
                await self.new_thread()

            async def close(self):
                pass

            async def new_thread(self, name=None, cwd=None):
                owner.counter += 1
                self.thread_id = f'thread-{owner.counter}'
                self.cwd = str(cwd or bot.WORKSPACE)
                self.pending_thread_name = name
                self.new_threads.append((name, self.cwd))

            async def set_thread_name(self, name):
                self.pending_thread_name = name
                return False

            def normalize_thread_name(self, name):
                return ' '.join(str(name).split())

            async def run(self, prompt, title_prompt=None):
                self.runs.append({'prompt': prompt, 'title_prompt': title_prompt,
                                  'cwd': self.cwd, 'thread': self.thread_id})
                self.active_turn_id = f'turn-{len(self.runs)}'
                try:
                    if 'MAKE A FILE' in prompt:
                        outbox = Path(re.search(r'save it \(or a copy\) in `([^`]+)/`', prompt).group(1))
                        outbox.mkdir(parents=True, exist_ok=True)
                        (outbox / 'result.txt').write_text('generated')
                    await owner.gate.wait()
                finally:
                    self.active_turn_id = None
                return f'answer {len(self.runs)}'

            async def steer(self, prompt):
                if self.steer_hook:
                    await self.steer_hook()
                if not self.active_turn_id or not self.steer_ok:
                    raise RuntimeError('no active turn to steer')
                self.steers.append(prompt)
                return self.active_turn_id

            async def interrupt(self):
                return bool(self.active_turn_id)

            async def list_threads(self, limit=10):
                self.resume_choices = [{'id': 'thread-1', 'name': 'Files chat'}]
                return self.resume_choices

            async def resume_thread(self, thread_id):
                self.thread_id = thread_id
                self.cwd = bot.WORKSPACE
                return {'id': thread_id, 'cwd': bot.WORKSPACE}

            async def resume_confirmation(self, resumed, listed_thread):
                return f"Resumed conversation:\n{resumed['id']}"

            async def status_text(self):
                return 'status ok'

        self.FakeBackend = FakeBackend
        self.real_backend_class = bot.CodexAppServer

        async def telegram(method, values=None, timeout=70):
            if method == 'deleteWebhook':
                return True
            if method == 'getUpdates':
                return [await self.updates.get()]
            if method == 'getFile':
                return {'file_path': f"documents/{values['file_id']}"}
            if method in {'sendChatAction', 'editMessageText', 'deleteMessage'}:
                return True
            raise AssertionError(f'unexpected Telegram call: {method}')

        def download(file_path, destination):
            destination.write_bytes(f'content of {file_path}'.encode())

        async def send(chat_id, text, citation_sources=None, message_thread_id=None):
            await self.messages.put((message_thread_id, text))
            return [{'message_id': 1}]

        async def send_document(chat_id, path, caption=None, message_thread_id=None):
            self.documents.append((message_thread_id, Path(path), caption))

        async def rename(chat_id, topic, name):
            self.renames.append((topic, name))

        async def guard(sessions):
            await asyncio.Event().wait()

        self.patches = [
            patch.object(bot, 'telegram', telegram),
            patch.object(bot, 'telegram_file_download', download),
            patch.object(bot, 'send_message', send),
            patch.object(bot, 'send_document', send_document),
            patch.object(bot, 'rename_forum_topic', rename),
            patch.object(bot, 'monitor_cgroup_oom', guard),
            patch.object(bot, 'CodexAppServer', FakeBackend),
            patch.object(bot, 'WORKSPACE', str(self.workspace)),
            patch.object(bot, 'CONVERSATION_STATE_FILE', Path(self.tmp.name) / 'state.json'),
            patch.object(bot, 'ATTACHMENT_BATCH_SECONDS', 0.15),
        ]
        for p in self.patches:
            p.start()
        self.main = asyncio.create_task(bot.main())
        self.n = 0

    async def asyncTearDown(self):
        self.gate.set()
        self.main.cancel()
        try:
            await self.main
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    # helpers -------------------------------------------------------------
    async def send_text(self, text, topic=TOPIC):
        self.n += 1
        await self.updates.put(text_update(self.n, text, topic))

    async def send_doc(self, name, **kwargs):
        self.n += 1
        await self.updates.put(doc_update(self.n, name, **kwargs))

    async def receive(self, timeout=1.0):
        return (await asyncio.wait_for(self.messages.get(), timeout))[1]

    async def assert_quiet(self, seconds=0.4):
        await asyncio.sleep(seconds)
        self.assertTrue(self.messages.empty(), f'unexpected message: {self.messages._queue}')

    async def wait_for(self, predicate, timeout=1.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                self.fail('condition not reached')
            await asyncio.sleep(0.01)

    def backend(self):
        return self.backends[-1]

    def sessions_root(self):
        return self.workspace / 'telegram_sessions' / f'topic-{TOPIC}'

    # tests ---------------------------------------------------------------
    async def test_interface_list_matches_codex_backend_and_fake(self):
        fake = self.FakeBackend()
        real = self.real_backend_class()
        for name in bot.BACKEND_REQUIRED:
            self.assertTrue(hasattr(fake, name), f'fake lacks {name}')
            self.assertTrue(hasattr(real, name), f'CodexAppServer lacks {name}')
        for attribute in bot.OPTIONAL_COMMAND_ATTRIBUTES.values():
            self.assertTrue(hasattr(real, attribute), attribute)

    async def test_files_only_drop_acks_once_then_prompt_uses_all_files(self):
        await self.send_doc('Report A.docx', media_group_id='g1')
        await self.send_doc('Report B.docx', media_group_id='g1')
        ack = await self.receive()
        await self.assert_quiet()
        backend = self.backend()
        self.assertEqual(backend.runs, [])
        self.assertIn('📎 Saved 2 files to this conversation: Report A.docx, Report B.docx', ack)
        self.assertIn('Send your instructions when ready.', ack)
        folders = list(self.sessions_root().iterdir())
        self.assertEqual(len(folders), 1)
        folder = folders[0]
        self.assertIn(f'Folder: {folder}', ack)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['Report A.docx', 'Report B.docx'])
        # The conversation started with files: the agent thread works in that folder.
        self.assertEqual(backend.cwd, str(folder))

        await self.send_text('Compare these two reports')
        reply = await self.receive()
        self.assertEqual(reply, 'answer 1')
        run = backend.runs[0]
        self.assertTrue(run['prompt'].startswith('Compare these two reports'))
        self.assertIn(str(folder / 'Report A.docx'), run['prompt'])
        self.assertIn(str(folder / 'Report B.docx'), run['prompt'])
        self.assertNotIn('minute', run['prompt'])
        self.assertIn(str(folder / 'outbox'), run['prompt'])
        self.assertEqual(run['cwd'], str(folder))

        await self.send_text('And a follow-up')
        self.assertEqual(await self.receive(), 'answer 2')
        self.assertNotIn('Report A.docx', backend.runs[1]['prompt'])

    async def test_new_resets_folder_and_pending_files(self):
        await self.send_doc('old.pdf')
        first_ack = await self.receive()
        self.assertIn('Saved 1 file', first_ack)
        await self.send_text('/new Second try')
        self.assertIn('Preparing', await self.receive())
        self.assertIn('Started a new conversation', await self.receive())
        backend = self.backend()
        self.assertEqual(backend.cwd, str(self.workspace))
        await self.send_text('hello')
        self.assertEqual(await self.receive(), 'answer 1')
        self.assertNotIn('old.pdf', backend.runs[0]['prompt'])
        # A text-first conversation keeps the workspace as cwd ...
        self.assertEqual(backend.runs[0]['cwd'], str(self.workspace))
        # ... but its uploads go to its own (named) folder.
        await self.send_doc('new.pdf')
        ack = await self.receive()
        folders = sorted(p.name for p in self.sessions_root().iterdir())
        self.assertEqual(len(folders), 2)
        new_folder = next(p for p in self.sessions_root().iterdir() if p.name.endswith('-second-try'))
        self.assertIn(f'Folder: {new_folder}', ack)
        self.assertTrue((new_folder / 'new.pdf').is_file())
        self.assertEqual(backend.cwd, str(self.workspace))

    async def test_files_with_caption_run_once_with_pending_files(self):
        await self.send_doc('earlier.csv')
        self.assertIn('Saved 1 file', await self.receive())
        # Album: Telegram puts the caption on one item only.
        await self.send_doc('a.png', media_group_id='g2')
        await self.send_doc('b.png', media_group_id='g2', caption='Describe all of these')
        reply = await self.receive()
        self.assertEqual(reply, 'answer 1')
        await self.assert_quiet()
        backend = self.backend()
        self.assertEqual(len(backend.runs), 1)
        prompt = backend.runs[0]['prompt']
        self.assertTrue(prompt.startswith('Describe all of these'))
        for name in ('earlier.csv', 'a.png', 'b.png'):
            self.assertIn(name, prompt)
        self.assertIn('3 files', prompt)

    async def test_text_while_running_steers_the_turn(self):
        self.gate.clear()
        await self.send_text('Long task')
        await self.wait_for(lambda: self.backends and self.backend().active_turn_id)
        await self.send_text('also say BANANA')
        self.assertIn('↪️ Added to the running task', await self.receive())
        backend = self.backend()
        self.assertEqual(backend.steers, ['also say BANANA'])
        # Slash commands still work while the task runs.
        await self.send_text('/status')
        status = await self.receive()
        self.assertIn('status ok', status)
        self.assertIn('Conversation folder:', status)
        self.gate.set()
        self.assertEqual(await self.receive(), 'answer 1')
        await self.assert_quiet()
        self.assertEqual(len(backend.runs), 1)

    async def test_files_during_run_are_acked_and_ride_with_next_steer(self):
        self.gate.clear()
        await self.send_text('Long task')
        await self.wait_for(lambda: self.backends and self.backend().active_turn_id)
        await self.send_doc('late.xlsx')
        ack = await self.receive()
        self.assertIn('Saved 1 file', ack)
        self.assertIn('A task is still running', ack)
        backend = self.backend()
        self.assertEqual(len(backend.runs), 1)
        await self.send_text('use this sheet too')
        self.assertIn('(1 file included)', await self.receive())
        self.assertIn('late.xlsx', backend.steers[0])
        self.gate.set()
        self.assertEqual(await self.receive(), 'answer 1')
        self.assertEqual(len(backend.runs), 1)

    async def test_steer_failure_queues_and_runs_next(self):
        self.gate.clear()
        await self.send_text('First task')
        await self.wait_for(lambda: self.backends and self.backend().active_turn_id)
        backend = self.backend()
        backend.steer_ok = False
        await self.send_text('Second task')
        queued = await self.receive()
        self.assertIn('Queued', queued)
        self.assertNotIn('already working', queued)
        self.gate.set()
        self.assertEqual(await self.receive(), 'answer 1')
        self.assertEqual(await self.receive(), 'answer 2')
        self.assertTrue(backend.runs[1]['prompt'].startswith('Second task'))

    async def test_steer_racing_with_turn_end_starts_a_new_run(self):
        self.gate.clear()
        await self.send_text('First task')
        await self.wait_for(lambda: self.backends and self.backend().active_turn_id)
        backend = self.backend()

        async def finish_turn_first():
            self.gate.set()
            await asyncio.sleep(0.05)

        backend.steer_hook = finish_turn_first
        await self.send_text('Late addition')
        replies = [await self.receive(), await self.receive()]
        self.assertEqual(replies, ['answer 1', 'answer 2'])
        self.assertTrue(backend.runs[1]['prompt'].startswith('Late addition'))

    async def test_stop_drops_queued_messages(self):
        self.gate.clear()
        await self.send_text('First task')
        await self.wait_for(lambda: self.backends and self.backend().active_turn_id)
        backend = self.backend()
        backend.steer_ok = False
        await self.send_text('Second task')
        self.assertIn('Queued', await self.receive())
        await self.send_text('/stop')
        stop = await self.receive()
        self.assertIn('Stopping the current task', stop)
        self.assertIn('Dropped 1 queued message', stop)
        self.gate.set()
        self.assertEqual(await self.receive(), 'answer 1')
        await self.assert_quiet()
        self.assertEqual(len(backend.runs), 1)

    async def test_agent_outbox_files_are_sent_back(self):
        await self.send_text('MAKE A FILE please')
        self.assertEqual(await self.receive(), 'answer 1')
        await self.wait_for(lambda: self.documents)
        topic, document, caption = self.documents[0]
        self.assertEqual(topic, TOPIC)
        self.assertEqual(document.name, 'result.txt')
        self.assertEqual(document.parent.name, 'outbox')
        self.assertEqual(document.parent.parent.parent, self.sessions_root())

    async def test_resume_restores_files_first_folder(self):
        await self.send_doc('keep.txt')
        self.assertIn('Saved 1 file', await self.receive())
        backend = self.backend()
        folder = Path(backend.cwd)
        thread = backend.thread_id
        await self.send_text('read it')
        self.assertEqual(await self.receive(), 'answer 1')
        await self.send_text('/new')
        await self.receive()
        await self.receive()
        self.assertEqual(backend.cwd, str(self.workspace))
        backend.resume_choices = [{'id': thread, 'name': 'Files chat'}]
        await self.send_text('/resume 1')
        self.assertIn(f'Resumed conversation:\n{thread}', await self.receive())
        self.assertEqual(backend.cwd, str(folder))
        await self.send_doc('more.txt')
        ack = await self.receive()
        self.assertIn(f'Folder: {folder}', ack)
        self.assertTrue((folder / 'more.txt').is_file())

    async def test_command_caption_runs_command_then_keeps_files(self):
        await self.send_text('hello')
        self.assertEqual(await self.receive(), 'answer 1')
        await self.send_doc('fresh.docx', caption='/new')
        self.assertIn('Preparing', await self.receive())
        self.assertIn('Started a new conversation', await self.receive())
        ack = await self.receive()
        self.assertIn('Saved 1 file', ack)
        backend = self.backend()
        self.assertEqual(Path(backend.cwd) / 'fresh.docx', Path(ack.split('Folder: ')[1].splitlines()[0]) / 'fresh.docx')


if __name__ == '__main__':
    unittest.main(verbosity=2)
