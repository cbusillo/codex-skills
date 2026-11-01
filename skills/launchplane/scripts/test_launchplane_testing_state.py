# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///

import copy
import hashlib
import io
import json
import stat
import sys
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import pytest
from test_launchplane_helpers import write_action as helper


class Invocation(NamedTuple):
    code: int
    output: dict[str, Any]
    post: Any
    read: Any
    settings: Any


def profile() -> dict[str, Any]:
    return {
        "product": "example",
        "driver_id": "generic-web",
        "lifecycle_state": "active",
        "display_name": "Café PRIVATE_SENTINEL",
        "production_use": "business",
        "image": {"repository": "registry.example/site"},
        "lanes": [
            {"context": "example", "instance": "testing", "base_url": "https://testing.example.invalid"},
            {"context": "example", "instance": "prod", "base_url": "https://example.invalid"},
        ],
    }


def response(value: dict[str, Any]) -> Any:
    result = helper._ServiceResponse({"trace_id": "launchplane_req_profile", "profile": value}, 200)
    result.etag = (
        '"' + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest() + '"'
    )
    return result


def plan_args(path: Path, kind: str = "none") -> list[str]:
    return [
        "testing-state-dry-run",
        "--product",
        "example",
        "--context",
        "example",
        "--kind",
        kind,
        "--reason",
        "Declare the disposable testing application's state",
        "--profile-request-file",
        str(path),
    ] + (["--container-path", "/app/data"] if kind == "volume" else [])


def invoke(args: list[str], *, read_body: Any = None, post_error: Any = None, read_error: Any = None) -> Invocation:
    output = io.StringIO()
    fixture_repo = Path(args[args.index("--profile-request-file") + 1]).parent / "repo"
    with (
        patch.object(helper, "active_repo_root", return_value=fixture_repo),
        patch.object(
            helper,
            "prepare_operator_settings",
            return_value={"service_url": "https://plane.example.invalid", "token": "PRIVATE_SENTINEL"},
        ) as settings,
        patch.object(helper, "request_launchplane_read", return_value=read_body, side_effect=read_error) as read,
        patch.object(
            helper,
            "request_launchplane",
            return_value={"status": "accepted", "trace_id": "launchplane_req_saved", "records": {"product_profile": "example"}},
            side_effect=post_error,
        ) as post,
        redirect_stdout(output),
    ):
        code = helper.main(args)
    return Invocation(code, json.loads(output.getvalue()), post, read, settings)


def reviewed_args(path: Path, planned: dict[str, Any]) -> list[str]:
    evidence = path.with_name("review.json")
    evidence.write_text(json.dumps(planned))
    return [
        "testing-state-apply",
        "--profile-request-file",
        str(path),
        "--idempotency-key",
        "original-state-key",
        "--reviewed-dry-run",
        "--expected-plan-digest",
        planned["result"]["plan_sha256"],
        "--dry-run-evidence-file",
        str(evidence),
    ]


@pytest.mark.parametrize("kind", ["none", "volume"])
def test_state_apply_preserves_neighbors_and_original_conditional_request(tmp_path: Path, kind: str) -> None:
    original = profile()
    current = response(original)
    path = tmp_path / "private-request.json"
    planned = invoke(plan_args(path, kind), read_body=current)
    assert planned.code == 0
    planned.post.assert_not_called()
    assert path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
    expected = copy.deepcopy(original)
    expected["lanes"][0]["generic_web_testing_state"] = {"kind": kind, "container_path": "/app/data" if kind == "volume" else ""}
    applied = invoke(reviewed_args(path, planned.output), read_body=response(expected))
    assert applied.code == 0, applied.output
    applied.post.assert_called_once()
    sent = applied.post.call_args.kwargs
    assert sent["body"] == expected
    assert sent["if_match"] == current.etag
    assert sent["idempotency_key"] == "original-state-key"
    assert applied.output["result"]["read_back_matches"] is True
    assert "PRIVATE_SENTINEL" not in json.dumps([planned.output, applied.output])


def test_profile_snapshot_tampering_cannot_change_a_neighbor(tmp_path: Path) -> None:
    path = tmp_path / "private-request.json"
    planned = invoke(plan_args(path), read_body=response(profile()))
    assert planned.code == 0
    staged = json.loads(path.read_text())
    staged["profile"]["production_use"] = "different"
    path.write_text(json.dumps(staged))
    applied = invoke(reviewed_args(path, planned.output))
    assert applied.code != 0
    applied.settings.assert_not_called()
    applied.post.assert_not_called()
    applied.read.assert_not_called()


def test_uncertain_state_write_replays_exact_staged_body_without_a_new_profile(tmp_path: Path) -> None:
    path = tmp_path / "private-request.json"
    original = profile()
    planned = invoke(plan_args(path), read_body=response(original))
    assert planned.code == 0
    args = reviewed_args(path, planned.output)
    uncertain = invoke(args, post_error=TimeoutError("PRIVATE_SENTINEL"))
    assert uncertain.output["status"] == "outcome_unknown"
    uncertain.post.assert_called_once()
    uncertain.read.assert_not_called()
    replayed = invoke(args, read_body=response(original))
    assert replayed.post.call_args.kwargs == uncertain.post.call_args.kwargs
    assert replayed.output["status"] == "accepted_unverified"
    assert replayed.output["result"]["read_back_matches"] is False
    assert "PRIVATE_SENTINEL" not in json.dumps(uncertain.output)


def test_state_plan_requires_a_matching_strong_profile_revision(tmp_path: Path) -> None:
    current = response(profile())
    current.etag = "W/" + current.etag
    path = tmp_path / "private-request.json"
    planned = invoke(plan_args(path), read_body=current)
    assert planned.code != 0
    assert not path.exists()
    planned.post.assert_not_called()


def test_state_plan_never_overwrites_existing_request_evidence(tmp_path: Path) -> None:
    path = tmp_path / "private-request.json"
    path.write_text("existing evidence")
    planned = invoke(plan_args(path), read_body=response(profile()))
    assert planned.code != 0
    assert path.read_text() == "existing evidence"
    planned.post.assert_not_called()


def test_existing_state_is_observed_without_a_profile_write(tmp_path: Path) -> None:
    current = profile()
    current["lanes"][0]["generic_web_testing_state"] = {"kind": "none", "container_path": ""}
    path = tmp_path / "private-request.json"
    planned = invoke(plan_args(path), read_body=response(current))
    assert planned.code == 0
    assert planned.output["result"]["changed"] is False
    applied = invoke(reviewed_args(path, planned.output), read_body=response(current))
    assert applied.code == 0
    assert applied.output["status"] == "unchanged"
    assert applied.output["result"]["applied"] is False
    applied.post.assert_not_called()
    applied.read.assert_called_once()


@pytest.mark.parametrize("status", [403, 412, 500])
def test_state_write_refusals_and_uncertainty_keep_trace_without_retry(tmp_path: Path, status: int) -> None:
    path = tmp_path / "private-request.json"
    planned = invoke(plan_args(path), read_body=response(profile()))
    assert planned.code == 0
    trace = "launchplane_req_state_refusal"
    error = urllib.error.HTTPError(
        "https://plane.example.invalid",
        status,
        "PRIVATE_SENTINEL",
        {},
        io.BytesIO(
            json.dumps(
                {
                    "trace_id": trace,
                    "error": {
                        "code": "authorization_denied" if status == 403 else "product_profile_changed",
                        "message": "PRIVATE_SENTINEL",
                    },
                }
            ).encode()
        ),
    )
    applied = invoke(reviewed_args(path, planned.output), post_error=error)
    assert applied.code != 0
    assert (applied.output["status"] == "outcome_unknown") is (status == 500)
    assert applied.output["summary"]["trace_id"] == trace
    assert applied.output["summary"]["http_status"] == status
    assert "PRIVATE_SENTINEL" not in json.dumps(applied.output)
    applied.post.assert_called_once()
    applied.read.assert_not_called()


def test_transport_keeps_the_strong_revision_header_without_putting_it_in_json() -> None:
    current = response(profile())
    wire = MagicMock()
    wire.__enter__.return_value = wire
    wire.status = current.http_status
    wire.headers = {"ETag": current.etag}
    wire.read.return_value = json.dumps(dict(current)).encode()
    settings = {"token": "fixture-only"}
    with patch.object(helper, "safe_urlopen", return_value=wire) as transport:
        observed = helper.request_launchplane_read(
            service_url="https://plane.example.invalid", path="/v1/product-profiles/example", settings=settings, query={}, timeout=10
        )
        assert observed.etag == current.etag
        assert "etag" not in observed
        helper.request_launchplane(
            service_url="https://plane.example.invalid",
            path="/v1/product-profiles",
            settings=settings,
            body=profile(),
            timeout=10,
            idempotency_key="original",
            if_match=observed.etag,
        )
        sent = transport.call_args.args[0]
        assert sent.get_header("If-match") == current.etag
        assert json.loads(sent.data) == profile()


def test_readback_denial_does_not_discard_an_accepted_profile_write(tmp_path: Path) -> None:
    path = tmp_path / "private-request.json"
    planned = invoke(plan_args(path), read_body=response(profile()))
    trace = "launchplane_req_readback_denied"
    error = urllib.error.HTTPError(
        "https://plane.example.invalid",
        403,
        "PRIVATE_SENTINEL",
        {},
        io.BytesIO(
            json.dumps({"trace_id": trace, "error": {"code": "authorization_denied", "message": "PRIVATE_SENTINEL"}}).encode()
        ),
    )
    applied = invoke(reviewed_args(path, planned.output), read_error=error)
    assert applied.output["status"] == "accepted_unverified"
    assert applied.output["result"]["applied"] is True
    assert applied.output["result"]["read_back_matches"] is False
    assert applied.output["summary"]["read_back_status"] == "denied"
    assert applied.output["summary"]["read_back_trace_id"] == trace
    assert "PRIVATE_SENTINEL" not in json.dumps(applied.output)
    applied.post.assert_called_once()
    applied.read.assert_called_once()


@pytest.mark.parametrize("field,value", [("driver_id", "odoo"), ("lifecycle_state", "retired"), ("lanes", [])])
def test_state_plan_requires_an_active_generic_testing_lane(tmp_path: Path, field: str, value: Any) -> None:
    current = profile()
    current[field] = value
    path = tmp_path / "private-request.json"
    planned = invoke(plan_args(path), read_body=response(current))
    assert planned.code != 0
    assert not path.exists()
    planned.post.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
