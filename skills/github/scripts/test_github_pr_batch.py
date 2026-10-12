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
import tempfile
import os
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
    numbers = [int(key[1:]) for key in fixture["data"]["repository"]]
    observed = reader()
    with patch.object(
        observed, "_transport_request", return_value=response(fixture)
    ) as transport:
        pulls = batch.batch_pull_requests(observed, "owner/repo", numbers)
        assert transport.call_count == 1
        for number, pull in pulls.items():
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
        assert len(numbers) * 3 >= 18
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
        lambda doc: doc.update(errors=[{"message": "missing grant"}]),
        lambda doc: doc["data"].update(repository=None),
        lambda doc: doc["data"]["repository"][f"p{numbers[0]}"].pop("reviews"),
        lambda doc: doc["data"]["repository"][f"p{numbers[0]}"]["comments"].update(
            totalCount=999
        ),
        lambda doc: doc["data"]["repository"][f"p{numbers[0]}"].update(
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
        f"/repos/owner/repo/issues/{node['number']}/comments", step_prefix="comments"
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


def test_default_unchanged_reads_retain_zero_charged_requests():
    with (
        tempfile.TemporaryDirectory() as root,
        patch.dict(os.environ, {"GITHUB_READ_CACHE_DIR": root}),
    ):
        observed = reader()
        observed.cache_enabled = True
        observed.cache_revalidate = True
        first = response({"number": 1})
        first.headers = {"etag": "fixture-validator"}
        unchanged = response(None)
        unchanged.status = 304
        with patch.object(
            observed, "_transport_request", side_effect=[first, unchanged]
        ) as transport:
            assert observed.get_json("/repos/owner/repo/pulls/1", step="cold") == {
                "number": 1
            }
            assert observed.get_json("/repos/owner/repo/pulls/1", step="unchanged") == {
                "number": 1
            }
        assert (
            transport.call_args.kwargs["extra_headers"]["If-None-Match"]
            == "fixture-validator"
        )
        assert observed.requests[-1]["status"] == 304
        assert observed.requests[-1]["cache"] == "revalidated"


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


if __name__ == "__main__":
    for test_name, test in list(globals().items()):
        if test_name.startswith("test_"):
            test()
            print("ok", test_name)
