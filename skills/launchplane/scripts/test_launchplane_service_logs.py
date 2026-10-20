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
    def run_reader(
        self,
        stdout=b"",
        stderr=b"",
        extra=(),
        config=None,
        failure=None,
        driver_tail=False,
        image_id=False,
        containers=None,
        transport_stderr=b"",
    ):
        calls = []
        ids = [CONTAINER] if containers is None else containers

        def remote(argv, **kwargs):
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            calls.append(argv)
            command = shlex.split(argv[-1])
            if command[0] == "env":
                command = command[1:]
                while command and "=" in command[0]:
                    command = command[1:]
            self.assertEqual(command[:4], ["pct", "exec", str(CONFIG["lxc_id"]), "--"])
            docker = command[4:]
            combined = False
            if docker[:2] == ["sh", "-c"]:
                docker = shlex.split(docker[2])
                combined = docker[-1] == "2>&1"
                if combined:
                    docker = docker[:-1]
            if docker[:2] == ["docker", "ps"]:
                data = "".join(f"{identity}\n" for identity in ids).encode()
                return REAL_POPEN(
                    [
                        sys.executable,
                        "-c",
                        f"import sys; sys.stdout.buffer.write({data!r})",
                    ],
                    **kwargs,
                )
            if docker[:2] == ["docker", "inspect"]:
                data = "".join(
                    json.dumps(
                        {
                            "id": identity,
                            "image": CONFIG["image_repository"] + ":test",
                            "image_id": "sha256:" + ("c" if image_id else "b") * 64,
                            "project": CONFIG["compose_project"],
                            "service": CONFIG["compose_service"],
                            "created_at": START,
                            "started_at": START,
                            "finished_at": END,
                            "state": "exited",
                            "exit_code": 137,
                            "oom_killed": False,
                        }
                    )
                    + "\n"
                    for identity in docker
                    if identity in ids
                ).encode()
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
            self.assertIn(docker[-1], ids)
            emitted = stdout
            if driver_tail and "--tail" in docker:
                emitted = b"\n".join(
                    stdout.split(b"\n")[-int(docker[docker.index("--tail") + 1]) - 1 :]
                )
            data = emitted + stderr if combined else emitted
            diagnostics = transport_stderr if combined else stderr + transport_stderr
            return REAL_POPEN(
                [
                    sys.executable,
                    "-c",
                    f"import sys; sys.stdout.buffer.write({data!r}); sys.stderr.buffer.write({diagnostics!r})",
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
            [entry["stream"] for entry in result["entries"]], ["combined", "combined"]
        )
        self.assertTrue(calls)
        self.assertFalse(result["complete"])

    def test_redacts_secret_shapes_before_any_output(self):
        cases = [
            ('password="two words private"', "two words private"),
            ('{"password": "S3NTINEL-json\\"private"}', "S3NTINEL-json"),
            ("DB_PASSWORD=plain-private", "plain-private"),
            ("Authorization: Basic dXNlcjpwcml2YXRl", "dXNlcjpwcml2YXRl"),
            ("Bearer bearer-private-token", "bearer-private-token"),
            ("Cookie: session=cookie-private; theme=private-theme", "cookie-private"),
            ("Set-Cookie: session=set-cookie-private; HttpOnly", "set-cookie-private"),
            ('{"cookie": "json-cookie-private"}', "json-cookie-private"),
            ("apiKey=camel-private", "camel-private"),
            ("X-Api-Key: header-private", "header-private"),
            ("signing_key=signing-private", "signing-private"),
            ("https://" + "user:url-private" + "@example.test/", "url-private"),
            ("token ghp_" + "A" * 36, "ghp_" + "A" * 36),
            ("key sk-proj-" + "B" * 40, "sk-proj-" + "B" * 40),
            (
                "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwcml2YXRlIn0.c2lnbmF0dXJl",
                "eyJhbGci",
            ),
            ("AKIA" + "Z" * 16, "AKIA" + "Z" * 16),
            (
                "-----BEGIN "
                + "PRIVATE KEY-----\n"
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
            ("--until", "9999-12-31T23:59:59-01:00"),
            ("--since", "0001-01-01T00:00:00+01:00"),
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
        self.assertLessEqual(len(calls), 3)
        self.assertEqual(sum("docker logs" in call[-1] for call in calls), 1)
        self.assertNotIn("provider-private", json.dumps(result))
        self.assertNotIn("ops-test", json.dumps(result))

    def test_no_matches_is_not_proof_of_absence_and_tail_is_bounded(self):
        raw = b"2026-01-02T12:01:00Z hello\n2026-01-02T12:02:00Z world\n"
        status, result, _ = self.run_reader(
            raw, extra=("--lines", "1", "--trace-id", "absent")
        )
        self.assertEqual(status, 0)
        self.assertEqual(result["entries"], [])
        self.assertFalse(result["complete"])
        status, result, _ = self.run_reader(raw, extra=("--lines", "1"))
        self.assertEqual(result["entries"][-1]["message"], "world")
        self.assertTrue(result["tail_limit_reached"])

    def test_unparseable_log_output_is_an_error_not_empty_success(self):
        status, result, _ = self.run_reader(b"unframed password=S3NTINEL-unframed\n")
        self.assertEqual(status, 1)
        self.assertNotIn("S3NTINEL-unframed", json.dumps(result))

    def test_id_path_colon_and_encoded_boundaries(self):
        records = [
            "POST /v1/operations/op-1/cancel failed",
            "operation:op-1: failed",
            "op-1. completed",
            "op-1.5 different operation",
            "op-10 different operation",
            "operation%3Dop-1 failed",
        ]
        raw = "".join(
            "2026-01-02T12:01:00Z " + line + "\n" for line in records
        ).encode()
        status, result, _ = self.run_reader(raw, extra=("--operation-id", "op-1"))
        self.assertEqual(status, 0)
        messages = [entry["message"] for entry in result["entries"]]
        self.assertEqual(len(messages), 4)
        self.assertTrue(
            all("different operation" not in message for message in messages)
        )
        self.assertIn("/v1/operations/op-1/cancel", messages[0])

    def test_oversized_record_returns_no_partial_data(self):
        raw = (
            b"2026-01-02T12:01:00Z event=started\n"
            + b"2026-01-02T12:02:00Z "
            + b"password=S3NTINEL-oversize " * 2000
            + b"\n"
        )
        status, result, _ = self.run_reader(raw)
        self.assertEqual(status, 1)
        self.assertNotIn("entries", result)
        self.assertNotIn("S3NTINEL", json.dumps(result))

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
                'private_key="-----BEGIN '
                + 'PRIVATE KEY-----\\nshort-private\\n-----END PRIVATE KEY-----"',
                "short-private",
            ),
            ("sk_live_" + "X" * 24, "sk_live_" + "X" * 24),
            ('{"name":"DB_PASSWORD","value":"pair-private"}', "pair-private"),
            ("('API_TOKEN', 'tuple-private')", "tuple-private"),
            ('{"auth":"auth-private"}', "auth-private"),
            ("sessionid=session-private", "session-private"),
            ("command --password cli-private", "cli-private"),
            ("value " + "AIza" + "A" * 35, "AIza" + "A" * 35),
            ("value " + "xoxb-" + "B" * 20, "xoxb-" + "B" * 20),
            ("value " + "ASIA" + "C" * 16, "ASIA" + "C" * 16),
            ("value " + "glpat-" + "D" * 20, "glpat-" + "D" * 20),
            ("https://" + "user:ab/cd" + "@example.test/", "ab/cd"),
            (
                "https://" + "user:p@url-fragment-private" + "@example.test/",
                "url-fragment-private",
            ),
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
        self.assertEqual(status, 1)
        self.assertNotIn("abcdefgh", json.dumps(result))

    def test_wrong_image_and_overlarge_response_fail_without_partial_data(self):
        status, result, calls = self.run_reader(
            config={**CONFIG, "image_repository": "registry.example.test/other"}
        )
        self.assertEqual(status, 1)
        self.assertEqual(result["error"], "unexpected_service_image")
        self.assertLessEqual(len(calls), 2)
        with patch.object(reader, "MAX_RESPONSE_BYTES", 512):
            status, result, _ = self.run_reader(b"x" * 1024)
        self.assertEqual(status, 1)
        self.assertNotIn("entries", result)

    def test_local_parse_deadline_fails_closed(self):
        raw = b"2026-01-02T12:01:00Z event=started\n"
        with (
            patch.object(reader.time, "monotonic", side_effect=[0, 2]),
            self.assertRaises(reader.LogReadError),
        ):
            reader.entries_from_stream(
                raw, 0, reader.timestamp(START), reader.timestamp(END), (), 1, 1
            )

    def test_historical_id_is_found_before_display_tail(self):
        raw = (
            b"2026-01-02T12:01:00Z op-1 original\n"
            + b"2026-01-02T13:01:00Z later noise\n" * 5
        )
        status, result, _ = self.run_reader(
            raw, extra=("--lines", "1", "--operation-id", "op-1"), driver_tail=True
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            [entry["message"] for entry in result["entries"]], ["op-1 original"]
        )

    def test_controls_inside_message_are_not_record_boundaries(self):
        status, result, _ = self.run_reader(
            "2026-01-02T12:01:00Z hello\rworld\u2028end\n".encode()
        )
        self.assertEqual(status, 0)
        message = result["entries"][0]["message"]
        self.assertTrue(all(part in message for part in ("hello", "world", "end")))
        self.assertFalse(any(char in message for char in ("\r", "\u2028", "\n")))
        self.assertEqual(result["changed_record_count"], 1)

    def test_replacement_image_and_container_limit_have_coverage(self):
        status, original, _ = self.run_reader()
        self.assertEqual(status, 0)
        status, result, _ = self.run_reader(image_id=True)
        self.assertEqual(status, 0)
        self.assertNotEqual(
            original["containers"][0]["image_id"], result["containers"][0]["image_id"]
        )
        ids = [f"{i:064x}" for i in range(reader.MAX_CONTAINERS + 2)]
        status, result, _ = self.run_reader(image_id=True, containers=ids)
        self.assertEqual(status, 0)
        self.assertEqual(result["omitted_containers"], len(ids) - reader.MAX_CONTAINERS)
        self.assertEqual(result["containers"][0]["state"], "exited")
        self.assertEqual(result["containers"][0]["exit_code"], 137)

    def test_filter_survives_redaction_and_multiline_secrets_fail_closed(self):
        raw = b"2026-01-02T12:01:00Z idempotency_key=op-1 trace.1 failed\n"
        status, result, _ = self.run_reader(raw, extra=("--trace-id", "trace.1"))
        self.assertEqual(status, 0)
        self.assertEqual(len(result["entries"]), 1)
        for message in (
            "password:\n  yaml-private",
            "-----BEGIN PGP PRIVATE "
            + "KEY BLOCK-----\nshort-pgp-private\n-----END PGP PRIVATE KEY BLOCK-----",
        ):
            raw = "".join(
                "2026-01-02T12:01:00Z " + line + "\n" for line in message.split("\n")
            ).encode()
            status, result, _ = self.run_reader(raw)
            self.assertEqual(status, 0)
            self.assertNotIn("yaml-private", json.dumps(result))
            self.assertNotIn("short-pgp-private", json.dumps(result))

    def test_transport_warnings_are_separate_and_never_emitted(self):
        status, result, _ = self.run_reader(
            b"2026-01-02T12:01:00Z event=restarted\n",
            transport_stderr=b"perl: warning: S3NTINEL-transport-secret\n",
        )
        self.assertEqual(status, 0)
        self.assertTrue(result["transport_diagnostics_present"])
        self.assertEqual(result["entries"][0]["message"], "event=restarted")
        self.assertNotIn("S3NTINEL-transport-secret", json.dumps(result))

    def test_invalid_calendar_and_cli_arguments_are_safe_json(self):
        for extra in (
            ("--since", "2026-13-40T12:00:00Z"),
            ("--lines", "not-an-integer"),
        ):
            status, result, calls = self.run_reader(extra=extra)
            self.assertEqual(status, 1)
            self.assertFalse(result["ok"])
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
