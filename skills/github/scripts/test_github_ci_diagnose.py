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
import urllib.parse
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
from urllib.parse import urlsplit, urlunsplit

with open(os.environ["FAKE_GH_RESPONSES"], encoding="utf-8") as handle:
    responses = json.load(handle)
path = next((arg for arg in sys.argv[1:] if arg.startswith("/") or arg.startswith("http")), "")
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1:] == ["api", "--help"]:
    print("api flags: --allow-escape-sequences")
    raise SystemExit(0)
entry = responses.get(path)
if entry is None and "/annotations?" in path:
    parsed = urlsplit(path)
    entry = responses.get(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))
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
    max_lines: int | None = None,
    json_output: bool = True,
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
        if max_lines is not None:
            command.extend(["--max-lines", str(max_lines)])
        if json_output:
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


def snippet_for_log(lines: list[str], max_lines: int = 60) -> str:
    responses = base_responses()
    responses["/repos/o/r/actions/jobs/33/logs"]["body"] = "\n".join(lines)
    process, _ = run_fixture(responses, max_lines=max_lines)
    assert process.returncode == 1, process.stderr
    payload = json.loads(process.stdout)
    assert payload["diagnostics"]["degraded"] is False
    snippet = payload["checks"][0]["failureSnippet"]
    assert len(snippet.splitlines()) <= max_lines
    return snippet


def test_unittest_failure_precedes_database_teardown_errors() -> None:
    for kind, exception in (("FAIL", "AssertionError: 503 != 202: authz_policy_unavailable"),
                            ("ERROR", "KeyError: 'result'")):
        header = f"{kind}: test_hold (tests.HoldTests.test_hold)"
        lines = ["setup"] * 50 + [
            "=" * 70, header, "-" * 70, "Traceback (most recent call last):",
            '  File "tests/test_hold.py", line 20, in test_hold',
            "    self.assertEqual(response.status_code, 202)", exception,
            "", "-" * 70, "Ran 12 tests in 1.0s", "", "FAILED (failures=1)",
            "##[error]Process completed with exit code 1.",
        ] + ["postgres | ERROR: duplicate key value violates unique constraint"] * 100
        # Actions timestamps must not hide the test runner's summary syntax.
        snippet = snippet_for_log([f"2026-10-16T01:14:44.0000000Z {line}" for line in lines])
        assert header in snippet, snippet
        assert exception in snippet, snippet


def test_pytest_failure_summary_precedes_teardown_noise() -> None:
    for kind in ("FAILED", "ERROR"):
        failure = f"{kind} tests/test_hold.py::test_hold - AssertionError: missing policy"
        snippet = snippet_for_log(["setup"] * 50 + [failure] + ["cleanup"] * 40 + [
            "postgres | ERROR: duplicate key value violates unique constraint",
        ])
        assert failure in snippet, snippet


def test_service_error_with_module_path_does_not_impersonate_pytest_summary() -> None:
    failure = "FAILED tests/test_hold.py::test_hold - AssertionError: missing policy"
    snippet = snippet_for_log([failure] + ["cleanup"] * 70 + ["ERROR my_crate::db: connection reset"])
    assert failure in snippet, snippet


def test_failed_command_precedes_teardown_noise_without_test_summary() -> None:
    failure = "##[error]Process completed with exit code 2."
    snippet = snippet_for_log(["compiler: fatal error: missing header", failure]
                              + ["cleanup"] * 60 + ["postgres | ERROR: expected constraint failure"])
    assert failure in snippet, snippet
    assert "missing header" in snippet, snippet


def test_small_snippet_keeps_selected_failure_marker() -> None:
    header = "FAIL: test_hold (tests.HoldTests.test_hold)"
    snippet = snippet_for_log(["setup"] * 40 + [header] + ["trace detail"] * 40, max_lines=1)
    assert header in snippet, snippet


def test_short_excerpt_at_log_end_keeps_available_causal_context() -> None:
    cause = "linker could not resolve fixture symbol"
    failure = "##[error]Process completed with exit code 2."
    snippet = snippet_for_log(["setup"] * 40 + [cause] + ["linker detail"] * 5 + [failure], max_lines=7)
    assert cause in snippet, snippet
    assert failure in snippet, snippet


def test_later_failed_command_outranks_earlier_continued_test_failure() -> None:
    earlier = "FAIL: test_optional (tests.OptionalTests.test_optional)"
    cause = "compiler: fatal error: missing header"
    failure = "##[error]Process completed with exit code 2."
    snippet = snippet_for_log([earlier, "##[error]Process completed with exit code 1."]
                              + ["build output"] * 80 + [cause, failure]
                              + ["cleanup"] * 80 + ["postgres | ERROR: expected constraint failure"])
    assert cause in snippet, snippet
    assert failure in snippet, snippet
    assert earlier not in snippet, snippet


def test_logs_without_test_summary_keep_bounded_fallbacks() -> None:
    tail = ["ok helper"] * 100 + ["error: one or more helper tests failed", "Error: exit status 1"]
    for failure in (
        "Traceback (most recent call last):\nsubprocess.TimeoutExpired: helper deadline",
        "not ok fixture", "FAILED fixture.py",
    ):
        snippet = snippet_for_log([f"2026-10-23T18:00:00Z {line}" for line in failure.splitlines()] + tail)
        assert all(cause in snippet for cause in failure.splitlines()), snippet
    failure = "fatal: compiler failed to start"
    snippet = snippet_for_log(["setup"] * 60 + [failure, "compiler detail", "done"])
    assert failure in snippet, snippet
    assert "compiler detail" in snippet, snippet
    tail = [f"progress {index}" for index in range(80)]
    assert snippet_for_log(tail, max_lines=7).splitlines() == tail[-7:]


def test_failed_command_keeps_earlier_generic_cause_before_teardown() -> None:
    cause = "fatal: linker cannot resolve fixture symbol"
    snippet = snippet_for_log(["setup"] * 40 + [cause] + ["linker progress"] * 70
                              + ["##[error]Process completed with exit code 2."]
                              + ["cleanup"] * 70 + ["postgres | ERROR: expected constraint failure"])
    assert cause in snippet, snippet


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
    annotations_path = f"/repos/o/r/check-runs/{check['id']}/annotations"
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


def test_third_party_check_run_retains_failure_and_annotations() -> None:
    responses, check, annotations_path = synthetic_check_responses()
    check.update(name="coverage", details_url="https://ci.example.invalid/build/coverage")
    process, calls = run_fixture(responses)
    payload = json.loads(process.stdout)
    assert process.returncode == 1
    result = payload["checks"][1]
    assert result["classification"] == "failing"
    assert result["detailsUrl"] == check["details_url"]
    assert result["annotations"] == responses[annotations_path]["body"]
    assert payload["failingCount"] == 2 and payload["externalCount"] == 0
    assert not any(f"/actions/runs/{check['id']}" in call for call in calls)


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


def test_synthetic_annotations_are_bounded_and_terminal_controls_removed() -> None:
    responses, _, path = synthetic_check_responses()
    annotation = responses[path]["body"][0]
    message = annotation["message"]
    annotation["message"] = f"\x1b]0;fixture-title\x07\x1b[31m{message}\x1b[0m"
    annotation["path"] = f"\x1b[31m{annotation['path']}\x1b[0m"
    # One extra item lets the helper distinguish a truncated list from a full one.
    responses[path] = {"body": [annotation, annotation]}
    process, calls = run_fixture(responses, max_lines=1)
    payload = json.loads(process.stdout)
    check = payload["checks"][1]
    assert len(check["annotations"]) == 1
    assert check["annotationsTruncated"] is True
    assert check["annotations"][0]["message"] == message
    assert all("\x1b" not in str(value) for value in check["annotations"][0].values())
    assert sum(urllib.parse.urlsplit(call.split()[-1]).path == path for call in calls) == 1
    process, _ = run_fixture(responses, max_lines=1, json_output=False)
    assert process.returncode == 1
    assert message in process.stdout
    assert "\x1b" not in process.stdout


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
        test_unittest_failure_precedes_database_teardown_errors,
        test_pytest_failure_summary_precedes_teardown_noise,
        test_service_error_with_module_path_does_not_impersonate_pytest_summary,
        test_failed_command_precedes_teardown_noise_without_test_summary,
        test_small_snippet_keeps_selected_failure_marker,
        test_short_excerpt_at_log_end_keeps_available_causal_context,
        test_later_failed_command_outranks_earlier_continued_test_failure,
        test_logs_without_test_summary_keep_bounded_fallbacks,
        test_failed_command_keeps_earlier_generic_cause_before_teardown,
        test_third_party_check_run_retains_failure_and_annotations,
        test_synthetic_annotations_are_bounded_and_terminal_controls_removed,
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
