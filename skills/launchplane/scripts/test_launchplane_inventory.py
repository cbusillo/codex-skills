#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
# ///
"""Offline request, pagination and value-free inventory regressions."""

import copy
import io
import json
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from test_launchplane_helpers import contract, write_action as helper


def response():
    return {
        "status": "ok",
        "trace_id": "launchplane_req_inventory",
        "inventory": {
            "scope": "provider_credential_visible",
            "provider_pagination": "unpaginated_project_all",
            "inventory_digest": "a" * 64,
            "total_projects": 2,
            "total_environments": 1,
            "total_targets": 1,
            "offset": 0,
            "limit": 1,
            "next_offset": 1,
            "projects": [
                {
                    "project_id": "untracked",
                    "name": "Untracked project",
                    "environments": [
                        {
                            "environment_id": "preview-env",
                            "name": "Previews",
                            "targets": [
                                {
                                    "target_id": "database",
                                    "category": "postgres",
                                    "name": "Database only",
                                    "placement": {
                                        "kind": "server",
                                        "server_id": "guest",
                                        "server_name": "apps-example-testing",
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    }


def invoke(body, *arguments, error=None):
    output = io.StringIO()
    with (
        patch.object(
            helper,
            "prepare_operator_settings",
            return_value={"service_url": "https://plane.example.invalid"},
        ),
        patch.object(
            helper, "request_launchplane_read", return_value=body, side_effect=error
        ) as read,
        patch.object(helper, "request_launchplane") as post,
        redirect_stdout(output),
    ):
        code = helper.main(["dokploy-inventory-read", "--limit", "1", *arguments])
    return code, json.loads(output.getvalue()), read, post


def test_reads_service_only_and_preserves_names_and_completeness():
    code, result, read, post = invoke(response())
    assert code == 0
    assert result["result"] == response()["inventory"]
    assert read.call_args.kwargs["path"] == contract.helper_command_path(
        "dokploy-inventory-read"
    )
    assert read.call_args.kwargs["query"] == {"offset": "0", "limit": "1"}
    post.assert_not_called()
    continuation = response()
    continuation["inventory"].update(
        offset=1,
        next_offset=None,
        projects=[{"project_id": "empty", "name": "Empty", "environments": []}],
    )
    code, result, read, post = invoke(
        continuation, "--offset", "1", "--inventory-digest", "a" * 64
    )
    assert code == 0
    assert result["result"]["projects"][0]["environments"] == []
    assert (
        read.call_args.kwargs["query"]["inventory_digest"]
        == continuation["inventory"]["inventory_digest"]
    )
    post.assert_not_called()


@pytest.mark.parametrize(
    "location", ["top", "inventory", "project", "environment", "target", "placement"]
)
def test_unexpected_fields_never_reach_output(location):
    body = response()
    inventory = body["inventory"]
    project = inventory["projects"][0]
    environment = project["environments"][0]
    target = environment["targets"][0]
    sources = {
        "top": body,
        "inventory": inventory,
        "project": project,
        "environment": environment,
        "target": target,
        "placement": target["placement"],
    }
    sources[location]["env"] = "PASSWORD=must-not-appear"
    code, result, _, post = invoke(body)
    assert code == 1
    assert "must-not-appear" not in json.dumps(result)
    assert result["result"] == {}
    post.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"next_offset": 0},
        {"projects": []},
        {"inventory_digest": "invalid"},
        {"total_projects": True},
        {"offset": 1},
        {"limit": 2},
    ],
)
def test_incomplete_or_mismatched_page_rejected(change):
    body = copy.deepcopy(response())
    body["inventory"].update(change)
    code, result, _, post = invoke(body)
    assert code == 1
    assert result["result"] == {}
    post.assert_not_called()


def test_local_host_stays_explicit_without_invented_hostname():
    body = response()
    target = body["inventory"]["projects"][0]["environments"][0]["targets"][0]
    target["placement"] = {
        "kind": "dokploy_host",
        "server_id": None,
        "server_name": None,
    }
    code, result, _, _ = invoke(body)
    assert code == 0
    assert (
        result["result"]["projects"][0]["environments"][0]["targets"][0]["placement"]
        == target["placement"]
    )


def test_bad_continuation_rejected_before_read():
    code, _, read, post = invoke(response(), "--offset", "1")
    assert code == 2
    read.assert_not_called()
    post.assert_not_called()


@pytest.mark.parametrize(
    "name",
    [
        "task-runner",
        "disk-backup",
        "helpdesk-worker",
        "odoo-production-2024",
        "hetzner-fsn1-prod-01",
        "cookie-consent",
        "credential-rotator",
        "laughs_app",
        "highs_cache",
        "  Exact  Unicode café  ",
    ],
)
def test_preserves_display_names_and_nanoid_alphabet(name):
    body = response()
    project = body["inventory"]["projects"][0]
    environment = project["environments"][0]
    target = environment["targets"][0]
    project.update(project_id="_project", name=name)
    environment.update(environment_id="-environment", name=name)
    target.update(target_id="_target", name=name)
    target["placement"].update(server_id="-server", server_name=name)
    code, result, _, post = invoke(body)
    assert code == 0
    assert result["result"] == body["inventory"]
    post.assert_not_called()


@pytest.mark.parametrize(
    "name",
    [
        "PASSWORD=must-not-appear",
        "ghp_" + "PrivateFixture" * 8,
        "Bearer PrivateFixture",
        "bad\nname",
    ],
)
def test_sensitive_or_control_text_names_fail_without_echo(name):
    body = response()
    body["inventory"]["projects"][0]["name"] = name
    code, result, _, _ = invoke(body)
    assert code == 1
    assert name not in json.dumps(result)
    assert result["summary"]["trace_id"] == body["trace_id"]


def test_http_conflict_and_malformed_body_keep_safe_diagnostics():
    body = {
        "status": "rejected",
        "trace_id": "launchplane_req_conflict",
        "error": {
            "code": "dokploy_inventory_changed",
            "message": "PASSWORD=must-not-appear",
        },
    }
    error = urllib.error.HTTPError(
        "https://plane.example.invalid",
        409,
        "Conflict",
        {},
        io.BytesIO(json.dumps(body).encode()),
    )
    code, result, _, post = invoke({}, error=error)
    assert code == 1
    assert result["summary"]["trace_id"] == body["trace_id"]
    assert "must-not-appear" not in json.dumps(result)
    post.assert_not_called()
    code, result, _, _ = invoke(
        {}, error=helper.ServiceResponseError({"http_status": 200})
    )
    assert code == 1
    assert result["summary"]["http_status"] == 200


@pytest.mark.parametrize(
    "identifier", ["ABC_ghs_defghijklmnopq", "_sk-Abcdefghijklmnopqr"]
)
def test_random_provider_id_fragments_are_not_credentials(identifier):
    body = response()
    body["inventory"]["projects"][0]["project_id"] = identifier
    code, result, _, _ = invoke(body)
    assert code == 0
    assert result["result"] == body["inventory"]


@pytest.mark.parametrize(
    "identifier", ["bad/id", "bad id", "ghp_" + "PrivateFixture" * 8]
)
def test_invalid_or_credential_shaped_identifier_rejected(identifier):
    body = response()
    body["inventory"]["projects"][0]["project_id"] = identifier
    code, result, _, _ = invoke(body)
    assert code == 1
    assert identifier not in json.dumps(result)


@pytest.mark.parametrize("count", ["total_environments", "total_targets"])
def test_single_complete_page_requires_exact_totals(count):
    body = response()
    body["inventory"].update(total_projects=1, next_offset=None)
    body["inventory"][count] += 1
    code, result, _, _ = invoke(body)
    assert code == 1
    assert result["result"] == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
