#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["cryptography==50.0.2", "jsonschema==4.26.0", "rfc3339-validator==0.1.4", "websockets==17.4"]
# ///
from __future__ import annotations

import base64
import copy
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location("empty_replacement_operator", SCRIPT_DIR / "launchplane-write-action.py")
assert SPEC is not None and SPEC.loader is not None
helper: Any = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = helper
SPEC.loader.exec_module(helper)
private_io: Any = importlib.import_module("launchplane-call")


class FixtureResponse(io.BytesIO):
    def __init__(self, raw: bytes, status: int) -> None:
        super().__init__(raw)
        self.code = status


class ClosingFailure(io.BytesIO):
    def close(self) -> None:
        if not self.closed:
            super().close()
            raise OSError("fictional evidence close failure")


class PrivateReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve() / "task-evidence"
        self.root.mkdir()
        self.body: dict[str, Any] = {
            "schema_version": 1,
            "product": "fictional-odoo",
            "replacement": {
                "product": "fictional-odoo",
                "instance": "prod",
                "strategy": "recreate-in-place",
                "data_source_mode": "empty",
                "allow_empty_data": True,
                "confirmation": "private-confirmation",
                "artifact_id": "fictional-artifact",
                "source_git_ref": "a" * 40,
                "expected_current_artifact_id": "",
            },
        }
        self.response: Any = {
            "status": "accepted",
            "trace_id": "trace-fictional",
            "records": {},
            "result": {
                "product": self.body["product"],
                "instance": "prod",
                "plan_status": "ready",
                "strategy": "recreate-in-place",
                "data_source_mode": "empty",
                "allow_empty_data": True,
                "expected_artifact_id": "fictional-artifact",
                "expected_source_git_ref": "a" * 40,
                "current_target": {"target_name": "private-target", "domains": ["private.invalid"], "volumes": ["private-volume"]},
            },
        }
        self.status = 202
        self.sent: list[dict[str, Any]] = []
        self.raw: bytes | None = None
        self.error: Exception | None = None
        self.enterContext(
            patch.object(helper, "resolve_settings", return_value={"service_url": "https://service.invalid", "token": "fixture"})
        )
        self.enterContext(patch.object(private_io.operator, "request_launchplane_response", side_effect=self.transport))

    def transport(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        if self.error is not None:
            raise self.error
        return FixtureResponse(self.raw if self.raw is not None else json.dumps(self.response).encode(), self.status)

    def invoke(
        self, command: str, *, body: dict[str, Any] | None = None, review: Path | None = None, result: Path | None = None
    ) -> tuple[int, dict[str, Any], Path]:
        request_file = self.root / "request.json"
        request_file.write_text(json.dumps(self.body if body is None else body))
        result = result or self.root / f"result-{len(self.sent)}.json"
        argv = [command, "--payload-file", str(request_file), "--result-file", str(result)]
        if command.endswith("apply"):
            argv += ["--idempotency-key", "fictional-original-key"]
            if review is not None:
                argv += ["--reviewed-plan-file", str(review)]
        output = io.StringIO()
        with redirect_stdout(output):
            code = helper.main(argv)
        return code, json.loads(output.getvalue()), result

    def accept_replacement(self) -> None:
        sent = self.sent[0]["body"]
        self.response = {
            "status": "accepted",
            "result": {
                "operation_id": "operation-fictional",
                "product": sent["product"],
                "instance": "prod",
                "request": sent["replacement"],
                "idempotency_key": "fictional-original-key",
                "status": "queued",
            },
        }

    def test_private_plan_sends_existing_empty_fields_and_saves_full_reply(self) -> None:
        code, public, result = self.invoke("target-replacement-plan-read")
        self.assertEqual(code, 0)
        self.assertEqual(self.sent[0]["body"], self.body)
        self.assertEqual(self.sent[0]["path"], helper.helper_command_path("target-replacement-plan-read"))
        saved = json.loads(result.read_text())
        self.assertEqual(json.loads(base64.b64decode(saved["raw_response_base64"])), self.response)
        self.assertEqual(saved["request"], self.body)
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
        for private in ("private-target", "private.invalid", "private-volume", "private-confirmation", "fictional-original-key"):
            self.assertNotIn(private, json.dumps(public))
        self.assertEqual(len(self.sent), 1)

    def test_apply_requires_exact_ready_review_before_any_request(self) -> None:
        _, _, review = self.invoke("target-replacement-plan-read")
        self.sent.clear()
        changed = copy.deepcopy(self.body)
        changed["replacement"]["reason"] = "changed intent"
        code, _, _ = self.invoke("target-replacement-apply", body=changed, review=review)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.sent, [])

    def test_apply_retains_service_receipt_without_public_private_fields(self) -> None:
        _, _, review = self.invoke("target-replacement-plan-read")
        self.accept_replacement()
        code, public, result = self.invoke("target-replacement-apply", review=review)
        self.assertEqual(code, 0)
        self.assertEqual(public["result"]["operation_id"], self.response["result"]["operation_id"])
        self.assertEqual(self.sent[-1]["idempotency_key"], "fictional-original-key")
        self.assertEqual(self.sent[-1]["path"], helper.helper_command_path("target-replacement-apply"))
        self.assertEqual(json.loads(base64.b64decode(json.loads(result.read_text())["raw_response_base64"])), self.response)
        self.assertNotIn("private-confirmation", json.dumps(public))
        self.assertEqual(len(self.sent), 2)

    def test_policy_apply_carries_reviewed_service_digest_and_exact_intent(self) -> None:
        policy = {
            "schema_version": 1,
            "product": "fictional-odoo",
            "context": "fictional",
            "instance": "prod",
            "enabled": True,
            "approval_issue_url": "https://github.com/example/fictional/issues/1",
            "data_source_mode": "empty",
            "confirmation": "private-confirmation",
            "expected_target_name": "private-target",
            "expected_domains": ["private.invalid"],
            "reason": "Fictional qualification",
        }
        self.response = {"status": "accepted", "result": {**policy, "mode": "dry-run", "plan_sha256": "b" * 64, "applied": False}}
        code, public, review = self.invoke("prelaunch-rebuild-policy-dry-run", body=policy)
        self.assertEqual(code, 0)
        self.assertNotIn("private-target", json.dumps(public))
        self.response = {"status": "accepted", "result": {"mode": "apply", "applied": True}}
        code, _, _ = self.invoke("prelaunch-rebuild-policy-apply", body=policy, review=review)
        self.assertEqual(code, 0)
        self.assertEqual(self.sent[-1]["body"], {**policy, "mode": "apply", "reviewed_plan_sha256": "b" * 64})
        self.assertEqual(self.sent[-1]["path"], helper.helper_command_path("prelaunch-rebuild-policy-apply"))
        self.assertEqual(len(self.sent), 2)

    def test_scope_rejects_unrelated_policy_or_replacement_inputs(self) -> None:
        for replacement in (
            {"production_use": "prelaunch"},
            {"runtime_sync": True},
            {"verify_health": False},
            {"strategy": "replace-and-cutover"},
        ):
            body = copy.deepcopy(self.body)
            body["replacement"].update(replacement)
            code, _, _ = self.invoke("target-replacement-plan-read", body=body)
            self.assertNotEqual(code, 0)
        self.assertEqual(self.sent, [])

    def test_error_and_malformed_responses_are_saved_privately_without_retries(self) -> None:
        for raw, status in (
            (b'{"error":{"code":"authorization_denied","message":"private-target private.invalid"}}', 403),
            (b"private-target malformed", 502),
        ):
            self.raw, self.status = raw, status
            code, public, result = self.invoke("target-replacement-plan-read")
            self.assertNotEqual(code, 0)
            self.assertEqual(base64.b64decode(json.loads(result.read_text())["raw_response_base64"]), raw)
            self.assertNotIn("private-target", json.dumps(public))
        self.assertEqual(len(self.sent), 2)

    def test_existing_result_file_refuses_before_transport(self) -> None:
        result = self.root / "retained.json"
        result.write_text("retained")
        code, _, _ = self.invoke("target-replacement-plan-read", result=result)
        self.assertNotEqual(code, 0)
        self.assertEqual(result.read_text(), "retained")
        self.assertEqual(self.sent, [])

    def test_blocked_cross_service_and_malformed_reviews_refuse_apply(self) -> None:
        _, _, review = self.invoke("target-replacement-plan-read")
        original = json.loads(review.read_text())
        self.sent.clear()
        for field, value in (("service_fingerprint", "different"), ("raw_response_base64", "W10="), ("http_status", 403)):
            changed = {**original, field: value}
            review.write_text(json.dumps(changed))
            code, _, _ = self.invoke("target-replacement-apply", review=review)
            self.assertNotEqual(code, 0)
        for field, value in (("plan_status", "blocked"), ("instance", "testing"), ("data_source_mode", "existing")):
            changed = copy.deepcopy(original)
            response = json.loads(base64.b64decode(changed["raw_response_base64"]))
            response["result"][field] = value
            changed["raw_response_base64"] = base64.b64encode(json.dumps(response).encode()).decode()
            review.write_text(json.dumps(changed))
            code, _, _ = self.invoke("target-replacement-apply", review=review)
            self.assertNotEqual(code, 0)
        self.assertEqual(self.sent, [])

    def test_uncertain_apply_preserves_original_evidence_and_never_retries(self) -> None:
        _, _, review = self.invoke("target-replacement-plan-read")
        self.error = TimeoutError("private-target")
        code, public, result = self.invoke("target-replacement-apply", review=review)
        self.assertEqual(code, 1)
        self.assertEqual(public["status"], "outcome_unknown")
        self.assertEqual(json.loads(result.read_text())["request"], self.body)
        self.assertNotIn("private-target", json.dumps(public))
        self.assertEqual(len(self.sent), 2)

    def test_no_candidate_plan_can_review_the_current_artifact(self) -> None:
        del self.body["replacement"]["artifact_id"]
        del self.body["replacement"]["source_git_ref"]
        self.body["replacement"]["expected_current_artifact_id"] = "current-artifact"
        self.response["result"].update(expected_artifact_id="current-artifact", expected_source_git_ref="b" * 40)
        self.status = 200
        _, _, review = self.invoke("target-replacement-plan-read")
        self.accept_replacement()
        code, _, _ = self.invoke("target-replacement-apply", review=review)
        self.assertEqual(code, 0)

    def test_policy_review_mismatch_and_unapplied_receipts_do_not_report_success(self) -> None:
        policy = {
            "product": "fictional-odoo",
            "context": "fictional",
            "instance": "prod",
            "enabled": False,
            "reason": "Fictional policy",
        }
        self.response = {"status": "accepted", "result": {"mode": "dry-run", "applied": False, "plan_sha256": "c" * 64}}
        _, _, review = self.invoke("prelaunch-rebuild-policy-dry-run", body=policy)
        before = len(self.sent)
        code, _, _ = self.invoke("prelaunch-rebuild-policy-apply", body={**policy, "context": "other"}, review=review)
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.sent), before)
        self.response = {"status": "accepted", "result": {"mode": "dry-run", "applied": False}}
        code, public, _ = self.invoke("prelaunch-rebuild-policy-apply", body=policy, review=review)
        self.assertNotEqual(code, 0)
        self.assertEqual(public["status"], "outcome_unknown")

    def test_misaligned_apply_receipt_is_unknown_and_private_raw_reply_survives(self) -> None:
        _, _, review = self.invoke("target-replacement-plan-read")
        self.raw = b'{"status":"accepted", "result":{"operation_id":"private-target"}, "duplicate":1,"duplicate":2}'
        code, public, result = self.invoke("target-replacement-apply", review=review)
        self.assertNotEqual(code, 0)
        self.assertEqual(public["status"], "outcome_unknown")
        self.assertEqual(base64.b64decode(json.loads(result.read_text())["raw_response_base64"]), self.raw)
        self.assertNotIn("private-target", json.dumps(public))

    def test_no_candidate_apply_requires_the_reviewed_current_artifact_cas(self) -> None:
        del self.body["replacement"]["artifact_id"]
        del self.body["replacement"]["source_git_ref"]
        self.response["result"]["expected_artifact_id"] = "current-artifact"
        _, _, review = self.invoke("target-replacement-plan-read")
        code, _, _ = self.invoke("target-replacement-apply", review=review)
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.sent), 1)

    def test_normalized_confirmation_and_instance_match_the_producer_receipt(self) -> None:
        self.body["replacement"].update(confirmation=" PRIVATE-CONFIRMATION ", instance=" PROD ")
        _, _, review = self.invoke("target-replacement-plan-read")
        sent = self.sent[0]["body"]
        self.assertEqual(sent["replacement"]["confirmation"], "private-confirmation")
        self.accept_replacement()
        code, _, _ = self.invoke("target-replacement-apply", review=review)
        self.assertEqual(code, 0)

    def test_apply_http_and_evidence_failures_preserve_unknown_without_retries(self) -> None:
        _, _, review = self.invoke("target-replacement-plan-read")
        for status in (408, 503):
            self.status = status
            before = len(self.sent)
            code, public, _ = self.invoke("target-replacement-apply", review=review)
            self.assertEqual(code, 1)
            self.assertEqual(public["status"], "outcome_unknown")
            self.assertEqual(len(self.sent), before + 1)
        self.status = 202
        self.accept_replacement()
        for target, replacement in (("save_evidence", lambda *args: False), ("open_evidence", lambda *args: ClosingFailure())):
            before = len(self.sent)
            with patch.object(private_io, target, side_effect=replacement):
                code, public, _ = self.invoke("target-replacement-apply", review=review)
            self.assertEqual(code, 1)
            self.assertEqual(public["status"], "outcome_unknown")
            self.assertEqual(len(self.sent), before + 1)


if __name__ == "__main__":
    unittest.main()
