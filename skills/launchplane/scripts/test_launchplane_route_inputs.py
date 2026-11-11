# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.2.0"]
# ///
from __future__ import annotations

import copy
import io
import json
import stat
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch

import launchplane_route_authority as authority
import pytest
from test_launchplane_helpers import write_action as h
from test_launchplane_inert_planners import DOMAINS, inspection
from test_launchplane_route_authority import (
    SCOPE,
    absent,
    binding_record,
    binding_response,
    blocked_binding,
    http_error,
    profiles,
    run,
    verification_posts,
    verification_reads,
)


def invoke(path: Path, reads: list[Any], command: str = "route-authority-discover") -> tuple[int, dict[str, Any], Any]:
    output = io.StringIO()
    argv = [
        command,
        *[arg for key, value in SCOPE.items() for arg in ("--" + key, value)],
        "--result-file",
        str(path),
    ]
    with (
        patch.object(
            h,
            "prepare_operator_settings",
            return_value={"service_url": "https://example.invalid"},
        ),
        patch.object(h, "request_launchplane_read", side_effect=reads) as read,
        patch.object(h, "request_launchplane") as post,
        redirect_stdout(output),
    ):
        code = h.main(argv)
    post.assert_not_called()
    return code, json.loads(output.getvalue()), read


def current() -> dict[str, Any]:
    return {
        "status": "ok",
        "trace_id": "launchplane_req_current",
        "record": binding_record(),
    }


def sources() -> list[Any]:
    observed = inspection()
    observed["inspect"]["tracked_target"]["domains"] = DOMAINS
    return [profiles(), current(), observed, current()]


def test_private_packet_preserves_sources_and_feeds_ingress_continuation(
    tmp_path: Path,
) -> None:
    reads = sources()
    reads[1]["record"]["domains"] = [{"domain_name": "stale.example.invalid", "role": "primary"}]
    reads[3] = copy.deepcopy(reads[1])
    reads[2]["inspect"]["provider"]["token"] = "DO_NOT_EXPORT"
    path = tmp_path / "packet.json"
    code, public, _ = invoke(path, reads)
    assert code == 0 and public["result"]["private_result_saved"] is True
    packet = json.loads(path.read_text())
    assert packet["production_use"] == "live"
    assert packet["recorded_binding"]["domains"] == ["stale.example.invalid"]
    assert packet["current_target"]["tracked_domains"] == DOMAINS
    assert packet["current_target"]["provider_domains"] == DOMAINS
    assert packet["expected_current"]["record_sha256"] == reads[1]["record"]["record_sha256"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "DO_NOT_EXPORT" not in path.read_text()
    assert DOMAINS[0] not in json.dumps(public) and "edge-shop" not in json.dumps(public)
    args = h.parse_args(
        [
            "route-binding-dry-run",
            "--product",
            "shop",
            "--context",
            "shop",
            "--instance",
            "prod",
            "--reason",
            "verify",
            "--ingress-payload-file",
            str(path),
        ]
    )
    body = h._ingress_plan_input(args, payload_file=str(path), verify_route_binding=True)
    assert body["route"] == {"domain_names": DOMAINS, "edge_endpoint_key": "edge-shop"}
    assert "expected_host_id" not in body
    code, plan, _, post = run(
        "route-binding-dry-run",
        [profiles(), *absent(), *verification_reads(), *absent()],
        [
            blocked_binding("ingress_verification_required"),
            *verification_posts(),
            binding_response(),
        ],
        private=packet,
    )
    assert code == 0 and plan["result"]["verification_refreshed"] is True
    comparison = post.call_args_list[1].kwargs["body"]["ingress"]
    verified = post.call_args_list[2].kwargs["body"]["ingress"]
    assert "expected_host_id" not in comparison and "verify_route_binding" not in comparison
    assert verified["verify_route_binding"] is True and verified["expected_host_id"] > 0
    assert all(verified[key] is False for key in ("allow_create", "allow_update", "allow_enable_disable"))


@pytest.mark.parametrize(
    "change",
    [
        "production_missing",
        "production_unknown",
        "edge_missing",
        "domains_missing",
        "target_mismatch",
        "scope_mismatch",
        "binding_changed",
        "domain_drift",
    ],
)
def test_incomplete_or_changed_sources_never_save_usable_packet(tmp_path: Path, change: str) -> None:
    reads = sources()
    if change == "production_missing":
        reads[0]["profile"].pop("production_use")
    elif change == "production_unknown":
        reads[0]["profile"]["production_use"] = "unknown"
    elif change == "edge_missing":
        reads[1]["record"]["ingress"].pop("endpoint_key")
    elif change == "domains_missing":
        reads[2]["inspect"]["tracked_target"].pop("domains")
    elif change == "target_mismatch":
        reads[2]["inspect"]["provider"]["id"] = "other"
    elif change == "scope_mismatch":
        reads[1]["record"]["context"] = "other"
    elif change == "binding_changed":
        reads[3]["record"]["record_sha256"] = "b" * 64
    else:
        reads[2]["inspect"]["tracked_target"]["domains"] = ["other.example.invalid"]
    path = tmp_path / "packet.json"
    code, public, _ = invoke(path, reads)
    assert code != 0 and not path.exists()
    assert "private_result_saved" not in public["result"]
    if change in {
        "production_missing",
        "production_unknown",
        "edge_missing",
        "domains_missing",
    }:
        expected = {
            "production_missing": "production_use_missing",
            "production_unknown": "production_use_unknown",
            "edge_missing": "route_binding_edge_missing",
            "domains_missing": "tracked_domains_missing",
        }
        assert public["summary"]["error_code"] == expected[change]


def test_absent_record_is_saved_as_absent_without_invented_ingress(
    tmp_path: Path,
) -> None:
    path = tmp_path / "absent.json"
    code, _, _ = invoke(path, [profiles(), *absent()])
    packet = json.loads(path.read_text())
    assert code == 0 and packet["expected_current"] == {"state": "absent"}
    assert packet["recorded_binding"] is None and packet["ingress_payload"] is None


@pytest.mark.parametrize(
    "error",
    [
        http_error(403, "authorization_denied"),
        http_error(code="unknown_route"),
        http_error(503, "unavailable"),
        {"status": "ok", "record": {}},
    ],
)
def test_failed_binding_read_never_becomes_absence(tmp_path: Path, error: Any) -> None:
    path = tmp_path / "packet.json"
    code, _, _ = invoke(path, [profiles(), error])
    assert code != 0 and not path.exists()


@pytest.mark.parametrize("destination", ["existing", "symlink", "repository"])
def test_private_destination_refused_before_requests(tmp_path: Path, destination: str) -> None:
    path = tmp_path / "packet.json"
    if destination == "existing":
        path.write_text("preserve")
    elif destination == "symlink":
        path.symlink_to(tmp_path / "absent")
    else:
        path = Path(__file__).parent / "never-create-private.json"
    code, _, read = invoke(path, [])
    assert code != 0 and read.call_count == 0
    if destination == "existing":
        assert path.read_text() == "preserve"


def test_binding_private_read_exports_only_recorded_facts(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    code, _, read = invoke(path, [current()], "route-binding-read")
    packet = json.loads(path.read_text())
    assert code == 0 and read.call_count == 1
    assert packet["recorded_binding"]["domains"] == DOMAINS
    assert packet["expected_current"]["record_sha256"] == binding_record()["record_sha256"]
    assert packet["ingress_payload"] is None and "current_target" not in packet and "production_use" not in packet


def test_untraced_or_oversized_sources_cannot_save_complete_private_evidence(
    tmp_path: Path,
) -> None:
    for change in ("trace", "size"):
        reads = sources()
        if change == "trace":
            reads[2].pop("trace_id")
        else:
            domains = [f"alias-{number}.example.invalid" for number in range(101)]
            reads[2]["inspect"]["tracked_target"]["domains"] = domains
            reads[2]["inspect"]["provider"]["domains"] = [{"host": domain} for domain in domains]
        path = tmp_path / f"{change}.json"
        code, _, _ = invoke(path, reads)
        assert code != 0 and not path.exists()


def test_private_save_failure_never_claims_saved_evidence(tmp_path: Path) -> None:
    with patch.object(
        authority,
        "write_staged",
        side_effect=ValueError("private_request_unavailable"),
    ):
        code, public, _ = invoke(tmp_path / "packet.json", sources())
    assert code != 0 and public["summary"]["error_code"] == "private_result_unavailable"
    assert public["result"].get("private_result_saved") is not True


def test_malformed404_and_unconfirmed_empty_listing_are_not_absence(
    tmp_path: Path,
) -> None:
    import urllib.error

    malformed = urllib.error.HTTPError(
        "https://example.invalid",
        404,
        "missing",
        Message(),
        io.BytesIO(b"<html>Not Found</html>"),
    )
    false_absence = {
        "status": "ok",
        "trace_id": "launchplane_req_list",
        **SCOPE,
        "records": [],
        "count": 1,
    }
    for number, responses in enumerate(([profiles(), malformed], [profiles(), http_error(), false_absence])):
        path = tmp_path / f"{number}.json"
        code, _, _ = invoke(path, responses)
        assert code != 0 and not path.exists()


def test_recorded_nonedge_binding_and_display_name_remain_readable(
    tmp_path: Path,
) -> None:
    response = current()
    response["record"]["ingress"] = {
        "provider": "none",
        "endpoint_key": "",
        "termination_kind": "none",
    }
    response["record"]["domains"] = []
    response["record"]["provider_target"]["target_name"] = "Fictional target (prod)"
    path = tmp_path / "direct.json"
    code, _, _ = invoke(path, [response], "route-binding-read")
    recorded = json.loads(path.read_text())["recorded_binding"]
    assert code == 0 and recorded["domains"] == [] and recorded["edge_endpoint_key"] == ""
    assert recorded["target_name"] == response["record"]["provider_target"]["target_name"]
    discovery = tmp_path / "discover.json"
    code, public, _ = invoke(discovery, [profiles(), response])
    assert code == 0 and public["result"]["ingress_input_available"] is False
    assert json.loads(discovery.read_text())["ingress_payload"] is None


def test_private_scope_is_selected_from_full_profile_beyond_public_lane_limit(
    tmp_path: Path,
) -> None:
    reads = sources()
    lane = reads[0]["profile"]["lanes"][0]
    reads[0]["profile"]["lanes"] = [
        {"context": f"unrelated-{number}", "instance": "prod"} for number in range(h.PRODUCT_PROFILE_MAX_LANES + 1)
    ] + [lane]
    path = tmp_path / "packet.json"
    code, _, _ = invoke(path, reads)
    packet = json.loads(path.read_text())
    assert code == 0 and packet["scope"] == SCOPE
    assert "unrelated" not in path.read_text()


def test_split_ingress_domains_stay_separate_and_cannot_make_one_edge_payload(
    tmp_path: Path,
) -> None:
    reads = sources()
    external = "external.example.invalid"
    reads[1]["record"]["external_ingress"] = {
        "domains": [external],
        "evidence_url": "https://do-not-export.invalid",
    }
    path = tmp_path / "record.json"
    code, _, _ = invoke(path, [reads[1]], "route-binding-read")
    assert code == 0 and json.loads(path.read_text())["recorded_binding"]["external_domains"] == [external]
    assert "do-not-export" not in path.read_text()
    path = tmp_path / "packet.json"
    code, public, _ = invoke(path, reads)
    assert code != 0 and not path.exists()
    assert public["summary"]["error_code"] == "route_inputs_split_ingress_unsupported"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
