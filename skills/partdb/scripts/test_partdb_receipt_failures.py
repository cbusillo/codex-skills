#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
# ///

from __future__ import annotations

import json
from pathlib import Path

import pytest

import test_partdb_write as fixtures


@pytest.fixture(autouse=True)
def isolated_user_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user-home")
    monkeypatch.setenv("CODE_HOME", str(tmp_path / "code-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))


def approved_files(tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    artifact = fixtures.artifact_plan()
    plan_path = tmp_path / "plan.json"
    approval_path = tmp_path / "approval.json"
    fixtures.write_json(plan_path, artifact)
    fixtures.write_json(approval_path, fixtures.approval(artifact))
    return plan_path, approval_path, artifact


def assert_consumed_failure(
    monkeypatch: pytest.MonkeyPatch, plan_path: Path, approval_path: Path, artifact: dict[str, object],
) -> None:
    module = fixtures.partdb_write
    stored_receipt = module.receipt_path(plan_path, artifact["digest"])
    assert json.loads(stored_receipt.read_text())["outcome"] == "needs-reconciliation"
    monkeypatch.setitem(vars(module.partdb_read), "context", lambda: pytest.fail("consumed approval must not resolve context"))
    with pytest.raises(module.WriteError, match="already used"):
        module.apply(fixtures.apply_args(plan_path, approval_path))


@pytest.mark.parametrize("provider_failure", ["missing", "invalid-json"])
def test_actual_context_provider_failure_finalizes_receipt_without_write_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider_failure: str,
) -> None:
    module = fixtures.partdb_write
    plan_path, approval_path, artifact = approved_files(tmp_path)
    private_repo = tmp_path / "private"
    provider = private_repo / "scripts" / "infra-context.py"
    provider.parent.mkdir(parents=True)
    if provider_failure == "invalid-json":
        provider.write_text('print("not-json")\n')
    code_home = tmp_path / "code-home"
    code_home.mkdir()
    (code_home / "local-context.toml").write_text(f"[docs]\nlocal_infra = {str(private_repo)!r}\n")
    patches: list[tuple[object, ...]] = []
    monkeypatch.setitem(vars(module), "write_environment", lambda *_args: pytest.fail("write authority must not be obtained"))
    monkeypatch.setitem(vars(module), "patch_lot", lambda *args: patches.append(args))

    with pytest.raises(module.partdb_read.PartdbError):
        module.apply(fixtures.apply_args(plan_path, approval_path))
    assert patches == []
    assert_consumed_failure(monkeypatch, plan_path, approval_path, artifact)


@pytest.mark.parametrize("failed_stage", ["environment", "schema", "read-before", "write-authority", "patch", "read-after"])
def test_later_apply_failures_preserve_terminal_receipts_and_refuse_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failed_stage: str,
) -> None:
    module = fixtures.partdb_write
    plan_path, approval_path, artifact = approved_files(tmp_path)
    operation = module.validate_plan(artifact)["operation"]
    reads = 0
    patches: list[tuple[object, ...]] = []

    def fail(*_args: object) -> None:
        raise module.WriteError("fixture failure")

    def read(*_args: object) -> dict[str, int | float]:
        nonlocal reads
        reads += 1
        if (failed_stage == "read-before" and reads == 1) or (failed_stage == "read-after" and reads == 2):
            raise module.partdb_read.PartdbError("fixture read failure")
        return {"amount": operation["prior_amount"] if reads == 1 else operation["target_amount"]}

    def patch(*args: object) -> None:
        patches.append(args)
        if failed_stage == "patch":
            fail()

    monkeypatch.setitem(vars(module.partdb_read), "context", lambda: (tmp_path, {}))
    monkeypatch.setitem(vars(module.partdb_read), "environment", fail if failed_stage == "environment" else lambda *_args: ("https://private.invalid", "read-fixture"))
    monkeypatch.setitem(vars(module), "verify_lot_patch_schema", fail if failed_stage == "schema" else lambda *_args: None)
    monkeypatch.setitem(vars(module), "read_lot", read)
    monkeypatch.setitem(vars(module), "write_environment", fail if failed_stage == "write-authority" else lambda *_args: ("https://private.invalid", "write-fixture"))
    monkeypatch.setitem(vars(module), "patch_lot", patch)

    with pytest.raises((module.WriteError, module.partdb_read.PartdbError)):
        module.apply(fixtures.apply_args(plan_path, approval_path))
    assert len(patches) == (1 if failed_stage in {"patch", "read-after"} else 0)
    assert_consumed_failure(monkeypatch, plan_path, approval_path, artifact)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
