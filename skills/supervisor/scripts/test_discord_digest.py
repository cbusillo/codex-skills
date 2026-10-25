#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline sender contract tests; all HTTP stays on a fake loopback server."""

import contextlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import discord_digest as sender


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.posts = []
        self.attachments = []
        self.fail_at = None
        self.metadata = {"guild_id": "11", "channel_id": "22"}
        self.redirect = False
        self.rate_limited = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                owner.requests.append(self.path)
                if owner.redirect:
                    self.send_response(302)
                    self.send_header("Location", owner.url + "/redirect")
                    self.end_headers()
                    return
                self.respond(200, owner.metadata)

            def do_POST(self):
                owner.requests.append(self.path)
                body = self.rfile.read(int(self.headers['Content-Length']))
                content_type = self.headers["Content-Type"]
                if content_type.startswith("multipart/form-data"):
                    message = BytesParser(policy=default).parsebytes(
                        f"Content-Type: {content_type}\r\n\r\n".encode() + body)
                    parts = list(message.iter_parts())
                    owner.posts.append(json.loads(parts[0].get_payload(decode=True)))
                    owner.attachments.append((parts[1].get_filename(), parts[1].get_payload(decode=True)))
                else:
                    owner.posts.append(json.loads(body))
                if len(owner.posts) == owner.fail_at:
                    self.respond(500, {"error": owner.url})
                else:
                    self.respond(200, {"channel_id": "22", "id": str(100 + len(owner.posts))})

            def respond(self, code, data):
                self.send_response(code)
                if owner.rate_limited:
                    self.send_header("X-RateLimit-Remaining", "0")
                    self.send_header("X-RateLimit-Reset-After", "0.25")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/webhooks/123/offline-secret-token"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = {"PATH": os.environ.get("PATH", ""), "CODE_HOME": str(self.root)}

    def run_sender(self, report="digest", args=(), env=None):
        return subprocess.run([sys.executable, sender.__file__, *args], input=report,
                              text=True, capture_output=True, env=env or self.env, timeout=10, check=False)

    def test_file_and_stdin_send_confirmed_receipts(self):
        report = "# Digest\n@everyone <@123>\n" + "🧭" * 2100 + "\nend"
        file = self.root / "report.md"
        file.write_text(report)
        for args in ((), ("--file", str(file))):
            self.posts.clear()
            self.requests.clear()
            result = self.run_sender(report, args, {**self.env, sender.KEY: self.url})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual("".join(p['content'] for p in self.posts), report)
            self.assertTrue(all(len(p['content'].encode('utf-16-le')) // 2 <= 2000 for p in self.posts))
            self.assertTrue(all(p['allowed_mentions'] == {'parse': []} for p in self.posts))
            self.assertEqual(result.stdout.splitlines(), [
                f"https://discord.com/channels/11/22/{101 + i}" for i in range(len(self.posts))])
            endpoint = urlsplit(self.url).path
            self.assertEqual(self.requests, [endpoint, *([endpoint + "?wait=true"] * len(self.posts))])
            self.assertNotIn(self.url, result.stdout + result.stderr)

    def test_attachment_is_one_confirmed_post_with_mentions_disabled(self) -> None:
        source = "Full report\n" + "🧭 pending choice @everyone\n" * 100
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            sender.deliver("Daily digest: see attached waits", self.url, source)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["allowed_mentions"], {"parse": []})
        self.assertEqual(self.attachments, [("stranded-work.md", source.encode("utf-8"))])
        self.assertEqual(output.getvalue().strip(), "https://discord.com/channels/11/22/101")

    def test_missing_input_file_names_read_failure_without_path(self) -> None:
        result = self.run_sender(args=("--file", str(self.root / "private-missing.md")),
                                 env={**self.env, sender.KEY: self.url})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "digest_report_read_failed")
        self.assertEqual(self.requests, [])

    def test_code_home_precedence_and_no_shell_execution(self):
        other = self.root / "other"
        other.mkdir()
        (other / "local.env").write_text(f'{sender.KEY}=wrong\n')
        marker = self.root / 'must-not-exist'
        (self.root / "local.env").write_text(
            f'UNRELATED=$(touch {marker})\nexport {sender.KEY}="{self.url}" # private\n')
        result = self.run_sender(env={**self.env, "CODEX_HOME": str(other)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        result = self.run_sender(env={**self.env, sender.KEY: self.url})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_codex_home_and_default_home_fallback(self):
        for env, path in (({"CODEX_HOME": str(self.root)}, self.root),
                          ({}, self.root / '.code')):
            path.mkdir(exist_ok=True)
            (path / 'local.env').write_text(f"{sender.KEY}='{self.url}'\n")
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(Path, 'home', return_value=self.root):
                self.assertEqual(sender.webhook_url(), self.url)

    def test_missing_key_is_named_and_value_free(self):
        (self.root / 'local.env').write_text('UNRELATED=private-other-value\n')
        result = self.run_sender()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), 'digest_webhook_missing')
        self.assertEqual(result.stdout, '')
        self.assertEqual(self.requests, [])

    def test_partial_failure_preserves_prior_receipt_and_never_retries(self):
        self.fail_at = 2
        result = self.run_sender('x' * 5000, env={**self.env, sender.KEY: self.url})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(result.stdout.strip(), 'https://discord.com/channels/11/22/101')
        self.assertEqual(result.stderr.strip(), 'digest_http_failed')
        self.assertNotIn(self.url, result.stdout + result.stderr)

    def test_successful_send_respects_bucket_reset(self):
        self.rate_limited = True
        with mock.patch.object(sender.time, 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()):
            sender.deliver('x' * 2100, self.url)
        self.assertTrue(sleep.called)
        self.assertTrue(all(call.args == (0.25,) for call in sleep.call_args_list))
        self.assertEqual(len(self.posts), 2)

    def test_protocol_error_does_not_echo_secret_response(self):
        opener = mock.Mock()
        opener.open.side_effect = http.client.BadStatusLine(self.url)
        with self.assertRaisesRegex(sender.DeliveryError, '^digest_http_failed$'):
            sender.request_json(opener, self.url)

    def test_invalid_metadata_prevents_send(self):
        self.metadata = {'channel_id': '22', 'guild_id': self.url}
        result = self.run_sender(env={**self.env, sender.KEY: self.url})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), 'digest_response_invalid')
        self.assertEqual(self.posts, [])

    def test_redirect_is_not_followed(self):
        self.redirect = True
        result = self.run_sender(env={**self.env, sender.KEY: self.url})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result.stderr.strip(), 'digest_http_redirect')

    def test_empty_or_secret_report_never_posts(self):
        for report in ('  \n', self.url):
            result = self.run_sender(report, env={**self.env, sender.KEY: self.url})
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(self.url, result.stdout + result.stderr)
        self.assertEqual(self.requests, [])

    def test_invalid_url_is_value_free(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {sender.KEY: 'https://invalid.example/private-secret'}, clear=True), contextlib.redirect_stderr(output):
            self.assertEqual(sender.main([]), 1)
        self.assertEqual(output.getvalue().strip(), 'digest_webhook_invalid')


if __name__ == "__main__":
    unittest.main()
