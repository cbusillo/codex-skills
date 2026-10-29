# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import copy
import unittest
from typing import Any
from unittest.mock import Mock, patch

import github_api
import github_next_inventory as inventory
import github_next_queue as queue
import github_plan_claim as claim
import ready_handoff
import test_gh_plan_next as cli
from test_github_next_queue import evidence, item
from test_github_work_graph import connection, node


def test_default_global_next_evaluates_every_inventoried_record() -> None:
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
        with module.github_next_reads.NextReads(workers=1, seconds=args.time_budget) as reads:
            self.assertIsNone(reads.deadline)
        test_default_global_next_evaluates_every_inventoried_record()

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
        self.assertEqual(queue.classify(row, evidence(), trusted={"owner"})["state"], "review")
        for actor, state in [
            ("person:owner", "waiting"),
            ("event:watch", "waiting"),
            ("agent:integrator", "underway"),
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
        self.assertEqual(queue.classify(row, observed, trusted={"owner"})["state"], "waiting")
        row["discussion"]["complete"] = False
        self.assertEqual(queue.classify(row, observed, trusted={"owner"})["state"], "review")

    def test_global_pr_is_available_from_the_same_complete_inventory(self) -> None:
        with cli.global_fixture([], [], {}) as (module, result, _reads):
            raw = cli.global_issue("someone/pr-only", 11, labels=[])
            raw.update(
                user={"login": "someone"},
                body="## Current Status\nNext actor: agent\n"
                + module.github_plan_wait.marker(module.github_plan_wait.reviewed(evidence="Full fixture reviewed")),
                pull_request={"url": "https://github.com/someone/pr-only/pull/11"},
                html_url="https://github.com/someone/pr-only/pull/11",
                _graph_comments=[],
                head={
                    "sha": "a" * 40,
                    "ref": "work/source",
                    "repo": {"full_name": "someone/pr-only"},
                },
                base={"repo": {"full_name": "someone/pr-only"}},
            )

            def discover(*_args: object, **_kwargs: object) -> tuple[list[dict], dict]:
                module.NEXT_READS.inventory = {"someone/pr-only": [raw]}
                return [raw], {
                    "complete": True,
                    "repositories": [{"repo": "someone/pr-only", "direction": None}],
                }

            with patch.multiple(
                module,
                discover_direction_work=discover,
                annotate_next_queue=module.real_annotate_next_queue,
                load_direction=lambda *_: "# Direction\n## Order\n4. Own projects: pr-only.\n## Milestones\n",
                read_queue_ownership=lambda *_: {**evidence(), "pulls": [raw]},
                wait_authors=lambda *_: {"someone"},
            ):
                module.cmd_next(module.build_parser().parse_args(["--repo", "someone/direction", "next"]))
            self.assertEqual(
                [row["number"] for row in result["available_candidates"]],
                [raw["number"]],
            )
            self.assertEqual(result["available_candidates"][0]["record_type"], "pull_request")
            self.assertTrue(result["available_candidates"][0]["queue"]["claim_required"])

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
        reader.graphql_json.side_effect = responses
        rows = inventory.read_repository(reader, "owner/repo")
        self.assertEqual([row["number"] for row in rows], [1, 2, 3])
        self.assertEqual(len(rows[0]["_graph_comments"]), 2)
        self.assertEqual(rows[-1]["head"]["sha"], pull["headRefOid"])
        self.assertEqual(reader.graphql_json.call_count, 3)

    def test_failed_or_changing_inventory_is_never_complete(self) -> None:
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
            with self.assertRaises((inventory.github_read.GitHubReadShapeError, KeyError)):
                inventory.read_repository(reader, "owner/repo")


if __name__ == "__main__":
    unittest.main()
