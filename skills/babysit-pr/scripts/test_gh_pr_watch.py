#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pytest==9.1.1",
#     "PyYAML==6.0.3",
# ]
# ///

import argparse
import base64
import json
import multiprocessing
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Any

os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/codex-skills-test.env"

import gh_pr_watch
import pytest


@pytest.fixture(autouse=True)
def isolated_github_state(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_RETRY_STATE_DIR", str(tmp_path / "retry"))
    monkeypatch.setenv("GITHUB_READ_CACHE_DIR", str(tmp_path / "cache"))


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
        self.results = []
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

    def fake_popen(cmd, stdout, stderr, text, start_new_session, env):
        assert stdout == subprocess.PIPE
        assert stderr == subprocess.PIPE
        assert text is True
        assert start_new_session is True
        assert float(env["GITHUB_RETRY_DEADLINE_AT"]) <= gh_pr_watch.time.time() + gh_pr_watch.COMMAND_TIMEOUT_SECONDS
        calls.append(cmd)
        return SimpleNamespace(returncode=0, communicate=lambda timeout: ("ok\n", ""))

    monkeypatch.setattr(gh_pr_watch.subprocess, "Popen", fake_popen)

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
    reader.results = [SimpleNamespace(headers={"x-poll-interval": "90"})]
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

    assert snapshot["minimum_poll_seconds"] == 90


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

    monkeypatch.setattr(gh_pr_watch.github_read.random, "uniform", lambda _low, _high: 2.0)
    monkeypatch.setattr(gh_pr_watch.time, "sleep", fake_sleep)

    with pytest.raises(StopWatch):
        gh_pr_watch.run_watch(argparse.Namespace(poll_seconds=30))

    assert sleeps == [32, 302]
    assert [event for event, _ in events] == ["snapshot", "snapshot"]


def test_25_unchanged_pending_watchers_fit_half_an_hourly_budget(monkeypatch):
    """Replay the real watch loop and HTTP transport with no free 304s.

    Seven mutable REST representations per poll is deliberately pessimistic.
    This qualifies local poll traffic, not remote controller or write traffic.
    """
    clock = [0.0]
    costs = [0]
    monkeypatch.setenv("CODEX_AUTOMATION_LOGIN", "fixture-bot")
    monkeypatch.setattr(gh_pr_watch.time, "time", lambda: clock[0])
    monkeypatch.setattr(gh_pr_watch.github_read.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(gh_pr_watch.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(gh_pr_watch, "print_event", lambda *_args: None)
    monkeypatch.setattr(gh_pr_watch.github_api, "default_retry_runtime", lambda: gh_pr_watch.github_api.RetryRuntime(
        now=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds), jitter=lambda _seconds: 0
    ))

    def transport(command, **_kwargs):
        assert "api" in command
        costs[0] += 1
        return subprocess.CompletedProcess(command, 0, stdout=(
            'HTTP/2 200\ncontent-type: application/json\n\n' + json.dumps({"revision": costs[0]})
        ).encode(), stderr=b"GitHub automation actor: fixture-bot (source: github_app)")

    monkeypatch.setattr(gh_pr_watch.subprocess, "run", transport)
    pending = {"pr": sample_pr(), "checks": sample_checks(all_terminal=False, pending_count=1),
               "new_review_items": [], "actions": ["idle"]}
    paths = ["pulls/7", "issues/7/comments", "pulls/7/comments", "pulls/7/reviews",
             "commits/abc/check-runs", "commits/abc/status", "actions/runs?head_sha=abc"]

    def snapshot(_args):
        if clock[0] >= 3600:
            return {**pending, "actions": ["stop_pr_closed"]}, Path("unused")
        reader = gh_pr_watch.watcher_reader()
        for path in paths:
            reader.get_json(f"/repos/example/app/{path}", step="replay")
        return pending, Path("unused")

    monkeypatch.setattr(gh_pr_watch, "collect_snapshot", snapshot)
    totals = []
    for quiet_interval in (60, 300):
        costs[0] = 0
        for _ in range(25):
            clock[0] = 0
            assert gh_pr_watch.run_watch(argparse.Namespace(poll_seconds=60, green_poll_seconds=quiet_interval)) == 0
        totals.append(costs[0])
    baseline, optimized = totals
    assert baseline > 7750
    assert optimized < 7750 / 2
    assert optimized < baseline / 3
    print(json.dumps({"scenario": "25 unchanged pending watchers, seven mutable REST reads per poll, one hour",
                      "baseline_requests": baseline, "optimized_requests": optimized,
                      "coverage": "local watcher HTTP calls only"}))


@pytest.fixture
def watcher_transport(monkeypatch):
    """Use the real watcher reader and retry policy with offline HTTP replies."""
    clock = [1000.0]
    calls = []
    replies = []
    monkeypatch.setenv("CODEX_AUTOMATION_LOGIN", "fixture-bot")
    monkeypatch.delenv("GITHUB_RETRY_DEADLINE_AT", raising=False)
    for name, value in {"GITHUB_RETRY_DRAIN_SECONDS": "0", "GITHUB_RETRY_MAX_ATTEMPTS": "4",
                        "GITHUB_RETRY_MAX_WAIT_SECONDS": "30", "GITHUB_RETRY_BASE_BACKOFF_SECONDS": "1",
                        "GITHUB_RETRY_WAIT_SLICE_SECONDS": "1"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(gh_pr_watch.github_api, "default_retry_runtime", lambda: gh_pr_watch.github_api.RetryRuntime(
        now=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        jitter=lambda _seconds: 0,
        progress=lambda _event: None,
    ))

    def transport(command, **_kwargs):
        calls.append((clock[0], command))
        status, headers, body, _actor = replies.pop(0)
        if status == 0:
            return subprocess.CompletedProcess(command, 1, stdout=json.dumps(body).encode(), stderr=b"")
        output = f"HTTP/2 {status}\ncontent-type: application/json\n"
        output += "".join(f"{name}: {value}\n" for name, value in headers.items())
        output += "\n" + json.dumps(body)
        return subprocess.CompletedProcess(command, int(status >= 400), stdout=output.encode(),
                                           stderr=b"")

    monkeypatch.setattr(gh_pr_watch.subprocess, "run", transport)
    return SimpleNamespace(clock=clock, calls=calls, replies=replies)


@pytest.mark.parametrize("cause", ["deadline_exceeded", "invalid_credentials", "permission_denied", "actor_mismatch"])
def test_watch_main_preserves_pr_metadata_failure(monkeypatch, tmp_path, capsys, cause):
    state_path = tmp_path / "watch-state.json"
    saved_state = {"head_sha": "existing-head", "retries_by_sha": {"existing-head": 1}}
    state_path.write_text(json.dumps(saved_state))
    args = argparse.Namespace(pr="42", repo="example/repo", state_file=str(state_path),
                              watch=True, retry_failed_now=False)
    monkeypatch.setattr(gh_pr_watch, "parse_args", lambda: args)
    monkeypatch.setattr(gh_pr_watch, "PR_HELPER", str(gh_pr_watch.DEFAULT_PR_HELPER))
    payload = {"ok": False, "exit_code": 1, "operation": "github.pr.view", "attempts": 1,
               "effective_deadline": 1002, "retry_exhausted_reason": cause,
               "failure": {"cause": cause, "message": "Read failed token=fixture-private-value"}}
    calls = []

    def transport(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(gh_pr_watch.subprocess, "run", transport)
    assert gh_pr_watch.main() == 1
    captured = capsys.readouterr()
    event = json.loads(captured.out)
    assert event["event"] == "read_error"
    assert event["payload"]["ok"] is False
    assert event["payload"]["failure"]["cause"] == cause
    assert event["payload"]["attempts"] == payload["attempts"]
    assert event["payload"]["effective_deadline"] == payload["effective_deadline"]
    assert event["payload"]["retry_exhausted_reason"] == payload["retry_exhausted_reason"]
    assert "fixture-private-value" not in captured.out
    assert captured.err == ""
    assert len(calls) == 1
    assert calls[0][-4:] == ["--repo", "example/repo", "view", "42"]
    assert json.loads(state_path.read_text()) == saved_state


def test_watch_recovers_transient_read_and_continues_polling(monkeypatch, watcher_transport):
    fixture = watcher_transport
    fixture.replies.extend([
        (503, {"retry-after": "2"}, {"message": "No server is currently available to service your request"}, "fixture-bot"),
        (200, {}, {"state": "open"}, "fixture-bot"),
        (200, {}, {"state": "closed"}, "fixture-bot"),
    ])
    events = []
    readers = []

    def snapshot(_args):
        reader = gh_pr_watch.watcher_reader()
        readers.append(reader)
        # Distinct paths avoid cache coalescing on the second poll.
        body = reader.get_json(f"/repos/example/app/issues/7/comments?page={len(readers)}", step="comments")
        closed = body["state"] == "closed"
        return {"pr": {**sample_pr(), "closed": closed},
                "checks": sample_checks(all_terminal=False, pending_count=1),
                "actions": ["stop_pr_closed" if closed else "idle"]}, Path("unused")

    monkeypatch.setattr(gh_pr_watch, "collect_snapshot", snapshot)
    monkeypatch.setattr(gh_pr_watch, "print_event", lambda event, payload: events.append((event, payload)))
    monkeypatch.setattr(gh_pr_watch.github_read, "poll_delay", lambda seconds, *_a, **_kw: seconds)
    monkeypatch.setattr(gh_pr_watch.time, "sleep", lambda seconds: fixture.clock.__setitem__(0, fixture.clock[0] + seconds))

    assert gh_pr_watch.run_watch(argparse.Namespace(poll_seconds=30)) == 0
    assert [event for event, _payload in events] == ["snapshot", "snapshot", "stop"]
    assert [when for when, _command in fixture.calls] == [1000, 1002, 1032]
    assert readers[0].last_result.ok
    assert readers[0].last_result.as_dict()["attempts"] == 2
    assert readers[0].last_result.as_dict()["elapsed_wait"] == 2
    assert not readers[0].degraded_reasons


def test_watcher_deadline_returns_structured_failure_before_retry(monkeypatch, watcher_transport):
    fixture = watcher_transport
    fixture.replies.append((503, {"retry-after": "10"}, {"message": "Service Unavailable"}, "fixture-bot"))
    reader = gh_pr_watch.watcher_reader()
    monkeypatch.setenv("GITHUB_RETRY_DEADLINE_AT", str(fixture.clock[0] + 2))

    with pytest.raises(gh_pr_watch.github_read.GitHubReadError) as raised:
        reader.get_json("/repos/example/app/issues/7/comments", step="comments")

    result = raised.value.result
    assert len(fixture.calls) == 1
    assert not result.ok
    assert result.failure.cause == "deadline_exceeded"
    assert result.as_dict()["retry_exhausted_reason"] == "deadline_exceeded"
    assert result.as_dict()["effective_deadline"] == 1002
    assert fixture.clock[0] <= result.as_dict()["effective_deadline"]
    assert reader.failed_results == [result]


@pytest.mark.parametrize("watcher_first", [True, False])
def test_watcher_throttle_shares_cooldown_with_another_reader(monkeypatch, watcher_transport, watcher_first):
    fixture = watcher_transport
    fixture.replies.extend([
        (403, {"retry-after": "10"}, {"message": "You have exceeded a secondary rate limit"}, "fixture-bot"),
        (200, {}, {"name": "app"}, "fixture-bot"),
    ])
    watcher = gh_pr_watch.watcher_reader()
    other = gh_pr_watch.github_read.GitHubReader(
        gh_cmd=gh_pr_watch.GH_COMMAND, expected_actor="fixture-bot", operation="github.read.repository",
    )
    first, second = (watcher, other) if watcher_first else (other, watcher)
    monkeypatch.setenv("GITHUB_RETRY_DEADLINE_AT", str(fixture.clock[0] + 2))
    with pytest.raises(gh_pr_watch.github_read.GitHubReadError):
        first.get_json("/repos/example/app/issues/7/comments", step="comments")

    monkeypatch.delenv("GITHUB_RETRY_DEADLINE_AT")
    assert second.get_json("/repos/example/app", step="repository") == {"name": "app"}
    assert [when for when, _command in fixture.calls] == [1000, 1010]
    assert second.last_result.ok
    assert second.last_result.as_dict()["elapsed_wait"] > 0


@pytest.mark.parametrize("status,body,actor,cause", [
    (401, {"message": "Bad credentials"}, "fixture-bot", "invalid_credentials"),
    (403, {"message": "Resource not accessible by integration"}, "fixture-bot", "permission_denied"),
    (0, {"ok": False, "failure": {"cause": "actor_mismatch", "message": "Unexpected authenticated actor",
                                  "retryable": False, "fallback_eligible": False, "disposition": "stop"}},
     "unexpected-user", "actor_mismatch"),
])
def test_watcher_read_failures_stop_without_outer_retry(watcher_transport, status, body, actor, cause):
    # The terminal actor case tests propagation of an already classified refusal;
    # default read authentication is exercised through the real wrapper below.
    fixture = watcher_transport
    fixture.replies.append((status, {}, body, actor))
    reader = gh_pr_watch.watcher_reader()
    with pytest.raises(gh_pr_watch.github_read.GitHubReadError) as raised:
        reader.get_json("/repos/example/app/issues/7/comments", step="comments")

    result = raised.value.result
    assert not result.ok
    assert result.failure.cause == cause
    assert result.failure.retryable is False
    assert len(fixture.calls) == 1
    assert fixture.clock[0] == 1000
    assert fixture.calls[0][1][0] == gh_pr_watch.GH_COMMAND
    assert reader.expected_actor == "fixture-bot"


@pytest.mark.parametrize("status,headers,body,cause", [
    (503, {"retry-after": "10"}, {"message": "Service Unavailable"}, "deadline_exceeded"),
    (401, {}, {"message": "Bad credentials"}, "invalid_credentials"),
    (403, {}, {"message": "Resource not accessible by integration"}, "permission_denied"),
])
def test_watch_main_emits_structured_read_error(monkeypatch, watcher_transport, capsys, status, headers, body, cause):
    watcher_transport.replies.append((status, headers, body, "fixture-bot"))
    monkeypatch.setenv("GITHUB_RETRY_DEADLINE_AT", "1002")
    monkeypatch.setattr(gh_pr_watch, "parse_args", lambda: argparse.Namespace(watch=True, retry_failed_now=False))

    def snapshot(_args):
        reader = gh_pr_watch.watcher_reader()
        reader.get_json("/repos/example/app/issues/7/comments", step="comments")
        pytest.fail("failed read must not become a successful snapshot")

    monkeypatch.setattr(gh_pr_watch, "collect_snapshot", snapshot)
    assert gh_pr_watch.main() == 1
    captured = capsys.readouterr()
    event = json.loads(captured.out)
    assert event["event"] == "read_error"
    assert event["payload"]["ok"] is False
    assert event["payload"]["failure"]["cause"] == cause
    assert event["payload"]["effective_deadline"] == 1002
    assert len(watcher_transport.calls) == 1
    assert captured.err == ""


@pytest.mark.parametrize("status,message,cause", [
    (401, "Bad credentials", "invalid_credentials"),
    (403, "Resource not accessible by integration", "permission_denied"),
])
@pytest.mark.parametrize("entrypoint", ["reader", "main"])
def test_watcher_auth_failure_does_not_fall_back_inside_wrapper(monkeypatch, tmp_path, capsys, status, message, cause, entrypoint):
    """Run the real credential wrapper with a fake gh, never real credentials."""
    for name in ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
                 "GH_TOKEN", "GITHUB_TOKEN", "GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK",
                 "GH_WITH_ENV_TOKEN_REQUIRE_AUTOMATION_AUTH", "GH_PR_WATCH_GH",
                 "GH_WITH_ENV_TOKEN_OWN_USER", "GH_WITH_ENV_TOKEN_CLASSIFIER",
                 "GH_WITH_ENV_TOKEN_IDENTITY_HELPER", "GITHUB_RETRY_DEADLINE_AT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gh_pr_watch, "GH_COMMAND", str(gh_pr_watch.DEFAULT_GH))
    monkeypatch.setattr(gh_pr_watch, "PR_HELPER", str(gh_pr_watch.DEFAULT_PR_HELPER))
    monkeypatch.setenv("CODEX_AUTOMATION_LOGIN", "fixture-bot")
    monkeypatch.setenv("CODEX_GITHUB_TOKEN", "offline-fixture-token")
    monkeypatch.setenv("GH_WITH_ENV_TOKEN_PYTHON", sys.executable)
    monkeypatch.setenv("GITHUB_RETRY_MAX_ATTEMPTS", "4")
    call_log = tmp_path / "gh-calls"
    monkeypatch.setenv("WATCHER_FIXTURE_CALL_LOG", str(call_log))
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        'if [ -z "${GH_TOKEN:-}" ]; then\n'
        '  printf "active\\n" >> "$WATCHER_FIXTURE_CALL_LOG"\n'
        '  printf \'HTTP/2 200\\ncontent-type: application/json\\n\\n{"login":"unexpected-user"}\\n\'\n'
        "  exit 0\nfi\n"
        'printf "automation\\n" >> "$WATCHER_FIXTURE_CALL_LOG"\n'
        f"printf 'HTTP/2 {status}\\ncontent-type: application/json\\n\\n%s\\n' '{json.dumps({'message': message})}'\n"
        f"printf 'gh: HTTP {status}\\n' >&2\nexit 1\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)
    monkeypatch.setenv("GH_WITH_ENV_TOKEN_GH", str(fake_gh))
    if entrypoint == "reader":
        reader = gh_pr_watch.watcher_reader()
        with pytest.raises(gh_pr_watch.github_read.GitHubReadError) as raised:
            reader.get_json("/repos/example/app/issues/7/comments", step="comments")
        assert raised.value.result.failure.cause == cause
    else:
        monkeypatch.setattr(gh_pr_watch, "parse_args", lambda: argparse.Namespace(
            pr="42", repo="example/app", state_file=str(tmp_path / "watch-state.json"),
            watch=True, retry_failed_now=False,
        ))
        assert gh_pr_watch.main() == 1
        event = json.loads(capsys.readouterr().out)
        assert event["event"] == "read_error"
        assert event["payload"]["failure"]["cause"] == cause
        assert event["payload"]["attempts"] == 1
        assert event["payload"]["effective_deadline"] > 0
    assert call_log.read_text().splitlines() == ["automation"]


@pytest.mark.parametrize("stop_action", ["stop_pr_closed", "stop_exhausted_retries"])
@pytest.mark.parametrize("server_floor", [0, 90])
def test_run_watch_reports_chosen_sleep_interval(monkeypatch, stop_action, server_floor):
    args = argparse.Namespace(poll_seconds=17, green_poll_seconds=83)
    green: dict[str, Any] = {
        "pr": sample_pr(),
        "checks": sample_checks(),
        "new_review_items": [],
        "actions": ["ready_to_merge"],
        "minimum_poll_seconds": server_floor,
    }
    changed_green = {**green, "pr": {**sample_pr(), "head_sha": "new-head"}}
    pending = {
        **changed_green,
        "checks": sample_checks(all_terminal=False, pending_count=1),
        "actions": ["idle"],
    }
    snapshots = iter([
        green, green, changed_green, changed_green, pending, pending,
        changed_green, {**changed_green, "actions": [stop_action]},
    ])
    events = []
    sleeps = []
    monkeypatch.setattr(
        gh_pr_watch, "collect_snapshot",
        lambda _args: (next(snapshots), Path("unused-state.json")),
    )
    monkeypatch.setattr(
        gh_pr_watch, "print_event",
        lambda event, payload: events.append((event, payload)),
    )

    def fake_sleep(seconds):
        event, payload = events[-1]
        assert event == "snapshot"
        assert payload["next_poll_seconds"] == seconds
        sleeps.append(seconds)

    monkeypatch.setattr(gh_pr_watch.github_read.random, "uniform", lambda _low, _high: 2.0)
    monkeypatch.setattr(gh_pr_watch.time, "sleep", fake_sleep)

    assert gh_pr_watch.run_watch(args) == 0
    assert sleeps == [max(value, server_floor) + 2 for value in [
        args.poll_seconds, args.green_poll_seconds,
        args.poll_seconds, args.green_poll_seconds,
        args.poll_seconds, args.poll_seconds * 2, args.poll_seconds,
    ]]
    assert [event for event, _ in events] == ["snapshot"] * 8 + ["stop"]


def test_default_state_survives_process_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "durable-state"))
    path = gh_pr_watch.default_state_file_for(sample_pr())
    assert path.is_relative_to(tmp_path / "durable-state")
    saved = {"pending_reruns_by_sha": {"abc123": {"1": {"run_attempt": 1, "outcome": "submitting"}}},
             "retries_by_sha": {"abc123": 1}}
    ctx = multiprocessing.get_context("fork")
    writer = ctx.Process(target=lambda: gh_pr_watch.save_state(path, saved))
    writer.start()
    writer.join(timeout=5)
    assert writer.exitcode == 0
    assert gh_pr_watch.load_state(gh_pr_watch.default_state_file_for(sample_pr())) == (saved, False)
    assert path.stat().st_mode & 0o777 == 0o600


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




@pytest.mark.parametrize("resolved,outdated,comment_sha", [(False, False, "abc123"), (False, True, "old"), (True, False, "abc123")])
def test_security_threads_survive_seen_comments(monkeypatch, resolved, outdated, comment_sha):
    pr = sample_pr()
    comment = {"id": 17, "user": {"login": "github-advanced-security[bot]"}, "body": "CodeQL finding", "commit_id": comment_sha}
    monkeypatch.setattr(gh_pr_watch, "gh_api_list_paginated", lambda endpoint, **kwargs: [comment] if endpoint.endswith("/comments") and "/pulls/" in endpoint else [])
    body = {"data": {"repository": {"nameWithOwner": pr["repo"], "pullRequest": {
        "number": pr["number"], "url": pr["url"], "headRefOid": pr["head_sha"],
        "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [{
            "id": "thread17", "isResolved": resolved, "isOutdated": outdated,
            "comments": {"pageInfo": {"hasNextPage": False}, "nodes": [{"databaseId": 17, "commit": {"oid": comment_sha}}]},
        }]},
    }}}}
    reader = ReviewReader(body=body)
    state = {}
    first = gh_pr_watch.fetch_new_review_items(pr, state, True, reader=reader)
    assert len(first) == (0 if resolved else 1)
    # A subsequent snapshot has no new comments, but unresolved threads still block.
    second = gh_pr_watch.fetch_new_review_items(pr, state, False, reader=reader)
    assert second == []
    thread = pr["review_threads"]["threads"][0]
    assert thread["is_resolved"] is resolved
    assert thread["is_outdated"] is outdated
    assert thread["comments"][0]["matches_current_head"] is (comment_sha == pr["head_sha"])
    actions = gh_pr_watch.recommend_actions(pr, sample_checks(), [], [], second, 0, 3)
    assert ("ready_to_merge" in actions) is resolved
    assert ("resolve_review_threads" in actions) is (not resolved)


@pytest.mark.parametrize("fault", ["head", "errors", "truncated", "missing_comment", "missing_resolution"])
def test_incomplete_security_threads_never_prove_ready(fault):
    pr = sample_pr()
    item = {"number": pr["number"], "url": pr["url"], "headRefOid": pr["head_sha"],
            "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [{
                "id": "thread17", "isResolved": False, "isOutdated": False,
                "comments": {"pageInfo": {"hasNextPage": False}, "nodes": [{"databaseId": 17, "commit": {"oid": pr["head_sha"]}}]},
            }]}}
    body = {"data": {"repository": {"nameWithOwner": pr["repo"], "pullRequest": item}}}
    if fault == "head":
        item["headRefOid"] = "different"
    elif fault == "errors":
        body["errors"] = [{"message": "unavailable"}]
    elif fault == "truncated":
        item["reviewThreads"]["nodes"][0]["comments"]["pageInfo"]["hasNextPage"] = True
    elif fault == "missing_comment":
        item["reviewThreads"]["nodes"] = []
    else:
        del item["reviewThreads"]["nodes"][0]["isResolved"]
    pr["review_threads"] = gh_pr_watch.fetch_review_threads(pr, [{"id": "17", "author": "github-advanced-security[bot]"}], ReviewReader(body=body))
    assert pr["review_threads"]["status"] == "unknown"
    actions = gh_pr_watch.recommend_actions(pr, sample_checks(), [], [], [], 0, 3)
    assert "ready_to_merge" not in actions
    assert "review_thread_resolution_unavailable" in actions


def test_security_thread_pagination_pins_every_page():
    pr = sample_pr()
    class PagedReader(ReviewReader):
        def __init__(self):
            super().__init__()
            self.variables = []

        def graphql_json(self, query, variables, **kwargs):
            self.variables.append(variables)
            last = variables["cursor"] == "next"
            self.body = {"data": {"repository": {"nameWithOwner": pr["repo"], "pullRequest": {
                "number": pr["number"], "url": pr["url"], "headRefOid": pr["head_sha"],
                "reviewThreads": {"pageInfo": {"hasNextPage": not last, "endCursor": "next"}, "nodes": [{
                    "id": "thread17", "isResolved": False, "isOutdated": False,
                    "comments": {"pageInfo": {"hasNextPage": False}, "nodes": [{"databaseId": 17, "commit": {"oid": pr["head_sha"]}}]},
                }] if last else []},
            }}}}
            return super().graphql_json(query, variables, **kwargs)
    reader = PagedReader()
    evidence = gh_pr_watch.fetch_review_threads(pr, [{"id": "17", "author": "github-advanced-security[bot]"}], reader)
    assert evidence["status"] == "available"
    assert len(evidence["threads"]) == 1
    assert [variables["cursor"] for variables in reader.variables] == [None, "next"]
    assert all(variables["number"] == pr["number"] for variables in reader.variables)


def test_multiple_bot_comments_count_as_one_thread():
    pr = sample_pr()
    body = {"data": {"repository": {"nameWithOwner": pr["repo"], "pullRequest": {
        "number": pr["number"], "url": pr["url"], "headRefOid": pr["head_sha"],
        "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [{
            "id": "thread17", "isResolved": False, "isOutdated": False,
            "comments": {"pageInfo": {"hasNextPage": False}, "nodes": [
                {"databaseId": i, "commit": {"oid": pr["head_sha"]}} for i in (17, 18)
            ]},
        }]},
    }}}}
    evidence = gh_pr_watch.fetch_review_threads(pr, [
        {"id": str(i), "author": "github-advanced-security[bot]"} for i in (17, 18)
    ], ReviewReader(body=body))
    assert evidence["status"] == "available"
    assert len(evidence["threads"]) == 1
    assert {c["comment_id"] for c in evidence["threads"][0]["comments"]} == {"17", "18"}


def test_resolved_comments_do_not_replay_after_graphql_failure(monkeypatch):
    pr = sample_pr()
    comment = {"id": 17, "user": {"login": "github-advanced-security[bot]"}, "body": "CodeQL finding"}
    monkeypatch.setattr(gh_pr_watch, "gh_api_list_paginated", lambda endpoint, **kwargs: [comment] if endpoint.endswith("/comments") and "/pulls/" in endpoint else [])
    body = {"data": {"repository": {"nameWithOwner": pr["repo"], "pullRequest": {
        "number": pr["number"], "url": pr["url"], "headRefOid": pr["head_sha"],
        "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [{
            "id": "thread17", "isResolved": True, "isOutdated": False,
            "comments": {"pageInfo": {"hasNextPage": False}, "nodes": [{"databaseId": 17, "commit": {"oid": pr["head_sha"]}}]},
        }]},
    }}}}
    reader = ReviewReader(body=body)
    state = {}
    assert gh_pr_watch.fetch_new_review_items(pr, state, True, reader=reader) == []
    reader.body = {"errors": [{"message": "transient error"}]}
    assert gh_pr_watch.fetch_new_review_items(pr, state, False, reader=reader) == []
    assert pr["review_threads"]["status"] == "unknown"
    # Reopening remains a persistent blocker, despite the now-seen comment.
    reader.body = body
    body["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0]["isResolved"] = False
    assert gh_pr_watch.fetch_new_review_items(pr, state, False, reader=reader) == []
    assert "resolve_review_threads" in gh_pr_watch.recommend_actions(pr, sample_checks(), [], [], [], 0, 3)
def retry_snapshot(monkeypatch, tmp_path, runs, jobs) -> tuple[dict[str, Any], Path]:
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


class AcquisitionReader:
    def __init__(self):
        self.results = []
        self.cache_enabled = True
        self.pr = {"state": "open", "merged": False, "head": {"sha": "abc123"}, "base": {"ref": "main"}}
        self.degraded_reasons = []
        self.jobs: list[dict[str, Any]] = [{"id": 7, "run_id": 1, "run_attempt": 1, "head_sha": "abc123",
                      "runner_id": 0, "steps": [], "status": "completed", "conclusion": "cancelled",
                      "check_run_url": "https://api.github.com/repos/openai/codex/check-runs/9"}]
        self.annotations: list[dict[str, Any]] = [{"annotation_level": "failure", "message":
            "The job was not acquired by Runner of type hosted even after multiple attempts."}]
        self.check = {"id": 9, "node_id": "check9", "head_sha": "abc123", "conclusion": "cancelled"}
        self.run = {"id": 1, "head_sha": "abc123", "run_attempt": 1,
                    "status": "completed", "conclusion": "failure"}
        self.node: dict[str, Any] = {"databaseId": 9, "isRequired": True,
                     "checkSuite": {"repository": {"nameWithOwner": "openai/codex"}}}
        self.errors = None
        self.paths = []

    def diagnostics(self):
        return {}

    def paged_json(self, path, **_kwargs):
        self.paths.append(path)
        return self.annotations if path.endswith("/annotations") else self.jobs

    def get_json(self, path, **_kwargs):
        self.paths.append(path)
        if "/pulls/" in path:
            assert not self.cache_enabled
            return self.pr
        return self.check if "/check-runs/" in path else self.run

    def graphql_json(self, _query, variables, **_kwargs):
        assert variables == {"id": "check9", "number": 123}
        return SimpleNamespace(ok=True, body={"data": {"node": self.node}, "errors": self.errors})


def test_required_runner_acquisition_admitted_without_logs():
    reader = AcquisitionReader()
    assert gh_pr_watch.runner_acquisition_retry(sample_pr(), failed_run(1), reader)
    assert "/repos/openai/codex/actions/runs/1/attempts/1/jobs" in reader.paths
    assert not any("logs" in path for path in reader.paths)


@pytest.mark.parametrize("target,changes", [
    ("jobs", {"steps": [{"conclusion": "success"}]}),
    ("jobs", {"runner_id": 4}),
    ("jobs", {"conclusion": "success"}),
    ("jobs", {"head_sha": "old"}),
    ("jobs", {"run_attempt": 2}),
    ("jobs", {"run_id": 2}),
    ("jobs", {"check_run_url": "https://api.github.com/repos/other/repo/check-runs/9"}),
    ("check", {"head_sha": "old"}),
    ("check", {"id": 10}),
    ("run", {"run_attempt": 2}),
    ("run", {"status": "queued"}),
    ("node", {"isRequired": False}),
    ("node", {"checkSuite": {"repository": {"nameWithOwner": "other/repo"}}}),
])
def test_acquisition_rejects_nonrequired_executed_or_stale_evidence(target: str, changes: dict[str, Any]):
    reader = AcquisitionReader()
    record: dict[str, Any] = reader.jobs[0] if target == "jobs" else getattr(reader, target)
    record.update(changes)
    assert not gh_pr_watch.runner_acquisition_retry(sample_pr(), failed_run(1), reader)


@pytest.mark.parametrize("case", ["no_jobs", "no_annotations", "concurrency", "graphql_error", "actor", "read_error", "mixed_success", "cancelled_run"])
def test_acquisition_exclusions_and_unavailable_evidence(case):
    reader = AcquisitionReader()
    run = failed_run(1)
    if case == "no_jobs":
        reader.jobs = []
    elif case == "no_annotations":
        reader.annotations = []
    elif case == "concurrency":
        reader.annotations[0]["message"] = "Cancelled due to a higher priority waiting request"
    elif case == "graphql_error":
        reader.errors = [{"message": "unavailable"}]
    elif case == "actor":
        reader.degraded_reasons = [{"component": "actor"}]
    elif case == "read_error":
        def unavailable(*_args, **_kwargs):
            raise gh_pr_watch.github_read.GitHubReadError("unavailable", result=None, diagnostics={})
        reader.paged_json = unavailable
    elif case == "mixed_success":
        reader.jobs.append({**reader.jobs[0], "conclusion": "success"})
    else:
        run["conclusion"] = "cancelled"
    assert not gh_pr_watch.runner_acquisition_retry(sample_pr(), run, reader)


@pytest.mark.parametrize("outcome", ["success", "rejected", "unknown"])
def test_acquisition_full_retry_preserves_budget_and_intent(monkeypatch, tmp_path, outcome):
    run = {**failed_run(1), "retry_mode": "runner_acquisition"}
    _, path = retry_snapshot(monkeypatch, tmp_path, [run, failed_run(2, "cancelled")], [])
    reader = AcquisitionReader()
    monkeypatch.setattr(gh_pr_watch, "watcher_reader", lambda: reader)
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *a: sample_pr())
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *a, **kw: [reader.run])
    writes = []
    def submit(args, **_kwargs):
        saved = gh_pr_watch.load_state(path)[0]
        assert saved["retries_by_sha"]["abc123"] == 1
        assert saved["pending_reruns_by_sha"]["abc123"]["1"]["outcome"] == "submitting"
        writes.append(args)
        if outcome == "rejected":
            raise gh_pr_watch.GhCommandError("HTTP 422: This workflow run cannot be retried")
        if outcome == "unknown":
            raise gh_pr_watch.GhCommandError("connection lost")
    monkeypatch.setattr(gh_pr_watch, "gh_text", submit)
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert writes == [["run", "rerun", "1"]]
    assert 2 in result["skipped_run_ids"]
    state = gh_pr_watch.load_state(path)[0]
    assert gh_pr_watch.current_retry_count(state, "abc123") == (0 if outcome == "rejected" else 1)
    pending = state["pending_reruns_by_sha"]["abc123"]
    if outcome == "rejected":
        assert pending == {}
    else:
        assert pending["1"]["retry_mode"] == "runner_acquisition"
        assert pending["1"]["outcome"] == ("confirmed" if outcome == "success" else "submitting")
        # A second invocation cannot replay either a confirmed or unknown write.
        assert gh_pr_watch.retry_failed_now(argparse.Namespace())["reason"] == "rerun_outcome_pending"
        assert len(writes) == 1


@pytest.mark.parametrize("change", ["head", "base", "closed", "attempt", "evidence", "budget"])
def test_acquisition_retry_revalidates_before_write(monkeypatch, tmp_path, change):
    run = {**failed_run(1), "retry_mode": "runner_acquisition"}
    _, path = retry_snapshot(monkeypatch, tmp_path, [run], [])
    reader = AcquisitionReader()
    monkeypatch.setattr(gh_pr_watch, "watcher_reader", lambda: reader)
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *a, **kw: [reader.run])
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *a: {**sample_pr(), "head_sha": "new" if change == "head" else "abc123"})
    if change == "head":
        reader.pr["head"]["sha"] = "new"
    elif change == "base":
        reader.pr["base"]["ref"] = "other"
    elif change == "closed":
        reader.pr["state"] = "closed"
    elif change == "attempt":
        reader.run["run_attempt"] = 2
    elif change == "evidence":
        reader.node["isRequired"] = False
    elif change == "budget":
        gh_pr_watch.save_state(path, {"retries_by_sha": {"abc123": 3}})
    monkeypatch.setattr(gh_pr_watch, "gh_text", lambda *a, **kw: pytest.fail("unexpected write"))
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert not result["rerun_attempted"]
    assert gh_pr_watch.current_retry_count(gh_pr_watch.load_state(path)[0], "abc123") == (3 if change == "budget" else 0)


@pytest.mark.parametrize("case", ["required", "optional", "other_tool", "other_workflow", "comment_only", "malformed", "wrong_path"])
def test_codeql_ruleset_requires_real_analysis_workflow(case):
    reader = AcquisitionReader()
    pr = sample_pr()
    run = {**reader.run, "path": ".github/workflows/scan.yml"}
    rules = [{"type": "code_scanning", "parameters": {
        "code_scanning_tools": [{"tool": "Other" if case == "other_tool" else "CodeQL"}]}}]
    if case == "optional":
        rules = []
    workflow = "jobs:\n  scan:\n    steps:\n      - uses: github/codeql-action/analyze@v4\n"
    if case == "other_workflow":
        workflow = "jobs: {notice: {steps: [{run: echo notification}]}}"
    elif case == "comment_only":
        workflow = "# uses: github/codeql-action/analyze@v4\njobs: {}"
    elif case == "malformed":
        workflow = "jobs: [invalid"
    content = {"path": "different.yml" if case == "wrong_path" else run["path"],
               "encoding": "base64", "content": base64.b64encode(workflow.encode()).decode()}
    paths = []
    def read_rules(path, **_kwargs):
        paths.append(path)
        return rules
    def read_source(path, **_kwargs):
        paths.append(path)
        return content
    reader.paged_json = read_rules
    reader.get_json = read_source
    assert gh_pr_watch.required_codeql_workflow(pr, run, reader) == (case == "required")
    if case == "required":
        assert paths == ["/repos/openai/codex/rules/branches/main",
                         "/repos/openai/codex/contents/.github/workflows/scan.yml?ref=abc123"]


def test_ruleset_requirement_admits_acquisition_when_job_is_not_required(monkeypatch):
    reader = AcquisitionReader()
    reader.node["isRequired"] = False
    seen = []
    def required(pr, run, evidence):
        seen.append((pr["head_sha"], run["run_attempt"], evidence))
        return True
    monkeypatch.setattr(gh_pr_watch, "required_codeql_workflow", required)
    assert gh_pr_watch.runner_acquisition_retry(sample_pr(), failed_run(1), reader)
    assert seen == [("abc123", 1, reader)]


@pytest.mark.parametrize("body", [{"data": None, "errors": [{"message": "timeout"}]},
                                 {"data": {"node": {"databaseId": 9, "checkSuite": None}}},
                                 {"data": {"node": {"databaseId": 9, "checkSuite": {"repository": None}}}}])
def test_acquisition_partial_graphql_declines_without_crashing(body):
    reader = AcquisitionReader()
    reader.graphql_json = lambda *_a, **_kw: SimpleNamespace(ok=True, body=body)
    assert not gh_pr_watch.runner_acquisition_retry(sample_pr(), failed_run(1), reader)


def test_acquisition_repository_names_are_case_insensitive():
    reader = AcquisitionReader()
    assert gh_pr_watch.runner_acquisition_retry({**sample_pr(), "repo": "OpenAI/Codex"}, failed_run(1), reader)


@pytest.mark.parametrize("admit", [True, False])
@pytest.mark.parametrize("gate", ["terminal", "pending", "closed", "budget"])
def test_snapshot_offers_only_verified_acquisition_recovery(monkeypatch, tmp_path, admit, gate):
    reader = AcquisitionReader()
    if not admit:
        reader.annotations = []
    pr = sample_pr()
    if gate == "closed":
        pr["closed"] = True
    if gate in {"closed", "pending", "budget"}:
        reader.paged_json = lambda *_a, **_kw: pytest.fail("premature acquisition proof read")
    monkeypatch.setattr(gh_pr_watch, "watcher_reader", lambda: reader)
    monkeypatch.setattr(gh_pr_watch, "get_authenticated_login", lambda *_a, **_kw: "fixture-bot")
    monkeypatch.setattr(gh_pr_watch, "fetch_new_review_items", lambda *_a, **_kw: [])
    monkeypatch.setattr(gh_pr_watch.github_read, "pull_request_checks", lambda *_a, **_kw: {})
    monkeypatch.setattr(gh_pr_watch, "summarize_checks", lambda *_a, **_kw: sample_checks(failed_count=1, all_terminal=gate != "pending"))
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *_a, **_kw: [reader.run])
    monkeypatch.setattr(gh_pr_watch, "failed_jobs_from_workflow_runs", lambda *_a, **_kw: [failed_job(1, "cancelled")])
    args = argparse.Namespace(max_flaky_retries=0 if gate == "budget" else 3)
    snapshot, _ = gh_pr_watch.collect_locked_snapshot(args, pr, {}, tmp_path / "state.json")
    assert ("retry_failed_checks" in snapshot["actions"]) == (admit and gate == "terminal")
    assert (snapshot["failed_runs"][0].get("retry_mode") == "runner_acquisition") == (admit and gate == "terminal")


def test_acquisition_bad_pagination_declines_without_crashing():
    reader = AcquisitionReader()
    def bad_shape(*_a, **_kw):
        raise gh_pr_watch.github_read.GitHubReadShapeError("repeated page")
    reader.paged_json = bad_shape
    assert not gh_pr_watch.runner_acquisition_retry(sample_pr(), failed_run(1), reader)


def test_changed_pr_preserves_an_earlier_ordinary_retry(monkeypatch, tmp_path):
    recovery = {**failed_run(2), "retry_mode": "runner_acquisition"}
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1), recovery], [failed_job(1)])
    reader = AcquisitionReader()
    reader.run["id"] = 2
    reader.jobs[0]["run_id"] = 2
    monkeypatch.setattr(gh_pr_watch, "watcher_reader", lambda: reader)
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *_a, **_kw: [
        {**reader.run, "id": 1}, reader.run,
    ])
    writes = []
    def submit(args, **_kw):
        writes.append(args)
        reader.pr["head"]["sha"] = "new"
    monkeypatch.setattr(gh_pr_watch, "gh_text", submit)
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert result["reason"] == "pr_changed"
    assert result["rerun_run_ids"] == [1]
    assert result["rerun_count"] == 1
    assert result["retries_used"] == 1
    assert writes == [["run", "rerun", "1", "--failed"]]
    saved = gh_pr_watch.load_state(path)[0]
    assert saved["pending_reruns_by_sha"]["abc123"] == {"1": {"outcome": "confirmed", "run_attempt": 1}}


@pytest.mark.parametrize("outcome,inventory,action", [
    ("confirmed", [], "stop_missing_rerun"),
    ("submitting", [], "stop_unknown_rerun"),
    ("submitting", [{"id": 1, "head_sha": "abc123", "run_attempt": 1}], "stop_unknown_rerun"),
    ("confirmed", [{"id": 1, "head_sha": "abc123", "run_attempt": 1}], "check_rerun_outcome"),
])
def test_pending_lifecycle_snapshot_and_watch_stop(monkeypatch, tmp_path, outcome, inventory, action):
    path = tmp_path / "state.json"
    intent = {"run_attempt": 1, "outcome": outcome}
    gh_pr_watch.save_state(path, {"pending_reruns_by_sha": {"abc123": {"1": intent}},
                                 "retries_by_sha": {"abc123": 1}})
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_a, **_kw: sample_pr())
    monkeypatch.setattr(gh_pr_watch, "get_authenticated_login", lambda *_a: "fixture-bot")
    monkeypatch.setattr(gh_pr_watch, "fetch_new_review_items", lambda *_a, **_kw: [])
    monkeypatch.setattr(gh_pr_watch.github_read, "pull_request_checks", lambda *_a, **_kw: {})
    monkeypatch.setattr(gh_pr_watch, "summarize_checks", lambda *_a, **_kw: sample_checks())
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *_a, **_kw: inventory)
    monkeypatch.setattr(gh_pr_watch, "failed_jobs_from_workflow_runs", lambda *_a, **_kw: [])
    args = argparse.Namespace(pr="123", repo=None, state_file=str(path), max_flaky_retries=3,
                              poll_seconds=60, green_poll_seconds=300)
    snapshot, _ = gh_pr_watch.collect_snapshot(args)
    assert action in snapshot["actions"]
    assert "ready_to_merge" not in snapshot["actions"]
    assert "retry_failed_checks" not in snapshot["actions"]
    assert snapshot["retry_state"]["missing_confirmed_run_ids"] == (
        ["1"] if outcome == "confirmed" and not inventory else [])
    assert gh_pr_watch.load_state(path)[0]["pending_reruns_by_sha"]["abc123"] == {"1": intent}
    assert gh_pr_watch.retry_failed_now(args)["reason"] == "rerun_outcome_pending"
    if action.startswith("stop_"):
        events = []
        monkeypatch.setattr(gh_pr_watch, "print_event", lambda event, payload: events.append(event))
        monkeypatch.setattr(gh_pr_watch.time, "sleep", lambda *_a: pytest.fail("polling after terminal disposition"))
        assert gh_pr_watch.run_watch(args) == 0
        assert events == ["snapshot", "stop"]


def test_default_location_copies_legacy_evidence_without_reset(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "durable"))
    legacy = tmp_path / "legacy.json"
    saved = {"pending_reruns_by_sha": {"abc123": {"1": {"run_attempt": 1, "outcome": "submitting"}}},
             "retries_by_sha": {"abc123": 2}}
    gh_pr_watch.save_state(legacy, saved)
    monkeypatch.setattr(gh_pr_watch, "legacy_state_file_for", lambda _pr: legacy)
    monkeypatch.setattr(gh_pr_watch, "resolve_pr", lambda *_a, **_kw: sample_pr())
    observed = []
    def collect(_args, _pr, _diag, target_path):
        observed.append(gh_pr_watch.load_state(target_path))
        return {}, target_path
    monkeypatch.setattr(gh_pr_watch, "collect_locked_snapshot", collect)
    args = argparse.Namespace(pr="123", repo=None, state_file=None)
    _, path = gh_pr_watch.collect_snapshot(args)
    assert path != legacy
    assert observed == [(saved, False)]
    assert gh_pr_watch.load_state(legacy) == (saved, False)
    changed = {**saved, "retries_by_sha": {"abc123": 3}}
    gh_pr_watch.save_state(path, changed)
    gh_pr_watch.collect_snapshot(args)
    assert observed[-1] == (changed, False)


def test_nonretryable_rejection_is_not_resubmitted_until_new_attempt(monkeypatch, tmp_path):
    snapshot, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    writes = []
    def reject(args, **_kw):
        writes.append(args)
        raise gh_pr_watch.GhCommandError("HTTP 422: This workflow run cannot be retried")
    monkeypatch.setattr(gh_pr_watch, "gh_text", reject)
    first = gh_pr_watch.retry_failed_now(argparse.Namespace())
    second = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert first["retries_used"] == second["retries_used"] == 0
    assert second["skipped_run_ids"] == [1]
    assert len(writes) == 1
    state = gh_pr_watch.load_state(path)[0]
    assert gh_pr_watch.reconcile_rejected_reruns(state, "other", []) == {}
    assert gh_pr_watch.reconcile_rejected_reruns(state, "abc123", [])
    assert gh_pr_watch.reconcile_rejected_reruns(state, "abc123", [
        {"id": 1, "head_sha": "abc123", "run_attempt": 2},
    ]) == {}
    snapshot["failed_runs"][0]["run_attempt"] = 2
    monkeypatch.setattr(gh_pr_watch, "gh_text", lambda args, **_kw: writes.append(args))
    assert gh_pr_watch.retry_failed_now(argparse.Namespace())["rerun_run_ids"] == [1]
    assert len(writes) == 2


def test_rejected_attempt_snapshot_has_terminal_disposition(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    gh_pr_watch.save_state(path, {"rejected_reruns_by_sha": {"abc123": {"1": {"run_attempt": 1}}}})
    monkeypatch.setattr(gh_pr_watch, "get_authenticated_login", lambda *_a: "fixture-bot")
    monkeypatch.setattr(gh_pr_watch, "fetch_new_review_items", lambda *_a, **_kw: [])
    monkeypatch.setattr(gh_pr_watch.github_read, "pull_request_checks", lambda *_a, **_kw: {})
    monkeypatch.setattr(gh_pr_watch, "summarize_checks", lambda *_a, **_kw: sample_checks(failed_count=1))
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *_a, **_kw: [
        {"id": 1, "head_sha": "abc123", "run_attempt": 1, "status": "completed", "conclusion": "failure"},
    ])
    monkeypatch.setattr(gh_pr_watch, "failed_jobs_from_workflow_runs", lambda *_a, **_kw: [failed_job(1)])
    snapshot, _ = gh_pr_watch.collect_locked_snapshot(argparse.Namespace(max_flaky_retries=3), sample_pr(), {}, path)
    assert "stop_nonretryable_rerun" in snapshot["actions"]
    assert "retry_failed_checks" not in snapshot["actions"]
    assert snapshot["retry_state"]["rejected_run_ids"] == ["1"]


def test_hung_command_releases_lock_and_preserves_unknown_intent(monkeypatch, tmp_path):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    command = tmp_path / "hung-gh"
    pid_file = tmp_path / "command.pid"
    command.write_text(f"#!{sys.executable}\nimport os, subprocess, sys, time\n"
                       f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                       "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                       "time.sleep(60)\n")
    command.chmod(0o700)
    monkeypatch.setattr(gh_pr_watch, "GH_COMMAND", str(command))
    monkeypatch.setattr(gh_pr_watch, "COMMAND_TIMEOUT_SECONDS", 0.5)
    ctx = multiprocessing.get_context("fork")
    results = ctx.Queue()
    def retry():
        results.put(gh_pr_watch.retry_failed_now(argparse.Namespace()))
    worker = ctx.Process(target=retry)
    try:
        worker.start()
        worker.join(timeout=5)
        assert worker.exitcode == 0, "hung command or inherited pipe kept state locked"
        result = results.get(timeout=1)
        assert result["reason"] == "rerun_outcome_unknown"
        assert result["retries_used"] == 1
        assert gh_pr_watch.load_state(path)[0]["pending_reruns_by_sha"]["abc123"]["1"]["outcome"] == "submitting"
        monkeypatch.setattr(gh_pr_watch, "LOCK_TIMEOUT_SECONDS", 0.2)
        assert gh_pr_watch.retry_failed_now(argparse.Namespace())["reason"] == "rerun_outcome_pending"
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join()
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), gh_pr_watch.signal.SIGKILL)
            except ProcessLookupError:
                pass
        results.close()


def test_contended_lock_wait_is_bounded_without_touching_state(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    saved = {"pending_reruns_by_sha": {"abc123": {"1": {"outcome": "submitting", "run_attempt": 1}}}}
    gh_pr_watch.save_state(path, saved)
    ctx = multiprocessing.get_context("fork")
    held, release = ctx.Event(), ctx.Event()
    def holder():
        with gh_pr_watch.state_lock(path):
            held.set()
            release.wait(timeout=5)
    worker = ctx.Process(target=holder)
    try:
        worker.start()
        assert held.wait(timeout=2)
        monkeypatch.setattr(gh_pr_watch, "LOCK_TIMEOUT_SECONDS", 0.1)
        with pytest.raises(RuntimeError, match="State lock timed out"):
            with gh_pr_watch.state_lock(path):
                pytest.fail("entered a foreign-held lock")
        assert gh_pr_watch.load_state(path)[0] == saved
    finally:
        release.set()
        worker.join(timeout=2)
        if worker.is_alive():
            worker.terminate()
            worker.join()


def test_sync_failure_prevents_write_and_keeps_saved_intent(monkeypatch, tmp_path):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    saved = {"pr": {"number": 123}}
    gh_pr_watch.save_state(path, saved)
    def fail_sync(_fd):
        raise OSError("disk unavailable")
    monkeypatch.setattr(gh_pr_watch.os, "fsync", fail_sync)
    monkeypatch.setattr(gh_pr_watch, "gh_text", lambda *_a, **_kw: pytest.fail("write before durable intent"))
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert result["reason"] == "state_save_error"
    assert result["error"] == "disk unavailable"
    assert result["rerun_run_ids"] == []
    assert gh_pr_watch.load_state(path)[0] == saved
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("failed_read", ["pr", "acquisition"])
def test_later_read_error_reports_already_confirmed_progress(monkeypatch, tmp_path, capsys, failed_read):
    recovery = {**failed_run(2), "retry_mode": "runner_acquisition"}
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1), recovery], [failed_job(1)])
    reader = AcquisitionReader()
    monkeypatch.setattr(gh_pr_watch, "watcher_reader", lambda: reader)
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *_a, **_kw: [
        {"id": run_id, "head_sha": "abc123", "run_attempt": 1,
         "status": "completed", "conclusion": "failure"} for run_id in (1, 2)
    ])
    failure = gh_pr_watch.github_api.ApiResult(ok=False, status=503, body=None)
    def unavailable(*_a, **_kw):
        raise gh_pr_watch.github_read.GitHubReadError("service unavailable", result=failure, diagnostics={})
    if failed_read == "pr":
        monkeypatch.setattr(gh_pr_watch, "runner_acquisition_retry", lambda *_a, **_kw: True)
        reader.get_json = unavailable
    else:
        reader.paged_json = unavailable
    writes = []
    monkeypatch.setattr(gh_pr_watch, "gh_text", lambda args, **_kw: writes.append(args))
    monkeypatch.setattr(gh_pr_watch, "parse_args", lambda: argparse.Namespace(retry_failed_now=True))
    assert gh_pr_watch.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "rerun_read_error"
    assert result["rerun_run_ids"] == [1]
    assert result["rerun_count"] == result["retries_used"] == 1
    assert result["read_error"]["status"] == 503
    assert writes == [["run", "rerun", "1", "--failed"]]
    assert gh_pr_watch.load_state(path)[0]["pending_reruns_by_sha"]["abc123"] == {
        "1": {"run_attempt": 1, "outcome": "confirmed"},
    }


def test_snapshot_honors_long_managed_cooldown(monkeypatch, tmp_path, watcher_transport):
    fixture = watcher_transport
    monkeypatch.setenv("GITHUB_RETRY_MAX_WAIT_SECONDS", "120")
    monkeypatch.setattr(gh_pr_watch.time, "time", lambda: fixture.clock[0])
    fixture.replies.extend([
        (503, {"retry-after": "70"}, {"message": "Service unavailable"}, "fixture-bot"),
        (200, {}, [], "fixture-bot"),
    ])
    monkeypatch.setattr(gh_pr_watch, "get_authenticated_login", lambda *_a: "fixture-bot")
    def reviews(pr, _state, **kwargs):
        return kwargs["reader"].get_json(f"/repos/{pr['repo']}/issues/123/comments", step="comments")
    monkeypatch.setattr(gh_pr_watch, "fetch_new_review_items", reviews)
    monkeypatch.setattr(gh_pr_watch.github_read, "pull_request_checks", lambda *_a, **_kw: {})
    monkeypatch.setattr(gh_pr_watch, "summarize_checks", lambda *_a, **_kw: sample_checks())
    monkeypatch.setattr(gh_pr_watch, "get_workflow_runs_for_sha", lambda *_a, **_kw: [])
    monkeypatch.setattr(gh_pr_watch, "failed_jobs_from_workflow_runs", lambda *_a, **_kw: [])
    snapshot, _ = gh_pr_watch.collect_locked_snapshot(argparse.Namespace(max_flaky_retries=3), sample_pr(), {}, tmp_path / "state.json")
    assert fixture.clock[0] >= 1070
    assert len(fixture.calls) == 2
    assert "ready_to_merge" in snapshot["actions"]


def test_watch_continues_after_bounded_lock_contention(monkeypatch):
    reads = [gh_pr_watch.StateLockTimeout(Path("state.json")),
             ({"pr": sample_pr(), "actions": ["stop_pr_closed"]}, Path("state.json"))]
    def collect(_args):
        result = reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    events, sleeps = [], []
    monkeypatch.setattr(gh_pr_watch, "collect_snapshot", collect)
    monkeypatch.setattr(gh_pr_watch, "print_event", lambda event, payload: events.append(event))
    monkeypatch.setattr(gh_pr_watch.time, "sleep", sleeps.append)
    monkeypatch.setattr(gh_pr_watch.github_read, "poll_delay", lambda seconds, *_a, **_kw: seconds)
    assert gh_pr_watch.run_watch(argparse.Namespace(poll_seconds=60, green_poll_seconds=300)) == 0
    assert events == ["state_busy", "snapshot", "stop"]
    assert sleeps == [60]


def test_expired_deadline_does_not_launch_or_charge_a_write(monkeypatch, tmp_path):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    monkeypatch.setenv("GITHUB_RETRY_DEADLINE_AT", str(gh_pr_watch.time.time() - 10))
    monkeypatch.setattr(gh_pr_watch.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("launch after deadline"))
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert result["reason"] == "rerun_rejected"
    assert "no write sent" in result["error"]
    assert result["retries_used"] == 0
    assert gh_pr_watch.load_state(path)[0]["pending_reruns_by_sha"]["abc123"] == {}


def test_directory_sync_failure_stops_before_api_and_reports_error(monkeypatch, tmp_path, capsys):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    sync = gh_pr_watch.os.fsync
    calls = []
    def fail_directory(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError("directory sync unsupported")
        sync(fd)
    monkeypatch.setattr(gh_pr_watch.os, "fsync", fail_directory)
    monkeypatch.setattr(gh_pr_watch, "gh_text", lambda *_a, **_kw: pytest.fail("write after failed sync"))
    monkeypatch.setattr(gh_pr_watch, "parse_args", lambda: argparse.Namespace(retry_failed_now=True))
    assert gh_pr_watch.main() == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["reason"] == "state_save_error"
    assert result["error"] == "directory sync unsupported"
    assert result["rerun_run_ids"] == []
    assert not captured.err
    state = gh_pr_watch.load_state(path)[0]
    assert result["not_sent_run_ids"] == [1]
    assert result["unsent_state_restored"] is True
    assert state["pending_reruns_by_sha"]["abc123"] == {}
    assert state["retries_by_sha"]["abc123"] == 0


def test_unsent_storage_recovery_keeps_earlier_confirmed_write(monkeypatch, tmp_path):
    _, path = retry_snapshot(monkeypatch, tmp_path,
                             [failed_run(1), failed_run(2)], [failed_job(1), failed_job(2)])
    sync = gh_pr_watch.os.fsync
    count = 0
    def fail_third_directory(fd):
        nonlocal count
        count += 1
        if count == 6:
            raise OSError("directory fsync interrupted")
        sync(fd)
    monkeypatch.setattr(gh_pr_watch.os, "fsync", fail_third_directory)
    writes = []
    monkeypatch.setattr(gh_pr_watch, "gh_text", lambda args, **_kw: writes.append(args[2]))
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert result["reason"] == "state_save_error"
    assert result["not_sent_run_ids"] == [2]
    assert result["unsent_state_restored"] is True
    assert result["rerun_run_ids"] == [1]
    assert result["retries_used"] == 1
    assert writes == ["1"]
    assert gh_pr_watch.load_state(path)[0]["pending_reruns_by_sha"]["abc123"] == {
        "1": {"run_attempt": 1, "outcome": "confirmed"},
    }


def test_failed_unsent_restoration_preserves_recovery_error_and_intent(monkeypatch, tmp_path):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    original_save = gh_pr_watch.save_state
    saves = 0
    def unavailable(path_arg, state):
        nonlocal saves
        saves += 1
        if saves == 1:
            original_save(path_arg, state)
            raise OSError("directory fsync failed after replace")
        raise OSError("storage remains unavailable")
    monkeypatch.setattr(gh_pr_watch, "save_state", unavailable)
    monkeypatch.setattr(gh_pr_watch, "gh_text", lambda *_a, **_kw: pytest.fail("unsent write"))
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert result["reason"] == "state_save_error"
    assert result["not_sent_run_ids"] == [1]
    assert result["unsent_state_restored"] is False
    assert result["recovery_error"] == "storage remains unavailable"
    assert result["rerun_attempted"] is False
    assert result["retries_used"] == 1
    assert gh_pr_watch.load_state(path)[0]["pending_reruns_by_sha"]["abc123"]["1"]["outcome"] == "submitting"


def test_relative_xdg_location_uses_home_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", "relative-state")
    monkeypatch.setattr(gh_pr_watch.Path, "home", lambda: tmp_path)
    assert gh_pr_watch.default_state_file_for(sample_pr()).is_relative_to(tmp_path / ".local" / "state")


def test_confirmation_save_failure_reports_completed_write(monkeypatch, tmp_path, capsys):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    writes = []
    def submit(args, **_kw):
        writes.append(args)
        def fail_sync(_fd):
            raise OSError("disk unavailable after confirmation")
        monkeypatch.setattr(gh_pr_watch.os, "fsync", fail_sync)
    monkeypatch.setattr(gh_pr_watch, "gh_text", submit)
    monkeypatch.setattr(gh_pr_watch, "parse_args", lambda: argparse.Namespace(retry_failed_now=True))
    assert gh_pr_watch.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "state_save_error"
    assert result["rerun_run_ids"] == [1]
    assert result["rerun_count"] == result["retries_used"] == 1
    assert len(writes) == 1
    assert gh_pr_watch.load_state(path)[0]["pending_reruns_by_sha"]["abc123"]["1"]["outcome"] == "submitting"


@pytest.mark.parametrize("failed_stage", ["preflight", "write"])
def test_real_wrapper_distinguishes_preflight_refusal_from_unknown_write(monkeypatch, tmp_path, failed_stage):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    for name in ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
                 "GH_TOKEN", "GITHUB_TOKEN", "GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK",
                 "GH_WITH_ENV_TOKEN_OWN_USER", "GH_WITH_ENV_TOKEN_CLASSIFIER",
                 "GH_WITH_ENV_TOKEN_IDENTITY_HELPER", "GITHUB_RETRY_DEADLINE_AT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_AUTOMATION_LOGIN", "fixture-bot")
    monkeypatch.setenv("CODEX_GITHUB_TOKEN", "offline-fixture-token")
    monkeypatch.setenv("GH_WITH_ENV_TOKEN_PYTHON", sys.executable)
    monkeypatch.setenv("GITHUB_RETRY_MAX_WAIT_SECONDS", "120")
    calls = tmp_path / "calls"
    command = tmp_path / "fake-gh"
    command.write_text(f"#!{sys.executable}\nimport sys\n"
                       f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                       f"if {failed_stage!r} == 'write' and '/user' in sys.argv:\n"
                       "    print('HTTP/2 200\\ncontent-type: application/json\\n\\n{\"login\":\"fixture-bot\"}')\n"
                       "    sys.exit(0)\n"
                       "print('HTTP/2 503\\nRetry-After: 70\\ncontent-type: application/json\\n\\n{\"message\":\"Service unavailable\"}')\n"
                       "sys.exit(1)\n")
    command.chmod(0o700)
    monkeypatch.setenv("GH_WITH_ENV_TOKEN_GH", str(command))
    monkeypatch.setattr(gh_pr_watch, "GH_COMMAND", str(gh_pr_watch.DEFAULT_GH))
    monkeypatch.setattr(gh_pr_watch, "COMMAND_TIMEOUT_SECONDS", 5.0)
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    actual_calls = calls.read_text().splitlines()
    assert "/user" in actual_calls[0]
    pending = gh_pr_watch.load_state(path)[0]["pending_reruns_by_sha"]["abc123"]
    if failed_stage == "preflight":
        assert result["reason"] == "rerun_rejected"
        assert "refusing write" in result["error"]
        assert result["retries_used"] == 0
        assert len(actual_calls) == 1
        assert pending == {}
    else:
        assert result["reason"] == "rerun_outcome_unknown"
        assert result["retries_used"] == 1
        assert len(actual_calls) == 2
        assert "rerun" in actual_calls[1]
        assert pending["1"]["outcome"] == "submitting"


@pytest.mark.parametrize("termination", ["SIGTERM", "SIGHUP", "SIGKILL"])
def test_parent_termination_cleans_catchable_commands_and_preserves_unknown(monkeypatch, tmp_path, termination):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    command = tmp_path / "fake-gh"
    pid_file = tmp_path / "command.pid"
    heartbeat = tmp_path / "heartbeat"
    descendant = tmp_path / "descendant-heartbeat"
    ticking = "import pathlib,time; p=pathlib.Path({!r}); n=0\nwhile True:\n n+=1; p.write_text(str(n)); time.sleep(0.02)\n"
    command.write_text(f"#!{sys.executable}\nimport os,subprocess,sys\n"
                       f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                       f"subprocess.Popen([sys.executable, '-c', {ticking.format(str(descendant))!r}])\n"
                       + ticking.format(str(heartbeat)))
    command.chmod(0o700)
    monkeypatch.setattr(gh_pr_watch, "GH_COMMAND", str(command))
    monkeypatch.setattr(gh_pr_watch, "COMMAND_TIMEOUT_SECONDS", 5.0)
    worker = multiprocessing.get_context("fork").Process(
        target=lambda: gh_pr_watch.retry_failed_now(argparse.Namespace()),
    )
    try:
        worker.start()
        deadline = gh_pr_watch.time.monotonic() + 3
        while not (heartbeat.exists() and descendant.exists() and descendant.stat().st_size):
            assert gh_pr_watch.time.monotonic() < deadline, "offline command did not start"
            gh_pr_watch.time.sleep(0.01)
        os.kill(worker.pid, getattr(gh_pr_watch.signal, termination))
        worker.join(timeout=2)
        assert not worker.is_alive()
        gh_pr_watch.time.sleep(0.1)
        before = (heartbeat.read_text(), descendant.read_text())
        gh_pr_watch.time.sleep(0.2)
        after = (heartbeat.read_text(), descendant.read_text())
        if termination == "SIGKILL":
            assert after != before, "uncatchable termination coverage did not leave a live command"
        else:
            assert after == before, "catchable termination left a command/descendant alive"
        saved = gh_pr_watch.load_state(path)[0]
        assert saved["pending_reruns_by_sha"]["abc123"]["1"]["outcome"] == "submitting"
        assert saved["retries_by_sha"]["abc123"] == 1
        monkeypatch.setattr(gh_pr_watch, "LOCK_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr(gh_pr_watch, "gh_text", lambda *_a, **_kw: pytest.fail("unknown write replay"))
        assert gh_pr_watch.retry_failed_now(argparse.Namespace())["reason"] == "rerun_outcome_pending"
    finally:
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=2)
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), gh_pr_watch.signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("refusal", ["missing_login", "missing_identity", "missing_token", "user_mismatch",
                                    "app_mismatch", "incomplete_app", "app_failure", "malformed_app", "contributor"])
def test_real_wrapper_prewrite_receipts_release_only_unsent_intent(monkeypatch, tmp_path, refusal):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    for name in ("GITHUB_APP_ID", "GITHUB_APP_INSTALLATION_ID", "GITHUB_APP_PRIVATE_KEY_PATH",
                 "GH_TOKEN", "GITHUB_TOKEN", "CODEX_GITHUB_TOKEN", "CODEX_AUTOMATION_LOGIN",
                 "GH_WITH_ENV_TOKEN_EXPECTED_LOGIN", "GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK",
                 "GH_WITH_ENV_TOKEN_OWN_USER", "GH_WITH_ENV_TOKEN_CLASSIFIER",
                 "GH_WITH_ENV_TOKEN_IDENTITY_HELPER", "GITHUB_RETRY_DEADLINE_AT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_SKILLS_ENV_FILE", str(tmp_path / "missing.env"))
    if refusal not in {"missing_login", "missing_identity"}:
        monkeypatch.setenv("CODEX_AUTOMATION_LOGIN", "fixture-bot")
    if refusal not in {"missing_token", "missing_identity"}:
        monkeypatch.setenv("CODEX_GITHUB_TOKEN", "offline-fixture-token")
    monkeypatch.setenv("GH_WITH_ENV_TOKEN_PYTHON", sys.executable)
    calls = tmp_path / "calls"
    command = tmp_path / "fake-gh"
    command.write_text(f"#!{sys.executable}\nimport sys\n"
                       f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                       "print('HTTP/2 200\\ncontent-type: application/json\\n\\n{\"login\":\"other-fixture\"}')\n")
    command.chmod(0o700)
    monkeypatch.setenv("GH_WITH_ENV_TOKEN_GH", str(command))
    if refusal.startswith("app_") or refusal in {"incomplete_app", "malformed_app", "contributor"}:
        monkeypatch.setenv("GITHUB_APP_ID", "1")
        if refusal != "incomplete_app":
            monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "2")
            monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", "/offline/unused.pem")
        identity = tmp_path / "identity.py"
        if refusal == "app_failure":
            identity.write_text("import sys; print('offline App refusal', file=sys.stderr); sys.exit(1)\n")
        elif refusal == "contributor":
            identity.write_text("import sys; sys.exit(3)\n")
        elif refusal == "malformed_app":
            identity.write_text("print('missing-token')\n")
        else:
            identity.write_text("print('other-fixture'); print('offline-fixture-token')\n")
        monkeypatch.setenv("GH_WITH_ENV_TOKEN_IDENTITY_HELPER", str(identity))
    monkeypatch.setattr(gh_pr_watch, "GH_COMMAND", str(gh_pr_watch.DEFAULT_GH))
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert result["reason"] == "rerun_rejected"
    assert result["not_sent_run_ids"] == [1]
    assert result["retries_used"] == 0
    actual_calls = calls.read_text().splitlines() if calls.exists() else []
    assert all("rerun" not in call for call in actual_calls)
    assert gh_pr_watch.load_state(path)[0]["pending_reruns_by_sha"]["abc123"] == {}


def test_refusal_text_without_matching_wrapper_receipt_stays_unknown(monkeypatch, tmp_path):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    command = tmp_path / "custom-gh"
    command.write_text("#!/bin/sh\nprintf 'error: unable to verify the automation GitHub actor; refusing write\\n' >&2\nexit 1\n")
    command.chmod(0o700)
    monkeypatch.setattr(gh_pr_watch, "GH_COMMAND", str(command))
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert result["reason"] == "rerun_outcome_unknown"
    assert result["retries_used"] == 1
    assert gh_pr_watch.load_state(path)[0]["pending_reruns_by_sha"]["abc123"]["1"]["outcome"] == "submitting"


def test_command_restores_parent_signal_handlers(monkeypatch):
    before = {sig: gh_pr_watch.signal.getsignal(sig) for sig in (gh_pr_watch.signal.SIGTERM, gh_pr_watch.signal.SIGHUP)}
    monkeypatch.setattr(gh_pr_watch, "GH_COMMAND", sys.executable)
    assert gh_pr_watch.gh_text(["-c", "print('offline')"]).strip() == "offline"
    assert {sig: gh_pr_watch.signal.getsignal(sig) for sig in before} == before


@pytest.mark.parametrize("receipt", [None, {"schema_version": 1, "nonce": "old-invocation", "write_outcome": "not_started"}])
def test_default_wrapper_requires_matching_receipt(monkeypatch, tmp_path, receipt):
    _, path = retry_snapshot(monkeypatch, tmp_path, [failed_run(1)], [failed_job(1)])
    monkeypatch.setattr(gh_pr_watch, "GH_COMMAND", str(gh_pr_watch.DEFAULT_GH))
    text = "error: unable to verify the automation GitHub actor; refusing write\n"
    if receipt is not None:
        text += json.dumps(receipt) + "\n"
    monkeypatch.setattr(gh_pr_watch.subprocess, "Popen", lambda *_a, **_kw: SimpleNamespace(
        returncode=1, communicate=lambda timeout: ("", text),
    ))
    result = gh_pr_watch.retry_failed_now(argparse.Namespace())
    assert result["reason"] == "rerun_outcome_unknown"
    assert result["retries_used"] == 1
    assert gh_pr_watch.load_state(path)[0]["pending_reruns_by_sha"]["abc123"]["1"]["outcome"] == "submitting"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
