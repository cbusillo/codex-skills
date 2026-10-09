#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
# ///

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import threading
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("partdb-write.py")
MODULE_SPEC = importlib.util.spec_from_file_location("partdb_write", MODULE_PATH)
assert MODULE_SPEC is not None
partdb_write = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
sys.modules[MODULE_SPEC.name] = partdb_write
MODULE_SPEC.loader.exec_module(partdb_write)


@pytest.fixture(autouse=True)
def isolated_user_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user-home")


def artifact_plan(lot_id: int = 7, prior_amount: int = 1, target_amount: int = 2) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": partdb_write.PLAN_KIND,
        "nonce": "0" * 32,
        "operation": {"lot_id": lot_id, "prior_amount": prior_amount, "target_amount": target_amount},
    }
    value["digest"] = partdb_write.digest(value)
    return value


def test_plan_inherits_context_fallback_from_read_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    private_repo = tmp_path / "private"
    provider = private_repo / "scripts" / "infra-context.py"
    provider.parent.mkdir(parents=True)
    provider.write_text(
        "import json\n"
        "print(json.dumps({'schema_version': 'partdb.context.v1', "
        "'api': {'env_file': '.env', 'base_url_env': 'PARTDB_BASE_URL', "
        "'read_token_env': 'PARTDB_READ_TOKEN'}, 'policy': {'allow_mutations': True}}))\n",
        encoding="utf-8",
    )
    (private_repo / ".env").write_text(
        "PARTDB_BASE_URL=https://private.invalid\nPARTDB_READ_TOKEN=read-token\n",
        encoding="utf-8",
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "local-context.toml").write_text(
        f"[docs]\nlocal_infra = {str(private_repo)!r}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODE_HOME", str(tmp_path / "missing-code-home"))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user-home")
    monkeypatch.setitem(vars(partdb_write), "verify_lot_patch_schema", lambda *_args: None)
    monkeypatch.setitem(vars(partdb_write), "read_lot", lambda *_args: {"amount": 1})
    intent = tmp_path / "intent.json"
    output = tmp_path / "plan.json"
    intent.write_text(
        json.dumps({"op": "part-lot-amount-set", "lot_id": 7, "amount": 2}),
        encoding="utf-8",
    )

    partdb_write.plan(argparse.Namespace(intent=str(intent), output=str(output)))

    assert json.loads(output.read_text(encoding="utf-8"))["operation"] == {
        "lot_id": 7,
        "prior_amount": 1,
        "target_amount": 2,
    }


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value))


def approval(plan: dict[str, object]) -> dict[str, object]:
    return {
        "kind": partdb_write.APPROVAL_KIND,
        "plan_digest": plan["digest"],
        "receipt_authority": partdb_write.receipt_authority(partdb_write.receipt_root(), create=True),
    }


def approved_plan_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, dict[str, object]]:
    plan_path = tmp_path / "plan.json"
    approval_path = tmp_path / "approval.json"
    plan = artifact_plan()
    write_json(plan_path, plan)
    write_json(approval_path, approval(plan))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: (tmp_path, {}))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "environment", lambda *_args: ("https://private.invalid", "read-token"))
    monkeypatch.setitem(vars(partdb_write), "verify_lot_patch_schema", lambda *_args: None)
    return plan_path, approval_path, plan


def apply_args(plan_path: Path, approval_path: Path, *, apply: bool = True) -> argparse.Namespace:
    return argparse.Namespace(plan=str(plan_path), approval=str(approval_path), apply=apply)


@pytest.mark.parametrize("authority_failure", ["legacy", "missing", "different"])
def test_apply_refuses_unbound_or_unavailable_receipt_authority_before_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, authority_failure: str,
) -> None:
    plan_path = tmp_path / "plan.json"
    approval_path = tmp_path / "approval.json"
    artifact = artifact_plan()
    authorization = approval(artifact)
    write_json(plan_path, artifact)
    if authority_failure == "legacy":
        del authorization["receipt_authority"]
    elif authority_failure == "missing":
        (partdb_write.receipt_root() / partdb_write.AUTHORITY_FILE_NAME).unlink()
    else:
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "other-user-home")
        partdb_write.receipt_authority(partdb_write.receipt_root(), create=True)
    write_json(approval_path, authorization)
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: pytest.fail("context must not be accessed"))

    with pytest.raises(partdb_write.WriteError, match="receipt authority"):
        partdb_write.apply(apply_args(plan_path, approval_path))
    assert not partdb_write.receipt_path(artifact["digest"]).exists()


@pytest.mark.parametrize("legacy_kind", ["partdb-write-plan.v1", "partdb-write-plan.v2"])
def test_consumed_legacy_plan_cannot_receive_new_approval(tmp_path: Path, legacy_kind: str) -> None:
    artifact = artifact_plan()
    artifact["kind"] = legacy_kind
    if legacy_kind.endswith(".v2"):
        artifact["instance_id"] = "0" * 64
    artifact["digest"] = partdb_write.digest({key: value for key, value in artifact.items() if key != "digest"})
    plan_path = tmp_path / "legacy-plan.json"
    approval_path = tmp_path / "approval.json"
    write_json(plan_path, artifact)
    legacy_receipt = tmp_path / ".partdb-write-receipts" / f"{artifact['digest']}.json"
    legacy_receipt.parent.mkdir()
    write_json(legacy_receipt, partdb_write.receipt(artifact["digest"], "verified"))

    with pytest.raises(partdb_write.WriteError, match="create and approve a new plan"):
        partdb_write.approve(argparse.Namespace(plan=str(plan_path), approve=artifact["digest"], output=str(approval_path)))
    assert not approval_path.exists()
    assert not partdb_write.receipt_root().exists()


def test_concurrent_authority_initialization_publishes_complete_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()
    results: list[str] = []
    errors: list[Exception] = []
    original_nonce = partdb_write.secrets.token_hex

    def delayed_nonce(size: int) -> str:
        if threading.current_thread() is first:
            started.set()
            if not release.wait(5):
                raise RuntimeError("fixture synchronization timed out")
        return original_nonce(size)

    def initialize() -> None:
        try:
            results.append(partdb_write.receipt_authority(tmp_path / "ledger", create=True))
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=initialize)
    monkeypatch.setitem(vars(partdb_write.secrets), "token_hex", delayed_nonce)
    first.start()
    try:
        assert started.wait(5)
        second = partdb_write.receipt_authority(tmp_path / "ledger", create=True)
    finally:
        release.set()
        first.join(5)
    assert not first.is_alive()
    assert errors == []
    assert results == [second]


@pytest.mark.parametrize("relocation", ["copy", "move"])
@pytest.mark.parametrize("uncertain_write", [False, True])
def test_relocated_approval_cannot_replay_after_stock_returns_to_prior_amount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, relocation: str, uncertain_write: bool,
) -> None:
    amount = 1
    fail_write = uncertain_write
    patches: list[tuple[object, ...]] = []
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: (tmp_path, {}))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "environment", lambda *_args: ("https://private.invalid", "read-fixture"))
    monkeypatch.setitem(vars(partdb_write), "verify_lot_patch_schema", lambda *_args: None)
    monkeypatch.setitem(vars(partdb_write), "read_lot", lambda *_args: {"amount": amount})
    monkeypatch.setitem(vars(partdb_write), "write_environment", lambda *_args: ("https://private.invalid", "write-fixture"))

    def patch(*args: object) -> None:
        nonlocal amount
        patches.append(args)
        amount = 4
        if fail_write:
            raise partdb_write.WriteError("uncertain fixture write")

    monkeypatch.setitem(vars(partdb_write), "patch_lot", patch)

    def new_approved_plan(directory: Path) -> tuple[Path, Path, dict[str, object]]:
        directory.mkdir()
        intent = directory / "intent.json"
        plan_path = directory / "plan.json"
        approval_path = directory / "approval.json"
        write_json(intent, {"op": "part-lot-amount-set", "lot_id": 7, "amount": 4})
        partdb_write.plan(argparse.Namespace(intent=str(intent), output=str(plan_path)))
        artifact = json.loads(plan_path.read_text())
        partdb_write.approve(argparse.Namespace(plan=str(plan_path), approve=artifact["digest"], output=str(approval_path)))
        return plan_path, approval_path, artifact

    plan_path, approval_path, artifact = new_approved_plan(tmp_path / "original")
    if uncertain_write:
        with pytest.raises(partdb_write.WriteError, match="uncertain fixture write"):
            partdb_write.apply(apply_args(plan_path, approval_path))
    else:
        partdb_write.apply(apply_args(plan_path, approval_path))
    amount = 1
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    for source in (plan_path, approval_path):
        if relocation == "copy":
            shutil.copyfile(source, relocated / source.name)
        else:
            source.rename(relocated / source.name)
    with pytest.raises(partdb_write.WriteError, match="already used"):
        partdb_write.apply(apply_args(relocated / "plan.json", relocated / "approval.json"))
    assert len(patches) == 1
    assert json.loads(partdb_write.receipt_path(artifact["digest"]).read_text())["outcome"] == (
        "needs-reconciliation" if uncertain_write else "verified"
    )
    fail_write = False
    fresh_plan, fresh_approval, _artifact = new_approved_plan(tmp_path / "fresh")
    partdb_write.apply(apply_args(fresh_plan, fresh_approval))
    assert len(patches) == 2


def test_plan_writes_reviewable_exact_diff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    intent = tmp_path / "intent.json"
    output = tmp_path / "plan.json"
    write_json(intent, {"op": "part-lot-amount-set", "lot_id": 7, "amount": 2})
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: (tmp_path, {"policy": {"allow_mutations": True}}))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "environment", lambda *_args: ("https://private.invalid", "read-token"))
    monkeypatch.setitem(vars(partdb_write), "verify_lot_patch_schema", lambda *_args: None)
    monkeypatch.setitem(vars(partdb_write), "read_lot", lambda *_args: {"amount": 1})
    monkeypatch.setitem(vars(partdb_write.secrets), "token_hex", lambda _size: "0" * 32)

    partdb_write.plan(argparse.Namespace(intent=str(intent), output=str(output)))

    artifact = json.loads(output.read_text())
    assert artifact == artifact_plan()
    assert json.loads(capsys.readouterr().out)["operation"] == artifact["operation"]


def test_apply_requires_flag_before_any_context_access(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path, approval_path, _plan = approved_plan_files(monkeypatch, tmp_path)
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: pytest.fail("context must not be accessed"))

    with pytest.raises(partdb_write.WriteError, match="without --apply"):
        partdb_write.apply(apply_args(plan_path, approval_path, apply=False))


def test_apply_refuses_drift_before_write_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path, approval_path, plan = approved_plan_files(monkeypatch, tmp_path)
    monkeypatch.setitem(vars(partdb_write), "read_lot", lambda *_args: {"amount": 3})
    monkeypatch.setitem(vars(partdb_write), "write_environment", lambda *_args: pytest.fail("write token must not be read"))

    with pytest.raises(partdb_write.WriteError, match="changed after planning"):
        partdb_write.apply(apply_args(plan_path, approval_path))
    receipt = json.loads(partdb_write.receipt_path(plan["digest"]).read_text())
    assert receipt["outcome"] == "needs-reconciliation"


def test_apply_already_target_is_idempotent_without_write_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path, approval_path, plan = approved_plan_files(monkeypatch, tmp_path)
    monkeypatch.setitem(vars(partdb_write), "read_lot", lambda *_args: {"amount": 2})
    monkeypatch.setitem(vars(partdb_write), "write_environment", lambda *_args: pytest.fail("write token must not be read"))

    partdb_write.apply(apply_args(plan_path, approval_path))

    assert json.loads(partdb_write.receipt_path(plan["digest"]).read_text())["outcome"] == "already-target"


def test_apply_writes_then_read_back_verifies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path, approval_path, plan = approved_plan_files(monkeypatch, tmp_path)
    reads = iter(({"amount": 1}, {"amount": 2}))
    patched: list[tuple[object, ...]] = []
    monkeypatch.setitem(vars(partdb_write), "read_lot", lambda *_args: next(reads))
    monkeypatch.setitem(vars(partdb_write), "write_environment", lambda *_args: ("https://private.invalid", "write-token"))
    monkeypatch.setitem(vars(partdb_write), "patch_lot", lambda *args: patched.append(args))

    partdb_write.apply(apply_args(plan_path, approval_path))

    assert patched == [("https://private.invalid", "write-token", 7, 2)]
    assert json.loads(partdb_write.receipt_path(plan["digest"]).read_text())["outcome"] == "verified"


def test_apply_refuses_reused_approval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path, approval_path, plan = approved_plan_files(monkeypatch, tmp_path)
    partdb_write.receipt_path(plan["digest"]).parent.mkdir(parents=True, exist_ok=True)
    partdb_write.receipt_path(plan["digest"]).write_text(json.dumps(partdb_write.receipt(plan["digest"], "verified")))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: pytest.fail("context must not be accessed"))

    with pytest.raises(partdb_write.WriteError, match="already used"):
        partdb_write.apply(apply_args(plan_path, approval_path))


def test_apply_refuses_same_read_and_write_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path, approval_path, _plan = approved_plan_files(monkeypatch, tmp_path)
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: (tmp_path, {}))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "environment", lambda *_args: ("https://private.invalid", "same-token"))
    monkeypatch.setitem(vars(partdb_write), "verify_lot_patch_schema", lambda *_args: None)
    monkeypatch.setitem(vars(partdb_write), "read_lot", lambda *_args: {"amount": 1})
    monkeypatch.setitem(vars(partdb_write), "write_environment", lambda *_args: ("https://private.invalid", "same-token"))

    with pytest.raises(partdb_write.WriteError, match="credentials must be separate"):
        partdb_write.apply(apply_args(plan_path, approval_path))


def test_write_environment_requires_enabled_policy_and_declared_distinct_tokens(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(partdb_write.WriteError, match="does not permit"):
        partdb_write.write_environment(tmp_path, {"policy": {"allow_mutations": False}})

    env_file = tmp_path / ".env"
    env_file.write_text("PARTDB_BASE_URL=https://private.invalid\nPARTDB_WRITE_TOKEN=write-token\n")
    monkeypatch.delenv("PARTDB_BASE_URL", raising=False)
    monkeypatch.delenv("PARTDB_WRITE_TOKEN", raising=False)

    config = {
        "api": {"base_url_env": "PARTDB_BASE_URL", "env_file": ".env", "read_token_env": "PARTDB_READ_TOKEN", "write_token_env": "PARTDB_WRITE_TOKEN"},
        "policy": {"allow_mutations": True},
    }
    assert partdb_write.write_environment(tmp_path, config) == ("https://private.invalid", "write-token")
    config["api"]["write_token_env"] = "PARTDB_READ_TOKEN"
    with pytest.raises(partdb_write.WriteError, match="credentials must be separate"):
        partdb_write.write_environment(tmp_path, config)


def test_schema_probe_requires_amount_merge_patch_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(vars(partdb_write.partdb_read), "request", lambda *_args, **_kwargs: {"paths": {partdb_write.LOT_PATH: {"patch": {"requestBody": {"content": {}}}}}})

    with pytest.raises(partdb_write.WriteError, match="does not support"):
        partdb_write.verify_lot_patch_schema("https://private.invalid", "read-token")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
