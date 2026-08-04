#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pytest==9.1.1"]
# ///

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("partdb-read.py")
MODULE_SPEC = importlib.util.spec_from_file_location("partdb_read", MODULE_PATH)
assert MODULE_SPEC is not None
partdb_read = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
sys.modules[MODULE_SPEC.name] = partdb_read
MODULE_SPEC.loader.exec_module(partdb_read)


def test_context_check_uses_private_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    private_repo = tmp_path / "private"
    provider = private_repo / "scripts" / "infra-context.py"
    provider.parent.mkdir(parents=True)
    provider.write_text(
        "import json\n"
        "print(json.dumps({'schema_version': 'partdb.context.v1', "
        "'api': {'env_file': '.env', 'base_url_env': 'PARTDB_BASE_URL', "
        "'read_token_env': 'PARTDB_READ_TOKEN'}, 'policy': {'allow_mutations': False}}))\n"
    )
    code_home = tmp_path / "code-home"
    code_home.mkdir()
    (code_home / "local-context.toml").write_text(f"[docs]\nlocal_infra = {str(private_repo)!r}\n")
    monkeypatch.setenv("CODE_HOME", str(code_home))

    private, context = partdb_read.context()

    assert private == private_repo
    assert context["schema_version"] == "partdb.context.v1"


def test_http_errors_redact_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    error = urllib.error.HTTPError("https://private.invalid/api/parts", 401, "Unauthorized", {}, None)
    monkeypatch.setattr(partdb_read.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(partdb_read.PartdbError) as raised:
        partdb_read.request("https://private.invalid", "secret-token", "/api/parts")

    assert "private.invalid" not in str(raised.value)
    assert "secret-token" not in str(raised.value)
    assert "HTTP 401" in str(raised.value)


def test_read_environment_accepts_explicit_mutation_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("PARTDB_BASE_URL=https://private.invalid\nPARTDB_READ_TOKEN=read-token\n")
    monkeypatch.delenv("PARTDB_BASE_URL", raising=False)
    monkeypatch.delenv("PARTDB_READ_TOKEN", raising=False)

    assert partdb_read.environment(
        tmp_path,
        {
            "api": {"base_url_env": "PARTDB_BASE_URL", "env_file": ".env", "read_token_env": "PARTDB_READ_TOKEN"},
            "policy": {"allow_mutations": True},
        },
    ) == ("https://private.invalid", "read-token")
