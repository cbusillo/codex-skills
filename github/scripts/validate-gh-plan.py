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
import urllib.parse
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


def test_plan_index_paginates_filters_prs_and_honors_limit() -> None:
    plan = load_plan_module()
    calls: list[dict[str, Any]] = []

    def issue(number: int, *, pull_request: bool = False) -> dict[str, Any]:
        item: dict[str, Any] = {
            "number": number,
            "title": f"Issue {number}",
            "state": "open",
            "updated_at": f"2026-07-17T00:{number:02d}:00Z",
            "html_url": f"https://github.com/owner/repo/issues/{number}",
            "labels": [{"name": "zeta"}, {"name": "alpha"}],
            "milestone": {"title": "Wave 1"},
        }
        if pull_request:
            item["pull_request"] = {"url": f"https://api.github.com/repos/owner/repo/pulls/{number}"}
        return item

    page_one = [issue(1), issue(2)] + [issue(number, pull_request=True) for number in range(100, 198)]
    assert len(page_one) == 100

    def fake_api_json(method: str, path: str, _payload: Any = None, **kwargs: Any) -> tuple[str, Any]:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        calls.append({"method": method, "path": path, "query": query, "kwargs": kwargs})
        assert method == "GET"
        assert kwargs["bucket"] == "rest_core"
        page = int(query["page"][0])
        if page == 1:
            return "automation-gh", page_one
        if page == 2:
            return "automation-gh", [issue(3)]
        raise AssertionError(f"unexpected page: {page}")

    plan.api_json = fake_api_json
    plan.gh_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("index must not use gh issue list"))
    plan.load_config = lambda _repo: {"labels": {"plan": "plan"}}
    output = StringIO()
    with redirect_stdout(output):
        plan.cmd_index(types.SimpleNamespace(repo="owner/repo", state="open", limit=3, label=None))

    payload = json.loads(output.getvalue())
    assert payload["count"] == 3, payload
    assert [item["number"] for item in payload["plans"]] == [1, 2, 3], payload
    assert payload["plans"][0]["state"] == "OPEN", payload
    assert payload["plans"][0]["labels"] == ["zeta", "alpha"], payload
    assert payload["plans"][0]["milestone"] == "Wave 1", payload
    assert len(calls) == 2, calls
    assert calls[0]["query"] == {
        "labels": ["plan"],
        "state": ["open"],
        "sort": ["updated"],
        "direction": ["desc"],
        "per_page": ["100"],
        "page": ["1"],
    }, calls[0]

    calls.clear()
    with redirect_stdout(StringIO()):
        plan.cmd_index(types.SimpleNamespace(repo="owner/repo", state="all", limit=1, label="priority/high"))
    assert calls[0]["query"]["labels"] == ["priority/high"], calls[0]
    assert calls[0]["query"]["state"] == ["all"], calls[0]


def test_plan_search_uses_search_bucket_and_conditional_state() -> None:
    plan = load_plan_module()
    calls: list[dict[str, Any]] = []

    def issue(number: int, *, state: str = "open") -> dict[str, Any]:
        return {
            "number": number,
            "title": f"Roadmap result {number}",
            "state": state,
            "updated_at": f"2026-07-17T00:{number % 60:02d}:00Z",
            "html_url": f"https://github.com/owner/repo/issues/{number}",
            "labels": [{"name": "plan"}],
            "milestone": None,
        }

    def fake_api_json(method: str, path: str, _payload: Any = None, **kwargs: Any) -> tuple[str, Any]:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        calls.append({"method": method, "path": path, "query": query, "kwargs": kwargs})
        assert method == "GET"
        assert urllib.parse.urlparse(path).path == "/search/issues"
        assert kwargs["bucket"] == "search"
        page = int(query["page"][0])
        query_text = query["q"][0]
        if "is:open" in query_text:
            if page == 1:
                items = [issue(number) for number in range(1, 101)]
            elif page == 2:
                assert kwargs["completed_steps"] == ["search_plan_issues_page_1"], kwargs
                items = [issue(101)]
            else:
                raise AssertionError(f"unexpected page: {page}")
            total_count = 101
        else:
            assert page == 1, page
            items = [issue(42, state="closed")]
            total_count = 1
        return "automation-gh", {
            "total_count": total_count,
            "incomplete_results": False,
            "items": items,
        }

    plan.api_json = fake_api_json
    plan.gh_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("search must not use gh issue list"))

    open_output = StringIO()
    with redirect_stdout(open_output):
        plan.cmd_search(types.SimpleNamespace(repo="owner/repo", query="roadmap", state="open", limit=101))
    all_output = StringIO()
    with redirect_stdout(all_output):
        plan.cmd_search(types.SimpleNamespace(repo="owner/repo", query="roadmap", state="all", limit=5))

    open_query = calls[0]["query"]["q"][0]
    assert open_query == "roadmap repo:owner/repo is:issue is:open", open_query
    all_query = calls[2]["query"]["q"][0]
    assert all_query == "roadmap repo:owner/repo is:issue", all_query
    assert "is:open" not in all_query and "is:closed" not in all_query
    open_payload = json.loads(open_output.getvalue())
    assert open_payload["count"] == 101, open_payload
    assert open_payload["issues"][0]["state"] == "OPEN", open_payload
    assert open_payload["issues"][-1]["number"] == 101, open_payload
    all_payload = json.loads(all_output.getvalue())
    assert all_payload["issues"][0]["state"] == "CLOSED", all_payload
    assert all_payload["issues"][0]["labels"] == ["plan"], all_payload


def test_plan_ensure_labels_uses_paged_rest_and_reconciles_conflict() -> None:
    plan = load_plan_module()
    calls: list[dict[str, Any]] = []
    first_page = [{"name": f"other-{number}"} for number in range(100)]

    def fake_api_json(method: str, path: str, payload: Any = None, **kwargs: Any) -> tuple[str, Any]:
        parsed_path = urllib.parse.urlparse(path)
        query = urllib.parse.parse_qs(parsed_path.query)
        calls.append({"method": method, "path": path, "payload": payload, "kwargs": kwargs})
        if method == "GET" and parsed_path.path == "/repos/owner/repo/labels":
            page = int(query["page"][0])
            return "automation-gh", first_page if page == 1 else [{"name": "Plan"}]
        if method == "POST" and parsed_path.path == "/repos/owner/repo/labels":
            assert payload["color"] and payload["description"]
            if payload["name"] in {"plan:waiting", "team/platform"}:
                raise plan.PlanError("Validation Failed", api_result={"status": 422})
            return "automation-gh", {"name": payload["name"]}
        if method == "GET" and parsed_path.path == "/repos/owner/repo/labels/plan%3Awaiting":
            return "automation-gh", {"name": "plan:waiting"}
        if method == "GET" and parsed_path.path == "/repos/owner/repo/labels/team%2Fplatform":
            return "automation-gh", {"name": "team/platform"}
        raise AssertionError({"method": method, "path": path, "payload": payload})

    plan.api_json = fake_api_json
    plan.gh_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("labels must not use gh label list"))
    plan.run_raw = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("labels must not use gh label create"))
    config = {
        "label_defs": {
            "plan": {"color": "5319e7", "description": "Durable planning issue"},
            "plan:active": {"color": "0e8a16", "description": "Plan is actionable now"},
            "plan:waiting": {"color": "fbca04", "description": "Plan is waiting"},
            "team/platform": {"color": "1d76db", "description": "Platform team"},
        }
    }
    actor, created = plan.ensure_labels(
        "owner/repo",
        ["plan", "plan:active", "plan:waiting", "team/platform"],
        config,
    )

    assert actor == "automation-gh", actor
    assert created == ["plan:active"], created
    label_list_calls = [call for call in calls if call["method"] == "GET" and "/labels?" in call["path"]]
    assert len(label_list_calls) == 2, calls
    created_names = [call["payload"]["name"] for call in calls if call["method"] == "POST"]
    assert created_names == ["plan:active", "plan:waiting", "team/platform"], calls
    assert any("plan%3Awaiting" in call["path"] for call in calls), calls
    assert any("team%2Fplatform" in call["path"] for call in calls), calls


def test_plan_ensure_labels_skips_existing_case_insensitively() -> None:
    plan = load_plan_module()
    calls: list[dict[str, Any]] = []

    def fake_api_json(method: str, path: str, payload: Any = None, **kwargs: Any) -> tuple[str, Any]:
        calls.append({"method": method, "path": path, "payload": payload, "kwargs": kwargs})
        assert method == "GET", calls
        assert urllib.parse.urlparse(path).path == "/repos/owner/repo/labels", path
        return "automation-gh", [
            {"name": "Plan"},
            {"name": "PLAN:ACTIVE"},
            {"name": "plan:waiting"},
        ]

    plan.api_json = fake_api_json
    plan.gh_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("labels must not use gh label list"))
    plan.run_raw = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("existing labels must not be created"))
    actor, created = plan.ensure_labels(
        "owner/repo",
        ["plan", "plan:active", "plan:waiting"],
        {"label_defs": {}},
    )

    assert actor == "automation-gh", actor
    assert created == [], created
    assert len(calls) == 1, calls


def test_plan_paged_rest_failure_receives_completed_page_evidence() -> None:
    plan = load_plan_module()
    calls: list[dict[str, Any]] = []

    def fake_api_json(method: str, path: str, _payload: Any = None, **kwargs: Any) -> tuple[str, Any]:
        calls.append({"method": method, "path": path, "kwargs": kwargs})
        page = int(urllib.parse.parse_qs(urllib.parse.urlparse(path).query)["page"][0])
        if page == 1:
            return "automation-gh", [{"number": number} for number in range(100)]
        assert kwargs["completed_steps"] == ["search_plan_issues_page_1"], kwargs
        assert kwargs["failed_step"] == "search_plan_issues_page_2", kwargs
        assert kwargs["bucket"] == "search", kwargs
        raise plan.PlanError("provider failure", api_result={"status": 503, "bucket": "search"})

    plan.api_json = fake_api_json
    try:
        plan.collect_paged_rest_items(
            "/search/issues",
            query={"q": "repo:owner/repo is:issue"},
            bucket="search",
            step_prefix="search_plan_issues",
            limit=101,
            collection_key=None,
        )
    except plan.PlanError as exc:
        assert plan.plan_error_status(exc) == 503, exc.api_result
    else:
        raise AssertionError("second page failure should propagate")
    assert len(calls) == 2, calls


def test_plan_rest_command_context_and_limit_validation() -> None:
    plan = load_plan_module()
    assert plan.PLAN_COMMAND_CONTEXT["index"] == ("rest_api", "rest_core", False)
    assert plan.PLAN_COMMAND_CONTEXT["search"] == ("rest_api", "search", False)
    assert plan.PLAN_COMMAND_CONTEXT["ensure-labels"] == ("rest_api", "rest_core", True)

    parser = plan.build_parser()
    for command in (["index", "--limit", "0"], ["search", "query", "--limit", "-1"]):
        try:
            parser.parse_args(command)
        except plan.github_api_core.ArgumentParsingError as exc:
            assert "invalid limit" in str(exc), exc
        else:
            raise AssertionError(f"expected invalid limit failure: {command}")


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

    project_calls = [call for call in calls if call["args"] and call["args"][0] == "project"]
    assert project_calls, calls
    assert all(call["recoverable"] for call in project_calls), project_calls
    assert all(call["prefer_active"] for call in calls), calls

    calls.clear()
    with redirect_stdout(StringIO()):
        plan.cmd_project_list(types.SimpleNamespace(owner="owner", limit=30, closed=False))

    assert len(calls) == 2, calls
    assert calls[0]["args"][-1] == "rate_limit", calls
    assert calls[1]["args"][:2] == ["project", "list"], calls
    assert all(call["prefer_active"] for call in calls), calls
    assert all(call["recoverable"] for call in calls), calls


def test_repo_config_path_skips_missing_home_candidate() -> None:
    plan = load_plan_module()
    original_git_root = plan.git_root
    original_repo_from_git = plan.repo_from_git
    original_home = plan.pathlib.Path.home
    checked: list[Path] = []

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        plan.git_root = lambda: None
        plan.pathlib.Path.home = lambda: home

        def fake_repo_from_git(candidate: Path | None = None) -> str | None:
            checked.append(candidate or Path.cwd())
            return "owner/repo"

        plan.repo_from_git = fake_repo_from_git
        try:
            assert plan.repo_config_path("owner/repo") is None
        finally:
            plan.git_root = original_git_root
            plan.repo_from_git = original_repo_from_git
            plan.pathlib.Path.home = original_home

    assert checked == [], checked


def test_manager_for_repo_passes_raw_values_without_people_resolver() -> None:
    plan = load_plan_module()
    original_resolver = plan.PEOPLE_RESOLVER
    plan.PEOPLE_RESOLVER = Path("/tmp/not-present-people-resolver.py")
    try:
        manager = plan.manager_for_repo(
            {
                "workflow": {
                    "default_manager": "@default",
                    "repo_managers": {"owner/repo": "@repo-manager"},
                }
            },
            "owner/repo",
        )
    finally:
        plan.PEOPLE_RESOLVER = original_resolver

    assert manager == "@repo-manager"


def test_manager_for_repo_skips_unresolved_person_ref() -> None:
    plan = load_plan_module()
    original_resolver = plan.PEOPLE_RESOLVER
    plan.PEOPLE_RESOLVER = Path("/tmp/not-present-people-resolver.py")
    try:
        manager = plan.manager_for_repo(
            {"workflow": {"default_manager": "person:example-manager"}},
            "owner/repo",
        )
    finally:
        plan.PEOPLE_RESOLVER = original_resolver

    assert manager is None


def test_explicit_unresolved_person_manager_is_skipped() -> None:
    plan = load_plan_module()
    original_resolver = plan.PEOPLE_RESOLVER
    plan.PEOPLE_RESOLVER = Path("/tmp/not-present-people-resolver.py")
    try:
        assert plan.resolve_required_manager_value("person:example-manager") is None
        assert plan.selected_manager_value(
            "person:example-manager",
            {"workflow": {"default_manager": "Code", "repo_managers": {}}},
            "owner/repo",
        ) is None
    finally:
        plan.PEOPLE_RESOLVER = original_resolver


def test_person_resolution_requires_uv() -> None:
    plan = load_plan_module()
    original_resolver = plan.PEOPLE_RESOLVER
    original_run = plan.subprocess.run
    original_which = plan.shutil.which
    plan.PEOPLE_RESOLVER = SCRIPT
    plan.shutil.which = lambda name: None

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"person resolver should not run without uv: {command}")

    plan.subprocess.run = fake_run
    try:
        assert plan.resolve_person_for_project("person:example-manager") is None
    finally:
        plan.PEOPLE_RESOLVER = original_resolver
        plan.subprocess.run = original_run
        plan.shutil.which = original_which


def test_raw_manager_values_do_not_resolve_through_people() -> None:
    plan = load_plan_module()
    original_resolver = plan.PEOPLE_RESOLVER
    original_run = plan.subprocess.run
    plan.PEOPLE_RESOLVER = SCRIPT

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"raw manager value should not invoke people resolver: {command}")

    plan.subprocess.run = fake_run
    try:
        assert plan.resolve_manager_value("Example") == "Example"
        assert plan.resolve_manager_value("@example-manager") == "@example-manager"
    finally:
        plan.PEOPLE_RESOLVER = original_resolver
        plan.subprocess.run = original_run


def test_manager_for_repo_resolves_person_ref_to_project_label_when_available() -> None:
    plan = load_plan_module()
    original_resolver = plan.PEOPLE_RESOLVER
    original_run = plan.subprocess.run
    original_which = plan.shutil.which
    plan.PEOPLE_RESOLVER = SCRIPT
    plan.shutil.which = lambda name: "/usr/bin/uv" if name == "uv" else None

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command == [
            "/usr/bin/uv",
            "run",
            str(SCRIPT),
            "person:example-manager",
            "--strict",
        ], command
        return completed(json.dumps({
            "status": "matched",
            "match": {
                "id": "example-manager",
                "display_name": "Example Manager",
                "preferred_reference": "Example",
                "github": "example-manager",
                "mention_style": "@example-manager",
            },
        }))

    plan.subprocess.run = fake_run
    try:
        manager = plan.manager_for_repo(
            {
                "workflow": {
                    "default_manager": "person:example-manager",
                    "repo_managers": {},
                }
            },
            "owner/repo",
        )
    finally:
        plan.PEOPLE_RESOLVER = original_resolver
        plan.subprocess.run = original_run
        plan.shutil.which = original_which

    assert manager == "Example"


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
    assert payload["project_fields"]["warning"] is True, payload
    assert payload["project_fields"]["blocking"] is False, payload
    assert payload["project_fields"]["operation"] == "create_project_sync", payload
    assert payload["project_fields"]["target"] == {"owner": "owner", "project": "Roadmap"}, payload


def test_create_reports_stale_project_as_non_blocking_warning() -> None:
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
    plan.gh_json = lambda args, **kwargs: ("automation-gh", []) if args[:2] == ["issue", "list"] else (_ for _ in ()).throw(AssertionError(args))
    plan.ensure_labels = lambda repo, wanted, config: ("automation-gh", [])
    plan.rest_create_issue = lambda repo, title, body, labels, milestone: ("automation-gh", issue)
    plan.resolve_project = lambda owner, project, recoverable=False: (_ for _ in ()).throw(
        plan.project_error(f"Project not found for {owner}: {project}")
    )

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
    assert payload["project_fields"]["warning"] is True, payload
    assert payload["project_fields"]["blocking"] is False, payload
    assert payload["project_fields"]["operation"] == "create_project_sync", payload
    assert payload["project_fields"]["error_code"] == "lookup_stale", payload
    assert payload["project_fields"]["target"] == {"owner": "owner", "project": "Roadmap"}, payload
    assert "planning.projects" in payload["project_fields"]["recommended_action"], payload
    assert "Verify the acting GitHub identity can see the Project." in payload["project_fields"]["recommended_actions"], payload


def test_create_reports_project_auth_denied_as_non_blocking_warning() -> None:
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
    calls: list[dict[str, Any]] = []

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
    plan.resolve_project = lambda owner, project, recoverable=False: ("automation-gh", 7, {"title": project, "number": 7})

    def fake_run_raw(
        args: list[str],
        *,
        prefer_active: bool = False,
        recoverable: bool = False,
        **_kwargs: Any,
    ) -> tuple[str, str, str]:
        calls.append({"args": args, "prefer_active": prefer_active, "recoverable": recoverable})
        if args[:2] == ["project", "item-add"] and recoverable:
            raise plan.project_error("HTTP 403: resource not accessible by integration")
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
    assert payload["project_fields"]["warning"] is True, payload
    assert payload["project_fields"]["blocking"] is False, payload
    assert payload["project_fields"]["operation"] == "create_project_sync", payload
    assert payload["project_fields"]["error_code"] == "project_auth_denied", payload
    assert payload["project_fields"]["target"] == {"owner": "owner", "project": "Roadmap"}, payload
    assert "Grant the automation identity access to the Project." in payload["project_fields"]["recommended_actions"], payload
    assert all(call["args"][:2] != ["gh", "project"] for call in calls), calls


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


def test_close_delegates_comment_to_shared_rest_helper() -> None:
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
    comment_call: dict[str, Any] = {}

    plan.load_config = lambda repo: {
        "labels": {"active": "plan:active", "done": "plan:done"},
        "projects": {},
    }
    plan.get_issue = lambda ref, repo: ("automation-gh", issue)
    plan.comment_route = lambda: ("automation-gh", "fake-gh", "shiny-code-bot")

    def fake_run_raw(args: list[str], **_kwargs: Any) -> tuple[str, str, str]:
        calls.append(args)
        if args[:2] in (["issue", "edit"], ["issue", "close"]):
            return "automation-gh", "", ""
        raise AssertionError(f"unexpected run_raw args: {args}")

    def fake_comment(kind: str, number: int, body: str, **kwargs: Any) -> dict[str, Any]:
        comment_call.update({"kind": kind, "number": number, "body": body, **kwargs})
        return {
            "kind": kind,
            "repo": kwargs["repo"],
            "number": number,
            "actor": "shiny-code-bot",
            "expected_actor": "shiny-code-bot",
            "comment_action": "created",
            "url": "https://github.com/owner/repo/issues/10#issuecomment-1",
            "comment": {"id": 1, "url": "https://github.com/owner/repo/issues/10#issuecomment-1"},
            "completed_steps": ["update_labels", "resolve_actor", "post_close_comment"],
        }

    plan.run_raw = fake_run_raw
    original_comment = plan.github_comment_core.comment
    plan.github_comment_core.comment = fake_comment
    try:
        output = StringIO()
        with redirect_stdout(output):
            plan.cmd_close(types.SimpleNamespace(
                repo="owner/repo",
                issue="10",
                reason="completed",
                body="Completed with evidence.\n",
                body_file=None,
                owner=None,
                project=None,
            ))
    finally:
        plan.github_comment_core.comment = original_comment

    payload = json.loads(output.getvalue())
    assert comment_call["kind"] == "issue", comment_call
    assert comment_call["number"] == 10, comment_call
    assert comment_call["body"] == "Completed with evidence.\n", comment_call
    assert comment_call["gh_cmd"] == "fake-gh", comment_call
    assert comment_call["completed_steps"] == ["update_labels"], comment_call
    assert payload["comment"]["comment_action"] == "created", payload
    assert payload["comment"]["url"].endswith("#issuecomment-1"), payload
    assert payload["completed_steps"] == [
        "update_labels",
        "resolve_actor",
        "post_close_comment",
        "close_issue",
    ], payload
    assert any(call[:2] == ["issue", "close"] for call in calls), calls


def test_comment_route_matches_explicit_auth_policy() -> None:
    plan = load_plan_module()
    previous = os.environ.pop("GH_PLAN_SKIP_BOT", None)
    try:
        actor, gh_cmd, expected_actor = plan.comment_route()
        assert actor == "automation-gh", actor
        assert Path(gh_cmd) == plan.BOT_GH, gh_cmd
        assert expected_actor == plan.EXPECTED_ACTOR, expected_actor

        os.environ["GH_PLAN_SKIP_BOT"] = "1"
        actor, gh_cmd, expected_actor = plan.comment_route()
        assert actor == "active-gh-user", actor
        assert gh_cmd == "gh", gh_cmd
        assert expected_actor is None, expected_actor
    finally:
        if previous is None:
            os.environ.pop("GH_PLAN_SKIP_BOT", None)
        else:
            os.environ["GH_PLAN_SKIP_BOT"] = previous


def test_close_preserves_comment_reconciliation_after_partial_failure() -> None:
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
        "projects": {},
    }
    plan.get_issue = lambda ref, repo: ("automation-gh", issue)
    plan.comment_route = lambda: ("automation-gh", "fake-gh", "shiny-code-bot")

    def fake_run_raw(args: list[str], **_kwargs: Any) -> tuple[str, str, str]:
        calls.append(args)
        if args[:2] == ["issue", "edit"]:
            return "automation-gh", "", ""
        raise AssertionError(f"unexpected run_raw args: {args}")

    failure = plan.github_api_core.FailureDetail(
        cause="network_provider_failure",
        message="Comment write outcome is unknown",
        retryable=False,
        fallback_eligible=False,
        disposition="stop",
        write_outcome="unknown",
        completed_steps=["update_labels", "resolve_actor"],
        failed_step="create_comment",
    )

    def fake_comment(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise plan.github_comment_core.CommentError(
            failure.message,
            failure=failure,
            api_result={
                "status": 0,
                "completed_steps": failure.completed_steps,
                "failed_step": failure.failed_step,
            },
            payload={
                "comment_action": "create",
                "reconciliation": {
                    "strategy": "list_actor_comments_and_compare_body",
                    "actor": "shiny-code-bot",
                },
            },
        )

    plan.run_raw = fake_run_raw
    original_comment = plan.github_comment_core.comment
    plan.github_comment_core.comment = fake_comment
    try:
        try:
            plan.cmd_close(types.SimpleNamespace(
                repo="owner/repo",
                issue="10",
                reason="completed",
                body="Completed with evidence.\n",
                body_file=None,
                owner=None,
                project=None,
            ))
        except plan.PlanError as exc:
            assert exc.payload["comment_action"] == "create", exc.payload
            assert exc.payload["reconciliation"]["strategy"] == "list_actor_comments_and_compare_body", exc.payload
            assert exc.failure is failure
        else:
            raise AssertionError("expected comment failure")
    finally:
        plan.github_comment_core.comment = original_comment

    assert calls == [[
        "issue",
        "edit",
        "10",
        "-R",
        "owner/repo",
        "--remove-label",
        "plan:active",
        "--add-label",
        "plan:done",
    ]], calls


def test_close_reports_project_auth_denied_as_non_blocking_warning() -> None:
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
    plan.project_meta = lambda owner, project, recoverable=False: (
        "automation-gh",
        7,
        {"title": project, "number": 7},
    )
    plan.project_fields = lambda owner, project_number, recoverable=False: (_ for _ in ()).throw(
        plan.project_error("HTTP 403: resource not accessible by integration")
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
    assert payload["project"]["error_code"] == "project_auth_denied", payload
    assert "Use Project-capable auth for this operation." in payload["project"]["recommended_actions"], payload
    assert any(call[:2] == ["issue", "close"] for call in calls), calls


def test_close_syncs_project_before_closing_issue() -> None:
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
    plan.project_meta = lambda owner, project, recoverable=False: (
        "automation-gh",
        7,
        {"id": "project-id", "title": project, "number": 7},
    )
    plan.project_fields = lambda owner, project_number, recoverable=False: {
        "Status": {
            "id": "status-field",
            "name": "Status",
            "type": "ProjectV2SingleSelectField",
            "options": [{"name": "Done", "id": "done-option"}],
        },
        "Focus": {"id": "focus-field", "name": "Focus", "type": "ProjectV2Field"},
    }
    plan.find_project_item = lambda owner, project_number, issue_url, **kwargs: {"id": "item-id"}

    def fake_run_raw(args: list[str], **_kwargs: Any) -> tuple[str, str, str]:
        calls.append(args)
        if args[:2] in (["issue", "edit"], ["issue", "close"], ["project", "item-edit"]):
            return "automation-gh", "{}", ""
        raise AssertionError(f"unexpected run_raw args: {args}")

    plan.run_raw = fake_run_raw

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
    issue_close_index = next(i for i, call in enumerate(calls) if call[:2] == ["issue", "close"])
    project_edit_indices = [i for i, call in enumerate(calls) if call[:2] == ["project", "item-edit"]]
    assert project_edit_indices, calls
    assert max(project_edit_indices) < issue_close_index, calls


def test_find_project_item_uses_issue_query_and_higher_limit() -> None:
    plan = load_plan_module()
    calls: list[list[str]] = []

    def fake_gh_json(args: list[str], **_kwargs: Any) -> tuple[str, Any]:
        calls.append(args)
        if args[:2] == ["api", "-H"] and args[-1] == "rate_limit":
            return "automation-gh", {"resources": {"graphql": {"remaining": 200, "reset": 999}}}
        if args[:2] == ["project", "item-list"]:
            return "automation-gh", {
                "items": [
                    {
                        "id": "item-id",
                        "content": {"url": "https://github.com/owner/repo/issues/214"},
                    }
                ]
            }
        raise AssertionError(f"unexpected gh_json args: {args}")

    plan.gh_json = fake_gh_json
    item = plan.find_project_item(
        "owner",
        7,
        "https://github.com/owner/repo/issues/214",
        recoverable=True,
    )

    assert item["id"] == "item-id", item
    item_list = next(call for call in calls if call[:2] == ["project", "item-list"])
    assert "--query" in item_list, item_list
    assert "#214" in item_list, item_list
    assert "--limit" in item_list, item_list
    assert "50" in item_list, item_list


def test_find_project_item_falls_back_when_query_misses_exact_issue() -> None:
    plan = load_plan_module()
    calls: list[list[str]] = []

    def fake_gh_json(args: list[str], **_kwargs: Any) -> tuple[str, Any]:
        calls.append(args)
        if args[:2] == ["api", "-H"] and args[-1] == "rate_limit":
            return "automation-gh", {"resources": {"graphql": {"remaining": 200, "reset": 999}}}
        if args[:2] == ["project", "item-list"] and "--query" in args:
            return "automation-gh", {
                "items": [
                    {
                        "id": "other-item-id",
                        "content": {"url": "https://github.com/other/repo/issues/214"},
                    }
                ]
            }
        if args[:2] == ["project", "item-list"]:
            return "automation-gh", {
                "items": [
                    {
                        "id": "item-id",
                        "content": {"url": "https://github.com/owner/repo/issues/214"},
                    }
                ]
            }
        raise AssertionError(f"unexpected gh_json args: {args}")

    plan.gh_json = fake_gh_json
    item = plan.find_project_item(
        "owner",
        7,
        "https://github.com/owner/repo/issues/214",
        recoverable=True,
    )

    assert item["id"] == "item-id", item
    item_lists = [call for call in calls if call[:2] == ["project", "item-list"]]
    assert len(item_lists) == 2, item_lists
    assert "--query" in item_lists[0], item_lists
    assert "--query" not in item_lists[1], item_lists


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


def test_label_defs_cover_planning_labels_without_generic_fallback() -> None:
    plan = load_plan_module()
    config = plan.DEFAULT_CONFIG
    label_names = set(config["labels"].values())
    defs = config.get("label_defs", {})

    missing = sorted(label_names - set(defs))
    assert not missing, f"label_defs missing entries for: {missing}"

    generic_descriptions = sorted(
        name
        for name in label_names
        if not defs[name].get("description") or defs[name].get("description") == "Planning label"
    )
    assert not generic_descriptions, (
        f"labels using generic fallback description: {generic_descriptions}"
    )


def test_create_refuses_to_mint_undocumented_extra_labels() -> None:
    plan = load_plan_module()
    calls: list[list[str]] = []
    api_calls: list[dict[str, Any]] = []

    def fake_gh_json(args: list[str], **_kwargs: Any) -> tuple[str, Any]:
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return "automation-gh", []
        raise AssertionError(args)

    def fake_api_json(method: str, path: str, payload: Any = None, **_kwargs: Any) -> tuple[str, Any]:
        api_calls.append({"method": method, "path": path, "payload": payload})
        if method == "GET" and path.startswith("/repos/owner/repo/labels?"):
            return "automation-gh", []
        if method == "POST" and path == "/repos/owner/repo/labels":
            return "automation-gh", {"name": payload["name"]}
        raise AssertionError({"method": method, "path": path, "payload": payload})

    plan.load_config = lambda repo: {
        "labels": {"plan": "plan", "active": "plan:active"},
        "label_defs": {
            "plan": {"color": "5319e7", "description": "Durable planning issue"},
            "plan:active": {"color": "0e8a16", "description": "Plan is actionable now"},
        },
        "projects": {"enabled": False},
        "project_fields": {"focus": "Focus", "manager": "Manager", "finish_line": "Finish Line"},
        "workflow": {"default_manager": None, "repo_managers": {}},
    }
    plan.gh_json = fake_gh_json
    plan.api_json = fake_api_json
    plan.run_raw = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("label writes must use REST"))

    try:
        plan.cmd_create(types.SimpleNamespace(
            repo="owner/repo",
            title="Undocumented label",
            title_flag=None,
            body="## Current Status\n\nActionable.\n",
            body_file=None,
            label=["waiting"],
            milestone=None,
            project=None,
            force=False,
            plan_status="active",
            focus=None,
            manager=None,
            finish_line=None,
        ))
    except plan.PlanError as exc:
        assert "Refusing to create undocumented label(s): waiting" in str(exc), str(exc)
    else:
        raise AssertionError("missing undocumented extra label should fail")

    created_names = [call["payload"]["name"] for call in api_calls if call["method"] == "POST"]
    assert created_names == ["plan", "plan:active"], created_names


def test_create_allows_existing_extra_labels_without_creating_them() -> None:
    plan = load_plan_module()
    captured: dict[str, Any] = {}
    calls: list[list[str]] = []
    api_calls: list[dict[str, Any]] = []
    issue = {
        "repo": "owner/repo",
        "number": 12,
        "id": 1012,
        "title": "Existing label",
        "body": "## Current Status\n\nActionable.\n",
        "html_url": "https://github.com/owner/repo/issues/12",
        "labels": [{"name": "plan"}, {"name": "plan:active"}, {"name": "customer"}],
        "state": "open",
    }

    def fake_gh_json(args: list[str], **_kwargs: Any) -> tuple[str, Any]:
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return "automation-gh", []
        raise AssertionError(args)

    def fake_api_json(method: str, path: str, payload: Any = None, **_kwargs: Any) -> tuple[str, Any]:
        api_calls.append({"method": method, "path": path, "payload": payload})
        if method == "GET" and path.startswith("/repos/owner/repo/labels?"):
            return "automation-gh", [{"name": "customer"}]
        if method == "POST" and path == "/repos/owner/repo/labels":
            return "automation-gh", {"name": payload["name"]}
        raise AssertionError({"method": method, "path": path, "payload": payload})

    plan.load_config = lambda repo: {
        "labels": {"plan": "plan", "active": "plan:active"},
        "label_defs": {
            "plan": {"color": "5319e7", "description": "Durable planning issue"},
            "plan:active": {"color": "0e8a16", "description": "Plan is actionable now"},
        },
        "projects": {"enabled": False},
        "project_fields": {"focus": "Focus", "manager": "Manager", "finish_line": "Finish Line"},
        "workflow": {"default_manager": None, "repo_managers": {}},
    }
    plan.gh_json = fake_gh_json
    plan.api_json = fake_api_json
    plan.run_raw = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("label writes must use REST"))
    plan.rest_create_issue = lambda repo, title, body, labels, milestone: (captured.setdefault("labels", labels), ("automation-gh", issue))[1]

    output = StringIO()
    with redirect_stdout(output):
        plan.cmd_create(types.SimpleNamespace(
            repo="owner/repo",
            title="Existing label",
            title_flag=None,
            body="## Current Status\n\nActionable.\n",
            body_file=None,
            label=["customer"],
            milestone=None,
            project=None,
            force=False,
            plan_status="active",
            focus=None,
            manager=None,
            finish_line=None,
        ))

    payload = json.loads(output.getvalue())
    assert payload["ok"] is True, payload
    assert captured["labels"] == ["plan", "plan:active", "customer"], captured
    created_names = [call["payload"]["name"] for call in api_calls if call["method"] == "POST"]
    assert created_names == ["plan", "plan:active"], created_names


def test_run_raw_does_not_change_actor_on_graphql_rate_limit() -> None:
    plan = load_plan_module()
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[0].endswith("gh-with-env-token"):
            return completed(stderr="GraphQL: API rate limit already exceeded", returncode=1)
        raise AssertionError(f"active gh should not be called for rate limits: {command}")

    plan.subprocess.run = fake_run
    try:
        plan.run_raw(["api", "rate_limit"], recoverable=True)
    except plan.PlanError as exc:
        assert "GraphQL: API rate limit already exceeded" in str(exc), exc
    else:
        raise AssertionError("GraphQL rate limit should remain on the automation actor")
    assert len(calls) == 1, calls

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


def test_plan_cli_emits_shared_terminal_envelope() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        gh_path = tmp_path / "gh"
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"${1:-}\" == \"api\" && \"$*\" == *'repos/owner/repo/issues/1'* ]]; then\n"
            "  printf '%s\\n' '{\"number\":1,\"id\":1001,\"title\":\"Plan\",\"body\":\"## Current Status\\n\\nReady.\\n\",\"html_url\":\"https://github.com/owner/repo/issues/1\",\"labels\":[],\"state\":\"open\"}'\n"
            "  exit 0\n"
            "fi\n"
            "printf 'unexpected command: %s\\n' \"$*\" >&2\n"
            "exit 1\n"
        )
        gh_path.chmod(0o755)
        env = dict(
            os.environ,
            PATH=f"{tmp_path}:{os.environ['PATH']}",
            GH_PLAN_SKIP_BOT="1",
            GH_PLAN_ALLOW_ACTIVE_FIRST="1",
            CODE_HOME=str(tmp_path / ".code"),
        )
        result = REAL_SUBPROCESS_RUN(
            [sys.executable, str(SCRIPT), "--repo", "owner/repo", "show", "1", "--full"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )

    assert result.returncode == 0, result
    assert len([line for line in result.stdout.splitlines() if line.strip()]) == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1, payload
    assert payload["operation"] == "github.plan.show", payload
    assert payload["transport"] == "rest_api", payload
    assert payload["bucket"] == "rest_core", payload
    assert payload["actor"] == "active-gh-user", payload
    assert payload["issue"]["number"] == 1, payload
    assert result.stderr == "", result.stderr


def test_plan_project_query_failure_is_not_a_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        gh_path = tmp_path / "gh"
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"${1:-}\" == \"api\" && \"$*\" == *'rate_limit'* ]]; then\n"
            "  printf '{\"resources\":{\"graphql\":{\"limit\":5000,\"remaining\":100,\"reset\":1700000000}}}\\n'\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"${1:-} ${2:-}\" == \"project list\" ]]; then\n"
            "  printf 'GraphQL: API rate limit already exceeded\\n' >&2\n"
            "  exit 1\n"
            "fi\n"
            "printf 'unexpected command: %s\\n' \"$*\" >&2\n"
            "exit 1\n"
        )
        gh_path.chmod(0o755)
        env = dict(
            os.environ,
            PATH=f"{tmp_path}:{os.environ['PATH']}",
            GH_PLAN_SKIP_BOT="1",
            GH_PLAN_ALLOW_ACTIVE_FIRST="1",
            CODE_HOME=str(tmp_path / ".code"),
        )
        result = REAL_SUBPROCESS_RUN(
            [sys.executable, str(SCRIPT), "project-list", "--owner", "owner"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )

    assert result.returncode == 1, result
    assert len([line for line in result.stdout.splitlines() if line.strip()]) == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["operation"] == "github.plan.project_list", payload
    assert payload["failure"]["cause"] == "graphql_primary_rate_limited", payload
    assert payload["graphql_operation"] == "query", payload
    assert payload["write_outcome"] is None, payload
    assert payload["retryable"] is True, payload
    assert "GraphQL" in result.stderr, result.stderr


def test_python_helper_parser_failures_emit_envelopes() -> None:
    pr_result = REAL_SUBPROCESS_RUN(
        [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "comment", "1"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert pr_result.returncode == 2, pr_result
    pr_payload = json.loads(pr_result.stdout)
    assert pr_payload["operation"] == "github.pr.comment", pr_payload
    assert pr_payload["failure"]["cause"] == "validation_error", pr_payload
    assert pr_payload["write_outcome"] == "not_started", pr_payload
    assert "required" in pr_result.stderr, pr_result.stderr

    plan_result = REAL_SUBPROCESS_RUN(
        [sys.executable, str(SCRIPT), "project-list"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert plan_result.returncode == 2, plan_result
    plan_payload = json.loads(plan_result.stdout)
    assert plan_payload["operation"] == "github.plan.project_list", plan_payload
    assert plan_payload["failure"]["cause"] == "validation_error", plan_payload
    assert plan_payload["write_outcome"] is None, plan_payload
    assert "required" in plan_result.stderr, plan_result.stderr


def test_plan_missing_bot_route_fails_closed() -> None:
    plan = load_plan_module()
    plan.BOT_GH = Path("/definitely/missing/gh-with-env-token")
    original_skip = os.environ.pop("GH_PLAN_SKIP_BOT", None)
    original_active = os.environ.pop("GH_PLAN_ALLOW_ACTIVE_FIRST", None)

    def unexpected_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("active gh must not run without explicit authorization")

    plan.subprocess.run = unexpected_run
    try:
        try:
            plan.run_raw(["issue", "close", "1", "-R", "owner/repo"])
        except plan.PlanError as exc:
            assert exc.failure is not None, exc
            assert exc.failure.cause == "invalid_credentials", exc.failure
            assert exc.failure.write_outcome == "not_started", exc.failure
        else:
            raise AssertionError("missing automation helper should fail closed")
    finally:
        if original_skip is not None:
            os.environ["GH_PLAN_SKIP_BOT"] = original_skip
        if original_active is not None:
            os.environ["GH_PLAN_ALLOW_ACTIVE_FIRST"] = original_active


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


def test_pr_helper_write_commands_route_through_configured_gh() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        body_path = tmp_path / "body.md"
        body_path.write_text("## Body\n\nCloses #146\n")
        gh_path = tmp_path / "gh"
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "payload=''\n"
            "if [[ \"$*\" == *'--input -'* ]]; then payload=\"$(cat)\"; fi\n"
            "printf '%s | %s\\n' \"$*\" \"$payload\" >>\"$GH_PR_TEST_LOG\"\n"
            "case \"$*\" in\n"
            "  pr\\ create*) printf 'https://github.com/owner/repo/pull/9\\n' ;;\n"
            "  pr\\ edit*) printf 'https://github.com/owner/repo/pull/9\\n' ;;\n"
            "  *'/user'*) printf '{\"login\":\"shiny-code-bot\"}\\n' ;;\n"
            "  *'/repos/owner/repo/issues/9/comments'*) printf '{\"id\":1,\"html_url\":\"https://github.com/owner/repo/pull/9#issuecomment-1\",\"user\":{\"login\":\"shiny-code-bot\"}}\\n' ;;\n"
            "  *) printf '{}\\n' ;;\n"
            "esac\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path), GH_PR_TEST_LOG=str(log_path))
        create = REAL_SUBPROCESS_RUN(
            [
                sys.executable,
                str(PR_SCRIPT),
                "--repo",
                "owner/repo",
                "create",
                "--title",
                "Bot write path",
                "--body-file",
                str(body_path),
                "--base",
                "main",
                "--head",
                "feature",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        edit = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "edit", "9", "--body-file", str(body_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        comment = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "comment", "9", "--body-file", str(body_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=True,
        )
        create_without_body = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "create", "--title", "Missing body"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        edit_without_changes = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "edit", "9"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        calls = log_path.read_text().splitlines()
    assert json.loads(create.stdout)["url"] == "https://github.com/owner/repo/pull/9"
    edit_payload = json.loads(edit.stdout)
    assert edit_payload["operation"] == "github.pr.edit"
    assert edit_payload["action"] == "edit"
    assert json.loads(comment.stdout)["url"] == "https://github.com/owner/repo/pull/9#issuecomment-1"
    assert create_without_body.returncode == 1
    assert "requires --body-file" in create_without_body.stderr
    assert json.loads(create_without_body.stdout)["schema_version"] == 1
    assert edit_without_changes.returncode == 1
    assert "requires at least one edit flag" in edit_without_changes.stderr
    assert json.loads(edit_without_changes.stdout)["schema_version"] == 1
    assert any(
        f"pr create --repo owner/repo --title Bot write path --body-file {body_path} --base main --head feature" in call
        for call in calls
    ), calls
    assert any(f"pr edit 9 --repo owner/repo --body-file {body_path}" in call for call in calls), calls
    assert any("/repos/owner/repo/issues/9/comments" in call and "Closes #146" in call for call in calls), calls
    assert not any("pr create --repo owner/repo --title Missing body" in call for call in calls), calls
    assert not any(call.startswith("pr edit 9 --repo owner/repo |") for call in calls), calls


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
    payload = json.loads(result.stdout)
    assert payload["ok"] is False, payload
    assert payload["schema_version"] == 1, payload
    assert payload["error"] == "PR merge failed", payload
    assert payload["detail"] == "gh: Not Found (HTTP 404)", payload
    assert payload["operation"] == "github.pr.merge", payload
    assert payload["action"] == "merge", payload
    assert payload["repo"] == "owner/repo", payload
    assert payload["pr"] == 12, payload
    assert payload["endpoint"] == "/repos/owner/repo/pulls/12/merge", payload
    assert payload["headSha"] == "head-sha", payload
    assert "token scope" in payload["hint"], payload
    assert payload["api_result"]["failure"]["cause"] == "network_provider_failure", payload
    assert result.stderr.strip() == "error: PR merge failed", result.stderr


def test_pr_helper_rest_failure_preserves_diagnostics_and_redacts_secrets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        body_path = tmp_path / "comment.md"
        body_path.write_text("Review note\n")
        gh_path = tmp_path / "gh"
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"$*\" == *'/user'* ]]; then\n"
            "  printf '{\"login\":\"shiny-code-bot\"}\\n'\n"
            "  exit 0\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/issues/9/comments'* ]]; then\n"
            "  printf 'HTTP/2.0 403 \\r\\n'\n"
            "  printf 'content-type: application/json\\r\\n'\n"
            "  printf 'x-github-request-id: request-123\\r\\n'\n"
            "  printf '\\r\\n'\n"
            "  printf '{\"message\":\"Forbidden token=synthetic-secret\",\"credentials\":\"synthetic-private\"}\\n'\n"
            "  exit 1\n"
            "fi\n"
            "printf '{}\\n'\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path))
        result = REAL_SUBPROCESS_RUN(
            [
                sys.executable,
                str(PR_SCRIPT),
                "--repo",
                "owner/repo",
                "comment",
                "9",
                "--body-file",
                str(body_path),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )

    assert result.returncode == 1, result
    assert "synthetic-secret" not in result.stdout + result.stderr, result
    assert "synthetic-private" not in result.stdout + result.stderr, result
    payload = json.loads(result.stdout)
    api_result = payload["api_result"]
    assert payload["operation"] == "github.pr.comment", payload
    assert payload["failure"]["cause"] == "permission_denied", payload
    assert payload["request_id"] == "request-123", payload
    assert payload["write_outcome"] == "not_started", payload
    assert payload["fallback_eligible"] is True, payload
    assert api_result["failure"]["cause"] == "permission_denied", payload
    assert api_result["failure"]["request_id"] == "request-123", payload
    assert "body" not in api_result, payload
    assert "[REDACTED]" in api_result["failure"]["message"], payload


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
            "payload=''\n"
            "if [[ \"$*\" == *'--input -'* ]]; then payload=\"$(cat)\"; fi\n"
            "printf '%s | %s\\n' \"$*\" \"$payload\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/user'* ]]; then\n"
            "  printf '{\"login\":\"shiny-code-bot\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  if [[ \"$payload\" != *'superseded by #13'* ]]; then exit 2; fi\n"
            "  if [[ \"$payload\" != *'Issue-closing references'* ]]; then exit 2; fi\n"
            "  printf '{\"id\":1,\"html_url\":\"https://github.com/owner/repo/pull/12#issuecomment-1\",\"user\":{\"login\":\"shiny-code-bot\"}}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls?state=open'* ]]; then\n"
            "  printf '[{\"number\":12}]\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo?per_page='* || \"$*\" == *'/repos/owner/repo' ]]; then\n"
            "  printf '{\"default_branch\":\"main\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/git/refs/heads/old-topic'* ]]; then\n"
            "  if [[ \"$*\" != *'--method DELETE'* ]]; then exit 2; fi\n"
            "  exit 0\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* && \"$*\" == *'--method PATCH'* ]]; then\n"
            "  if [[ \"$payload\" == *'\"state\": \"closed\"'* ]]; then\n"
            "    printf '{\"number\":12,\"title\":\"Old\",\"state\":\"closed\",\"draft\":false,\"mergeable\":true,\"mergeable_state\":\"clean\",\"html_url\":\"https://github.com/owner/repo/pull/12\",\"head\":{\"ref\":\"old-topic\",\"sha\":\"old-sha\",\"repo\":{\"full_name\":\"owner/repo\"}},\"base\":{\"ref\":\"main\",\"repo\":{\"full_name\":\"owner/repo\"}}}\\n'\n"
            "  else\n"
            "    if [[ \"$payload\" != *'Refs #144'* || \"$payload\" != *'Refs owner/repo#145'* ]]; then exit 2; fi\n"
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
    assert '\"state\": \"closed\"' in calls, calls


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
            "payload=''\n"
            "if [[ \"$*\" == *'--input -'* ]]; then payload=\"$(cat)\"; fi\n"
            "printf '%s | %s\\n' \"$*\" \"$payload\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  exit 3\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* && \"$*\" == *'--method PATCH'* ]]; then\n"
            "  if [[ \"$payload\" == *'\"state\": \"closed\"'* ]]; then\n"
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
    payload = json.loads(result.stdout)
    assert payload["ok"] is False, payload
    assert payload["error"] == "gh: Forbidden (HTTP 403)", payload
    assert payload["api_result"]["failure"]["cause"] == "permission_denied", payload
    assert result.stderr.strip() == "error: gh: Forbidden (HTTP 403)", result.stderr
    assert not any("/repos/owner/repo/issues/12/comments" in line for line in calls.splitlines()), calls
    assert not any(
        "/repos/owner/repo/pulls/12" in line and "--method PATCH" in line and '\"body\":' in line
        for line in calls.splitlines()
    ), calls


def test_pr_helper_supersede_reports_comment_failure_after_close() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log_path = tmp_path / "calls.log"
        gh_path = tmp_path / "gh"
        pr_json = (
            '{"number":12,"title":"Old","state":"open","draft":false,'
            '"body":"Refs #144","mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/12",'
            '"head":{"ref":"old-topic","sha":"old-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        winner_json = (
            '{"number":13,"title":"New","state":"open","draft":false,'
            '"body":"Refs #144","mergeable":true,"mergeable_state":"clean",'
            '"html_url":"https://github.com/owner/repo/pull/13",'
            '"head":{"ref":"new-topic","sha":"new-sha","repo":{"full_name":"owner/repo"}},'
            '"base":{"ref":"main","repo":{"full_name":"owner/repo"}}}'
        )
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "payload=''\n"
            "if [[ \"$*\" == *'--input -'* ]]; then payload=\"$(cat)\"; fi\n"
            "printf '%s | %s\\n' \"$*\" \"$payload\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/user'* ]]; then\n"
            "  printf '{\"login\":\"shiny-code-bot\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  printf 'HTTP/2.0 503 \\r\\ncontent-type: application/json\\r\\n\\r\\n{\"message\":\"upstream unavailable\"}\\n'\n"
            "  exit 1\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* && \"$*\" == *'--method PATCH'* ]]; then\n"
            "  printf '{\"number\":12,\"state\":\"closed\"}\\n'\n"
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
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "supersede", "12", "--by", "13"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        calls = log_path.read_text().splitlines()

    assert result.returncode == 1, result
    payload = json.loads(result.stdout)
    assert payload["operation"] == "github.pr.supersede", payload
    assert payload["failure"]["cause"] == "network_provider_failure", payload
    assert payload["write_outcome"] == "unknown", payload
    assert payload["completed_steps"] == ["close_pull_request", "resolve_actor"], payload
    assert payload["failed_step"] == "post_supersede_comment", payload
    assert payload["status"] == 503, payload
    assert payload["actor"] == "shiny-code-bot", payload
    close_index = next(index for index, call in enumerate(calls) if "/pulls/12" in call and "--method PATCH" in call)
    comment_index = next(index for index, call in enumerate(calls) if "/issues/12/comments" in call)
    assert close_index < comment_index, calls


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
            "payload=''\n"
            "if [[ \"$*\" == *'--input -'* ]]; then payload=\"$(cat)\"; fi\n"
            "printf '%s | %s\\n' \"$*\" \"$payload\" >>\"$GH_PR_TEST_LOG\"\n"
            "if [[ \"$*\" == *'/user'* ]]; then\n"
            "  printf '{\"login\":\"shiny-code-bot\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  printf '{\"id\":1,\"html_url\":\"https://github.com/owner/repo/pull/12#issuecomment-1\",\"user\":{\"login\":\"shiny-code-bot\"}}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls/12'* && \"$*\" == *'--method PATCH'* ]]; then\n"
            "  if [[ \"$payload\" == *'Refs #144'* ]]; then\n"
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
            "  printf 'remote ref could not be deleted credentials=synthetic-delete authorization=synthetic-auth\\n' >&2\n"
            "  exit 1\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/pulls?state=open'* ]]; then\n"
            "  printf '[{\"number\":12}]\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo?per_page='* || \"$*\" == *'/repos/owner/repo' ]]; then\n"
            "  printf '{\"default_branch\":\"main\"}\\n'\n"
            "elif [[ \"$*\" == *'/user'* ]]; then\n"
            "  printf '{\"login\":\"shiny-code-bot\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  printf '{\"id\":1,\"html_url\":\"https://github.com/owner/repo/pull/12#issuecomment-1\",\"user\":{\"login\":\"shiny-code-bot\"}}\\n'\n"
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
    assert "synthetic-delete" not in result.stdout + result.stderr, result
    assert "synthetic-auth" not in result.stdout + result.stderr, result
    assert payload["deletedBranch"]["deleted"] is False, payload
    assert payload["deletedBranch"]["ref"] == "heads/old-topic", payload
    assert payload["deletedBranch"]["stderr"] == (
        "remote ref could not be deleted credentials=[REDACTED] authorization=[REDACTED]"
    ), payload
    assert payload["deletedBranch"]["api_result"]["failure"]["cause"] == "network_provider_failure", payload
    assert payload["cleanupWarnings"] == [
        {
            "kind": "remote_branch_delete_failed",
            "ref": "heads/old-topic",
            "stderr": "remote ref could not be deleted credentials=[REDACTED] authorization=[REDACTED]",
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
            "elif [[ \"$*\" == *'/user'* ]]; then\n"
            "  printf '{\"login\":\"shiny-code-bot\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  printf '{\"id\":1,\"html_url\":\"https://github.com/owner/repo/pull/12#issuecomment-1\",\"user\":{\"login\":\"shiny-code-bot\"}}\\n'\n"
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
            "elif [[ \"$*\" == *'/user'* ]]; then\n"
            "  printf '{\"login\":\"shiny-code-bot\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  printf '{\"id\":1,\"html_url\":\"https://github.com/owner/repo/pull/12#issuecomment-1\",\"user\":{\"login\":\"shiny-code-bot\"}}\\n'\n"
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
            "elif [[ \"$*\" == *'/user'* ]]; then\n"
            "  printf '{\"login\":\"shiny-code-bot\"}\\n'\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/issues/12/comments'* ]]; then\n"
            "  printf '{\"id\":1,\"html_url\":\"https://github.com/owner/repo/pull/12#issuecomment-1\",\"user\":{\"login\":\"shiny-code-bot\"}}\\n'\n"
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
            "  printf 'HTTP/2.0 200 \\r\\ncontent-type: application/json\\r\\n'\n"
            "  if [[ \"$*\" != *'page=2'* ]]; then\n"
            "    printf 'link: <https://api.github.com/repos/other/repo/commits/head-sha/check-runs?per_page=100&page=2>; rel=\"next\"\\r\\n'\n"
            "  fi\n"
            "  printf '\\r\\n'\n"
            "  if [[ \"$*\" == *'page=2'* ]]; then\n"
            "    printf '{\"check_runs\":[{\"name\":\"ci-2\",\"status\":\"completed\",\"conclusion\":\"failure\"}]}\\n'\n"
            "  else\n"
            "    printf '{\"check_runs\":[{\"name\":\"ci-1\",\"status\":\"completed\",\"conclusion\":\"success\"}]}\\n'\n"
            "  fi\n"
            "elif [[ \"$*\" == *'/repos/other/repo/commits/head-sha/statuses'* ]]; then\n"
            "  printf 'HTTP/2.0 200 \\r\\ncontent-type: application/json\\r\\n'\n"
            "  if [[ \"$*\" != *'page=2'* ]]; then\n"
            "    printf 'link: <https://api.github.com/repos/other/repo/commits/head-sha/statuses?per_page=100&page=2>; rel=\"next\"\\r\\n'\n"
            "  fi\n"
            "  printf '\\r\\n'\n"
            "  if [[ \"$*\" == *'page=2'* ]]; then\n"
            "    printf '[{\"context\":\"legacy-2\",\"state\":\"error\"}]\\n'\n"
            "  else\n"
            "    printf '[{\"context\":\"legacy-1\",\"state\":\"success\"}]\\n'\n"
            "  fi\n"
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
    assert "page=2" in calls
    assert "--paginate" not in calls


def test_pr_helper_paged_rest_failure_preserves_diagnostics() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        gh_path = tmp_path / "gh"
        gh_path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [[ \"$*\" == *'/repos/owner/repo/pulls/12'* ]]; then\n"
            "  printf '{\"head\":{\"sha\":\"head-sha\"}}\\n'\n"
            "  exit 0\n"
            "elif [[ \"$*\" == *'/repos/owner/repo/commits/head-sha/check-runs'* ]]; then\n"
            "  printf 'HTTP/2.0 403 \\r\\ncontent-type: application/json\\r\\nx-github-request-id: paged-request\\r\\n\\r\\n'\n"
            "  printf '{\"message\":\"Forbidden credentials=synthetic-paged\"}\\n'\n"
            "  exit 1\n"
            "fi\n"
            "printf '{}\\n'\n"
        )
        gh_path.chmod(0o755)
        env = dict(os.environ, GH_PR_GH=str(gh_path))
        result = REAL_SUBPROCESS_RUN(
            [sys.executable, str(PR_SCRIPT), "--repo", "owner/repo", "checks", "12"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )

    assert result.returncode == 1, result
    assert "synthetic-paged" not in result.stdout + result.stderr, result
    assert result.stdout.strip(), result.stderr
    payload = json.loads(result.stdout)
    assert payload["api_result"]["failure"]["cause"] == "permission_denied", payload
    assert payload["api_result"]["request_id"] == "paged-request", payload


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
        test_plan_index_paginates_filters_prs_and_honors_limit,
        test_plan_search_uses_search_bucket_and_conditional_state,
        test_plan_ensure_labels_uses_paged_rest_and_reconciles_conflict,
        test_plan_ensure_labels_skips_existing_case_insensitively,
        test_plan_paged_rest_failure_receives_completed_page_evidence,
        test_plan_rest_command_context_and_limit_validation,
        test_project_commands_are_recoverable,
        test_repo_config_path_skips_missing_home_candidate,
        test_manager_for_repo_passes_raw_values_without_people_resolver,
        test_manager_for_repo_skips_unresolved_person_ref,
        test_explicit_unresolved_person_manager_is_skipped,
        test_person_resolution_requires_uv,
        test_raw_manager_values_do_not_resolve_through_people,
        test_manager_for_repo_resolves_person_ref_to_project_label_when_available,
        test_create_reports_issue_when_project_sync_fails,
        test_create_reports_stale_project_as_non_blocking_warning,
        test_create_reports_project_auth_denied_as_non_blocking_warning,
        test_close_reports_stale_project_as_non_blocking_warning,
        test_close_delegates_comment_to_shared_rest_helper,
        test_comment_route_matches_explicit_auth_policy,
        test_close_preserves_comment_reconciliation_after_partial_failure,
        test_close_reports_project_auth_denied_as_non_blocking_warning,
        test_close_syncs_project_before_closing_issue,
        test_find_project_item_uses_issue_query_and_higher_limit,
        test_find_project_item_falls_back_when_query_misses_exact_issue,
        test_create_supports_waiting_plan_status,
        test_label_defs_cover_planning_labels_without_generic_fallback,
        test_create_refuses_to_mint_undocumented_extra_labels,
        test_create_allows_existing_extra_labels_without_creating_them,
        test_run_raw_does_not_change_actor_on_graphql_rate_limit,
        test_run_raw_is_bot_first_even_when_prefer_active_is_requested,
        test_plan_cli_emits_shared_terminal_envelope,
        test_plan_project_query_failure_is_not_a_write,
        test_python_helper_parser_failures_emit_envelopes,
        test_plan_missing_bot_route_fails_closed,
        test_pr_helper_uses_rest_endpoints_for_common_pr_work,
        test_pr_helper_write_commands_route_through_configured_gh,
        test_pr_helper_list_paginates_only_when_limit_exceeds_one_page,
        test_pr_helper_delete_branch_uses_rest_ref_delete,
        test_pr_helper_merge_404_includes_recovery_context,
        test_pr_helper_rest_failure_preserves_diagnostics_and_redacts_secrets,
        test_pr_helper_supersede_comments_neutralizes_and_closes,
        test_pr_helper_supersede_does_not_comment_when_close_fails,
        test_pr_helper_supersede_reports_comment_failure_after_close,
        test_pr_helper_supersede_warns_when_body_rewrite_fails_after_close,
        test_pr_helper_supersede_warns_when_branch_delete_fails,
        test_pr_helper_supersede_skips_branch_delete_when_winner_uses_same_branch,
        test_pr_helper_supersede_skips_branch_delete_when_third_pr_uses_branch,
        test_pr_helper_supersede_skips_branch_delete_for_default_branch,
        test_pr_helper_preserves_url_repo_and_paginates_checks,
        test_pr_helper_paged_rest_failure_preserves_diagnostics,
        test_pr_helper_accepts_enterprise_pr_urls,
        test_project_set_accepts_item_id_and_classifies_low_graphql,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")


if __name__ == "__main__":
    main()
