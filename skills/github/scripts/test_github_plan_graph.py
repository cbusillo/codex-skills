#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Recorded planning graph behavior and transport equivalence."""

import argparse
import copy
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch
from typing import Any, Iterator
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).parent))
import github_api
import github_plan_graph as graph
import github_read

FIXTURE = Path(__file__).parent / "fixtures/planning_graph.json"


@contextmanager
def mock_attribute(target: Any, attribute_name: str, **kwargs: Any) -> Iterator[Mock]:
    replacement = Mock(**kwargs)
    with patch.dict(vars(target), {attribute_name: replacement}):
        yield replacement


def load_plan() -> Any:
    spec = importlib.util.spec_from_file_location(
        "graph_plan_test", Path(__file__).with_name("gh-plan.py")
    )
    plan = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plan)
    return plan


def reader_for(*bodies):
    reader = Mock()
    reader.graphql_json.side_effect = [
        github_api.ApiResult(ok=True, status=200, body=copy.deepcopy(body))
        for body in bodies
    ]
    return reader


def test_recorded_graph_selection_and_request_savings():
    fixture = json.loads(FIXTURE.read_text())
    reader = reader_for(fixture)
    rows = graph.planning_graph(reader, "owner/repo", limit=20, page_size=20)
    assert len(rows) == 20
    plan = load_plan()
    rest_record = json.loads(
        FIXTURE.with_name("planning_rest_relationships.json").read_text()
    )
    rest_calls = []

    def collect(path, **_kwargs):
        rest_calls.append(path)
        number = int(path.split("/issues/")[1].split("/")[0])
        key = "sub_issues" if path.endswith("/sub_issues") else path.rsplit("/", 1)[-1]
        return "fixture-reader", rest_record[str(number)][key]

    config = plan.load_config("owner/repo")
    for plan_row in rows:
        rest = copy.deepcopy(plan_row)
        rest.pop("_graph_relationships")
        with mock_attribute(plan, "collect_paged_rest_items", side_effect=collect):
            _, rest_rel, rest_truncated = plan.read_next_issue_relationships(
                "owner/repo", plan_row["number"], rest
            )
        _, graph_rel, graph_truncated = plan.read_next_issue_relationships(
            "owner/repo", plan_row["number"], plan_row
        )
        assert (graph_rel, graph_truncated) == (rest_rel, rest_truncated)
        assert graph_rel == {
            key: [plan.compact_relationship_issue(item, key) for item in items]
            for key, items in rest_record[str(plan_row["number"])].items()
        }
        assert plan.evaluate_next_plan(
            plan_row, config=config, focus=None, relationships=graph_rel
        ) == plan.evaluate_next_plan(
            rest, config=config, focus=None, relationships=rest_rel
        )
    # Count the actual REST calls plus its inventory, compared with the batch.
    assert len(rest_calls) + 1 >= 12
    assert reader.graphql_json.call_count == 1
    # Repeated changing observations still spend one request each, not N graph reads.
    changed = copy.deepcopy(fixture)
    changed["data"]["repository"]["issues"]["nodes"][0]["title"] = "Changed plan"
    reader = reader_for(fixture, changed)
    graph.planning_graph(reader, "owner/repo", limit=20, page_size=20)
    assert (
        graph.planning_graph(reader, "owner/repo", limit=20, page_size=20)[0]["title"]
        == "Changed plan"
    )
    assert reader.graphql_json.call_count == 2


def issue_fixture():
    node = copy.deepcopy(
        json.loads(FIXTURE.read_text())["data"]["repository"]["issues"]["nodes"][0]
    )
    node["comments"] = {
        "totalCount": 2,
        "nodes": [
            {
                "databaseId": 1,
                "body": "First",
                "createdAt": "t",
                "updatedAt": "t",
                "url": "https://example/1",
                "author": {"login": "alice"},
            }
        ],
        "pageInfo": {"hasNextPage": True, "endCursor": "comments-1"},
    }
    node["labels"] = {
        "totalCount": 2,
        "nodes": [{"name": "plan"}],
        "pageInfo": {"hasNextPage": True, "endCursor": "labels-1"},
    }
    node["blockedBy"] = {
        "totalCount": 2,
        "nodes": [
            {
                "number": 1,
                "title": "Gate",
                "state": "OPEN",
                "url": "https://github.com/other/repo/issues/1",
                "repository": {"nameWithOwner": "other/repo"},
            }
        ],
        "pageInfo": {"hasNextPage": True, "endCursor": "blocked-1"},
    }
    return node


def test_full_discussion_and_all_connection_pagination():
    node = issue_fixture()
    first = {"data": {"repository": {"issue": node}}}
    more_labels = {
        "data": {
            "repository": {
                "issue": {
                    "updatedAt": node["updatedAt"],
                    "labels": {
                        "totalCount": 2,
                        "nodes": [{"name": "plan:active"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        }
    }
    more_blocked = {
        "data": {
            "repository": {
                "issue": {
                    "updatedAt": node["updatedAt"],
                    "blockedBy": {
                        "totalCount": 2,
                        "nodes": [
                            {
                                "number": 2,
                                "title": "Closed gate",
                                "state": "CLOSED",
                                "url": "https://github.com/other/repo/issues/2",
                                "repository": {"nameWithOwner": "other/repo"},
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        }
    }
    more_comments = {
        "data": {
            "repository": {
                "issue": {
                    "updatedAt": node["updatedAt"],
                    "comments": {
                        "totalCount": 2,
                        "nodes": [
                            {
                                "databaseId": 2,
                                "body": "Last",
                                "createdAt": "u",
                                "updatedAt": "u",
                                "url": "https://example/2",
                                "author": None,
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        }
    }
    reader = reader_for(first, more_labels, more_blocked, more_comments)
    rows = graph.planning_graph(reader, "owner/repo", number=node["number"])
    assert rows[0]["labels"] == [{"name": "plan"}, {"name": "plan:active"}]
    assert [c["body"] for c in rows[0]["_graph_comments"]] == ["First", "Last"]
    assert rows[0]["_graph_relationships"]["blocked_by"][1]["state"] == "closed"
    rows[0]["_graph_actor"] = "fixture-reader"
    plan = load_plan()
    with (
        mock_attribute(plan, "read_planning_graph", return_value=rows),
        mock_attribute(plan, "emit") as emit,
        mock_attribute(plan, "api_json", side_effect=AssertionError("unexpected REST")),
    ):
        plan.cmd_show(argparse.Namespace(repo="owner/repo", issue=str(node["number"]), full=True, batch_read=False))
    assert [c["body"] for c in emit.call_args.args[0]["issue"]["comments"]] == [
        "First",
        "Last",
    ]


def test_partial_and_incomplete_graph_never_become_available():
    fixture = json.loads(FIXTURE.read_text())
    for alter in [
        lambda document: document.update(errors=[{"message": "missing grant"}]),
        lambda document: document["data"].update(repository=None),
        lambda document: document["data"]["repository"]["issues"]["nodes"][0][
            "blocking"
        ].update(totalCount=999),
        lambda document: document["data"]["repository"]["issues"]["nodes"][0].pop(
            "labels"
        ),
    ]:
        body = copy.deepcopy(fixture)
        alter(body)
        try:
            graph.planning_graph(reader_for(body), "owner/repo", limit=20)
        except (github_read.GitHubReadShapeError, KeyError, TypeError):
            pass
        else:
            raise AssertionError("incomplete graph accepted")
    plan = load_plan()
    with (
        mock_attribute(
            plan.github_plan_graph,
            "planning_graph",
            side_effect=github_read.GitHubReadShapeError("partial"),
        ),
        mock_attribute(plan.github_read, "GitHubReader") as reader,
    ):
        reader.return_value.results = []
        assert plan.read_planning_graph("owner/repo") is None


def test_cancelled_graph_read_stops_instead_of_falling_back_to_rest():
    plan = load_plan()
    api = plan.github_api_core
    failure = api.FailureDetail(cause="cancelled", message="retry wait cancelled", retryable=False,
                                fallback_eligible=False, disposition="stop")
    cancelled = github_read.GitHubReadError("retry wait cancelled", diagnostics={}, result=api.ApiResult(
        ok=False, status=None, body=None, operation="github.plan.next", failure=failure))
    with (
        patch.object(plan, "EXPECTED_ACTOR", "fixture-reader"),
        patch.object(plan, "BOT_GH", Path(__file__)),
        mock_attribute(plan.github_identity, "active_auth_fallback_allowed", return_value=False),
        mock_attribute(plan.github_identity, "own_user_opted_in", return_value=False),
        mock_attribute(plan.github_plan_graph, "planning_graph", side_effect=cancelled),
        mock_attribute(plan.github_read, "GitHubReader") as reader,
        patch.dict(os.environ, {"GH_PLAN_SKIP_BOT": "0", "GH_PLAN_ALLOW_ACTIVE_FIRST": "0"}),
    ):
        reader.return_value.results = []
        try:
            plan.read_planning_graph("owner/repo")
        except plan.PlanError as exc:
            assert exc.failure is failure
        else:
            raise AssertionError("Ctrl-C during the graph read fell back to REST")


def test_inventory_pagination_and_changed_connection_fail_closed():
    fixture = json.loads(FIXTURE.read_text())
    first = copy.deepcopy(fixture)
    second = copy.deepcopy(fixture)
    a = first["data"]["repository"]["issues"]
    b = second["data"]["repository"]["issues"]
    a["nodes"] = a["nodes"][:10]
    b["nodes"] = b["nodes"][10:]
    a["totalCount"] = b["totalCount"] = 20
    a["pageInfo"] = {"hasNextPage": True, "endCursor": "next"}
    b["pageInfo"] = {"hasNextPage": False, "endCursor": None}
    assert len(graph.planning_graph(reader_for(first, second), "owner/repo")) == 20
    b["nodes"] = a["nodes"]
    try:
        graph.planning_graph(reader_for(first, second), "owner/repo")
    except github_read.GitHubReadShapeError:
        pass
    else:
        raise AssertionError("duplicate pages accepted")
    node = issue_fixture()
    changed = {"data": {"repository": {"issue": {"updatedAt": "changed"}}}}
    try:
        graph.planning_graph(
            reader_for({"data": {"repository": {"issue": node}}}, changed),
            "owner/repo",
            number=node["number"],
        )
    except github_read.GitHubReadShapeError:
        pass
    else:
        raise AssertionError("changed issue accepted")


def test_recorded_rest_show_provenance_and_fallback():
    record = json.loads(FIXTURE.with_name("planning_show.json").read_text())
    rows = graph.planning_graph(
        reader_for(record["graphql"]), "owner/repo", number=1720
    )
    rows[0]["_graph_actor"] = "fixture-reader"
    plan = load_plan()
    args = argparse.Namespace(
        repo="owner/repo", issue="1720", full=True, batch_read=True
    )
    with (
        mock_attribute(
            plan.github_identity,
            "configured_bot_logins",
            return_value={"fixture-bot[bot]"},
        ),
        mock_attribute(plan, "emit") as output,
    ):
        with mock_attribute(plan, "read_planning_graph", return_value=rows):
            plan.cmd_show(args)
        batched = output.call_args.args[0]["issue"]
        with (
            mock_attribute(plan, "read_planning_graph", return_value=None),
            mock_attribute(
                plan, "get_issue", return_value=("fixture-reader", record["rest"])
            ),
            mock_attribute(
                plan,
                "collect_paged_rest_items",
                return_value=("fixture-reader", record["comments"]),
            ),
            mock_attribute(plan, "read_issue_relationships", return_value=("fixture-reader", {
                key: [plan.compact_relationship_issue(row, key) for row in values]
                for key, values in rows[0]["_graph_relationships"].items()})),
        ):
            plan.cmd_show(args)
        assert output.call_args.args[0]["issue"] == batched
        assert batched["provenance"]["ownership"] == "automation_managed"


def test_local_next_batch_selection_matches_rest_and_falls_back():
    import test_gh_plan_next as fixtures

    recorded = json.loads(FIXTURE.read_text())
    rows = graph.planning_graph(reader_for(recorded), "owner/repo", limit=20)
    module = fixtures.load_module()
    args = argparse.Namespace(
        repo="owner/repo", milestone=None, limit=20, scan_limit=20, batch_read=True
    )
    module.BATCH_READ_CONTEXT.update({"diagnostics": {"actor": "fixture-reader"}})

    def relations(_repo: str, number: int, _issue: Any = None):
        row = next(item for item in rows if item["number"] == number)
        return (
            "",
            {
                key: [module.compact_relationship_issue(item, key) for item in items]
                for key, items in row["_graph_relationships"].items()
            },
            [],
        )

    with (
        fixtures.local_next_fixture(
            module, rows, read_relationships=module.read_next_issue_relationships
        ) as captured,
        mock_attribute(module, "read_planning_graph", return_value=rows),
    ):
        module.cmd_next(args)
        selected = captured["candidates"]
        excluded = captured["excluded"]
    rest_rows = [
        {key: value for key, value in row.items() if not key.startswith("_graph")}
        for row in rows
    ]
    with (
        fixtures.local_next_fixture(
            module, rest_rows, read_relationships=relations
        ) as captured,
        mock_attribute(module, "read_planning_graph", return_value=None),
    ):
        module.cmd_next(args)
        assert captured["candidates"] == selected
        assert captured["excluded"] == excluded


def test_real_inventory_rows_read_the_newer_wait_comment_before_selection():
    import test_gh_plan_next as fixtures

    module = fixtures.load_module()
    recorded = json.loads(FIXTURE.read_text())
    inventory = recorded["data"]["repository"]["issues"]
    node = copy.deepcopy(inventory["nodes"][0])
    clear = module.github_plan_wait.reviewed(evidence="Earlier body review found no remaining hold")
    held = module.github_plan_wait.reviewed(evidence="Client subsequently requested acceptance",
        waiting_for="Client acceptance", ends_when="Client accepts")
    node.update(body=module.github_plan_wait.marker(clear), author={"__typename": "User", "login": "owner"})
    for key in ("blockedBy", "blocking", "subIssues"):
        node[key] = {"nodes": [], "totalCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": None}}
    node["labels"] = {"nodes": [{"name": "plan"}, {"name": "plan:active"}],
        "totalCount": 2, "pageInfo": {"hasNextPage": False, "endCursor": None}}
    node.pop("comments", None)
    inventory.update(nodes=[node], totalCount=1, pageInfo={"hasNextPage": False, "endCursor": None})
    rows = graph.planning_graph(reader_for(recorded), "owner/repo")
    module.BATCH_READ_CONTEXT.update(diagnostics={"actor": "fixture-reader"})
    with fixtures.local_next_fixture(module, rows, read_relationships=module.read_next_issue_relationships) as result:
        collector = module.collect_paged_rest_items
        def discussion(path, **kwargs):
            if path.endswith("/comments"):
                return "fixture-reader", [{"user": {"login": "owner"}, "body": module.github_plan_wait.marker(held)}]
            return collector(path, **kwargs)
        with patch.object(module, "read_planning_graph", return_value=rows), \
                patch.object(module, "collect_paged_rest_items", discussion):
            module.cmd_next(argparse.Namespace(repo="owner/repo", milestone=None, limit=20, scan_limit=20, batch_read=True))
    assert not result["candidates"], result
    observed = next(row for row in result["excluded"] if row["number"] == node["number"])
    assert observed["exclusion"] == "waiting", observed
    assert observed["derived"]["waits"] == held["waits"], observed


def test_default_index_search_and_custody_consume_paged_graphql() -> None:
    plan = load_plan()
    recorded = json.loads(FIXTURE.with_name("planning_show.json").read_text())
    node = recorded["graphql"]["data"]["repository"]["issue"]

    def connection(nodes: list[dict[str, Any]], *, total: int | None = None, cursor: str | None = None) -> dict[str, Any]:
        return {
            "nodes": nodes,
            "totalCount": len(nodes) if total is None else total,
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
        }

    pull = copy.deepcopy(next(iter(json.loads(FIXTURE.with_name("pr_batch.json").read_text())["data"]["repository"].values())))
    pull.update(repository={"nameWithOwner": "owner/repo"}, milestone=None, authorAssociation="CONTRIBUTOR")
    owner = {"worker": "source", "session": "source-session", "branch": pull["headRefName"], "claimed_at": node["createdAt"]}
    old_comment = copy.deepcopy(node["comments"]["nodes"][0])
    old_comment["body"] = plan.github_plan_claim.marker(owner)
    release = {
        **old_comment,
        "databaseId": old_comment["databaseId"] + 1,
        "body": f"Released claim {old_comment['databaseId']}",
        "createdAt": "2026-10-13T00:00:00Z",
    }
    documents = [
        {"data": {"r0": {"nameWithOwner": "owner/repo", "issues": connection([node])}}},
        {"data": {"search": connection([{**node, "__typename": "Issue"}], total=2, cursor="search-next")}},
        {"data": {"search": connection([{**pull, "__typename": "PullRequest"}], total=2)}},
        {"data": {"r0": {"nameWithOwner": "owner/repo", "pullRequests": connection([pull])}}},
        {"data": {"r0": {"nameWithOwner": "owner/repo", "refs": connection([{"name": pull["headRefName"]}])}}},
        {
            "data": {
                "repository": {
                    "issueOrPullRequest": {
                        **node,
                        "__typename": "Issue",
                        "comments": connection([old_comment], total=2, cursor="history-next"),
                    }
                }
            }
        },
        {"data": {"repository": {"issue": {"updatedAt": node["updatedAt"], "comments": connection([release], total=2)}}}},
    ]
    reader = github_read.GitHubReader(gh_cmd="fake-gh", expected_actor="fixture-reader", actor="fixture-reader", strict_actor=True)
    responses = [github_api.ApiResult(ok=True, status=200, actor="fixture-reader", body=document) for document in documents]
    with (
        patch.object(github_api, "call_gh_with_retry", side_effect=responses) as requests,
        mock_attribute(plan, "next_graph_reader", return_value=reader),
        mock_attribute(plan, "api_json", return_value=("fixture-reader", {"archived": False})),
        mock_attribute(plan, "claim_inventory", wraps=plan.claim_inventory),
        mock_attribute(plan.github_plan_claim, "local_inventory", return_value={"worktrees": [], "local_branches": []}),
        mock_attribute(plan, "emit") as output,
    ):
        plan.cmd_index(argparse.Namespace(repo="owner/repo", label=None, state="open", limit=20))
        assert output.call_args.args[0]["plans"][0]["number"] == recorded["rest"]["number"]
        assert output.call_args.args[0]["inventory_complete"]
        plan.cmd_search(argparse.Namespace(repo="owner/repo", query="fixture", state="all", limit=20))
        result = output.call_args.args[0]
        assert [row["record_type"] for row in result["issues"]] == ["issue", "pull_request"]
        assert result["count"] == result["total_count"] == 2 and not result["incomplete_results"]
        _, pulls = plan.collect_paged_rest_items(
            "/repos/owner/repo/pulls", query={"state": "all"}, bucket="rest_core", step_prefix="claim"
        )
        inventory = plan.claim_inventory("owner/repo", node["number"])
        assert inventory["remote_branches"] == [pulls[0]["head"]["ref"]]
        _, comments = plan.collect_paged_rest_items(
            f"/repos/owner/repo/issues/{node['number']}/comments", query={}, bucket="rest_core", step_prefix="claim"
        )
        assert plan.github_plan_claim.discussion_evidence("", comments[:1], {**owner, "worker": "successor"})[0]
        assert not plan.github_plan_claim.discussion_evidence("", comments, {**owner, "worker": "successor"})[0]
        assert requests.call_count == len(documents)
        assert all(call.args[:2] == ("POST", "/graphql") for call in requests.call_args_list)


def test_partial_inventory_and_missing_later_history_never_clear_custody() -> None:
    plan = load_plan()
    node = issue_fixture()
    node["__typename"] = "Issue"
    failures = [
        [
            {
                "data": {
                    "r0": {
                        "nameWithOwner": "owner/repo",
                        "issues": {"nodes": [], "totalCount": 1, "pageInfo": {"hasNextPage": False}},
                    }
                }
            }
        ],
        [
            {"data": {"repository": {"issueOrPullRequest": node}}},
            {"data": {"repository": None}, "errors": [{"message": "Unavailable page"}]},
        ],
    ]
    for documents in failures:
        reader = github_read.GitHubReader(gh_cmd="fake-gh")
        responses = [github_api.ApiResult(ok=True, status=200, body=document) for document in documents]
        with (
            patch.object(github_api, "call_gh_with_retry", side_effect=responses),
            mock_attribute(plan, "next_graph_reader", return_value=reader),
        ):
            try:
                plan.collect_paged_rest_items(
                    "/repos/owner/repo/issues" if len(documents) == 1 else f"/repos/owner/repo/issues/{node['number']}/comments",
                    query={},
                    bucket="rest_core",
                    step_prefix="claim",
                    issue_only=True,
                )
            except plan.PlanError:
                continue
            raise AssertionError("Unread custody became a complete inventory")


def test_direction_blob_absence_is_distinct_from_unreadable_content() -> None:
    import test_gh_plan_direction as direction

    plan = load_plan()
    for blob, errors, expected in [
        ({"text": direction.DIRECTION, "isBinary": False, "isTruncated": False}, None, direction.DIRECTION),
        (None, None, None),
        ({"text": direction.DIRECTION, "isBinary": False, "isTruncated": True}, None, "error"),
        (None, [{"message": "Repository unavailable"}], "error"),
    ]:
        body = {"data": {"repository": {"object": blob}}, "errors": errors}
        reader = github_read.GitHubReader(gh_cmd="fake-gh")
        with (
            patch.object(github_api, "call_gh_with_retry", return_value=github_api.ApiResult(ok=True, status=200, body=body)),
            mock_attribute(plan, "next_graph_reader", return_value=reader),
        ):
            try:
                value = plan.load_direction("owner/repo")
            except plan.PlanError:
                assert expected == "error"
            else:
                assert value == expected


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
