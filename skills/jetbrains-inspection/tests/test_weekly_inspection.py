# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import weekly_inspection as weekly


class WeeklyInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "baselines.json"
        self.state = {"repositories": {}}

    def run_record(self, day: int, counts: dict[str, int], **changes: object) -> None:
        monday = dt.datetime(2026, 11, 2, tzinfo=dt.UTC) + dt.timedelta(days=day)
        run = {
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
            self.state["repositories"]["owner/repo"]["ceiling"]["counts"], {"types": 8, "duplicates": 8, "spelling": 20, "new": 0}
        )
        summary = weekly.report("owner/repo", path=self.path, now=dt.datetime(2026, 11, 16, tzinfo=dt.UTC))
        self.assertEqual(
            {row["category"]: row["direction"] for row in summary["top_categories"]},
            {"spelling": "stayed", "duplicates": "down", "types": "up"},
        )
        self.assertEqual(summary["status"], "complete")

    def test_incomplete_run_preserves_baseline_and_exposes_unknown(self) -> None:
        self.run_record(0, {"types": 10})
        self.run_record(7, {}, status="unknown", reason="coverage incomplete")
        record = self.state["repositories"]["owner/repo"]
        self.assertEqual(record["latest"]["counts"], {"types": 10})
        self.assertEqual(record["ceiling"]["counts"], {"types": 10})
        self.assertEqual(weekly.report("owner/repo", path=self.path)["status"], "unknown")

    def test_same_week_and_incomparable_or_missing_week_never_invent_trends(self) -> None:
        self.run_record(0, {"types": 10})
        self.run_record(0, {"types": 8})
        self.assertNotIn("previous", self.state["repositories"]["owner/repo"])
        for day, signature in ((14, {"profile": "shared"}), (21, {"profile": "changed"})):
            self.run_record(day, {"types": 6}, signature=signature)
            summary = weekly.report("owner/repo", path=self.path)
            self.assertEqual(summary["top_categories"][0]["direction"], "unavailable")
        self.assertEqual(self.state["repositories"]["owner/repo"]["ceiling"]["counts"], {"types": 6})

    def test_full_artifact_counts_existing_and_advisory_observations(self) -> None:
        findings = Path(self.temporary.name) / "findings.json"
        findings.write_text(json.dumps([{"rule_id": "types", "introduced": False}, {"rule_id": "spelling", "severity": "TYPO"}]))
        result = {
            "backend": "native",
            "agent_result": {"verdict": "GREEN"},
            "coverage": {"complete": True},
            "cleanup": {"status": "closed"},
            "findings": [],
            "findings_artifact": str(findings),
        }
        self.assertEqual(weekly.category_counts(result), {"types": 1, "spelling": 1})
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
            patch.object(weekly, "audit_repositories", return_value=["owner/broken", "owner/good"]),
            patch.object(weekly, "inspection_worktree", side_effect=[ValueError("missing checkout"), worktree]),
            patch.object(weekly, "inspect_repository", side_effect=fixture_inspection),
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
            patch.object(weekly, "inspection_helper", return_value=helper),
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

    def test_missing_or_corrupt_baseline_is_unknown(self) -> None:
        self.assertEqual(weekly.report("owner/repo", path=self.path)["status"], "unknown")
        self.path.write_text("{")
        self.assertEqual(weekly.report("owner/repo", path=self.path)["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
