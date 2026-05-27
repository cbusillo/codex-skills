#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Regression checks for gh-plan.py command shapes.

These checks avoid live GitHub calls. They load gh-plan.py as a module and
replace subprocess.run with a tiny gh simulator so prompt/skill regressions fail
before they reach automation.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from contextlib import redirect_stdout
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from typing import Any, Optional


SCRIPT = Path(__file__).with_name("gh-plan.py")
PR_SCRIPT = Path(__file__).with_name("gh-pr.py")
REAL_SUBPROCESS_RUN = subprocess.run


def load_plan_module() -> Any:
    spec = importlib.util.spec_from_file_location("gh_plan_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def normalized_gh_args(command: list[str]) -> list[str]:
    if command and command[0].endswith("gh-with-env-token"):
        return command[1:]
    if command and command[0] == "gh":
        return command[1:]
    return command


def test_issue_body_updates_use_rest_patch() -> None:
    plan = load_plan_module()
    calls: list[tuple[list[str], Optional[str]]] = []
    issues: dict[tuple[str, int], dict[str, Any]] = {
        ("owner/repo", 1): {
            "repo": "owner/repo",
            "number": 1,
            "id": 1001,
            "title": "Source",
            "body": "## Relationships\n\nOld text\n",
            "html_url": "https://github.com/owner/repo/issues/1",
            "labels": [],
            "state": "open",
        },
        ("owner/repo", 2): {
            "repo": "owner/repo",
            "number": 2,
            "id": 1002,
            "title": "Target",
            "body": "",
            "html_url": "https://github.com/owner/repo/issues/2",
            "labels": [],
            "state": "open",
        },
    }

    def fake_run(gh_command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        input_text = kwargs.get("input")
        calls.append((gh_command, input_text))
        normalized_args = normalized_gh_args(gh_command)
        if not normalized_args or normalized_args[0] != "api":
            raise AssertionError(f"unexpected command: {gh_command}")
        endpoint = next((arg for arg in normalized_args if arg.startswith("repos/")), "")
        method = normalized_args[normalized_args.index("-X") + 1] if "-X" in normalized_args else "GET"
        parts = endpoint.split("/")
        repo = "/".join(parts[1:3])
        number = int(parts[4])
        if method == "GET":
            return completed(json.dumps(issues[(repo, number)]))
        if method == "PATCH":
            assert normalized_args[-2:] == ["--input", "-"], normalized_args
            payload = json.loads(input_text or "{}")
            assert set(payload) == {"body"}, payload
            issues[(repo, number)].update(payload)
            return completed(json.dumps(issues[(repo, number)]))
        raise AssertionError(f"unexpected api method: {method}")

    plan.subprocess.run = fake_run
    with redirect_stdout(StringIO()):
        plan.cmd_update_section(types.SimpleNamespace(repo="owner/repo", issue="1", section="Relationships", body="New body", body_file=None))
        plan.cmd_link(types.SimpleNamespace(repo="owner/repo", issue="1", relationship="related", target="2"))
        plan.cmd_unlink(types.SimpleNamespace(repo="owner/repo", issue="1", relationship="related", target="2"))

    patch_calls = [(cmd, body) for cmd, body in calls if "PATCH" in cmd]
    assert len(patch_calls) == 3, patch_calls
    for command, body in patch_calls:
        gh_args = normalized_gh_args(command)
        assert gh_args[0] == "api", command
        assert "issue" not in gh_args, command
        assert "--input" in gh_args and gh_args[gh_args.index("--input") + 1] == "-", command
        assert json.loads(body or "{}"), command


def test_project_commands_are_recoverable() -> None:
    plan = load_plan_module()
    calls: list[dict[str, Any]] = []

    def fake_gh_json(
        args: list[str],
        *,
        prefer_active: bool = False,
        recoverable: bool = False,
        **_kwargs: Any,
    ) -> tuple[str, Any]:
        calls.append({"args": args, "prefer_active": prefer_active, "recoverable": recoverable})
        if args[:2] == ["api", "-H"] and args[-1] == "rate_limit":
            return "active-gh-user", {"resources": {"graphql": {"remaining": 200, "reset": 999}}}
        if args[:2] == ["project", "list"]:
            return "active-gh-user", {"projects": [{"title": "Roadmap", "number": 7, "id": "project-id"}]}
        if args[:2] == ["project", "item-add"]:
            return "active-gh-user", {"id": "item-id"}
        raise AssertionError(f"unexpected gh_json args: {args}")

    plan.gh_json = fake_gh_json
    plan.load_config = lambda repo: {"projects": {"default_project": "Roadmap"}}
    plan.get_issue = lambda ref, repo: (
        "automation-gh",
        {"repo": "owner/repo", "number": 1, "html_url": "https://github.com/owner/repo/issues/1"},
    )
    with redirect_stdout(StringIO()):
        plan.cmd_project_add(types.SimpleNamespace(repo="owner/repo", issue="1", owner=None, project=None))
        plan.cmd_project_list(types.SimpleNamespace(owner="owner", limit=30, closed=False))

    project_calls = [call for call in calls if call["args"] and call["args"][0] == "project"]
    assert project_calls, calls
    assert all(call["recoverable"] for call in project_calls), project_calls


def test_create_reports_issue_when_project_sync_fails() -> None:
    plan = load_plan_module()
    issue = {
        "repo": "owner/repo",
        "number": 10,
        "id": 1010,
        "title": "Durable plan",
        "body": "## Finish Line\n\nShip it.\n",
        "html_url": "https://github.com/owner/repo/issues/10",
        "labels": [{"name": "plan"}, {"name": "plan:active"}],
        "state": "open",
    }

    plan.load_config = lambda repo: {
        "labels": {"plan": "plan", "active": "plan:active"},
        "projects": {"enabled": True, "owner": "owner", "default_project": "Roadmap"},
        "project_fields": {"focus": "Focus", "manager": "Manager", "finish_line": "Finish Line"},
        "workflow": {"default_manager": "Code", "repo_managers": {}},
    }
    original_gh_json = plan.gh_json

    def fake_gh_json(
        args: list[str],
        *,
        input_text: Optional[str] = None,
        prefer_active: bool = False,
        recoverable: bool = False,
    ) -> tuple[str, Any]:
        if args[:2] == ["issue", "list"]:
            return "automation-gh", []
        if args[:2] == ["api", "-H"] and args[-1] == "rate_limit":
            return "automation-gh", {"resources": {"graphql": {"remaining": 200, "reset": 999}}}
        return original_gh_json(
            args,
            input_text=input_text,
            prefer_active=prefer_active,
            recoverable=recoverable,
        )

    plan.gh_json = fake_gh_json
    plan.ensure_labels = lambda repo, wanted, config: ("automation-gh", [])
    plan.rest_create_issue = lambda repo, title, body, labels, milestone: ("automation-gh", issue)
    plan.resolve_project = lambda owner, project, recoverable=False: ("active-gh-user", 7, {"title": project, "number": 7})

    def fake_run_raw(
        args: list[str],
        *,
        recoverable: bool = False,
        **_kwargs: Any,
    ) -> tuple[str, str, str]:
        if args[:2] == ["project", "item-add"] and recoverable:
            raise plan.PlanError("project sync throttled")
        raise AssertionError(f"unexpected run_raw args: {args}")

    plan.run_raw = fake_run_raw
    output = StringIO()
    with redirect_stdout(output):
        plan.cmd_create(types.SimpleNamespace(
            repo="owner/repo",
            title="Durable plan",
            title_flag=None,
            body="## Finish Line\n\nShip it.\n",
            body_file=None,
            label=None,
            milestone=None,
            project=None,
            force=False,
            plan_status="active",
            focus="Now",
            manager=None,
            finish_line=None,
        ))

    payload = json.loads(output.getvalue())
    assert payload["ok"] is True, payload
    assert payload["issue"]["number"] == 10, payload
    assert payload["project_fields"]["error"] == "project sync throttled", payload
    assert payload["project_fields"]["error_code"] == "project_update_failed", payload


def test_close_reports_stale_project_as_non_blocking_warning() -> None:
    plan = load_plan_module()
    issue = {
        "repo": "owner/repo",
        "number": 10,
        "id": 1010,
        "title": "Durable plan",
        "body": "## Finish Line\n\nShip it.\n",
        "html_url": "https://github.com/owner/repo/issues/10",
        "labels": [{"name": "plan"}, {"name": "plan:active"}],
        "state": "open",
    }
    calls: list[list[str]] = []

    plan.load_config = lambda repo: {
        "labels": {"active": "plan:active", "done": "plan:done"},
        "projects": {"owner": "owner", "default_project": "Roadmap"},
        "project_fields": {"focus": "Focus"},
    }
    plan.get_issue = lambda ref, repo: ("automation-gh", issue)

    def fake_run_raw(args: list[str], **_kwargs: Any) -> tuple[str, str, str]:
        calls.append(args)
        if args[:2] in (["issue", "edit"], ["issue", "close"]):
            return "automation-gh", "", ""
        raise AssertionError(f"unexpected run_raw args: {args}")

    plan.run_raw = fake_run_raw
    plan.project_meta = lambda owner, project, recoverable=False: (_ for _ in ()).throw(
        plan.project_error(f"Project not found for {owner}: {project}")
    )

    output = StringIO()
    with redirect_stdout(output):
        plan.cmd_close(types.SimpleNamespace(
            repo="owner/repo",
            issue="10",
            reason="completed",
            body=None,
            body_file=None,
            owner=None,
            project=None,
        ))

    payload = json.loads(output.getvalue())
    assert payload["ok"] is True, payload
    assert payload["closed"]["number"] == 10, payload
    assert payload["project"]["warning"] is True, payload
    assert payload["project"]["blocking"] is False, payload
    assert payload["project"]["operation"] == "close_project_sync", payload
    assert payload["project"]["error_code"] == "lookup_stale", payload
    assert payload["project"]["target"] == {"owner": "owner", "project": "Roadmap"}, payload
    assert any(call[:2] == ["issue", "close"] for call in calls), calls


def test_create_supports_waiting_plan_status() -> None:
    plan = load_plan_module()
    captured: dict[str, Any] = {}
    issue = {
        "repo": "owner/repo",
        "number": 11,
        "id": 1011,
        "title": "Waiting plan",
        "body": "## Current Status\n\nWaiting for: evidence.\n",
        "html_url": "https://github.com/owner/repo/issues/11",
        "labels": [{"name": "plan"}, {"name": "plan:waiting"}],
        "state": "open",
    }

    plan.load_config = lambda repo: {
        "labels": {"plan": "plan", "waiting": "plan:waiting"},
        "projects": {"enabled": False},
        "project_fields": {"focus": "Focus", "manager": "Manager", "finish_line": "Finish Line"},
        "workflow": {"default_manager": None, "repo_managers": {}},
    }
    plan.gh_json = lambda args, **kwargs: ("automation-gh", []) if args[:2] == ["issue", "list"] else (_ for _ in ()).throw(AssertionError(args))
    plan.ensure_labels = lambda repo, wanted, config: (captured.setdefault("labels", wanted), ("automation-gh", []))[1]
    plan.rest_create_issue = lambda repo, title, body, labels, milestone: (captured.setdefault("created_labels", labels), ("automation-gh", issue))[1]

    output = StringIO()
    with redirect_stdout(output):
        plan.cmd_create(types.SimpleNamespace(
            repo="owner/repo",
            title="Waiting plan",
            title_flag=None,
            body="## Current Status\n\nWaiting for: evidence.\n",
            body_file=None,
            label=None,
            milestone=None,
            project=None,
            force=False,
            plan_status="waiting",
            focus=None,
            manager=None,
            finish_line=None,
        ))

    payload = json.loads(output.getvalue())
    assert payload["ok"] is True, payload
    assert captured["labels"] == ["plan", "plan:waiting"], captured
    assert captured["created_labels"] == ["plan", "plan:waiting"], captured


def test_run_raw_falls_back_only_for_graphql_rate_limit() -> None:
    plan = load_plan_module()
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0].endswith("gh-with-env-token"):
            return completed(stderr="GraphQL: API rate limit already exceeded", returncode=1)
        if command[0] == "gh":
            return completed(stdout='{"ok": true}')
        raise AssertionError(f"unexpected command: {command}")

    plan.subprocess.run = fake_run
    with redirect_stderr(StringIO()):
        actor, stdout, _ = plan.run_raw(["api", "rate_limit"], recoverable=True)
    assert actor == "active-gh-user", actor
    assert json.loads(stdout) == {"ok": True}, stdout
    assert len(calls) == 2, calls

    calls.clear()

    def fake_non_rate_failure(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0].endswith("gh-with-env-token"):
            return completed(stderr="HTTP 403: resource not accessible by integration", returncode=1)
        raise AssertionError(f"active gh should not be called for non-rate failure: {command}")

    plan.subprocess.run = fake_non_rate_failure
    try:
        plan.run_raw(["api", "repos/owner/repo"], recoverable=True)
    except plan.PlanError as exc:
        assert "resource not accessible" in str(exc), exc
    else:
        raise AssertionError("non-rate bot failure should not fall back to active gh")
    assert len(calls) == 1, calls


def test_run_raw_is_bot_first_even_when_prefer_active_is_requested() -> None:
    plan = load_plan_module()
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0].endswith("gh-with-env-token"):
            return completed(stdout='{"bot": true}')
        raise AssertionError(f"active gh should not be called before bot: {command}")

    plan.subprocess.run = fake_run
    actor, stdout, _ = plan.run_raw(["project", "list"], prefer_active=True, recoverable=True)
    assert actor == "automation-gh", actor
    assert json.loads(stdout) == {"bot": True}, stdout
    assert len(calls) == 1, calls


def test_pr_helper_uses_rest_endpoints_for_common_pr_work() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/repos/owner/repo/pulls?state=open'* ]]; then\n"
            "  if [[ \"$*\" != *'per_page=20'* ]]; then exit 2; fi\n"
            "  if [[ \"$*\" == *'--paginate'* ]]; then exit 2; fi\n"
            "  printf '[{\"number\":12,\"title\":\"Demo\",\"state\":\"open\",\"draft\":false,\"merged_at\":\"2026-05-19T01:23:52Z\",\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://github.com/owner/repo/pull/12\",\"head\":{\"ref\":\"topic\",\"sha\":\"head-sha\",\"repo\":{\"full_name\":\"owner/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"owner/repo\"}}}]\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12/merge'* ]]; then\n"
            "  printf '{\"merged\":true,\"sha\":\"merge-sha\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* ]]; then\n"
            "  printf '{\"number\":12,\"title\":\"Demo\",\"state\":\"open\",\"draft\":false,\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://github.com/owner/repo/pull/12\",\"head\":{\"ref\":\"topic\",\"sha\":\"head-sha\",\"repo\":{\"full_name\":\"owner/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"owner/repo\"}}}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/commits/head-sha/check-runs'* ]]; then\n"
            "  printf '{\"check_runs\":[{\"name\":\"ci\",\"status\":\"completed\",\"conclusion\":\"success\"}]}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/commits/head-sha/statuses'* ]]; then\n"
            "  printf '[]\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/commits/head-sha/status'* ]]; then\n"
            "  printf '{\"state\":\"success\"}\\n'\n"
            "elif [[ \"$*\" == *'/rate_limit'* ]]; then\n"
            "  printf '{\"resources\":{\"core\":{\"remaining\":4999},\"graphql\":{\"remaining\":0}}}\\n'\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path), GH_PR_TEST_LOG=str(log_path))
        view = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "view", "12"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        list_result = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "list", "--state", "open", "--limit", "20"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        checks = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "checks", "12"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        merge = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "merge", "12"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        rate = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "rate-limit"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        calls = log_path.read_text()
    view_pr = json.loads(view.stdout)["pr"]
    assert view_pr["number"] == 12
    assert view_pr["labels"] == []
    assert view_pr["isDraft"] is False
    assert view_pr["mergeStateStatus"] == "CLEAN"
    assert "reviewDecision" in view_pr and view_pr["reviewDecision"] is None
    assert "statusCheckRollup" in view_pr and view_pr["statusCheckRollup"] is None
    list_prs = json.loads(list_result.stdout)["pullRequests"]
    assert len(list_prs) == 1
    assert list_prs[0]["number"] == 12
    assert list_prs[0]["merged"] is True
    assert list_prs[0]["mergeStateStatus"] == "CLEAN"
    assert "reviewDecision" in list_prs[0] and list_prs[0]["reviewDecision"] is None
    assert "statusCheckRollup" in list_prs[0] and list_prs[0]["statusCheckRollup"] is None
    checks_summary = json.loads(checks.stdout)["summary"]
    assert checks_summary["combinedState"] is None
    assert checks_summary["combinedStateRaw"] == "success"
    assert checks_summary["legacyStatusesPresent"] is False
    assert json.loads(merge.stdout)["merge"]["merged"] is True
    assert json.loads(rate.stdout)["graphql"]["remaining"] == 0
    assert "/repos/owner/repo/pulls/12" in calls
    assert "/repos/owner/repo/pulls?state=open" in calls
    assert "/repos/owner/repo/pulls/12/merge" in calls
    assert "graphql" not in calls.lower()


def test_pr_helper_list_paginates_only_when_limit_exceeds_one_page() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'--paginate'* ]]; then exit 2; fi\n"
            "if [[ \"$*\" == *'/repos/owner/repo/pulls?state=open'* ]]; then\n"
            "  emit_page() { python3 - \"$1\" \"$2\" <<'PY'\n"
            "import json, sys\n"
            "start = int(sys.argv[1])\n"
            "count = int(sys.argv[2])\n"
            "print(json.dumps([{\"number\": start + index, \"title\": f\"pr {start + index}\"} for index in range(count)]))\n"
            "PY\n"
            "  }\n"
            "  case \"$*\" in\n"
            "    *'per_page=20&page=1'*) printf '[{\"number\":20,\"title\":\"small\"}]\\n' ;;\n"
            "    *'per_page=100&page=1'*) emit_page 1 100 ;;\n"
            "    *'per_page=100&page=2'*) emit_page 101 100 ;;\n"
            "    *'per_page=100&page=3'*) emit_page 201 100 ;;\n"
            "    *) exit 2 ;;\n"
            "  esac\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path), GH_PR_TEST_LOG=str(log_path))
        small = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "list", "--state", "open", "--limit", "20"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        large = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "list", "--state", "open", "--limit", "250"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        calls = log_path.read_text()
    assert [item["number"] for item in json.loads(small.stdout)["pullRequests"]] == [20]
    assert [item["number"] for item in json.loads(large.stdout)["pullRequests"]] == list(range(1, 251))
    list_calls = [line for line in calls.splitlines() if "/repos/owner/repo/pulls?state=open" in line]
    assert len(list_calls) == 4
    assert any(re.search(r"per_page=20&page=1", line) for line in list_calls)
    assert any(re.search(r"per_page=100&page=1", line) for line in list_calls)
    assert any(re.search(r"per_page=100&page=2", line) for line in list_calls)
    assert any(re.search(r"per_page=100&page=3", line) for line in list_calls)
    assert "--paginate" not in calls
    assert "graphql" not in calls.lower()


def test_pr_helper_delete_branch_uses_rest_ref_delete() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/repos/owner/repo/git/refs/heads/topic'* ]]; then\n"
            "  if [[ \"$*\" != *'--method DELETE'* ]]; then exit 2; fi\n"
            "  exit 0\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12/merge'* ]]; then\n"
            "  printf '{\"merged\":true,\"sha\":\"merge-sha\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* ]]; then\n"
            "  printf '{\"number\":12,\"title\":\"Demo\",\"state\":\"open\",\"draft\":false,\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://github.com/owner/repo/pull/12\",\"head\":{\"ref\":\"topic\",\"sha\":\"head-sha\",\"repo\":{\"full_name\":\"owner/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"owner/repo\"}}}\\n'\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path), GH_PR_TEST_LOG=str(log_path))
        merge = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "merge", "12", "--delete-branch"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        calls = log_path.read_text()

    payload = json.loads(merge.stdout)
    assert payload["merge"]["merged"] is True, payload
    assert payload["deletedBranch"]["deleted"] is True, payload
    assert "/repos/owner/repo/git/refs/heads/topic" in calls, calls


def test_pr_helper_merge_404_includes_recovery_context() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        gh_path = tmp_path / "gh"
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"$*\" == *'/repos/owner/repo/pulls/12/merge'* ]]; then\n"
            "  printf 'gh: Not Found (HTTP 404)\\n' >&2\n"
            "  exit 1\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* ]]; then\n"
            "  printf '{\"number\":12,\"title\":\"Demo\",\"state\":\"open\",\"draft\":false,\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://github.com/owner/repo/pull/12\",\"head\":{\"ref\":\"topic\",\"sha\":\"head-sha\",\"repo\":{\"full_name\":\"owner/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"owner/repo\"}}}\\n'\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path))
        result = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "merge", "12"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )

    assert result.returncode == 1, result
    payload = json.loads(result.stderr)
    assert payload["ok"] is False, payload
    assert payload["error"] == "PR merge failed", payload
    assert payload["detail"] == "gh: Not Found (HTTP 404)", payload
    assert payload["operation"] == "merge", payload
    assert payload["repo"] == "owner/repo", payload
    assert payload["pr"] == 12, payload
    assert payload["endpoint"] == "/repos/owner/repo/pulls/12/merge", payload
    assert payload["headSha"] == "head-sha", payload
    assert "token scope" in payload["hint"], payload


def test_pr_helper_supersede_comments_neutralizes_and_closes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        pr_json = (
            '{"number":12,"title":"Old","state":"open","draft":false,'
            '"body":"Closes #144\\nFixes owner/repo#145\\nResolves https://github.com/owner/repo/issues/146",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/12",'
            '"head":{"ref":"old-topic","sha":"old-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        winner_json = (
            '{"number":13,"title":"New","state":"open","draft":false,'
            '"body":"Closes #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/13",'
            '"head":{"ref":"new-topic","sha":"new-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  if [[ \"$*\" != *'superseded by #13'* ]]; then exit 2; fi\n"
            "  if [[ \"$*\" != *'Issue-closing references'* ]]; then exit 2; fi\n"
            "  printf '{\"html_url\":\"https://github.com/owner/repo/pull/12#issuecomment-1\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls?state=open'* ]]; then\n"
            "  printf '[{\"number\":12}]\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo?per_page='* || \"$*\" == *'/repos/owner/repo' ]]; then\n"
            "  printf '{\"default_branch\":\"main\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/git/refs/heads/old-topic'* ]]; then\n"
            "  if [[ \"$*\" != *'--method DELETE'* ]]; then exit 2; fi\n"
            "  exit 0\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* && \"$*\" == *'--method PATCH'* ]]; then\n"
            "  if [[ \"$*\" == *'state=closed'* ]]; then\n"
            "    printf '{\"number\":12,\"title\":\"Old\",\"state\":\"closed\",\"draft\":false,\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://github.com/owner/repo/pull/12\",\"head\":{\"ref\":\"old-topic\",\"sha\":\"old-sha\",\"repo\":{\"full_name\":\"owner/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"owner/repo\"}}}\\n'\n"
            "  else\n"
            "    if [[ \"$*\" != *'body=Refs #144'* || \"$*\" != *'Refs owner/repo#145'* ]]; then exit 2; fi\n"
            "    printf '%s\\n' \"$PR_JSON\"\n"
            "  fi\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* ]]; then\n"
            "  printf '%s\\n' \"$PR_JSON\"\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/13'* ]]; then\n"
            "  printf '%s\\n' \"$WINNER_JSON\"\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(
            os.environ,
            GH_PR_GH=str(gh_path),
            GH_PR_TEST_LOG=str(log_path),
            PR_JSON=pr_json,
            WINNER_JSON=winner_json,
        )
        result = REAL_SUBPROCESS_RUN(
            [
                sys.executable,
                str(PR_SCRIPT),
                "--repo",
                "owner/repo",
                "supersede",
                "12",
                "--by",
                "13",
                "--reason",
                "New PR has the agreed tests.",
                "--delete-branch",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        calls = log_path.read_text()

    payload = json.loads(result.stdout)
    assert payload["bodyUpdated"] is True, payload
    assert payload["closed"] is True, payload
    assert payload["deletedBranch"] == {"deleted": True, "ref": "heads/old-topic", "stderr": ""}, payload
    assert payload["commentUrl"].endswith("#issuecomment-1"), payload
    assert payload["neutralizedClosingReferences"] == [
        {"from": "Resolves https://github.com/owner/repo/issues/146", "to": "Refs owner/repo#146"},
        {"from": "Closes #144", "to": "Refs #144"},
        {"from": "Fixes owner/repo#145", "to": "Refs owner/repo#145"},
    ], payload
    assert "/repos/owner/repo/issues/12/comments" in calls, calls
    assert "/repos/owner/repo/git/refs/heads/old-topic" in calls, calls
    assert "state=closed" in calls, calls


def test_pr_helper_supersede_does_not_comment_when_close_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        pr_json = (
            '{"number":12,"title":"Old","state":"open","draft":false,'
            '"body":"Closes #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/12",'
            '"head":{"ref":"old-topic","sha":"old-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        winner_json = (
            '{"number":13,"title":"New","state":"open","draft":false,'
            '"body":"Closes #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/13",'
            '"head":{"ref":"new-topic","sha":"new-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  exit 3\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* && \"$*\" == *'--method PATCH'* ]]; then\n"
            "  if [[ \"$*\" == *'state=closed'* ]]; then\n"
            "    printf 'gh: Forbidden (HTTP 403)\\n' >&2\n"
            "    exit 1\n"
            "  fi\n"
            "  exit 4\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* ]]; then\n"
            "  printf '%s\\n' \"$PR_JSON\"\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/13'* ]]; then\n"
            "  printf '%s\\n' \"$WINNER_JSON\"\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path), GH_PR_TEST_LOG=str(log_path), PR_JSON=pr_json, WINNER_JSON=winner_json)
        result = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "supersede", "12", "--by", "13"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        calls = log_path.read_text()

    assert result.returncode == 1, result
    payload = json.loads(result.stderr)
    assert payload["ok"] is False, payload
    assert payload["error"] == "gh: Forbidden (HTTP 403)", payload
    assert not any("/repos/owner/repo/issues/12/comments" in line for line in calls.splitlines()), calls
    assert not any(
        "/repos/owner/repo/pulls/12" in line and "--method PATCH" in line and "body=" in line
        for line in calls.splitlines()
    ), calls


def test_pr_helper_supersede_warns_when_body_rewrite_fails_after_close() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        pr_json = (
            '{"number":12,"title":"Old","state":"open","draft":false,'
            '"body":"Closes #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/12",'
            '"head":{"ref":"old-topic","sha":"old-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        winner_json = (
            '{"number":13,"title":"New","state":"open","draft":false,'
            '"body":"Closes #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/13",'
            '"head":{"ref":"new-topic","sha":"new-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  printf '{\"html_url\":\"https://github.com/owner/repo/pull/12#issuecomment-1\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* && \"$*\" == *'--method PATCH'* ]]; then\n"
            "  if [[ \"$*\" == *'body=Refs #144'* ]]; then\n"
            "    printf 'gh: Forbidden (HTTP 403)\\n' >&2\n"
            "    exit 1\n"
            "  fi\n"
            "  printf '{\"number\":12,\"title\":\"Old\",\"state\":\"closed\",\"draft\":false,\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://github.com/owner/repo/pull/12\",\"head\":{\"ref\":\"old-topic\",\"sha\":\"old-sha\",\"repo\":{\"full_name\":\"owner/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"owner/repo\"}}}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* ]]; then\n"
            "  printf '%s\\n' \"$PR_JSON\"\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/13'* ]]; then\n"
            "  printf '%s\\n' \"$WINNER_JSON\"\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path), GH_PR_TEST_LOG=str(log_path), PR_JSON=pr_json, WINNER_JSON=winner_json)
        result = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "supersede", "12", "--by", "13"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )

    payload = json.loads(result.stdout)
    assert payload["ok"] is True, payload
    assert payload["closed"] is True, payload
    assert payload["bodyUpdated"] is False, payload
    assert payload["cleanupWarnings"] == [
        {
            "kind": "body_update_failed",
            "reason": "Supersede close/comment succeeded, but the PR body could not be rewritten to neutralize closing keywords.",
            "stderr": "gh: Forbidden (HTTP 403)",
        }
    ], payload


def test_pr_helper_supersede_warns_when_branch_delete_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        gh_path = tmp_path / "gh"
        pr_json = (
            '{"number":12,"title":"Old","state":"open","draft":false,'
            '"body":"Refs #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/12",'
            '"head":{"ref":"old-topic","sha":"old-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        winner_json = (
            '{"number":13,"title":"New","state":"open","draft":false,'
            '"body":"Closes #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/13",'
            '"head":{"ref":"new-topic","sha":"new-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"$*\" == *'/repos/owner/repo/git/refs/heads/old-topic'* ]]; then\n"
            "  printf 'remote ref could not be deleted\\n' >&2\n"
            "  exit 1\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls?state=open'* ]]; then\n"
            "  printf '[{\"number\":12}]\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo?per_page='* || \"$*\" == *'/repos/owner/repo' ]]; then\n"
            "  printf '{\"default_branch\":\"main\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  printf '{\"html_url\":\"https://github.com/owner/repo/pull/12#issuecomment-1\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* && \"$*\" == *'--method PATCH'* ]]; then\n"
            "  printf '{\"number\":12,\"title\":\"Old\",\"state\":\"closed\",\"draft\":false,\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://github.com/owner/repo/pull/12\",\"head\":{\"ref\":\"old-topic\",\"sha\":\"old-sha\",\"repo\":{\"full_name\":\"owner/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"owner/repo\"}}}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* ]]; then\n"
            "  printf '%s\\n' \"$PR_JSON\"\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/13'* ]]; then\n"
            "  printf '%s\\n' \"$WINNER_JSON\"\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path), PR_JSON=pr_json, WINNER_JSON=winner_json)
        result = REAL_SUBPROCESS_RUN(
            [
                sys.executable,
                str(PR_SCRIPT),
                "--repo",
                "owner/repo",
                "supersede",
                "12",
                "--by",
                "13",
                "--delete-branch",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )

    payload = json.loads(result.stdout)
    assert payload["closed"] is True, payload
    assert payload["deletedBranch"] == {
        "deleted": False,
        "ref": "heads/old-topic",
        "stderr": "remote ref could not be deleted",
    }, payload
    assert payload["cleanupWarnings"] == [
        {
            "kind": "remote_branch_delete_failed",
            "ref": "heads/old-topic",
            "stderr": "remote ref could not be deleted",
        }
    ], payload


def test_pr_helper_supersede_skips_branch_delete_when_winner_uses_same_branch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        pr_json = (
            '{"number":12,"title":"Old","state":"open","draft":false,'
            '"body":"Refs #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/12",'
            '"head":{"ref":"shared-topic","sha":"old-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        winner_json = (
            '{"number":13,"title":"New","state":"open","draft":false,'
            '"body":"Closes #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/13",'
            '"head":{"ref":"shared-topic","sha":"new-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/repos/owner/repo/git/refs/heads/shared-topic'* ]]; then\n"
            "  exit 2\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls?state=open'* ]]; then\n"
            "  printf '[{\"number\":12}]\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo?per_page='* || \"$*\" == *'/repos/owner/repo' ]]; then\n"
            "  printf '{\"default_branch\":\"main\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  printf '{\"html_url\":\"https://github.com/owner/repo/pull/12#issuecomment-1\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* && \"$*\" == *'--method PATCH'* ]]; then\n"
            "  printf '{\"number\":12,\"title\":\"Old\",\"state\":\"closed\",\"draft\":false,\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://github.com/owner/repo/pull/12\",\"head\":{\"ref\":\"shared-topic\",\"sha\":\"old-sha\",\"repo\":{\"full_name\":\"owner/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"owner/repo\"}}}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* ]]; then\n"
            "  printf '%s\\n' \"$PR_JSON\"\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/13'* ]]; then\n"
            "  printf '%s\\n' \"$WINNER_JSON\"\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path), GH_PR_TEST_LOG=str(log_path), PR_JSON=pr_json, WINNER_JSON=winner_json)
        result = REAL_SUBPROCESS_RUN(
            [
                sys.executable,
                str(PR_SCRIPT),
                "--repo",
                "owner/repo",
                "supersede",
                "12",
                "--by",
                "13",
                "--delete-branch",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        calls = log_path.read_text()

    payload = json.loads(result.stdout)
    assert payload["deletedBranch"] is None, payload
    assert payload["cleanupWarnings"] == [
        {
            "kind": "remote_branch_delete_skipped_active_pr",
            "ref": "heads/shared-topic",
            "reason": "Superseded and canonical PRs share the same head branch.",
        }
    ], payload
    assert "/repos/owner/repo/git/refs/heads/shared-topic" not in calls, calls


def test_pr_helper_supersede_skips_branch_delete_when_third_pr_uses_branch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        pr_json = (
            '{"number":12,"title":"Old","state":"open","draft":false,'
            '"body":"Refs #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/12",'
            '"head":{"ref":"old-topic","sha":"old-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        winner_json = (
            '{"number":13,"title":"New","state":"open","draft":false,'
            '"body":"Closes #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/13",'
            '"head":{"ref":"new-topic","sha":"new-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/repos/owner/repo/git/refs/heads/old-topic'* ]]; then\n"
            "  exit 2\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls?state=open'* ]]; then\n"
            "  printf '[{\"number\":12},{\"number\":14}]\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo?per_page='* || \"$*\" == *'/repos/owner/repo' ]]; then\n"
            "  printf '{\"default_branch\":\"main\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  printf '{\"html_url\":\"https://github.com/owner/repo/pull/12#issuecomment-1\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* && \"$*\" == *'--method PATCH'* ]]; then\n"
            "  printf '{\"number\":12,\"title\":\"Old\",\"state\":\"closed\",\"draft\":false,\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://github.com/owner/repo/pull/12\",\"head\":{\"ref\":\"old-topic\",\"sha\":\"old-sha\",\"repo\":{\"full_name\":\"owner/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"owner/repo\"}}}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* ]]; then\n"
            "  printf '%s\\n' \"$PR_JSON\"\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/13'* ]]; then\n"
            "  printf '%s\\n' \"$WINNER_JSON\"\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path), GH_PR_TEST_LOG=str(log_path), PR_JSON=pr_json, WINNER_JSON=winner_json)
        result = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "supersede", "12", "--by", "13", "--delete-branch"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        calls = log_path.read_text()

    payload = json.loads(result.stdout)
    assert payload["deletedBranch"] is None, payload
    assert payload["cleanupWarnings"] == [
        {
            "kind": "remote_branch_delete_skipped_active_pr",
            "ref": "heads/old-topic",
            "reason": "Another open PR uses the superseded PR head branch.",
            "pullRequests": "14",
        }
    ], payload
    assert "/repos/owner/repo/git/refs/heads/old-topic" not in calls, calls


def test_pr_helper_supersede_skips_branch_delete_for_default_branch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        pr_json = (
            '{"number":12,"title":"Old","state":"open","draft":false,'
            '"body":"Refs #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/12",'
            '"head":{"ref":"release","sha":"old-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        winner_json = (
            '{"number":13,"title":"New","state":"open","draft":false,'
            '"body":"Closes #144",'
            '"mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/13",'
            '"head":{"ref":"new-topic","sha":"new-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/repos/owner/repo/git/refs/heads/release'* ]]; then\n"
            "  exit 2\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls?state=open'* ]]; then\n"
            "  printf '[{\"number\":12}]\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo?per_page='* || \"$*\" == *'/repos/owner/repo' ]]; then\n"
            "  printf '{\"default_branch\":\"release\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  printf '{\"html_url\":\"https://github.com/owner/repo/pull/12#issuecomment-1\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* && \"$*\" == *'--method PATCH'* ]]; then\n"
            "  printf '{\"number\":12,\"title\":\"Old\",\"state\":\"closed\",\"draft\":false,\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://github.com/owner/repo/pull/12\",\"head\":{\"ref\":\"release\",\"sha\":\"old-sha\",\"repo\":{\"full_name\":\"owner/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"owner/repo\"}}}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* ]]; then\n"
            "  printf '%s\\n' \"$PR_JSON\"\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/13'* ]]; then\n"
            "  printf '%s\\n' \"$WINNER_JSON\"\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path), GH_PR_TEST_LOG=str(log_path), PR_JSON=pr_json, WINNER_JSON=winner_json)
        result = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "supersede", "12", "--by", "13", "--delete-branch"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        calls = log_path.read_text()

    payload = json.loads(result.stdout)
    assert payload["deletedBranch"] is None, payload
    assert payload["cleanupWarnings"] == [
        {
            "kind": "remote_branch_delete_skipped_shared_ref",
            "ref": "heads/release",
            "reason": "Superseded PR head branch is the repository default branch.",
        }
    ], payload
    assert "/repos/owner/repo/git/refs/heads/release" not in calls, calls


def test_pr_helper_preserves_url_repo_and_paginates_checks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/repos/other/repo/pulls/44'* ]]; then\n"
            "  printf '{\"number\":44,\"title\":\"Cross repo\",\"state\":\"open\",\"draft\":false,\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://github.com/other/repo/pull/44\",\"head\":{\"ref\":\"topic\",\"sha\":\"head-sha\",\"repo\":{\"full_name\":\"other/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"other/repo\"}}}\\n'\n"
            "elif [[ \"$*\" == *'/repos/other/repo/commits/head-sha/check-runs'* ]]; then\n"
            "  if [[ \"$*\" != *'--paginate'* || \"$*\" != *'--slurp'* ]]; then exit 2; fi\n"
            "  printf '[{\"check_runs\":[{\"name\":\"ci-1\",\"status\":\"completed\",\"conclusion\":\"success\"}]},{\"check_runs\":[{\"name\":\"ci-2\",\"status\":\"completed\",\"conclusion\":\"failure\"}]}]\\n'\n"
            "elif [[ \"$*\" == *'/repos/other/repo/commits/head-sha/statuses'* ]]; then\n"
            "  if [[ \"$*\" != *'--paginate'* || \"$*\" != *'--slurp'* ]]; then exit 2; fi\n"
            "  printf '[[{\"context\":\"legacy-1\",\"state\":\"success\"}],[{\"context\":\"legacy-2\",\"state\":\"error\"}]]\\n'\n"
            "elif [[ \"$*\" == *'/repos/other/repo/commits/head-sha/status'* ]]; then\n"
            "  printf '{\"state\":\"failure\"}\\n'\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path), GH_PR_TEST_LOG=str(log_path))
        checks = REAL_SUBPROCESS_RUN(
            [
                sys.executable,
                str(PR_SCRIPT),
                "--repo",
                "owner/repo",
                "checks",
                "https://github.com/other/repo/pull/44",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        calls = log_path.read_text()

    payload = json.loads(checks.stdout)
    assert checks.returncode == 0, checks.stderr
    assert payload["repo"] == "other/repo", payload
    assert payload["summary"]["checkRunCount"] == 2, payload
    assert payload["summary"]["statusCount"] == 2, payload
    assert payload["summary"]["failingCount"] == 2, payload
    assert payload["summary"]["combinedState"] == "failure", payload
    assert payload["summary"]["combinedStateRaw"] == "failure", payload
    assert payload["summary"]["legacyStatusesPresent"] is True, payload
    assert "/repos/other/repo/pulls/44" in calls
    assert "/repos/owner/repo/pulls/44" not in calls


def test_pr_helper_accepts_enterprise_pr_urls() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/repos/enterprise/repo/pulls/5'* ]]; then\n"
            "  printf '{\"number\":5,\"title\":\"Enterprise\",\"state\":\"open\",\"draft\":false,\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://ghe.example.com/enterprise/repo/pull/5\",\"head\":{\"ref\":\"topic\",\"sha\":\"head-sha\",\"repo\":{\"full_name\":\"enterprise/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"enterprise/repo\"}}}\\n'\n"
            "else\n"
            "  printf '{}\\n'\n"
            "fi\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path), GH_PR_TEST_LOG=str(log_path))
        view = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "view", "https://ghe.example.com/enterprise/repo/pull/5"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        calls = log_path.read_text()
    payload = json.loads(view.stdout)
    assert payload["repo"] == "enterprise/repo", payload
    assert payload["pr"]["number"] == 5, payload
    assert "/repos/enterprise/repo/pulls/5" in calls, calls


def test_project_set_accepts_item_id_and_classifies_low_graphql() -> None:
    plan = load_plan_module()
    calls: list[dict[str, Any]] = []

    def fake_gh_json(args: list[str], **_kwargs: Any) -> tuple[str, Any]:
        calls.append({"kind": "gh_json", "args": args})
        if args[:2] == ["api", "-H"] and args[-1] == "rate_limit":
            return "automation-gh", {"resources": {"graphql": {"remaining": 200, "reset": 999}}}
        raise AssertionError(f"unexpected gh_json args: {args}")

    def fake_run_raw(args: list[str], **kwargs: Any) -> tuple[str, str, str]:
        calls.append({"kind": "run_raw", "args": args, "kwargs": kwargs})
        return "automation-gh", "{}", ""

    plan.gh_json = fake_gh_json
    plan.run_raw = fake_run_raw
    plan.project_meta = lambda owner, project_ref, recoverable=False: (
        "automation-gh",
        7,
        {"id": "project-id", "title": "Roadmap"},
    )
    plan.project_fields = lambda owner, project_number, recoverable=False: {
        "Focus": {
            "id": "field-focus",
            "name": "Focus",
            "type": "ProjectV2SingleSelectField",
            "options": [{"name": "Now", "id": "option-now"}],
        }
    }
    result = plan.set_project_fields(
        owner="owner",
        project_ref="Roadmap",
        issue_url="https://github.com/owner/repo/issues/90",
        config={"project_fields": {"focus": "Focus"}},
        focus="Now",
        item_id="PVTI_item",
        recoverable=True,
    )
    assert result["updated"] == {"Focus": "Now"}, result
    assert not any(call["args"][:2] == ["project", "item-list"] for call in calls), calls
    assert any("PVTI_item" in call["args"] for call in calls if call["kind"] == "run_raw"), calls

    plan.gh_json = lambda args, **kwargs: (
        "automation-gh",
        {"resources": {"graphql": {"remaining": 0, "reset": 12345}}},
    )
    try:
        plan.ensure_graphql_budget(recoverable=True)
    except plan.ClassifiedPlanError as exc:
        assert exc.code == "rate_limited", exc.code
        assert exc.retry_at == 12345, exc.retry_at
    else:
        raise AssertionError("low GraphQL quota should be classified as rate_limited")


def main() -> None:
    tests = [
        test_issue_body_updates_use_rest_patch,
        test_project_commands_are_recoverable,
        test_create_reports_issue_when_project_sync_fails,
        test_close_reports_stale_project_as_non_blocking_warning,
        test_create_supports_waiting_plan_status,
        test_run_raw_falls_back_only_for_graphql_rate_limit,
        test_run_raw_is_bot_first_even_when_prefer_active_is_requested,
        test_pr_helper_uses_rest_endpoints_for_common_pr_work,
        test_pr_helper_list_paginates_only_when_limit_exceeds_one_page,
        test_pr_helper_delete_branch_uses_rest_ref_delete,
        test_pr_helper_merge_404_includes_recovery_context,
        test_pr_helper_supersede_comments_neutralizes_and_closes,
        test_pr_helper_supersede_does_not_comment_when_close_fails,
        test_pr_helper_supersede_warns_when_body_rewrite_fails_after_close,
        test_pr_helper_supersede_warns_when_branch_delete_fails,
        test_pr_helper_supersede_skips_branch_delete_when_winner_uses_same_branch,
        test_pr_helper_supersede_skips_branch_delete_when_third_pr_uses_branch,
        test_pr_helper_supersede_skips_branch_delete_for_default_branch,
        test_pr_helper_preserves_url_repo_and_paginates_checks,
        test_pr_helper_accepts_enterprise_pr_urls,
        test_project_set_accepts_item_id_and_classifies_low_graphql,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")


if __name__ == "__main__":
    main()
