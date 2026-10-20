#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline behavior and secret-custody checks for the service log reader."""

import contextlib
import importlib.util
import io
import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "service_logs", Path(__file__).with_name("launchplane-service-logs.py")
)
assert SPEC is not None and SPEC.loader is not None
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)

CONFIG = {
    "ssh_host": "ops-test",
    "lxc_id": 123,
    "compose_project": "control-test",
    "compose_service": "api",
    "image_repository": "registry.example.test/control",
}
START = "2026-01-02T12:00:00Z"
END = "2026-01-02T12:10:00Z"
CONTAINER = "a" * 64
REAL_POPEN = subprocess.Popen


class ServiceLogTests(unittest.TestCase):
    def run_reader(self, stdout=b"", stderr=b"", extra=(), config=None, failure=None):
        calls = []

        def remote(argv, **kwargs):
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            calls.append(argv)
            command = shlex.split(argv[-1])
            self.assertEqual(command[:4], ["pct", "exec", "123", "--"])
            docker = command[4:]
            if docker[:2] == ["docker", "ps"]:
                data = f"{CONTAINER} {CONFIG['image_repository']}:test\n".encode()
                return REAL_POPEN(
                    [
                        sys.executable,
                        "-c",
                        f"import sys; sys.stdout.buffer.write({data!r})",
                    ],
                    **kwargs,
                )
            self.assertEqual(docker[:2], ["docker", "logs"])
            if failure:
                return REAL_POPEN(
                    [
                        sys.executable,
                        "-c",
                        f"import sys; sys.stderr.buffer.write({failure!r}); sys.exit(1)",
                    ],
                    **kwargs,
                )
            self.assertEqual(docker[docker.index("--since") + 1], START)
            self.assertEqual(docker[docker.index("--until") + 1], END)
            self.assertEqual(docker[-1], CONTAINER)
            return REAL_POPEN(
                [
                    sys.executable,
                    "-c",
                    f"import sys; sys.stdout.buffer.write({stdout!r}); sys.stderr.buffer.write({stderr!r})",
                ],
                **kwargs,
            )

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "reader.json"
            config_path.write_text(json.dumps(CONFIG if config is None else config))
            output, errors = io.StringIO(), io.StringIO()
            with (
                patch.object(reader.subprocess, "Popen", side_effect=remote),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(errors),
            ):
                status = reader.main(
                    [
                        "--config",
                        str(config_path),
                        "--since",
                        START,
                        "--until",
                        END,
                        *extra,
                    ]
                )
            self.assertEqual(errors.getvalue(), "")
            return status, json.loads(output.getvalue()), calls

    def test_window_and_literal_ids_cover_both_streams(self):
        stdout = (
            b"2026-01-02T11:59:59Z op-1 trace.1 too early\n"
            b"2026-01-02T12:00:00.000000001Z op-1 trace.1 started\n"
            b"2026-01-02T12:01:00Z op-10 trace.1 wrong operation\n"
            b"2026-01-02T12:02:00Z op-1 traceX1 wrong trace\n"
            b"2026-01-02T12:10:00Z op-1 trace.1 too late\n"
        )
        stderr = b"2026-01-02T12:03:00Z op-1 trace.1 failed\n"
        status, result, calls = self.run_reader(
            stdout, stderr, ("--operation-id", "op-1", "--trace-id", "trace.1")
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            [entry["message"] for entry in result["entries"]],
            ["op-1 trace.1 started", "op-1 trace.1 failed"],
        )
        self.assertEqual(
            [entry["stream"] for entry in result["entries"]], ["stdout", "stderr"]
        )
        self.assertEqual(len(calls), 2)
        self.assertFalse(result["complete"])

    def test_redacts_secret_shapes_before_any_output(self):
        cases = [
            ('password="two words private"', "two words private"),
            ('{"password": "json\\"private"}', "json"),
            ("DB_PASSWORD=plain-private", "plain-private"),
            ("Authorization: Basic dXNlcjpwcml2YXRl", "dXNlcjpwcml2YXRl"),
            ("Bearer bearer-private-token", "bearer-private-token"),
            ("Cookie: session=cookie-private; theme=private-theme", "cookie-private"),
            ("Set-Cookie: session=set-cookie-private; HttpOnly", "set-cookie-private"),
            ('{"cookie": "json-cookie-private"}', "json-cookie-private"),
            ("apiKey=camel-private", "camel-private"),
            ("X-Api-Key: header-private", "header-private"),
            ("signing_key=signing-private", "signing-private"),
            ("https://user:url-private@example.test/", "url-private"),
            ("token ghp_" + "A" * 36, "ghp_" + "A" * 36),
            ("key sk-proj-" + "B" * 40, "sk-proj-" + "B" * 40),
            (
                "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwcml2YXRlIn0.c2lnbmF0dXJl",
                "eyJhbGci",
            ),
            ("AKIA" + "Z" * 16, "AKIA" + "Z" * 16),
            (
                "-----BEGIN PRIVATE KEY-----\n"
                + "Q" * 64
                + "\nshort-private-body\n-----END PRIVATE KEY-----",
                "short-private-body",
            ),
        ]
        for message, secret in cases:
            with self.subTest(message=message):
                raw = "".join(
                    f"2026-01-02T12:01:00Z {part}\n" for part in message.splitlines()
                ).encode()
                status, result, _ = self.run_reader(raw, raw)
                self.assertEqual(status, 0)
                rendered = json.dumps(result)
                self.assertNotIn(secret, rendered)
                self.assertIn("[redacted", rendered)

    def test_truncated_private_key_body_and_terminal_controls(self):
        raw = (
            b"2026-01-02T12:01:00Z "
            + b"A" * 64
            + b"\n2026-01-02T12:02:00Z visible\x1b[31m text\n"
        )
        status, result, _ = self.run_reader(raw)
        self.assertEqual(status, 0)
        self.assertNotIn("A" * 64, json.dumps(result))
        self.assertNotIn("\x1b", json.dumps(result, ensure_ascii=False))

    def test_invalid_queries_and_config_do_not_read_remote(self):
        for extra in (
            ("--until", START),
            ("--since", "2026-01-01T00:00:00Z"),
            ("--lines", "0"),
            ("--lines", "-1"),
            ("--operation-id", "$(touch /tmp/unwanted)"),
            ("--since", "2026-01-02T12:00:00"),
        ):
            with self.subTest(extra=extra):
                status, result, calls = self.run_reader(extra=extra)
                self.assertEqual(status, 1)
                self.assertEqual(calls, [])
                self.assertFalse(result["ok"])
        for field, value in (
            ("ssh_host", "-oProxyCommand=evil"),
            ("compose_service", "api; touch /tmp/unwanted"),
            ("lxc_id", True),
        ):
            with self.subTest(field=field):
                status, _, calls = self.run_reader(config={**CONFIG, field: value})
                self.assertEqual(status, 1)
                self.assertEqual(calls, [])

    def test_provider_errors_never_escape_and_no_retries(self):
        status, result, calls = self.run_reader(
            failure=b"Cookie: session=provider-private"
        )
        self.assertEqual(status, 1)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("provider-private", json.dumps(result))
        self.assertNotIn("ops-test", json.dumps(result))

    def test_no_matches_is_not_proof_of_absence_and_tail_is_bounded(self):
        raw = b"2026-01-02T12:01:00Z hello\n2026-01-02T12:02:00Z world\n"
        status, result, _ = self.run_reader(
            raw, extra=("--lines", "1", "--trace-id", "absent")
        )
        self.assertEqual(status, 0)
        self.assertEqual(result["entries"], [])
        self.assertTrue(result["tail_limit_reached"])
        self.assertFalse(result["complete"])

    def test_unparseable_log_output_is_an_error_not_empty_success(self):
        status, result, _ = self.run_reader(b"unframed password=private\n")
        self.assertEqual(status, 1)
        self.assertNotIn("private", json.dumps(result))

    def test_nanosecond_window_boundaries(self):
        self.assertGreater(
            reader.timestamp("2026-01-02T12:00:00.000000002Z"),
            reader.timestamp("2026-01-02T12:00:00.000000001Z"),
        )

    def test_split_and_structured_secrets(self):
        cases = [
            ('{"api_key": ["array-private", "second-private"]}', "array-private"),
            ('{"password": {"value": "nested-private"}}', "nested-private"),
            ('{"password": "unterminated-private', "unterminated-private"),
            (
                'private_key="-----BEGIN PRIVATE KEY-----\\nshort-private\\n-----END PRIVATE KEY-----"',
                "short-private",
            ),
            ("sk_live_" + "X" * 24, "sk_live_" + "X" * 24),
        ]
        for message, secret in cases:
            with self.subTest(message=message):
                status, result, _ = self.run_reader(
                    f"2026-01-02T12:01:00Z {message}\n".encode()
                )
                self.assertEqual(status, 0)
                self.assertNotIn(secret, json.dumps(result))

    def test_tail_without_pem_start_redacts_short_final_fragment(self):
        raw = b"2026-01-02T12:01:00Z abcdefgh\n2026-01-02T12:02:00Z -----END RSA PRIVATE KEY-----\n"
        status, result, _ = self.run_reader(raw)
        self.assertEqual(status, 0)
        self.assertNotIn("abcdefgh", json.dumps(result))

    def test_wrong_image_and_overlarge_response_fail_without_partial_data(self):
        status, result, calls = self.run_reader(
            config={**CONFIG, "image_repository": "registry.example.test/other"}
        )
        self.assertEqual(status, 1)
        self.assertEqual(result["error"], "unexpected_service_image")
        self.assertEqual(len(calls), 1)
        with patch.object(reader, "MAX_RESPONSE_BYTES", 512):
            status, result, _ = self.run_reader(b"x" * 1024)
        self.assertEqual(status, 1)
        self.assertNotIn("entries", result)


if __name__ == "__main__":
    unittest.main()
