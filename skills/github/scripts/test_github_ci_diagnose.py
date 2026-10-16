#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""End-to-end REST fixture tests for github-ci-diagnose.py."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/codex-skills-test.env"
os.environ["CODEX_AUTOMATION_LOGIN"] = "fixture-automation"
os.environ["CODEX_AUTOMATION_EMAIL"] = "fixture-automation@example.invalid"
os.environ["GH_WITH_ENV_TOKEN_EXPECTED_LOGIN"] = "fixture-automation"

import github_api  # noqa: E402


SCRIPT = Path(__file__).with_name("github-ci-diagnose.py")


FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["FAKE_GH_RESPONSES"], encoding="utf-8") as handle:
    responses = json.load(handle)
path = next((arg for arg in sys.argv[1:] if arg.startswith("/") or arg.startswith("http")), "")
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1:] == ["api", "--help"]:
    print("api flags: --allow-escape-sequences")
    raise SystemExit(0)
entry = responses.get(path)
if entry is None:
    print(f"unexpected path: {path}", file=sys.stderr)
    raise SystemExit(2)
status = int(entry.get("status", 200))
content_type = entry.get("content_type", "application/json")
print(f"HTTP/2.0 {status} ")
print(f"content-type: {content_type}")
print("x-github-request-id: CI:123")
print("x-ratelimit-limit: 5000")
print("x-ratelimit-remaining: 4990")
print("x-ratelimit-reset: 1784304000")
print("x-ratelimit-used: 10")
print("x-ratelimit-resource: core")
print()
body = entry.get("body")
if isinstance(body, str) and "\x1b" in body and "--allow-escape-sequences" not in sys.argv:
    print("response contains terminal escape sequences", file=sys.stderr)
    raise SystemExit(1)
if body is not None:
    print(body if isinstance(body, str) else json.dumps(body))
raise SystemExit(0 if 200 <= status < 300 else 1)
'''


def pull() -> dict[str, Any]:
    return {
        "number": 7,
        "title": "Demo",
        "state": "open",
        "draft": False,
        "html_url": "https://github.com/o/r/pull/7",
        "head": {"sha": "abc", "ref": "feature", "repo": {"full_name": "o/r"}},
        "base": {"ref": "main", "repo": {"full_name": "o/r"}},
    }


def run_metadata() -> dict[str, Any]:
    return {
        "id": 22,
        "name": "CI",
        "display_title": "Run tests",
        "status": "completed",
        "conclusion": "failure",
        "head_branch": "feature",
        "head_sha": "abc",
        "event": "pull_request",
        "created_at": "2026-07-17T00:00:00Z",
        "html_url": "https://github.com/o/r/actions/runs/22",
        "run_attempt": 1,
    }


def base_responses(details_url: str = "https://github.com/o/r/actions/runs/22/job/33") -> dict[str, Any]:
    return {
        "/repos/o/r/pulls/7": {"body": pull()},
        "/repos/o/r/commits/abc/check-runs?per_page=100&page=1": {
            "body": {
                "check_runs": [{
                    "id": 11,
                    "name": "tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "details_url": details_url,
                    "check_suite": {"id": 44},
                    "app": {"name": "GitHub Actions"},
                }],
            },
        },
        "/repos/o/r/commits/abc/statuses?per_page=100&page=1": {"body": []},
        "/repos/o/r/commits/abc/status": {"body": {"state": "failure"}},
        "/repos/o/r/actions/runs?head_sha=abc&per_page=100&page=1": {"body": {"workflow_runs": [run_metadata()]}},
        "/repos/o/r/actions/runs/22": {"body": run_metadata()},
        "/repos/o/r/actions/jobs/33/logs": {
            "body": "setup\n\x1b[31merror: assertion failed\x1b[0m\nsummary",
            "content_type": "text/plain",
        },
    }


def run_fixture(
    responses: dict[str, Any],
    *,
    pr: str | None = "7",
    github_repo: str | None = "o/r",
    origin: str | None = "git@github.com:o/r.git",
    upstream: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=root, check=True)
        if origin:
            subprocess.run(["git", "remote", "add", "origin", origin], cwd=root, check=True)
        if upstream:
            subprocess.run(["git", "remote", "add", "upstream", upstream], cwd=root, check=True)
        fake_gh = root / "fake-gh.py"
        fake_gh.write_text(FAKE_GH, encoding="utf-8")
        fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IXUSR)
        responses_path = root / "responses.json"
        responses_path.write_text(json.dumps(responses), encoding="utf-8")
        log_path = root / "calls.log"
        env = {
            **os.environ,
            "GITHUB_CI_DIAGNOSE_GH": str(fake_gh),
            "FAKE_GH_RESPONSES": str(responses_path),
            "FAKE_GH_LOG": str(log_path),
            "GITHUB_RETRY_STATE_DIR": str(root / "retry-state"),
            "CODEX_SKILLS_ENV_FILE": str(root / "missing.env"),
        }
        command = [sys.executable, str(SCRIPT), "--repo", "."]
        if github_repo:
            command.extend(["--github-repo", github_repo])
        if pr:
            command.extend(["--pr", pr])
        command.append("--json")
        process = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        calls = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
        return process, calls


def test_failing_check_uses_rest_metadata_and_job_log() -> None:
    process, calls = run_fixture(base_responses())
    assert process.returncode == 1, process.stderr
    payload = json.loads(process.stdout)
    assert payload["failingCount"] == 1
    assert payload["checks"][0]["jobId"] == "33"
    assert "error: assertion failed" in payload["checks"][0]["failureSnippet"]
    assert "\x1b" not in payload["checks"][0]["failureSnippet"]
    assert payload["diagnostics"]["degraded"] is False
    assert all(call == "api --help" or call.startswith("api --method GET --include") for call in calls)
    assert any("/actions/jobs/33/logs" in call and "--allow-escape-sequences" in call for call in calls)
    assert not any("pr checks" in call or "run view" in call for call in calls)


def test_job_reader_maps_check_name_when_url_has_no_job_id() -> None:
    responses = base_responses("https://github.com/o/r/actions/runs/22")
    responses["/repos/o/r/actions/runs/22/jobs?filter=latest&per_page=100&page=1"] = {
        "body": {"jobs": [{"id": 33, "run_id": 22, "name": "tests", "status": "completed", "conclusion": "failure"}]},
    }
    process, _ = run_fixture(responses)
    payload = json.loads(process.stdout)
    assert process.returncode == 1
    assert payload["checks"][0]["jobId"] == "33"


def synthetic_check_responses() -> tuple[dict[str, Any], dict[str, Any], str]:
    responses = base_responses()
    actions_check = responses["/repos/o/r/commits/abc/check-runs?per_page=100&page=1"]["body"]["check_runs"][0]
    check = {
        **actions_check,
        "id": actions_check["id"] + 1,
        "name": "CodeQL",
        "check_suite": {"id": 55},
    }
    check["details_url"] = f"https://github.com/o/r/runs/{check['id']}"
    responses["/repos/o/r/commits/abc/check-runs?per_page=100&page=1"]["body"]["check_runs"].append(check)
    annotations_path = f"/repos/o/r/check-runs/{check['id']}/annotations?per_page=100&page=1"
    responses[annotations_path] = {"body": [{
        "path": "src/example.py",
        "start_line": 3,
        "end_line": 3,
        "annotation_level": "failure",
        "message": "Unsafe data flow in fixture",
    }]}
    for suffix in ("", "/jobs?filter=latest&per_page=100&page=1"):
        responses[f"/repos/o/r/actions/runs/{check['id']}{suffix}"] = {
            "status": 404, "body": {"message": "Not Found"},
        }
    return responses, check, annotations_path


def test_synthetic_failure_preserves_annotations_without_actions_run_lookup() -> None:
    responses, synthetic, annotations_path = synthetic_check_responses()
    process, calls = run_fixture(responses)
    payload = json.loads(process.stdout)
    assert process.returncode == 1, process.stderr
    assert payload["failingCount"] == 2
    assert payload["externalCount"] == 0
    actions, check = payload["checks"]
    assert actions["failureSnippet"]
    assert check["classification"] == "failing"
    assert check["conclusion"] == synthetic["conclusion"]
    assert check["detailsUrl"] == synthetic["details_url"]
    assert check["checkRunId"] == str(synthetic["id"])
    assert check["runId"] is None and check["jobId"] is None
    assert check["annotations"] == responses[annotations_path]["body"]
    assert check["note"] and "error" not in check
    assert payload["diagnostics"]["degraded"] is False
    assert any(annotations_path in call for call in calls)
    assert not any(f"/actions/runs/{synthetic['id']}" in call for call in calls)
    assert not any(f"/actions/jobs/{synthetic['id']}" in call for call in calls)


def test_synthetic_pending_stays_pending_without_actions_or_annotation_lookup() -> None:
    responses, synthetic, annotations_path = synthetic_check_responses()
    synthetic.update(status="in_progress", conclusion=None)
    process, calls = run_fixture(responses)
    payload = json.loads(process.stdout)
    assert process.returncode == 1, process.stderr
    assert payload["pendingCount"] == 1 and payload["failingCount"] == 1
    check = payload["checks"][1]
    assert check["classification"] == "pending"
    assert check["runId"] is None
    assert not any(f"/actions/runs/{synthetic['id']}" in call for call in calls)
    assert not any(annotations_path in call for call in calls)


def test_unavailable_synthetic_annotations_keep_failure_evidence() -> None:
    responses, synthetic, annotations_path = synthetic_check_responses()
    responses[annotations_path] = {"status": 403, "body": {"message": "Resource not accessible by integration"}}
    process, calls = run_fixture(responses)
    payload = json.loads(process.stdout)
    assert process.returncode == 1, process.stderr
    assert payload["failingCount"] == 2
    check = payload["checks"][1]
    assert check["classification"] == "failing"
    assert check["detailsUrl"] == synthetic["details_url"]
    assert check["annotationError"]
    assert "checkAnnotations" in payload["diagnostics"]["degradedComponents"]
    assert not any(f"/actions/runs/{synthetic['id']}" in call for call in calls)


def test_missing_run_permission_degrades_but_keeps_log_evidence() -> None:
    responses = base_responses()
    responses["/repos/o/r/actions/runs/22"] = {
        "status": 403,
        "body": {"message": "Resource not accessible by integration"},
    }
    process, _ = run_fixture(responses)
    payload = json.loads(process.stdout)
    assert process.returncode == 1
    assert payload["failingCount"] == 1
    assert "assertion failed" in payload["checks"][0]["failureSnippet"]
    assert payload["diagnostics"]["degraded"] is True
    assert "workflowRun" in payload["diagnostics"]["degradedComponents"]
    assert any(request.get("cause") == "permission_denied" for request in payload["diagnostics"]["requests"])


def test_missing_status_permission_keeps_check_runs_and_marks_counts_partial() -> None:
    responses = base_responses()
    responses["/repos/o/r/commits/abc/statuses?per_page=100&page=1"] = {
        "status": 403,
        "body": {"message": "Resource not accessible by integration"},
    }
    process, _ = run_fixture(responses)
    payload = json.loads(process.stdout)
    assert process.returncode == 1
    assert payload["failingCount"] == 1
    assert payload["countsComplete"] is False
    assert "commitStatuses" in payload["unavailableCheckComponents"]
    assert payload["checks"][0]["failureSnippet"]


def test_external_status_remains_explicit_without_actions_job_reads() -> None:
    responses = {
        "/repos/o/r/pulls/7": {"body": pull()},
        "/repos/o/r/commits/abc/check-runs?per_page=100&page=1": {"body": {"check_runs": []}},
        "/repos/o/r/commits/abc/statuses?per_page=100&page=1": {
            "body": [{
                "id": 99,
                "context": "external-ci",
                "state": "failure",
                "description": "failed",
                "target_url": "https://ci.example.invalid/build/1",
            }],
        },
        "/repos/o/r/commits/abc/status": {"body": {"state": "failure"}},
        "/repos/o/r/actions/runs?head_sha=abc&per_page=100&page=1": {"body": {"workflow_runs": []}},
    }
    process, calls = run_fixture(responses)
    payload = json.loads(process.stdout)
    assert process.returncode == 0
    assert payload["externalCount"] == 1
    assert payload["failingCount"] == 0
    assert payload["checks"][0]["classification"] == "external"
    assert any("/actions/runs?head_sha=abc" in call for call in calls)
    assert not any("/actions/jobs/" in call for call in calls)


def test_explicit_pr_url_does_not_require_origin_remote() -> None:
    process, _ = run_fixture(
        base_responses(),
        pr="https://github.com/o/r/pull/7",
        github_repo=None,
        origin=None,
    )
    payload = json.loads(process.stdout)
    assert process.returncode == 1
    assert payload["repo"] == "o/r"
    assert payload["pr"] == "https://github.com/o/r/pull/7"


def test_current_branch_pr_can_resolve_from_upstream_for_fork() -> None:
    fork_pull = pull()
    fork_pull["head"]["repo"]["full_name"] = "fork/r"
    fork_pull["base"]["repo"]["full_name"] = "base/r"
    responses = {
        "/repos/fork/r/pulls?state=open&head=fork%3Afeature&per_page=2&page=1": {"body": []},
        "/repos/base/r/pulls?state=open&head=fork%3Afeature&per_page=2&page=1": {"body": [fork_pull]},
        "/repos/base/r/pulls/7": {"body": fork_pull},
        "/repos/base/r/commits/abc/check-runs?per_page=100&page=1": {"body": {"check_runs": []}},
        "/repos/base/r/commits/abc/statuses?per_page=100&page=1": {"body": []},
        "/repos/base/r/commits/abc/status": {"body": {"state": "success"}},
        "/repos/base/r/actions/runs?head_sha=abc&per_page=100&page=1": {"body": {"workflow_runs": []}},
    }
    process, calls = run_fixture(
        responses,
        pr=None,
        github_repo=None,
        origin="git@github.com:fork/r.git",
        upstream="git@github.com:base/r.git",
    )
    payload = json.loads(process.stdout)
    assert process.returncode == 0, process.stderr
    assert payload["repo"] == "base/r"
    assert payload["pr"] == "7"
    assert any("/repos/base/r/pulls?state=open&head=fork%3Afeature" in call for call in calls)


def test_primary_branch_match_skips_inaccessible_upstream() -> None:
    origin_pull = pull()
    origin_pull["head"]["repo"]["full_name"] = "fork/r"
    origin_pull["base"]["repo"]["full_name"] = "fork/r"
    responses = {
        "/repos/fork/r/pulls?state=open&head=fork%3Afeature&per_page=2&page=1": {"body": [origin_pull]},
        "/repos/fork/r/pulls/7": {"body": origin_pull},
        "/repos/fork/r/commits/abc/check-runs?per_page=100&page=1": {"body": {"check_runs": []}},
        "/repos/fork/r/commits/abc/statuses?per_page=100&page=1": {"body": []},
        "/repos/fork/r/commits/abc/status": {"body": {"state": "success"}},
        "/repos/fork/r/actions/runs?head_sha=abc&per_page=100&page=1": {"body": {"workflow_runs": []}},
    }
    process, calls = run_fixture(
        responses,
        pr=None,
        github_repo=None,
        origin="git@github.com:fork/r.git",
        upstream="git@github.com:private/r.git",
    )
    payload = json.loads(process.stdout)
    assert process.returncode == 0, process.stderr
    assert payload["repo"] == "fork/r"
    assert not any("/repos/private/r/pulls" in call for call in calls)


def test_fixture_ignores_inherited_live_cooldown() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp)
        policy = github_api.RetryPolicy(state_dir=state_dir)
        store = github_api.SharedCooldownStore(state_dir)
        actor = github_api.github_identity.automation_login()
        key = github_api._cooldown_key("github.com", actor, "rest_core")
        store.publish(key, ready_at=github_api.time.time() + 120,
                      cause="secondary_rate_limited", now=github_api.time.time(), policy=policy)
        before = {path.name: path.read_bytes() for path in state_dir.iterdir()}
        with patch.dict(os.environ, {"GITHUB_RETRY_STATE_DIR": str(state_dir),
                                   "GITHUB_RETRY_MAX_WAIT_SECONDS": "2"}):
            process, calls = run_fixture(base_responses())
        payload = json.loads(process.stdout)
        assert payload["failingCount"] == 1, payload
        assert payload["diagnostics"]["degraded"] is False, payload
        assert {path.name: path.read_bytes() for path in state_dir.iterdir()} == before



def test_queued_replacement_remains_pending_in_diagnosis() -> None:
    responses = base_responses("https://github.com/o/r/actions/runs/22/job/11")
    check = responses["/repos/o/r/commits/abc/check-runs?per_page=100&page=1"]["body"]["check_runs"][0]
    check.update({"head_sha": "abc", "app": {"slug": "github-actions"}, "conclusion": "cancelled"})
    old = {"id": 22, "workflow_id": 71, "run_number": 1, "run_attempt": 1,
           "head_sha": "abc", "head_branch": "feature", "head_repository": {"id": 81},
           "event": "pull_request", "check_suite_id": 44, "status": "completed", "conclusion": "cancelled"}
    new = {**old, "id": 23, "run_number": 2, "check_suite_id": 45, "status": "queued", "conclusion": None}
    responses["/repos/o/r/actions/runs?head_sha=abc&per_page=100&page=1"] = {"body": {"workflow_runs": [old, new]}}
    process, _ = run_fixture(responses)
    payload = json.loads(process.stdout)
    assert payload["failingCount"] == 0
    assert payload["pendingCount"] == 1
    assert payload["countsComplete"] is True



def test_skipped_replacement_does_not_count_historical_failure() -> None:
    responses = base_responses("https://github.com/o/r/actions/runs/22/job/11")
    check = responses["/repos/o/r/commits/abc/check-runs?per_page=100&page=1"]["body"]["check_runs"][0]
    check.update({"head_sha": "abc", "app": {"slug": "github-actions"}})
    current = {**check, "id": 21, "check_suite": {"id": 45}, "conclusion": "skipped",
               "details_url": "https://github.com/o/r/actions/runs/23/job/21"}
    responses["/repos/o/r/commits/abc/check-runs?per_page=100&page=1"]["body"]["check_runs"].append(current)
    old = {"id": 22, "workflow_id": 71, "run_number": 1, "run_attempt": 1,
           "head_sha": "abc", "head_branch": "feature", "head_repository": {"id": 81},
           "event": "pull_request", "check_suite_id": 44, "status": "completed", "conclusion": "cancelled"}
    new = {**old, "id": 23, "run_number": 2, "check_suite_id": 45, "conclusion": "success"}
    responses["/repos/o/r/actions/runs?head_sha=abc&per_page=100&page=1"] = {"body": {"workflow_runs": [old, new]}}
    responses["/repos/o/r/actions/jobs/11/logs"] = responses["/repos/o/r/actions/jobs/33/logs"]
    process, calls = run_fixture(responses)
    payload = json.loads(process.stdout)
    assert process.returncode == 0
    assert payload["failingCount"] == payload["pendingCount"] == 0
    assert payload["countsComplete"] is True
    assert not any("/actions/jobs/11/logs" in call for call in calls)


def main() -> None:
    tests = [
        test_synthetic_failure_preserves_annotations_without_actions_run_lookup,
        test_synthetic_pending_stays_pending_without_actions_or_annotation_lookup,
        test_unavailable_synthetic_annotations_keep_failure_evidence,
        test_skipped_replacement_does_not_count_historical_failure,
        test_queued_replacement_remains_pending_in_diagnosis,
        test_fixture_ignores_inherited_live_cooldown,
        test_failing_check_uses_rest_metadata_and_job_log,
        test_job_reader_maps_check_name_when_url_has_no_job_id,
        test_missing_run_permission_degrades_but_keeps_log_evidence,
        test_missing_status_permission_keeps_check_runs_and_marks_counts_partial,
        test_external_status_remains_explicit_without_actions_job_reads,
        test_explicit_pr_url_does_not_require_origin_remote,
        test_current_branch_pr_can_resolve_from_upstream_for_fork,
        test_primary_branch_match_skips_inaccessible_upstream,
    ]
    failed: list[str] = []
    for test in tests:
        try:
            test()
            print(f"ok {test.__name__}")
        except Exception as exc:
            print(f"FAIL {test.__name__}: {exc}", file=sys.stderr)
            failed.append(test.__name__)
    print()
    if failed:
        print(f"{len(failed)}/{len(tests)} tests FAILED", file=sys.stderr)
        raise SystemExit(1)
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
