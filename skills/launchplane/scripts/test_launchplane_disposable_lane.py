#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
# ///
"""Disposable-lane callers bind review, refuse unsafe facts and never guess outcomes."""

import io
import json
import urllib.error
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import pytest

from test_launchplane_helpers import write_action as h
import launchplane_contract as contract
import launchplane_disposable_lane as lane

SERVICE = "https://plane.example.invalid"
BINDING = "a" * 64
ROUTE = contract.operation_path("record_disposable_lane_safety")
# Built at run time so the public-safety scanner sees no token-shaped literal.
EVIDENCE_HASH = "c" * 64
ACTOR = "PRIVATE_ACTOR_SENTINEL"
COVERAGE = {"usage": "staff_testing_hold", "data": "qualified_resettable_odoo_generation",
            "production_resources": "same_kind_production_target_alias", "historical_generations": "not_current"}


def times(observed_offset=-1, lifetime=lane.DEFAULT_LIFETIME_SECONDS):
    """Observation window relative to now, as the producer writes it."""
    observed = datetime.now(UTC) + timedelta(seconds=observed_offset)
    return {"observed_at": observed.isoformat(), "expires_at": (observed + timedelta(seconds=lifetime)).isoformat()}


def scope(preview=False, **updates):
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
                   "automatic_fact_collection": "unavailable", "source_coverage": COVERAGE,
                   "reconcile_requested": False},
    }[kind]
    return {"status": "accepted", "trace_id": f"launchplane_req_{kind}", "records": {"product_profile": "example"},
            "result": {**result, **updates}}


def preview_record(**updates):
    return receipt("record", observer_kind="director_agent_preview_cleanup", reconcile_requested=True,
                   usage="unused", data="reproducible", **updates)


def invoke(mode="inspect", *, body=None, inspect_body=None, responses=None, edit_evidence=None,
           service=SERVICE, reviewed=True, expected=BINDING):
    output = io.StringIO()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "repo").mkdir()
        payload = root / "request.json"
        evidence_path = root / "inspect.json"
        payload.write_text(json.dumps(inspect_body or scope()))
        with (patch.object(h, "active_repo_root", return_value=root / "repo"),
              patch.object(h, "prepare_operator_settings", return_value={"service_url": SERVICE}) as settings,
              patch.object(h, "request_launchplane", return_value=receipt("inspect")) as post,
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
            payload.write_text(json.dumps(body or scope()))
            settings.return_value = {"service_url": service}
            post.side_effect = responses if responses is not None else (
                [receipt("inspect")] if mode == "inspect" else [receipt(mode), receipt("inspect")])
            argv = [lane.PREFIX + mode, "--payload-file", str(payload)]
            if mode != "inspect":
                argv += ["--expected-binding-sha256", expected, "--inspect-evidence-file", str(evidence_path)]
                if reviewed:
                    argv.append("--reviewed-inspect")
            code = h.main(argv)
            return code, json.loads(output.getvalue()), [call.kwargs for call in post.call_args_list], settings.call_count


def http_error(status, code):
    body = {"trace_id": "launchplane_req_refusal", "error": {"code": code, "message": "PRIVATE_SENTINEL"}}
    return urllib.error.HTTPError(SERVICE, status, "PRIVATE_SENTINEL", Message(), io.BytesIO(json.dumps(body).encode()))


@pytest.mark.parametrize("mode", lane.MODES)
def test_each_mode_sends_its_exact_body_and_rechecks_the_binding(mode):
    body = scope(lifetime_seconds=120) if mode in {"collect", "record"} else scope()
    responses = [receipt(mode, **times(lifetime=120)), receipt("inspect")] if mode == "collect" else None
    code, output, calls, _ = invoke(mode, body=body, responses=responses)
    assert code == 0, output
    assert all(call["path"] == ROUTE == contract.helper_command_path(lane.PREFIX + mode) for call in calls)
    expected = {**body, "mode": mode}
    if mode in {"collect", "record"}:
        expected["expected_binding_sha256"] = BINDING
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
    assert output["result"]["effect_readback"] == "unavailable"
    assert output["result"]["provider_mutation"] is False


def test_preview_record_sends_facts_and_reports_the_requested_reconcile():
    body = scope(preview=True, usage="unused", data="reproducible", usage_evidence="PRIVATE_SENTINEL ref",
                 data_evidence="PRIVATE_SENTINEL data")
    code, output, calls, _ = invoke("record", body=body, inspect_body=scope(preview=True),
                                    responses=[preview_record(), receipt("inspect")])
    assert code == 0, output
    assert calls[0]["body"] == {**body, "mode": "record", "expected_binding_sha256": BINDING}
    assert output["result"]["reconcile_requested"] is True
    assert output["result"]["usage"] == "unused"
    assert output["request"] == {"mode": "record", "payload_source": "private_file", "known_fact_count": 2}
    text = json.dumps(output)
    assert "PRIVATE_SENTINEL" not in text and "preview-12" not in text and "example-testing" not in text


@pytest.mark.parametrize("observation", [None,
    {"usage": "in_use", "data": "only_copy", "production_resources": "shared"},
    {"usage": "unused", "data": "reproducible", "production_resources": "isolated"},
])
def test_inspect_keeps_stored_observation_separate_from_fresh_unknown_facts(observation):
    code, output, calls, _ = invoke(responses=[receipt("inspect", observation=observation)])
    assert code == 0 and len(calls) == 1
    assert output["result"]["observation_sha256"] == EVIDENCE_HASH
    assert output["result"]["observation"] == observation
    assert all(output["result"][fact] == "unknown" for fact in lane.FACTS)


@pytest.mark.parametrize("removed", [("observation_sha256",), ("observation",), ("observation_sha256", "observation")])
def test_inspect_without_stored_observation_metadata_is_invalid(removed):
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
    ("record", scope(preview=True, usage="unused")),
    ("record", scope(preview=True, data="reproducible", data_evidence=" ")),
    ("record", scope(preview=True, usage="maybe", usage_evidence="ref")),
    ("record", scope(replaces_operation_id="op-1")),
])
def test_unsafe_or_unsupported_requests_refuse_before_configuration(mode, body):
    code, _, calls, settings = invoke(mode, body=body, inspect_body=scope(preview="preview_id" in body))
    assert code != 0 and calls == [] and settings == 0


@pytest.mark.parametrize("change", [
    lambda e: e.update(operation="disposable-lane-collect"),
    lambda e: e.update(status="denied"),
    lambda e: e["request"].update(mode="record"),
    lambda e: e["result"].update(binding_sha256="b" * 64),
    lambda e: e["result"].update(review_binding_sha256="b" * 64),
    lambda e: e["result"].update(service_fingerprint="b" * 64),
    lambda e: e.update(result=None),
])
@pytest.mark.parametrize("mode", ["collect", "record"])
def test_ineligible_inspect_evidence_never_reaches_transport(mode, change):
    code, _, calls, settings = invoke(mode, edit_evidence=change)
    assert code != 0 and calls == [] and settings == 0


@pytest.mark.parametrize("mode", ["collect", "record"])
def test_review_acknowledgement_digest_scope_and_service_are_bound(mode):
    code, _, calls, settings = invoke(mode, reviewed=False)
    assert code != 0 and calls == [] and settings == 0
    code, _, calls, settings = invoke(mode, expected="b" * 64)
    assert code != 0 and calls == [] and settings == 0
    code, _, calls, settings = invoke(mode, body=scope(context="other-testing"))
    assert code != 0 and calls == [] and settings == 0
    code, _, calls, _ = invoke(mode, service="https://different.example.invalid")
    assert code != 0 and calls == []


def test_clear_is_not_offered_until_the_service_binds_it():
    with pytest.raises(SystemExit), redirect_stdout(io.StringIO()), patch.object(h, "request_launchplane") as post:
        h.main([lane.PREFIX + "clear", "--payload-file", "/private/request.json"])
    assert post.call_count == 0


@pytest.mark.parametrize("updates", [{"usage": "unused"}, {"automatic_fact_collection": "unavailable"},
    {"binding_sha256": "not-a-digest"}, {"observation_sha256": "not-a-digest"},
    {"observation_sha256": None}, {"observation": "PRIVATE_SENTINEL"}, {"observation": []},
    {"observation": {}},
    {"observation": {"usage": "unknown", "data": "unknown", "production_resources": "shared", "extra": "x"}},
    {"observation": {"usage": {}, "data": "unknown", "production_resources": "shared"}},
    {"observation": {"usage": "unused", "data": "maybe", "production_resources": "shared"}},
    {"unexpected": "PRIVATE_SENTINEL"}, {"provider_mutation": False},
])
def test_untrustworthy_inspect_is_invalid(updates):
    code, output, calls, _ = invoke("inspect", responses=[receipt("inspect", **updates)])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "invalid"
    assert output["warnings"][0]["code"] == "invalid_response"
    assert "result" not in output or output["result"] == {}


@pytest.mark.parametrize("mode,updates", [
    ("record", {"usage": "unused"}),
    ("record", {"production_resources": "isolated"}),
    ("record", {"reconcile_requested": True}),
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
def test_untrustworthy_mutating_receipt_reports_no_facts_and_an_unknown_outcome(mode, updates):
    code, output, calls, _ = invoke(mode, responses=[receipt(mode, **updates), receipt("inspect")])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "outcome_unknown"
    assert [item["code"] for item in output["warnings"]][0] == "invalid_response"
    assert output["summary"]["recommendation"] == lane.RECOVERY
    assert output["summary"]["trace_id"] == f"launchplane_req_{mode}"
    assert "result" not in output or output["result"] == {}
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("window", [
    lambda: times(observed_offset=-lane.DEFAULT_LIFETIME_SECONDS - 1),
    lambda: times(observed_offset=lane.CLOCK_SKEW_SECONDS + 60),
    lambda: times(lifetime=lane.DEFAULT_LIFETIME_SECONDS + 1),
    lambda: times(lifetime=0),
    lambda: {**times(), "expires_at": "not-a-time"},
])
def test_collect_outside_its_requested_window_is_refused(window):
    code, output, calls, _ = invoke("collect", responses=[receipt("collect", **window()), receipt("inspect")])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "outcome_unknown"
    assert "result" not in output or output["result"] == {}


def test_collect_lifetime_is_bounded_by_the_requested_lifetime():
    code, output, calls, _ = invoke("collect", body=scope(lifetime_seconds=120),
                                    responses=[receipt("collect", **times(lifetime=121)), receipt("inspect")])
    assert code != 0 and len(calls) == 1 and output["status"] == "outcome_unknown"
    code, output, _, _ = invoke("collect", body=scope(lifetime_seconds=120),
                                responses=[receipt("collect", **times(lifetime=120)), receipt("inspect")])
    assert code == 0, output


def test_unrecognized_usage_reason_is_normalized_as_the_producer_client_does():
    usage = {"recent_signins": True, "authenticated_sessions": False, "reason_code": "future_probe_reason"}
    code, output, _, _ = invoke("collect", responses=[receipt("collect", odoo_usage=usage), receipt("inspect")])
    assert code == 0, output
    assert output["result"]["odoo_usage"]["reason_code"] == lane.UNRECOGNIZED_REASON
    assert "future_probe_reason" not in json.dumps(output)


def test_preview_collect_reports_recorded_evidence_kind_only():
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
def test_preview_collect_refuses_a_recorded_fact_without_a_reference(blank):
    body = scope(preview=True)
    collected = receipt("collect", usage="in_use", usage_evidence=blank,
                        odoo_usage={"reason_code": "unsupported_lane"})
    code, output, _, _ = invoke("collect", body=body, inspect_body=body, responses=[collected, receipt("inspect")])
    assert code != 0 and output["status"] == "outcome_unknown"
    assert "result" not in output or not output["result"]


def test_preview_record_must_return_exactly_the_submitted_facts():
    body = scope(preview=True, usage="unused", usage_evidence="ref")
    code, output, calls, _ = invoke("record", body=body, inspect_body=scope(preview=True),
                                    responses=[preview_record(), receipt("inspect")])
    assert code != 0 and len(calls) == 1 and output["status"] == "outcome_unknown"
    assert "result" not in output or output["result"] == {}


def test_collect_projects_evidence_kind_only_and_omits_the_observer():
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


@pytest.mark.parametrize("mode", ["collect", "record"])
@pytest.mark.parametrize("failure", [TimeoutError("PRIVATE_SENTINEL"), ConnectionResetError("PRIVATE_SENTINEL"),
                                     h.ServiceResponseError({"http_status": 202}), http_error(503, "unavailable")])
def test_lost_mutating_exchange_is_unknown_and_never_retried(mode, failure):
    code, output, calls, _ = invoke(mode, responses=[failure])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "outcome_unknown"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_inspect_transport_loss_is_an_unavailable_read():
    code, output, calls, _ = invoke("inspect", responses=[TimeoutError("PRIVATE_SENTINEL")])
    assert code != 0 and len(calls) == 1
    assert output["status"] == "unavailable"


@pytest.mark.parametrize("mode", ["collect", "record"])
@pytest.mark.parametrize("readback", [receipt("inspect", binding_sha256="b" * 64), TimeoutError("PRIVATE_SENTINEL"),
                                      receipt("inspect", usage="in_use"), http_error(404, "disposable_lane_not_found")])
def test_unconfirmed_readback_leaves_the_write_accepted_unverified(mode, readback):
    code, output, calls, _ = invoke(mode, responses=[receipt(mode), readback])
    assert code != 0 and len(calls) == 2
    assert output["status"] == "accepted_unverified"
    assert output["result"]["mode"] == mode
    assert output["result"].get("binding_readback_matches") is not True
    assert output["summary"]["trace_id"] == f"launchplane_req_{mode}"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


@pytest.mark.parametrize("mode", lane.MODES)
@pytest.mark.parametrize("status,code", [(403, "authorization_denied"), (404, "disposable_lane_not_found"),
                                         (409, "disposable_observation_refused")])
def test_refusal_keeps_its_trace_without_switching_route_or_identity(mode, status, code):
    code_out, output, calls, _ = invoke(mode, responses=[http_error(status, code)])
    assert code_out != 0 and len(calls) == 1
    assert calls[0]["path"] == ROUTE and set(calls[0]) == {"service_url", "path", "settings", "body", "timeout"}
    assert output["summary"]["trace_id"] == "launchplane_req_refusal"
    assert output["summary"]["error_code"] == code
    assert output["status"] == h._status_for_http_error(status, {"error": {"code": code}})
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
