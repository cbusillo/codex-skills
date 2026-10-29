#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import re
import unittest
from typing import Any
from unittest.mock import Mock, patch

import test_gh_plan_next as cli
from test_github_work_graph import connection, node, replay, unavailable


def native_nodes(sources: list[dict[str, Any]], edges: dict[tuple[str, int], dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for source in sources:
        raw = node(source["number"])
        raw.update(
            repository={"nameWithOwner": source["repo"]},
            url=source["html_url"],
            author={"login": source["repo"].split("/")[0]},
            title=source["title"],
        )
        if source.get("body"):
            raw["body"] = source["body"]
        milestone = source.get("milestone")
        if milestone:
            raw["milestone"] = {
                "number": milestone["number"],
                "title": milestone["title"],
                "state": milestone["state"].upper(),
                "createdAt": milestone["created_at"],
                "dueOn": None,
                "url": milestone["html_url"],
            }
        related = edges.get((source["repo"], source["number"]), cli.relationships())
        for field, name in (("blockedBy", "blocked_by"), ("blocking", "blocking"), ("subIssues", "sub_issues")):
            raw[field] = connection(
                [
                    {
                        "number": value["number"],
                        "title": value["title"],
                        "state": value["state"].upper(),
                        "url": value["html_url"],
                        "repository": {"nameWithOwner": value["repo"]},
                    }
                    for value in related[name]
                ]
            )
        rows.append(raw)
    return rows


def run_native_graph(
    roots: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: dict[tuple[str, int], dict[str, Any]],
    *,
    discovered: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], Any, list[str]]:
    clock = {"now": 100.0}
    replayed = replay(native_nodes([*roots, *nodes, *(discovered or [])], edges))
    ownership = []
    with cli.global_fixture(roots, nodes, edges, discovered=discovered) as (module, result, _individual_reads):
        collect = module.collect_paged_rest_items
        fallback_relationships = module.read_next_issue_relationships
        reader = object.__new__(module.PlanningGraphReader)
        reader.request_actor = reader.actor = "automation-gh"
        reader.expected_actor = "automation-gh"
        reader.deadline_at = None
        reader.results = []

        def transport(self: Any, query: str, variables: dict[str, Any], **kwargs: Any) -> Any:
            clock["now"] += 1
            response = replayed.graphql_json(query, variables, **kwargs)
            self.results.append(response)
            return response

        def list_items(path: str, **kwargs: Any) -> Any:
            if path.endswith(("/pulls", "/branches")):
                clock["now"] += 1
                ownership.append(path)
                return "automation-gh", []
            return collect(path, **kwargs)

        def relationships(repo: str, number: int, raw: dict[str, Any]) -> Any:
            if "_graph_relationships" in raw:
                related, truncated = module.github_work_graph.read_relationships(repo, number, raw, unavailable)
                return "automation-gh", related, truncated
            return fallback_relationships(repo, number, raw)

        args = cli.next_args()
        args.time_budget = 20
        args.read_workers = 4
        with (
            patch.object(module.github_read.GitHubReader, "graphql_json", transport),
            patch.multiple(
                module,
                next_graph_reader=lambda _repo: reader,
                get_issue=unavailable,
                collect_paged_rest_items=list_items,
                read_next_issue_relationships=relationships,
                annotate_next_queue=module.real_annotate_next_queue,
            ),
            patch.object(
                module.github_plan_claim,
                "local_inventory",
                return_value={"remote_branches": [], "local_branches": [], "worktrees": []},
            ),
            patch.object(module.time, "time", lambda: clock["now"]),
        ):
            module.cmd_next(args)
    return result, replayed, ownership


class GlobalWorkGraphTests(unittest.TestCase):
    def test_unread_inventory_is_loud_while_a_read_empty_track_inventory_is_known_empty(self) -> None:
        import time

        with cli.global_fixture([], [], {}) as (module, result, _reads):
            module.cmd_next(cli.next_args())
            self.assertEqual({row["state"] for row in result["milestone_summary"]}, {"no_linked_work"})
            module.next_live_breakage = lambda *_args: ([], {"complete": False})
            with self.assertRaises(module.ClassifiedPlanError) as caught:
                module.cmd_next(cli.next_args())
            self.assertEqual(caught.exception.code, "live_breakage_unread")
            self.assertEqual({row["state"] for row in caught.exception.payload["milestone_summary"]}, {"unknown"})
            self.assertEqual(caught.exception.payload["recommended_next_action"], "review_partial_coverage")
        with cli.global_fixture([], [], {}) as (module, _result, _reads):
            collect = module.collect_paged_rest_items

            def expired(path: str, **kwargs: Any) -> Any:
                module.NEXT_READS.deadline = time.time() - 1
                return collect(path, **kwargs)

            module.collect_paged_rest_items = expired
            with self.assertRaises(module.ClassifiedPlanError) as caught:
                module.cmd_next(cli.next_args())
            self.assertEqual(caught.exception.code, "work_graph_unread")
            self.assertEqual({row["state"] for row in caught.exception.payload["milestone_summary"]}, {"unknown"})
            self.assertNotEqual(module.CURRENT_RETRY_FIELDS["recommended_next_action"], "none")

    def test_live_inventory_wrapper_reports_bounds_and_preserves_terminal_read_failures(self) -> None:
        module = cli.load_module()
        args = cli.next_args()
        args.repo_limit = 1
        repositories = [{"full_name": "someone/one"}, {"full_name": "someone/two"}]
        with patch.dict(module.github_identity.__dict__, {"github_app_config": lambda: {}}):
            module.collect_paged_rest_items = Mock(
                side_effect=lambda path, **_kwargs: ("fixture", repositories if path == "/installation/repositories" else [])
            )
            _rows, context = module.real_next_live_breakage("someone/direction", args)
            self.assertFalse(context["complete"])
            self.assertEqual(context["repository_inventory"]["repository_limit"], args.repo_limit)
            self.assertEqual(context["repository_inventory"]["inventory_observed_count"], len(repositories))
            self.assertEqual(context["recommended_next_action"], "increase_repo_limit")
            module.collect_paged_rest_items = Mock(side_effect=module.PlanError("Inventory unavailable"))
            self.assertFalse(module.real_next_live_breakage("someone/direction", args)[1]["complete"])
            terminal = module.ClassifiedPlanError("fixture_terminal", "Stop at the configured reader")
            module.collect_paged_rest_items = Mock(side_effect=terminal)
            with self.assertRaises(module.ClassifiedPlanError) as caught:
                module.real_next_live_breakage("someone/direction", args)
            self.assertIs(caught.exception, terminal)
        with cli.global_fixture([], [], {}) as (module, _result, _reads):
            module.next_live_breakage = lambda *_args: ([], context)
            with self.assertRaises(module.ClassifiedPlanError) as caught:
                module.cmd_next(cli.next_args())
            self.assertEqual(caught.exception.payload["recommended_next_action"], "increase_repo_limit")

    def test_unavailable_track_inventory_or_all_track_bodies_cannot_report_empty_success(self) -> None:
        root = cli.track("someone/direction", 1, "First")
        for inventory_failed in (True, False):
            with cli.global_fixture([root], [], {}) as (module, _result, _reads):
                module.CURRENT_RETRY_FIELDS["recommended_next_action"] = "retry_after_reported_reset"
                inventory = module.github_work_graph.TrackInventory("fixture", [root], {}, True)
                with patch.dict(
                    module.github_work_graph.__dict__,
                    {
                        "track_inventory": Mock(side_effect=ValueError("Unavailable"))
                        if inventory_failed
                        else Mock(return_value=inventory)
                    },
                ):
                    module.get_issue = Mock(side_effect=module.PlanError("Track unavailable"))
                    with self.assertRaises(module.ClassifiedPlanError) as caught:
                        module.cmd_next(cli.next_args())
                self.assertEqual(caught.exception.code, "work_graph_unread")
                self.assertEqual(caught.exception.payload["recommended_next_action"], "retry_after_reported_reset")
                self.assertEqual({row["state"] for row in caught.exception.payload["milestone_summary"]}, {"unknown"})

    def test_marked_live_work_is_evaluated_before_milestone_work(self) -> None:
        roots = [cli.track("someone/direction", 1, "First"), cli.track("someone/direction", 2, "Second")]
        leaf = cli.global_issue("someone/product", 10)
        incident = cli.global_issue(
            "someone/live", 90, labels=["plan", "plan:active", cli.load_module().github_direction_next.LIVE_BREAKAGE_LABEL]
        )
        edges = {(roots[0]["repo"], 1): cli.relationships(sub_issues=[leaf])}
        with cli.global_fixture(roots, [leaf, incident], edges, discovered=[incident]) as (module, result, _reads):
            relationship_reads = module.read_next_issue_relationships
            observed = []
            get_issue = Mock(wraps=module.get_issue)
            module.get_issue = get_issue

            def read(repo: str, number: int, *args: Any, **kwargs: Any) -> Any:
                observed.append(number)
                return relationship_reads(repo, number, *args, **kwargs)

            module.read_next_issue_relationships = read
            reference = {key: incident[key] for key in ("repo", "number", "title", "html_url", "state")}
            module.next_live_breakage = lambda *_args: ([reference], {"complete": True})
            module.cmd_next(cli.next_args())
            get_issue.assert_any_call(str(incident["number"]), incident["repo"])
            self.assertLess(observed.index(incident["number"]), observed.index(roots[0]["number"]))
            self.assertEqual(result["candidates"][0]["number"], incident["number"])

    def test_success_recommendation_reflects_read_coverage_and_preserves_specific_transport_guidance(self) -> None:
        roots = [cli.track("someone/direction", 1, "First"), cli.track("someone/direction", 2, "Second")]
        leaf = cli.global_issue("someone/product", 10)
        edges = {(roots[0]["repo"], 1): cli.relationships(sub_issues=[leaf])}
        with cli.global_fixture(roots, [leaf], edges) as (module, result, _reads):
            module.CURRENT_RETRY_FIELDS["recommended_next_action"] = "inspect_last_failure"
            module.cmd_next(cli.next_args())
            self.assertEqual(result["recommended_next_action"], "none")

            def unread_ownership(items: list[dict[str, Any]], _args: Any) -> dict[str, Any]:
                for item in items:
                    item["queue"] = {"state": "review", "claim_required": True}
                return {"repositories": {leaf["repo"]: {"complete": False}}}

            module.annotate_next_queue = unread_ownership
            module.CURRENT_RETRY_FIELDS["recommended_next_action"] = "inspect_last_failure"
            module.cmd_next(cli.next_args())
            self.assertFalse(result["candidate_coverage"]["complete"])
            self.assertEqual(result["recommended_next_action"], "review_partial_coverage")
            module.CURRENT_RETRY_FIELDS["recommended_next_action"] = "retry_after_reported_reset"
            module.cmd_next(cli.next_args())
            self.assertEqual(result["recommended_next_action"], "retry_after_reported_reset")

    def test_discovery_prefetch_keeps_full_batches_until_the_final_chunk(self) -> None:
        root = cli.track("someone/direction", 1, "First")
        leaf = cli.global_issue("someone/product", 2)
        discovered = [cli.global_issue("someone/product", number) for number in range(20, 29)]
        edges = {(root["repo"], 1): cli.relationships(sub_issues=[leaf])}
        result, reader, _ownership = run_native_graph([root], [leaf], edges, discovered=discovered)
        numbers = {row["number"] for row in discovered}
        batches = [
            numbers.intersection(map(int, re.findall(r"issue\(number:(\d+)\)", call.args[0])))
            for call in reader.graphql_json.call_args_list
        ]
        batches = [batch for batch in batches if batch]
        self.assertEqual(set.union(*batches), numbers)
        workers = result["performance"]["workers"]
        self.assertLessEqual(len(batches), (len(discovered) + workers - 1) // workers)

    def test_native_graph_reaches_ownership_inside_a_deterministic_request_budget(self) -> None:
        root = cli.track("someone/direction", 1, "First")
        leaves = [cli.global_issue("someone/product", number) for number in range(2, 12)]
        edges = {(root["repo"], 1): cli.relationships(sub_issues=leaves)}
        result, reader, ownership = run_native_graph([root], leaves, edges)
        self.assertTrue(result["available_candidates"], result.get("excluded"))
        self.assertTrue(all(row["queue"]["observed_ownership_complete"] for row in result["available_candidates"]))
        self.assertFalse(result["performance"]["deadline_exhausted"])
        self.assertTrue(ownership)
        self.assertLess(reader.graphql_json.call_count, len(leaves))
        self.assertGreaterEqual(result["performance"]["reads"], reader.graphql_json.call_count)

    def test_ancestry_and_discovery_seed_assignments_keep_native_observations(self) -> None:
        root = cli.track("someone/direction", 1, "First")
        milestone_leaf = cli.global_issue("someone/product", 2)
        parent = cli.global_issue("someone/product", 20)
        child = cli.global_issue("someone/product", 21)
        edges = {
            (root["repo"], 1): cli.relationships(sub_issues=[milestone_leaf]),
            (parent["repo"], 20): cli.relationships(sub_issues=[child]),
        }
        result, _reader, _ownership = run_native_graph([root], [milestone_leaf], edges, discovered=[child, parent])
        observed = next(row for row in result["candidates"] if row["number"] == child["number"])
        self.assertTrue(observed["discussion"]["complete"])
        ancestors = observed["discussion"]["parents"]
        self.assertTrue(ancestors)
        self.assertEqual(ancestors[0]["number"], parent["number"])
        self.assertTrue(ancestors[0]["discussion"]["complete"])
        self.assertNotIn("unknown_ancestry", {row.get("exclusion") for row in result["excluded"]})


if __name__ == "__main__":
    unittest.main()
