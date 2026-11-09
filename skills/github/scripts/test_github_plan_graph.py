#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Recorded planning graph behavior and transport equivalence."""

import argparse
import copy
import importlib.util
import json
import os
import re
import sys
import tempfile
from urllib.parse import urlencode
from dataclasses import replace
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


def transport_for(documents: list[dict[str, Any]]) -> Any:
    replies = iter(documents)

    def respond(_method: str, _path: str, payload: dict[str, Any], **_kwargs: Any) -> github_api.ApiResult:
        body = copy.deepcopy(next(replies))
        data = body.get("data") or {}
        if "r0" in data:
            alias = re.search(r"(\w+)\s*:\s*repository\b", payload["query"])
            assert alias is not None
            data[alias[1]] = data.pop("r0")
        return github_api.ApiResult(ok=True, status=200, actor="fixture-reader", body=body)

    return respond


def search_node(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    repo = item.get("repository_url", "").removeprefix("https://api.github.com/repos/")
    return {
        "__typename": "PullRequest" if "pull_request" in item else "Issue",
        "number": item.get("number"), "databaseId": item.get("id", item.get("number")),
        "title": item.get("title", ""), "body": item.get("body", ""), "state": item.get("state", "open").upper(),
        "stateReason": None, "createdAt": item.get("created_at", "2026-10-01T00:00:00Z"),
        "updatedAt": item.get("updated_at", "2026-10-01T00:00:00Z"), "closedAt": None,
        "url": item.get("html_url", ""), "repository": {"nameWithOwner": repo},
        "author": {"login": item.get("user", {}).get("login", repo.split("/")[0]), "__typename": "User"},
        "authorAssociation": "OWNER", "milestone": None,
        "labels": {"nodes": item.get("labels", []), "totalCount": len(item.get("labels", [])),
                   "pageInfo": {"hasNextPage": False, "endCursor": None}},
        "headRefOid": "a" * 40, "headRefName": "work/example", "baseRefName": "main",
        "headRepository": {"nameWithOwner": repo}, "baseRepository": {"nameWithOwner": repo},
        "isDraft": False, "merged": False, "mergedAt": None,
    }


def fixture_search_query(reader: Any, document: str, **_kwargs: Any) -> dict[str, Any]:
    query_match = re.search(r'search\(query:("(?:[^"\\]|\\.)*")', document)
    size_match = re.search(r'first:(\d+)', document)
    cursor_match = re.search(r'after:(null|"[^"]*")', document)
    assert query_match is not None and size_match is not None and cursor_match is not None
    query = json.loads(query_match[1])
    size = int(size_match[1])
    after = json.loads(cursor_match[1])
    page = int(after or 1)
    response = reader.request("GET", "/search/issues?" + urlencode({
        "q": query, "per_page": size, "page": page, "sort": "created", "order": "desc",
    }), bucket="search")
    payload = response.body
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise github_read.GitHubReadShapeError("invalid search page")
    total = payload.get("total_count")
    return {"search": {"nodes": [search_node(row) for row in payload["items"]], "totalCount": total,
                       "pageInfo": {"hasNextPage": len(payload["items"]) == size and page * size < (total or 0),
                                    "endCursor": str(page + 1)}}}


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
        with mock_attribute(plan, "collect_paged_rest_items", side_effect=collect), mock_attribute(plan, "next_graph_reader", return_value=None):
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
            mock_attribute(plan, "next_graph_reader", return_value=None),
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
        {"data": {"search": connection([{**node, "__typename": "Issue"}], total=3, cursor="search-next")}},
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
    with (
        patch.object(github_api, "call_gh_with_retry", side_effect=transport_for(documents)),
        mock_attribute(plan, "next_graph_reader", return_value=reader),
        mock_attribute(plan, "api_json", return_value=("fixture-reader", {"archived": False})),
        mock_attribute(plan, "get_issue", return_value=("fixture-reader", copy.deepcopy(recorded["rest"]))),
        mock_attribute(plan.github_plan_claim, "local_inventory", return_value={"worktrees": [], "local_branches": []}),
        mock_attribute(plan, "emit") as output,
    ):
        plan.cmd_index(argparse.Namespace(repo="owner/repo", label=None, state="open", limit=20))
        assert output.call_args.args[0]["plans"][0]["number"] == recorded["rest"]["number"]
        assert output.call_args.args[0]["inventory_complete"]
        plan.cmd_search(argparse.Namespace(repo="owner/repo", query="fixture", state="all", limit=20))
        result = output.call_args.args[0]
        assert [row["record_type"] for row in result["issues"]] == ["issue", "pull_request"]
        assert result["count"] == 2 and result["total_count"] == 3 and result["incomplete_results"]
        _, pulls = plan.read_planning_inventory("owner/repo", kind="pullRequest", state="all")
        inventory = plan.claim_inventory("owner/repo", node["number"])
        assert inventory["remote_branches"] == [pulls[0]["head"]["ref"]]
        _, _, comments, _ = plan.claim_snapshot(str(node["number"]), "owner/repo")
        assert plan.github_plan_claim.discussion_evidence("", comments[:1], {**owner, "worker": "successor"})[0]
        assert not plan.github_plan_claim.discussion_evidence("", comments, {**owner, "worker": "successor"})[0]


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
                if len(documents) == 1:
                    plan.read_planning_inventory("owner/repo")
                else:
                    plan.read_planning_connection("owner/repo", node["number"], "comments")
            except plan.PlanError as error:
                assert error.failure is not None
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
            except plan.PlanError as error:
                assert expected == "error" and error.failure is not None
            else:
                assert value == expected


def test_index_keeps_label_intersection_truncation_and_archive_scope() -> None:
    plan = load_plan()
    source = json.loads(FIXTURE.with_name("planning_show.json").read_text())["graphql"]["data"]["repository"]["issue"]
    rows = []
    for number, labels in [(100, ["plan"]), (101, ["plan", "customer"]), (102, ["plan", "customer"])]:
        row = copy.deepcopy(source)
        row.update(
            number=number,
            labels={"nodes": [{"name": label} for label in labels], "totalCount": len(labels), "pageInfo": {"hasNextPage": False}},
        )
        rows.append(row)
    for archived in (False, True):
        documents = [
            {
                "data": {
                    "r0": {
                        "nameWithOwner": "owner/repo",
                        "isArchived": archived,
                        "issues": {"nodes": rows[:1], "totalCount": 3, "pageInfo": {"hasNextPage": True, "endCursor": "next"}},
                    }
                }
            },
            {
                "data": {
                    "r0": {
                        "nameWithOwner": "owner/repo",
                        "isArchived": archived,
                        "issues": {"nodes": rows[1:], "totalCount": 3, "pageInfo": {"hasNextPage": False}},
                    }
                }
            },
        ]
        reader = github_read.GitHubReader(gh_cmd="fake-gh", actor="fixture-reader", expected_actor="fixture-reader")
        with (
            patch.object(github_api, "call_gh_with_retry", side_effect=transport_for(documents)),
            mock_attribute(plan, "next_graph_reader", return_value=reader),
            mock_attribute(plan, "emit") as output,
        ):
            plan.cmd_index(
                plan.build_parser().parse_args(["--repo", "owner/repo", "index", "--label", "PLAN,customer", "--limit", "1"])
            )
        result = output.call_args.args[0]
        assert [row["number"] for row in result["plans"]] == ([] if archived else [101])
        assert result["truncated"] is not archived
        assert result["inventory_complete"] is archived


def test_global_queue_degrades_deadlines_and_hidden_graphql_sources() -> None:
    plan = load_plan()
    for hidden in (False, True):
        reader = github_read.GitHubReader(gh_cmd="fake-gh", actor="fixture-reader", expected_actor="fixture-reader")
        result = github_api.ApiResult(
            ok=True, status=200, body={"errors": [{"type": "NOT_FOUND", "path": ["r0"], "message": "Not found"}]}
        )
        with (
            plan.github_next_reads.NextReads(workers=1, seconds=0) as reads,
            mock_attribute(plan, "next_graph_reader", return_value=reader),
            patch.dict(vars(plan), {"NEXT_READS": reads}),
            mock_attribute(
                reader,
                "graphql_json",
                **({"return_value": result} if hidden else {"side_effect": plan.github_next_reads.ReadDeadline("Budget exhausted")}),
            ),
        ):
            observed = plan.read_queue_ownership("owner/repo")
            assert not observed["complete"] and observed["coverage"]["error"]
            assert bool(reads.budget_stops) is not hidden


def test_concurrent_inventory_changes_remain_retryable() -> None:
    plan = load_plan()
    node = json.loads(FIXTURE.with_name("planning_show.json").read_text())["graphql"]["data"]["repository"]["issue"]
    documents = [
        {
            "data": {
                "r0": {
                    "nameWithOwner": "owner/repo",
                    "issues": {"nodes": [node], "totalCount": 2, "pageInfo": {"hasNextPage": True, "endCursor": "next"}},
                }
            }
        },
        {
            "data": {
                "r0": {"nameWithOwner": "owner/repo", "issues": {"nodes": [], "totalCount": 3, "pageInfo": {"hasNextPage": False}}}
            }
        },
    ]
    reader = github_read.GitHubReader(gh_cmd="fake-gh", actor="fixture-reader", expected_actor="fixture-reader")
    with (
        patch.object(github_api, "call_gh_with_retry", side_effect=transport_for(documents)),
        mock_attribute(plan, "next_graph_reader", return_value=reader),
    ):
        try:
            plan.read_planning_inventory("owner/repo")
        except plan.PlanError as error:
            assert error.failure is not None and error.failure.retryable
        else:
            raise AssertionError("Changing inventory became complete")


def test_global_ownership_and_comment_reads_reuse_complete_observations() -> None:
    plan = load_plan()
    recorded = json.loads(FIXTURE.with_name("planning_show.json").read_text())
    row = graph.planning_graph(reader_for(recorded["graphql"]), "owner/repo", number=recorded["rest"]["number"])[0]
    row["_graph_comments_complete"] = True
    pull = {"number": 7, "pull_request": {"url": "https://github.com/owner/repo/pull/7"}, "head": {"ref": "work/source"}}
    reader = plan.PlanningGraphReader(gh_cmd="fake-gh", actor="fixture-reader", expected_actor="fixture-reader")
    documents = [
        {
            "data": {
                "r0": {
                    "nameWithOwner": "owner/repo",
                    "refs": {"nodes": [{"name": "work/source"}], "totalCount": 1, "pageInfo": {"hasNextPage": False}},
                }
            }
        }
    ]
    with (
        plan.github_next_reads.NextReads(workers=1, seconds=0) as reads,
        patch.dict(vars(plan), {"NEXT_READS": reads}),
        mock_attribute(plan, "next_graph_reader", return_value=reader),
        patch.object(github_api, "call_gh_with_retry", side_effect=transport_for(documents)) as requests,
        patch.object(Path, "is_dir", return_value=False),
    ):
        reads.inventory["owner/repo"] = [pull]
        plan.github_work_graph.publish_issue_pages(reads, row, "fixture-reader")
        first = plan.read_queue_ownership("owner/repo")
        count = requests.call_count
        second = plan.read_queue_ownership("owner/repo")
        _, comments = plan.read_planning_connection("owner/repo", row["number"], "comments")
        reads.publish(plan.github_work_graph.issue_path("owner/repo", row["number"]), ("fixture-reader", {**row, "parent": None}))
        assert plan.read_next_parent("owner/repo", row["number"]) is None
        assert requests.call_count == count
        assert reads.requests == requests.call_count
        assert first["complete"] and second["complete"] and first["pulls"] == second["pulls"] == [pull]
        assert comments == row["_graph_comments"]


def test_index_recovers_graphql_quota_through_the_same_actor_rest_reader() -> None:
    plan = load_plan()
    record = json.loads(FIXTURE.with_name("planning_show.json").read_text())["rest"]
    reader = github_read.GitHubReader(gh_cmd="fake-gh", actor="fixture-reader", expected_actor="fixture-reader")
    result = github_api.ApiResult(
        ok=False,
        status=200,
        body=None,
        bucket="graphql",
        actor="fixture-reader",
        failure=github_api.FailureDetail(
            cause="graphql_primary_rate_limited",
            message="Quota unavailable",
            retryable=True,
            fallback_eligible=False,
            disposition="retry",
        ),
    )
    with (
        patch.object(github_api, "call_gh_with_retry", return_value=result),
        mock_attribute(plan, "next_graph_reader", return_value=reader),
        mock_attribute(plan, "api_json", side_effect=[("fixture-reader", [record]), ("fixture-reader", {"archived": False})]),
        mock_attribute(plan, "emit") as output,
    ):
        plan.cmd_index(plan.build_parser().parse_args(["--repo", "owner/repo", "index"]))
    assert output.call_args.args[0]["plans"][0]["number"] == record["number"]
    assert output.call_args.args[0]["inventory_complete"]


def test_recorded_cooldown_recovers_in_global_next_and_direction_ranking() -> None:
    plan = load_plan()
    record = json.loads(FIXTURE.with_name("planning_show.json").read_text())["rest"]
    with tempfile.TemporaryDirectory() as temporary:
        policy = replace(github_api.optional_batch_retry_policy(), state_dir=Path(temporary))
        store = github_api.SharedCooldownStore(policy.state_dir)
        store.publish(
            github_api._cooldown_key("github.com", "fixture-reader", "graphql"),
            ready_at=2000,
            cause="graphql_primary_rate_limited",
            now=1000,
            policy=policy,
        )
        remote = Mock(side_effect=AssertionError("The recorded cooldown must suppress the remote attempt"))
        cooldown = github_api.run_with_retry(
            remote,
            operation="github.plan.next",
            is_write=False,
            actor="fixture-reader",
            expected_actor="fixture-reader",
            bucket="graphql",
            deadline_at=1030,
            retry_policy=policy,
            retry_runtime=github_api.RetryRuntime(now=lambda: 1000, sleep=Mock()),
        )
        reader = plan.PlanningGraphReader(gh_cmd="fake-gh", actor="fixture-reader", expected_actor="fixture-reader")
        with (
            plan.github_next_reads.NextReads(workers=1, seconds=0) as reads,
            patch.dict(vars(plan), {"NEXT_READS": reads}),
            mock_attribute(plan, "next_graph_reader", return_value=reader),
            patch.object(github_api, "call_gh_with_retry", return_value=cooldown),
            mock_attribute(
                plan, "api_json", side_effect=[("fixture-reader", [record]), ("fixture-reader", {"content": "IyBEaXJlY3Rpb24K"})]
            ),
        ):
            _, rows = plan.read_planning_inventory("owner/repo")
            assert rows == [record]
            assert plan.load_direction("owner/repo").startswith("# Direction")
            assert not reads.budget_stops
        remote.assert_not_called()


def test_deleted_fork_remains_foreign_custody_instead_of_crashing() -> None:
    plan = load_plan()
    pull = copy.deepcopy(next(iter(json.loads(FIXTURE.with_name("pr_batch.json").read_text())["data"]["repository"].values())))
    pull.update(
        state="OPEN",
        merged=False,
        headRepository=None,
        repository={"nameWithOwner": "owner/repo"},
        milestone=None,
        authorAssociation="CONTRIBUTOR",
    )
    row = graph.rest_record({**pull, "labels": [{"name": "plan"}]}, kind="pullRequest")
    claim = {"branch": row["head"]["ref"]}
    empty = {"local_branches": [], "remote_branches": [], "worktrees": []}
    conflicts = plan.github_plan_claim.artifact_evidence(
        empty, [row], 9999, claim, own_record=True, repo="owner/repo", retained_repo="owner/repo"
    )
    assert any(item["source"] == "open_pr" for item in conflicts)
    try:
        plan.github_plan_claim.artifact_evidence(
            empty, [row], row["number"], claim, own_record=True, repo="owner/repo", subject_pr=row["number"]
        )
    except ValueError:
        pass
    else:
        raise AssertionError("A deleted fork became same-repository claimable")


def test_pr_native_relationships_and_bounded_comment_reads() -> None:
    plan = load_plan()
    node = json.loads(FIXTURE.with_name("planning_show.json").read_text())["graphql"]["data"]["repository"]["issue"]
    pull = {"__typename": "PullRequest", "number": node["number"], "updatedAt": node["updatedAt"], "repository": node["repository"]}
    comments = copy.deepcopy(node["comments"])
    comments.update(totalCount=len(comments["nodes"]) + 1, pageInfo={"hasNextPage": True, "endCursor": "unread-history"})
    replies = [
        {"data": {"repository": {"issueOrPullRequest": pull}}},
        {"data": {"repository": {"issueOrPullRequest": {**node, "__typename": "Issue", "comments": comments}}}},
    ]
    reader = github_read.GitHubReader(gh_cmd="fake-gh", actor="fixture-reader", expected_actor="fixture-reader")
    with (
        patch.object(github_api, "call_gh_with_retry", side_effect=transport_for(replies)),
        mock_attribute(plan, "next_graph_reader", return_value=reader),
    ):
        assert plan.read_planning_connection("owner/repo", node["number"], "subIssues")[1] == []
        _, bounded = plan.read_planning_connection("owner/repo", node["number"], "comments", limit=1)
        assert bounded == [graph.rest_comment(comments["nodes"][0])]


def test_closed_wait_targets_preserve_completion_uncertainty() -> None:
    plan = load_plan()
    from skills.direction.scripts import direction_audit

    source = json.loads(FIXTURE.with_name("planning_show.json").read_text())["graphql"]["data"]["repository"]["issue"]
    for reason, body, completed in [("NOT_PLANNED", "", False), ("COMPLETED", "State: Split", False), ("COMPLETED", "Done", True)]:
        node = {**source, "state": "CLOSED", "stateReason": reason, "body": body, "closedAt": source["updatedAt"]}
        reader = github_read.GitHubReader(gh_cmd="fake-gh", actor="fixture-reader", expected_actor="fixture-reader")
        with (
            patch.object(github_api, "call_gh_with_retry", side_effect=transport_for([{"data": {"repository": {"issue": node}}}])),
            mock_attribute(plan, "next_graph_reader", return_value=reader),
        ):
            _, target = plan.get_issue(str(node["number"]), "owner/repo", graphql=True)
            assert target["closed_at"] == node["closedAt"]
            assert direction_audit.closed_wait_prerequisite(target) is completed

    pull = copy.deepcopy(next(iter(json.loads(FIXTURE.with_name("pr_batch.json").read_text())["data"]["repository"].values())))
    pull.update(
        __typename="PullRequest",
        repository={"nameWithOwner": "owner/repo"},
        milestone=None,
        authorAssociation="CONTRIBUTOR",
        comments=source["comments"],
        closedAt=source["updatedAt"],
    )
    reader = github_read.GitHubReader(gh_cmd="fake-gh", actor="fixture-reader", expected_actor="fixture-reader")
    with (
        patch.object(github_api, "call_gh_with_retry", side_effect=transport_for([{"data": {"repository": {"issue": pull}}}])),
        mock_attribute(plan, "next_graph_reader", return_value=reader),
        mock_attribute(plan, "emit") as output,
    ):
        plan.cmd_show(plan.build_parser().parse_args(["--repo", "owner/repo", "show", str(pull["number"]), "--full"]))
    assert output.call_args.args[0]["issue"]["body"] == pull["body"]
    assert output.call_args.args[0]["issue"]["state"] == "closed"


def test_inbound_scan_reuses_full_graph_and_preserves_inventory_sentinel() -> None:
    plan = load_plan()
    source = json.loads(FIXTURE.with_name("planning_show.json").read_text())["graphql"]["data"]["repository"]["issue"]
    rows = graph.planning_graph(
        reader_for({"data": {"repository": {"issues": {"nodes": [source], "totalCount": 1, "pageInfo": {"hasNextPage": False}}}}}),
        "owner/repo",
    )
    target = {
        "repo": "other/product",
        "number": 44,
        "state": "open",
        "title": "Blocked",
        "html_url": "https://github.com/other/product/issues/44",
    }
    rows[0]["_graph_relationships"]["blocking"] = [target]
    rows[0]["issue_dependencies_summary"]["blocking"] = 1
    with mock_attribute(plan, "read_planning_inventory", side_effect=AssertionError("Full observation should be reused")):
        _, report, coverage = plan.read_next_inbound_blockers(
            "owner/repo", scan_limit=1, observed_inventory=("fixture-reader", rows)
        )
        assert report[0]["blocking"][0]["number"] == target["number"]
        assert coverage["complete"]
        with patch.dict(vars(plan), {"NEXT_PLAN_INVENTORY_LIMIT": len(rows)}):
            _, _, coverage = plan.read_next_inbound_blockers(
                "owner/repo", scan_limit=1, observed_inventory=("fixture-reader", rows + rows)
            )
            assert coverage["inventory_truncated"] and not coverage["complete"]


def test_milestone_next_still_reports_unlisted_inbound_blockers() -> None:
    plan = load_plan()
    fixture = json.loads(FIXTURE.with_name("planning_show.json").read_text())
    root = graph.planning_graph(reader_for(fixture["graphql"]), "owner/repo", number=fixture["rest"]["number"])[0]
    milestone = {"number": 1, "title": "Selected", "state": "open", "created_at": root["created_at"]}
    root["milestone"] = milestone
    gate = copy.deepcopy(root)
    gate.update(number=root["number"] + 1, milestone=None)
    gate["issue_dependencies_summary"]["blocking"] = 1
    target = {
        "repo": "other/product",
        "number": 44,
        "state": "open",
        "title": "Blocked",
        "html_url": "https://github.com/other/product/issues/44",
    }
    gate["_graph_relationships"]["blocking"] = [target]
    vars(plan)["BATCH_READ_CONTEXT"]["diagnostics"] = {"actor": "fixture-reader"}
    with (
        mock_attribute(plan, "read_planning_graph", return_value=[root]),
        mock_attribute(plan, "read_planning_inventory", return_value=("fixture-reader", [root, gate])),
        mock_attribute(
            plan.github_milestone_core, "show_milestone", return_value={"actor": "fixture-reader", "milestone": milestone}
        ),
        mock_attribute(plan, "next_focus_context", return_value=(None, {}, {"available": False})),
        mock_attribute(plan, "next_wait_context", return_value={"items": [], "checked_issues": [], "unavailable": []}),
        mock_attribute(plan, "next_dependabot_work", return_value={}),
        mock_attribute(plan, "annotate_next_queue", return_value={"repositories": {}}),
        mock_attribute(plan, "wait_authors", return_value={"owner"}),
        mock_attribute(plan, "emit") as output,
    ):
        plan.cmd_next(plan.build_parser().parse_args(["--repo", "owner/repo", "next", "--milestone", "1"]))
    result = output.call_args.args[0]
    assert [row["number"] for row in result["blocking_work_elsewhere"]] == [gate["number"]]
    assert result["blocking_work_elsewhere"][0]["blocking"][0]["repo"] == target["repo"]


def test_failed_prefetch_stays_unread_when_reused_as_a_wait_target() -> None:
    plan = load_plan()
    fixture = json.loads(FIXTURE.with_name("planning_show.json").read_text())
    source = graph.planning_graph(reader_for(fixture["graphql"]), "owner/repo", number=fixture["rest"]["number"])[0]
    source["body"] = "## Current Status\nState: Waiting.\nWaiting for: Review #13.\n"
    failures = [
        github_read.GitHubReadShapeError("Missing prefetched graph"),
        github_read.GitHubReadError(
            "Source inaccessible",
            result=github_api.ApiResult(
                ok=False,
                status=403,
                body=None,
                bucket="graphql",
                failure=github_api.FailureDetail("identity_refused", "Source inaccessible", False, False, "stop"),
            ),
            diagnostics={},
        ),
    ]
    reader = plan.PlanningGraphReader(gh_cmd="fake-gh", actor="fixture-reader", expected_actor="fixture-reader")
    for failure in failures:
        with (
            plan.github_next_reads.NextReads(workers=1, seconds=0) as reads,
            patch.dict(vars(plan), {"NEXT_READS": reads}),
            mock_attribute(plan, "next_graph_reader", return_value=reader),
        ):

            def unavailable() -> Any:
                raise failure

            try:
                reads.observe("/repos/owner/repo/issues/13", unavailable)
            except type(failure):
                pass
            result = plan.next_wait_context(
                [source],
                scan_limit=1,
                inventory_complete=True,
                structured_waits={("owner/repo", source["number"]): None},
                reference_records=[source],
                repository_metadata={"owner/repo": {"full_name": "owner/repo", "archived": False}},
            )
            assert not result["complete"] and result["unavailable"]


def test_metadata_is_available_without_unrelated_discussion_pages() -> None:
    plan = load_plan()
    node = copy.deepcopy(json.loads(FIXTURE.with_name("planning_show.json").read_text())["graphql"]["data"]["repository"]["issue"])
    node["comments"].update(totalCount=1000, pageInfo={"hasNextPage": True, "endCursor": "unavailable-history"})
    node["blockedBy"] = {"nodes": [], "totalCount": 1, "pageInfo": {"hasNextPage": True, "endCursor": "unavailable-blockers"}}
    reader = github_read.GitHubReader(gh_cmd="fake-gh", actor="fixture-reader", expected_actor="fixture-reader")
    with (
        patch.object(github_api, "call_gh_with_retry", side_effect=transport_for([{"data": {"repository": {"issue": node}}}])),
        mock_attribute(plan, "next_graph_reader", return_value=reader),
    ):
        _, result = plan.get_issue(str(node["number"]), "owner/repo", graphql=True)
    assert result["body"] == node["body"] and result["comments"] == node["comments"]["totalCount"]
    assert not result.get("_graph_comments_complete")


def test_native_closure_metadata_preserves_holds_in_wait_consumers() -> None:
    plan = load_plan()
    from skills.direction.scripts import direction_audit

    original = json.loads(FIXTURE.with_name("planning_show.json").read_text())["graphql"]["data"]["repository"]["issue"]
    for reason, body, remaining in [("NOT_PLANNED", "", 0), ("COMPLETED", "State: Split", 0), ("COMPLETED", "", 1)]:
        node = copy.deepcopy(original)
        node["body"] = "## Current Status\nState: Waiting.\nWaiting for: None.\n"
        blocker = {
            "number": 13,
            "title": "Unfinished prerequisite",
            "state": "CLOSED",
            "stateReason": reason,
            "body": body,
            "closedAt": node["updatedAt"],
            "url": "https://github.com/owner/repo/issues/13",
            "repository": {"nameWithOwner": "owner/repo"},
            "subIssuesSummary": {"total": 1, "completed": 1 - remaining, "percentCompleted": 0},
        }
        node["blockedBy"] = {"nodes": [blocker], "totalCount": 1, "pageInfo": {"hasNextPage": False}}
        node["issueDependenciesSummary"].update(blockedBy=0, totalBlockedBy=1)
        row = graph.planning_graph(reader_for({"data": {"repository": {"issue": node}}}), "owner/repo", number=node["number"])[0]
        [normalized] = row["_graph_relationships"]["blocked_by"]
        assert not direction_audit.closed_wait_prerequisite(normalized)
        with mock_attribute(plan, "next_graph_reader", return_value=None):
            result = plan.next_wait_context(
                [row],
                scan_limit=1,
                inventory_complete=True,
                structured_waits={("owner/repo", row["number"]): None},
                reference_records=[row],
                repository_metadata={"owner/repo": {"full_name": "owner/repo", "archived": False}},
            )
        assert result["complete"] and not result["items"], result
        compact = {key: value for key, value in normalized.items() if key in {"repo", "number", "title", "state", "html_url"}}
        incomplete = {**row, "_graph_relationships": {**row["_graph_relationships"], "blocked_by": [compact]}}
        with (
            plan.github_next_reads.NextReads(workers=1, seconds=0) as reads,
            patch.dict(vars(plan), {"NEXT_READS": reads}),
            mock_attribute(plan, "api_json_uncached", return_value=("fixture-reader", [normalized])),
        ):
            plan.github_work_graph.publish_issue_pages(reads, incomplete, "fixture-reader")
            _, observed = plan.api_json(
                "GET", f"/repos/owner/repo/issues/{row['number']}/dependencies/blocked_by?per_page=100&page=1"
            )
            assert not direction_audit.closed_wait_prerequisite(observed[0])


def test_root_and_nested_graphql_failures_do_not_invent_missing_targets() -> None:
    for kind in ("NOT_FOUND", "FORBIDDEN"):
        for root in ({"number": 13}, None):
            result = github_api.ApiResult(
                ok=True,
                status=200,
                bucket="graphql",
                body={
                    "data": {"repository": {"issue": root}},
                    "errors": [{"type": kind, "path": ["repository", "issue", "blockedBy", "nodes", 0]}],
                },
            )
            try:
                github_read.graphql_data(result, root_path=("repository", "issue"))
            except github_read.GitHubReadShapeError as error:
                assert error.rest_recovery_allowed is (root is not None)
            else:
                raise AssertionError("A nested access error became an available or missing root")
        result.body["errors"] = [{"type": kind, "path": ["repository", "issue"]}]
        try:
            github_read.graphql_data(result, root_path=("repository", "issue"))
        except github_read.GitHubReadError as error:
            assert error.result.failure is not None and not error.rest_recovery_allowed
        else:
            raise AssertionError("Root access failure lost its classification")


def test_partial_search_retains_candidates_and_strict_reads_still_refuse() -> None:
    first: dict[str, Any] = {**issue_fixture(), "__typename": "Issue",
                             "labels": {"totalCount": 0, "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
    second = {**copy.deepcopy(first), "number": first["number"] + 1}

    def page(nodes: list[dict[str, Any]], total: int, cursor: str | None = None) -> dict[str, Any]:
        return {"data": {"search": {"nodes": nodes, "totalCount": total,
                "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}}}

    failed_page = {"data": {"search": None}, "errors": [{"type": "FORBIDDEN", "path": ["search"]}]}
    rows, metadata = graph.search_records(reader_for(page([first], 2, "next"), failed_page), "user:owner is:issue", limit=1000,
                                         allow_partial=True)
    assert [row["number"] for row in rows] == [first["number"]]
    assert metadata["incomplete_results"] and metadata["errors"] and metadata["total_count"] == 2
    try:
        graph.search_records(reader_for(page([first], 2, "next"), failed_page), "user:owner is:issue", limit=1000)
    except github_read.GitHubReadError:
        pass
    else:
        raise AssertionError("Strict callers accepted an unread page")
    duplicate = {**first, "repository": {"nameWithOwner": first["repository"]["nameWithOwner"].upper()}}
    for later_nodes, later_total in (([second], 1), ([duplicate, second], 3), ([{}, second], 3)):
        rows, metadata = graph.search_records(reader_for(page([first], 2, "next"), page(later_nodes, later_total)), "user:owner is:issue",
                                             limit=1000, allow_partial=True)
        assert [row["number"] for row in rows] == [first["number"], second["number"]]
        assert metadata["incomplete_results"] and metadata["errors"]


def test_repository_stop_preserves_pagination_and_other_repository_failure() -> None:
    reader = Mock(batch_diagnostic={"cost": 0})
    def respond(document: str, _variables: dict[str, Any], **_kwargs: Any) -> github_api.ApiResult:
        data: dict[str, Any] = {}
        for alias, repo in re.findall(r'(\w+):repository\(owner:"owner",name:"(\w+)"\)', document):
            nodes = [{"number": 3}, {"number": 2}, {"number": 1}]
            data[alias] = {"nameWithOwner": f"owner/{repo}", "issues": {
                "totalCount": 9, "nodes": nodes,
                "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}
            if 'after:"next"' in document:
                data[alias] = None
        return github_api.ApiResult(ok=True, status=200, body={"data": data})

    reader.graphql_json.side_effect = respond
    result = github_read.read_repository_connections(
        reader, ["owner/window", "owner/broken"], field="issues",
        selection=lambda cursor: f"issues(first:3,after:{json.dumps(cursor)}) {{ nodes {{ number }} }}",
        normalize=lambda repo, node: {**node, "repo": repo}, step="fixture_window",
        stop_when=lambda row: row["repo"] == "owner/window" and row["number"] < 2,
    )
    window_rows = result["owner/window"]
    assert isinstance(window_rows, list)
    assert [row["number"] for row in window_rows] == [3, 2]
    assert isinstance(result["owner/broken"], github_read.GitHubReadShapeError)
    assert reader.graphql_json.call_count == 2


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("ok", name)
