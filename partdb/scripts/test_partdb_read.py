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


def write_local_context(runtime_home: Path, private_repo: Path) -> Path:
    context_path = runtime_home / "local-context.toml"
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(
        f"[docs]\nlocal_infra = {str(private_repo)!r}\n",
        encoding="utf-8",
    )
    return context_path


def write_provider(private_repo: Path) -> Path:
    provider = private_repo / "scripts" / "infra-context.py"
    provider.parent.mkdir(parents=True, exist_ok=True)
    provider.write_text(
        "import json\n"
        "print(json.dumps({'schema_version': 'partdb.context.v1', "
        "'api': {'env_file': '.env', 'base_url_env': 'PARTDB_BASE_URL', "
        "'read_token_env': 'PARTDB_READ_TOKEN'}, 'policy': {'allow_mutations': False}}))\n",
        encoding="utf-8",
    )
    return provider


def test_context_check_uses_private_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    private_repo = tmp_path / "private"
    write_provider(private_repo)
    code_home = tmp_path / "code-home"
    write_local_context(code_home, private_repo)
    monkeypatch.setenv("CODE_HOME", str(code_home))

    private, context = partdb_read.context()

    assert private == private_repo
    assert context["schema_version"] == "partdb.context.v1"


def test_context_falls_back_to_codex_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    private_repo = tmp_path / "private"
    write_provider(private_repo)
    codex_home = tmp_path / "codex-home"
    write_local_context(codex_home, private_repo)
    monkeypatch.setenv("CODE_HOME", str(tmp_path / "missing-code-home"))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(partdb_read.Path, "home", lambda: tmp_path / "user-home")

    private, context = partdb_read.context()

    assert private == private_repo
    assert context["schema_version"] == "partdb.context.v1"


def test_invalid_candidates_fall_back_to_default_home(tmp_path: Path) -> None:
    code_home = tmp_path / "code-home"
    code_home.mkdir()
    (code_home / "local-context.toml").write_bytes(b"\xff\xfe\x00")
    codex_context = tmp_path / "codex-home" / "local-context.toml"
    codex_context.mkdir(parents=True)
    private_repo = tmp_path / "private"
    private_repo.mkdir()
    default_home = tmp_path / "user-home"
    write_local_context(default_home / ".code", private_repo)

    resolved = partdb_read.resolve_local_infra_repo(
        {"CODE_HOME": str(code_home), "CODEX_HOME": str(codex_context.parent)},
        default_home,
    )

    assert resolved == private_repo


def test_whitespace_pointer_falls_back_to_codex_home(tmp_path: Path) -> None:
    code_home = tmp_path / "code-home"
    code_home.mkdir()
    (code_home / "local-context.toml").write_text(
        '[docs]\nlocal_infra = "   "\n',
        encoding="utf-8",
    )
    private_repo = tmp_path / "private"
    private_repo.mkdir()
    codex_home = tmp_path / "codex-home"
    write_local_context(codex_home, private_repo)

    resolved = partdb_read.resolve_local_infra_repo(
        {"CODE_HOME": str(code_home), "CODEX_HOME": str(codex_home)},
        tmp_path / "user-home",
    )

    assert resolved == private_repo


def test_configured_missing_repo_reports_generic_error(tmp_path: Path) -> None:
    missing_repo = tmp_path / "missing-private-repo"
    code_home = tmp_path / "code-home"
    write_local_context(code_home, missing_repo)

    with pytest.raises(
        partdb_read.PartdbError,
        match="^configured private Part-DB repo path is not available$",
    ) as raised:
        partdb_read.resolve_local_infra_repo(
            {"CODE_HOME": str(code_home)},
            tmp_path / "user-home",
        )

    assert str(missing_repo) not in str(raised.value)


def test_missing_context_reports_generic_error(tmp_path: Path) -> None:
    private_path = tmp_path / "private-runtime"

    with pytest.raises(partdb_read.PartdbError, match="^private Part-DB context is not configured$") as raised:
        partdb_read.resolve_local_infra_repo(
            {"CODE_HOME": str(private_path)},
            tmp_path / "user-home",
        )

    assert str(private_path) not in str(raised.value)


def test_context_check_missing_context_is_redacted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = tmp_path / "private-runtime"
    monkeypatch.setenv("CODE_HOME", str(private_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(partdb_read.Path, "home", lambda: tmp_path / "user-home")

    with pytest.raises(SystemExit) as raised:
        partdb_read.main(["context-check"])

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert captured.out == ""
    assert captured.err == "error: private Part-DB context is not configured\n"
    assert str(private_path) not in captured.err


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
