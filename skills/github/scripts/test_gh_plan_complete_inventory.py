# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import copy
import os
import unittest
from typing import Any
from unittest.mock import Mock, patch

import github_api
import github_next_inventory as inventory
import github_next_pr
import github_next_queue as queue
import github_plan_claim as claim
import ready_handoff
import test_gh_plan_next as cli
from test_github_next_queue import evidence, item
from test_github_work_graph import connection, node


def assert_default_global_next_evaluates_every_inventoried_record() -> None:
    module = cli.load_module()
    args = module.build_parser().parse_args(["--repo", "someone/direction", "next"])
    count = (args.scan_limit or module.NEXT_DEFAULT_SCAN_LIMIT) + 1
    rows = [cli.global_issue("someone/product", number) for number in range(1, count + 1)]
    with cli.global_fixture(roots=[], nodes=rows, edges={}, discovered=rows) as (
        module,
        result,
        _,
    ):
        args = module.build_parser().parse_args(["--repo", "someone/direction", "next"])
        with patch.multiple(module.github_client, recorded_client=Mock(return_value={"status": "unavailable"})):
            module.cmd_next(args)
    assert result["discovery_context"]["evaluated"] == len(rows)
    assert not result["discovery_context"]["unevaluated_count"]


class CompleteInventoryTests(unittest.TestCase):
    def test_default_global_read_has_no_deadline_and_evaluates_the_whole_inventory(
        self,
    ) -> None:
        module = cli.load_module()
        args = module.build_parser().parse_args(["next"])
        with patch.dict(os.environ, clear=True), module.github_next_reads.NextReads(workers=1, seconds=args.time_budget) as reads:
            self.assertIsNone(reads.deadline)
        assert_default_global_next_evaluates_every_inventoried_record()

    def test_shared_filter_preserves_pr_waits_ownership_and_missing_status(
        self,
    ) -> None:
        row = item()
        row.update(record_type="pull_request", head_sha="a" * 40)
        pull: dict[str, Any] = {
            "number": row["number"],
            "html_url": row["url"].replace("issues", "pull"),
            "title": "Change",
            "state": "open",
            "body": row["discussion"]["body"],
            "user": {"login": "owner"},
            "head": {
                "sha": row["head_sha"],
                "ref": "work/source",
                "repo": {"full_name": row["repo"]},
            },
            "base": {"repo": {"full_name": row["repo"]}},
        }
        observed = {**evidence(), "pulls": [pull]}
        self.assertEqual(queue.classify(row, observed, trusted={"owner"})["state"], "available")
        record = {"worker": "source", "session": "live-source", "branch": "work/source", "claimed_at": "2026-10-29T00:00:00Z"}
        source: dict[str, Any] = {
            "number": 7,
            "body": "",
            "_graph_comments_complete": True,
            "_graph_comments": [
                {"id": 9, "user": {"login": "owner"}, "body": claim.marker(record), "created_at": record["claimed_at"]}
            ],
        }
        observed["records"] = [source]
        pull["body"] += "\nCloses #7"
        self.assertEqual(queue.classify(row, observed, trusted={"owner"})["state"], "underway")
        source["_graph_comments"].append(
            {"id": 10, "user": {"login": "owner"}, "body": "Released claim 9", "created_at": "2026-10-29T00:01:00Z"}
        )
        self.assertEqual(queue.classify(row, observed, trusted={"owner"})["state"], "available")
        source["repo"] = "owner/source"
        source["_graph_comments"].pop()
        pull["body"] = "Closes https://github.com/owner/source/issues/7"
        self.assertEqual(queue.classify(row, observed, trusted={"owner"})["state"], "underway")
        source["body"] = "## Current Status\nNext actor: agent\n" + claim.marker(record)
        source["_graph_comments"] = []
        self.assertEqual(queue.classify(row, observed, trusted={"owner"})["state"], "underway")
        observed["records"] = []
        missing = queue.classify(row, observed, trusted={"owner"})
        self.assertEqual(missing["reason"], "source_custody_unread")
        self.assertFalse(missing["observed_ownership_complete"])
        pull["body"] = ""
        original_head = pull["head"]["repo"]
        for head in ({"full_name": "outside/fork"}, {"full_name": None}):
            pull["head"]["repo"] = head
            self.assertEqual(queue.classify(row, observed, trusted={"owner"})["reason"], "pr_canonical_issue_required")
        pull["head"]["repo"] = original_head
        self.assertEqual(queue.classify(row, evidence(), trusted={"owner"})["state"], "review")
        for actor, state in [
            ("person:owner", "waiting"),
            ("event:watch", "waiting"),
            ("agent:integrator", "available"),
            ("", "needs_status"),
        ]:
            changed = item(actor=actor)
            changed.update(record_type="pull_request")
            self.assertEqual(queue.classify(changed, observed, trusted={"owner"})["state"], state)
        pull["draft"] = True
        self.assertEqual(queue.classify(row, observed, trusted={"owner"})["state"], "waiting")
        pull.pop("draft")
        row["discussion"]["comments"] = [
            {
                "id": 1,
                "author": "owner",
                "created_at": "2026-10-29T01:00:00Z",
                "body": ready_handoff.handoff_body(row["head_sha"]),
            }
        ]
        self.assertEqual(queue.classify(row, observed, trusted={"owner"})["state"], "underway")
        row["discussion"]["comments"][0]["body"] = ready_handoff.handoff_body(row["head_sha"], waiting_for_client=True)
        with patch.object(github_next_pr, "read_acceptance", return_value=False):
            self.assertEqual(queue.classify(row, observed, trusted={"owner"})["state"], "waiting")
        row["discussion"]["complete"] = False
        self.assertEqual(queue.classify(row, observed, trusted={"owner"})["state"], "review")

    def test_global_pr_artifacts_never_hide_followthrough_or_missing_status(self) -> None:
        for actor, accepted, bucket in [("agent", False, "available_candidates"), ("", False, "needs_status"),
                                         (None, True, "available_candidates")]:
            with cli.global_fixture([], [], {}) as (module, result, _reads):
                raw = cli.global_issue("someone/pr-only", 11, labels=["live-breakage"])
                raw.update(
                    user={"login": "someone"},
                    body=(f"## Current Status\nNext actor: {actor}\n" if actor is not None else "## Current Status\n")
                    + module.github_plan_wait.marker(module.github_plan_wait.reviewed(evidence="Full fixture reviewed")),
                    pull_request={"url": "https://github.com/someone/pr-only/pull/11"},
                    html_url="https://github.com/someone/pr-only/pull/11",
                    _graph_comments=[{
                        "id": 1, "user": {"login": "someone"}, "created_at": "2026-10-29T01:00:00Z",
                        "body": ready_handoff.handoff_body("a" * 40, waiting_for_client=True)}] if accepted else [],
                    head={
                        "sha": "a" * 40,
                        "ref": "work/source",
                        "repo": {"full_name": "someone/pr-only"},
                    },
                    base={"repo": {"full_name": "someone/pr-only"}},
                )

                def discover(*_args: object, **_kwargs: object) -> tuple[list[dict], dict]:
                    module.NEXT_READS.inventory = {"someone/pr-only": [raw], "someone/direction": []}
                    return [raw], {
                        "complete": True,
                        "repositories": [
                            {"repo": "someone/pr-only", "direction": None},
                            {"repo": "someone/direction", "complete": True, "direction": None},
                        ],
                    }

                with patch.multiple(
                    module,
                    discover_direction_work=discover,
                    next_dependabot_work=module.real_next_dependabot_work,
                    annotate_next_queue=module.real_annotate_next_queue,
                    load_direction=lambda *_: "# Direction\n## Order\n4. Own projects: pr-only.\n## Milestones\n",
                    read_queue_ownership=lambda *_: {**evidence(), "pulls": [raw], "inventory": {
                        "local_branches": [raw["head"]["ref"]], "remote_branches": [],
                        "worktrees": [{"branch": raw["head"]["ref"], "path": "/workspace/old-source"}]}},
                    wait_authors=lambda *_: {"someone"},
                ), patch.object(github_next_pr, "read_acceptance", return_value=accepted) as acceptance:
                    module.cmd_next(module.build_parser().parse_args(["--repo", "someone/direction", "next"]))
                self.assertEqual(
                    [row["number"] for row in result[bucket]],
                    [raw["number"]],
                )
                self.assertEqual(result[bucket][0]["record_type"], "pull_request")
                self.assertTrue(result[bucket][0]["queue"]["claim_required"])
                self.assertEqual(acceptance.call_count, int(accepted))
                self.assertEqual(result["pr_followthrough"][0]["queue"], result[bucket][0]["queue"])

    def test_partial_source_inventory_keeps_ranked_work_and_reports_the_gap(self) -> None:
        root = cli.track("someone/direction", 1, "First")
        leaf = cli.global_issue("someone/product", 2)
        leaf["parent"] = {"repo": root["repo"], "number": root["number"], "state": "open", "html_url": root["html_url"]}
        with cli.global_fixture([root], [leaf], {(root["repo"], 1): cli.relationships(sub_issues=[leaf])}) as (module, result, _):
            original_wait = module.next_wait_context

            def check_parent_evidence(issues: list[dict], **kwargs: Any) -> dict:
                parent = next(row for row in issues if row["repo"] == root["repo"] and row["number"] == root["number"])
                self.assertEqual(parent["body"], root["body"])
                self.assertTrue(parent.get("labels"))
                return original_wait(issues, **kwargs)

            original = module.collect_paged_rest_items

            def collect(path: str, **kwargs: Any) -> Any:
                if path == "/installation/repositories":
                    return "bot", [{"full_name": name} for name in ("someone/direction", "someone/product", "someone/unavailable")]
                return original(path, **kwargs)

            def read_repository(_reader: Any, repo: str) -> list[dict]:
                if repo == "someone/unavailable":
                    raise module.github_read.GitHubReadShapeError("Pagination changed")
                return [root] if repo.endswith("direction") else [leaf]

            with (
                patch.multiple(
                    module,
                    discover_direction_work=module.real_discover_direction_work,
                    next_wait_context=check_parent_evidence,
                    next_graph_reader=lambda *_: Mock(results=[], actor="bot"),
                    collect_paged_rest_items=collect,
                ),
                patch.multiple(module.github_identity, github_app_config=dict),
                patch.multiple(module.github_next_inventory, read_repository=read_repository),
            ):
                module.cmd_next(module.build_parser().parse_args(["--repo", "someone/direction", "next"]))
            self.assertTrue(result["candidates"])
            self.assertFalse(result["discovery_context"]["complete"])
            self.assertFalse(result["candidate_coverage"]["complete"])
            self.assertTrue(any(row.get("error") for row in result["discovery_context"]["repositories"]))

    def test_milestone_scope_does_not_read_the_unrelated_portfolio_or_include_its_prs(self) -> None:
        root = cli.track("someone/direction", 1, "First")
        unrelated = cli.global_issue("someone/other", 11)
        unrelated.update(pull_request={"url": "pull"}, _graph_comments=[])
        with cli.global_fixture([root], [], {}, discovered=[unrelated]) as (module, result, _):
            discovery = Mock(side_effect=AssertionError("Unrelated portfolio must not be read"))
            with (
                patch.multiple(module, discover_direction_work=discovery),
                patch.multiple(
                    module.github_milestone_core, show_milestone=lambda *_args, **_kwargs: {"milestone": root["milestone"]}
                ),
            ):
                module.cmd_next(cli.next_args(milestone="First"))
            self.assertFalse(discovery.called)
            self.assertFalse(any(row.get("record_type") == "pull_request" for row in result["candidates"]))

    def test_rest_inventory_completes_source_discussions_and_disabled_tracker_prs(self) -> None:
        module = cli.load_module()
        raw = cli.issue(7)
        raw.update(body="", user={"login": "owner"})
        pull = {
            "repo": "owner/repo",
            "number": 8,
            "state": "open",
            "html_url": "https://github.com/owner/repo/pull/8",
            "title": "Change",
            "body": "Closes #7",
            "head": {"ref": "work/source"},
        }
        calls = []

        def collect(path: str, **_kwargs: Any) -> Any:
            calls.append(path)
            if path == "/installation/repositories":
                return "bot", [
                    {"full_name": "owner/repo"},
                    {"full_name": "owner/pr-only", "has_issues": False},
                    {"full_name": "other/repo"},
                    {"full_name": "owner/archive", "archived": True},
                ]
            if path.endswith("/comments"):
                return "bot", [{"id": 1, "body": "source status", "user": {"login": "owner"}}]
            if path == "/repos/owner/repo/issues":
                return "bot", [raw, {**pull, "pull_request": {"url": pull["html_url"]}}]
            if path.endswith("/pulls"):
                return "bot", [pull]
            raise AssertionError(path)

        with (
            patch.multiple(module, collect_paged_rest_items=collect, load_direction=lambda *_: cli.DIRECTION),
            patch.multiple(module.github_identity, github_app_config=dict),
        ):
            rows, coverage = module.real_discover_direction_work("owner/direction", cli.next_args())
        self.assertTrue(coverage["complete"])
        self.assertTrue(all(row["_graph_comments_complete"] for row in rows))
        self.assertFalse(any(path == "/repos/owner/pr-only/issues" for path in calls))
        self.assertEqual(next(row for row in rows if row["number"] == raw["number"])["_graph_comments"][0]["id"], 1)
        self.assertEqual({row.get("exclusion") for row in coverage["repositories"]}, {None, "other_owner", "archived_or_disabled"})

    def test_paged_issue_and_pr_inventory_completes_nested_connections(self) -> None:
        first = node(1)
        first["assignees"] = connection([])
        first["parent"] = None
        first["comments"] = connection([], total=1)
        first["comments"]["nodes"] = [
            {
                "databaseId": 1,
                "body": "status",
                "author": {"login": "owner"},
                "createdAt": first["createdAt"],
                "updatedAt": first["updatedAt"],
                "url": "comment",
            }
        ]
        first["comments"]["totalCount"] = 2
        first["comments"]["pageInfo"] = {
            "hasNextPage": True,
            "endCursor": "comment-page",
        }
        second = copy.deepcopy(first)
        second.update(
            number=2,
            url="https://github.com/owner/repo/issues/2",
            comments=connection([]),
        )
        pull = {
            **copy.deepcopy(second),
            "number": 3,
            "url": "https://github.com/owner/repo/pull/3",
            "isDraft": False,
            "headRefOid": "a" * 40,
            "headRefName": "work/source",
            "baseRefName": "main",
            "headRepository": {"nameWithOwner": "owner/repo"},
            "baseRepository": {"nameWithOwner": "owner/repo"},
        }
        page = connection([first], total=2)
        page["pageInfo"] = {"hasNextPage": True, "endCursor": "issue-page"}
        reader = Mock(actor="bot")
        reader.graphql_json.side_effect = [
            github_api.ApiResult(
                ok=True,
                status=200,
                body={
                    "data": {
                        "repository": {
                            "issues": page,
                            "pullRequests": connection([pull]),
                        }
                    }
                },
            ),
            github_api.ApiResult(
                ok=True,
                status=200,
                body={
                    "data": {
                        "repository": {
                            "issue": {
                                "updatedAt": first["updatedAt"],
                                "comments": connection(
                                    [
                                        {
                                            **first["comments"]["nodes"][0],
                                            "databaseId": 2,
                                        }
                                    ],
                                    total=2,
                                ),
                            }
                        }
                    }
                },
            ),
            github_api.ApiResult(
                ok=True,
                status=200,
                body={"data": {"repository": {"issues": connection([second], total=2)}}},
            ),
        ]
        responses = list(reader.graphql_json.side_effect)
        responses[1].body["data"]["repository"]["issue"]["comments"]["pageInfo"] = {
            "hasNextPage": False,
            "endCursor": None,
        }
        responses[2].body["data"]["repository"]["issues"]["pageInfo"] = {
            "hasNextPage": False,
            "endCursor": None,
        }

        def respond(query: str, _variables: dict, **_kwargs: Any) -> github_api.ApiResult:
            response = (
                responses[1] if "issue(number:1)" in query else responses[2] if 'after:"issue-page"' in query else responses[0]
            )
            return copy.deepcopy(response)

        reader.graphql_json.side_effect = respond
        rows = inventory.read_repository(reader, "owner/repo")
        self.assertEqual([row["number"] for row in rows], [1, 2, 3])
        self.assertEqual(len(rows[0]["_graph_comments"]), 2)
        self.assertEqual(rows[-1]["head"]["sha"], pull["headRefOid"])

    def test_failed_or_incomplete_inventory_is_never_complete(self) -> None:
        first = node(1)
        first.update(assignees=connection([]), parent=None)
        for payload in [
            {"errors": [{"message": "unavailable"}]},
            {
                "data": {
                    "repository": {
                        "issues": connection([first], total=2),
                        "pullRequests": connection([]),
                    }
                }
            },
        ]:
            reader = Mock(actor="bot")
            reader.graphql_json.return_value = github_api.ApiResult(ok=True, status=200, body=payload)
            with self.assertRaises(inventory.github_read.GitHubReadShapeError):
                inventory.read_repository(reader, "owner/repo")


if __name__ == "__main__":
    unittest.main()
