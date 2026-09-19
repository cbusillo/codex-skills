#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused validation for classify_auto_review_ledger.py."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).with_name("classify_auto_review_ledger.py")


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("classify_auto_review_ledger", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load classify_auto_review_ledger.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def put_file(path: Path, text: str) -> None:
    subprocess.run(["/bin/sh", "-c", "printf '%s' \"$2\" > \"$1\"", "sh", str(path), text], check=True)


def init_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    put_file(root / "README.md", "test\n")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(root / "remote.git")], cwd=root, check=True)
    subprocess.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], cwd=root, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", head], cwd=root, check=True)
    return head


def test_detached_auto_review_ledger_is_not_actionable(module: ModuleType) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        head = init_repo(repo)
        ledger = f"""
<auto_review_ledger schema_version="1">
run id=139529ee status=Completed freshness=Current target=matching_head source=Tui branch=auto-review-628291b6 snapshot=628291b61b7d age=gte2h findings=2 summary=Detached proposal
  finding id=f1 priority=1 location={repo}/.code/working/codex-skills/branches/auto-review-628291b6/github-work-rollup/scripts/github_work_rollup.py:541 title=[P1] Preserve derived_context
  finding id=f2 priority=2 location={repo}/.code/working/codex-skills/branches/auto-review-628291b6/github-work-rollup/scripts/github_work_rollup.py:739 title=[P2] Rank repos
</auto_review_ledger>
"""
        runs = module.parse_ledger(ledger)
        payload = module.classify_run(runs[0], module.collect_git_state(repo))
    if payload["classification"] != "detached_auto_review":
        raise AssertionError(f"expected detached classification: {payload}")
    if payload["actionable_by_default"]:
        raise AssertionError(f"detached ledger should not be actionable by default: {payload}")
    if payload["snapshot_matches_head"]:
        raise AssertionError(f"stale snapshot should not match current head {head}: {payload}")
    if payload["recommended_action"] != "ignore_or_summarize_as_stale_detached_noise":
        raise AssertionError(f"expected stale detached recommendation: {payload}")
    finding = payload["findings"][0]
    if "location" in finding or "title" in finding:
        raise AssertionError(f"default output should redact raw finding details: {payload}")
    if finding["location_kind"] != "detached_auto_review_worktree" or not finding["location_id"]:
        raise AssertionError(f"expected stable redacted location metadata: {payload}")


def test_trusted_local_details_include_raw_location(module: ModuleType) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        init_repo(repo)
        location = f"{repo}/.code/working/codex-skills/branches/auto-review-x/file.py:1"
        ledger = f"run id=detached status=Completed branch=auto-review-x snapshot=1234567 findings=1\n  finding id=f1 priority=2 location={location} title=[P2] Example\n"
        run = module.parse_ledger(ledger)[0]
        payload = module.classify_run(run, module.collect_git_state(repo), trusted_local_details=True)
    finding = payload["findings"][0]
    if finding.get("location") != location or finding.get("title") != "[P2] Example":
        raise AssertionError(f"trusted details should preserve raw finding fields: {payload}")


def test_current_head_auto_review_worktree_is_actionable(module: ModuleType) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        head = init_repo(repo)
        ledger = f"""
run id=current-auto status=Completed branch=auto-review-{head[:12]} snapshot={head[:12]} findings=1 summary=Current review from detached worktree
  finding id=f1 priority=1 location={repo}/.code/working/codex-skills/branches/auto-review-{head[:12]}/src/app.py:10 title=[P1] Current finding
"""
        run = module.parse_ledger(ledger)[0]
        payload = module.classify_run(run, module.collect_git_state(repo))
    if payload["classification"] != "current_target" or not payload["actionable_by_default"]:
        raise AssertionError(f"current-head auto-review findings should stay actionable: {payload}")


def test_human_auto_review_named_task_branch_is_not_generated_review_branch(module: ModuleType) -> None:
    if module.is_auto_review_branch("auto-review-friction-339"):
        raise AssertionError("human task branches with auto-review prefix should not match generated auto-review branch shape")
    if not module.is_auto_review_branch("auto-review-628291b6"):
        raise AssertionError("generated auto-review branch shape should still match")


def test_current_head_review_is_actionable(module: ModuleType) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        head = init_repo(repo)
        ledger = f"""
run id=current status=Completed freshness=Current target=matching_head branch=feature/example snapshot={head[:12]} findings=1 summary=Current review
  finding id=f1 priority=1 location={repo}/src/app.py:10 title=[P1] Current finding
"""
        run = module.parse_ledger(ledger)[0]
        payload = module.classify_run(run, module.collect_git_state(repo))
    if payload["classification"] != "current_target":
        raise AssertionError(f"expected current target: {payload}")
    if not payload["actionable_by_default"]:
        raise AssertionError(f"current-target findings should be actionable: {payload}")


def test_json_cli_summary(module: ModuleType) -> None:
    del module
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        init_repo(repo)
        ledger = Path(tmp) / "ledger.txt"
        put_file(ledger, "run id=detached status=Completed branch=auto-review-1234567 snapshot=1234567 findings=0\n")
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), str(ledger), "--repo", str(repo), "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        raise AssertionError(f"classifier failed: {completed.stderr}")
    payload = json.loads(completed.stdout)
    if payload["summary"].get("detached_auto_review") != 1:
        raise AssertionError(f"expected detached summary count: {payload}")


def main() -> int:
    module = load_module()
    test_detached_auto_review_ledger_is_not_actionable(module)
    test_trusted_local_details_include_raw_location(module)
    test_current_head_auto_review_worktree_is_actionable(module)
    test_human_auto_review_named_task_branch_is_not_generated_review_branch(module)
    test_current_head_review_is_actionable(module)
    test_json_cli_summary(module)
    print("ok validate-classify-auto-review-ledger")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
