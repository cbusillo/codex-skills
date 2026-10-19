#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Recorded PR batching, unchanged conditional behavior and fail-closed recovery."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import github_api
import github_pr_batch as batch
import github_read

FIXTURE = Path(__file__).parent / "fixtures/pr_batch.json"


def reader() -> github_read.GitHubReader:
    return github_read.GitHubReader(
        gh_cmd="fake-gh",
        actor="fixture-reader",
        expected_actor="fixture-reader",
        operation="github.pr.watch",
        strict_actor=True,
    )


def response(body: Any) -> github_api.ApiResult:
    return github_api.ApiResult(
        ok=True,
        status=200,
        body=copy.deepcopy(body),
        actor="fixture-reader",
        expected_actor="fixture-reader",
        operation="github.pr.watch",
        bucket="graphql",
    )


def test_recorded_six_pr_checks_and_request_drop():
    fixture = json.loads(FIXTURE.read_text())
    rest = json.loads(FIXTURE.with_name("pr_batch_rest_checks.json").read_text())
    discussions = json.loads(
        FIXTURE.with_name("pr_batch_rest_comments.json").read_text()
    )
    numbers = [int(key[1:]) for key in fixture["data"]["repository"]]
    observed = reader()
    with patch.object(
        observed, "_transport_request", return_value=response(fixture)
    ) as transport:
        pulls = batch.batch_pull_requests(observed, "owner/repo", numbers)
        assert transport.call_count == 1
        for number, pull in pulls.items():
            comment_path = f"/repos/owner/repo/issues/{number}/comments"
            assert (
                observed.paged_json(comment_path, step_prefix="recorded_comments")
                == discussions[comment_path]
            )
            assert (
                observed.get_json(f"/repos/owner/repo/pulls/{number}", step="metadata")[
                    "head"
                ]["sha"]
                == pull["head"]["sha"]
            )
            path = f"/repos/owner/repo/commits/{pull['head']['sha']}/check-runs"
            checks = observed.paged_json(
                path, step_prefix="checks", collection_key="check_runs"
            )
            expected = rest[path]["check_runs"]
            assert sorted(
                [github_read.normalize_check_run(x) for x in checks],
                key=lambda x: x["id"],
            ) == sorted(
                [github_read.normalize_check_run(x) for x in expected],
                key=lambda x: x["id"],
            )
            path = f"/repos/owner/repo/commits/{pull['head']['sha']}/statuses"
            actual = observed.paged_json(path, step_prefix="statuses")
            assert [github_read.normalize_status(x) for x in actual] == [
                github_read.normalize_status(x) for x in rest[path]
            ]
        assert transport.call_count == 1
        baseline_reader = reader()

        def baseline_response(_method, endpoint, **_kwargs):
            endpoint = endpoint.split("?")[0]
            return response(
                rest[endpoint]
                if endpoint in rest
                else {"number": int(endpoint.rsplit("/", 1)[-1])}
            )

        with patch.object(
            baseline_reader, "_transport_request", side_effect=baseline_response
        ) as baseline_transport:
            for baseline_number, baseline_pull in pulls.items():
                baseline_reader.get_json(
                    f"/repos/owner/repo/pulls/{baseline_number}",
                    step="baseline_metadata",
                )
                baseline_reader.paged_json(
                    f"/repos/owner/repo/commits/{baseline_pull['head']['sha']}/check-runs",
                    step_prefix="baseline_checks",
                    collection_key="check_runs",
                )
                baseline_reader.paged_json(
                    f"/repos/owner/repo/commits/{baseline_pull['head']['sha']}/statuses",
                    step_prefix="baseline_statuses",
                )
        assert baseline_transport.call_count >= transport.call_count * 2
        # Repeated changing pass is a new query, never an offline snapshot.
        changed = copy.deepcopy(fixture)
        changed["data"]["repository"][f"p{numbers[0]}"]["title"] = "Changed"
        transport.return_value = response(changed)
        assert (
            batch.batch_pull_requests(observed, "owner/repo", numbers)[numbers[0]][
                "title"
            ]
            == "Changed"
        )
        assert transport.call_count == 2


def test_one_pass_actor_and_head_isolation():
    fixture = json.loads(FIXTURE.read_text())
    numbers = [int(key[1:]) for key in fixture["data"]["repository"]]
    observed = reader()
    with patch.object(
        observed, "_transport_request", return_value=response(fixture)
    ) as transport:
        pulls = batch.batch_pull_requests(observed, "owner/repo", numbers)
        number = numbers[0]
        path = f"/repos/owner/repo/pulls/{number}"
        assert observed.get_json(path, step="first")["number"] == number
        fresh = {"head": {"sha": "b" * 40}, "labels": []}
        transport.return_value = response(fresh)
        assert observed.get_json(path, step="final") == fresh
        assert transport.call_count == 2
        observed.actor = "another-actor"
        with patch.object(observed, "request", return_value=response(fresh)) as rest:
            assert (
                observed.get_json(
                    f"/repos/owner/repo/pulls/{numbers[1]}", step="actor_change"
                )
                == fresh
            )
            assert rest.call_count == 1
        assert not observed.batch_responses
        assert pulls[number]["head"]["sha"] != "b" * 40


def test_partial_errors_head_drift_and_missing_connections_recover_to_rest():
    fixture = json.loads(FIXTURE.read_text())
    numbers = [int(key[1:]) for key in fixture["data"]["repository"]]
    for change in [
        lambda document: document.update(errors=[{"message": "missing grant"}]),
        lambda document: document["data"].update(repository=None),
        lambda document: document["data"]["repository"][f"p{numbers[0]}"].pop(
            "reviews"
        ),
        lambda document: document["data"]["repository"][f"p{numbers[0]}"][
            "comments"
        ].update(totalCount=999),
        lambda document: document["data"]["repository"][f"p{numbers[0]}"].update(
            headRefOid="b" * 40
        ),
    ]:
        doc = copy.deepcopy(fixture)
        change(doc)
        observed = reader()
        with patch.object(observed, "_transport_request", return_value=response(doc)):
            assert batch.try_batch(observed, "owner/repo", numbers) is None
        assert observed.batch_responses == {}
        assert observed.batch_diagnostic["used"] is False
        with patch.object(
            observed, "request", return_value=response({"number": numbers[0]})
        ) as rest:
            assert (
                observed.get_json(
                    f"/repos/owner/repo/pulls/{numbers[0]}", step="fallback"
                )["number"]
                == numbers[0]
            )
            assert rest.call_count == 1
    observed = reader()
    failure = github_api.ApiResult(
        ok=False,
        status=403,
        body={"message": "GraphQL exhausted"},
        actor="fixture-reader",
        expected_actor="fixture-reader",
        operation="github.pr.watch",
        bucket="graphql",
    )
    with patch.object(observed, "_transport_request", return_value=failure):
        assert batch.try_batch(observed, "owner/repo", numbers) is None
    with patch.object(
        observed, "request", return_value=response({"number": numbers[0]})
    ):
        assert (
            observed.get_json(
                f"/repos/owner/repo/pulls/{numbers[0]}", step="rest_after_quota"
            )["number"]
            == numbers[0]
        )


def test_reviews_and_comments_paginate_before_publication():
    fixture = json.loads(FIXTURE.read_text())
    key = next(iter(fixture["data"]["repository"]))
    node = fixture["data"]["repository"][key]
    comment = {
        "databaseId": 1,
        "body": "Early",
        "createdAt": "a",
        "updatedAt": "a",
        "url": "https://example/1",
        "author": {"__typename": "Bot", "login": "fixture-bot"},
        "authorAssociation": "CONTRIBUTOR",
    }
    node["comments"] = {
        "totalCount": 2,
        "nodes": [comment],
        "pageInfo": {"hasNextPage": True, "endCursor": "next"},
    }
    later = copy.deepcopy(comment)
    later.update(databaseId=2, body="Late")
    more = {
        "data": {
            "repository": {
                "pullRequest": {
                    "headRefOid": node["headRefOid"],
                    "updatedAt": node["updatedAt"],
                    "comments": {
                        "totalCount": 2,
                        "nodes": [later],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            }
        }
    }
    observed = reader()
    with patch.object(
        observed, "_transport_request", side_effect=[response(fixture), response(more)]
    ):
        batch.batch_pull_requests(observed, "owner/repo", [node["number"]])
    comments = observed.paged_json(
        f"/repos/owner/repo/issues/{node['number']}/comments",
        step_prefix="comments",
        params={"per_page": 100},
    )
    assert [item["body"] for item in comments] == ["Early", "Late"]
    assert comments[0]["user"] == {"login": "fixture-bot[bot]", "type": "Bot"}
    more["data"]["repository"]["pullRequest"]["headRefOid"] = "b" * 40
    observed = reader()
    with patch.object(
        observed, "_transport_request", side_effect=[response(fixture), response(more)]
    ):
        assert batch.try_batch(observed, "owner/repo", [node["number"]]) is None
    assert observed.batch_responses == {}


def test_batch_keeps_actions_evidence_and_cannot_hide_missing_runs():
    fixture = json.loads(FIXTURE.read_text())
    baseline = json.loads(FIXTURE.with_name("pr_batch_rest_checks.json").read_text())
    key = next(iter(fixture["data"]["repository"]))
    node = fixture["data"]["repository"][key]
    observed = reader()
    with patch.object(observed, "_transport_request", return_value=response(fixture)):
        batch.batch_pull_requests(observed, "owner/repo", [node["number"]])
    # Combined status remains a real REST read; missing Actions run identity
    # cannot become green merely because the batched checks all succeeded.
    with patch.object(
        observed, "request", return_value=response({"state": "success"})
    ) as rest:
        result = github_read.pull_request_checks(
            observed,
            "owner/repo",
            node["number"],
            head_sha=node["headRefOid"],
            workflow_runs=[],
        )
    assert rest.call_args.args[1].endswith("/status")
    assert result["summary"]["countsComplete"] is False
    assert "workflowSelection" in result["summary"]["unavailableComponents"]
    expected = reader()
    paths = [
        f"/repos/owner/repo/commits/{node['headRefOid']}/check-runs",
        f"/repos/owner/repo/commits/{node['headRefOid']}/statuses",
    ]

    def from_rest(_method, path, **_kwargs):
        path = path.split("?")[0]
        return response(baseline[path] if path in paths else {"state": "success"})

    with patch.object(expected, "_transport_request", side_effect=from_rest):
        legacy = github_read.pull_request_checks(
            expected,
            "owner/repo",
            node["number"],
            head_sha=node["headRefOid"],
            workflow_runs=[],
        )
    assert result["summary"] == legacy["summary"]


def test_context_pagination_and_statuses_remain_complete():
    fixture = json.loads(FIXTURE.read_text())
    node = next(iter(fixture["data"]["repository"].values()))
    contexts = node["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
    status = {
        "__typename": "StatusContext",
        "id": "status-node",
        "context": "external",
        "state": "PENDING",
        "description": "Fixture description",
        "targetUrl": "https://example/check",
        "createdAt": "2026-01-01T00:00:00Z",
    }
    contexts.update(
        totalCount=2,
        nodes=[status],
        pageInfo={"hasNextPage": True, "endCursor": "next-context"},
    )
    second = dict(status, id="status-node-2", context="required", state="SUCCESS")
    more = {
        "data": {
            "repository": {
                "pullRequest": {
                    "headRefOid": node["headRefOid"],
                    "updatedAt": node["updatedAt"],
                    "commits": {
                        "nodes": [
                            {
                                "commit": {
                                    "oid": node["headRefOid"],
                                    "statusCheckRollup": {
                                        "contexts": {
                                            "totalCount": 2,
                                            "nodes": [second],
                                            "pageInfo": {
                                                "hasNextPage": False,
                                                "endCursor": None,
                                            },
                                        }
                                    },
                                }
                            }
                        ]
                    },
                }
            },
            "rateLimit": {"cost": 1},
        }
    }
    observed = reader()
    with patch.object(
        observed, "_transport_request", side_effect=[response(fixture), response(more)]
    ):
        assert batch.try_batch(observed, "owner/repo", [node["number"]]) is not None
    statuses = observed.paged_json(
        f"/repos/owner/repo/commits/{node['headRefOid']}/statuses",
        step_prefix="statuses",
    )
    assert [(x["context"], x["state"]) for x in statuses] == [
        ("external", "pending"),
        ("required", "success"),
    ]
    assert (
        observed.batch_diagnostic["cost"]
        == fixture["data"]["rateLimit"]["cost"] + more["data"]["rateLimit"]["cost"]
    )


def test_watcher_discussion_keeps_verified_publisher_fields_on_rest():
    fixture = json.loads(FIXTURE.read_text())
    node = next(iter(fixture["data"]["repository"].values()))
    observed = reader()
    with patch.object(observed, "_transport_request", return_value=response(fixture)):
        assert (
            batch.try_batch(
                observed, "owner/repo", [node["number"]], include_discussion=False
            )
            is not None
        )
    comment = {
        "id": 1,
        "user": {"login": "publisher[bot]", "id": 123},
        "performed_via_github_app": {"id": 456},
        "body": "Owner decision",
    }
    with patch.object(observed, "request", return_value=response([comment])) as rest:
        comments = observed.paged_json(
            f"/repos/owner/repo/issues/{node['number']}/comments",
            step_prefix="client_feedback",
            params={"per_page": 100},
        )
    assert comments[0]["user"]["id"] == comment["user"]["id"]
    assert (
        comments[0]["performed_via_github_app"] == comment["performed_via_github_app"]
    )
    assert rest.call_count == 1


if __name__ == "__main__":
    for test_name, test in list(globals().items()):
        if test_name.startswith("test_"):
            test()
            print("ok", test_name)
