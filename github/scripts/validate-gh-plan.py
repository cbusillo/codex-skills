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
PR_REST_SCRIPT = Path(__file__).with_name("gh-pr-rest.py")
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
    assert payload["project_fields"] == {"error": "project sync throttled"}, payload


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


def test_pr_rest_helper_uses_rest_endpoints() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >>\"$GH_PR_REST_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/repos/owner/repo/pulls/12/merge'* ]]; then\n"
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
        env = dict(os.environ, GH_PR_REST_GH=str(gh_path), GH_PR_REST_TEST_LOG=str(log_path))
        view = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_REST_SCRIPT), "--repo", "owner/repo", "view", "12"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        checks = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_REST_SCRIPT), "--repo", "owner/repo", "checks", "12"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        merge = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_REST_SCRIPT), "--repo", "owner/repo", "merge", "12"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        rate = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_REST_SCRIPT), "--repo", "owner/repo", "rate-limit"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        calls = log_path.read_text()
    assert json.loads(view.stdout)["pr"]["number"] == 12
    assert json.loads(checks.stdout)["summary"]["combinedState"] == "success"
    assert json.loads(merge.stdout)["merge"]["merged"] is True
    assert json.loads(rate.stdout)["graphql"]["remaining"] == 0
    assert "/repos/owner/repo/pulls/12" in calls
    assert "/repos/owner/repo/pulls/12/merge" in calls
    assert "graphql" not in calls.lower()


def main() -> None:
    tests = [
        test_issue_body_updates_use_rest_patch,
        test_project_commands_are_recoverable,
        test_create_reports_issue_when_project_sync_fails,
        test_create_supports_waiting_plan_status,
        test_run_raw_falls_back_only_for_graphql_rate_limit,
        test_run_raw_is_bot_first_even_when_prefer_active_is_requested,
        test_pr_rest_helper_uses_rest_endpoints,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")


if __name__ == "__main__":
    main()
