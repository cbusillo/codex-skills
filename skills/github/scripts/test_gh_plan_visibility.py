# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import Mock, patch

import test_gh_plan_next as cli
from test_github_next_queue import evidence


def request(number: int, labels: list[str] | None = None) -> dict[str, Any]:
    return {**cli.issue(number), "labels": labels or [], "user": {"login": "owner"}, "body": "Please repair the export."}


def filter_rows(rows: list[dict[str, Any]], labels: list[str] | None, limit: int) -> list[dict[str, Any]]:
    return [row for row in rows if not labels or set(labels) <= {
        label if isinstance(label, str) else label["name"] for label in row["labels"]}][:limit]


class VisibilityTests(unittest.TestCase):
    def test_index_includes_unlabeled_requests_preserves_explicit_queries_and_reports_omissions(self) -> None:
        module = cli.load_module()
        rows = [request(1), request(2, ["customer"]), request(3, ["plan", "plan:active"])]

        def collect(_path: str, *, query: dict[str, Any], limit: int, **_kwargs: Any) -> tuple[str, list[dict[str, Any]]]:
            return "automation-gh", filter_rows(rows, [query["labels"]] if query.get("labels") else None, limit)

        for options, archived, expected, complete in [([], False, [1, 2, 3], True), (["--label", "customer"], False, [2], True),
                    (["--limit", "2"], False, [1, 2], False), (["--limit", "2"], True, [], True),
                    (["--limit", "2", "--include-archived"], True, [1, 2], False)]:
            output: dict[str, Any] = {}
            with patch.multiple(module, collect_paged_rest_items=collect, emit=output.update,
                                api_json=Mock(return_value=("automation-gh", {"archived": archived}))):
                module.cmd_index(module.build_parser().parse_args(["--repo", "owner/repo", "index", *options]))
            self.assertEqual([row["number"] for row in output["plans"]], expected)
            self.assertEqual(output["inventory_complete"], complete)
            self.assertEqual(output["truncated"], not complete)

    def test_local_next_surfaces_untriaged_requests_on_both_read_routes_and_keeps_holds(self) -> None:
        for batch in (False, True):
            module = cli.load_module()
            rows = [request(1), request(2, ["customer"]), request(5), request(6), request(7, ["agent:claude"]), request(9)]
            rows[-1]["body"] = "## Current Status\nNext actor: agent\nNext action: Repair the export.\n"
            waits = {number: {"version": 1, "state": "review", "evidence": "Not triaged", "waits": []} for number in (1, 2)}
            waits[6] = module.github_plan_wait.reviewed(
                waiting_for="Device test", ends_when="Tester records a result", evidence="Requested test")

            def collect(_path: str, *, query: dict[str, Any], limit: int, **_kwargs: Any) -> tuple[str, list[dict[str, Any]]]:
                return "automation-gh", filter_rows(rows, [query["labels"]] if query.get("labels") else None, limit)

            def graph(_repo: str, *, labels: list[str] | None = None, limit: int, **_kwargs: Any) -> list[dict[str, Any]]:
                module.BATCH_READ_CONTEXT["diagnostics"] = {"actor": "automation-gh"}
                return filter_rows(rows, labels, limit)

            def relations(_repo: str, number: int, _raw: dict[str, Any]) -> tuple[str, dict[str, Any], list[str]]:
                return "automation-gh", cli.relationships(blocked_by=[cli.related(50)] if number == 5 else []), []

            with cli.local_next_fixture(module, rows, collect_plans=collect, read_relationships=relations, waits=waits) as output:
                with patch.multiple(module, annotate_next_queue=module.real_annotate_next_queue, read_planning_graph=graph,
                                    get_issue=lambda number, _repo: ("automation-gh", next(row for row in rows if row["number"] == int(number))),
                                    read_queue_ownership=lambda _repo: evidence(), wait_authors=lambda _repo: {"owner"},
                                    next_status_body_time=lambda *_args: None):
                    args = module.build_parser().parse_args(["--repo", "owner/repo", "next", "--agent", "codex"]
                                                           + (["--batch-read"] if batch else []))
                    module.cmd_next(args)
            self.assertEqual([row["number"] for row in output["needs_status"]], [1, 2])
            self.assertEqual([row["number"] for row in output["available_candidates"]], [9])
            self.assertEqual({row["number"]: row["exclusion"] for row in output["excluded"]}, {
                5: "blocked_by_open_dependency", 6: "waiting", 7: "assigned_elsewhere"})
            self.assertTrue(all(row["queue"]["claim_required"] for row in output["needs_status"]))
            self.assertFalse(output["truncated"])

    def test_local_next_prioritizes_unlabeled_milestone_work_before_reporting_scan_omissions(self) -> None:
        module = cli.load_module()
        rows = [request(1), {**request(2), "milestone": cli.milestone_data(1, "First", created_at="2026-07-01T00:00:00Z")}]
        with cli.local_next_fixture(module, rows, read_relationships=lambda *_args: ("automation-gh", cli.relationships(), [])) as output:
            args = module.build_parser().parse_args(["--repo", "owner/repo", "next", "--scan-limit", "1"])
            module.cmd_next(args)
        self.assertEqual([row["number"] for row in output["candidates"]], [2])
        self.assertEqual(output["inventory_count"], len(rows))
        self.assertEqual(output["evaluated"], 1)
        self.assertTrue(output["truncated"])


if __name__ == "__main__":
    unittest.main()
