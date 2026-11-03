#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


BOOTSTRAP = """
import runpy, subprocess, sys
from unittest.mock import patch
script = sys.argv.pop(1)
response = subprocess.CompletedProcess([], 0, stdout=sys.stdin.buffer.read(), stderr=b'')
with patch.object(subprocess, 'run', return_value=response):
    runpy.run_path(script, run_name='__main__')
"""


class PublicReadTests(unittest.TestCase):
    def read(self, *flags: str) -> subprocess.CompletedProcess[str]:
        source = "https://github.com/example/repo/issues/1#issuecomment-1"
        comments = [
            {"id": 1, "html_url": source, "user": {"login": "fixture-bot"}, "body": "Director question: **Q1218.** Keep both?"},
            {
                "id": 2,
                "html_url": source.replace("-1", "-2"),
                "user": {"login": "fixture-bot"},
                "body": f"Director decision Q1218: Keep both.\n\nOnly after https://example.test/backup passes.\n\nQuestion: {source}",
            },
            {
                "id": 3,
                "html_url": source.replace("-1", "-3"),
                "user": {"login": "outsider"},
                "body": f"Director decision Q1218: Drop both.\n\nQuestion: {source}",
            },
        ]
        response = "HTTP/2.0 200\ncontent-type: application/json\n\n" + json.dumps(comments)
        with tempfile.TemporaryDirectory() as home:
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    BOOTSTRAP,
                    str(Path(__file__).with_name("oq.py")),
                    "example/repo#1",
                    "--owner",
                    "director",
                    *flags,
                ],
                cwd=home,
                input=response,
                text=True,
                capture_output=True,
                timeout=20,
                env={
                    "PATH": os.environ["PATH"],
                    "HOME": home,
                    "CODE_HOME": home,
                    "CODEX_HOME": home,
                    "CODEX_AUTOMATION_LOGIN": "fixture-bot",
                    "CODEX_GITHUB_TOKEN": "fixture-token",
                },
            )

    def test_public_read_works_outside_the_catalog(self) -> None:
        result = self.read()
        self.assertEqual(result.returncode, 0, result.stderr)
        question = json.loads(result.stdout)["questions"][0]
        self.assertEqual(question["status"], "answered")
        self.assertNotIn("answer_records", question)

    def test_public_read_exposes_only_accepted_answer_text_and_its_source(self) -> None:
        result = self.read("--include-answers")
        self.assertEqual(result.returncode, 0, result.stderr)
        question = json.loads(result.stdout)["questions"][0]
        self.assertEqual(question["number"], "Q1218")
        self.assertEqual(
            question["answer_records"],
            [{"url": question["answers"][0], "answer": "Keep both.\n\nOnly after https://example.test/backup passes."}],
        )


if __name__ == "__main__":
    unittest.main()
