#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Archive context keeps historical inventories visible without stale work."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("archive_plan", SCRIPTS / "gh-plan.py")
assert SPEC and SPEC.loader
PLAN: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)
from skills.direction.scripts import direction_audit as AUDIT


def issue(number: int, **fields: Any) -> dict[str, Any]:
    return {"number": number, "repo": "owner/history", "state": "open",
            "title": "Historical work", "labels": ["plan", "plan:active"],
            "body": "## Current Status\nState: Active.\nWaiting for: Supervisor routing.\n",
            "issue_dependencies_summary": {"blocked_by": 0, "total_blocked_by": 0}, **fields}


class ArchiveTests(unittest.TestCase):
    def test_index_retains_rows_and_refreshes_archive_metadata(self) -> None:
        args = PLAN.build_parser().parse_args(["--repo", "owner/history", "index"])
        emit = Mock()
        read = Mock(side_effect=[("automation-gh", {"archived": True}),
                                 ("automation-gh", {"archived": False})])
        with patch.multiple(PLAN, collect_paged_rest_items=Mock(return_value=("automation-gh", [issue(1), issue(2)])),
                            load_config=lambda _: PLAN.DEFAULT_CONFIG, api_json=read, emit=emit):
            PLAN.cmd_index(args)
            frozen = emit.call_args.args[0]
            PLAN.cmd_index(args)
            current = emit.call_args.args[0]
        self.assertEqual([row["number"] for row in frozen["plans"]], [1, 2])
        self.assertEqual(frozen["count"], current["count"])
        self.assertTrue(frozen["repository"]["archived"])
        self.assertFalse(current["repository"]["archived"])
        self.assertEqual(frozen["repository"]["disposition"], "frozen_historical")
        self.assertEqual(current["repository"]["disposition"], "current")
        self.assertEqual(read.call_count, 2)
        for output in (frozen, current):
            self.assertTrue(all(row["repository"] == output["repository"] for row in output["plans"]))
        self.assertEqual(read.call_args.args, ("GET", "/repos/owner/history"))

    def test_index_unknown_preserves_inventory(self) -> None:
        args = PLAN.build_parser().parse_args(["--repo", "owner/history", "index"])
        for metadata in ({}, {"archived": "false"}, None, PLAN.PlanError("metadata denied")):
            read = Mock(side_effect=metadata) if isinstance(metadata, Exception) else Mock(return_value=("automation-gh", metadata))
            emit = Mock()
            with patch.multiple(PLAN, collect_paged_rest_items=Mock(return_value=("automation-gh", [issue(1)])),
                                load_config=lambda _: PLAN.DEFAULT_CONFIG, api_json=read, emit=emit):
                PLAN.cmd_index(args)
            output = emit.call_args.args[0]
            self.assertEqual(output["count"], 1)
            self.assertIsNone(output["repository"]["archived"])
            self.assertEqual(output["repository"]["disposition"], "unknown")

    def test_archived_stale_report_accounts_for_all_open_rows_without_reconciliation_reads(self) -> None:
        rows = [issue(1), issue(2, labels=["plan", "plan:blocked"]),
                issue(3, labels=["plan", "plan:waiting"]), issue(4, labels=["plan"]),
                issue(5, state="closed"), issue(6, pull_request={})]
        read = Mock(return_value={"archived": True})
        for complete in (True, False):
            report = AUDIT.stale_wait_report(rows, "owner/history", fetch=read, inventory_complete=complete)
            self.assertEqual([row["number"] for row in report["frozen_issues"]], [1, 2, 3, 4])
            self.assertEqual(report["items"], [])
            self.assertEqual(report["checked"], 0)
            self.assertEqual(report["complete"], complete)
            self.assertTrue(report["repository"]["archived"])
        self.assertEqual(read.call_args.args[0], ["api", "repos/owner/history", "--method", "GET"])
        self.assertEqual(read.call_count, 2)
        self.assertEqual(rows[0]["labels"], ["plan", "plan:active"])

    def test_unknown_archive_state_degrades_coverage_without_losing_wait_evidence(self) -> None:
        row = issue(1, labels=["plan:waiting"])
        for metadata in ({"archived": False}, {}, AUDIT.AuditError("HTTP 403")):
            read = Mock(side_effect=metadata) if isinstance(metadata, Exception) else Mock(return_value=metadata)
            report = AUDIT.stale_wait_report([row], "owner/history", fetch=read)
            self.assertEqual(report["checked"], 1)
            self.assertEqual(report["items"][0]["number"], 1)
            self.assertEqual(report["frozen_issues"], [])
            known = isinstance(metadata, dict) and metadata.get("archived") is False
            self.assertEqual(report["complete"], known)
            if not known:
                self.assertIsNone(report["repository"]["archived"])
                self.assertEqual(report["unavailable"], [{"source": "repository", "reason": "unavailable"}])

    def test_global_wait_context_preserves_archive_disposition_and_skips_references(self) -> None:
        row = issue(1, labels=["plan:waiting"], body="## Current Status\nWaiting for: PR #71 to merge.\n")
        read = Mock(return_value=("automation-gh", {"archived": True}))
        with patch.multiple(PLAN, api_json=read, load_config=lambda _: PLAN.DEFAULT_CONFIG):
            report = PLAN.next_wait_context([row], scan_limit=10, inventory_complete=True)
        self.assertTrue(report["complete"])
        self.assertEqual(report["items"], [])
        self.assertEqual(report["references"], {})
        self.assertEqual(report["frozen_issues"][0]["repo"], "owner/history")
        self.assertTrue(report["repositories"]["owner/history"]["archived"])
        self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
