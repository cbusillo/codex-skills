#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest",
# ]
# ///

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
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
        "schema_version": "npmplus.ops.v1",
        "api": {
            "env_file": ".code/local.env",
            "base_url_env": "NPMPLUS_BASE_URL",
            "identity_env": "NPMPLUS_AUTOMATION_EMAIL",
            "secret_env": "NPMPLUS_AUTOMATION_PASSWORD",
        },
        "refs": {
            "canary": {"kind": "proxy_host", "id": 123},
        },
        "pilot": {"default_ref": "canary"},
        "policy": {"allowed_apply_actions": ["proxy-host-enable", "proxy-host-disable"]},
    }
    payload.update(overrides)
    return payload


def test_load_context_accepts_public_safe_schema(tmp_path: Path) -> None:
    private_repo = make_private_repo(tmp_path)
    provider = write_provider(private_repo, context_payload())

    context = npmplus_ops.load_context(private_repo, provider, "default")

    assert context.profile == "default"
    assert context.env_file == private_repo / ".code" / "local.env"
    assert context.default_pilot_ref == "canary"
    assert context.refs == {"canary": {"kind": "proxy_host", "id": 123}}
    assert context.allowed_apply_actions == {"proxy-host-enable", "proxy-host-disable"}


def test_context_rejects_absolute_env_file_path(tmp_path: Path) -> None:
    private_repo = make_private_repo(tmp_path)
    provider = write_provider(
        private_repo,
        context_payload(
            api={
                "env_file": "/private/path",
                "base_url_env": "A",
                "identity_env": "B",
                "secret_env": "C",
            }
        ),
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
