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
    MAX_LITERAL_ATTEMPTS,
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
            (["GET /cb?token%3D" + secret + " [200]"], (secret,), ()),
            (
                ['{"event":"redirect"} next=/cb%3Ftoken%3D' + secret],
                (secret,),
                ("redirect",),
            ),
            (['{"a":1} body=\\"password\\":\\"' + secret + '\\"'], (secret,), ()),
            (
                [
                    json.dumps(
                        {"variable": "PRIVATEKEY", "val": secret, "event": "failed"}
                    )
                ],
                (secret,),
                ("failed",),
            ),
            (
                [
                    json.dumps({"key": "LOG_LEVEL", "value": "debug"}),
                    "key=LOG_LEVEL value=debug",
                ],
                (),
                ("LOG_LEVEL", "debug"),
            ),
            (
                [
                    "password: 'wrapped",
                    "WARNING slow request",
                    "  '" + secret + "'",
                    "event=restarted",
                ],
                (secret,),
                ("restarted",),
            ),
            (["data=" + secret + " key=DB_PASSWORD"], (secret,), ()),
            (["default=" + secret + " name=SMTP_PASSWORD"], (secret,), ()),
            (["val=" + secret + " variable=API_TOKEN"], (secret,), ()),
            (["- {value: " + secret + ", name: DB_PASSWORD}"], (secret,), ()),
            (
                [
                    "provider {'value': '"
                    + secret
                    + "', 'name': 'DB_PASSWORD'} rejected"
                ],
                (secret,),
                (),
            ),
            (
                ["bad env: ('API_TOKEN', '" + secret + "') rejected"],
                (secret,),
                ("rejected",),
            ),
            (
                ["DB_PASS=" + secret, "SMTP_PASS=" + secret, "user=a pass=" + secret],
                (secret,),
                (),
            ),
            ([json.dumps({"db_pass": secret, "smtp_creds": secret})], (secret,), ()),
            (
                [json.dumps({"compass": "north", "bypass": "disabled"})],
                (),
                ("north", "disabled"),
            ),
            (
                ["[worker] bad env: ('API_TOKEN', '" + secret + "') rejected"],
                (secret,),
                ("rejected",),
            ),
            (["retry (2/3): ('API_TOKEN', '" + secret + "')"], (secret,), ()),
            (["[ERROR] argv: ['deploy', '--token', '" + secret + "']"], (secret,), ()),
            (
                ["data=" + secret + ' extra={"attempt":1} key=DB_PASSWORD'],
                (secret,),
                (),
            ),
            (['value={"raw":"' + secret + '"} name=DB_PASSWORD'], (secret,), ()),
            (["value=" + secret + ' {"name":"DB_PASSWORD"}'], (secret,), ()),
            (['{"value":"' + secret + '"} {"name":"DB_PASSWORD"}'], (secret,), ()),
            (
                [
                    "[INFO] "
                    + json.dumps(
                        {"password": secret, "event": "failed", "trace_id": trace}
                    )
                ],
                (secret,),
                ("failed", trace),
            ),
            (["PGPASSWORD=" + secret], (secret,), ()),
            (
                [json.dumps({"secrets": {"odoo_admin": secret}, "event": "failed"})],
                (secret,),
                ("failed",),
            ),
            ([json.dumps({"api_keys": [secret], "tokens": [secret]})], (secret,), ()),
            (["name=DB_PASSWORD value=" + secret], (secret,), ()),
            (["key=DB_PASSWORD value=" + secret], (secret,), ()),
            (["name: DB_PASSWORD, value: " + secret], (secret,), ()),
            (["- {name: DB_PASSWORD, value: " + secret + "}"], (secret,), ()),
            (
                ["basic validation failed", "pulled image digest sha256:" + "d" * 64],
                (),
                ("basic validation failed", "sha256:" + "d" * 64),
            ),
            (
                [
                    "(psycopg2.errors.UniqueViolation) duplicate key value",
                    "(1/3) migrating",
                    "{level=info event=migrating}",
                ],
                (),
                ("UniqueViolation", "(1/3) migrating", "event=migrating"),
            ),
            (
                ["https://registry.npmjs.org/@scope/pkg"],
                (),
                ("registry.npmjs.org/@scope/pkg",),
            ),
            (
                [
                    "password: | # private field",
                    "WARNING slow request",
                    "  " + secret,
                    "event=restarted",
                ],
                (secret,),
                ("restarted",),
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

    def test_complete_sensitive_records_preserve_later_tracebacks(self):
        secret = "S3NTINEL-complete-record"
        records = [
            json.dumps({"session": secret}),
            json.dumps({"body": json.dumps({"password": secret})}),
            'password="' + secret + '"',
            "token='" + secret + "'",
            "password=" + secret,
            "token=[REDACTED]",
            "api_key=(unset)",
            "{'secrets': [SecretStr('**')]}",
            json.dumps(json.dumps({"password": secret})),
            "{'password': {'v': '" + secret + "'}, 'x': 1}",
        ]
        for record in records:
            with self.subTest(record=record):
                state = Redactor()
                rendered = "\n".join(
                    state.feed(line).message
                    for line in [
                        record,
                        "Traceback (most recent call last):",
                        '  File "/app/x.py", line 3',
                        "    raise ValueError",
                        "ValueError: boom",
                    ]
                )
                self.assertNotIn(secret, rendered)
                self.assertIn("/app/x.py", rendered)
                self.assertIn("raise ValueError", rendered)
                self.assertIn("ValueError: boom", rendered)
        state = Redactor()
        state.feed("password: |-")
        self.assertNotIn(
            secret, state.feed("  " + json.dumps({"innocent": secret})).message
        )

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

    def test_malformed_literals_do_not_raise(self):
        for text in ("{[1]: 2}", "{1, []}"):
            self.assertIsInstance(Redactor().feed(text).message, str)

    def test_oversized_and_unbound_key_records_fail_without_output(self):
        with self.assertRaises(RedactionError):
            Redactor().feed("a." * MAX_MESSAGE_BYTES)
        with self.assertRaises(RedactionError):
            Redactor().feed("-----END RSA PRIVATE KEY-----")
        state = Redactor()
        marker = "S3NTINELBudget"
        masked = state.feed(("[" + marker + "] ") * (MAX_LITERAL_ATTEMPTS + 1))
        self.assertTrue(masked.redacted)
        self.assertNotIn(marker, masked.message)
        self.assertIn("restarted", state.feed("event=restarted").message)
        state.feed("[worker] " * (MAX_LITERAL_ATTEMPTS + 1) + "password=[")
        self.assertNotIn(marker, state.feed("  " + marker).message)

    def test_balanced_rejected_spans_do_not_exhaust_nested_candidates(self):
        count = MAX_LITERAL_ATTEMPTS + 4
        sql = "INSERT INTO t (" + ", ".join(f"c{i}" for i in range(count))
        sql += ") VALUES (" + ", ".join(f"%(c{i})s" for i in range(count)) + ")"
        queue = "queue=[" + ", ".join(f"Operation(op-{i})" for i in range(count)) + "]"
        for record in (sql, queue, json.dumps({"query": sql})):
            self.assertIn(
                "c0" if record != queue else "op-0", Redactor().feed(record).message
            )
        secret = "S3NTINELSkipped"
        state = Redactor()
        masked = state.feed("Op((a), password=" + secret + ') {"trace_id":"T"}')
        self.assertNotIn(secret, masked.message)
        trace = "trace-budget-evidence-991"
        later = state.feed(
            "Op("
            + "(x)" * count
            + ") "
            + json.dumps({"token": secret, "trace_id": trace})
        )
        self.assertNotIn(secret, later.message)
        self.assertIn(trace, later.message)


if __name__ == "__main__":
    unittest.main()
