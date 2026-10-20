#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Secret absence and diagnostic preservation on the same bounded records."""

import json
import unittest
from urllib.parse import quote

from launchplane_service_log_redaction import (
    MAX_MESSAGE_BYTES,
    RedactionError,
    Redactor,
)


class RedactionTests(unittest.TestCase):
    def test_supported_secret_framings_preserve_neighboring_evidence(self):
        secret = "S3NTINEL-private-8392"
        sha = "a" * 40
        trace = "launchplane_req_" + "b" * 32
        corpus = [
            (
                [
                    json.dumps(
                        {
                            "event": "provider_error",
                            "body": json.dumps({"password": secret}),
                            "trace_id": trace,
                        }
                    )
                ],
                (secret,),
                ("provider_error", trace),
            ),
            (
                [
                    json.dumps(
                        {
                            "body": json.dumps(
                                {"value": secret, "name": "DB_PASSWORD"}
                            ),
                            "event": "failed",
                        }
                    )
                ],
                (secret,),
                ("failed", "DB_PASSWORD"),
            ),
            (
                [r"{\"password\":\"" + secret + r"\",\"event\":\"failed\"}"],
                (secret,),
                ("failed",),
            ),
            (["token%3D" + secret], (secret,), ()),
            ([quote(quote("password=" + secret))], (secret,), ()),
            (
                ["password: |-", "  " + secret, "trace_id=" + trace + " restarted"],
                (secret,),
                (trace, "restarted"),
            ),
            (
                [
                    "- name: DB_PASSWORD",
                    "  value: " + secret,
                    "- name: ORDINARY",
                    "  value: healthy",
                ],
                (secret,),
                ("ORDINARY", "healthy"),
            ),
            (["  " + secret, "event=restarted"], (secret,), ("restarted",)),
            (
                [
                    '"value": "' + secret + '",',
                    '"name": "DB_PASSWORD"',
                    "event=restarted",
                ],
                (secret,),
                ("restarted",),
            ),
            (
                [
                    json.dumps(
                        {
                            "author": "agent",
                            "inside": "worker",
                            "private": False,
                            "token_count": 3,
                            "idempotency_key": "operation-1",
                            "trace_id": trace,
                            "commit_sha": sha,
                        }
                    )
                ],
                (),
                ("agent", "worker", "false", "token_count", "operation-1", trace, sha),
            ),
            (
                [
                    "deploying "
                    + sha
                    + " image registry.example.test/app@sha256:"
                    + "c" * 64
                ],
                (),
                (sha, "sha256:" + "c" * 64),
            ),
            (
                [
                    'File "/app/launchplane/service/operations/runner.py", line 88',
                    "KeyboardInterrupt",
                    "retrying non-private upload",
                ],
                (),
                (
                    "/app/launchplane/service/operations/runner.py",
                    "KeyboardInterrupt",
                    "non-private upload",
                ),
            ),
            (["Cookie: session=" + secret + "; HttpOnly"], (secret,), ()),
            (["command --password " + secret], (secret,), ()),
            (['password="' + secret + '"'], (secret,), ()),
            (['password={"value":"' + secret + '"}'], (secret,), ()),
            (['"API_TOKEN":"' + secret + '"'], (secret,), ()),
            (["('API_TOKEN', '" + secret + "')"], (secret,), ("API_TOKEN",)),
            (
                ["https://" + "user:p@" + secret + "@example.test/"],
                (secret,),
                ("example.test",),
            ),
        ]
        for records, absent, present in corpus:
            with self.subTest(records=records):
                state = Redactor()
                rendered = "\n".join(state.feed(record).message for record in records)
                for value in absent:
                    self.assertNotIn(value, rendered)
                for value in present:
                    self.assertIn(value, rendered)

    def test_provider_shapes_and_private_key_blocks(self):
        tokens = [
            "ghp_" + "A" * 36,
            "sk-proj-" + "B" * 40,
            "AKIA" + "C" * 16,
            "AIza" + "D" * 35,
            "xoxb-" + "E" * 20,
            "glpat-" + "F" * 20,
            "rk_live_" + "G" * 24,
        ]
        for token in tokens:
            with self.subTest(token=token):
                self.assertNotIn(
                    token, Redactor().feed("provider returned " + token).message
                )
        for kind in ("PRIVATE KEY", "RSA PRIVATE KEY", "PGP PRIVATE KEY BLOCK"):
            state = Redactor()
            records = [
                "-----BEGIN " + kind + "-----",
                "S3NTINEL-private-key-body",
                "-----END " + kind + "-----",
                "event=restarted",
            ]
            rendered = "\n".join(state.feed(record).message for record in records)
            self.assertNotIn("S3NTINEL-private-key-body", rendered)
            self.assertIn("restarted", rendered)
        state = Redactor()
        state.feed(
            'private_key="-----BEGIN '
            + 'PRIVATE KEY-----\\nS3NTINEL-inline-key\\n-----END PRIVATE KEY-----"'
        )
        self.assertIn("restarted", state.feed("event=restarted").message)

    def test_normalization_is_bounded_and_redaction_is_idempotent(self):
        text = json.dumps({"password": "S3NTINEL-nested-914", "event": "failed"})
        for encoded in (text, json.dumps(text), quote(text), quote(quote(text))):
            once = Redactor().feed(encoded).message
            self.assertNotIn("S3NTINEL-nested-914", once)
            self.assertEqual(Redactor().feed(once).message, once)
        for _ in range(12):
            text = quote(text)
        with self.assertRaises(RedactionError):
            Redactor().feed(text)

    def test_oversized_and_unbound_key_records_fail_without_output(self):
        with self.assertRaises(RedactionError):
            Redactor().feed("a." * MAX_MESSAGE_BYTES)
        with self.assertRaises(RedactionError):
            Redactor().feed("-----END RSA PRIVATE KEY-----")


if __name__ == "__main__":
    unittest.main()
