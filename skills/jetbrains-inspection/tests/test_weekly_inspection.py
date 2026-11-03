# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("weekly_inspection", SCRIPTS / "weekly_inspection.py")
if spec is None or spec.loader is None:
    raise ImportError("weekly inspection helper is unavailable")
weekly: Any = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = weekly
spec.loader.exec_module(weekly)


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


class WeeklyInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "baselines.json"
        self.state = {"repositories": {}}
        self.evidence = Path(self.temporary.name) / "evidence"
        self.evidence.mkdir()

    def run_record(self, day: int, counts: dict[str, int], **changes: object) -> None:
        monday = dt.datetime(2026, 11, 2, tzinfo=dt.UTC) + dt.timedelta(days=day)
        run: dict[str, Any] = {
            "status": "complete",
            "at": monday.isoformat(),
            "week": monday.date().isoformat(),
            "signature": {"profile": "shared"},
            "counts": counts,
            "source": "revision",
            "evidence": "receipt",
        }
        run.update(changes)
        weekly.record_run(self.state, "owner/repo", run)
        self.path.write_text(json.dumps(self.state))

    def test_ceilings_only_fall_and_reports_compare_last_week_not_ceiling(self) -> None:
        self.run_record(0, {"types": 10, "duplicates": 8, "spelling": 20})
        self.run_record(7, {"types": 8, "duplicates": 11, "spelling": 20, "new": 1})
        self.run_record(14, {"types": 9, "duplicates": 9, "spelling": 20, "new": 3})
        self.assertEqual(
            self.state["repositories"]["owner/repo"]["ceiling"], {"types": 8, "duplicates": 8, "spelling": 20, "new": 0}
        )
        summary = weekly.report("owner/repo", path=self.path, now=dt.datetime(2026, 11, 16, tzinfo=dt.UTC))
        self.assertEqual(
            {row["category"]: row["direction"] for row in summary["top_categories"]},
            {"spelling": "stayed", "duplicates": "down", "types": "up"},
        )
        self.assertEqual(summary["status"], "complete")

    def test_incomplete_run_preserves_baseline_and_exposes_unknown(self) -> None:
        self.run_record(0, {"types": 10})
        self.run_record(0, {}, status="unknown", reason="coverage incomplete")
        record = self.state["repositories"]["owner/repo"]
        self.assertEqual(record["latest"]["counts"], {"types": 10})
        self.assertEqual(record["ceiling"], {"types": 10})
        self.assertEqual(
            weekly.report("owner/repo", path=self.path, now=dt.datetime(2026, 11, 3, tzinfo=dt.UTC))["status"], "unknown"
        )

    def test_same_week_and_incomparable_or_missing_week_never_invent_trends(self) -> None:
        self.run_record(0, {"types": 10})
        self.run_record(0, {"types": 8})
        self.assertNotIn("previous", self.state["repositories"]["owner/repo"])
        for day, signature in ((14, {"profile": "shared"}), (21, {"profile": "changed"})):
            self.run_record(day, {"types": 6}, signature=signature)
            summary = weekly.report("owner/repo", path=self.path)
            self.assertEqual(summary["top_categories"][0]["direction"], "unavailable")
        self.run_record(28, {"types": 4}, signature={"profile": "changed"})
        summary = weekly.report("owner/repo", path=self.path, now=dt.datetime(2026, 11, 30, tzinfo=dt.UTC))
        self.assertEqual(summary["top_categories"][0]["ceiling"], 4)
        self.assertEqual(summary["top_categories"][0]["direction"], "down")

    def test_full_artifact_counts_existing_and_advisory_observations(self) -> None:
        findings = Path(self.temporary.name) / "findings.json"
        findings.write_text(json.dumps([{"rule_id": "types", "introduced": False}, {"rule_id": "spelling", "severity": "TYPO"}]))
        result: dict[str, Any] = {
            "backend": "native",
            "agent_result": {"verdict": "GREEN"},
            "coverage": {"complete": True},
            "cleanup": {"status": "closed"},
            "findings": [],
            "findings_artifact": str(findings),
        }
        self.assertEqual(weekly.category_counts(result), {"types": 1, "spelling": 1})
        result.update(findings_artifact=None, finding_count=0, preexisting_finding_count=0, advisory_finding_count=0)
        self.assertEqual(weekly.category_counts(result), {})
        result["advisory_finding_count"] = 1
        with self.assertRaisesRegex(ValueError, "artifact"):
            weekly.category_counts(result)
        result["coverage"]["complete"] = False
        with self.assertRaisesRegex(ValueError, "incomplete"):
            weekly.category_counts(result)

    def test_failed_repository_does_not_hide_the_next_repository(self) -> None:
        worktree = Path(self.temporary.name) / "worktree"

        def fixture_inspection(_worktree: Path, _evidence: Path, now: dt.datetime) -> dict[str, Any]:
            return {
                "status": "complete",
                "at": now.isoformat(),
                "week": now.date().isoformat(),
                "signature": {},
                "counts": {"types": 4},
                "source": "fixture",
                "evidence": str(_evidence),
            }

        with (
            patch.object(
                weekly,
                "audit_repositories",
                return_value=(["owner/broken", "owner/good"], {"complete": True, "unknown": {}, "gaps": []}),
            ),
            patch_attr(weekly, "inspection_worktree", side_effect=[ValueError("missing checkout"), worktree]),
            patch_attr(weekly, "inspect_repository", side_effect=fixture_inspection),
        ):
            code = weekly.main(
                [
                    "--owner",
                    "owner",
                    "--repository-root",
                    self.temporary.name,
                    "--evidence-root",
                    str(Path(self.temporary.name) / "evidence"),
                    "--baseline",
                    str(self.path),
                ]
            )
        saved = weekly.read_baselines(self.path)["repositories"]
        self.assertEqual(code, 0)
        self.assertEqual(saved["owner/broken"]["attempt"]["status"], "unknown")
        self.assertEqual(saved["owner/good"]["latest"]["counts"], {"types": 4})
        self.assertEqual(saved["owner/broken"]["attempt"]["at"], saved["owner/good"]["latest"]["at"])

    def test_lane_selection_keeps_exclusions_and_names_generated_lock_exception(self) -> None:
        root = Path(self.temporary.name)
        metadata = {
            "qualityGate": {
                "inspection": {
                    "lanes": [
                        {
                            "id": "python",
                            "ide": "PyCharm",
                            "required": True,
                            "include": ["**/*.py", "**/*.yaml"],
                            "exclude": ["vendor/**"],
                        },
                        {"id": "frontend", "ide": "WebStorm", "required": True, "include": ["**/*.ts"], "projectPath": "frontend"},
                    ]
                }
            }
        }
        (root / ".github").mkdir()
        (root / ".github/github.json").write_text(json.dumps(metadata))
        for file in ("code.py", "vendor/dependency.py", "frontend/app.ts", "frontend/pnpm-lock.yaml", "README.md"):
            target = root / file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("")
        helper = weekly.inspection_helper()
        with (
            patch_attr(weekly, "inspection_helper", return_value=helper),
            patch.object(
                helper,
                "inspection_scope_paths",
                return_value=["code.py", "vendor/dependency.py", "frontend/app.ts", "frontend/pnpm-lock.yaml", "README.md"],
            ),
        ):
            groups, omitted = weekly.selections(root)
        self.assertEqual(groups, {"python": ["code.py"], "frontend": ["frontend/app.ts"]})
        self.assertEqual([row["file"] for row in omitted["excluded"]], ["vendor/dependency.py"])
        self.assertEqual(omitted["generated_locks"], ["frontend/pnpm-lock.yaml"])
        self.assertEqual(omitted["unmatched"], ["README.md"])

    def test_retired_worktree_is_recreated_and_ignored_environment_is_preserved(self) -> None:
        root = Path(self.temporary.name)
        created = root / "created"
        created.mkdir()
        source = root / "repo"
        self.state["repositories"]["owner/repo"] = {"worktree": str(root / "retired"), "branch": "work/retired"}

        def fixture_git(_path: Path, *args: str) -> str:
            if args[:3] == ("remote", "get-url", "origin"):
                return "https://github.com/owner/repo.git"
            if args[0] == "symbolic-ref":
                return "refs/remotes/origin/main"
            if args[0] == "rev-parse":
                return str(source / ".git")
            if args[0] == "branch":
                return self.state["repositories"]["owner/repo"]["branch"]
            if args[0] == "status" and "--ignored" in args:
                return "!! .venv/"
            return ""

        result = subprocess.CompletedProcess([], 0, stdout=f"{created}\n")
        with patch_attr(weekly, "git", side_effect=fixture_git), patch.object(subprocess, "run", return_value=result) as creator:
            self.assertEqual(weekly.inspection_worktree("owner/repo", root, self.state), created)
            (created / ".venv").mkdir()
            self.assertEqual(weekly.inspection_worktree("owner/repo", root, self.state), created)
            self.assertEqual(creator.call_count, 1)

    def test_discovery_failure_is_reported_and_next_schedule_can_run(self) -> None:
        with patch_attr(weekly, "audit_repositories", side_effect=ValueError("read unavailable")):
            self.assertEqual(
                weekly.main(
                    [
                        "--owner",
                        "owner",
                        "--repository-root",
                        self.temporary.name,
                        "--evidence-root",
                        str(self.evidence),
                        "--baseline",
                        str(self.path),
                    ]
                ),
                0,
            )
        self.assertEqual(weekly.read_baselines(self.path)["discovery"]["status"], "unknown")
        with patch_attr(weekly, "audit_repositories", return_value=([], {"complete": True, "unknown": {}, "gaps": []})):
            self.assertEqual(
                weekly.main(
                    [
                        "--owner",
                        "owner",
                        "--repository-root",
                        self.temporary.name,
                        "--evidence-root",
                        str(self.evidence),
                        "--baseline",
                        str(self.path),
                    ]
                ),
                0,
            )
        self.assertEqual(weekly.read_baselines(self.path)["discovery"]["status"], "complete")

    def test_busy_or_corrupt_baseline_preserves_bytes_and_does_not_stop_schedule(self) -> None:
        self.path.write_text("{corrupt")
        before = self.path.read_bytes()
        args = [
            "--owner",
            "owner",
            "--repository-root",
            self.temporary.name,
            "--evidence-root",
            str(self.evidence),
            "--baseline",
            str(self.path),
        ]
        with weekly.service_lock(self.path, "inspection"):
            self.assertEqual(weekly.main(args), 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(weekly.main(args), 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_root_selection_excludes_generated_lock_and_normalizes_helper_error(self) -> None:
        root = Path(self.temporary.name)
        (root / ".github").mkdir()
        config = root / ".github/github.json"
        config.write_text(json.dumps({"qualityGate": {"inspection": {"ide": "PyCharm"}}}))
        helper = weekly.inspection_helper()
        with (
            patch_attr(weekly, "inspection_helper", return_value=helper),
            patch.object(
                helper, "inspection_scope_paths", return_value=["code.py", "docs/pnpm-lock.yaml", "docs/site.yaml", "README.md"]
            ),
        ):
            groups, omitted = weekly.selections(root)
        self.assertEqual(groups, {"project": ["code.py", "docs/site.yaml"]})
        self.assertEqual(omitted["generated_locks"], ["docs/pnpm-lock.yaml"])
        config.write_text(json.dumps({"qualityGate": {"inspection": {"lanes": [{"id": "bad"}]}}}))
        with self.assertRaises(ValueError):
            weekly.selections(root)

    def test_partial_discovery_runs_known_repository_and_records_unknown_adoption(self) -> None:
        discovery = {"complete": False, "unknown": {"owner/unread": "adoption unavailable"}, "gaps": []}
        now = dt.datetime.now(dt.UTC)
        run: dict[str, Any] = {
            "status": "complete",
            "at": now.isoformat(),
            "week": now.date().isoformat(),
            "source": "fixture",
            "signature": {},
            "counts": {"types": 1},
            "evidence": "receipt",
        }
        with (
            patch_attr(weekly, "audit_repositories", return_value=(["owner/good"], discovery)),
            patch_attr(weekly, "inspection_worktree", return_value=Path(self.temporary.name)) as prepare,
            patch_attr(weekly, "inspect_repository", return_value=run),
        ):
            self.assertEqual(
                weekly.main(
                    [
                        "--owner",
                        "owner",
                        "--repository-root",
                        self.temporary.name,
                        "--evidence-root",
                        str(self.evidence),
                        "--baseline",
                        str(self.path),
                    ]
                ),
                0,
            )
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(weekly.report("owner/good", path=self.path)["status"], "complete")
        self.assertEqual(weekly.report("owner/unread", path=self.path)["status"], "unknown")

    def test_missing_or_corrupt_baseline_is_unknown(self) -> None:
        self.assertEqual(weekly.report("owner/repo", path=self.path)["status"], "unknown")
        self.path.write_text("{")
        self.assertEqual(weekly.report("owner/repo", path=self.path)["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
