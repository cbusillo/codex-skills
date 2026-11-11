#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0", "jsonschema==4.26.0", "rfc3339-validator==0.1.4"]
# ///
import copy
import hashlib
import json
import sys
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from test_launchplane_testing_candidate_release import PLAN, argv, invoke, response


def seal(evidence: dict[str, Any]) -> dict[str, Any]:
    def omit_nulls(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: omit_nulls(item) for key, item in value.items() if item is not None}
        if isinstance(value, list):
            return [omit_nulls(item) for item in value]
        return value

    payload = omit_nulls({key: value for key, value in evidence.items() if key != "packet_sha256"})
    evidence["packet_sha256"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return evidence


def packet() -> dict[str, Any]:
    return json.loads((Path(__file__).parent / "fixtures" / "candidate-custody-evidence.json").read_text())


def test_held_incomplete_packet_is_private_and_does_not_change_release_review(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "task-evidence"
    evidence_dir.mkdir()
    target = evidence_dir / "custody.json"
    evidence = packet()
    code, result, calls = invoke(
        argv() + ["--custody-evidence-file", str(target)],
        response("held", evidence=evidence),
    )
    assert code == 1 and result["status"] == "held"
    assert len(calls) == 1 and calls[0]["body"] == {
        "mode": "dry-run",
        "include_evidence": True,
    }
    assert target.stat().st_mode & 0o777 == 0o600
    assert calls[0]["no_store"] is True
    saved = json.loads(target.read_text())
    assert saved["evidence"] == evidence and saved["trace_id"] == "trace-test"
    assert saved["release_review"]["status"] == "held"
    assert result["result"]["outcomes"] == {
        "pass": 1,
        "fail": 2,
        "unavailable": 2,
        "not_reached": 1,
        "not_applicable": 0,
    }
    public = json.dumps(result)
    assert "conversion_shape" not in public and "candidate_sha256" not in public and "consumers" not in public
    assert result["result"]["file_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result["result"]["packet_sha256"] == evidence["packet_sha256"]
    assert result["result"]["custody_evidence_file"] == str(target)


@pytest.mark.parametrize(
    "mutation",
    [
        {"evidence": None},
        {"trace_id": ""},
        {"trace_id": None},
    ],
)
def test_missing_packet_or_wrong_envelope_is_unavailable(tmp_path: Path, mutation: dict[str, Any]) -> None:
    target = tmp_path / "custody.json"
    reply = response(evidence=packet()) | mutation
    code, result, calls = invoke(argv() + ["--custody-evidence-file", str(target)], reply)
    assert code != 0 and result["status"] == "unavailable" and len(calls) == 1
    assert not target.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"packet_sha256": "0" * 64},
        {"plan_sha256": "0" * 64},
        {"observed_at": "not-a-date"},
        {"version": True},
        {"checks": {"arbitrary": "pass"}},
        {"checks": {"acquisition": "unknown"}},
        {"lifecycle": {"container_count": -1}},
        {"lifecycle": {"container_count": 2.0}},
        {"volumes": [{"role": "ODOO_DB_VOLUME", "name_sha256": "z" * 64}]},
        {
            "volumes": [
                {
                    "role": "ODOO_DB_VOLUME",
                    "name_sha256": "d" * 64,
                    "raw": "PRIVATE_SENTINEL",
                }
            ]
        },
        {"raw": "PRIVATE_SENTINEL"},
    ],
)
def test_malformed_schema_binding_and_hash_never_saves_or_leaks(tmp_path: Path, change: dict[str, Any]) -> None:
    target = tmp_path / "custody.json"
    evidence = packet() | copy.deepcopy(change)
    if "packet_sha256" not in change:
        seal(evidence)
    code, result, _ = invoke(argv() + ["--custody-evidence-file", str(target)], response(evidence=evidence))
    assert code != 0 and result["status"] == "unavailable" and not target.exists()
    assert "PRIVATE_SENTINEL" not in json.dumps(result)


def test_null_exclusion_hash_and_eligible_review_are_compatible(tmp_path: Path) -> None:
    evidence = packet()
    evidence["completed_at"] = None
    evidence["endpoint_sha256"] = None
    evidence["lifecycle"]["snapshot_sha256"] = None
    seal(evidence)
    code, result, _ = invoke(
        argv() + ["--custody-evidence-file", str(tmp_path / "packet.json")],
        response(evidence=evidence),
    )
    assert code == 0
    reviewed = tmp_path / "review.json"
    reviewed.write_text(json.dumps(result))
    code, result, calls = invoke(argv("apply", str(reviewed)), response("released"))
    assert code == 0 and calls[0]["body"] == {
        "mode": "apply",
        "reviewed_plan_sha256": PLAN,
    }


def test_receiver_and_existing_destination_refuse_before_job(tmp_path: Path) -> None:
    target = tmp_path / "packet.json"
    args = argv() + ["--custody-evidence-file", str(target)]
    code, _, calls = invoke(args + ["--receiver-operation-id", "receiver"], response(evidence=packet()))
    assert code != 0 and not calls
    target.write_text("keep")
    code, _, calls = invoke(args, response(evidence=packet()))
    assert code != 0 and not calls and target.read_text() == "keep"


@pytest.mark.parametrize("filename", ["double  spaces.json", "ghp_fictional.json"])
def test_unsafe_or_normalized_public_reference_refuses_before_job(tmp_path: Path, filename: str) -> None:
    target = tmp_path / filename
    code, _, calls = invoke(argv() + ["--custody-evidence-file", str(target)], response(evidence=packet()))
    assert code != 0 and not calls and not target.exists()


def test_save_failure_preserves_safe_trace_and_reports_evidence_unavailable(tmp_path: Path) -> None:
    target = tmp_path / "custody.json"

    def raced_destination(_request: object) -> dict[str, object]:
        target.write_text("PRIVATE_SENTINEL")
        return response(evidence=packet())

    code, result, calls = invoke(argv() + ["--custody-evidence-file", str(target)], raced_destination)
    assert code == 1 and result["status"] == "unavailable" and len(calls) == 1
    assert target.read_text() == "PRIVATE_SENTINEL"
    assert result["summary"]["trace_id"] == "trace-test"
    assert result["summary"]["error_code"] == "custody_evidence_unavailable"
    assert "PRIVATE_SENTINEL" not in json.dumps(result)
    assert result["summary"]["recommendation"] == result["warnings"][0]["message"]


@pytest.mark.parametrize("filename", ["missing/packet.json", ""])
def test_missing_parent_or_empty_destination_refuses_before_job(tmp_path: Path, filename: str) -> None:
    path = str(tmp_path / filename) if filename else ""
    code, _, calls = invoke(argv() + ["--custody-evidence-file", path], response(evidence=packet()))
    assert code != 0 and not calls


def test_unavailable_schema_validation_refuses_before_job(tmp_path: Path) -> None:
    with patch.dict(sys.modules, {"jsonschema": None}):
        code, _, calls = invoke(argv() + ["--custody-evidence-file", str(tmp_path / "packet.json")], response(evidence=packet()))
    assert code != 0 and not calls


def test_unresolvable_user_path_is_a_structured_refusal() -> None:
    from launchplane_testing_state import write_staged
    from test_launchplane_helpers import write_action

    with patch("pwd.getpwnam", side_effect=KeyError):
        code, _, calls = invoke(argv() + ["--custody-evidence-file", "~fictional-user/packet.json"], response(evidence=packet()))
        assert code != 0 and not calls
        with pytest.raises(ValueError):
            write_staged(write_action, "~fictional-user/packet.json", {})


def test_default_ignores_unsolicited_packet_and_keeps_original_projection() -> None:
    code, original, calls = invoke(argv(), response())
    code_extra, extra, extra_calls = invoke(argv(), response(evidence={"raw": "PRIVATE_SENTINEL"}))
    assert code == code_extra == 0 and calls == extra_calls
    assert {key: value for key, value in original.items() if key != "generated_at"} == {
        key: value for key, value in extra.items() if key != "generated_at"
    }


@pytest.mark.parametrize("cache_control", ["no-store", ""])
def test_private_post_enforces_no_store_on_the_same_authenticated_transport(cache_control: str) -> None:
    from test_launchplane_helpers import write_action

    class Reply:
        status = 200

        def __init__(self) -> None:
            self.headers = Message()
            self.headers["Cache-Control"] = cache_control

        def __enter__(self) -> "Reply":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @staticmethod
        def read(_limit: int) -> bytes:
            return json.dumps(response(evidence=packet())).encode()

    with patch.object(write_action, "safe_urlopen", return_value=Reply()) as transport:
        kwargs = {
            "service_url": "https://plane.example.invalid",
            "path": "/v1/products/opw/testing-rebuild/original/candidate-release",
            "settings": {"token": "fictional"},
            "body": {"mode": "dry-run", "include_evidence": True},
            "timeout": 3,
            "no_store": True,
        }
        if cache_control == "no-store":
            result = write_action.request_launchplane(**kwargs)
            assert result["evidence"] == packet()
        else:
            with pytest.raises(write_action.ServiceResponseError):
                write_action.request_launchplane(**kwargs)
        assert transport.call_count == 1
        request = transport.call_args.args[0]
        assert request.method == "POST" and request.get_header("Authorization") == "Bearer fictional"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
