import json
import os
import pathlib
import tempfile
import unittest

from bot import CodexRunner, Config, extract_session_id, safe_directory, split_message


class SplitMessageTests(unittest.TestCase):
    def test_short_message_is_unchanged(self):
        self.assertEqual(split_message("hello", 10), ["hello"])

    def test_long_message_is_split_within_limit(self):
        chunks = split_message("one two three four", 8)
        self.assertEqual(" ".join(chunks), "one two three four")
        self.assertTrue(all(len(chunk) <= 8 for chunk in chunks))


class SessionParsingTests(unittest.TestCase):
    def test_extracts_nested_thread_id(self):
        event = {"type": "thread.started", "data": {"thread_id": "abc-123"}}
        self.assertEqual(extract_session_id(json.dumps(event)), "abc-123")

    def test_ignores_non_json_output(self):
        self.assertIsNone(extract_session_id("not json"))


class SafeDirectoryTests(unittest.TestCase):
    def test_allows_child_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            child = root / "project"
            child.mkdir()
            self.assertEqual(safe_directory(root, "project"), child)

    def test_rejects_parent_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            with self.assertRaisesRegex(ValueError, "inside CODEX_WORKSPACE"):
                safe_directory(root, "..")


class CodexRunnerTests(unittest.TestCase):
    def test_runs_prompt_and_captures_answer_and_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "output = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
                "prompt = sys.stdin.read()\n"
                "output.write_text('answer: ' + prompt)\n"
                "print(json.dumps({'type': 'thread.started', 'thread_id': 'thread-1'}))\n"
            )
            fake_codex.chmod(0o755)
            config = Config(
                token="token",
                chat_id=1,
                workspace=root,
                codex_bin=os.fspath(fake_codex),
            )

            answer, session_id = CodexRunner(config).run("do the thing", root, None)

            self.assertEqual(answer, "answer: do the thing")
            self.assertEqual(session_id, "thread-1")


if __name__ == "__main__":
    unittest.main()
