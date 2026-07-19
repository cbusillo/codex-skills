#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest",
# ]
# ///

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from http.cookiejar import Cookie
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("npmplus-ops.py")
MODULE_SPEC = importlib.util.spec_from_file_location("npmplus_ops", MODULE_PATH)
assert MODULE_SPEC is not None
npmplus_ops = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
sys.modules[MODULE_SPEC.name] = npmplus_ops
MODULE_SPEC.loader.exec_module(npmplus_ops)


PRIVATE_LITERALS = (
    "private-node-01",
    "10.99.",
    "example-internal.test",
    "private-canary",
    "private container command",
    "private hypervisor command",
)


def put_text(path: Path, value: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(value)


def test_runtime_home_prefers_code_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    code_home = tmp_path / "chris-code"
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODE_HOME", str(code_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert npmplus_ops.runtime_home() == code_home


def test_runtime_home_uses_codex_home_before_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    codex_home = tmp_path / "codex"
    monkeypatch.delenv("CODE_HOME", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    assert npmplus_ops.runtime_home() == codex_home


def make_private_repo(tmp_path: Path) -> Path:
    private_repo = tmp_path / "private"
    (private_repo / "scripts").mkdir(parents=True)
    (private_repo / ".code").mkdir()
    put_text(
        private_repo / ".code" / "local.env",
        "NPMPLUS_BASE_URL=https://npmplus.invalid\n"
        "NPMPLUS_AUTOMATION_EMAIL=robot@example.invalid\n"
        "NPMPLUS_AUTOMATION_PASSWORD=secret-value\n",
    )
    return private_repo


def write_provider(private_repo: Path, payload: dict[str, object]) -> Path:
    provider = private_repo / "scripts" / "infra-context.py"
    put_text(
        provider,
        "#!/usr/bin/env python3\n"
        "import json\n"
        f"print(json.dumps({payload!r}))\n",
    )
    return provider


def context_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "npmplus.ops.v2",
        "api": {
            "env_file": ".code/local.env",
            "base_url_env": "NPMPLUS_BASE_URL",
            "identity_env": "NPMPLUS_AUTOMATION_EMAIL",
            "secret_env": "NPMPLUS_AUTOMATION_PASSWORD",
            "expected_base_url": "https://npmplus.invalid",
            "expected_principal": {"id": 7, "email": "robot@example.invalid"},
        },
        "refs": {
            "canary": {
                "kind": "proxy_host",
                "id": 123,
                "allowed_apply_actions": ["proxy-host-enable", "proxy-host-disable"],
                "identity": {"domain_names": ["private.example.invalid"]},
                "write_evidence": {
                    "snapshot_ready": True,
                    "rollback_ready": True,
                    "external_validation_ready": True,
                },
            },
        },
        "pilot": {"default_ref": "canary"},
    }
    payload.update(overrides)
    return payload


def test_load_context_accepts_public_safe_schema(tmp_path: Path) -> None:
    private_repo = make_private_repo(tmp_path)
    provider = write_provider(private_repo, context_payload())

    context = npmplus_ops.load_context(private_repo, provider, "default")

    assert context.profile == "default"
    assert context.schema_version == "npmplus.ops.v2"
    assert context.env_file == private_repo / ".code" / "local.env"
    assert context.default_pilot_ref == "canary"
    assert context.refs == {
        "canary": {
            "kind": "proxy_host",
            "id": 123,
            "allowed_apply_actions": {"proxy-host-enable", "proxy-host-disable"},
            "identity": {"domain_names": ["private.example.invalid"]},
            "write_evidence": {
                "snapshot_ready": True,
                "rollback_ready": True,
                "external_validation_ready": True,
            },
        }
    }
    assert context.expected_base_url == "https://npmplus.invalid"
    assert context.expected_principal == {
        "id": 7,
        "email": "robot@example.invalid",
    }


def test_legacy_context_is_read_only_compatible(tmp_path: Path) -> None:
    private_repo = make_private_repo(tmp_path)
    payload = context_payload()
    payload["schema_version"] = "npmplus.ops.v1"
    api = payload["api"]
    assert isinstance(api, dict)
    api.pop("expected_base_url")
    api.pop("expected_principal")
    refs = payload["refs"]
    assert isinstance(refs, dict)
    ref = refs["canary"]
    assert isinstance(ref, dict)
    ref.pop("allowed_apply_actions")
    ref.pop("identity")
    ref.pop("write_evidence")
    payload["policy"] = {
        "allowed_apply_actions": ["proxy-host-enable", "proxy-host-disable"]
    }
    context = npmplus_ops.load_context(
        private_repo, write_provider(private_repo, payload), "default"
    )

    assert context.schema_version == "npmplus.ops.v1"
    assert context.refs["canary"]["allowed_apply_actions"] == set()
    with pytest.raises(npmplus_ops.OpsError, match="require private context schema"):
        npmplus_ops.authorize_lifecycle_write(
            context, context.refs["canary"], "proxy-host-enable"
        )



def test_context_rejects_absolute_env_file_path(tmp_path: Path) -> None:
    private_repo = make_private_repo(tmp_path)
    payload = context_payload()
    api = payload["api"]
    assert isinstance(api, dict)
    api["env_file"] = "/private/path"
    provider = write_provider(
        private_repo,
        payload,
    )

    with pytest.raises(npmplus_ops.OpsError, match="must stay inside"):
        npmplus_ops.load_context(private_repo, provider, "default")


def test_context_rejects_non_public_ref_names(tmp_path: Path) -> None:
    private_repo = make_private_repo(tmp_path)
    provider = write_provider(
        private_repo,
        context_payload(refs={"prod.internal.example": {"kind": "proxy_host", "id": 123}}),
    )

    with pytest.raises(npmplus_ops.OpsError, match="invalid ref name"):
        npmplus_ops.load_context(private_repo, provider, "default")


def test_context_provider_rejects_parent_path_escape(tmp_path: Path) -> None:
    private_repo = make_private_repo(tmp_path)
    outside_provider = tmp_path / "outside.py"
    put_text(outside_provider, "print('{}')\n")

    with pytest.raises(npmplus_ops.OpsError, match="must stay inside"):
        npmplus_ops.run_context_provider(private_repo, Path("../outside.py"), "default")


def test_context_provider_failure_redacts_output(tmp_path: Path) -> None:
    private_repo = make_private_repo(tmp_path)
    provider = private_repo / "scripts" / "infra-context.py"
    put_text(
        provider,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('secret-host.example.invalid', file=sys.stderr)\n"
        "raise SystemExit(2)\n",
    )

    with pytest.raises(npmplus_ops.OpsError) as exc_info:
        npmplus_ops.run_context_provider(private_repo, provider, "default")

    assert "secret-host" not in str(exc_info.value)
    assert "output redacted" in str(exc_info.value)


def test_summary_redacts_proxy_host_fields() -> None:
    host = {
        "id": 123,
        "domain_names": ["private.example.invalid"],
        "forward_host": "10.0.0.5",
        "forward_port": 443,
        "certificate_id": 77,
        "access_list_id": 0,
        "enabled": True,
        "http2_support": True,
        "http3_support": "enabled",
        "npmplus_auth_request": "none",
        "npmplus_noindex": True,
        "locations": [],
    }

    summary = npmplus_ops.summarize_proxy_host(host, target_ref="canary")
    rendered = json.dumps(summary, sort_keys=True)

    assert summary == {
        "access_list_id_present": False,
        "auth_request": "none",
        "certificate_id_present": True,
        "domain_count": 1,
        "enabled": True,
        "http2": True,
        "http3": True,
        "location_count": 0,
        "noindex": True,
        "target_ref": "canary",
    }
    assert "private.example.invalid" not in rendered
    assert "10.0.0.5" not in rendered
    assert "123" not in rendered


def test_inventory_summary_redacts_ids_and_domains() -> None:
    hosts = [
        {"id": 1, "domain_names": ["a.example.invalid"], "enabled": True, "npmplus_auth_request": "none"},
        {"id": 2, "domain_names": ["b.example.invalid"], "enabled": False, "npmplus_auth_request": "auth"},
    ]

    summary = npmplus_ops.summarize_proxy_host_inventory(hosts)
    rendered = json.dumps(summary, sort_keys=True)

    assert summary == {
        "auth_request_configured_count": 1,
        "count": 2,
        "disabled_count": 1,
        "enabled_count": 1,
    }
    assert "example.invalid" not in rendered
    assert '"id"' not in rendered


@pytest.mark.parametrize("cookie_name", ["token", "__Host-Http-token"])
def test_authenticate_accepts_legacy_and_host_prefixed_token_cookies(
    monkeypatch: pytest.MonkeyPatch, cookie_name: str
) -> None:
    client = npmplus_ops.NpmplusClient(
        npmplus_ops.ApiConfig(
            base_url="https://npmplus.invalid",
            identity="robot@example.invalid",
            secret="secret-value",
            timeout=1,
            expected_principal={"id": 7, "email": "robot@example.invalid"},
        )
    )

    def fake_request(
        method: str, path: str, *, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        if path == "/api/tokens":
            assert method == "POST"
            assert body == {
                "identity": "robot@example.invalid",
                "secret": "secret-value",
            }
            client.cookies.set_cookie(
                Cookie(
                    version=0,
                    name=cookie_name,
                    value="redacted",
                    port=None,
                    port_specified=False,
                    domain="npmplus.invalid",
                    domain_specified=True,
                    domain_initial_dot=False,
                    path="/",
                    path_specified=True,
                    secure=True,
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )
            return {"expires": "redacted"}
        assert method == "GET"
        assert path == "/api/users/me"
        assert body is None
        return {"id": 7, "email": "robot@example.invalid"}

    monkeypatch.setattr(client, "request", fake_request)

    assert client.authenticate() == {
        "ok": True,
        "payload_keys": ["expires"],
        "principal_verified": True,
        "token_cookie_present": True,
    }


def test_authenticate_rejects_wrong_authenticated_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = npmplus_ops.NpmplusClient(
        npmplus_ops.ApiConfig(
            base_url="https://npmplus.invalid",
            identity="robot@example.invalid",
            secret="secret-value",
            timeout=1,
            expected_principal={"id": 7, "email": "robot@example.invalid"},
        )
    )

    def fake_request(
        method: str, path: str, *, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        if path == "/api/tokens":
            client.cookies.set_cookie(
                Cookie(
                    version=0,
                    name="token",
                    value="redacted",
                    port=None,
                    port_specified=False,
                    domain="npmplus.invalid",
                    domain_specified=True,
                    domain_initial_dot=False,
                    path="/",
                    path_specified=True,
                    secure=True,
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )
            return {"expires": "redacted"}
        return {"id": 99, "email": "other@example.invalid"}

    monkeypatch.setattr(client, "request", fake_request)

    with pytest.raises(npmplus_ops.OpsError, match="authenticated principal"):
        client.authenticate()


def test_public_engine_does_not_contain_private_literals() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    for literal in PRIVATE_LITERALS:
        assert literal not in source


def test_help_does_not_contain_private_literals() -> None:
    result = subprocess.run(
        ["python3", str(MODULE_PATH), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    for literal in PRIVATE_LITERALS:
        assert literal not in result.stdout


def test_api_config_rejects_wrong_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private_repo = make_private_repo(tmp_path)
    provider = write_provider(private_repo, context_payload())
    context = npmplus_ops.load_context(private_repo, provider, "default")
    monkeypatch.setenv("NPMPLUS_BASE_URL", "https://other.invalid")
    with pytest.raises(
        npmplus_ops.OpsError,
        match="does not match private context expected instance",
    ):
        npmplus_ops.load_api_config(context, 10)


def test_api_config_rejects_wrong_principal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private_repo = make_private_repo(tmp_path)
    provider = write_provider(private_repo, context_payload())
    context = npmplus_ops.load_context(private_repo, provider, "default")
    monkeypatch.setenv("NPMPLUS_AUTOMATION_EMAIL", "other@example.invalid")
    with pytest.raises(
        npmplus_ops.OpsError,
        match="does not match private context expected principal",
    ):
        npmplus_ops.load_api_config(context, 10)


def test_verify_host_identity_accepts_match() -> None:
    host = {"id": 123, "domain_names": ["private.example.invalid"]}
    ref = {"id": 123, "identity": {"domain_names": ["private.example.invalid"]}}

    npmplus_ops.verify_host_identity(host, ref)


def test_verify_host_identity_rejects_wrong_id() -> None:
    host = {"id": 999, "domain_names": ["private.example.invalid"]}
    ref = {"id": 123, "identity": {"domain_names": ["private.example.invalid"]}}
    with pytest.raises(npmplus_ops.OpsError, match="id does not match"):
        npmplus_ops.verify_host_identity(host, ref)


def test_verify_host_identity_rejects_domain_mismatch() -> None:
    host = {"id": 123, "domain_names": ["wrong.example.invalid"]}
    ref = {"id": 123, "identity": {"domain_names": ["private.example.invalid"]}}
    with pytest.raises(npmplus_ops.OpsError, match="domain names do not match"):
        npmplus_ops.verify_host_identity(host, ref)


def test_authorize_lifecycle_write_is_per_ref_and_requires_evidence(
    tmp_path: Path,
) -> None:
    private_repo = make_private_repo(tmp_path)
    context = npmplus_ops.load_context(
        private_repo, write_provider(private_repo, context_payload()), "default"
    )
    ref = context.refs["canary"]

    with pytest.raises(npmplus_ops.OpsError, match="does not allow"):
        npmplus_ops.authorize_lifecycle_write(
            context, ref, "proxy-host-unsupported"
        )

    ref["write_evidence"]["rollback_ready"] = False
    with pytest.raises(npmplus_ops.OpsError, match="write evidence is incomplete"):
        npmplus_ops.authorize_lifecycle_write(
            context, ref, "proxy-host-enable"
        )


def test_cmd_lifecycle_rejects_unauthorized_ref_before_client_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private_repo = make_private_repo(tmp_path)
    payload = context_payload()
    refs = payload["refs"]
    assert isinstance(refs, dict)
    ref = refs["canary"]
    assert isinstance(ref, dict)
    ref["allowed_apply_actions"] = ["proxy-host-disable"]
    context = npmplus_ops.load_context(
        private_repo, write_provider(private_repo, payload), "default"
    )
    monkeypatch.setattr(npmplus_ops, "build_context", lambda args: context)

    def fail_build_client(*args: object) -> None:
        raise AssertionError("client must not be built before authorization")

    monkeypatch.setattr(npmplus_ops, "build_client", fail_build_client)
    args = argparse.Namespace(
        apply=True,
        host_ref="canary",
        lifecycle_action="enable",
    )

    with pytest.raises(npmplus_ops.OpsError, match="does not allow"):
        npmplus_ops.cmd_lifecycle(args)


def test_lifecycle_rechecks_target_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = npmplus_ops.NpmplusClient(
        npmplus_ops.ApiConfig(
            base_url="https://npmplus.invalid",
            identity="robot@example.invalid",
            secret="secret-value",
            timeout=1,
            expected_principal=None,
        )
    )
    calls: list[tuple[str, str]] = []

    def fake_request(
        method: str, path: str, *, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        calls.append((method, path))
        return {
            "id": 999,
            "domain_names": ["private.example.invalid"],
            "enabled": False,
        }

    monkeypatch.setattr(client, "request", fake_request)
    ref = {"id": 123, "identity": {"domain_names": ["private.example.invalid"]}}

    with pytest.raises(npmplus_ops.OpsError, match="id does not match"):
        client.lifecycle("enable", ref)
    assert calls == [("GET", "/api/nginx/proxy-hosts/123")]


def test_lifecycle_verifies_same_target_after_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = npmplus_ops.NpmplusClient(
        npmplus_ops.ApiConfig(
            base_url="https://npmplus.invalid",
            identity="robot@example.invalid",
            secret="secret-value",
            timeout=1,
            expected_principal=None,
        )
    )
    responses: list[dict[str, object]] = [
        {
            "id": 123,
            "domain_names": ["private.example.invalid"],
            "enabled": False,
        },
        {},
        {
            "id": 999,
            "domain_names": ["private.example.invalid"],
            "enabled": True,
        },
    ]

    def fake_request(
        method: str, path: str, *, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        return responses.pop(0)

    monkeypatch.setattr(client, "request", fake_request)
    ref = {"id": 123, "identity": {"domain_names": ["private.example.invalid"]}}

    with pytest.raises(npmplus_ops.OpsError, match="id does not match"):
        client.lifecycle("enable", ref)


def test_lifecycle_cli_has_no_raw_id_bypass() -> None:
    parser = npmplus_ops.build_parser()
    enable_parser = next(
        action.choices["proxy-host-enable"]
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    assert all("--host-id" not in action.option_strings for action in enable_parser._actions)
