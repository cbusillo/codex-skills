# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0"]
# ///

import copy
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from test_launchplane_helpers import SETTINGS, _reviewed_apply_argv, _run_main, _write_json
from test_launchplane_helpers import write_action as h

PREFIX = "dokploy-target-disable-empty-compose-auto-deploy-"
REVIEW: dict[str, Any] = {"observed_auto_deploy": True, "observed_at": "2026-11-11T17:30:00.123456Z", "plan_sha256": "a" * 64}


def private_payload() -> dict[str, Any]:
    return {
        "context": "fictional-site",
        "instance": "prod",
        "target_id": "fictional-compose",
        "project_id": "fictional-project",
        "environment_id": "fictional-environment",
        "server_id": "fictional-server",
        "expected_current_provider_target": {
            "provider_id": "dokploy",
            "target_id": "fictional-compose",
            "display_name": "PRIVATE_DISPLAY",
            "target_category": "compose",
            "provider_target_type": "compose",
            "provider_evidence": {},
        },
        "reason": "PRIVATE_REASON",
    }


def response(mode: str = "dry-run", *, replayed: bool = False, observed: bool = True) -> dict[str, Any]:
    value: dict[str, Any] = {
        "status": "accepted",
        "trace_id": "launchplane_req_fixture_current",
        "records": {},
        "result": {
            "mode": "dry-run" if mode == "dry-run" else "apply",
            "operation": "disable-empty-compose-auto-deploy",
            "context": "fictional-site",
            "instance": "prod",
            "applied": mode != "dry-run",
            "route_domain_ids": [],
            "setup": {
                "automatic_deployment_disabled": mode != "dry-run" or not observed,
                "automation_review": {**REVIEW, "observed_auto_deploy": observed},
            },
        },
    }
    if replayed:
        value.update(replayed=True, original_trace_id="launchplane_req_fixture_original")
    return value


def dry_run(tmp_path: Path, payload: dict[str, Any] | None = None) -> tuple[str, str, dict[str, Any]]:
    path = _write_json(tmp_path, "request.json", private_payload() if payload is None else payload)
    code, evidence, posts, reads = _run_main([PREFIX + "dry-run", "--payload-file", path], post=response())
    assert code == 0, evidence
    assert len(posts) == 1 and reads == []
    assert posts[0]["body"]["operation"] == response()["result"]["operation"]
    assert posts[0]["body"]["product"] == "launchplane"
    assert posts[0]["idempotency_key"] == ""
    assert not {"confirmation", "automation_review"} & posts[0]["body"].keys()
    return path, _write_json(tmp_path, "review.json", evidence), evidence


def invoke(mode: str, path: str, review_path: str, **kwargs: Any) -> tuple[Any, ...]:
    arguments = _reviewed_apply_argv(REVIEW["plan_sha256"], review_path, key="original-correction")
    if mode == "readback":
        arguments.remove("--reviewed-dry-run")
    return _run_main([PREFIX + mode, "--payload-file", path, *arguments], **kwargs)


def test_correction_preserves_original_review_and_readback_never_mutates(tmp_path: Path) -> None:
    path, review_path, evidence = dry_run(tmp_path)
    assert evidence["result"]["automation_review"] == REVIEW
    for mode in ("apply", "readback"):
        code, receipt, posts, reads = invoke(mode, path, review_path, post=response(mode))
        assert code == 0, receipt
        assert len(posts) == 1 and reads == []
        body = posts[0]["body"]
        assert body["mode"] == mode and body["automation_review"] == REVIEW
        assert posts[0]["idempotency_key"] == "original-correction"
        assert {key: body[key] for key in private_payload()} == private_payload()
        assert receipt["result"]["automatic_deployment_disabled"] is True
        assert receipt["result"]["automation_review"]["observed_auto_deploy"] is True
        if mode == "apply":
            assert body["confirmation"] == h.DOKPLOY_TARGET_SETUP_CONFIRMATION
        else:
            assert "confirmation" not in body
        printed = json.dumps(receipt)
        for private in (
            "PRIVATE_DISPLAY",
            "PRIVATE_REASON",
            "fictional-compose",
            "fictional-project",
            "fictional-environment",
            "fictional-server",
            "provider_evidence",
            "service_fingerprint",
        ):
            assert private not in printed


@pytest.mark.parametrize(
    "field,value",
    [
        ("instance", "testing"),
        ("target_type", "application"),
        ("project_id", ""),
        ("expected_current_provider_target", {}),
        ("automation_review", REVIEW),
        ("autoDeploy", False),
        ("env", "PRIVATE=secret"),
    ],
)
def test_invalid_correction_inputs_never_reach_transport(tmp_path: Path, field: str, value: Any) -> None:
    body = {**private_payload(), field: value}
    path = _write_json(tmp_path, "invalid.json", body)
    code, output, posts, reads = _run_main([PREFIX + "dry-run", "--payload-file", path], post=response())
    assert code != 0 and not posts and not reads, output


@pytest.mark.parametrize(
    "field", ["context", "target_id", "project_id", "environment_id", "server_id", "reason", "expected_current_provider_target"]
)
def test_changed_private_intent_cannot_use_prior_review(tmp_path: Path, field: str) -> None:
    path, review_path, _ = dry_run(tmp_path)
    changed = private_payload()
    changed[field] = {**changed[field], "display_name": "OTHER"} if isinstance(changed[field], dict) else "other"
    _write_json(tmp_path, "request.json", changed)
    code, output, posts, reads = invoke("apply", path, review_path, post=response("apply"))
    assert code != 0 and not posts and not reads, output


@pytest.mark.parametrize(
    "field,value", [("context", "fictional-site "), ("instance", "testing"), ("operation", "create-compose"), ("mode", "readback")]
)
def test_raw_result_identity_is_checked_before_safe_projection(tmp_path: Path, field: str, value: str) -> None:
    path, review_path, _ = dry_run(tmp_path)
    bad = response("apply")
    bad["result"][field] = value
    code, output, posts, _ = invoke("apply", path, review_path, post=bad)
    assert code != 0 and len(posts) == 1, output


@pytest.mark.parametrize("field", ["applied", "automatic_deployment_disabled", "observed_auto_deploy"])
@pytest.mark.parametrize("value", [None, "false", 0, 1])
def test_result_and_review_booleans_cannot_be_coerced(tmp_path: Path, field: str, value: Any) -> None:
    path, review_path, _ = dry_run(tmp_path)
    bad = response("apply")
    location = bad["result"] if field == "applied" else bad["result"]["setup"]
    if field == "observed_auto_deploy":
        location = location["automation_review"]
    location[field] = value
    code, output, posts, _ = invoke("apply", path, review_path, post=bad)
    assert code != 0 and len(posts) == 1, output
    del location[field]
    code, output, posts, _ = invoke("apply", path, review_path, post=bad)
    assert code != 0 and len(posts) == 1, output


@pytest.mark.parametrize(
    "field,value", [("observed_auto_deploy", False), ("observed_at", "2026-11-11T17:31:00+00:00"), ("plan_sha256", "b" * 64)]
)
def test_returned_review_must_match_the_whole_original(tmp_path: Path, field: str, value: Any) -> None:
    path, review_path, _ = dry_run(tmp_path)
    bad = response("readback")
    bad["result"]["setup"]["automation_review"][field] = value
    code, output, posts, _ = invoke("readback", path, review_path, post=bad)
    assert code != 0 and len(posts) == 1, output


def test_completed_replay_is_stored_evidence_without_current_state_claim(tmp_path: Path) -> None:
    path, review_path, _ = dry_run(tmp_path)
    for mode in ("apply", "readback"):
        code, receipt, posts, reads = invoke(mode, path, review_path, post=response(mode, replayed=True))
        assert code == 0 and len(posts) == 1 and not reads
        assert receipt["result"]["replayed"] is True
        assert receipt["result"]["current_state_verified"] is False
        assert receipt["summary"]["original_trace_id"] == response(mode, replayed=True)["original_trace_id"]


def test_expired_original_review_readback_and_false_noop_follow_service_contract(tmp_path: Path) -> None:
    path, review_path, _ = dry_run(tmp_path)
    code, receipt, posts, _ = invoke("readback", path, review_path, post=response("readback"))
    assert code == 0 and len(posts) == 1, receipt
    path = _write_json(tmp_path, "request.json", private_payload())
    code, evidence, posts, _ = _run_main([PREFIX + "dry-run", "--payload-file", path], post=response(observed=False))
    assert code == 0 and evidence["result"]["applied"] is False
    assert evidence["result"]["automatic_deployment_disabled"] is True


def test_uncertainty_and_service_refusals_keep_original_correction_without_retry(tmp_path: Path) -> None:
    path, review_path, _ = dry_run(tmp_path)
    errors: list[Exception] = [TimeoutError("PRIVATE_HOST")]
    for status, code in (
        (409, "mutation_reconciliation_required"),
        (409, "mutation_in_progress"),
        (409, "idempotency_key_reused"),
        (409, "invalid_dokploy_target_setup"),
        (503, "dokploy_target_evidence_unavailable"),
        (403, "authorization_denied"),
    ):
        errors.append(
            urllib.error.HTTPError(
                "https://private.invalid",
                status,
                "PRIVATE",
                Message(),
                io.BytesIO(
                    json.dumps(
                        {
                            "status": "error",
                            "trace_id": "launchplane_req_fixture_failure",
                            "error": {"code": code, "message": "PRIVATE_HOST"},
                        }
                    ).encode()
                ),
            )
        )
    for error in errors:
        code, output, posts, reads = invoke("apply", path, review_path, post=error)
        assert code != 0 and len(posts) == 1 and not reads, output
        assert "PRIVATE_HOST" not in json.dumps(output)
        assert posts[0]["idempotency_key"] == "original-correction"


def test_unpublished_fields_never_leak_and_edited_evidence_cannot_authorize(tmp_path: Path) -> None:
    path, review_path, evidence = dry_run(tmp_path)
    for location in ("outer", "result", "setup", "review"):
        bad = response("apply")
        target = bad if location == "outer" else bad["result"]
        if location in {"setup", "review"}:
            target = target["setup"]
        if location == "review":
            target = target["automation_review"]
        target["private"] = {"env": "PRIVATE_SECRET", "composeFile": "PRIVATE_SOURCE", "mounts": ["PRIVATE_MOUNT"]}
        code, output, posts, _ = invoke("apply", path, review_path, post=bad)
        assert code != 0 and len(posts) == 1, output
        assert "PRIVATE_" not in json.dumps(output)
    edited = copy.deepcopy(evidence)
    edited["result"]["automation_review"]["observed_auto_deploy"] = False
    _write_json(tmp_path, "review.json", edited)
    code, output, posts, _ = invoke("apply", path, review_path, post=response("apply"))
    assert code != 0 and not posts, output


@pytest.mark.parametrize("changed", ["service", "acknowledgement", "naive_time", "extra_review", "reference_type"])
def test_review_custody_and_closed_schema_are_enforced_before_dispatch(tmp_path: Path, changed: str) -> None:
    path, review_path, evidence = dry_run(tmp_path)
    arguments = [PREFIX + "apply", "--payload-file", path, *_reviewed_apply_argv(REVIEW["plan_sha256"], review_path)]
    settings = dict(SETTINGS)
    if changed == "service":
        settings["service_url"] = "https://other.example.invalid"
    elif changed == "acknowledgement":
        arguments.remove("--reviewed-dry-run")
    elif changed == "reference_type":
        private = private_payload()
        private["expected_current_provider_target"]["target_category"] = {}
        _write_json(tmp_path, "request.json", private)
    else:
        review = evidence["result"]["automation_review"]
        if changed == "naive_time":
            review["observed_at"] = "2026-11-11T17:30:00"
        else:
            review["provider"] = "PRIVATE_PROVIDER"
        _write_json(tmp_path, "review.json", evidence)
    code, output, posts, reads = _run_main(arguments, post=response("apply"), settings=settings)
    assert code != 0 and not posts and not reads, output


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "ok"},
        {"records": {"target": "PRIVATE"}},
        {"replayed": 1},
        {"trace_id": " launchplane_req_fixture_current"},
        {"replayed": True, "original_trace_id": " launchplane_req_fixture_current"},
    ],
)
def test_malformed_completion_envelope_cannot_claim_success(tmp_path: Path, updates: dict[str, Any]) -> None:
    path, review_path, _ = dry_run(tmp_path)
    bad = {**response("apply"), **updates}
    code, output, posts, reads = invoke("apply", path, review_path, post=bad)
    assert code != 0 and len(posts) == 1 and not reads, output


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
