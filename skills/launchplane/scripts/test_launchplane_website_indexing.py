# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0"]
# ///

from __future__ import annotations

import copy
import hashlib
import io
import json
import stat
import urllib.error
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import patch

import pytest
from test_launchplane_helpers import write_action as helper


class Invocation(NamedTuple):
    code: int
    output: dict[str, Any]
    transport: Any
    result_file: Path


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan(*, committed: bool = False) -> dict[str, Any]:
    result = {
        "binding_sha256": digest({"runtime": "fictional-artifact", "product": "example"}),
        "database_uuid": "PRIVATE_DATABASE_SENTINEL",
        "native": {
            "website_id": 7,
            "public_origin": "https://private-site.example.invalid",
            "production_use": "prelaunch",
            "field": "custom_code_head",
            "before_sha256": digest("unrelated native head"),
            "after_sha256": digest("unrelated native head with managed element"),
            "changed": True,
            "managed_noindex_before": False,
            "managed_noindex_after": True,
        },
    }
    return {**result, "plan_sha256": digest(result), "committed": committed}


def envelope(mode: str) -> dict[str, Any]:
    indexing: dict[str, Any] = {
        "mode": mode,
        "public_origin": plan()["native"]["public_origin"],
    }
    post: dict[str, Any] = {
        "context": "example",
        "instance": "prod",
        "website_indexing": indexing,
    }
    if mode == "apply":
        indexing["reviewed_plan"] = plan()
        post["reason"] = "Protect the prelaunch native website."
    return {"product": "example", "post_deploy": post}


def response(result: dict[str, Any], **extras: Any) -> dict[str, Any]:
    return {
        "status": "accepted",
        "trace_id": "launchplane_req_indexing",
        "records": {},
        "result": result,
        **extras,
    }


class Response(io.BytesIO):
    def __init__(self, value: object) -> None:
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.status = 202


def invoke(
    tmp_path: Path,
    mode: str,
    *,
    body: dict[str, Any] | None = None,
    returned: object = None,
    error: Exception | None = None,
    destination: Path | None = None,
    private_path: Path | None = None,
    review_file: Path | None = None,
) -> Invocation:
    (tmp_path / "repo").mkdir(exist_ok=True)
    private = private_path or tmp_path / "request.json"
    private.write_text(json.dumps(body if body is not None else envelope(mode)))
    result_file = destination or tmp_path / "result.json"
    args = [
        "odoo-website-indexing-" + mode,
        "--payload-file",
        str(private),
        "--result-file",
        str(result_file),
    ]
    if mode == "apply":
        args += ["--idempotency-key", "original-private-key"]
        if review_file is not None:
            args += ["--reviewed-plan-file", str(review_file)]
    output = io.StringIO()
    with (
        patch.object(helper, "active_repo_root", return_value=tmp_path / "repo"),
        patch.object(
            helper,
            "prepare_operator_settings",
            return_value={
                "service_url": "https://plane.example.invalid",
                "token": "PRIVATE_TOKEN_SENTINEL",
            },
        ),
        patch.object(
            helper,
            "safe_urlopen",
            return_value=Response(returned if returned is not None else response(plan(committed=mode == "apply"))),
            side_effect=error,
        ) as transport,
        redirect_stdout(output),
    ):
        code = helper.main(args)
    public = output.getvalue()
    for private_value in (
        "PRIVATE_DATABASE_SENTINEL",
        "PRIVATE_TOKEN_SENTINEL",
        "original-private-key",
        plan()["native"]["public_origin"],
        str(result_file),
        "Protect the prelaunch",
    ):
        assert private_value not in public
    return Invocation(code, json.loads(public), transport, result_file)


@pytest.mark.parametrize("mode", ["dry-run", "apply", "readback"])
@pytest.mark.parametrize("selector", ["omitted", None, 7])
def test_indexing_carries_only_typed_native_branch_and_retains_complete_private_receipt(
    tmp_path: Path,
    mode: str,
    selector: object,
) -> None:
    body = envelope(mode)
    if selector != "omitted":
        body["post_deploy"]["website_indexing"]["website_id"] = selector
    result = invoke(tmp_path, mode, body=body)
    assert result.code == 0
    result.transport.assert_called_once()
    request = result.transport.call_args.args[0]
    assert request.method == "POST"
    assert request.full_url.endswith(helper.helper_command_path("odoo-website-indexing-" + mode))
    assert json.loads(request.data) == body
    assert request.get_header("Authorization") == "Bearer PRIVATE_TOKEN_SENTINEL"
    assert request.get_header("Idempotency-key") == ("original-private-key" if mode == "apply" else None)
    assert json.loads(result.result_file.read_text()) == response(plan(committed=mode == "apply"))
    assert stat.S_IMODE(result.result_file.stat().st_mode) == 0o600
    assert result.output["result"]["plan_sha256"] == plan()["plan_sha256"]
    assert result.output["result"]["native"]["before_sha256"] == plan()["native"]["before_sha256"]
    assert result.output["result"]["committed"] is (mode == "apply")


@pytest.mark.parametrize("mode", ["dry-run", "apply", "readback"])
@pytest.mark.parametrize(
    "invalid",
    [
        "maintenance",
        "toggle",
        "mode",
        "review",
        "instance",
        "website",
        "origin",
        "plan",
    ],
)
def test_indexing_refuses_invalid_or_broad_requests_before_transport(tmp_path: Path, mode: str, invalid: str) -> None:
    body = envelope(mode)
    post = body["post_deploy"]
    indexing = post["website_indexing"]
    if invalid == "maintenance":
        post["connection_isolation"] = "apply"
    elif invalid == "toggle":
        indexing["production_use"] = "live"
    elif invalid == "mode":
        indexing["mode"] = "apply" if mode != "apply" else "readback"
    elif invalid == "review":
        if mode == "apply":
            indexing.pop("reviewed_plan")
        else:
            indexing["reviewed_plan"] = {"binding_sha256": "incomplete"}
    elif invalid == "instance":
        post["instance"] = "testing"
    elif invalid == "website":
        indexing["website_id"] = True
    elif invalid == "origin":
        indexing["public_origin"] = "https://example-user@example.invalid/path"
    elif invalid == "plan":
        indexing["reviewed_plan"] = {**plan(), "plan_sha256": "invalid-digest"}
    result = invoke(tmp_path, mode, body=body)
    assert result.code != 0
    result.transport.assert_not_called()
    assert not result.result_file.exists()


@pytest.mark.parametrize("field", ["binding_sha256", "database_uuid", "native", "plan_sha256", "committed"])
def test_indexing_incomplete_plan_never_authorizes_apply(tmp_path: Path, field: str) -> None:
    body = envelope("apply")
    body["post_deploy"]["website_indexing"]["reviewed_plan"].pop(field)
    result = invoke(tmp_path, "apply", body=body)
    assert result.code != 0
    result.transport.assert_not_called()


@pytest.mark.parametrize(
    "field",
    [
        "binding_sha256",
        "database_uuid",
        "website_id",
        "public_origin",
        "production_use",
        "before_sha256",
        "after_sha256",
        "managed_noindex_before",
        "managed_noindex_after",
        "committed",
    ],
)
def test_indexing_foreign_or_uncommitted_apply_receipt_stays_unverified(tmp_path: Path, field: str) -> None:
    returned = plan(committed=True)
    target = returned["native"] if field in returned["native"] else returned
    target[field] = (
        not target[field]
        if type(target[field]) is bool
        else (target[field] + 1 if type(target[field]) is int else str(target[field]) + "foreign")
    )
    returned["plan_sha256"] = digest({key: returned[key] for key in ("binding_sha256", "database_uuid", "native")})
    result = invoke(tmp_path, "apply", returned=response(returned))
    assert result.code != 0
    assert result.output["status"] in {"accepted_unverified", "outcome_unknown"}
    assert not result.result_file.exists()


@pytest.mark.parametrize("failure", ["timeout", "malformed", "shape", "missing", "unsafe"])
def test_indexing_uncertain_apply_keeps_original_key_without_retry(tmp_path: Path, failure: str) -> None:
    returned: Any = response(plan(committed=True))
    error = None
    if failure == "timeout":
        error = TimeoutError("PRIVATE_TOKEN_SENTINEL")
    elif failure == "malformed":
        returned = b"not JSON PRIVATE_TOKEN_SENTINEL"
    elif failure == "shape":
        returned["result"]["raw"] = "PRIVATE_TOKEN_SENTINEL"
    elif failure == "missing":
        returned["result"].pop("database_uuid")
    else:
        returned["trace_id"] = "https://private.invalid/PRIVATE_TOKEN_SENTINEL"
    result = invoke(tmp_path, "apply", returned=returned, error=error)
    assert result.code != 0
    assert result.output["status"] in {"accepted_unverified", "outcome_unknown"}
    result.transport.assert_called_once()
    assert not result.result_file.exists()


def test_indexing_original_key_replay_preserves_receipt_and_service_trace(
    tmp_path: Path,
) -> None:
    returned = response(
        plan(committed=True),
        replayed=True,
        original_trace_id="launchplane_req_original",
    )
    result = invoke(tmp_path, "apply", returned=returned)
    assert result.code == 0
    assert json.loads(result.result_file.read_text()) == returned
    assert result.output["summary"]["replayed"] is True
    assert result.output["summary"]["original_trace_id"] == returned["original_trace_id"]
    replay = invoke(tmp_path, "apply", returned=returned, destination=tmp_path / "replayed.json")
    replay_request = replay.transport.call_args.args[0]
    assert replay.code == 0
    assert replay_request.data == result.transport.call_args.args[0].data
    assert replay_request.get_header("Idempotency-key") == result.transport.call_args.args[0].get_header("Idempotency-key")


@pytest.mark.parametrize("code", [403, 409, 503])
def test_indexing_preserves_refusal_or_uncertainty_without_unsafe_details(tmp_path: Path, code: int) -> None:
    error = urllib.error.HTTPError(
        "https://plane.example.invalid",
        code,
        "PRIVATE_TOKEN_SENTINEL",
        Message(),
        io.BytesIO(
            json.dumps(
                {
                    "trace_id": "launchplane_req_refusal",
                    "error": {
                        "code": "website_indexing_unverified",
                        "message": "PRIVATE_DATABASE_SENTINEL",
                    },
                }
            ).encode()
        ),
    )
    result = invoke(tmp_path, "apply", error=error)
    assert result.code != 0
    result.transport.assert_called_once()
    assert result.output["summary"]["trace_id"] == "launchplane_req_refusal"
    assert result.output["summary"]["http_status"] == code
    assert not result.result_file.exists()


@pytest.mark.parametrize("unsafe", ["existing", "symlink", "repository", "same-input"])
def test_indexing_private_destination_refuses_before_effect(tmp_path: Path, unsafe: str) -> None:
    destination = tmp_path / "output.json"
    original = tmp_path / "protected.json"
    original.write_text("original")
    if unsafe == "existing":
        destination.write_text("original")
    elif unsafe == "symlink":
        destination.symlink_to(original)
    elif unsafe == "repository":
        destination = tmp_path / "repo" / "output.json"
        destination.parent.mkdir()
    else:
        destination = tmp_path / "request.json"
    result = invoke(tmp_path, "apply", destination=destination)
    assert result.code != 0
    result.transport.assert_not_called()
    assert original.read_text() == "original"


def test_indexing_repository_input_refuses_before_effect(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    result = invoke(tmp_path, "apply", private_path=repo / "request.json")
    assert result.code != 0
    result.transport.assert_not_called()


def test_indexing_readback_retains_fresh_native_after_image_without_claiming_apply(
    tmp_path: Path,
) -> None:
    returned = copy.deepcopy(plan())
    native = returned["native"]
    native.update(before_sha256=native["after_sha256"], changed=False, managed_noindex_before=True)
    returned["plan_sha256"] = digest({key: returned[key] for key in ("binding_sha256", "database_uuid", "native")})
    result = invoke(tmp_path, "readback", returned=response(returned))
    assert result.code == 0
    assert result.output["result"]["native"]["changed"] is False
    assert result.output["result"]["committed"] is False


@pytest.mark.parametrize("location", ["envelope", "post", "indexing", "missing", "null"])
def test_indexing_closed_branch_cannot_fall_through_to_maintenance(tmp_path: Path, location: str) -> None:
    body = envelope("dry-run")
    if location == "missing":
        body["post_deploy"].pop("website_indexing")
    elif location == "null":
        body["post_deploy"]["website_indexing"] = None
    else:
        target = (
            body
            if location == "envelope"
            else body["post_deploy"]
            if location == "post"
            else body["post_deploy"]["website_indexing"]
        )
        target["unexpected_private_field"] = "PRIVATE_TOKEN_SENTINEL"
    result = invoke(tmp_path, "dry-run", body=body)
    assert result.code != 0
    result.transport.assert_not_called()


def test_indexing_saved_plan_can_be_applied_without_manual_plan_copy(
    tmp_path: Path,
) -> None:
    body = envelope("dry-run")
    body["post_deploy"]["website_indexing"].pop("mode")
    body["post_deploy"]["reason"] = "Protect the prelaunch native website."
    planned = invoke(tmp_path, "dry-run", body=body)
    applied = invoke(
        tmp_path,
        "apply",
        body=body,
        review_file=planned.result_file,
        destination=tmp_path / "receipt.json",
    )
    assert planned.code == applied.code == 0
    submitted = json.loads(applied.transport.call_args.args[0].data)
    assert submitted["post_deploy"]["website_indexing"]["reviewed_plan"] == json.loads(planned.result_file.read_text())["result"]
    assert submitted["post_deploy"]["website_indexing"]["mode"] == "apply"
    assert body["post_deploy"]["website_indexing"].get("reviewed_plan") is None


def test_indexing_private_write_failure_preserves_accepted_commit(
    tmp_path: Path,
) -> None:
    import launchplane_website_indexing as indexing

    with patch.object(indexing, "write_staged", side_effect=ValueError("private_request_unavailable")):
        result = invoke(tmp_path, "apply")
    assert result.code != 0
    assert result.output["status"] == "accepted_unverified"
    assert result.output["result"]["committed"] is True
    assert result.output["summary"]["error_code"] == "private_result_unavailable"
    result.transport.assert_called_once()


@pytest.mark.parametrize("mode", ["dry-run", "apply", "readback"])
def test_indexing_native_timeout_and_explicit_override_reach_transport(tmp_path: Path, mode: str) -> None:
    import launchplane_website_indexing as indexing

    result = invoke(tmp_path, mode)
    assert result.transport.call_args.kwargs["timeout"] == indexing.TIMEOUT_SECONDS
    args = [
        "--timeout",
        "91",
        "odoo-website-indexing-" + mode,
        "--payload-file",
        "unused",
        "--result-file",
        "unused",
    ]
    if mode == "apply":
        args += ["--idempotency-key", "unused"]
    assert helper.parse_args(args).timeout == 91


@pytest.mark.parametrize("state", ["prelaunch", "live"])
def test_indexing_accepts_authoritative_state_and_producer_origin_normalization(tmp_path: Path, state: str) -> None:
    body = envelope("dry-run")
    body["post_deploy"]["website_indexing"]["public_origin"] += "/"
    returned = plan()
    returned["native"]["production_use"] = state
    returned["native"]["managed_noindex_after"] = state == "prelaunch"
    result = invoke(tmp_path, "dry-run", body=body, returned=response(returned))
    assert result.code == 0
    assert result.output["result"]["native"]["production_use"] == state
    assert json.loads(result.transport.call_args.args[0].data) == body


def test_indexing_invalid_private_origin_never_leaks_parse_error(tmp_path: Path) -> None:
    body = envelope("apply")
    body["post_deploy"]["website_indexing"]["public_origin"] = "https://example.invalid:PRIVATE_TOKEN_SENTINEL"
    result = invoke(tmp_path, "apply", body=body)
    assert result.code != 0
    result.transport.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
