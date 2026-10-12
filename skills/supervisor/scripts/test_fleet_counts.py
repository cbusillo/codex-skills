#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Replay recorded GraphQL counts and exercise incomplete snapshot failures offline."""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import fleet_counts
from github_api import ApiResult

FIXTURE = Path(__file__).with_name("fixtures") / "fleet-counts.json"


class FleetCountsTests(unittest.TestCase):
    def setUp(self):
        self.pages = json.loads(FIXTURE.read_text())
        self.reader = Mock()
        self.reader.diagnostics.return_value = {}

    def replay(self, pages=None, **kwargs):
        self.reader.graphql_json.side_effect = [ApiResult(ok=True, status=200, body=page)
            for page in (pages if pages is not None else self.pages)]
        return fleet_counts.fleet_counts(self.reader, "fixture-owner", **kwargs)

    def test_recorded_pages_count_issues_and_prs_separately(self):
        data = self.replay(page_size=25)
        nodes = [node for page in self.pages
                 for node in page["data"]["repositoryOwner"]["repositories"]["nodes"]]
        self.assertEqual(data["totals"], {"repositories": len(nodes),
            "open_issues": sum(node["issues"]["totalCount"] for node in nodes),
            "open_prs": sum(node["pullRequests"]["totalCount"] for node in nodes)})
        self.assertEqual(data["repositories"], [{"repository": node["nameWithOwner"],
            "open_issues": node["issues"]["totalCount"],
            "open_prs": node["pullRequests"]["totalCount"]} for node in nodes])
        calls = self.reader.graphql_json.call_args_list
        self.assertEqual(len(calls), len(self.pages))
        self.assertIsNone(calls[0].args[1]["cursor"])
        for previous, call in zip(self.pages, calls[1:]):
            self.assertEqual(call.args[1]["cursor"],
                previous["data"]["repositoryOwner"]["repositories"]["pageInfo"]["endCursor"])

    def test_failure_after_first_page_has_no_totals(self):
        self.reader.graphql_json.side_effect = [ApiResult(ok=True, status=200, body=self.pages[0]),
            ApiResult(ok=False, status=403, body=None)]
        with patch.object(fleet_counts, "GitHubReader", return_value=self.reader), \
             patch.object(fleet_counts.sys, "argv", ["fleet_counts.py", "--owner", "fixture-owner"]), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(fleet_counts.main(), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertNotIn("totals", result)
        self.assertNotIn("repositories", result)

    def test_partial_graphql_errors_and_bad_repository_counts_refuse(self):
        bad_pages = []
        partial = copy.deepcopy(self.pages[0]); partial["errors"] = [{"message": "permission denied"}]
        bad_pages.append(partial)
        for field, value in (("isArchived", True), ("nameWithOwner", "another-owner/repo"),
                             ("issues", {"totalCount": None}), ("pullRequests", {"totalCount": True})):
            page = copy.deepcopy(self.pages[0])
            page["data"]["repositoryOwner"]["repositories"]["nodes"][0][field] = value
            bad_pages.append(page)
        bad_pages.append({"data": {"repositoryOwner": None}})
        for page in bad_pages:
            with self.subTest(page=page), self.assertRaises((ValueError, TypeError)):
                self.replay([page])

    def test_incomplete_duplicate_and_nonadvancing_inventory_refuse(self):
        for fault in ("total", "duplicate", "cursor", "empty", "truncated"):
            pages = copy.deepcopy(self.pages)
            first = pages[0]["data"]["repositoryOwner"]["repositories"]
            last = pages[-1]["data"]["repositoryOwner"]["repositories"]
            if fault == "total": last["totalCount"] += 1
            if fault == "duplicate": last["nodes"][0] = first["nodes"][0]
            if fault == "cursor":
                last["pageInfo"] = first["pageInfo"].copy()
            if fault == "empty": first["nodes"] = []
            if fault == "truncated": last["nodes"].pop()
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                self.replay(pages)
        with self.assertRaises(ValueError):
            self.replay(max_pages=1)

    def test_empty_fleet_is_valid(self):
        page = {"data": {"repositoryOwner": {"repositories": {"totalCount": 0,
            "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
        self.assertEqual(self.replay([page])["totals"],
                         {"repositories": 0, "open_issues": 0, "open_prs": 0})


if __name__ == "__main__":
    unittest.main()
