#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
# ///
"""Disposable-lane callers bind review, refuse unsafe facts and never guess outcomes."""

import hashlib
import io
import json
import urllib.error
from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import launchplane_contract as contract
import launchplane_disposable_lane as lane
import pytest
from test_launchplane_helpers import write_action as h

SERVICE = "https://plane.example.invalid"
BINDING = "a" * 64
ROUTE = contract.operation_path("record_disposable_lane_safety")
# Built at run time so the public-safety scanner sees no token-shaped literal.
EVIDENCE_HASH = "c" * 64
EMPTY_OBSERVATION_HASH = hashlib.sha256(b"null").hexdigest()
ACTOR = "PRIVATE_ACTOR_SENTINEL"
REPLACED_OPERATION = "retained-operation-fixture"
COVERAGE = {"usage": "staff_testing_hold", "data": "qualified_resettable_odoo_generation",
            "production_resources": "same_kind_production_target_alias", "historical_generations": "not_current"}


def times(observed_offset: int = -1, lifetime: int = lane.DEFAULT_LIFETIME_SECONDS) -> dict[str, str]:
    """Observation window relative to now, as the producer writes it."""
    observed = datetime.now(UTC) + timedelta(seconds=observed_offset)
    return {"observed_at": observed.isoformat(), "expires_at": (observed + timedelta(seconds=lifetime)).isoformat()}


def scope(preview: bool = False, **updates: object) -> dict[str, object]:
    body = {"product": "example", "context": "example-testing", "instance": "testing"}
    if preview:
        body.update(instance="pr-12", preview_id="preview-12")
    return {**body, **updates}


def receipt(kind: str, **updates: object) -> dict[str, Any]:
    # Keep these independent of the consumer's field allowlists.
    result = {
        "inspect": {"mode": "inspect", "binding_sha256": BINDING, "usage": "unknown", "data": "unknown",
                    "production_resources": "unknown", "automatic_fact_collection": "limited_service_records",
                    "source_coverage": COVERAGE, "observation_sha256": EVIDENCE_HASH, "observation": None},
        "collect": {"mode": "collect", "binding_sha256": BINDING, "observed_by": ACTOR,
                    "observer_kind": "director_agent", "usage": "in_use", "data": "unknown",
                    "production_resources": "shared",
                    "usage_evidence": f"service:odoo-testing-usage:{BINDING}:2026-10-19T10:00:00.123456+00:00",
                    "data_evidence": "",
                    "isolation_evidence": f"service:production-target-alias:{BINDING}:{EVIDENCE_HASH}",
                    "historical_usage_retained": False,
                    "odoo_usage": {"recent_signins": True, "authenticated_sessions": False, "reason_code": "complete"},
                    "never_served_qualification": "never_served_not_attempted", **times(),
                    "provider_mutation": False,
                    "automatic_fact_collection": "limited_service_records", "source_coverage": COVERAGE},
        "record": {"mode": "record", "binding_sha256": BINDING, "observed_by": ACTOR,
                   "observer_kind": "director_agent", "usage": "unknown", "data": "unknown",
                   "production_resources": "unknown", "provider_mutation": False,
                   "automatic_fact_collection": "unavailable", "source_coverage": COVERAGE},
        "clear": {"mode": "clear", "binding_sha256": BINDING, "observation_sha256": EVIDENCE_HASH,
                  "provider_mutation": False},
        "prepare-rebuild": {"mode": "prepare-rebuild", "operation_id": REPLACED_OPERATION,
                            "binding_sha256": BINDING, "historical_effects": "unknown", "provider_mutation": False},
    }[kind]
    return {"status": "accepted", "trace_id": f"launchplane_req_{kind}", "records": {"product_profile": "example"},
            "result": {**result, **updates}}


def invoke(mode: str = "inspect", *, body: dict[str, Any] | None = None,
           inspect_body: dict[str, Any] | None = None,
           responses: list[dict[str, Any] | Exception] | None = None,
           edit_evidence: Callable[[dict[str, Any]], object] | None = None,
           service: str = SERVICE, reviewed: bool = True, expected: str = BINDING,
           inspect_response: dict[str, Any] | None = None,
           expected_observation: str | None = EVIDENCE_HASH) -> tuple[int, dict[str, Any], list[Mapping[str, Any]], int]:
    output = io.StringIO()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "repo").mkdir()
        payload = root / "request.json"
        evidence_path = root / "inspect.json"
        payload.write_text(json.dumps(inspect_body or scope()))
        with (patch.object(h, "active_repo_root", return_value=root / "repo"),
              patch.object(h, "prepare_operator_settings", return_value={"service_url": SERVICE}) as settings,
              patch.object(h, "request_launchplane", return_value=inspect_response or receipt("inspect")) as post,
              redirect_stdout(output)):
            if mode != "inspect":
                code = h.main([lane.PREFIX + "inspect", "--payload-file", str(payload)])
                assert code == 0, output.getvalue()
                evidence = json.loads(output.getvalue())
                if edit_evidence:
                    edit_evidence(evidence)
                evidence_path.write_text(json.dumps(evidence))
                output.seek(0)
                output.truncate()
                post.reset_mock()
                settings.reset_mock()
            default_body = scope(replaces_operation_id=REPLACED_OPERATION) if mode == "prepare-rebuild" else scope()
            payload.write_text(json.dumps(default_body if body is None else body))
            settings.return_value = {"service_url": service}
            readback = receipt("inspect", observation_sha256=(
                EMPTY_OBSERVATION_HASH if mode == "clear" else EVIDENCE_HASH))
            post.side_effect = responses if responses is not None else (
                [receipt("inspect")] if mode == "inspect" else [receipt(mode), readback])
            argv = [lane.PREFIX + mode, "--payload-file", str(payload)]
            if mode != "inspect":
                argv += ["--expected-binding-sha256", expected, "--inspect-evidence-file", str(evidence_path)]
                if reviewed:
                    argv.append("--reviewed-inspect")
                if mode == "clear" and expected_observation is not None:
                    argv += ["--expected-observation-sha256", expected_observation]
            code = h.main(argv)
            return code, json.loads(output.getvalue()), [call.kwargs for call in post.call_args_list], settings.call_count


def http_error(status: int, code: str) -> urllib.error.HTTPError:
    body = {"trace_id": "launchplane_req_refusal", "error": {"code": code, "message": "PRIVATE_SENTINEL"}}
    return urllib.error.HTTPError(SERVICE, status, "PRIVATE_SENTINEL", Message(), io.BytesIO(json.dumps(body).encode()))


@pytest.mark.parametrize("mode", lane.MODES)
def test_each_mode_sends_its_exact_body_and_rechecks_the_binding(mode: str) -> None:
    body = scope(lifetime_seconds=120) if mode in {"collect", "record"} else scope()
    if mode == "prepare-rebuild":
        body["replaces_operation_id"] = REPLACED_OPERATION
    responses = [receipt(mode, **times(lifetime=120)), receipt("inspect")] if mode == "collect" else None
    code, output, calls, _ = invoke(mode, body=body, responses=responses)
    assert code == 0, output
    assert all(call["path"] == ROUTE == contract.helper_command_path(lane.PREFIX + mode) for call in calls)
    expected = {**body, "mode": mode}
    if mode != "inspect":
        expected["expected_binding_sha256"] = BINDING
    if mode == "clear":
        expected["expected_observation_sha256"] = EVIDENCE_HASH
    assert calls[0]["body"] == expected
    assert "idempotency_key" not in calls[0]
    if mode == "inspect":
        assert len(calls) == 1
        assert {output["result"][fact] for fact in lane.FACTS} == {"unknown"}
        assert output["result"]["binding_sha256"] == BINDING
        return
    assert [call["body"] for call in calls[1:]] == [{**scope(), "mode": "inspect"}]
    assert output["status"] == "accepted"
    assert output["result"]["binding_readback_matches"] is True
    assert output["result"]["effect_readback"] == ("verified" if mode == "clear" else "unavailable")
    assert output["result"]["provider_mutation"] is False


@pytest.mark.parametrize("facts", [{}, {"usage": "unused", "usage_evidence": "PRIVATE_SENTINEL"}])
def test_preview_record_refuses_before_configuration(facts: dict[str, str]) -> None:
    code, _, calls, settings = invoke("record", body=scope(preview=True, **facts), inspect_body=scope(preview=True))
    assert code != 0 and calls == [] and settings == 0


def test_record_accepts_the_current_producer_receipt_and_replacement_link() -> None:
    body = scope(replaces_operation_id=REPLACED_OPERATION)
    code, output, calls, _ = invoke("record", body=body)
    assert code == 0, output
    assert calls[0]["body"] == {**body, "mode": "record", "expected_binding_sha256": BINDING}
    assert all(output["result"][fact] == "unknown" for fact in lane.FACTS)
    assert REPLACED_OPERATION not in json.dumps(output)


@pytest.mark.parametrize("legacy,accepted", [(False, True), (True, False), (None, False), (0, False), ("false", False)])
def test_testing_record_accepts_only_a_strictly_false_legacy_reconcile_field(legacy: object, accepted: bool) -> None:
    code, output, calls, _ = invoke("record", responses=[receipt("record", reconcile_requested=legacy), receipt("inspect")])
    assert (code == 0) is accepted
    assert len(calls) == (2 if accepted else 1)
    assert "reconcile_requested" not in output["result"]


def test_collect_accepts_service_qualified_replacement_resource_evidence() -> None:
    reference = "service:testing-resources:" + EVIDENCE_HASH
    collected = receipt("collect", usage="unknown", usage_evidence="", data="reproducible", data_evidence=reference,
                        production_resources="separation_ready", isolation_evidence=reference,
                        odoo_usage={"reason_code": "unsupported_lane"})
    code, output, _, _ = invoke("collect", responses=[collected, receipt("inspect")])
    assert code == 0, output
    assert output["result"]["evidence"]["data"]["kind"] == "testing-resources"
    assert output["result"]["evidence"]["production_resources"]["kind"] == "testing-resources"
    assert reference not in json.dumps(output)


@pytest.mark.parametrize("observation", [None,
    {"usage": "in_use", "data": "only_copy", "production_resources": "shared"},
    {"usage": "unused", "data": "reproducible", "production_resources": "isolated"},
])
def test_inspect_keeps_stored_observation_separate_from_fresh_unknown_facts(observation: dict[str, str] | None) -> None:
    code, output, calls, _ = invoke(responses=[receipt("inspect", observation=observation)])
    assert code == 0 and len(calls) == 1
    assert output["result"]["observation_sha256"] == EVIDENCE_HASH
    assert output["result"]["observation"] == observation
    assert all(output["result"][fact] == "unknown" for fact in lane.FACTS)


@pytest.mark.parametrize("removed", [("observation_sha256",), ("observation",), ("observation_sha256", "observation")])
def test_inspect_without_stored_observation_metadata_is_invalid(removed: tuple[str, ...]) -> None:
    response = receipt("inspect")
    for field in removed:
        response["result"].pop(field)
    code, output, calls, _ = invoke(responses=[response])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "invalid" and output["result"] == {}


@pytest.mark.parametrize("mode,body", [
    ("inspect", scope(instance="prod")),
    ("inspect", scope(preview=True, instance="prod")),
    ("inspect", scope(instance="staging")),
    ("inspect", scope(product="../escape")),
    ("inspect", scope(mode="record")),
    ("inspect", scope(expected_binding_sha256=BINDING)),
    ("inspect", scope(usage="unused", usage_evidence="ref")),
    ("collect", scope(lifetime_seconds=0)),
    ("collect", scope(lifetime_seconds=lane.MAX_LIFETIME_SECONDS + 1)),
    ("collect", scope(lifetime_seconds=True)),
    ("record", scope(usage="unused", usage_evidence="ref")),
    ("record", scope(production_resources="isolated", isolation_evidence="ref")),
    ("record", scope(replaces_operation_id="../escape")),
    ("clear", scope(instance="prod")),
    ("clear", scope(preview=True, instance="prod")),
    ("clear", scope(usage="unused")),
    ("clear", scope(data_evidence="PRIVATE_SENTINEL")),
    ("clear", scope(lifetime_seconds=120)),
    ("clear", scope(expected_observation_sha256=EVIDENCE_HASH)),
    ("clear", scope(reconcile_requested=True)),
])
def test_unsafe_or_unsupported_requests_refuse_before_configuration(mode: str, body: dict[str, object]) -> None:
    code, _, calls, settings = invoke(mode, body=body, inspect_body=scope(preview="preview_id" in body))
    assert code != 0 and calls == [] and settings == 0


@pytest.mark.parametrize("change", [
    lambda e: e.update(operation="disposable-lane-collect"),
    lambda e: e.update(status="denied"),
    lambda e: e["request"].update(mode="record"),
    lambda e: e["result"].update(binding_sha256="b" * 64),
    lambda e: e["result"].update(review_binding_sha256="b" * 64),
    lambda e: e["result"].update(service_fingerprint="b" * 64),
    lambda e: e["result"].update(observation_sha256="b" * 64),
    lambda e: e["result"].update(observation={"usage": "unused", "data": "reproducible",
                                          "production_resources": "isolated"}),
    lambda e: e["result"].update(observation={"usage": "maybe", "data": "unknown",
                                          "production_resources": "unknown"}),
    lambda e: e["result"].pop("observation_sha256"),
    lambda e: e["result"].pop("observation"),
    lambda e: e.update(result=None),
])
@pytest.mark.parametrize("mode", ["collect", "record", "clear", "prepare-rebuild"])
def test_ineligible_inspect_evidence_never_reaches_transport(
        mode: str, change: Callable[[dict[str, Any]], object]) -> None:
    code, output, calls, settings = invoke(mode, edit_evidence=change)
    assert code != 0 and calls == [] and settings == 0
    assert output["warnings"][0]["code"] == "reviewed_inspect_not_eligible"


@pytest.mark.parametrize("mode", ["collect", "record", "clear", "prepare-rebuild"])
def test_review_acknowledgement_digest_scope_and_service_are_bound(mode: str) -> None:
    code, _, calls, settings = invoke(mode, reviewed=False)
    assert code != 0 and calls == [] and settings == 0
    code, _, calls, settings = invoke(mode, expected="b" * 64)
    assert code != 0 and calls == [] and settings == 0
    changed_scope = scope(context="other-testing")
    if mode == "prepare-rebuild":
        changed_scope["replaces_operation_id"] = REPLACED_OPERATION
    code, _, calls, settings = invoke(mode, body=changed_scope)
    assert code != 0 and calls == [] and settings == 0
    code, _, calls, _ = invoke(mode, service="https://different.example.invalid")
    assert code != 0 and calls == []


@pytest.mark.parametrize("expected", ["not-a-digest", "b" * 64, "", "C" * 64])
def test_clear_requires_the_exact_reviewed_observation_digest(expected: str) -> None:
    code, _, calls, settings = invoke("clear", expected_observation=expected)
    assert code != 0 and calls == [] and settings == 0


def test_clear_requires_an_explicit_observation_digest_before_reading_private_files() -> None:
    with (pytest.raises(SystemExit), redirect_stderr(io.StringIO()),
          patch.object(h, "read_payload_file") as read, patch.object(h, "request_launchplane") as post):
        h.main([lane.PREFIX + "clear", "--payload-file", "/private/request.json",
                "--expected-binding-sha256", BINDING, "--inspect-evidence-file", "/private/inspect.json",
                "--reviewed-inspect"])
    assert read.call_count == post.call_count == 0


@pytest.mark.parametrize("preview", [False, True])
def test_clear_compares_the_reviewed_report_and_verifies_its_absence(preview: bool) -> None:
    observation = {"usage": "in_use", "data": "only_copy", "production_resources": "shared"}
    body = scope(preview=preview)
    code, output, calls, _ = invoke("clear", body=body, inspect_body=body,
                                  inspect_response=receipt("inspect", observation=observation))
    assert code == 0, output
    assert [call["body"] for call in calls] == [
        {**body, "mode": "clear", "expected_binding_sha256": BINDING,
         "expected_observation_sha256": EVIDENCE_HASH},
        {**body, "mode": "inspect"},
    ]
    assert all(call["path"] == ROUTE and "idempotency_key" not in call for call in calls)
    assert output["status"] == "accepted"
    assert output["result"]["effect_readback"] == "verified"
    assert output["result"]["binding_readback_matches"] is True
    assert output["result"]["observation_readback_matches"] is True
    assert output["result"]["observation_sha256"] == EVIDENCE_HASH
    assert output["result"]["provider_mutation"] is False
    assert not output["result"].keys() & lane.FACTS.keys()
    assert output["request"] == {"mode": "clear", "payload_source": "private_file"}
    assert all(value not in json.dumps(output) for value in ("example-testing", "preview-12", ACTOR))


def test_clear_can_compare_an_already_absent_observation() -> None:
    code, output, calls, _ = invoke("clear", expected_observation=EMPTY_OBSERVATION_HASH,
        inspect_response=receipt("inspect", observation_sha256=EMPTY_OBSERVATION_HASH),
        responses=[receipt("clear", observation_sha256=EMPTY_OBSERVATION_HASH),
                   receipt("inspect", observation_sha256=EMPTY_OBSERVATION_HASH)])
    assert code == 0 and len(calls) == 2, output
    assert calls[0]["body"]["expected_observation_sha256"] == EMPTY_OBSERVATION_HASH
    assert output["result"]["effect_readback"] == "verified"


@pytest.mark.parametrize("updates", [{"usage": "unused"}, {"automatic_fact_collection": "unavailable"},
    {"binding_sha256": "not-a-digest"}, {"observation_sha256": "not-a-digest"},
    {"observation_sha256": None}, {"observation": "PRIVATE_SENTINEL"}, {"observation": []},
    {"observation": {}},
    {"observation": {"usage": "unknown", "data": "unknown", "production_resources": "shared", "extra": "x"}},
    {"observation": {"usage": {}, "data": "unknown", "production_resources": "shared"}},
    {"observation": {"usage": "unused", "data": "maybe", "production_resources": "shared"}},
    {"unexpected": "PRIVATE_SENTINEL"}, {"provider_mutation": False},
])
def test_untrustworthy_inspect_is_invalid(updates: dict[str, object]) -> None:
    code, output, calls, _ = invoke("inspect", responses=[receipt("inspect", **updates)])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "invalid"
    assert output["warnings"][0]["code"] == "invalid_response"
    assert "result" not in output or output["result"] == {}


@pytest.mark.parametrize("mode,updates", [
    ("record", {"usage": "unused"}),
    ("record", {"production_resources": "isolated"}),
    ("record", {"binding_sha256": "b" * 64}),
    ("collect", {"usage": "maybe"}),
    ("collect", {"usage": "unused"}),
    ("collect", {"usage": "unknown"}),
    ("collect", {"historical_usage_retained": True, "usage": "unknown",
                 "odoo_usage": {"reason_code": "not_collected"}}),
    ("collect", {"never_served_qualification": "never_served"}),
    ("collect", {"usage_evidence": ""}),
    ("collect", {"unexpected": "PRIVATE_SENTINEL"}),
    ("collect", {"provider_mutation": True}),
    ("record", {"provider_mutation": "false"}),
    ("record", {"mode": "collect"}),
    ("clear", {"mode": "record"}),
    ("clear", {"binding_sha256": "b" * 64}),
    ("clear", {"observation_sha256": "b" * 64}),
    ("clear", {"observation_sha256": "not-a-digest"}),
    ("clear", {"observation_sha256": None}),
    ("clear", {"provider_mutation": True}),
    ("clear", {"provider_mutation": 0}),
    ("clear", {"usage": "unused"}),
    ("clear", {"observation": None}),
    ("clear", {"unexpected": "PRIVATE_SENTINEL"}),
    # Coverage and evidence kinds are closed producer vocabularies.
    ("collect", {"source_coverage": {**COVERAGE, "PRIVATE_SENTINEL": "x"}}),
    ("collect", {"source_coverage": {"usage": "staff_testing_hold"}}),
    ("record", {"source_coverage": {**COVERAGE, "extra_source": "x"}}),
    ("collect", {"usage_evidence": f"service:other-usage-kind:{BINDING}:2026-10-19T10:00:00+00:00"}),
    ("collect", {"isolation_evidence": f"service:other-alias-kind:{BINDING}:{EVIDENCE_HASH}"}),
    # A positive fact needs evidence the producer builds for that fact.
    ("collect", {"usage_evidence": "PRIVATE_SENTINEL free text"}),
    ("collect", {"isolation_evidence": f"service:testing-resource-inspection:{EVIDENCE_HASH}"}),
    ("collect", {"production_resources": "isolated",
                 "isolation_evidence": f"service:production-target-alias:{BINDING}:{EVIDENCE_HASH}"}),
    ("collect", {"data": "reproducible", "data_evidence": f"service:odoo-testing-usage:{BINDING}"}),
    ("collect", {"data": "only_copy", "data_evidence": f"service:testing-resource-inspection:{EVIDENCE_HASH}"}),
    ("collect", {"data_evidence": f"service:testing-resource-inspection:{EVIDENCE_HASH}"}),
    ("collect", {"usage": "unused", "usage_evidence": f"service:odoo-testing-never-served:{EVIDENCE_HASH}",
                 "odoo_usage": {"recent_signins": False, "authenticated_sessions": False,
                                "reason_code": "complete"}}),
])
def test_untrustworthy_mutating_receipt_reports_no_facts_and_an_unknown_outcome(
        mode: str, updates: dict[str, object]) -> None:
    code, output, calls, _ = invoke(mode, responses=[receipt(mode, **updates), receipt("inspect")])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "outcome_unknown"
    assert [item["code"] for item in output["warnings"]][0] == "invalid_response"
    if mode != "clear":
        assert output["summary"]["recommendation"] == lane.RECOVERY
    assert output["summary"]["trace_id"] == f"launchplane_req_{mode}"
    assert "result" not in output or output["result"] == {}
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("missing", ["mode", "binding_sha256", "observation_sha256", "provider_mutation"])
def test_incomplete_clear_receipt_leaves_the_effect_unknown(missing: str) -> None:
    response = receipt("clear")
    response["result"].pop(missing)
    code, output, calls, _ = invoke("clear", responses=[response])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "outcome_unknown"
    assert output["result"] == {}


@pytest.mark.parametrize("window", [
    lambda: times(observed_offset=-lane.DEFAULT_LIFETIME_SECONDS - 1),
    lambda: times(observed_offset=lane.CLOCK_SKEW_SECONDS + 60),
    lambda: times(lifetime=lane.DEFAULT_LIFETIME_SECONDS + 1),
    lambda: times(lifetime=0),
    lambda: {**times(), "expires_at": "not-a-time"},
])
def test_collect_outside_its_requested_window_is_refused(window: Callable[[], dict[str, str]]) -> None:
    code, output, calls, _ = invoke("collect", responses=[receipt("collect", **window()), receipt("inspect")])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "outcome_unknown"
    assert "result" not in output or output["result"] == {}


def test_collect_lifetime_is_bounded_by_the_requested_lifetime() -> None:
    code, output, calls, _ = invoke("collect", body=scope(lifetime_seconds=120),
                                    responses=[receipt("collect", **times(lifetime=121)), receipt("inspect")])
    assert code != 0 and len(calls) == 1 and output["status"] == "outcome_unknown"
    code, output, _, _ = invoke("collect", body=scope(lifetime_seconds=120),
                                responses=[receipt("collect", **times(lifetime=120)), receipt("inspect")])
    assert code == 0, output


def test_unrecognized_usage_reason_is_normalized_as_the_producer_client_does() -> None:
    usage = {"recent_signins": True, "authenticated_sessions": False, "reason_code": "future_probe_reason"}
    code, output, _, _ = invoke("collect", responses=[receipt("collect", odoo_usage=usage), receipt("inspect")])
    assert code == 0, output
    assert output["result"]["odoo_usage"]["reason_code"] == lane.UNRECOGNIZED_REASON
    assert "future_probe_reason" not in json.dumps(output)


def test_preview_collect_reports_recorded_evidence_kind_only() -> None:
    body = scope(preview=True)
    collected = receipt("collect", usage="in_use", usage_evidence="PRIVATE_SENTINEL recorded",
                        production_resources="separation_ready", isolation_evidence="PRIVATE_SENTINEL ref",
                        odoo_usage={"reason_code": "unsupported_lane"})
    code, output, _, _ = invoke("collect", body=body, inspect_body=body, responses=[collected, receipt("inspect")])
    assert code == 0, output
    assert output["result"]["evidence"]["usage"]["kind"] == "recorded"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)
    testing = receipt("collect", usage_evidence="PRIVATE_SENTINEL recorded")
    code, output, _, _ = invoke("collect", responses=[testing, receipt("inspect")])
    assert code != 0 and output["status"] == "outcome_unknown"


@pytest.mark.parametrize("blank", ["", " "])
def test_preview_collect_refuses_a_recorded_fact_without_a_reference(blank: str) -> None:
    body = scope(preview=True)
    collected = receipt("collect", usage="in_use", usage_evidence=blank,
                        odoo_usage={"reason_code": "unsupported_lane"})
    code, output, _, _ = invoke("collect", body=body, inspect_body=body, responses=[collected, receipt("inspect")])
    assert code != 0 and output["status"] == "outcome_unknown"
    assert "result" not in output or not output["result"]


def test_collect_projects_evidence_kind_only_and_omits_the_observer() -> None:
    code, output, _, _ = invoke("collect")
    assert code == 0, output
    assert output["result"]["evidence"] == {
        "usage": {"present": True, "kind": "odoo-testing-usage"},
        "data": {"present": False},
        "production_resources": {"present": True, "kind": "production-target-alias"},
    }
    assert output["result"]["odoo_usage"]["reason_code"] == "complete"
    text = json.dumps(output)
    assert EVIDENCE_HASH not in text and ACTOR not in text and "observed_by" not in text


@pytest.mark.parametrize("mode", ["collect", "record", "clear", "prepare-rebuild"])
@pytest.mark.parametrize("failure", [TimeoutError("PRIVATE_SENTINEL"), ConnectionResetError("PRIVATE_SENTINEL"),
                                     h.ServiceResponseError({"http_status": 202}), http_error(503, "unavailable"),
                                     http_error(408, "request_timeout")])
def test_lost_mutating_exchange_is_unknown_and_never_retried(mode: str, failure: Exception) -> None:
    code, output, calls, _ = invoke(mode, responses=[failure])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "outcome_unknown"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_inspect_transport_loss_is_an_unavailable_read() -> None:
    code, output, calls, _ = invoke("inspect", responses=[TimeoutError("PRIVATE_SENTINEL")])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "unavailable"


@pytest.mark.parametrize("mode", ["collect", "record", "clear", "prepare-rebuild"])
@pytest.mark.parametrize("readback", [receipt("inspect", binding_sha256="b" * 64), TimeoutError("PRIVATE_SENTINEL"),
                                      receipt("inspect", usage="in_use"), http_error(404, "disposable_lane_not_found")])
def test_unconfirmed_readback_leaves_the_write_accepted_unverified(
        mode: str, readback: dict[str, Any] | Exception) -> None:
    code, output, calls, _ = invoke(mode, responses=[receipt(mode), readback])
    assert code != 0 and len(calls) == 2
    assert output["status"] == "accepted_unverified"
    assert output["result"]["mode"] == mode
    assert output["result"].get("binding_readback_matches") is not True
    assert output["summary"]["trace_id"] == f"launchplane_req_{mode}"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("readback", [
    receipt("inspect"),
    receipt("inspect", observation_sha256=EMPTY_OBSERVATION_HASH,
            observation={"usage": "unused", "data": "reproducible", "production_resources": "isolated"}),
    receipt("inspect", observation={"usage": "in_use", "data": "only_copy", "production_resources": "shared"}),
])
def test_clear_requires_both_the_empty_summary_and_canonical_absent_report_digest(readback: dict[str, Any]) -> None:
    code, output, calls, _ = invoke("clear", responses=[receipt("clear"), readback])
    assert code != 0 and len(calls) == 2
    assert output["status"] == "accepted_unverified"
    assert output["result"]["binding_readback_matches"] is True
    assert output["result"]["observation_readback_matches"] is False
    assert output["result"]["effect_readback"] != "verified"
    assert output["result"]["provider_mutation"] is False
    assert not output["result"].keys() & lane.FACTS.keys()


def test_prepare_rebuild_binds_the_operation_but_does_not_claim_capture_readback() -> None:
    code, output, calls, _ = invoke("prepare-rebuild")
    assert code == 0, output
    assert [call["body"] for call in calls] == [
        {**scope(), "replaces_operation_id": REPLACED_OPERATION, "mode": "prepare-rebuild",
         "expected_binding_sha256": BINDING},
        {**scope(), "mode": "inspect"},
    ]
    assert all(call["path"] == ROUTE and "idempotency_key" not in call for call in calls)
    assert output["result"]["operation_id_matches"] is True
    assert output["result"]["historical_effects"] == "unknown"
    assert output["result"]["provider_mutation"] is False
    assert output["result"]["binding_readback_matches"] is True
    assert output["result"]["effect_readback"] == "unavailable"
    assert not output["result"].keys() & (lane.FACTS.keys() | {"observation", "operation_id"})
    assert all(value not in json.dumps(output) for value in (REPLACED_OPERATION, "example-testing", ACTOR))


@pytest.mark.parametrize("body", [
    scope(), scope(replaces_operation_id=""), scope(replaces_operation_id=" "),
    scope(replaces_operation_id=None), scope(replaces_operation_id=123),
    scope(replaces_operation_id="../other"), scope(replaces_operation_id="op\nprivate"),
    scope(instance="prod", replaces_operation_id=REPLACED_OPERATION),
    scope(instance="staging", replaces_operation_id=REPLACED_OPERATION),
    scope(preview=True, replaces_operation_id=REPLACED_OPERATION),
    scope(preview_id="", replaces_operation_id=REPLACED_OPERATION),
    scope(usage="unknown", replaces_operation_id=REPLACED_OPERATION),
    scope(lifetime_seconds=120, replaces_operation_id=REPLACED_OPERATION),
    scope(retained_target_id="PRIVATE_SENTINEL", replaces_operation_id=REPLACED_OPERATION),
    scope(expected_binding_sha256=BINDING, replaces_operation_id=REPLACED_OPERATION),
])
def test_prepare_rebuild_refuses_unsupported_intent_before_configuration(body: dict[str, object]) -> None:
    code, _, calls, settings = invoke("prepare-rebuild", body=body)
    assert code != 0 and calls == [] and settings == 0


@pytest.mark.parametrize("updates", [
    {"operation_id": "another-operation"}, {"operation_id": None}, {"operation_id": []},
    {"historical_effects": "none"}, {"historical_effects": None}, {"provider_mutation": True},
    {"provider_mutation": 0}, {"binding_sha256": "b" * 64}, {"mode": "record"},
    {"usage": "unused"}, {"retained_target_id": "PRIVATE_SENTINEL"}, {"extra": "PRIVATE_SENTINEL"},
])
def test_prepare_rebuild_refuses_a_mismatched_or_overclaiming_receipt(updates: dict[str, object]) -> None:
    code, output, calls, _ = invoke("prepare-rebuild", responses=[receipt("prepare-rebuild", **updates)])
    assert code != 0 and len(calls) == 1 and output["status"] == "outcome_unknown"
    assert "result" not in output or output["result"] == {}
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("field", ["operation_id", "historical_effects", "provider_mutation", "binding_sha256"])
def test_prepare_rebuild_requires_the_complete_service_receipt(field: str) -> None:
    response = receipt("prepare-rebuild")
    response["result"].pop(field)
    code, output, calls, _ = invoke("prepare-rebuild", responses=[response])
    assert code != 0 and len(calls) == 1 and output["status"] == "outcome_unknown"


@pytest.mark.parametrize("refused", [False, True])
def test_prepare_rebuild_can_be_explicitly_resubmitted_after_an_unknown_exchange(refused: bool) -> None:
    code, output, calls, _ = invoke("prepare-rebuild", responses=[TimeoutError("PRIVATE_SENTINEL")])
    assert code != 0 and output["status"] == "outcome_unknown" and len(calls) == 1
    responses = ([http_error(409, "disposable_observation_refused")] if refused else
                 [receipt("prepare-rebuild"), receipt("inspect")])
    retried_code, retried, retry_calls, _ = invoke("prepare-rebuild", responses=responses)
    assert calls[0]["body"] == retry_calls[0]["body"]
    if refused:
        assert retried_code != 0 and len(retry_calls) == 1
        assert not retried.get("result")
    else:
        assert retried_code == 0 and len(retry_calls) == 2
        assert retried["result"]["historical_effects"] == "unknown"
        assert retried["result"]["effect_readback"] == "unavailable"


@pytest.mark.parametrize("mode", lane.MODES)
@pytest.mark.parametrize("status,code", [(403, "authorization_denied"), (404, "disposable_lane_not_found"),
                                         (409, "disposable_observation_refused")])
def test_refusal_keeps_its_trace_without_switching_route_or_identity(mode: str, status: int, code: str) -> None:
    code_out, output, calls, _ = invoke(mode, responses=[http_error(status, code)])
    assert code_out != 0 and len(calls) == 1
    assert calls[0]["path"] == ROUTE and set(calls[0]) == {"service_url", "path", "settings", "body", "timeout"}
    assert output["summary"]["trace_id"] == "launchplane_req_refusal"
    assert output["summary"]["error_code"] == code
    assert output["status"] == h._status_for_http_error(status, {"error": {"code": code}})
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
