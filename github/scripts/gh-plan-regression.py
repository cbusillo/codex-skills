#!/usr/bin/env python3
"""Regression checks for gh-plan.py command shapes.

These checks avoid live GitHub calls. They load gh-plan.py as a module and
replace subprocess.run with a tiny gh simulator so prompt/skill regressions fail
before they reach automation.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import types
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).with_name("gh-plan.py")


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
    calls: list[tuple[list[str], str | None]] = []
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

    def fake_run(
        command: list[str],
        input: str | None = None,
        text: bool | None = None,
        stdout: Any = None,
        stderr: Any = None,
        cwd: Any = None,
    ) -> subprocess.CompletedProcess[str]:
        del text, stdout, stderr, cwd
        calls.append((command, input))
        args = normalized_gh_args(command)
        if not args or args[0] != "api":
            raise AssertionError(f"unexpected command: {command}")
        endpoint = next((arg for arg in args if arg.startswith("repos/")), "")
        method = args[args.index("-X") + 1] if "-X" in args else "GET"
        parts = endpoint.split("/")
        repo = "/".join(parts[1:3])
        number = int(parts[4])
        if method == "GET":
            return completed(json.dumps(issues[(repo, number)]))
        if method == "PATCH":
            assert args[-2:] == ["--input", "-"], args
            payload = json.loads(input or "{}")
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
        args = normalized_gh_args(command)
        assert args[0] == "api", command
        assert "issue" not in args, command
        assert "--input" in args and args[args.index("--input") + 1] == "-", command
        assert json.loads(body or "{}"), command


def test_project_commands_are_recoverable() -> None:
    plan = load_plan_module()
    calls: list[dict[str, Any]] = []

    def fake_gh_json(
        args: list[str],
        *,
        input_text: str | None = None,
        prefer_active: bool = False,
        recoverable: bool = False,
    ) -> tuple[str, Any]:
        del input_text
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


def main() -> None:
    tests = [test_issue_body_updates_use_rest_patch, test_project_commands_are_recoverable]
    for test in tests:
        test()
        print(f"ok {test.__name__}")


if __name__ == "__main__":
    main()
