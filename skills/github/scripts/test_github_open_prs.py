#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Discovery completeness and one-query behavior on synthetic provider replies."""
import copy
import unittest
from unittest.mock import Mock

import github_api
import github_open_prs
from github_read import GitHubReadError


def connection(nodes, total=None, cursor=None):
    return {"nodes": nodes, "totalCount": len(nodes) if total is None else total,
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def node(number=7):
    return {"number": number, "title": "Change", "body": "Notes", "state": "OPEN",
            "isDraft": False, "createdAt": "2026-10-21T00:00:00Z", "headRefOid": "a" * 40,
            "headRefName": "work/change", "baseRefName": "main", "headRepository": {"nameWithOwner": "example/fork"},
            "author": {"__typename": "User", "login": "author"}, "updatedAt": "2026-10-21T01:00:00Z",
            "url": "https://github.com/example/app/pull/7", "labels": connection([{"name": "hold"}])}


def repository(name, nodes, **kwargs):
    return {"nameWithOwner": name, "pullRequests": connection(nodes, **kwargs)}


def reader(*pages):
    value = Mock()
    value.batch_diagnostic = {}
    value.graphql_json.side_effect = [github_api.ApiResult(ok=True, status=200, body={"data": page}) for page in pages]
    return value


class DiscoveryTests(unittest.TestCase):
    def test_empty_and_many_repositories_use_zero_or_one_request(self):
        empty = reader()
        self.assertEqual(github_open_prs.scan(empty, []), {})
        empty.graphql_json.assert_not_called()
        repos = [f"example/app{i}" for i in range(26)]
        page = {f"r{i}": repository(repo, [node(i+1)]) for i, repo in enumerate(repos)}
        value = reader(page)
        result = github_open_prs.scan(value, repos + repos)
        self.assertEqual(value.graphql_json.call_count, 1)
        self.assertEqual(list(result), repos)
        pr = result[repos[0]][0]
        self.assertEqual((pr["user"]["login"], pr["head"]["repo"]["full_name"], pr["labels"], pr["state"]),
                         ("author", "example/fork", [{"name": "hold"}], "open"))

    def test_large_repository_sets_have_a_supported_bounded_query_path(self):
        repos = [f"example/app{i}" for i in range(101)]
        value = reader({f"r{i}": repository(repo, []) for i, repo in enumerate(repos[:100])},
                       {"r0": repository(repos[-1], [])})
        self.assertEqual(set(github_open_prs.scan(value, repos)), set(repos))
        self.assertEqual(value.graphql_json.call_count, 2)

    def test_only_overflowing_repository_is_queried_again(self):
        value = reader({"r0": repository("example/app", [node()], total=2, cursor="next"),
                        "r1": repository("example/empty", [])},
                       {"r0": repository("example/app", [node(8)], total=2)})
        result = github_open_prs.scan(value, ["example/app", "example/empty"])
        self.assertEqual([p["number"] for p in result["example/app"]], [7, 8])

    def test_incomplete_or_changing_results_never_publish_inventory(self):
        cases = [None, repository("example/wrong", []), repository("example/app", [node()], total=2),
                 repository("example/app", [node(), node()])]
        for bad in cases:
            with self.subTest(bad=bad):
                self.assertIsInstance(github_open_prs.scan(reader({"r0": bad}), ["example/app"])["example/app"], github_open_prs.InventoryUnavailable)
        for second in [repository("example/app", [node(8)], total=3),
                       repository("example/app", [node(8)], total=2, cursor="next")]:
            value = reader({"r0": repository("example/app", [node()], total=2, cursor="next")}, {"r0": second})
            self.assertIsInstance(github_open_prs.scan(value, ["example/app"])["example/app"], github_open_prs.InventoryUnavailable)
        partial = reader()
        partial.graphql_json.side_effect = None
        partial.graphql_json.return_value = github_api.ApiResult(ok=True, status=200, body={"data": {}, "errors": [{"message": "denied"}]})
        self.assertIsInstance(github_open_prs.scan(partial, ["example/app"])["example/app"], github_open_prs.InventoryUnavailable)

    def test_partial_execution_and_transport_errors_preserve_other_repositories(self):
        value = reader()
        value.graphql_json.side_effect = None
        value.graphql_json.return_value = github_api.ApiResult(ok=True, status=200, body={
            "data": {"r0": None, "r1": repository("example/good", [node()])},
            "errors": [{"path": ["r0"], "message": "not accessible"}]})
        inventory = github_open_prs.scan(value, ["example/missing", "example/good"])
        self.assertIsInstance(inventory["example/missing"], github_open_prs.InventoryUnavailable)
        self.assertEqual(len(inventory["example/good"]), 1)
        value.graphql_json.return_value = github_api.ApiResult(ok=False, status=503, body=None)
        inventory = github_open_prs.scan(value, ["example/app"])
        self.assertIsInstance(inventory["example/app"], GitHubReadError)
        self.assertEqual(inventory["example/app"].result.status, 503)

    def test_labels_paginate_before_discovery_can_admit_a_pr(self):
        pr = copy.deepcopy(node())
        pr["labels"] = connection([{"name": "first"}], total=2, cursor="label-next")
        value = reader({"r0": repository("example/app", [pr])}, {"repository": {"pullRequest": {
            "headRefOid": pr["headRefOid"], "updatedAt": pr["updatedAt"],
            "labels": connection([{"name": "hold"}], total=2)}}})
        result = github_open_prs.scan(value, ["example/app"])
        self.assertEqual(result["example/app"][0]["labels"], [{"name": "first"}, {"name": "hold"}])


if __name__ == "__main__":
    unittest.main()
