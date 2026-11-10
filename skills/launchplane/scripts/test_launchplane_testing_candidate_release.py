#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0"]
# ///
"""Candidate release apply binds a reviewed dry run and maps every service outcome."""
import io
import json
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from test_launchplane_helpers import write_action as h

PLAN = "b" * 64
KEY = "release-opw-1"
OPERATION = "odoo-target-replacement-opw-testing-1"


def argv(mode: str = "dry-run", evidence: str = "") -> list[str]:
    args = ["testing-candidate-release-" + mode, "--product", "opw", "--operation-id", OPERATION]
    if mode != "dry-run":
        args += ["--expected-plan-digest", PLAN, "--idempotency-key", KEY]
    if mode == "apply":
        args += ["--reviewed-dry-run", "--dry-run-evidence-file", evidence]
    return args


def response(status: str = "eligible", **changes: object) -> dict[str, object]:
    return {
        "trace_id": "trace-test",
        "product": "opw",
        "operation_id": OPERATION,
        "error_code": "candidate_preparation_failed",
        "plan_sha256": PLAN,
        "status": status,
        "reclaimable_bytes": 3500,
        **changes,
    }


def failure(code: str, status: int = 409) -> urllib.error.HTTPError:
    body = {"trace_id": "trace-test", "error": {"code": code, "message": "PRIVATE_SENTINEL"}}
    return urllib.error.HTTPError("https://plane.example.invalid", status, "Failure", {}, io.BytesIO(json.dumps(body).encode()))


def invoke(args: list[str], replies: list[object]) -> tuple[int, dict, list]:
    output = io.StringIO()
    with patch.object(h, "prepare_operator_settings", return_value={"service_url": "https://plane.example.invalid"}), patch.object(h, "request_launchplane", side_effect=replies) as post, redirect_stdout(output):
        code = h.main(args)
    return code, json.loads(output.getvalue()), post.call_args_list


def reviewed_evidence(tmp_path: Path) -> str:
    code, dry, calls = invoke(argv(), [response()])
    assert code == 0 and calls[0].kwargs["body"] == {"mode": "dry-run"}
    assert calls[0].kwargs["path"] == f"/v1/products/opw/testing-rebuild/{OPERATION}/candidate-release"
    assert dry["result"]["reclaimable_bytes"] == 3500
    evidence = tmp_path / "dry.json"
    evidence.write_text(json.dumps(dry))
    return str(evidence)


def test_reviewed_dry_run_binds_apply_and_readback(tmp_path: Path) -> None:
    evidence = reviewed_evidence(tmp_path)
    code, applied, calls = invoke(argv("apply", evidence), [response("released")])
    assert code == 0 and applied["result"]["status"] == "released"
    assert calls[0].kwargs["body"] == {"mode": "apply", "reviewed_plan_sha256": PLAN}
    assert calls[0].kwargs["idempotency_key"] == KEY
    code, readback, _ = invoke(argv("readback"), [response("released", replayed=True)])
    assert code == 0 and readback["result"]["replayed"] is True


def test_apply_without_matching_review_never_reaches_the_service(tmp_path: Path) -> None:
    for evidence in ("", str(tmp_path / "missing.json")):
        code, result, calls = invoke(argv("apply", evidence), [])
        assert code != 0 and result["status"] == "invalid" and not calls
    dry = json.loads(Path(reviewed_evidence(tmp_path)).read_text())
    dry["result"]["status"] = "absent"
    changed = tmp_path / "absent.json"
    changed.write_text(json.dumps(dry))
    code, result, calls = invoke(argv("apply", str(changed)), [])
    assert code != 0 and result["status"] == "invalid" and not calls


@pytest.mark.parametrize(
    "error,expected",
    [
        (failure("testing_in_use"), "refused"),
        (failure("candidate_release_selected"), "refused"),
        (failure("candidate_release_plan_changed"), "stale_plan"),
        (failure("idempotency_key_reused"), "conflict"),
        (failure("mutation_reconciliation_required"), "outcome_unknown"),
        (failure("internal", 502), "outcome_unknown"),
        (urllib.error.URLError("dropped"), "outcome_unknown"),
    ],
)
def test_apply_outcomes_map_to_their_recovery(tmp_path: Path, error: Exception, expected: str) -> None:
    code, result, _ = invoke(argv("apply", reviewed_evidence(tmp_path)), [error])
    assert code != 0 and result["status"] == expected
    assert "PRIVATE_SENTINEL" not in json.dumps(result)


def test_response_for_another_operation_or_plan_is_not_success(tmp_path: Path) -> None:
    evidence = reviewed_evidence(tmp_path)
    for reply in (response("released", operation_id="other"), response("released", plan_sha256="c" * 64)):
        code, result, _ = invoke(argv("apply", evidence), [reply])
        assert code != 0 and result["status"] == "outcome_unknown"
