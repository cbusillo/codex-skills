#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0"]
# ///
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from test_launchplane_helpers import _run_main, _write_json

PLAN = "b" * 64
KEY = "release-opw-1"
OPERATION = "odoo-target-replacement-opw-testing-1"


def argv(mode: str = "dry-run", evidence: str = "") -> list[str]:
    args = [
        "testing-candidate-release-" + mode,
        "--product",
        "opw",
        "--operation-id",
        OPERATION,
    ]
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
    body = {
        "trace_id": "trace-test",
        "error": {"code": code, "message": "PRIVATE_SENTINEL"},
    }
    return urllib.error.HTTPError(
        "https://plane.example.invalid",
        status,
        "Failure",
        Message(),
        io.BytesIO(json.dumps(body).encode()),
    )


def invoke(args: list[str], reply: object = None) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    code, output, posts, _ = _run_main(args, post=reply)
    return code, output, posts


def reviewed_evidence(tmp_path: Path) -> str:
    code, dry, calls = invoke(argv(), response())
    assert code == 0 and calls[0]["body"] == {"mode": "dry-run"}
    assert calls[0]["path"] == f"/v1/products/opw/testing-rebuild/{OPERATION}/candidate-release"
    assert dry["result"]["reclaimable_bytes"] == 3500
    return _write_json(tmp_path, "dry.json", dry)


def test_reviewed_dry_run_binds_apply_and_readback(tmp_path: Path) -> None:
    evidence = reviewed_evidence(tmp_path)
    code, applied, calls = invoke(argv("apply", evidence), response("released"))
    assert code == 0 and applied["result"]["status"] == "released"
    assert calls[0]["body"] == {"mode": "apply", "reviewed_plan_sha256": PLAN}
    assert calls[0]["idempotency_key"] == KEY
    code, readback, _ = invoke(argv("readback"), response("released", replayed=True))
    assert code == 0 and readback["result"]["replayed"] is True


def test_apply_without_matching_review_never_reaches_the_service(
    tmp_path: Path,
) -> None:
    for evidence in ("", str(tmp_path / "missing.json")):
        code, result, calls = invoke(argv("apply", evidence))
        assert code != 0 and result["status"] == "invalid" and not calls
    dry = json.loads(Path(reviewed_evidence(tmp_path)).read_text())
    dry["result"]["status"] = "absent"
    changed = _write_json(tmp_path, "absent.json", dry)
    code, result, calls = invoke(argv("apply", changed))
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
        (failure("timeout", 408), "outcome_unknown"),
        (failure("candidate_release_unavailable"), "outcome_unknown"),
        (urllib.error.URLError("dropped"), "outcome_unknown"),
    ],
)
def test_apply_outcomes_map_to_their_recovery(tmp_path: Path, error: Exception, expected: str) -> None:
    code, result, _ = invoke(argv("apply", reviewed_evidence(tmp_path)), error)
    assert code != 0 and result["status"] == expected
    assert "PRIVATE_SENTINEL" not in json.dumps(result)


@pytest.mark.parametrize(
    "error",
    [
        failure("bad_gateway", 502),
        failure("candidate_release_unavailable"),
        failure("testing_in_use"),
    ],
)
def test_readback_errors_preserve_uncertain_outcomes(error: Exception) -> None:
    code, result, _ = invoke(argv("readback"), error)
    assert code != 0 and result["status"] == "outcome_unknown"


def test_overlong_key_is_rejected_before_service_call() -> None:
    args = argv("readback")
    args[-1] = "k" * 201
    code, _, calls = invoke(args)
    assert code != 0 and not calls


def test_response_for_another_operation_or_plan_is_not_success(tmp_path: Path) -> None:
    evidence = reviewed_evidence(tmp_path)
    for reply in (
        response("released", operation_id="other"),
        response("released", plan_sha256="c" * 64),
        response(),
    ):
        code, result, _ = invoke(argv("apply", evidence), reply)
        assert code != 0 and result["status"] == "outcome_unknown"


def test_receiver_binds_review_and_original_readback(tmp_path: Path) -> None:
    receiver = ["--receiver-operation-id", "odoo-testing-receiver-1"]
    code, dry, calls = invoke(argv() + receiver, response())
    assert code == 0
    assert calls[0]["body"]["receiver_operation_id"] == receiver[1]
    saved = _write_json(tmp_path, "receiver.json", dry)
    code, _, calls = invoke(argv("apply", saved))
    assert code != 0 and not calls
    for mode in ("apply", "readback"):
        code, _, calls = invoke(argv(mode, saved) + receiver, response("released"))
        assert code == 0
        assert calls[0]["body"]["receiver_operation_id"] == receiver[1]
        assert calls[0]["idempotency_key"] == KEY


def test_held_plan_surfaces_evidence_and_cannot_apply(tmp_path: Path) -> None:
    digest = "a" * 64
    resource = {
        "role": "filestore",
        "volume_identity_sha256": digest,
        "status": "held",
        "reason": "staff_testing_hold",
        "backing_sha256": digest,
        "free_bytes": 10,
        "private_path": "/private/volumes/one",
    }
    candidate = {
        "namespace": "lp-testing-" + "a" * 32,
        "operation_id": OPERATION,
        "product": "opw",
        "context": "opw",
        "instance": "testing",
        "target_id": "target-1",
        "selected": True,
        "status": "held",
        "reasons": ["staff_testing_hold"],
        "ownership_sha256": digest,
        "source_evidence_sha256": "",
        "provider_evidence_sha256": digest,
        "lifecycle_snapshot_sha256": digest,
        "container_count": 1,
        "resources": [resource],
        "physical_readback": {digest: {"before_bytes": 12, "after_bytes": 10, "observed_change_bytes": -2}},
    }
    capacity = {
        "backing_sha256": digest,
        "free_bytes": 10,
        "required_additional_bytes": 14,
        "reserve_bytes": 3,
        "deficit_bytes": 7,
    }
    absent = {**resource, "volume_identity_sha256": "", "backing_sha256": "", "status": "absent"}
    absent.pop("private_path")
    unallocated = {
        **candidate,
        "operation_id": "another-original",
        "target_id": "",
        "selected": False,
        "resources": [absent],
        "physical_readback": {},
    }
    reply = response(
        "held",
        candidates=[candidate, unallocated],
        capacity=[capacity],
        capacity_refusal="receiver_candidate_unavailable",
        reclaimable_bytes=None,
    )
    _, dry, _ = invoke(argv(), reply)
    projected = dry["result"]
    assert projected["status"] == "held"
    assert projected["reclaimable_bytes"] is None
    assert projected["capacity"] == [capacity]
    assert projected["capacity_refusal"] == "receiver_candidate_unavailable"
    assert projected["candidates"][0] == {
        **candidate,
        "resources": [
            {key: value for key, value in resource.items() if key != "private_path"},
        ],
    }
    assert projected["candidates"][1] == unallocated
    candidate["physical_readback"] = {digest: {}}
    dry_unknown = invoke(argv(), reply)[1]
    assert dry_unknown["result"]["candidates"][0]["physical_readback"] == {
        digest: {"reason": "backing_readback_unknown"},
    }
    saved = _write_json(tmp_path, "held.json", dry)
    code, _, calls = invoke(argv("apply", saved))
    assert code != 0 and not calls
    code, readback, _ = invoke(argv("readback"), reply)
    assert code != 0 and readback["status"] == "held"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
