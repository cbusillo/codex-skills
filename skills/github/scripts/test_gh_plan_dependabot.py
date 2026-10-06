#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline behavior tests for old Dependabot PR discovery in next."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from test_gh_plan_next import (
    global_fixture, issue, load_module as load_next_module, next_args, relationships, track,
)

NOW = datetime(2026, 10, 5, 18, tzinfo=timezone.utc)


def load_module() -> Any:
    module = load_next_module()
    module.next_dependabot_work = module.real_next_dependabot_work
    return module


def pull(number=1, *, hours=48, author="dependabot[bot]", state="open", base="main", repo="owner/repo"):
    return {
        "number": number, "html_url": f"https://github.com/{repo}/pull/{number}",
        "title": "Bump dependencies", "state": state, "user": {"login": author},
        "created_at": (NOW - timedelta(hours=hours)).isoformat(),
        "base": {"ref": base}, "head": {"sha": f"head-{number}"},
    }


def enrolled(status="enrolled"):
    return {"source": "launchplane", "status": status, "targets": [{"baseBranch": "main"}]}


def discover(module: Any, pulls, *, sources=None, limit=10, scan_limit=50, status="enrolled", inventory_complete=True):
    reads = Mock(return_value=("automation-gh", pulls))
    with patch.multiple(module, read_next_train_enrollment=Mock(return_value=enrolled(status)), collect_paged_rest_items=reads):
        result = module.next_dependabot_work(
            sources or [{"repo": "owner/repo"}], scan_limit=scan_limit,
            limit=limit, now=NOW, inventory_complete=inventory_complete,
        )
    return result, reads


def test_age_author_state_branch_and_pr_identity():
    module: Any = load_module()
    result, reads = discover(module, [
        pull(hours=24), pull(2, hours=24 + 1 / 3600), pull(3, hours=23),
        pull(4, hours=72), pull(5, author="someone"), pull(6, state="closed"),
        pull(7, base="unlisted"), pull(8, hours=-1),
    ])
    assert [item["number"] for item in result["dependabot_candidates"]] == [4, 2]
    item = result["dependabot_candidates"][0]
    assert item["url"].endswith("/pull/4") and item["record_type"] == "pull_request"
    assert item["head_sha"] == "head-4" and item["age_hours"] == 72
    assert item["age"] == "72.0 hours" and item["ownership"] == "not_checked"
    assert "held" not in json.dumps(result) and "major" not in json.dumps(result)
    assert reads.call_args.args == ("/repos/owner/repo/pulls",)
    assert reads.call_args.kwargs["query"] == {"state": "open", "sort": "created", "direction": "asc"}
    assert reads.call_args.kwargs["limit"] == 51
    assert result["dependabot_context"]["complete"] is True


def test_enrollment_unknown_preserves_observations_without_claiming_eligibility():
    module: Any = load_module()
    result, reads = discover(module, [pull()], status="not_enrolled")
    assert result["dependabot_candidates"] == [] and reads.call_count == 1
    result, reads = discover(module, [pull()], status="unknown")
    assert reads.call_count == 1 and result["dependabot_candidates"] == []
    assert result["dependabot_unverified_candidates"][0]["enrollment"] == "unknown"
    assert result["dependabot_context"]["complete"] is False


def test_bounds_failures_and_invalid_dates_are_explicit():
    module: Any = load_module()
    result, _ = discover(module, [pull(n) for n in range(1, 5)], scan_limit=3, limit=1, inventory_complete=False)
    assert result["dependabot_candidate_count"] == 3 and len(result["dependabot_candidates"]) == 1
    context = result["dependabot_context"]
    assert context["complete"] is False and context["result_truncated"] is True
    assert context["repositories"][0]["truncated"] is True
    for created in ("missing", "2026-10-01", None):
        result, _ = discover(module, [{**pull(), "created_at": created}])
        assert result["dependabot_candidates"] == []
        assert result["dependabot_context"]["complete"] is False
    with patch.multiple(
        module, read_next_train_enrollment=Mock(return_value=enrolled()),
        collect_paged_rest_items=Mock(side_effect=module.PlanError("PR read unavailable")),
    ):
        result = module.next_dependabot_work([{"repo": "owner/repo"}], scan_limit=5, limit=5, now=NOW)
    assert result["dependabot_context"]["complete"] is False
    assert result["dependabot_context"]["repositories"][0]["error"] == "PR read unavailable"


def test_holds_duplicates_and_issue_tracker_disabled():
    module: Any = load_module()
    sources = [
        {"repo": "owner/held", "hold": {"reason": "Director hold"}},
        {"repo": "other/repo", "exclusion": "other_owner"},
        {"repo": "owner/archive", "exclusion": "archived_or_disabled"},
        {"repo": "owner/repo", "exclusion": "issues_disabled"},
        {"repo": "OWNER/REPO"},
    ]
    result, reads = discover(module, [pull()], sources=sources)
    assert reads.call_count == 1 and result["dependabot_candidate_count"] == 1
    assert result["dependabot_context"]["repositories"][0]["exclusion"] == "repository_held"


def test_service_outage_is_read_once_but_pr_observations_continue():
    module: Any = load_module()
    sources = [{"repo": f"owner/repo-{number}"} for number in range(3)]
    service = Mock(return_value=enrolled("unknown"))
    github = Mock(return_value=("automation-gh", [pull()]))
    with patch.multiple(module, read_next_train_enrollment=service, collect_paged_rest_items=github):
        result = module.next_dependabot_work(sources, scan_limit=5, limit=5, now=NOW)
    assert service.call_count == 1 and github.call_count == 3
    assert result["dependabot_candidates"] == []
    assert result["dependabot_unverified_candidate_count"] == 3
    assert result["dependabot_context"]["complete"] is False
    assert result["dependabot_context"]["repositories"][1]["enrollment"]["reason"] == "earlier_enrollment_read_unavailable"


def test_classified_quota_and_auth_failures_stop_without_more_reads():
    module: Any = load_module()
    for cause in ("primary_rate_limit", "authentication_failed"):
        failure = module.github_api_core.FailureDetail(
            cause=cause, message="terminal failure", retryable=False,
            fallback_eligible=False, disposition="stop",
        )
        error = module.PlanError("terminal failure", failure=failure)
        reads = Mock(side_effect=error)
        with patch.multiple(module, read_next_train_enrollment=Mock(return_value=enrolled()), collect_paged_rest_items=reads):
            try:
                module.next_dependabot_work([{"repo": "owner/first"}, {"repo": "owner/second"}], scan_limit=5, limit=5, now=NOW)
            except module.PlanError as caught:
                assert caught is error
            else:
                raise AssertionError("classified failures must preserve the existing stop policy")
        assert reads.call_count == 1


def test_enrollment_is_only_read_for_repositories_with_old_dependabot_prs():
    module: Any = load_module()
    sources = [{"repo": f"owner/repo-{number}"} for number in range(3)]
    service = Mock(return_value=enrolled())
    github = Mock(side_effect=[("automation-gh", []), ("automation-gh", [pull(hours=1)]), ("automation-gh", [pull()])])
    with patch.multiple(module, read_next_train_enrollment=service, collect_paged_rest_items=github):
        result = module.next_dependabot_work(sources, scan_limit=5, limit=5, now=NOW)
    assert github.call_count == 3 and service.call_count == 1
    assert service.call_args.args == ("owner/repo-2",)
    assert result["dependabot_candidates"][0]["repo"] == "owner/repo-2"
    assert result["dependabot_context"]["repositories"][0]["enrollment"]["reason"] == "no_old_dependabot_prs"


def test_terminal_and_changed_head_disappear_or_refresh_on_next_read():
    module: Any = load_module()
    first, _ = discover(module, [pull()])
    updated, _ = discover(module, [{**pull(), "head": {"sha": "updated-head"}}])
    assert first["dependabot_candidates"][0]["head_sha"] != updated["dependabot_candidates"][0]["head_sha"]
    closed, _ = discover(module, [pull(state="closed")])
    assert closed["dependabot_candidates"] == []


def test_policy_reader_uses_existing_read_only_projection_and_hides_diagnostics():
    module: Any = load_module()
    reader = module.real_read_next_train_enrollment
    valid = {"status": "available", "result": enrolled()}
    with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(valid))) as run:
        assert reader("owner/repo") == valid["result"]
    argv = run.call_args.args[0]
    assert argv[-3:] == ["merge-train-policy-read", "--repo", "owner/repo"]
    assert run.call_args.kwargs["timeout"] == 30
    for response in (
        SimpleNamespace(returncode=1, stdout='{"status":"denied","private":"secret"}'),
        SimpleNamespace(returncode=0, stdout="invalid secret"),
        SimpleNamespace(returncode=0, stdout="[]"),
        SimpleNamespace(returncode=0, stdout='{"status":"available","result":null}'),
    ):
        with patch("subprocess.run", return_value=response):
            assert reader("owner/repo") == {"source": "launchplane", "status": "unknown"}
    for error in (OSError("private"), subprocess.TimeoutExpired("private", 30)):
        with patch("subprocess.run", side_effect=error):
            assert reader("owner/repo")["status"] == "unknown"


def test_local_next_preserves_issue_ranking_and_includes_prs():
    module: Any = load_module()
    output = {}
    def collect(path, **kwargs):
        if path.endswith("/pulls"):
            return "automation-gh", [pull()]
        assert path.endswith("/issues") and kwargs["issue_only"]
        return "automation-gh", [issue(10)]
    with patch.multiple(
        module, load_config=lambda *_: module.DEFAULT_CONFIG, load_direction=lambda *_: None,
        collect_paged_rest_items=collect, read_next_train_enrollment=lambda *_: enrolled(),
        next_focus_context=lambda *_: (None, {}, {"available": False}),
        read_next_issue_relationships=lambda *_: (None, relationships(), []), emit=output.update,
    ):
        module.cmd_next(next_args(repo="owner/repo"))
    assert [item["number"] for item in output["candidates"]] == [10]
    assert [item["number"] for item in output["dependabot_candidates"]] == [1]
    assert output["candidate_count"] == 1


def test_global_next_finds_pr_only_repositories_and_respects_holds():
    with global_fixture([], [], {}) as (module, output, _reads):
        module.next_dependabot_work = module.real_next_dependabot_work
        original = module.collect_paged_rest_items
        def collect(path, **kwargs):
            if path.endswith("/pulls"):
                assert path == "/repos/someone/pr-only/pulls"
                return "automation-gh", [pull(repo="someone/pr-only")]
            return original(path, **kwargs)
        with patch.multiple(
            module, collect_paged_rest_items=collect, read_next_train_enrollment=lambda *_: enrolled(),
            discover_direction_work=lambda *_args, **_kwargs: ([], {
                "complete": True, "inventory_truncated": True, "repositories": [
                    {"repo": "someone/pr-only", "exclusion": "issues_disabled"},
                    {"repo": "someone/held", "hold": {"reason": "Director hold"}},
                ],
            }),
        ):
            module.cmd_next(next_args())
        assert output["candidates"] == []
        assert output["dependabot_candidates"][0]["repo"] == "someone/pr-only"
        assert output["dependabot_context"]["complete"] is False
        assert output["dependabot_context"]["repositories"][1]["exclusion"] == "repository_held"


def test_global_milestone_reports_that_pr_discovery_was_not_run():
    root = track("someone/direction", 1, "First")
    with global_fixture([root], [], {}) as (module, output, _reads):
        module.next_dependabot_work = Mock(side_effect=AssertionError("unrelated PR discovery"))
        with patch.multiple(module.github_milestone_core, show_milestone=lambda *_args, **_kwargs: {"milestone": root["milestone"]}):
            module.cmd_next(next_args(milestone="First"))
        assert output["dependabot_context"] == {"complete": False, "exclusion": "explicit_milestone_scope"}
        assert module.next_dependabot_work.call_count == 0


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
