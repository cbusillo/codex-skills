#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
"""Workflow-rule retirement reads bind the exact selection and never print private scope."""

import io
import json
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from test_launchplane_helpers import contract, write_action as h
import launchplane_workflow_rule_retirement as retirement

INSPECT, PLAN = "workflow-rule-retirement-inspect", "workflow-rule-retirement-plan"
SERVICE = "https://plane.example.invalid"
POLICY = "a" * 64
MANAGED, UNMANAGED = "b" * 64, "c" * 64


def private_request():
    return {"policy_record_id": "authz-policy-1", "policy_revision": 3, "policy_sha256": POLICY,
        "selection": [
            {"rule_sha256": MANAGED, "managed_set_id": "set-1", "managed_rule_id": "rule-1"},
            {"rule_sha256": UNMANAGED, "managed_set_id": None, "managed_rule_id": None},
        ]}


def response(mode="inspect", **updates):
    # Producer workflow_rule_retirement_plan (control_plane/authz_rule_retirement.py):
    # scope is the rule's model_dump(exclude_none=True). Independent of the consumer allowlist.
    result = {"policy_record_id": "authz-policy-1", "policy_revision": 3, "policy_sha256": POLICY,
        "selected_rules": [
            {"rule_sha256": MANAGED, "management": "managed", "broad_or_shared_scope": False, "scope": {
                "managed_set_id": "set-1", "managed_rule_id": "rule-1", "products": ["example"],
                "contexts": ["example-testing"], "instances": [], "actions": ["deploy.write"],
                "repository": "owner/repo", "repository_id": "123", "repository_owner_id": "456",
                "workflow_refs": ["owner/repo/.github/workflows/deploy.yml@refs/heads/main"],
                "job_workflow_refs": [], "event_names": ["push"], "refs": ["refs/heads/*"],
                "environments": []}},
            {"rule_sha256": UNMANAGED, "management": "unmanaged", "broad_or_shared_scope": True, "scope": {
                "products": ["*"], "contexts": [], "instances": [], "actions": ["deploy.write"],
                "repository": "owner/repo", "repository_id": "", "repository_owner_id": "",
                "workflow_refs": [], "job_workflow_refs": [], "event_names": [], "refs": [],
                "environments": []}},
        ],
        "dependency_verification": "unknown_requires_source_review", "apply_supported": False,
        "blockers": []}
    if mode == "plan":
        result.update(candidate_policy_sha256="d" * 64, surviving_workflow_rule_count=4,
            survivor_rules_sha256="e" * 64, other_principal_collections_preserved=True, plan_sha256="f" * 64)
    result.update(updates)
    return {"status": "ok", "trace_id": "launchplane_req_rule_retirement", "result": result}


def invoke(command=INSPECT, *, body=None, reply=None, repo_local=False):
    output = io.StringIO()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repo = root / "repo"
        repo.mkdir()
        payload = (repo if repo_local else root) / "request.json"
        payload.write_text(json.dumps(private_request() if body is None else body))
        mode = "plan" if command == PLAN else "inspect"
        with (patch.object(h, "active_repo_root", return_value=repo),
            patch.object(h, "prepare_operator_settings", return_value={"service_url": SERVICE}),
            patch.object(h, "request_launchplane", return_value=response(mode)) as post,
            redirect_stdout(output)):
            if reply is not None:
                post.side_effect = [reply]
            code = h.main([command, "--payload-file", str(payload)])
        return code, json.loads(output.getvalue()), list(post.call_args_list), str(payload)


@pytest.mark.parametrize("command,mode", [(INSPECT, "inspect"), (PLAN, "plan")])
def test_read_posts_exact_selection_and_projects_rules(command, mode):
    code, output, calls, payload_path = invoke(command)
    assert code == 0 and output["status"] == "ok" and len(calls) == 1
    call = calls[0].kwargs
    assert call["path"] == contract.helper_command_path(command)
    assert call["body"] == {**private_request(), "mode": mode}
    assert not call.get("idempotency_key")
    assert output["request"] == {"mode": mode, "payload_source": "private_file", "selection_count": 2}
    assert payload_path not in json.dumps(output)
    result = output["result"]
    assert result["apply_supported"] is False
    assert result["dependency_verification"] == retirement.SOURCE_REVIEW_REQUIRED
    managed, unmanaged = result["selected_rules"]
    assert (managed["management"], managed["managed_set_id"], managed["managed_rule_id"]) == ("managed", "set-1", "rule-1")
    assert managed["scope"]["refs"] == ["refs/heads/*"]
    assert managed["scope"]["repository_id"] == "123"
    assert managed["scope_complete"] is True
    assert (unmanaged["management"], unmanaged["managed_set_id"]) == ("unmanaged", None)
    assert unmanaged["scope"]["products"] == ["*"]
    assert output["summary"]["trace_id"] == response()["trace_id"]
    assert output["summary"]["selected_rule_count"] == 2
    assert output["summary"]["broad_rule_count"] == 1
    assert output["summary"]["blocker_codes"] == []
    plan_fields = {"candidate_policy_sha256", "surviving_workflow_rule_count", "survivor_rules_sha256",
        "other_principal_collections_preserved", "plan_sha256"}
    if mode == "plan":
        assert {key: result[key] for key in plan_fields} == {key: response("plan")["result"][key] for key in plan_fields}
    else:
        assert not plan_fields & result.keys()


def test_blockers_are_reported_as_codes():
    code, output, _, _ = invoke(reply=response(blockers=["last_administrator_protection"]))
    assert code == 0
    assert output["result"]["blockers"] == ["last_administrator_protection"]
    assert output["summary"]["blocker_codes"] == ["last_administrator_protection"]


def test_unlisted_and_invalid_fields_are_dropped_by_path():
    reply = response(note="PRIVATE_SENTINEL")
    rule = reply["result"]["selected_rules"][0]
    rule["description"] = "PRIVATE_SENTINEL"
    rule["scope"]["subjects"] = ["PRIVATE_SENTINEL"]
    rule["scope"]["environments"] = ["production", "has space PRIVATE_SENTINEL"]
    code, output, _, _ = invoke(reply=reply)
    assert code == 0 and "PRIVATE_SENTINEL" not in json.dumps(output)
    assert set(output["result"]["dropped_field_paths"]) == {
        "result.<unlisted field>", "selected_rules[].<unlisted field>",
        "selected_rules[].scope.<unlisted field>", "selected_rules[].scope.environments"}
    managed, unmanaged = output["result"]["selected_rules"]
    assert managed["scope"]["environments"] == ["production"]
    assert managed["scope_complete"] is False and unmanaged["scope_complete"] is True


def test_token_like_scope_value_invalidates_the_read():
    reply = response()
    reply["result"]["selected_rules"][1]["scope"]["refs"] = ["ghp_PRIVATESENTINEL"]
    code, output, _, _ = invoke(reply=reply)
    assert code == 1 and output["status"] == "invalid"
    assert "PRIVATESENTINEL" not in json.dumps(output)


def _set(path, value):
    def edit(body):
        target = body
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return edit


@pytest.mark.parametrize("edit", [
    lambda body: body.update(selection=[{"rule_sha256": f"{index:064x}", "managed_set_id": None,
        "managed_rule_id": None} for index in range(retirement.MAX_SELECTION + 1)]),
    lambda body: body.update(selection=[]),
    lambda body: body["selection"].append(dict(body["selection"][0])),
    _set(("selection", 0, "rule_sha256"), "B" * 64),
    _set(("selection", 0, "managed_rule_id"), None),
    _set(("selection", 1, "managed_set_id"), "set-2"),
    _set(("selection", 0, "extra"), True),
    _set(("mode",), "inspect"),
    _set(("reason",), "PRIVATE_SENTINEL"),
    _set(("policy_revision",), 0),
    _set(("policy_revision",), True),
    _set(("policy_sha256",), "a" * 63),
    _set(("policy_record_id",), "../escape PRIVATE_SENTINEL"),
])
def test_invalid_payload_is_refused_before_post(edit):
    body = private_request()
    edit(body)
    code, output, calls, _ = invoke(body=body)
    assert code == 2 and output["status"] == "invalid" and calls == []
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_max_selection_is_accepted():
    body = private_request()
    body["selection"] = [{"rule_sha256": f"{index:064x}", "managed_set_id": None, "managed_rule_id": None}
        for index in range(retirement.MAX_SELECTION)]
    _, _, calls, _ = invoke(body=body)
    assert len(calls) == 1


def test_repository_local_payload_is_refused_before_post():
    code, output, calls, _ = invoke(repo_local=True)
    assert code == 2 and output["status"] == "invalid" and calls == []


def _drop_rule(reply):
    reply["result"]["selected_rules"].pop()


def _extra_rule(reply):
    reply["result"]["selected_rules"].append({**reply["result"]["selected_rules"][1], "rule_sha256": "9" * 64})


@pytest.mark.parametrize("command,change", [
    (INSPECT, lambda reply: reply["result"].update(policy_sha256="9" * 64)),
    (INSPECT, lambda reply: reply["result"].update(policy_revision=4)),
    (INSPECT, lambda reply: reply["result"].update(policy_record_id="other")),
    (INSPECT, lambda reply: reply["result"].update(apply_supported=True)),
    (INSPECT, _drop_rule),
    (INSPECT, _extra_rule),
    (INSPECT, lambda reply: reply["result"]["selected_rules"][1].update(management="managed")),
    (INSPECT, lambda reply: reply["result"]["selected_rules"][0]["scope"].update(managed_rule_id="other")),
    (INSPECT, lambda reply: reply["result"].update(plan_sha256="f" * 64)),
    (INSPECT, lambda reply: reply.update(status="accepted")),
    (INSPECT, lambda reply: reply.update(records={"PRIVATE_SENTINEL": 1})),
    (INSPECT, lambda reply: reply["result"]["selected_rules"][1]["scope"].pop("workflow_refs")),
    (INSPECT, lambda reply: reply["result"]["selected_rules"][0]["scope"].pop("repository_id")),
    (INSPECT, lambda reply: reply["result"]["selected_rules"][0]["scope"].update(repository=None)),
    (INSPECT, lambda reply: reply["result"]["selected_rules"][1]["scope"].update(actions="deploy.write")),
    (INSPECT, lambda reply: reply["result"].update(dependency_verification="verified")),
    (PLAN, lambda reply: reply["result"].pop("plan_sha256")),
    (PLAN, lambda reply: reply["result"].update(surviving_workflow_rule_count=True)),
])
def test_response_drift_fails_closed(command, change):
    reply = response("plan" if command == PLAN else "inspect")
    change(reply)
    code, output, calls, _ = invoke(command, reply=reply)
    assert code == 1 and output["status"] == "invalid" and len(calls) == 1
    assert output["result"] == {}
    assert output["summary"]["trace_id"] == reply["trace_id"]
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def http_error(status, code):
    body = {"trace_id": "launchplane_req_refusal", "error": {"code": code, "message": "PRIVATE_SENTINEL"}}
    return urllib.error.HTTPError(SERVICE, status, "PRIVATE_SENTINEL", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.mark.parametrize("status,code,expected", [
    (403, "authorization_denied", "denied"),
    (409, "workflow_rule_selection_changed", "stale"),
    (409, "active_authz_policy_ambiguous", "stale"),
    (400, "invalid_request", "invalid_request"),
    (401, "unauthorized", "unauthorized"),
])
def test_http_refusals_keep_trace_and_read_classification(status, code, expected):
    exit_code, output, calls, _ = invoke(reply=http_error(status, code))
    assert exit_code == 1 and output["status"] == expected and len(calls) == 1
    assert output["summary"]["trace_id"] == "launchplane_req_refusal"
    assert output["summary"]["error_code"] == code
    assert output["warnings"] == [h.summarize_http_error(operation=INSPECT, request={},
        exc=http_error(status, code))["warnings"][0]]
    if status == 409:
        assert output["summary"]["recommendation"] == retirement.STALE_RECOMMENDATION
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_client_timeout_is_a_read_budget_diagnostic():
    code, output, calls, _ = invoke(reply=TimeoutError("PRIVATE_SENTINEL"))
    assert code == 1 and output["status"] == "unavailable" and len(calls) == 1
    assert output["summary"]["error_code"] == "client_timeout"
    assert "PRIVATE_SENTINEL" not in json.dumps(output)


def test_scope_keeps_ordinary_repository_and_context_identifiers() -> None:
    request = private_request()
    request["policy_record_id"] = "authz-desk-policy"
    managed = request["selection"][0]
    managed.update(managed_set_id="helpdesk-rules", managed_rule_id="task-rule")
    reply = response()
    reply["result"]["policy_record_id"] = request["policy_record_id"]
    scope = reply["result"]["selected_rules"][0]["scope"]
    scope.update(repository="owner/helpdesk-app", contexts=["kiosk-site", "helpdesk-*"],
        workflow_refs=["owner/kiosk-app/.github/workflows/*.yml@refs/heads/*"],
        **{key: managed[key] for key in retirement.SCOPE_MANAGED_FIELDS})
    code, payload, _, _ = invoke(body=request, reply=reply)
    assert code == 0
    assert payload["result"]["policy_record_id"] == request["policy_record_id"]
    assert all(payload["result"]["selected_rules"][0][key] == managed[key] for key in retirement.SCOPE_MANAGED_FIELDS)
    projected = payload["result"]["selected_rules"][0]["scope"]
    assert projected["repository"] == scope["repository"]
    assert projected["contexts"] == scope["contexts"]
    assert projected["workflow_refs"] == scope["workflow_refs"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
