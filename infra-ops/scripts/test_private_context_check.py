#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
# ///

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("private-context-check.py")
MODULE_SPEC = importlib.util.spec_from_file_location("private_context_check", MODULE_PATH)
assert MODULE_SPEC is not None
private_context_check = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
sys.modules[MODULE_SPEC.name] = private_context_check
MODULE_SPEC.loader.exec_module(private_context_check)


def write_context(path: Path, pointer: str = "private/ops") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[docs]\nlocal_infra = "{pointer}"\n', encoding="utf-8")


def test_code_home_precedes_other_candidates(tmp_path: Path) -> None:
    code_home = tmp_path / "code-home"
    codex_home = tmp_path / "codex-home"
    default_home = tmp_path / "user-home"
    code_context = code_home / "local-context.toml"
    write_context(code_context)
    write_context(codex_home / "local-context.toml")
    write_context(default_home / ".code" / "local-context.toml")

    resolved = private_context_check.resolve_local_context(
        None,
        {"CODE_HOME": str(code_home), "CODEX_HOME": str(codex_home)},
        default_home,
    )

    assert resolved == code_context


def test_missing_code_home_file_falls_back_to_codex_home(tmp_path: Path) -> None:
    code_home = tmp_path / "code-home"
    codex_home = tmp_path / "codex-home"
    codex_context = codex_home / "local-context.toml"
    write_context(codex_context)

    resolved = private_context_check.resolve_local_context(
        None,
        {"CODE_HOME": str(code_home), "CODEX_HOME": str(codex_home)},
        tmp_path / "user-home",
    )

    assert resolved == codex_context


def test_unconfigured_runtime_files_fall_back_to_default_home(tmp_path: Path) -> None:
    code_home = tmp_path / "code-home"
    codex_home = tmp_path / "codex-home"
    default_home = tmp_path / "user-home"
    code_home.mkdir()
    (code_home / "local-context.toml").write_text('[docs]\nother = "value"\n', encoding="utf-8")
    codex_home.mkdir()
    (codex_home / "local-context.toml").write_text("not valid toml =", encoding="utf-8")
    default_context = default_home / ".code" / "local-context.toml"
    write_context(default_context)

    resolved = private_context_check.resolve_local_context(
        None,
        {"CODE_HOME": str(code_home), "CODEX_HOME": str(codex_home)},
        default_home,
    )

    assert resolved == default_context


def test_complete_absence_reports_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODE_HOME", str(tmp_path / "code-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(
        private_context_check.Path,
        "home",
        lambda: tmp_path / "user-home",
    )

    assert private_context_check.main([]) == 1
    assert capsys.readouterr().out == "missing\n"


def test_main_uses_codex_home_when_code_home_file_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    write_context(codex_home / "local-context.toml")
    monkeypatch.setenv("CODE_HOME", str(tmp_path / "code-home"))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        private_context_check.Path,
        "home",
        lambda: tmp_path / "user-home",
    )

    assert private_context_check.main([]) == 0
    assert capsys.readouterr().out == "configured\n"


def test_explicit_path_does_not_fall_back(tmp_path: Path) -> None:
    code_home = tmp_path / "code-home"
    write_context(code_home / "local-context.toml")

    resolved = private_context_check.resolve_local_context(
        tmp_path / "missing.toml",
        {"CODE_HOME": str(code_home)},
        tmp_path / "user-home",
    )

    assert resolved is None


def test_invalid_or_unreadable_candidates_fail_closed(tmp_path: Path) -> None:
    directory_candidate = tmp_path / "code-home" / "local-context.toml"
    directory_candidate.mkdir(parents=True)
    malformed_candidate = tmp_path / "codex-home" / "local-context.toml"
    malformed_candidate.parent.mkdir(parents=True)
    malformed_candidate.write_text("not valid toml =", encoding="utf-8")

    resolved = private_context_check.resolve_local_context(
        None,
        {
            "CODE_HOME": str(directory_candidate.parent),
            "CODEX_HOME": str(malformed_candidate.parent),
        },
        tmp_path / "user-home",
    )

    assert resolved is None


def test_candidate_list_skips_blank_values_and_duplicates(tmp_path: Path) -> None:
    runtime_home = tmp_path / "runtime-home"
    default_home = tmp_path / "user-home"

    candidates = private_context_check.local_context_candidates(
        {"CODE_HOME": "  ", "CODEX_HOME": str(runtime_home)},
        default_home,
    )
    duplicate_candidates = private_context_check.local_context_candidates(
        {"CODE_HOME": str(runtime_home), "CODEX_HOME": str(runtime_home)},
        default_home,
    )

    assert candidates == [
        runtime_home / "local-context.toml",
        default_home / ".code" / "local-context.toml",
    ]
    assert duplicate_candidates == candidates


def test_configured_output_does_not_disclose_path_or_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context_path = tmp_path / "private-runtime" / "local-context.toml"
    private_value = "private/secret-operations-repo"
    write_context(context_path, private_value)

    assert private_context_check.main(["--local-context", str(context_path)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "configured\n"
    assert captured.err == ""
    assert str(context_path) not in captured.out
    assert private_value not in captured.out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
