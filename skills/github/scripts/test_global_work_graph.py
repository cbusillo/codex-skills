#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import re
import unittest
from typing import Any
from unittest.mock import patch

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
                next_source_delivery=lambda *_args, **_kwargs: {"complete": True, "merged_prs": []},
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
