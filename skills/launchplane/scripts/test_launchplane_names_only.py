#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///
"""Names-only reads through the CLI and synthetic HTTP responses; no live data."""

import io
import json
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import pytest

from test_launchplane_helpers import write_action as helper


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())
        self.status = 200


def invoke(kind, instance, response, *, failure=None):
    command = f"{'instance' if instance else 'context'}-{kind}-read"
    args = [command, "--context", "example-site"]
    if instance:
        args += ["--instance", instance]
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch.object(helper, "prepare_operator_settings", return_value={
        "service_url": "https://service.example.invalid", "token": "PRIVATE_SENTINEL",
    }), patch.object(helper, "safe_urlopen", return_value=Response(response),
                     side_effect=failure) as transport, \
            redirect_stdout(stdout), redirect_stderr(stderr):
        code = helper.main(args)
    assert "PRIVATE_SENTINEL" not in stdout.getvalue() + stderr.getvalue()
    return code, json.loads(stdout.getvalue()), transport


def body(kind, instance):
    records = ([{
        "scope": "global", "context": "", "instance": "",
        "keys": ["ODOO_VERSION", "EXAMPLE_PASSWORD"],
        "withheld_keys": ["EXAMPLE_PASSWORD"],
        "values": {"ODOO_VERSION": "PRIVATE_SENTINEL"},
        "updated_at": "2026-10-17T00:00:00Z",
    }] if kind == "runtime-settings" else [{
        "scope": "global", "context": "", "instance": "",
        "name": "EXAMPLE_PASSWORD", "integration": "runtime_environment",
        "status": "configured", "description": "PRIVATE_SENTINEL",
        "recent_audit_events": [{"metadata": {"value": "PRIVATE_SENTINEL"}}],
    }])
    return {"status": "ok", "trace_id": "launchplane_req_names_fixture",
            "context": "example-site", "instance": instance,
            "settings" if kind == "runtime-settings" else "secrets": records}


@pytest.mark.parametrize("kind", ["runtime-settings", "secrets"])
@pytest.mark.parametrize("instance", ["", "testing"])
def test_cli_reads_names_and_preserves_inherited_global_metadata(kind, instance):
    response = body(kind, instance)
    code, payload, transport = invoke(kind, instance, response)
    assert code == 0
    request = transport.call_args.args[0]
    suffix = f"/instances/{instance}" if instance else ""
    assert request.method == "GET"
    assert request.full_url == f"https://service.example.invalid/v1/contexts/example-site{suffix}/{kind}"
    assert request.data is None and transport.call_count == 1
    field = "settings" if kind == "runtime-settings" else "secrets"
    expected_fields = ({"scope", "context", "instance", "keys", "withheld_keys"}
                       if kind == "runtime-settings" else {"name", "integration", "scope", "status"})
    assert payload["result"][field] == [
        {key: value for key, value in response[field][0].items() if key in expected_fields}
    ]
    assert payload["summary"]["trace_id"] == response["trace_id"]


@pytest.mark.parametrize("kind", ["runtime-settings", "secrets"])
def test_values_and_unknown_nested_fields_never_reach_output(kind):
    response = body(kind, "testing")
    field = "settings" if kind == "runtime-settings" else "secrets"
    response["value"] = "PRIVATE_SENTINEL"
    response[field][0].update(value="PRIVATE_SENTINEL", ciphertext="PRIVATE_SENTINEL",
                              future={"nested": "PRIVATE_SENTINEL"})
    code, _, _ = invoke(kind, "testing", response)
    assert code == 0


@pytest.mark.parametrize("kind", ["runtime-settings", "secrets"])
def test_malformed_metadata_fails_closed_without_raw_response(kind):
    response = body(kind, "testing")
    field = "settings" if kind == "runtime-settings" else "secrets"
    response[field][0]["scope"] = {"value": "PRIVATE_SENTINEL"}
    code, payload, _ = invoke(kind, "testing", response)
    assert code == 1 and payload["status"] == "invalid"


def test_unusual_setting_key_is_counted_without_failing_shared_global_read():
    response = body("runtime-settings", "testing")
    response["settings"][0]["keys"] = ["web.base-url", "ODOO_VERSION", "K" * 129]
    code, payload, _ = invoke("runtime-settings", "testing", response)
    assert code == 0
    assert payload["result"]["settings"][0]["keys"] == ["ODOO_VERSION"]
    assert payload["result"]["dropped_name_count"] == 2


@pytest.mark.parametrize("kind", ["runtime-settings", "secrets"])
def test_denial_keeps_trace_and_never_retries(kind):
    error = urllib.error.HTTPError("https://service.example.invalid", 403,
                                  "PRIVATE_SENTINEL", {}, Response({
        "trace_id": "launchplane_req_denied_fixture",
        "error": {"code": "authorization_denied", "message": "PRIVATE_SENTINEL"},
    }))
    code, payload, transport = invoke(kind, "testing", {}, failure=error)
    assert code == 1 and transport.call_count == 1
    assert payload["summary"]["trace_id"] == "launchplane_req_denied_fixture"
    assert payload["summary"]["error_code"] == "authorization_denied"


@pytest.mark.parametrize("kind", ["runtime-settings", "secrets"])
@pytest.mark.parametrize("case", ["empty", "bounded", "wrong_lane", "bad_list", "unsafe_name"])
def test_inventory_limits_and_unusable_responses(kind, case):
    response = body(kind, "testing")
    field = "settings" if kind == "runtime-settings" else "secrets"
    if case == "empty":
        response[field] = []
    elif case == "bounded":
        response[field] *= helper.NAMES_ONLY_MAX_ITEMS + 1
        if kind == "runtime-settings":
            response[field][0]["keys"] = ["EXAMPLE_KEY"] * (helper.NAMES_ONLY_MAX_ITEMS + 1)
    elif case == "wrong_lane":
        response["instance"] = "prod"
    elif case == "bad_list":
        response[field] = {"value": "PRIVATE_SENTINEL"}
    else:
        record = response[field][0]
        if kind == "runtime-settings":
            record["keys"] = ["ghp_abcdef0123456789"]
        else:
            record["name"] = "ghp_abcdef0123456789"
    code, payload, _ = invoke(kind, "testing", response)
    if case in {"empty", "bounded"}:
        assert code == 0
        assert payload["result"]["records_truncated"] == (case == "bounded")
        assert len(payload["result"][field]) == (helper.NAMES_ONLY_MAX_ITEMS if case == "bounded" else 0)
        assert payload["result"]["names_truncated"] == (case == "bounded" and kind == "runtime-settings")
    else:
        assert code == 1 and payload["status"] == "invalid"
        assert "ghp_abcdef0123456789" not in json.dumps(payload)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))


@pytest.mark.parametrize("name", ["desk-marker", "helpdesk-ssh", "task-review", "risk-check"])
def test_ordinary_identifier_survives_names_only_projection(name: str) -> None:
    response = body("secrets", "testing")
    response["secrets"][0]["name"] = name
    code, payload, _ = invoke("secrets", "testing", response)
    assert code == 0
    assert payload["result"]["secrets"][0]["name"] == name


@pytest.mark.parametrize("name", ["sk-synthetic", "ref/sk-synthetic", "ref:sk-synthetic", "ghp_synthetic"])
def test_token_like_secret_name_is_rejected_without_output(name: str) -> None:
    response = body("secrets", "testing")
    response["secrets"][0]["name"] = name
    code, payload, _ = invoke("secrets", "testing", response)
    assert code != 0
    assert name not in json.dumps(payload)
