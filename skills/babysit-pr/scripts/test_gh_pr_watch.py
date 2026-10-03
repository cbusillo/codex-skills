#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
# ]
# ///

import argparse
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/codex-skills-test.env"

import gh_pr_watch
import pytest


def sample_pr() -> dict[str, Any]:
    return {
        "number": 123,
        "url": "https://github.com/openai/codex/pull/123",
        "repo": "openai/codex",
        "head_sha": "abc123",
        "head_branch": "feature",
        "base_branch": "main",
        "merge_commit_sha": "",
        "state": "OPEN",
        "merged": False,
        "closed": False,
        "draft": False,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "review_decision": "",
        "review_requirement": "satisfied",
        "review_decision_source": "graphql",
        "metadata_availability": {
            "draft": True,
            "mergeable": True,
            "merge_state_status": True,
            "review_decision": True,
        },
    }


def sample_checks(**overrides):
    checks = {
        "pending_count": 0,
        "failed_count": 0,
        "passed_count": 12,
        "all_terminal": True,
        "evidence_complete": True,
        "counts_are_lower_bounds": False,
        "unavailable_components": [],
        "head_matches": True,
    }
    checks.update(overrides)
    return checks


def sample_rest_view(**overrides):
    pr = {
        "number": 42,
        "url": "https://github.com/example/repo/pull/42",
        "state": "open",
        "merged": False,
        "mergedAt": None,
        "mergeCommitOid": "",
        "draft": False,
        "baseRefName": "main",
        "headRefName": "feature",
        "headRepository": "example/repo",
        "headRefOid": "b" * 40,
        "mergeable": True,
        "mergeStateStatus": "clean",
        "reviewDecision": None,
    }

    pr.update(overrides)
    return {
        "ok": True,
        "repo": "example/repo",
        "pr": pr,
        "_watcher_diagnostic": {"transport": "rest_api", "ok": True},
    }


class ReviewReader:
    def __init__(self, body=None, *, degraded_reasons=None):
        self.body = body
        self.requests = [{"ok": True, "status": 200}]
        self.degraded_reasons = list(degraded_reasons or [])
        self.degraded = None

    def graphql_json(self, *_args, **_kwargs):
        return type("Result", (), {"ok": True, "body": self.body})()

    def mark_degraded(self, *args):
        self.degraded = args
        self.degraded_reasons.append({"component": args[0], "code": args[1], "message": args[2]})

    def diagnostics(self):
        return {"requests": self.requests, "degradedReasons": self.degraded_reasons}


def test_resolve_pr_uses_rest_helper_and_exposes_final_merge_commit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        gh_pr_watch,
        "pr_helper_json",
        lambda *args, **kwargs: calls.append((args, kwargs))
        or sample_rest_view(
            state="closed",
            merged=True,
            mergedAt="2026-07-17T19:00:00Z",
            mergeCommitOid="a" * 40,
            mergeable=None,
            mergeStateStatus="unknown",
        ),
    )

    pr = gh_pr_watch.resolve_pr("42", repo_override="example/repo")

    assert pr["merged"] is True
    assert pr["closed"] is True
    assert pr["state"] == "MERGED"
    assert pr["base_branch"] == "main"
    assert pr["merge_commit_sha"] == "a" * 40
    assert pr["head_sha"] == "b" * 40
    assert pr["head_repository"] == "example/repo"
    assert pr["mergeable"] == "UNKNOWN"
    assert pr["metadata_availability"]["review_decision"] is False
    assert calls == [
        (("view",), {"pr_spec": "42", "repo": "example/repo"})
    ]


def test_resolve_pr_rejects_conflicting_repo_override(monkeypatch):
    monkeypatch.setattr(gh_pr_watch, "pr_helper_json", lambda *args, **kwargs: None)

    with pytest.raises(gh_pr_watch.GhCommandError, match="does not match --repo"):
        gh_pr_watch.resolve_pr(
            "https://github.com/example/repo/pull/42",
            repo_override="other/repo",
        )


def test_resolve_pr_accepts_extended_url_and_preserves_empty_open_merge_commit(
    monkeypatch,
):
    calls = []
    payload = sample_rest_view(mergeCommitOid="a" * 40)
    monkeypatch.setattr(
        gh_pr_watch,
        "pr_helper_json",
        lambda *args, **kwargs: calls.append((args, kwargs)) or payload,
    )
    url = "https://github.com/example/repo/pull/42/files?diff=split#discussion_r1"

    pr = gh_pr_watch.resolve_pr(url)

    assert pr["number"] == 42
    assert pr["merge_commit_sha"] == ""
    assert calls == [
        (("view",), {"pr_spec": "42", "repo": "example/repo"})
    ]


def test_pr_helper_json_forwards_gh_command_to_rest_helper(monkeypatch):
    calls = []
    monkeypatch.setattr(gh_pr_watch, "GH_COMMAND", "/graphql-fails-rest-works")
    monkeypatch.setattr(gh_pr_watch, "PR_HELPER", str(gh_pr_watch.DEFAULT_PR_HELPER))

    def fake_run(cmd, capture_output, text, env):
        assert capture_output is True
        assert text is True
        calls.append((cmd, env["GH_PR_GH"]))
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "transport": "rest_api",
                    "bucket": "rest_core",
                    "repo": "example/repo",
                    "pr": {"number": 42},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(gh_pr_watch.subprocess, "run", fake_run)

    payload = gh_pr_watch.pr_helper_json("view", "42", repo="example/repo")
    gh_pr_watch.pr_helper_json("view", "auto")

    assert payload["pr"] == {"number": 42}
    assert payload["_watcher_diagnostic"] == {
        "transport": "rest_api",
        "bucket": "rest_core",
        "ok": True,
        "returncode": 0,
    }
    assert calls == [
        (
            [
                sys.executable,
                str(gh_pr_watch.DEFAULT_PR_HELPER),
                "--repo",
                "example/repo",
                "view",
                "42",
            ],
            "/graphql-fails-rest-works",
        ),
        ([sys.executable, str(gh_pr_watch.DEFAULT_PR_HELPER), "view"], "/graphql-fails-rest-works"),
    ]


def test_pr_helper_json_accepts_partial_check_envelope(monkeypatch):
    partial = {
        "ok": False,
        "transport": "rest_api",
        "bucket": "rest_core",
        "pr": {"number": 42},
        "headSha": "b" * 40,
        "summary": {
            "failingCount": 1,
            "pendingCount": 0,
            "countsComplete": False,
            "countsAreLowerBounds": True,
            "unavailableComponents": ["commitStatuses"],
        },
    }
    monkeypatch.setattr(
        gh_pr_watch.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout=json.dumps(partial), stderr="redacted error"
        ),
    )
    monkeypatch.setattr(gh_pr_watch, "PR_HELPER", str(gh_pr_watch.DEFAULT_PR_HELPER))

    payload = gh_pr_watch.pr_helper_json(
        "checks", "42", repo="example/repo", allow_partial=True
    )
    summary = gh_pr_watch.summarize_checks(payload, expected_head_sha="b" * 40)

    assert summary == {
        "pending_count": 0,
        "failed_count": 1,
        "passed_count": 0,
        "all_terminal": False,
        "evidence_complete": False,
        "counts_are_lower_bounds": True,
        "unavailable_components": ["commitStatuses"],
        "head_matches": True,
    }


def test_mismatched_check_head_never_becomes_terminal_or_reruns(monkeypatch):
    payload = {
        "headSha": "c" * 40,
        "summary": {
            "checkRunCount": 0,
            "statusCount": 0,
            "failingCount": 0,
            "pendingCount": 0,
            "countsComplete": True,
            "countsAreLowerBounds": False,
            "unavailableComponents": [],
        },
    }

    summary = gh_pr_watch.summarize_checks(payload, expected_head_sha="b" * 40)

    assert summary["all_terminal"] is False
    assert summary["evidence_complete"] is False
    assert summary["head_matches"] is False

    snapshot = {
        "pr": sample_pr(),
        "checks": summary,
        "failed_runs": [{"run_id": 99}],
        "retry_state": {"current_sha_retries_used": 0, "max_flaky_retries": 3},
    }
    monkeypatch.setattr(
        gh_pr_watch,
        "collect_snapshot",
        lambda args: (snapshot, Path("/tmp/pr-babysit-state.json")),
    )

    result = gh_pr_watch.retry_failed_now(argparse.Namespace())

    assert result["rerun_attempted"] is False
    assert result["reason"] == "check_evidence_incomplete"


@pytest.mark.parametrize(
    "summary_overrides",
    [
        {"countsComplete": False},
        {"countsAreLowerBounds": True},
        {"unavailableComponents": ["checkRuns"]},
        {
            "checkRunCount": None,
            "statusCount": None,
            "countsComplete": None,
            "countsAreLowerBounds": None,
        },
        {"pendingCount": None},
    ],
)
def test_each_incomplete_check_axis_fails_closed(summary_overrides):
    summary = {
        "checkRunCount": 0,
        "statusCount": 0,
        "failingCount": 0,
        "pendingCount": 0,
        "countsComplete": True,
        "countsAreLowerBounds": False,
        "unavailableComponents": [],
        **summary_overrides,
    }

    checks = gh_pr_watch.summarize_checks(
        {"headSha": "b" * 40, "summary": summary},
        expected_head_sha="b" * 40,
    )

    assert checks["all_terminal"] is False
    assert checks["evidence_complete"] is False


def test_green_rest_snapshot_surfaces_unavailable_review_readiness():
    pr = sample_pr()
    pr["metadata_availability"]["review_decision"] = False

    assert gh_pr_watch.recommend_actions(
        pr,
        sample_checks(),
        [],
        [],
        [],
        0,
        3,
    ) == ["review_readiness_unavailable"]


def test_behind_branch_blocks_stale_readiness_and_preserves_other_actions():
    pr = sample_pr()
    pr["merge_state_status"] = "BEHIND"
    pr["review_requirement"] = "changes_requested"
    actions = gh_pr_watch.recommend_actions(
        pr, sample_checks(failed_count=1), [{"run_id": 99}], [],
        [{"kind": "review_comment", "id": "1"}], 0, 3
    )
    assert actions == [
        "address_review_changes", "process_review_comment", "diagnose_ci_failure",
        "retry_failed_checks", "update_behind_branch",
    ]
    assert gh_pr_watch.is_pr_ready_to_merge(pr, sample_checks(), []) is False


def test_unknown_merge_state_does_not_request_branch_update():
    pr = sample_pr()
    pr["merge_state_status"] = "UNKNOWN"
    pr["metadata_availability"]["merge_state_status"] = False
    assert "update_behind_branch" not in gh_pr_watch.recommend_actions(
        pr, sample_checks(), [], [], [], 0, 3
    )


def test_review_readiness_query_distinguishes_nullable_decision():
    pr = sample_pr()
    pr.update({"review_decision": "", "review_requirement": "unknown", "review_decision_source": "not_queried"})
    body = {
        "data": {"repository": {
            "nameWithOwner": "openai/codex",
            "pullRequest": {
                "number": 123, "url": pr["url"], "headRefOid": pr["head_sha"],
                "baseRefName": "main", "isDraft": False, "state": "OPEN",
                "reviewDecision": None, "mergeStateStatus": "CLEAN",
            },
        }}
    }

    review, _ = gh_pr_watch.query_review_readiness(pr, ReviewReader(body))
    gh_pr_watch.apply_review_readiness(pr, review)
    assert review["requirement"] == "not_applicable"
    assert pr["review_decision"] is None
    assert pr["metadata_availability"]["review_decision"] is True
    assert gh_pr_watch.is_pr_ready_to_merge(pr, sample_checks(), []) is True


def test_review_readiness_partial_error_is_unknown():
    pr = sample_pr()
    body = {"errors": [{"type": "RATE_LIMITED"}], "data": {"repository": {"pullRequest": None}}}

    reader = ReviewReader(body)
    review, _ = gh_pr_watch.query_review_readiness(pr, reader)
    gh_pr_watch.apply_review_readiness(pr, review)
    assert review["requirement"] == "unknown"
    assert review["source"] == "transport"
    assert reader.degraded[1] == "graphql_partial_error"
    assert gh_pr_watch.is_pr_ready_to_merge(pr, sample_checks(), []) is False


def test_review_readiness_missing_field_is_distinct_from_nullable():
    pr = sample_pr()
    body = {"data": {"repository": {"nameWithOwner": pr["repo"], "pullRequest": {
        "number": pr["number"], "url": pr["url"], "headRefOid": pr["head_sha"],
        "baseRefName": pr["base_branch"], "isDraft": False, "state": "OPEN",
        "reviewDecision": None,
    }}}}

    reader = ReviewReader(body)
    review, _ = gh_pr_watch.query_review_readiness(pr, reader)
    assert review["source"] == "missing_field"
    assert reader.degraded[1] == "graphql_missing_field"


def test_nullable_review_decision_does_not_override_blocking_merge_state():
    pr = sample_pr()
    body = {"data": {"repository": {
        "nameWithOwner": "openai/codex",
        "pullRequest": {
            "number": 123, "url": pr["url"], "headRefOid": pr["head_sha"],
            "baseRefName": "main", "isDraft": False, "state": "OPEN",
            "reviewDecision": None, "mergeStateStatus": "BLOCKED",
        },
    }}}

    review, _ = gh_pr_watch.query_review_readiness(pr, ReviewReader(body))
    gh_pr_watch.apply_review_readiness(pr, review)
    assert review["requirement"] == "unknown"
    assert gh_pr_watch.is_pr_ready_to_merge(pr, sample_checks(), []) is False


def test_review_readiness_requires_complete_check_evidence():
    pr = sample_pr()
    pr["metadata_availability"]["review_decision"] = False
    assert gh_pr_watch.is_review_readiness_unavailable(
        pr, sample_checks(evidence_complete=False), []
    ) is False


def test_review_readiness_requires_exact_check_head():
    pr = sample_pr()
    pr["metadata_availability"]["review_decision"] = False
    assert gh_pr_watch.is_review_readiness_unavailable(
        pr, sample_checks(head_matches=False), []
    ) is False
    assert gh_pr_watch.is_pr_ready_to_merge(
        {**pr, "review_requirement": "satisfied"}, sample_checks(head_matches=False), []
    ) is False


@pytest.mark.parametrize(
    ("decision", "requirement", "ready"),
    [
        ("APPROVED", "satisfied", True),
        ("REVIEW_REQUIRED", "pending", False),
        ("CHANGES_REQUESTED", "changes_requested", False),
    ],
)
def test_review_readiness_decision_matrix(decision: str, requirement: str, ready: bool):
    pr = sample_pr()
    body = {"data": {"repository": {
        "nameWithOwner": pr["repo"],
        "pullRequest": {
            "number": pr["number"], "url": pr["url"], "headRefOid": pr["head_sha"],
            "baseRefName": pr["base_branch"], "isDraft": False, "state": "OPEN",
            "reviewDecision": decision, "mergeStateStatus": "CLEAN",
        },
    }}}

    review, _ = gh_pr_watch.query_review_readiness(pr, ReviewReader(body))
    gh_pr_watch.apply_review_readiness(pr, review)
    assert review["requirement"] == requirement
    assert gh_pr_watch.is_pr_ready_to_merge(pr, sample_checks(), []) is ready
    actions = gh_pr_watch.recommend_actions(pr, sample_checks(), [], [], [], 0, 3)
    expected_action = {
        "pending": "awaiting_review",
        "changes_requested": "address_review_changes",
    }.get(requirement)
    if expected_action:
        assert expected_action in actions


@pytest.mark.parametrize("field", ["url", "headRefOid", "baseRefName", "isDraft", "state"])
def test_review_readiness_same_document_identity_mismatch_is_unknown(field: str):
    pr = sample_pr()
    item = {
        "number": pr["number"], "url": pr["url"], "headRefOid": pr["head_sha"],
        "baseRefName": pr["base_branch"], "isDraft": False, "state": "OPEN",
        "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN",
        field: {
            "url": "https://github.com/openai/codex/pull/999",
            "headRefOid": "different",
            "baseRefName": "other",
            "isDraft": True,
            "state": "CLOSED",
        }[field],
    }
    body = {"data": {"repository": {"nameWithOwner": pr["repo"], "pullRequest": item}}}

    reader = ReviewReader(body)
    review, _ = gh_pr_watch.query_review_readiness(pr, reader)
    assert review["requirement"] == "unknown"
    assert reader.degraded[1] == {
        "url": "graphql_head_mismatch",
        "headRefOid": "graphql_head_mismatch",
        "baseRefName": "graphql_state_mismatch",
        "isDraft": "graphql_state_mismatch",
        "state": "graphql_state_mismatch",
    }[field]


def test_review_readiness_unknown_enum_remains_actionable():
    pr = sample_pr()
    body = {"data": {"repository": {"nameWithOwner": pr["repo"], "pullRequest": {
        "number": pr["number"], "url": pr["url"], "headRefOid": pr["head_sha"],
        "baseRefName": pr["base_branch"], "isDraft": False, "state": "OPEN",
        "reviewDecision": "FUTURE_ENUM", "mergeStateStatus": "CLEAN",
    }}}}

    review, _ = gh_pr_watch.query_review_readiness(pr, ReviewReader(body))
    gh_pr_watch.apply_review_readiness(pr, review)
    assert review["requirement"] == "unknown"
    assert "review_readiness_unavailable" in gh_pr_watch.recommend_actions(
        pr, sample_checks(), [], [], [], 0, 3
    )


def test_review_readiness_actor_drift_is_unknown():
    pr = sample_pr()
    body = {"data": {"repository": {"nameWithOwner": pr["repo"], "pullRequest": {
        "number": pr["number"], "url": pr["url"], "headRefOid": pr["head_sha"],
        "baseRefName": pr["base_branch"], "isDraft": False, "state": "OPEN",
        "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN",
    }}}}

    review, _ = gh_pr_watch.query_review_readiness(
        pr, ReviewReader(body, degraded_reasons=[{"component": "actor", "code": "actor_changed"}])
    )
    assert review["requirement"] == "unknown"


def test_head_mismatch_does_not_diagnose_unrelated_check_failure():
    checks = sample_checks(
        all_terminal=False,
        evidence_complete=False,
        failed_count=1,
        head_matches=False,
    )

    assert gh_pr_watch.recommend_actions(
        sample_pr(),
        checks,
        [],
        [],
        [],
        0,
        3,
    ) == ["check_evidence_incomplete"]


def test_incomplete_evidence_still_diagnoses_proven_failure():
    checks = sample_checks(
        all_terminal=False,
        evidence_complete=False,
        counts_are_lower_bounds=True,
        failed_count=1,
        head_matches=True,
    )

    assert gh_pr_watch.recommend_actions(
        sample_pr(),
        checks,
        [{"run_id": 1}],
        [],
        [],
        0,
        3,
    ) == ["check_evidence_incomplete", "diagnose_ci_failure"]


def test_gh_text_uses_wrapper_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(gh_pr_watch, "GH_COMMAND", str(gh_pr_watch.DEFAULT_GH))

    def fake_run(cmd, check, capture_output, text):
        assert check is True
        assert capture_output is True
        assert text is True
        calls.append(cmd)

        class Result:
            stdout = "ok\n"

        return Result()

    monkeypatch.setattr(gh_pr_watch.subprocess, "run", fake_run)

    assert gh_pr_watch.gh_text(["run", "view", "99"], repo="openai/codex") == "ok\n"

    assert calls == [
        [
            str(gh_pr_watch.DEFAULT_GH),
            "-R",
            "openai/codex",
            "run",
            "view",
            "99",
        ]
    ]


def test_collect_snapshot_fetches_review_items_before_ci(monkeypatch, tmp_path):
    call_order = []
    summarize_kwargs = {}
    pr = sample_pr()

    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *args, **kwargs: pr)
    monkeypatch.setattr(gh_pr_watch, "load_state", lambda path: ({}, True))
    monkeypatch.setattr(
        gh_pr_watch,
        "get_authenticated_login",
        lambda reader=None: call_order.append("auth") or "octocat",
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "fetch_new_review_items",
        lambda *args, **kwargs: call_order.append("review") or [],
    )
    monkeypatch.setattr(
        gh_pr_watch.github_read,
        "pull_request_checks",
        lambda *args, **kwargs: call_order.append("checks")
        or {
            "headSha": pr["head_sha"],
            "summary": {},
            "_watcher_diagnostic": {},
        },
    )

    def fake_summarize(_checks, **kwargs):
        call_order.append("summarize")
        summarize_kwargs.update(kwargs)
        return sample_checks()

    monkeypatch.setattr(gh_pr_watch, "summarize_checks", fake_summarize)
    monkeypatch.setattr(
        gh_pr_watch,
        "get_workflow_runs_for_sha",
        lambda *args, **kwargs: call_order.append("workflow") or [],
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "failed_runs_from_workflow_runs",
        lambda *args, **kwargs: call_order.append("failed_runs") or [],
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "failed_jobs_from_workflow_runs",
        lambda *args, **kwargs: call_order.append("failed_jobs") or [],
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "recommend_actions",
        lambda *args, **kwargs: call_order.append("recommend") or ["idle"],
    )
    monkeypatch.setattr(gh_pr_watch, "save_state", lambda *args, **kwargs: None)

    watcher_args = argparse.Namespace(
        pr="123",
        repo=None,
        state_file=str(tmp_path / "watcher-state.json"),
        max_flaky_retries=3,
    )

    gh_pr_watch.collect_snapshot(watcher_args)

    assert call_order.index("review") < call_order.index("checks")
    assert call_order.index("review") < call_order.index("workflow")
    assert summarize_kwargs == {"expected_head_sha": pr["head_sha"]}


@pytest.mark.parametrize("status", ["queued", "pending", "waiting", "requested", "in_progress"])
def test_unstarted_workflow_run_keeps_green_checks_from_becoming_ready(monkeypatch, tmp_path, status):
    # codex-skills#970: CI queued for a runner had no check runs while finished checks were all green.
    pr = sample_pr()
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *args, **kwargs: dict(pr))
    monkeypatch.setattr(gh_pr_watch, "load_state", lambda path: ({}, True))
    monkeypatch.setattr(gh_pr_watch, "get_authenticated_login", lambda reader=None: "octocat")
    monkeypatch.setattr(gh_pr_watch, "fetch_new_review_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        gh_pr_watch.github_read,
        "pull_request_checks",
        lambda *args, **kwargs: {
            "headSha": pr["head_sha"],
            "summary": {
                "checkRunCount": 8, "statusCount": 0, "failingCount": 0, "pendingCount": 0,
                "countsComplete": True, "countsAreLowerBounds": False, "unavailableComponents": [],
            },
        },
    )
    runs = [
        {"id": 1, "name": "CodeQL", "head_sha": pr["head_sha"], "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "CI", "head_sha": pr["head_sha"], "status": status, "conclusion": None},
        {"id": 3, "name": "CI", "head_sha": "older", "status": "completed", "conclusion": "success"},
    ]
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *args, **kwargs: runs)
    monkeypatch.setattr(gh_pr_watch, "failed_jobs_from_workflow_runs", lambda *args, **kwargs: [])
    monkeypatch.setattr(gh_pr_watch, "save_state", lambda *args, **kwargs: None)

    snapshot, _ = gh_pr_watch.collect_snapshot(argparse.Namespace(
        pr="123", repo=None, state_file=str(tmp_path / "state.json"), max_flaky_retries=3
    ))

    assert snapshot["checks"]["all_terminal"] is False
    assert snapshot["checks"]["unfinished_workflow_run_count"] == 1
    assert "ready_to_merge" not in snapshot["actions"]

    runs[1].update(status="completed", conclusion="success")
    snapshot, _ = gh_pr_watch.collect_snapshot(argparse.Namespace(
        pr="123", repo=None, state_file=str(tmp_path / "state.json"), max_flaky_retries=3
    ))

    assert snapshot["checks"]["all_terminal"] is True
    assert snapshot["actions"] == ["ready_to_merge"]


def test_collect_snapshot_emits_review_request_and_session_degradation(monkeypatch, tmp_path):
    pr = sample_pr()
    pr["metadata_availability"]["review_decision"] = False

    reader = ReviewReader(
        degraded_reasons=[{"component": "actor", "code": "actor_changed"}]
    )
    reader.requests = [{"step": "review_readiness", "ok": True, "bucket": "graphql"}]
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *args, **kwargs: pr)
    monkeypatch.setattr(gh_pr_watch, "load_state", lambda path: ({}, True))
    monkeypatch.setattr(gh_pr_watch, "watcher_reader", lambda: reader)
    monkeypatch.setattr(gh_pr_watch, "get_authenticated_login", lambda active_reader=None: "octocat")
    monkeypatch.setattr(gh_pr_watch, "fetch_new_review_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        gh_pr_watch.github_read,
        "pull_request_checks",
        lambda *args, **kwargs: {"headSha": pr["head_sha"], "summary": {}, "_watcher_diagnostic": {}},
    )
    monkeypatch.setattr(gh_pr_watch, "summarize_checks", lambda *args, **kwargs: sample_checks())
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *args, **kwargs: [])
    monkeypatch.setattr(gh_pr_watch, "failed_runs_from_workflow_runs", lambda *args, **kwargs: [])
    monkeypatch.setattr(gh_pr_watch, "failed_jobs_from_workflow_runs", lambda *args, **kwargs: [])
    monkeypatch.setattr(gh_pr_watch, "save_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gh_pr_watch,
        "query_review_readiness",
        lambda *args, **kwargs: ({
            "status": "unknown", "source": "transport", "requirement": "unknown"
        }, reader.requests[-1]),
    )

    snapshot, _ = gh_pr_watch.collect_snapshot(argparse.Namespace(
        pr="123", repo=None, state_file=str(tmp_path / "state.json"), max_flaky_retries=3
    ))
    review_diagnostic = snapshot["read_diagnostics"]["review"]
    assert review_diagnostic["request"] == reader.requests[-1]
    assert review_diagnostic["readiness"] == {
        "status": "unknown", "source": "transport", "requirement": "unknown"
    }
    assert review_diagnostic["degradedReasons"] == reader.degraded_reasons


def test_recommend_actions_prioritizes_review_comments():
    actions = gh_pr_watch.recommend_actions(
        sample_pr(),
        sample_checks(failed_count=1),
        [{"run_id": 99}],
        [],
        [{"kind": "review_comment", "id": "1"}],
        0,
        3,
    )

    assert actions == [
        "process_review_comment",
        "diagnose_ci_failure",
        "retry_failed_checks",
    ]


@pytest.mark.parametrize(
    "terminal_fields",
    [
        {"merged": True, "closed": True, "state": "MERGED"},
        {"merged": False, "closed": True, "state": "CLOSED"},
    ],
)
def test_recommend_actions_stops_for_merged_or_closed_pr(terminal_fields):
    pr = {**sample_pr(), **terminal_fields}

    assert gh_pr_watch.recommend_actions(
        pr,
        sample_checks(),
        [],
        [],
        [],
        0,
        3,
    ) == ["stop_pr_closed"]


def test_fetch_new_review_items_surfaces_unknown_external_human(monkeypatch):
    external_comment = {
        "id": 10,
        "user": {"login": "new-contributor"},
        "author_association": "NONE",
        "created_at": "2026-07-26T12:00:00Z",
        "body": "I can help test this.",
        "html_url": "https://github.com/openai/codex/pull/123#issuecomment-10",
    }

    def fake_api(endpoint, repo=None):
        assert repo == "openai/codex"
        if endpoint.endswith("/issues/123/comments"):
            return [external_comment]
        return []

    monkeypatch.setattr(gh_pr_watch, "gh_api_list_paginated", fake_api)

    state = {}
    items = gh_pr_watch.fetch_new_review_items(
        sample_pr(),
        state,
        fresh_state=True,
        authenticated_login="fixture-automation",
    )

    assert [(item["author"], item["author_association"]) for item in items] == [
        ("new-contributor", "NONE")
    ]


def test_fetch_new_review_items_ignores_own_automation_comment(monkeypatch):
    automation_comment = {
        "id": 10,
        "user": {"login": "fixture-automation"},
        "author_association": "COLLABORATOR",
        "created_at": "2026-07-26T12:00:00Z",
        "body": "Completed through PR #123.",
        "html_url": "https://github.com/openai/codex/pull/123#issuecomment-10",
    }

    def fake_api(endpoint, repo=None):
        assert repo == "openai/codex"
        if endpoint.endswith("/issues/123/comments"):
            return [automation_comment]
        return []

    monkeypatch.setattr(gh_pr_watch, "gh_api_list_paginated", fake_api)

    assert (
        gh_pr_watch.fetch_new_review_items(
            sample_pr(),
            {},
            fresh_state=True,
            authenticated_login="fixture-automation",
        )
        == []
    )


def owner_feedback_comment(**overrides) -> dict[str, Any]:
    decision = {
        "schema_version": 1,
        "record_id": "product-review-example-pr-123-abc",
        "product": "example-site",
        "repository": "openai/codex",
        "pull_request_number": 123,
        "head_sha": "b" * 40,
        "preview_url": "https://preview.example.invalid/",
        "decision": "changes_requested",
        "reason": "Fix the price.\n\nKeep USB-C, café, and the second paragraph.",
        "owner_github_login": "site-owner",
        "owner_github_id": "71",
        "decided_at": "2026-09-26T12:00:00Z",
        "review_url": "https://launchplane.example/ui/owner-review?repository=openai%2Fcodex&pull_request=123&decision_id=product-review-example-pr-123-abc",
    }
    decision.update(overrides)
    if "review_url" not in overrides:
        decision["review_url"] = f"https://launchplane.example/ui/owner-review?repository=openai%2Fcodex&pull_request=123&decision_id={decision['record_id']}"
    return {
        "id": 10,
        "user": {"login": "fixture-service", "id": 99},
        "author_association": "COLLABORATOR",
        "created_at": "2026-09-26T12:00:00Z",
        "body": (
            f"<!-- launchplane:product-review:{decision['record_id']} -->\n"
            f"<!-- launchplane:owner-review {json.dumps(decision)} -->\n"
            f"Owner feedback:\n{decision['reason']}"
        ),
        "html_url": "https://github.com/openai/codex/pull/123#issuecomment-10",
    }


def mock_owner_feedback(monkeypatch, comments):
    monkeypatch.setattr(gh_pr_watch, "configured_bot_logins", lambda: frozenset({"fixture-service", "fixture-app[bot]"}))
    monkeypatch.setattr(
        gh_pr_watch, "gh_api_list_paginated",
        lambda endpoint, **_: comments if "/issues/" in endpoint else [],
    )
    saved = {
        decision["record_id"]: {**decision, "feedback_url": comment["html_url"]}
        for comment in comments
        for decision in [json.loads(comment["body"].splitlines()[1][len("<!-- launchplane:owner-review "):-4])]
    }
    monkeypatch.setattr(gh_pr_watch, "read_launchplane_owner_review", lambda pr, record_id="": saved[record_id] if record_id else max(saved.values(), key=lambda decision: gh_pr_watch.datetime.fromisoformat(decision["decided_at"])))


def test_owner_feedback_is_recovered_after_restart_without_repeating_new_event(monkeypatch, tmp_path):
    comment = owner_feedback_comment()
    mock_owner_feedback(monkeypatch, [comment])
    pr = {**sample_pr(), "head_sha": "b" * 40}
    state = {}

    first = gh_pr_watch.fetch_new_review_items(pr, state, True, authenticated_login="fixture-service")
    assert len(first) == 1
    assert first[0]["source"] == "launchplane_owner_review"
    assert first[0]["owner_review"]["reason"] == "Fix the price.\n\nKeep USB-C, café, and the second paragraph."
    assert first[0]["owner_review"]["matches_current_head"] is True
    path = tmp_path / "watch-state.json"
    gh_pr_watch.save_state(path, state)
    resumed, fresh = gh_pr_watch.load_state(path)

    assert gh_pr_watch.fetch_new_review_items(pr, resumed, fresh, authenticated_login="fixture-service") == []
    assert resumed["owner_review_items"] == first
    assert "address_owner_review_changes" in gh_pr_watch.recommend_actions(
        pr, sample_checks(), [], [], [], 0, 3, resumed["owner_review_items"]
    )
    assert "ready_to_merge" not in gh_pr_watch.recommend_actions(
        pr, sample_checks(), [], [], [], 0, 3, resumed["owner_review_items"]
    )


def test_earlier_owner_feedback_is_history_for_a_new_revision(monkeypatch):
    mock_owner_feedback(monkeypatch, [owner_feedback_comment(decision="accepted")])
    state = {}
    items = gh_pr_watch.fetch_new_review_items({**sample_pr(), "head_sha": "c" * 40}, state, True)
    assert items[0]["owner_review"]["matches_current_head"] is False
    assert items[0]["owner_review"]["head_sha"] == "b" * 40
    # Historical acceptance cannot supply missing GitHub review evidence.
    pr = {**sample_pr(), "head_sha": "c" * 40, "review_requirement": "pending", "review_decision": "REVIEW_REQUIRED"}
    actions = gh_pr_watch.recommend_actions(pr, sample_checks(), [], [], [], 0, 3, items)
    assert "awaiting_review" in actions
    assert "ready_to_merge" not in actions


@pytest.mark.parametrize("change", [
    {"repository": "other/repo"},
    {"pull_request_number": 124},
    {"head_sha": "unknown"},
    {"review_url": "https://example.com/unrelated"},
    {"decision": "changes_requested", "reason": ""},
])
def test_incomplete_or_wrong_subject_owner_feedback_is_an_explicit_read_failure(monkeypatch, change):
    mock_owner_feedback(monkeypatch, [owner_feedback_comment(**change)])
    state = {}
    assert gh_pr_watch.fetch_new_review_items(sample_pr(), state, True) == []
    assert "Owner feedback" in state["owner_review_errors"][0]["error"]


def test_projection_verifies_the_saved_receipt_without_a_publisher_allowlist(monkeypatch):
    comment = owner_feedback_comment()
    comment["user"] = {"login": "unconfigured-publisher[bot]", "id": 99}
    mock_owner_feedback(monkeypatch, [comment])
    assert len(gh_pr_watch.fetch_new_review_items(sample_pr(), {}, True)) == 1
    comment["user"] = {"login": "unconfigured-publisher[bot]", "id": None}
    state = {}
    assert gh_pr_watch.fetch_new_review_items(sample_pr(), state, True) == []
    assert "Unverified" in state["owner_review_errors"][0]["error"]


def test_receipt_repository_casing_does_not_break_verification(monkeypatch):
    comment = owner_feedback_comment()
    mock_owner_feedback(monkeypatch, [comment])
    comment["html_url"] = comment["html_url"].replace("openai/codex", "OpenAI/Codex")
    assert len(gh_pr_watch.fetch_new_review_items(sample_pr(), {}, True)) == 1


def test_latest_pending_owner_decision_is_read_before_an_older_acceptance_can_clear_readiness(monkeypatch):
    accepted = owner_feedback_comment(decision="accepted")
    mock_owner_feedback(monkeypatch, [accepted])
    saved_reader = gh_pr_watch.read_launchplane_owner_review
    pending = {**saved_reader(sample_pr()), "record_id": "later", "decision": "changes_requested",
        "reason": "This newer request must be read in full.", "decided_at": "2026-09-26T12:01:00Z", "feedback_url": ""}
    monkeypatch.setattr(gh_pr_watch, "read_launchplane_owner_review", lambda target, record_id="": saved_reader(target, record_id) if record_id else pending)
    pr = {**sample_pr(), "head_sha": "b" * 40}
    state = {}
    gh_pr_watch.fetch_new_review_items(pr, state, True)
    assert state["owner_review_items"][-1]["owner_review"]["reason"] == pending["reason"]
    actions = gh_pr_watch.recommend_actions(pr, sample_checks(), [], [], [], 0, 3, state["owner_review_items"])
    assert "address_owner_review_changes" in actions
    assert "owner_feedback_delivery_pending" in actions
    assert "ready_to_merge" not in actions


def test_newer_owner_acceptance_supersedes_requested_changes_without_granting_merge_authority(monkeypatch):
    changes = owner_feedback_comment()
    accepted = owner_feedback_comment(record_id="product-review-example-pr-123-accepted", decision="accepted", decided_at="2026-09-26T12:00:00.5Z")
    accepted["id"] = 11
    accepted["html_url"] = "https://github.com/openai/codex/pull/123#issuecomment-11"
    mock_owner_feedback(monkeypatch, [changes, accepted])
    pr = {**sample_pr(), "head_sha": "b" * 40, "draft": True}
    state = {}
    gh_pr_watch.fetch_new_review_items(pr, state, True)
    actions = gh_pr_watch.recommend_actions(pr, sample_checks(), [], [], [], 0, 3, state["owner_review_items"])
    assert "address_owner_review_changes" not in actions
    assert "ready_to_merge" not in actions


def test_own_automation_cannot_forge_owner_feedback_or_copy_a_receipt(monkeypatch):
    legitimate = owner_feedback_comment()
    mock_owner_feedback(monkeypatch, [legitimate])
    forged = owner_feedback_comment(reason="This did not come from the Owner.")
    monkeypatch.setattr(gh_pr_watch, "gh_api_list_paginated", lambda endpoint, **_: [forged] if "/issues/" in endpoint else [])
    state = {}
    assert gh_pr_watch.fetch_new_review_items(sample_pr(), state, True, authenticated_login="fixture-service") == []
    assert "saved decision or delivery receipt" in state["owner_review_errors"][0]["error"]
    forged["body"] = legitimate["body"]
    forged["id"] = 11
    forged["html_url"] = "https://github.com/openai/codex/pull/123#issuecomment-11"
    assert gh_pr_watch.fetch_new_review_items(sample_pr(), state, True) == []
    assert "saved decision or delivery receipt" in state["owner_review_errors"][0]["error"]


def test_launchplane_read_failure_does_not_erase_retained_owner_feedback(monkeypatch):
    mock_owner_feedback(monkeypatch, [owner_feedback_comment()])
    state = {}
    gh_pr_watch.fetch_new_review_items(sample_pr(), state, True)
    original = json.loads(json.dumps(state))
    def unavailable(*_):
        raise gh_pr_watch.GhCommandError("Launchplane read unavailable")
    monkeypatch.setattr(gh_pr_watch, "read_launchplane_owner_review", unavailable)
    assert gh_pr_watch.fetch_new_review_items(sample_pr(), state, False) == []
    assert state["owner_review_items"][0]["owner_review"] == original["owner_review_items"][0]["owner_review"]
    assert state["owner_review_items"][0]["verification_status"] == "unavailable"
    assert state["owner_review_errors"][0]["error"] == "Launchplane read unavailable"
    actions = gh_pr_watch.recommend_actions(sample_pr(), sample_checks(), [], [], [], 0, 3,
        state["owner_review_items"], state["owner_review_errors"])
    assert "owner_review_verification_unavailable" in actions
    assert "ready_to_merge" not in actions
    failed = {**sample_checks(), "failed_count": 1}
    assert "diagnose_ci_failure" in gh_pr_watch.recommend_actions(sample_pr(), failed, [], [], [], 0, 3,
        state["owner_review_items"], state["owner_review_errors"])
    closed = {**sample_pr(), "closed": True}
    assert "stop_pr_closed" in gh_pr_watch.recommend_actions(closed, sample_checks(), [], [], [], 0, 3,
        state["owner_review_items"], state["owner_review_errors"])
    mock_owner_feedback(monkeypatch, [owner_feedback_comment()])
    assert gh_pr_watch.fetch_new_review_items(sample_pr(), state, False) == []
    assert state["owner_review_errors"] == []
    assert state["owner_review_items"][0]["verification_status"] == "verified"


def test_recommend_actions_ignores_stale_failed_jobs_from_completed_runs():
    actions = gh_pr_watch.recommend_actions(
        sample_pr(),
        sample_checks(),
        [],
        [
            {
                "run_id": 99,
                "run_status": "completed",
                "run_conclusion": "failure",
                "job_name": "unit tests",
                "conclusion": "failure",
            }
        ],
        [],
        3,
        3,
    )

    assert actions == ["ready_to_merge"]


def test_recommend_actions_diagnoses_failed_jobs_from_active_runs():
    actions = gh_pr_watch.recommend_actions(
        sample_pr(),
        sample_checks(all_terminal=False, pending_count=1),
        [],
        [
            {
                "run_id": 99,
                "run_status": "in_progress",
                "run_conclusion": "",
                "job_name": "unit tests",
                "conclusion": "failure",
            }
        ],
        [],
        0,
        3,
    )

    assert actions == ["diagnose_ci_failure"]


def test_run_watch_keeps_polling_open_ready_to_merge_pr(monkeypatch):
    sleeps = []
    events = []
    snapshot = {
        "pr": sample_pr(),
        "checks": sample_checks(),
        "failed_runs": [],
        "failed_jobs": [],
        "new_review_items": [],
        "actions": ["ready_to_merge"],
        "retry_state": {
            "current_sha_retries_used": 0,
            "max_flaky_retries": 3,
        },
    }

    monkeypatch.setattr(
        gh_pr_watch,
        "collect_snapshot",
        lambda args: (snapshot, Path("/tmp/pr-babysit-state.json")),
    )
    monkeypatch.setattr(
        gh_pr_watch,
        "print_event",
        lambda event, payload: events.append((event, payload)),
    )

    class StopWatch(Exception):
        pass

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise StopWatch

    monkeypatch.setattr(gh_pr_watch.time, "sleep", fake_sleep)

    with pytest.raises(StopWatch):
        gh_pr_watch.run_watch(argparse.Namespace(poll_seconds=30))

    assert sleeps == [30, 300]
    assert [event for event, _ in events] == ["snapshot", "snapshot"]


def test_default_state_file_uses_neutral_prefix():
    assert gh_pr_watch.default_state_file_for(sample_pr()) == Path(
        "/tmp/pr-babysit-openai-codex-pr123.json"
    )


def test_failed_jobs_include_direct_logs_endpoint(monkeypatch):
    jobs_by_run = {
        99: [
            {
                "id": 555,
                "name": "unit tests",
                "status": "completed",
                "conclusion": "failure",
                "html_url": "https://github.com/openai/codex/actions/runs/99/job/555",
            },
            {
                "id": 556,
                "name": "lint",
                "status": "completed",
                "conclusion": "success",
            },
        ]
    }

    monkeypatch.setattr(
        gh_pr_watch,
        "get_jobs_for_run",
        lambda repo, run_id: jobs_by_run[run_id],
    )

    failed_jobs = gh_pr_watch.failed_jobs_from_workflow_runs(
        "openai/codex",
        [
            {
                "id": 99,
                "name": "CI",
                "status": "completed",
                "conclusion": "failure",
                "head_sha": "abc123",
            }
        ],
        "abc123",
    )

    assert failed_jobs == [
        {
            "run_id": 99,
            "workflow_name": "CI",
                "run_status": "completed",
                "run_conclusion": "failure",
            "job_id": 555,
            "job_name": "unit tests",
            "status": "completed",
            "conclusion": "failure",
            "html_url": "https://github.com/openai/codex/actions/runs/99/job/555",
            "logs_endpoint": "repos/openai/codex/actions/jobs/555/logs",
        }
    ]


def retry_snapshot(monkeypatch, tmp_path, runs, jobs):
    state_path = tmp_path / "retry-state.json"
    snapshot = {
        "pr": sample_pr(),
        "checks": sample_checks(failed_count=1),
        "failed_runs": runs,
        "failed_jobs": jobs,
        "retry_state": {"current_sha_retries_used": 0, "max_flaky_retries": 3},
    }
    monkeypatch.setattr(gh_pr_watch, "collect_snapshot", lambda args: (snapshot, state_path))
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *args, **kwargs: [
        {"id": run["run_id"], "head_sha": "abc123", "run_attempt": run["run_attempt"],
         "status": run["status"]} for run in runs
    ])
    return snapshot, state_path


def failed_run(run_id, conclusion="failure"):
    return {"run_id": run_id, "run_attempt": 1, "status": "completed", "conclusion": conclusion}


def failed_job(run_id, conclusion="failure"):
    return {"run_id": run_id, "status": "completed", "conclusion": conclusion}


def test_retry_skips_cancelled_notice_without_failed_jobs(monkeypatch, tmp_path):
    _, path = retry_snapshot(monkeypatch, tmp_path,
                             [failed_run(1), failed_run(2, "cancelled")], [failed_job(1)])
    writes = []
    monkeypatch.setattr(gh_pr_watch, "gh_text", lambda args, **kwargs: writes.append(args))

    result = gh_pr_watch.retry_failed_now(argparse.Namespace())

    assert writes == [["run", "rerun", "1", "--failed"]]
    assert result["rerun_run_ids"] == [1]
    assert result["skipped_run_ids"] == [2]
    assert result["retries_used"] == 1
    assert gh_pr_watch.current_retry_count(gh_pr_watch.load_state(path)[0], "abc123") == 1


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "success", "action_required"])
def test_retry_needs_actual_failed_job(monkeypatch, tmp_path, conclusion):
    retry_snapshot(monkeypatch, tmp_path, [failed_run(2, "cancelled")], [failed_job(2, conclusion)])
    monkeypatch.setattr(gh_pr_watch, "gh_text", lambda *args, **kwargs: pytest.fail("unexpected write"))

    result = gh_pr_watch.retry_failed_now(argparse.Namespace())

    assert result["reason"] == "no_rerunnable_failed_jobs"
    assert result["rerun_attempted"] is False
    assert result["retries_used"] == 0


@pytest.mark.parametrize("order", [[1, 2, 3], [2, 1, 3], [2]])
def test_nonretryable_rejection_preserves_success_and_continues(monkeypatch, tmp_path, order):
    _, path = retry_snapshot(monkeypatch, tmp_path,
                             [failed_run(n) for n in order], [failed_job(n) for n in order])

    def rerun(args, **_kwargs):
        if args[2] == "2":
            raise gh_pr_watch.GhCommandError("HTTP 422: This workflow run cannot be retried")

    monkeypatch.setattr(gh_pr_watch, "gh_text", rerun)
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    expected = [n for n in order if n != 2]

    assert result["rerun_run_ids"] == expected
    assert result["rerun_count"] == len(expected)
    assert result["skipped_run_ids"] == [2]
    assert result["retries_used"] == int(bool(expected))
    state = gh_pr_watch.load_state(path)[0]
    assert set(state["pending_reruns_by_sha"]["abc123"]) == {str(n) for n in expected}


@pytest.mark.parametrize("error", ["connection reset", "HTTP 503: service unavailable"])
def test_ambiguous_error_preserves_progress_and_blocks_replay(monkeypatch, tmp_path, error):
    snapshot, path = retry_snapshot(monkeypatch, tmp_path,
                                    [failed_run(1), failed_run(2), failed_run(3)],
                                    [failed_job(1), failed_job(2), failed_job(3)])
    writes = []

    def rerun(args, **_kwargs):
        writes.append(args[2])
        if args[2] == "2":
            raise gh_pr_watch.GhCommandError(error)

    monkeypatch.setattr(gh_pr_watch, "gh_text", rerun)
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert writes == ["1", "2"]
    assert result["reason"] == "rerun_outcome_unknown"
    assert result["rerun_run_ids"] == [1]
    assert result["rerun_count"] == 1
    assert result["unknown_run_id"] == 2
    assert result["retries_used"] == 1
    state = gh_pr_watch.load_state(path)[0]
    pending = gh_pr_watch.reconcile_pending_reruns(state, "abc123", [
        {"id": 1, "head_sha": "abc123", "run_attempt": 2},
        {"id": 2, "head_sha": "abc123", "run_attempt": 1},
    ])
    assert pending == {"2": {"run_attempt": 1, "outcome": "submitting"}}
    snapshot["retry_state"]["pending_run_ids"] = list(pending)
    assert gh_pr_watch.retry_failed_now(argparse.Namespace())["reason"] == "rerun_outcome_pending"
    assert writes == ["1", "2"]
    assert gh_pr_watch.reconcile_pending_reruns(state, "abc123", [
        {"id": 2, "head_sha": "abc123", "run_attempt": 2},
    ]) == {}


def test_crash_after_write_intent_cannot_replay(monkeypatch, tmp_path):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])

    def crash(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(gh_pr_watch, "gh_text", crash)
    with pytest.raises(KeyboardInterrupt):
        gh_pr_watch.retry_failed_now(argparse.Namespace())
    state = gh_pr_watch.load_state(path)[0]
    assert gh_pr_watch.current_retry_count(state, "abc123") == 1
    assert gh_pr_watch.reconcile_pending_reruns(state, "abc123", []) == {"1": {"run_attempt": 1, "outcome": "submitting"}}


def test_cancelled_run_with_failed_job_is_rerunnable(monkeypatch, tmp_path):
    retry_snapshot(monkeypatch, tmp_path, [failed_run(1, "cancelled")], [failed_job(1, "timed_out")])
    writes = []
    monkeypatch.setattr(gh_pr_watch, "gh_text", lambda args, **kwargs: writes.append(args))
    assert gh_pr_watch.retry_failed_now(argparse.Namespace())["rerun_run_ids"] == [1]
    assert len(writes) == 1



@pytest.mark.parametrize("run_attempt, expected_action", [(1, "check_rerun_outcome"), (2, "retry_failed_checks")])
@pytest.mark.parametrize("max_retries", [1, 3])
def test_snapshot_reconciles_write_intent_before_recommending_retry(monkeypatch, tmp_path, run_attempt, expected_action, max_retries):
    path = tmp_path / "state.json"
    gh_pr_watch.save_state(path, {"pending_reruns_by_sha": {"abc123": {"1": {"run_attempt": 1, "outcome": "submitting"}}},
                                  "retries_by_sha": {"abc123": 1}})
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *args, **kwargs: sample_pr())
    monkeypatch.setattr(gh_pr_watch, "get_authenticated_login", lambda reader=None: "octocat")
    monkeypatch.setattr(gh_pr_watch, "fetch_new_review_items", lambda *args, **kwargs: [])
    monkeypatch.setattr(gh_pr_watch.github_read, "pull_request_checks", lambda *args, **kwargs: {})
    monkeypatch.setattr(gh_pr_watch, "summarize_checks", lambda *args, **kwargs: sample_checks(failed_count=1))
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *args, **kwargs: [
        {"id": 1, "head_sha": "abc123", "run_attempt": run_attempt,
         "status": "completed", "conclusion": "failure"},
    ])
    monkeypatch.setattr(gh_pr_watch, "failed_jobs_from_workflow_runs", lambda *args, **kwargs: [failed_job(1)])
    args = argparse.Namespace(pr="123", repo=None, state_file=str(path), max_flaky_retries=max_retries)

    snapshot, _ = gh_pr_watch.collect_snapshot(args)

    if run_attempt == 2 and max_retries == 1:
        expected_action = "stop_exhausted_retries"
    assert ("stop_unknown_rerun" in snapshot["actions"]) == (run_attempt == 1)
    assert ("stop_exhausted_retries" in snapshot["actions"]) == (run_attempt == 2 and max_retries == 1)
    assert expected_action in snapshot["actions"]
    assert ("retry_failed_checks" in snapshot["actions"]) == (run_attempt == 2 and max_retries > 1)
    assert snapshot["retry_state"]["pending_run_ids"] == (["1"] if run_attempt == 1 else [])
    assert snapshot["retry_state"]["current_sha_retries_used"] == 1


def test_retry_cli_returns_partial_progress_with_failure_exit(monkeypatch, capsys):
    monkeypatch.setattr(gh_pr_watch, "parse_args", lambda: argparse.Namespace(retry_failed_now=True))
    monkeypatch.setattr(gh_pr_watch, "retry_failed_now", lambda args: {
        "rerun_run_ids": [1], "error": "connection reset", "reason": "rerun_outcome_unknown",
    })
    assert gh_pr_watch.main() == 1
    assert json.loads(capsys.readouterr().out)["rerun_run_ids"] == [1]


@pytest.mark.parametrize("error", ["HTTP 403: denied", "HTTP 422: invalid request",
                                   "run 1 cannot be rerun; Resource not accessible by integration",
                                   "failed to get run: HTTP 503: service unavailable"])
def test_confirmed_rejection_releases_intent_and_budget(monkeypatch, tmp_path, error):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])

    def reject(*_args, **_kwargs):
        raise gh_pr_watch.GhCommandError(error)

    monkeypatch.setattr(gh_pr_watch, "gh_text", reject)
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert result["reason"] == "rerun_rejected"
    assert result["error"] == error
    assert result["retries_used"] == 0
    assert gh_pr_watch.load_state(path)[0]["pending_reruns_by_sha"]["abc123"] == {}


def test_gh_rewritten_nonretryable_rejection_is_skipped(monkeypatch, tmp_path):
    retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])

    def reject(*_args, **_kwargs):
        raise gh_pr_watch.GhCommandError("run 1 cannot be rerun; This workflow run cannot be retried")

    monkeypatch.setattr(gh_pr_watch, "gh_text", reject)
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert result["skipped_run_ids"] == [1]
    assert result["retries_used"] == 0
    assert "error" not in result


def test_concurrent_retries_do_not_replay_unknown_write(monkeypatch, tmp_path):
    snapshot, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    writes_path = tmp_path / "writes.txt"

    def collect(_args):
        barrier.wait(timeout=10)
        return snapshot, path

    def submit(*_args, **_kwargs):
        with writes_path.open("a") as stream:
            stream.write("submitted\n")
        raise gh_pr_watch.GhCommandError("connection reset")

    monkeypatch.setattr(gh_pr_watch, "collect_snapshot", collect)
    monkeypatch.setattr(gh_pr_watch, "gh_text", submit)

    def worker():
        results.put(gh_pr_watch.retry_failed_now(argparse.Namespace())["reason"])

    workers = [ctx.Process(target=worker) for _ in range(2)]
    try:
        for process in workers:
            process.start()
        for process in workers:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert {results.get(timeout=2), results.get(timeout=2)} == {
            "rerun_outcome_unknown", "rerun_outcome_pending",
        }
        assert writes_path.read_text() == "submitted\n"
        assert gh_pr_watch.current_retry_count(gh_pr_watch.load_state(path)[0], "abc123") == 1
    finally:
        for process in workers:
            if process.is_alive():
                process.terminate()
                process.join()
        results.close()


def test_snapshot_cannot_overwrite_retry_intent(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    ctx = multiprocessing.get_context("fork")
    snapshot_reading = ctx.Event()
    finish_read = ctx.Event()
    retry_started = ctx.Event()
    snapshot = {"pr": sample_pr(), "retry_state": {"max_flaky_retries": 3}}
    args = argparse.Namespace(pr="123", repo=None, state_file=str(path), max_flaky_retries=3)
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *a, **kw: sample_pr())
    monkeypatch.setattr(gh_pr_watch, "get_authenticated_login", lambda reader=None: "octocat")
    monkeypatch.setattr(gh_pr_watch, "fetch_new_review_items", lambda *a, **kw: [])
    monkeypatch.setattr(gh_pr_watch.github_read, "pull_request_checks", lambda *a, **kw: {})
    monkeypatch.setattr(gh_pr_watch, "summarize_checks", lambda *a, **kw: sample_checks(failed_count=1))
    monkeypatch.setattr(gh_pr_watch, "failed_jobs_from_workflow_runs", lambda *a, **kw: [])

    def runs(*_args, **_kwargs):
        snapshot_reading.set()
        assert finish_read.wait(timeout=10)
        return [{"id": 1, "head_sha": "abc123", "run_attempt": 1,
                 "status": "completed", "conclusion": "failure"}]

    def unknown(*_args, **_kwargs):
        raise gh_pr_watch.GhCommandError("connection reset")

    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", runs)
    monkeypatch.setattr(gh_pr_watch, "gh_text", unknown)

    def retry():
        retry_started.set()
        with gh_pr_watch.state_lock(path):
            gh_pr_watch.submit_locked_reruns(snapshot, path, {
                "reason": None, "rerun_run_ids": [], "skipped_run_ids": [],
            }, [failed_run(1)])

    watcher = ctx.Process(target=lambda: gh_pr_watch.collect_snapshot(args))
    retry_process = ctx.Process(target=retry)
    try:
        watcher.start()
        assert snapshot_reading.wait(timeout=10)
        retry_process.start()
        assert retry_started.wait(timeout=10)
        finish_read.set()
        for process in (watcher, retry_process):
            process.join(timeout=15)
            assert process.exitcode == 0
        state = gh_pr_watch.load_state(path)[0]
        assert state["pending_reruns_by_sha"]["abc123"]["1"]["outcome"] == "submitting"
        assert gh_pr_watch.current_retry_count(state, "abc123") == 1
    finally:
        finish_read.set()
        for process in (watcher, retry_process):
            if process.is_alive():
                process.terminate()
                process.join()




@pytest.mark.parametrize("fresh_run", [
    {"id": 1, "head_sha": "abc123", "run_attempt": 2, "status": "completed"},
    {"id": 1, "head_sha": "abc123", "run_attempt": 1, "status": "in_progress"},
    {"id": 1, "head_sha": "other", "run_attempt": 1, "status": "completed"},
])
def test_retry_rechecks_run_after_snapshot_lock_gap(monkeypatch, tmp_path, fresh_run):
    retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *a, **kw: [fresh_run])
    monkeypatch.setattr(gh_pr_watch, "gh_text", lambda *a, **kw: pytest.fail("stale write"))
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert result["skipped_run_ids"] == [1]
    assert result["retries_used"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
