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
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).with_name("partdb-write.py")
MODULE_SPEC = importlib.util.spec_from_file_location("partdb_write", MODULE_PATH)
assert MODULE_SPEC is not None
partdb_write = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC.loader is not None
sys.modules[MODULE_SPEC.name] = partdb_write
MODULE_SPEC.loader.exec_module(partdb_write)


def artifact_plan(lot_id: int = 7, prior_amount: int = 1, target_amount: int = 2) -> dict[str, object]:
    value: dict[str, object] = {
        "kind": partdb_write.PLAN_KIND,
        "nonce": "0" * 32,
        "instance_id": partdb_write.instance_identity("https://private.invalid"),
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
    return {"kind": partdb_write.APPROVAL_KIND, "plan_digest": plan["digest"]}


def apply_args(plan_path: Path, approval_path: Path, *, apply: bool = True) -> argparse.Namespace:
    return argparse.Namespace(plan=str(plan_path), approval=str(approval_path), apply=apply)


@pytest.mark.parametrize("changed_instance", [False, True])
def test_planned_instance_is_reverified_before_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, changed_instance: bool,
) -> None:
    intent = tmp_path / "intent.json"
    plan_path = tmp_path / "plan.json"
    approval_path = tmp_path / "approval.json"
    write_json(intent, {"op": "part-lot-amount-set", "lot_id": 7, "amount": 4})
    base_url = "https://instance-a.invalid"
    amount = 1
    patched = []
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: (tmp_path, {}))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "environment", lambda *_args: (base_url, "read-fixture"))
    monkeypatch.setitem(vars(partdb_write), "verify_lot_patch_schema", lambda *_args: None)
    monkeypatch.setitem(vars(partdb_write), "read_lot", lambda *_args: {"amount": amount})

    def patch(*args: object) -> None:
        nonlocal amount
        patched.append(args)
        amount = 4

    monkeypatch.setitem(vars(partdb_write), "patch_lot", patch)
    partdb_write.plan(argparse.Namespace(intent=str(intent), output=str(plan_path)))
    artifact = json.loads(plan_path.read_text())
    partdb_write.approve(argparse.Namespace(plan=str(plan_path), approve=artifact["digest"], output=str(approval_path)))
    if changed_instance:
        base_url = "https://instance-b.invalid"
        monkeypatch.setitem(vars(partdb_write), "verify_lot_patch_schema", lambda *_args: pytest.fail("changed instance must not be queried"))
        monkeypatch.setitem(vars(partdb_write), "write_environment", lambda *_args: pytest.fail("write authority must not be obtained"))
        with pytest.raises(partdb_write.WriteError, match="instance changed after planning"):
            partdb_write.apply(apply_args(plan_path, approval_path))
        assert patched == []
    else:
        monkeypatch.setitem(vars(partdb_write), "write_environment", lambda *_args: (base_url, "write-fixture"))
        partdb_write.apply(apply_args(plan_path, approval_path))
        assert patched == [(base_url, "write-fixture", 7, 4)]
        assert json.loads(partdb_write.receipt_path(plan_path, artifact["digest"]).read_text())["outcome"] == "verified"


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
    plan_path = tmp_path / "plan.json"
    approval_path = tmp_path / "approval.json"
    plan = artifact_plan()
    write_json(plan_path, plan)
    write_json(approval_path, approval(plan))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: pytest.fail("context must not be accessed"))

    with pytest.raises(partdb_write.WriteError, match="without --apply"):
        partdb_write.apply(apply_args(plan_path, approval_path, apply=False))


def test_unbound_plan_requires_new_approval_before_context_access(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    approval_path = tmp_path / "approval.json"
    artifact = artifact_plan()
    del artifact["instance_id"]
    artifact["digest"] = partdb_write.digest({key: value for key, value in artifact.items() if key != "digest"})
    write_json(plan_path, artifact)
    write_json(approval_path, approval(artifact))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: pytest.fail("context must not be accessed"))

    with pytest.raises(partdb_write.WriteError, match="create and approve a new plan"):
        partdb_write.apply(apply_args(plan_path, approval_path))


@pytest.mark.parametrize(("current_amount", "expected_outcome"), [(3, "needs-reconciliation"), (2, "already-target")])
def test_apply_drift_and_noop_do_not_obtain_write_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, current_amount: int, expected_outcome: str,
) -> None:
    plan_path = tmp_path / "plan.json"
    approval_path = tmp_path / "approval.json"
    plan = artifact_plan()
    write_json(plan_path, plan)
    write_json(approval_path, approval(plan))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: (tmp_path, {}))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "environment", lambda *_args: ("https://private.invalid", "read-token"))
    monkeypatch.setitem(vars(partdb_write), "verify_lot_patch_schema", lambda *_args: None)
    monkeypatch.setitem(vars(partdb_write), "read_lot", lambda *_args: {"amount": current_amount})
    monkeypatch.setitem(vars(partdb_write), "write_environment", lambda *_args: pytest.fail("write token must not be read"))

    if expected_outcome == "needs-reconciliation":
        with pytest.raises(partdb_write.WriteError, match="changed after planning"):
            partdb_write.apply(apply_args(plan_path, approval_path))
    else:
        partdb_write.apply(apply_args(plan_path, approval_path))
    receipt = json.loads(partdb_write.receipt_path(plan_path, plan["digest"]).read_text())
    assert receipt["outcome"] == expected_outcome


def test_apply_writes_then_read_back_verifies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    approval_path = tmp_path / "approval.json"
    plan = artifact_plan()
    write_json(plan_path, plan)
    write_json(approval_path, approval(plan))
    reads = iter(({"amount": 1}, {"amount": 2}))
    patched: list[tuple[object, ...]] = []
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: (tmp_path, {}))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "environment", lambda *_args: ("https://private.invalid", "read-token"))
    monkeypatch.setitem(vars(partdb_write), "verify_lot_patch_schema", lambda *_args: None)
    monkeypatch.setitem(vars(partdb_write), "read_lot", lambda *_args: next(reads))
    monkeypatch.setitem(vars(partdb_write), "write_environment", lambda *_args: ("https://private.invalid", "write-token"))
    monkeypatch.setitem(vars(partdb_write), "patch_lot", lambda *args: patched.append(args))

    partdb_write.apply(apply_args(plan_path, approval_path))

    assert patched == [("https://private.invalid", "write-token", 7, 2)]
    assert json.loads(partdb_write.receipt_path(plan_path, plan["digest"]).read_text())["outcome"] == "verified"


def test_apply_refuses_reused_approval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    approval_path = tmp_path / "approval.json"
    plan = artifact_plan()
    write_json(plan_path, plan)
    write_json(approval_path, approval(plan))
    partdb_write.receipt_path(plan_path, plan["digest"]).parent.mkdir()
    partdb_write.receipt_path(plan_path, plan["digest"]).write_text(json.dumps(partdb_write.receipt(plan["digest"], "verified")))
    monkeypatch.setitem(vars(partdb_write.partdb_read), "context", lambda: pytest.fail("context must not be accessed"))

    with pytest.raises(partdb_write.WriteError, match="already used"):
        partdb_write.apply(apply_args(plan_path, approval_path))


def test_apply_refuses_same_read_and_write_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    approval_path = tmp_path / "approval.json"
    plan = artifact_plan()
    write_json(plan_path, plan)
    write_json(approval_path, approval(plan))
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
