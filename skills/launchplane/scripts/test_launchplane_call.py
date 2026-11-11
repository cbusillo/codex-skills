#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography==50.0.2", "jsonschema==4.26.0", "rfc3339-validator==0.1.4", "websockets==17.4"]
# ///
from __future__ import annotations

import importlib.util
import io
import json
import socket
import stat
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location("generic_caller", SCRIPT_DIR / "launchplane-call.py")
assert SPEC is not None and SPEC.loader is not None
caller: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(caller)


class CallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve() / "task-evidence" / "proof"
        self.root.mkdir(parents=True)
        self.route = "/v1/first"
        self.payload: dict[str, Any] = {
            "status": "accepted",
            "trace_id": "trace-example",
            "result": {"safe": True, "private": "private-canary"},
        }
        self.response_status = 202
        self.disconnect = False
        self.observed: list[tuple[str, Any]] = []
        self.properties = {"safe": {"type": "boolean", caller.SAFE_OUTPUT: True}, "private": {"type": "string"}}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
                return None

            def do_get(self) -> None:
                document = {
                    "paths": {
                        fixture.route: {
                            "post": {
                                "operationId": "fixture_operation",
                                "responses": {
                                    str(fixture.response_status): {
                                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Reply"}}}
                                    }
                                },
                            }
                        }
                    },
                    "x-launchplane-agent-contract": {
                        "operations": [{"operation_id": "fixture_operation", "method": "POST", "path": fixture.route}]
                    },
                    "components": {
                        "schemas": {
                            "Reply": {
                                "type": "object",
                                "properties": {"result": {"type": "object", "properties": fixture.properties}},
                            }
                        }
                    },
                }
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(document).encode())

            def do_post(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.observed.append((self.path, body))
                if fixture.disconnect:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(fixture.response_status)
                self.end_headers()
                self.wfile.write(json.dumps(fixture.payload).encode())

            do_GET = do_get
            do_POST = do_post

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), cast(Callable[..., BaseHTTPRequestHandler], Handler))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.enterContext(
            patch.object(
                caller.operator,
                "resolve_settings",
                return_value={
                    "service_url": f"http://127.0.0.1:{self.server.server_port}",
                    "token": "isolated-token",
                },
            )
        )

    def run_call(self, name: str, *options: str) -> tuple[int, dict[str, Any]]:
        output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["launchplane-call", "fixture_operation", "--mode", "dry-run", "--response-file", str(self.root / name), *options],
            ),
            redirect_stdout(output),
        ):
            result = caller.main()
        return result, json.loads(output.getvalue())

    def test_live_route_and_new_marked_field_need_no_client_change(self) -> None:
        self.assertEqual(self.run_call("first.json")[0], 0)
        self.route = "/v1/replacement"
        self.properties["new_count"] = {"type": "integer", caller.SAFE_OUTPUT: True}
        self.payload["result"]["new_count"] = 3
        code, output = self.run_call("second.json")
        self.assertEqual(code, 0)
        self.assertEqual([path for path, _ in self.observed], ["/v1/first", self.route])
        self.assertEqual(output["result"]["new_count"], self.payload["result"]["new_count"])

    def test_unmarked_data_stays_in_exclusive_private_evidence(self) -> None:
        self.properties["container"] = {"type": "object", caller.SAFE_OUTPUT: True}
        self.payload["result"]["container"] = {"secret": "container-canary"}
        code, output = self.run_call("private.json")
        self.assertEqual(code, 0)
        self.assertEqual(output["result"], {"safe": self.payload["result"]["safe"]})
        self.assertNotIn("canary", json.dumps(output))
        evidence = self.root / "private.json"
        self.assertEqual(json.loads(evidence.read_bytes()), self.payload)
        self.assertEqual(stat.S_IMODE(evidence.stat().st_mode), 0o600)
        self.assertEqual(self.run_call("private.json")[0], 1)
        self.assertEqual(len(self.observed), 1)

    def test_service_refusal_is_shown_without_echoed_details(self) -> None:
        self.response_status = 409
        self.payload = {
            "trace_id": "refusal-trace",
            "error": {
                "code": "review_required",
                "message": "Review the request first",
                "reason": "changed_plan",
                "details": "private-canary",
            },
        }
        code, output = self.run_call("refused.json")
        self.assertEqual(code, 1)
        self.assertEqual(output["error"], {key: self.payload["error"][key] for key in ("code", "message", "reason")})
        self.assertNotIn("canary", json.dumps(output))
        self.assertEqual(json.loads((self.root / "refused.json").read_bytes()), self.payload)

    def test_lost_apply_reply_keeps_unknown_outcome_without_retry(self) -> None:
        self.disconnect = True
        code, output = self.run_call("lost.json", "--mode", "apply", "--idempotency-key", "original-proof-key")
        self.assertEqual(code, 1)
        self.assertEqual(output["status"], "outcome_unknown")
        self.assertEqual(len(self.observed), 1)
        self.assertTrue((self.root / "lost.json").stat().st_size)

    def test_symlink_evidence_is_refused_before_any_request(self) -> None:
        target = self.root / "target.json"
        target.write_text("original")
        (self.root / "link.json").symlink_to(target)
        self.assertEqual(self.run_call("link.json")[0], 1)
        self.assertEqual(target.read_text(), "original")
        self.assertFalse(self.observed)


if __name__ == "__main__":
    unittest.main()
