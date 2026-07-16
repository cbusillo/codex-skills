#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Compact GitHub issue planning helper for Codex skills."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional

import github_api as github_api_core
import github_comment as github_comment_core


SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
BOT_GH = SKILL_DIR.parent / "github/scripts/gh-with-env-token"
PEOPLE_RESOLVER = SKILL_DIR.parent / "people/scripts/resolve_person.py"
API_VERSION_ARGS = ["-H", "X-GitHub-Api-Version: 2022-11-28"]
EXPECTED_ACTOR = os.environ.get("GH_WITH_ENV_TOKEN_EXPECTED_LOGIN") or "shiny-code-bot"
CURRENT_OPERATION = "github.plan.unknown"
CURRENT_TRANSPORT = "helper"
CURRENT_BUCKET = "unknown"
CURRENT_IS_WRITE = False

PLAN_COMMAND_CONTEXT: dict[str, tuple[str, str, bool]] = {
    "index": ("gh_cli_graphql", "graphql", False),
    "search": ("gh_cli_graphql", "graphql", False),
    "show": ("rest_api", "rest_core", False),
    "create": ("composite", "mixed", True),
    "update-section": ("rest_api", "rest_core", True),
    "link": ("rest_api", "rest_core", True),
    "unlink": ("rest_api", "rest_core", True),
    "deps": ("rest_api", "rest_core", False),
    "close": ("composite", "mixed", True),
    "project-add": ("gh_cli_graphql", "graphql", True),
    "project-set": ("gh_cli_graphql", "graphql", True),
    "project-list": ("gh_cli_graphql", "graphql", False),
    "ensure-labels": ("composite", "mixed", True),
}

DEFAULT_CONFIG: dict[str, Any] = {
    "labels": {
        "plan": "plan",
        "active": "plan:active",
        "blocked": "plan:blocked",
        "waiting": "plan:waiting",
        "stale": "plan:stale",
        "done": "plan:done",
    },
    "label_defs": {
        "plan": {"color": "5319e7", "description": "Durable planning issue"},
        "plan:active": {"color": "0e8a16", "description": "Plan is actionable now"},
        "plan:blocked": {"color": "d93f0b", "description": "Plan blocked by an open native dependency issue"},
        "plan:waiting": {"color": "fbca04", "description": "Durable plan parked pending a decision, event, or non-issue condition; not for PR QA"},
        "plan:stale": {"color": "bfbfbf", "description": "Plan needs review before guiding work"},
        "plan:done": {"color": "006b75", "description": "Plan completed or superseded"},
    },
    "default_sections": [
        "Finish Line",
        "Current Status",
        "Relationships",
        "Acceptance Criteria",
        "Open Questions",
    ],
    "projects": {"enabled": True, "owner": None, "default_project": None},
    "workflow": {"default_manager": None, "repo_managers": {}},
    "project_fields": {
        "focus": "Focus",
        "manager": "Manager",
        "finish_line": "Finish Line",
    },
}

class PlanError(Exception):
    def __init__(
        self,
        message: str,
        *,
        failure: Optional[github_api_core.FailureDetail] = None,
        api_result: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
    ):
        super().__init__(github_api_core.redact_string(message))
        self.failure = failure
        self.api_result = github_api_core.redact_body(api_result) if api_result is not None else None
        self.payload = github_api_core.redact_body(payload or {})


class ClassifiedPlanError(PlanError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_at: Optional[int] = None,
        failure: Optional[github_api_core.FailureDetail] = None,
        api_result: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message, failure=failure, api_result=api_result, payload=payload)
        self.code = code
        self.retry_at = retry_at


def die(
    message: str,
    *,
    detail: str | None = None,
    code: int = 1,
    error_code: str | None = None,
    retry_at: int | None = None,
    failure: github_api_core.FailureDetail | None = None,
    api_result: dict[str, Any] | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> None:
    if failure is None:
        failure = github_api_core.FailureDetail(
            cause=error_code or "plan_error",
            message=message,
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started" if CURRENT_IS_WRITE else None,
        )
    payload: dict[str, Any] = dict(extra_payload or {})
    if detail:
        payload["detail"] = detail.strip()
    if api_result is not None:
        payload["api_result"] = api_result
    expected_actor = (
        api_result["expected_actor"]
        if api_result is not None and "expected_actor" in api_result
        else EXPECTED_ACTOR
    )
    envelope = github_api_core.terminal_failure(
        failure,
        operation=CURRENT_OPERATION,
        payload=payload,
        expected_actor=expected_actor,
        transport=CURRENT_TRANSPORT,
        bucket=CURRENT_BUCKET,
        exit_code=code,
        error=message,
        error_code=error_code,
    )
    if api_result is not None:
        for key in (
            "actor",
            "status",
            "request_id",
            "quota",
            "rate_limit",
            "retry_at",
            "retry_after",
            "graphql_operation",
            "completed_steps",
            "failed_step",
        ):
            if api_result.get(key) is not None:
                envelope[key] = api_result[key]
    if retry_at is not None:
        envelope["retry_at"] = retry_at
    github_api_core.emit_terminal(envelope, stderr_message=f"error: {message}")
    raise SystemExit(code)


def emit(payload: Any) -> None:
    actor = payload.get("actor") if isinstance(payload, dict) else None
    expected_actor = payload.get("expected_actor", EXPECTED_ACTOR) if isinstance(payload, dict) else EXPECTED_ACTOR
    completed_steps = payload.get("completed_steps") if isinstance(payload, dict) else None
    github_api_core.emit_terminal(
        github_api_core.terminal_success(
            payload,
            operation=CURRENT_OPERATION,
            actor=actor,
            expected_actor=expected_actor,
            transport=CURRENT_TRANSPORT,
            bucket=CURRENT_BUCKET,
            completed_steps=completed_steps if isinstance(completed_steps, list) else None,
        )
    )


def get_runtime_home() -> pathlib.Path:
    if os.environ.get("CODE_HOME"):
        return pathlib.Path(os.environ["CODE_HOME"]).expanduser()
    if os.environ.get("CODEX_HOME"):
        return pathlib.Path(os.environ["CODEX_HOME"]).expanduser()
    code_home = pathlib.Path("~/.code").expanduser()
    if (code_home / "skills").is_dir() or (code_home / "plans").exists():
        return code_home
    return pathlib.Path("~/.codex").expanduser()


def workspace_config_path() -> pathlib.Path:
    return get_runtime_home() / "github-planning.json"


def comment_route() -> tuple[str, str, Optional[str]]:
    """Resolve the same explicit actor route used by plan writes."""
    skip_bot = os.environ.get("GH_PLAN_SKIP_BOT") == "1"
    if BOT_GH.exists() and not skip_bot:
        return "automation-gh", str(BOT_GH), EXPECTED_ACTOR
    if skip_bot:
        return "active-gh-user", "gh", None

    failure = github_api_core.FailureDetail(
        cause="invalid_credentials",
        message="Automation GitHub helper is unavailable and active auth was not explicitly authorized",
        retryable=False,
        fallback_eligible=True,
        disposition="requires_authorization",
        write_outcome="not_started",
        failed_step="auth_selection",
    )
    result = github_api_core.ApiResult(
        ok=False,
        status=0,
        body=None,
        operation=CURRENT_OPERATION,
        expected_actor=EXPECTED_ACTOR,
        host=github_api_core.DEFAULT_HOST,
        transport="rest_api",
        bucket="rest_core",
        failure=failure,
        failed_step="auth_selection",
    )
    raise PlanError(failure.message, failure=failure, api_result=result.as_dict())


def run_raw(
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    prefer_active: bool = False,
    recoverable: bool = False,
    operation: str | None = None,
    is_write: bool | None = None,
    bucket: str | None = None,
    graphql_operation: github_api_core.GraphQLOperation | None = None,
    completed_steps: list[str] | None = None,
    failed_step: str = "gh_invocation",
) -> tuple[str, str, str]:
    """Run gh through the configured actor without implicit identity fallback."""
    tried: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    skip_bot = os.environ.get("GH_PLAN_SKIP_BOT") == "1"
    bot_enabled = BOT_GH.exists() and not skip_bot
    active_first = prefer_active and os.environ.get("GH_PLAN_ALLOW_ACTIVE_FIRST") == "1"
    commands: list[tuple[str, list[str]]] = []
    if active_first:
        commands.append(("active-gh-user", ["gh", *args]))
    elif bot_enabled:
        commands.append(("automation-gh", [str(BOT_GH), *args]))
    elif skip_bot:
        commands.append(("active-gh-user", ["gh", *args]))
    else:
        inferred = github_api_core.infer_gh_command_context(args, input_text=input_text)
        resolved_is_write = inferred.is_write if is_write is None else is_write
        failure = github_api_core.FailureDetail(
            cause="invalid_credentials",
            message="Automation GitHub helper is unavailable and active auth was not explicitly authorized",
            retryable=False,
            fallback_eligible=True,
            disposition="requires_authorization",
            write_outcome="not_started" if resolved_is_write else None,
            completed_steps=list(completed_steps or []),
            failed_step="auth_selection",
        )
        result = github_api_core.ApiResult(
            ok=False,
            status=0,
            body=None,
            operation=operation or CURRENT_OPERATION,
            expected_actor=EXPECTED_ACTOR,
            host=github_api_core.DEFAULT_HOST,
            transport=inferred.transport,
            bucket=bucket or inferred.bucket,
            graphql_operation=graphql_operation or inferred.graphql_operation,
            completed_steps=list(completed_steps or []),
            failure=failure,
            failed_step="auth_selection",
        )
        raise PlanError(failure.message, failure=failure, api_result=result.as_dict())

    for actor, command in commands:
        proc = subprocess.run(
            command,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tried.append((actor, proc))
        if proc.returncode == 0:
            return actor, proc.stdout, proc.stderr

    actor, proc = tried[-1]
    if check:
        detail = "\n".join(
            f"[{name}] exit={p.returncode}\n{p.stderr.strip()}" for name, p in tried if p.stderr.strip()
        )
        inferred = github_api_core.infer_gh_command_context(args, input_text=input_text)
        resolved_is_write = inferred.is_write if is_write is None else is_write
        resolved_bucket = bucket or inferred.bucket
        resolved_graphql_operation = graphql_operation or inferred.graphql_operation
        result = github_api_core.legacy_process_result(
            proc.returncode,
            proc.stdout,
            proc.stderr,
            operation=operation or CURRENT_OPERATION,
            is_write=resolved_is_write,
            actor=actor,
            expected_actor=EXPECTED_ACTOR,
            transport=inferred.transport,
            bucket=resolved_bucket,
            graphql_operation=resolved_graphql_operation,
            completed_steps=completed_steps,
            failed_step=failed_step,
        )
        if result.failure and result.failure.cause == "rate_limited_unknown_bucket":
            probe = github_api_core.rate_limit_probe(
                gh_cmd=commands[-1][1][0],
                expected_actor=EXPECTED_ACTOR,
            )
            result = github_api_core.legacy_process_result(
                proc.returncode,
                proc.stdout,
                proc.stderr,
                operation=operation or CURRENT_OPERATION,
                is_write=resolved_is_write,
                actor=actor,
                expected_actor=EXPECTED_ACTOR,
                transport=inferred.transport,
                bucket=resolved_bucket,
                graphql_operation=resolved_graphql_operation,
                completed_steps=completed_steps,
                failed_step=failed_step,
                rate_limit_result=probe,
            )
        message = result.failure.message if result.failure else detail or proc.stdout or "gh command failed"
        raise PlanError(
            f"gh command failed: {message}",
            failure=result.failure,
            api_result=result.as_dict(),
        )
    return actor, proc.stdout, proc.stderr


PROJECT_CACHE: dict[tuple[Any, ...], Any] = {}
GRAPHQL_PREFLIGHT_MINIMUM = 25


def classify_project_error(message: str) -> str:
    lowered = message.lower()
    if "rate limit" in lowered or "graphql" in lowered or "secondary rate" in lowered:
        return "rate_limited"
    if (
        "resource not accessible" in lowered
        or "forbidden" in lowered
        or "permission denied" in lowered
        or "must have admin rights" in lowered
        or ("403" in lowered and "project" in lowered)
    ):
        return "project_auth_denied"
    if "not in project" in lowered:
        return "not_in_project"
    if "field not found" in lowered or "unknown option" in lowered or "no such field" in lowered:
        return "field_or_option_missing"
    if "could not resolve" in lowered or "not found" in lowered or "lookup" in lowered:
        return "lookup_stale"
    return "project_update_failed"


def project_error(
    message: str,
    *,
    retry_at: int | None = None,
    source: PlanError | None = None,
) -> ClassifiedPlanError:
    return ClassifiedPlanError(
        classify_project_error(message),
        message,
        retry_at=retry_at,
        failure=source.failure if source else None,
        api_result=source.api_result if source else None,
    )


def project_failure_payload(
    exc: PlanError,
    *,
    owner: str | None = None,
    project: str | None = None,
    operation: str | None = None,
    non_blocking: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": str(exc), "error_code": classify_project_error(str(exc))}
    if isinstance(exc, ClassifiedPlanError):
        payload["error_code"] = exc.code
        if exc.retry_at is not None:
            payload["retry_at"] = exc.retry_at
    if exc.api_result is not None:
        payload["api_result"] = exc.api_result
    if owner or project:
        payload["target"] = {"owner": owner, "project": project}
    if operation:
        payload["operation"] = operation
    if non_blocking:
        payload["warning"] = True
        payload["blocking"] = False
        payload["message"] = "Issue operation completed; Project reconciliation needs follow-up."
        if payload.get("error_code") == "lookup_stale":
            payload["recommended_action"] = (
                "Check planning.projects owner/default_project config, disable Projects, "
                "create the Project, or verify auth can see it."
            )
            payload["recommended_actions"] = [
                "Check planning.projects owner/default_project config.",
                "Create or rename the configured Project if it is intentionally missing.",
                "Verify the acting GitHub identity can see the Project.",
                "Disable Project sync if this repo or workspace should not use Projects.",
            ]
        elif payload.get("error_code") == "project_auth_denied":
            payload["recommended_action"] = (
                "Grant the automation identity access to the Project, use Project-capable "
                "auth for this operation, disable Project sync, or fix stale Project config."
            )
            payload["recommended_actions"] = [
                "Grant the automation identity access to the Project.",
                "Use Project-capable auth for this operation.",
                "Disable Project sync if this repo or workspace should not use Projects.",
                "Fix planning.projects owner/default_project config if it is stale.",
            ]
    return payload


def ensure_graphql_budget(
    *,
    prefer_active: bool = False,
    recoverable: bool = False,
    minimum: int = GRAPHQL_PREFLIGHT_MINIMUM,
) -> None:
    try:
        actor, data = gh_json(
            ["api", *API_VERSION_ARGS, "rate_limit"],
            prefer_active=prefer_active,
            recoverable=recoverable,
        )
    except PlanError as exc:
        if isinstance(exc, ClassifiedPlanError):
            raise
        if recoverable:
            raise project_error(f"rate_limit preflight failed: {exc}", source=exc) from exc
        raise
    del actor
    resources = data.get("resources") if isinstance(data, dict) else None
    graphql = resources.get("graphql") if isinstance(resources, dict) else None
    if not isinstance(graphql, dict):
        return
    remaining = graphql.get("remaining")
    reset = graphql.get("reset")
    if isinstance(remaining, int) and remaining < minimum:
        raise ClassifiedPlanError(
            "rate_limited",
            f"GraphQL quota too low for Project operation: remaining={remaining}, minimum={minimum}",
            retry_at=reset if isinstance(reset, int) else None,
        )


def gh_json(
    args: list[str],
    *,
    input_text: str | None = None,
    prefer_active: bool = False,
    recoverable: bool = False,
    operation: str | None = None,
    is_write: bool | None = None,
    bucket: str | None = None,
    graphql_operation: github_api_core.GraphQLOperation | None = None,
) -> tuple[str, Any]:
    actor, stdout, _ = run_raw(
        args,
        input_text=input_text,
        prefer_active=prefer_active,
        recoverable=recoverable,
        operation=operation,
        is_write=is_write,
        bucket=bucket,
        graphql_operation=graphql_operation,
    )
    if not stdout.strip():
        return actor, None
    try:
        return actor, json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise PlanError(f"Expected JSON from gh, got: {stdout[:300]}") from exc


def api_json(
    method: str,
    path: str,
    payload: Any = None,
    *,
    operation: str | None = None,
    is_write: bool | None = None,
    graphql_operation: github_api_core.GraphQLOperation | None = None,
    completed_steps: list[str] | None = None,
    failed_step: str | None = None,
) -> tuple[str, Any]:
    resolved_graphql_operation = graphql_operation
    if github_api_core.is_graphql_path(path) and resolved_graphql_operation is None:
        resolved_graphql_operation = github_api_core.infer_graphql_operation_type(payload)
    resolved_is_write = github_api_core.infer_is_write(
        method,
        path,
        payload,
        explicit_is_write=is_write,
        graphql_operation=resolved_graphql_operation,
    )
    bucket = "graphql" if github_api_core.is_graphql_path(path) else "rest_core"
    args = [
        "api",
        *API_VERSION_ARGS,
        "-X",
        method.upper(),
        path.lstrip("/"),
        "--include",
    ]
    input_text = None
    if payload is not None or method.upper() in ("POST", "PUT", "PATCH"):
        args.extend(["--input", "-"])
        input_text = json.dumps(payload if payload is not None else {})
    actor, stdout, _ = run_raw(
        args,
        input_text=input_text,
        operation=operation or CURRENT_OPERATION,
        is_write=resolved_is_write,
        bucket=bucket,
        graphql_operation=resolved_graphql_operation,
        failed_step=failed_step or "gh_invocation",
    )
    status, headers, body = github_api_core.parse_gh_include_output(stdout)
    if status == 0:
        status = 200
    rate_limit = github_api_core.RateLimitInfo.from_headers(headers)
    if not 200 <= status < 300 or (status == 200 and github_api_core._is_graphql_rate_limit_body(body)):
        failure = github_api_core.classify_error(status, headers, body, is_write=resolved_is_write)
        failure.completed_steps = completed_steps or []
        failure.failed_step = failed_step or f"http_{status}"
        result = github_api_core.ApiResult(
            ok=False,
            status=status,
            body=body,
            headers=headers,
            request_id=headers.get("x-github-request-id"),
            rate_limit=rate_limit if rate_limit.is_populated() else None,
            failure=failure,
            operation=operation or CURRENT_OPERATION,
            actor=actor,
            expected_actor=EXPECTED_ACTOR,
            host=github_api_core.DEFAULT_HOST,
            transport="graphql_api" if bucket == "graphql" else "rest_api",
            bucket=bucket,
            graphql_operation=resolved_graphql_operation,
            completed_steps=completed_steps or [],
            failed_step=failure.failed_step,
        )
        raise PlanError(failure.message, failure=failure, api_result=result.as_dict())
    return actor, body


def git_root(start: pathlib.Path | None = None) -> pathlib.Path | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start or pathlib.Path.cwd(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode == 0:
        return pathlib.Path(proc.stdout.strip())
    return None


def repo_from_git(start: pathlib.Path | None = None) -> str | None:
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=start or pathlib.Path.cwd(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?$", url)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return None


def default_repo(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    repo = repo_from_git()
    if repo:
        return repo
    actor, stdout, stderr = run_raw(["repo", "view", "--json", "nameWithOwner"], check=False)
    if not stdout.strip():
        raise PlanError("Could not resolve a GitHub repo; pass --repo OWNER/REPO")
    try:
        data = json.loads(stdout)
        return data["nameWithOwner"]
    except Exception as exc:  # pragma: no cover - defensive CLI UX
        raise PlanError("Could not resolve a GitHub repo; pass --repo OWNER/REPO") from exc


def deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def repo_config_path(repo: str | None) -> pathlib.Path | None:
    candidates: list[pathlib.Path] = []
    root = git_root()
    if root:
        candidates.append(root)
    if repo:
        repo_name = repo.split("/", 1)[1]
        candidates.append(pathlib.Path.home() / "Developer" / repo_name)
    for candidate in candidates:
        if not candidate.exists():
            continue
        if repo and repo_from_git(candidate) != repo:
            continue
        path = candidate / ".github/github.json"
        if path.exists():
            return path
    return None


def load_config(repo: str | None = None) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    workspace_config = workspace_config_path()
    if workspace_config.exists():
        config = deep_merge(config, json.loads(workspace_config.read_text()))
    repo_config = repo_config_path(repo)
    if repo_config and repo_config.exists():
        data = json.loads(repo_config.read_text())
        if isinstance(data.get("planning"), dict):
            config = deep_merge(config, data["planning"])
    return config


def labels(config: dict[str, Any], *keys: str) -> list[str]:
    return [config["labels"][key] for key in keys]


def manager_for_repo(config: dict[str, Any], repo: str) -> str | None:
    workflow = config.get("workflow") or {}
    repo_managers = workflow.get("repo_managers") or {}
    return resolve_manager_value(repo_managers.get(repo) or workflow.get("default_manager"))


def resolve_manager_value(value: Any) -> str | None:
    if not value:
        return None
    raw_value = str(value).strip()
    if not raw_value:
        return None
    if raw_value.casefold().startswith("person:"):
        return resolve_person_for_project(raw_value)
    return raw_value


def resolve_required_manager_value(value: Any) -> str | None:
    raw_value = str(value).strip() if value else ""
    return resolve_manager_value(raw_value)


def selected_manager_value(explicit_value: Any, config: dict[str, Any], repo: str) -> str | None:
    if explicit_value:
        return resolve_required_manager_value(explicit_value)
    return manager_for_repo(config, repo)


def resolve_person_for_project(value: str) -> str | None:
    if not PEOPLE_RESOLVER.exists():
        return None
    uv = shutil.which("uv")
    if not uv:
        return None
    try:
        command = [uv, "run", str(PEOPLE_RESOLVER), value, "--strict"]
        proc = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    match = payload.get("match") if isinstance(payload, dict) else None
    if not isinstance(match, dict):
        return None
    preferred = match.get("preferred_reference")
    if isinstance(preferred, str) and preferred.strip():
        return preferred.strip()
    display_name = match.get("display_name")
    if isinstance(display_name, str) and display_name.strip():
        return display_name.strip()
    return None


def normalize_labels(items: list[dict[str, Any]] | None) -> list[str]:
    return [item.get("name", "") for item in items or [] if item.get("name")]


def issue_ref(ref: str, repo: str) -> tuple[str, int]:
    ref = ref.strip()
    url = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", ref)
    if url:
        return url.group(1), int(url.group(2))
    full = re.fullmatch(r"([^/\s]+/[^#\s]+)#(\d+)", ref)
    if full:
        return full.group(1), int(full.group(2))
    if ref.startswith("#"):
        return repo, int(ref[1:])
    if ref.isdigit():
        return repo, int(ref)
    raise PlanError(f"Unsupported issue reference: {ref}")


def get_issue(ref: str, repo: str) -> tuple[str, dict[str, Any]]:
    issue_repo, number = issue_ref(ref, repo)
    actor, data = api_json("GET", f"/repos/{issue_repo}/issues/{number}", failed_step="get_issue")
    data["repo"] = issue_repo
    return actor, data


def issue_labels(issue: dict[str, Any]) -> list[str]:
    labels = issue.get("labels")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for item in labels:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def rest_create_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str],
    milestone: str | None = None,
) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    if milestone:
        payload["milestone"] = milestone
    actor, data = api_json(
        "POST",
        f"/repos/{repo}/issues",
        payload,
        failed_step="create_issue",
    )
    if not isinstance(data, dict):
        raise PlanError("gh api issue create returned no issue")
    data["repo"] = repo
    return actor, data


def rest_edit_issue(
    repo: str,
    number: int,
    *,
    body: str | None = None,
    title: str | None = None,
    labels: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {}
    if body is not None:
        payload["body"] = body
    if title is not None:
        payload["title"] = title
    if labels is not None:
        payload["labels"] = labels
    if not payload:
        raise PlanError("No issue fields to update")
    actor, data = api_json(
        "PATCH",
        f"/repos/{repo}/issues/{number}",
        payload,
        failed_step="edit_issue",
    )
    if not isinstance(data, dict):
        raise PlanError("gh api issue edit returned no issue")
    data["repo"] = repo
    return actor, data


def get_issue_compact(ref: str, repo: str) -> tuple[str, dict[str, Any]]:
    actor, issue = get_issue(ref, repo)
    return actor, compact_issue(issue)


def compact_issue(issue: dict[str, Any]) -> dict[str, Any]:
    milestone = issue.get("milestone") or {}
    deps = issue.get("issue_dependencies_summary") or {}
    sub = issue.get("sub_issues_summary") or {}
    repo = issue.get("repo") or (issue.get("repository") or {}).get("full_name")
    return {
        "repo": repo,
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "updated_at": issue.get("updated_at") or issue.get("updatedAt"),
        "url": issue.get("html_url") or issue.get("url"),
        "labels": normalize_labels(issue.get("labels")),
        "milestone": milestone.get("title") if isinstance(milestone, dict) else None,
        "dependencies": {
            "blocked_by": deps.get("blocked_by"),
            "blocking": deps.get("blocking"),
        },
        "sub_issues": {
            "total": sub.get("total"),
            "completed": sub.get("completed"),
            "percent_completed": sub.get("percent_completed"),
        },
    }


def section_map(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", body or ""))
    for idx, match in enumerate(matches):
        name = match.group(1).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        sections[name] = body[start:end].strip()
    return sections


def replace_section(body: str, section: str, new_text: str) -> str:
    body = body or ""
    pattern = re.compile(rf"(?ms)^##\s+{re.escape(section)}\s*\n.*?(?=^##\s+|\Z)")
    replacement = f"## {section}\n\n{new_text.strip()}\n\n"
    if pattern.search(body):
        return pattern.sub(replacement, body).rstrip() + "\n"
    if body and not body.endswith("\n"):
        body += "\n"
    return f"{body}\n{replacement}".lstrip()


def template_body(title: str) -> str:
    return f"""## Objective

{title}

## Finish Line

This work is done when the desired outcome is observable and the next re-entry
point is captured.

## Current Status

State: Active
Next action: Decide the first concrete implementation step.
Blocked by: None.
Last verified: Not yet verified.

## Scope

- In:
- Out:

## Acceptance Criteria

- [ ] Outcome is defined.
- [ ] Validation is captured.

## Relationships

- None yet.

## Validation

- Not defined yet.

## Decisions

- None yet.

## Open Questions

- None yet.
"""


def read_body(args: argparse.Namespace, fallback: str = "") -> str:
    if getattr(args, "body", None) is not None:
        return args.body
    body_file = getattr(args, "body_file", None)
    if body_file:
        if body_file == "-":
            return sys.stdin.read()
        return pathlib.Path(body_file).read_text()
    return fallback


def write_temp_body(body: str) -> pathlib.Path:
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(body)
        return pathlib.Path(tmp.name)


def relationship_line(rel: str, target: dict[str, Any]) -> str:
    target_repo = target["repo"]
    target_number = int(target["number"])
    return f"- {rel}: {target_repo}#{target_number} - {target.get('html_url') or target.get('url')}"


def add_relationship_note(body: str, rel: str, target: dict[str, Any]) -> str:
    line = relationship_line(rel, target)
    sections = section_map(body)
    current = sections.get("Relationships", "")
    if line in current:
        return body
    if not current or current.strip() in {"- None yet.", "None yet.", "None."}:
        new_text = line
    else:
        new_text = current.rstrip() + "\n" + line
    return replace_section(body, "Relationships", new_text)


def remove_relationship_note(body: str, rel: str, target: dict[str, Any]) -> str:
    line = relationship_line(rel, target)
    sections = section_map(body)
    current = sections.get("Relationships", "")
    kept = [item for item in current.splitlines() if item.strip() != line]
    new_text = "\n".join(kept).strip() or "- None yet."
    return replace_section(body, "Relationships", new_text)


def ensure_labels(
    repo: str,
    wanted: list[str],
    config: dict[str, Any],
    *,
    create_unknown: bool = True,
) -> tuple[str, list[str]]:
    actor, existing = gh_json(["label", "list", "-R", repo, "--json", "name", "--limit", "500"])
    existing_names = {item["name"] for item in existing or []}
    created: list[str] = []
    defs = config.get("label_defs", {})
    missing_without_defs: list[str] = []
    for name in wanted:
        if name in existing_names:
            continue
        info = defs.get(name)
        if info is None and not create_unknown:
            missing_without_defs.append(name)
            continue
        if info is None:
            info = {"color": "ededed", "description": "Planning label"}
        run_raw([
            "label",
            "create",
            name,
            "-R",
            repo,
            "--color",
            info.get("color", "ededed"),
            "--description",
            info.get("description", "Planning label"),
        ])
        created.append(name)
    if missing_without_defs:
        missing = ", ".join(sorted(missing_without_defs))
        raise PlanError(
            f"Refusing to create undocumented label(s): {missing}. "
            "Create them intentionally first, or add planning.label_defs metadata."
        )
    return actor, created


def cmd_index(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    label = args.label or config["labels"]["plan"]
    fields = "number,title,state,updatedAt,labels,milestone,url"
    actor, data = gh_json([
        "issue",
        "list",
        "-R",
        repo,
        "--state",
        args.state,
        "--limit",
        str(args.limit),
        "--label",
        label,
        "--json",
        fields,
    ])
    items = []
    for item in data or []:
        items.append({
            "repo": repo,
            "number": item["number"],
            "title": item["title"],
            "state": item["state"],
            "updated_at": item["updatedAt"],
            "url": item["url"],
            "labels": normalize_labels(item.get("labels")),
            "milestone": (item.get("milestone") or {}).get("title"),
        })
    emit({"ok": True, "actor": actor, "repo": repo, "count": len(items), "plans": items})


def cmd_search(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    query = args.query
    actor, data = gh_json([
        "issue",
        "list",
        "-R",
        repo,
        "--state",
        args.state,
        "--limit",
        str(args.limit),
        "--search",
        query,
        "--json",
        "number,title,state,updatedAt,labels,milestone,url",
    ])
    items = [{
        "repo": repo,
        "number": item["number"],
        "title": item["title"],
        "state": item["state"],
        "updated_at": item["updatedAt"],
        "url": item["url"],
        "labels": normalize_labels(item.get("labels")),
        "milestone": (item.get("milestone") or {}).get("title"),
    } for item in data or []]
    emit({"ok": True, "actor": actor, "repo": repo, "count": len(items), "issues": items})


def cmd_show(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    actor, issue = get_issue(args.issue, repo)
    body = issue.get("body") or ""
    result = compact_issue(issue)
    if args.full:
        result["body"] = body
    else:
        names = args.sections or config.get("default_sections") or []
        sections = section_map(body)
        result["sections"] = {name: sections.get(name, "") for name in names}
    emit({"ok": True, "actor": actor, "issue": result})


def cmd_create(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    title = (args.title_flag or args.title or "").strip()
    if not title:
        raise PlanError("Issue title is required (pass as positional argument or --title)")
    search_query = f'"{title}" in:title'
    _, matches = gh_json([
        "issue", "list", "-R", repo, "--state", "all", "--limit", "100",
        "--search", search_query, "--json", "number,title,state,url"
    ])
    exact = [item for item in matches or [] if item.get("title") == title]
    if exact and not args.force:
        emit({"ok": True, "deduped": True, "repo": repo, "existing": exact[0]})
        return

    base_labels = labels(config, "plan")
    if args.plan_status != "none":
        base_labels.extend(labels(config, args.plan_status))
    extra_labels = args.label or []
    wanted_labels = base_labels + extra_labels
    _, created_base_labels = ensure_labels(repo, base_labels, config)
    created_extra_labels: list[str] = []
    if extra_labels:
        _, created_extra_labels = ensure_labels(repo, extra_labels, config, create_unknown=False)
    created_labels = created_base_labels + created_extra_labels

    body = read_body(args, template_body(title))
    if args.finish_line:
        body = replace_section(body, "Finish Line", args.finish_line)
    project_config = config.get("projects") or {}
    project = args.project
    if project is None and project_config.get("enabled", True):
        project = project_config.get("default_project")
    actor, issue = rest_create_issue(
        repo,
        title,
        body,
        wanted_labels,
        args.milestone,
    )
    project_fields_set: dict[str, Any] = {}
    if project:
        try:
            owner = project_config.get("owner") or repo.split("/", 1)[0]
            _, number, _ = resolve_project(owner, project, recoverable=True)
            ensure_graphql_budget(prefer_active=True, recoverable=True)
            _, added_stdout, _ = run_raw(["project", "item-add", str(number), "--owner", owner, "--url", issue["html_url"], "--format", "json"], prefer_active=True, recoverable=True)
            added_item = json.loads(added_stdout) if added_stdout.strip() else {}
            added_item_id = added_item.get("id") if isinstance(added_item, dict) else None
            project_fields_set = set_project_fields(
                owner=owner,
                project_ref=project,
                issue_url=issue["html_url"],
                config=config,
                focus=args.focus,
                manager=selected_manager_value(args.manager, config, repo),
                finish_line=args.finish_line,
                item_id=added_item_id,
                recoverable=True,
            )
        except PlanError as exc:
            owner = project_config.get("owner") or repo.split("/", 1)[0]
            project_fields_set = project_failure_payload(
                exc,
                owner=owner,
                project=project,
                operation="create_project_sync",
                non_blocking=True,
            )
    emit({
        "ok": True,
        "actor": actor,
        "created_labels": created_labels,
        "project_fields": project_fields_set,
        "issue": compact_issue(issue),
    })


def cmd_update_section(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    issue_repo, number = issue_ref(args.issue, repo)
    _, issue = get_issue(args.issue, repo)
    body = issue.get("body") or ""
    new_text = read_body(args)
    updated = replace_section(body, args.section, new_text)
    actor, refreshed = rest_edit_issue(issue_repo, number, body=updated)
    emit({"ok": True, "actor": actor, "updated_section": args.section, "issue": compact_issue(refreshed)})


def cmd_link(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    actor_a, source = get_issue(args.issue, repo)
    _, target = get_issue(args.target, repo)
    source_repo = source["repo"]
    target_repo = target["repo"]
    source_number = int(source["number"])
    target_number = int(target["number"])
    rel = args.relationship

    if rel == "blocked-by":
        actor, _ = api_json(
            "POST",
            f"/repos/{source_repo}/issues/{source_number}/dependencies/blocked_by",
            {"issue_id": target["id"]},
            failed_step="link_blocked_by",
        )
    elif rel == "blocks":
        actor, _ = api_json(
            "POST",
            f"/repos/{target_repo}/issues/{target_number}/dependencies/blocked_by",
            {"issue_id": source["id"]},
            failed_step="link_blocks",
        )
    elif rel == "subissue":
        actor, _ = api_json(
            "POST",
            f"/repos/{source_repo}/issues/{source_number}/sub_issues",
            {"sub_issue_id": target["id"]},
            failed_step="link_subissue",
        )
    elif rel == "related":
        updated = add_relationship_note(source.get("body") or "", "related", target)
        actor, source = rest_edit_issue(source_repo, source_number, body=updated)
    else:
        raise PlanError(f"Unsupported relationship: {rel}")

    emit({
        "ok": True,
        "actor": actor,
        "relationship": rel,
        "source": compact_issue(source),
        "target": compact_issue(target),
    })


def cmd_unlink(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    _, source = get_issue(args.issue, repo)
    _, target = get_issue(args.target, repo)
    source_repo = source["repo"]
    target_repo = target["repo"]
    source_number = int(source["number"])
    target_number = int(target["number"])
    rel = args.relationship
    if rel == "blocked-by":
        actor, _ = api_json(
            "DELETE",
            f"/repos/{source_repo}/issues/{source_number}/dependencies/blocked_by/{target['id']}",
            failed_step="unlink_blocked_by",
        )
    elif rel == "blocks":
        actor, _ = api_json(
            "DELETE",
            f"/repos/{target_repo}/issues/{target_number}/dependencies/blocked_by/{source['id']}",
            failed_step="unlink_blocks",
        )
    elif rel == "subissue":
        actor, _ = api_json(
            "DELETE",
            f"/repos/{source_repo}/issues/{source_number}/sub_issue",
            {"sub_issue_id": target["id"]},
            failed_step="unlink_subissue",
        )
    elif rel == "related":
        updated = remove_relationship_note(source.get("body") or "", "related", target)
        actor, _ = rest_edit_issue(source_repo, source_number, body=updated)
    else:
        raise PlanError(f"Unlink supports blocked-by, blocks, subissue, and related; got {rel}")
    emit({"ok": True, "actor": actor, "relationship_removed": rel})


def cmd_deps(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    _, issue = get_issue(args.issue, repo)
    issue_repo = issue["repo"]
    number = int(issue["number"])
    result: dict[str, Any] = {"issue": compact_issue(issue)}
    for name, endpoint in [("blocked_by", "blocked_by"), ("blocking", "blocking")]:
        actor, data = api_json(
            "GET",
            f"/repos/{issue_repo}/issues/{number}/dependencies/{endpoint}",
            failed_step=f"read_{endpoint}",
        )
        result[name] = [compact_issue({**item, "repo": (item.get("repository") or {}).get("full_name")}) for item in data or []]
    actor, data = api_json(
        "GET",
        f"/repos/{issue_repo}/issues/{number}/sub_issues",
        failed_step="read_sub_issues",
    )
    result["sub_issues"] = [compact_issue({**item, "repo": (item.get("repository") or {}).get("full_name")}) for item in data or []]
    emit({"ok": True, "actor": actor, **result})


def resolve_project(owner: str, title_or_number: str, *, recoverable: bool = False) -> tuple[str, int, dict[str, Any] | None]:
    if title_or_number.isdigit():
        return "unknown", int(title_or_number), None
    cache_key = ("project", owner, title_or_number)
    if cache_key in PROJECT_CACHE:
        return PROJECT_CACHE[cache_key]
    ensure_graphql_budget(prefer_active=True, recoverable=recoverable)
    actor, data = gh_json(
        ["project", "list", "--owner", owner, "--format", "json", "--limit", "100"],
        prefer_active=True,
        recoverable=recoverable,
    )
    projects = data.get("projects", data) if isinstance(data, dict) else data
    for item in projects or []:
        if item.get("title") == title_or_number:
            result = (actor, int(item.get("number")), item)
            PROJECT_CACHE[cache_key] = result
            PROJECT_CACHE[("project", owner, str(result[1]))] = result
            return result
    raise project_error(f"Project not found for {owner}: {title_or_number}")


def project_meta(owner: str, title_or_number: str, *, recoverable: bool = False) -> tuple[str, int, dict[str, Any]]:
    actor, number, project_data = resolve_project(owner, title_or_number, recoverable=recoverable)
    if project_data:
        return actor, number, project_data
    cache_key = ("project", owner, str(number))
    if cache_key in PROJECT_CACHE and PROJECT_CACHE[cache_key][2]:
        return PROJECT_CACHE[cache_key]
    ensure_graphql_budget(prefer_active=True, recoverable=recoverable)
    actor, data = gh_json(["project", "view", str(number), "--owner", owner, "--format", "json"], prefer_active=True, recoverable=recoverable)
    PROJECT_CACHE[cache_key] = (actor, number, data)
    return actor, number, data


def project_fields(owner: str, project_number: int, *, recoverable: bool = False) -> dict[str, dict[str, Any]]:
    cache_key = ("fields", owner, project_number)
    if cache_key in PROJECT_CACHE:
        return PROJECT_CACHE[cache_key]
    ensure_graphql_budget(prefer_active=True, recoverable=recoverable)
    _, data = gh_json(["project", "field-list", str(project_number), "--owner", owner, "--format", "json"], prefer_active=True, recoverable=recoverable)
    fields = {item["name"]: item for item in data.get("fields", [])}
    PROJECT_CACHE[cache_key] = fields
    return fields


def project_items(
    owner: str,
    project_number: int,
    *,
    query: str | None = None,
    limit: int = 1000,
    recoverable: bool = False,
) -> list[dict[str, Any]]:
    cache_key = ("items", owner, project_number, query, limit)
    if cache_key in PROJECT_CACHE:
        return PROJECT_CACHE[cache_key]
    ensure_graphql_budget(prefer_active=True, recoverable=recoverable)
    args = [
        "project",
        "item-list",
        str(project_number),
        "--owner",
        owner,
        "--format",
        "json",
        "--limit",
        str(limit),
    ]
    if query:
        args.extend(["--query", query])
    _, data = gh_json(args, prefer_active=True, recoverable=recoverable)
    items = data.get("items", [])
    PROJECT_CACHE[cache_key] = items
    return items


def find_project_item(
    owner: str,
    project_number: int,
    issue_url: str,
    *,
    item_id: str | None = None,
    recoverable: bool = False,
    attempts: int = 3,
) -> dict[str, Any]:
    if item_id:
        return {"id": item_id}
    issue_number = issue_url.rstrip("/").rsplit("/", 1)[-1]
    for attempt in range(attempts):
        try:
            items = project_items(
                owner,
                project_number,
                query=f"#{issue_number}",
                limit=50,
                recoverable=recoverable,
            )
        except PlanError:
            items = project_items(owner, project_number, recoverable=recoverable)
        for item in items:
            content = item.get("content") or {}
            if content.get("url") == issue_url:
                return item
        if items:
            for item in project_items(owner, project_number, recoverable=recoverable):
                content = item.get("content") or {}
                if content.get("url") == issue_url:
                    return item
        if attempt + 1 < attempts:
            for key in list(PROJECT_CACHE):
                if len(key) >= 3 and key[:3] == ("items", owner, project_number):
                    PROJECT_CACHE.pop(key, None)
            time.sleep(1)
    raise project_error(f"Issue is not in project {owner}/{project_number}: {issue_url}")


def set_project_field(
    *,
    owner: str,
    project: dict[str, Any],
    project_number: int,
    item: dict[str, Any],
    field: dict[str, Any],
    value: str,
    recoverable: bool = False,
) -> str:
    args = [
        "project",
        "item-edit",
        "--id",
        item["id"],
        "--project-id",
        project["id"],
        "--field-id",
        field["id"],
        "--format",
        "json",
    ]
    if field.get("type") == "ProjectV2SingleSelectField":
        option = next((opt for opt in field.get("options", []) if opt.get("name") == value), None)
        if not option:
            raise project_error(f"Unknown option for {field['name']}: {value}")
        args.extend(["--single-select-option-id", option["id"]])
    else:
        args.extend(["--text", value])
    try:
        actor, _, _ = run_raw(args, prefer_active=True, recoverable=recoverable)
        return actor
    except PlanError as exc:
        if isinstance(exc, ClassifiedPlanError):
            raise
        raise project_error(str(exc)) from exc


def clear_project_field(
    *,
    project: dict[str, Any],
    item: dict[str, Any],
    field: dict[str, Any],
    recoverable: bool = False,
) -> str:
    try:
        actor, _, _ = run_raw([
            "project",
            "item-edit",
            "--id",
            item["id"],
            "--project-id",
            project["id"],
            "--field-id",
            field["id"],
            "--clear",
            "--format",
            "json",
        ], prefer_active=True, recoverable=recoverable)
        return actor
    except PlanError as exc:
        if isinstance(exc, ClassifiedPlanError):
            raise
        raise project_error(str(exc)) from exc


def set_project_fields(
    *,
    owner: str,
    project_ref: str,
    issue_url: str,
    config: dict[str, Any],
    focus: str | None = None,
    manager: str | None = None,
    finish_line: str | None = None,
    item_id: str | None = None,
    recoverable: bool = False,
) -> dict[str, Any]:
    actor, project_number, project = project_meta(owner, project_ref, recoverable=recoverable)
    fields = project_fields(owner, project_number, recoverable=recoverable)
    item = find_project_item(owner, project_number, issue_url, item_id=item_id, recoverable=recoverable)
    field_names = config.get("project_fields") or {}
    updates = {
        field_names.get("focus", "Focus"): focus,
        field_names.get("manager", "Manager"): manager,
        field_names.get("finish_line", "Finish Line"): finish_line,
    }
    updated: dict[str, str] = {}
    for field_name, value in updates.items():
        if not value:
            continue
        field = fields.get(field_name)
        if not field:
            raise project_error(f"Project field not found: {field_name}")
        actor = set_project_field(owner=owner, project=project, project_number=project_number, item=item, field=field, value=value, recoverable=recoverable)
        updated[field_name] = value
    return {"actor": actor, "project": project.get("title"), "updated": updated}


def cmd_project_add(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    _, issue = get_issue(args.issue, repo)
    issue_repo = issue["repo"]
    owner = args.owner or (config.get("projects") or {}).get("owner") or issue_repo.split("/", 1)[0]
    project = args.project or (config.get("projects") or {}).get("default_project")
    if not project:
        raise PlanError("Pass --project or set planning.projects.default_project")
    _, number, project_data = resolve_project(owner, project, recoverable=True)
    ensure_graphql_budget(prefer_active=True, recoverable=True)
    actor, data = gh_json([
        "project", "item-add", str(number), "--owner", owner, "--url", issue["html_url"], "--format", "json"
    ], prefer_active=True, recoverable=True)
    emit({"ok": True, "actor": actor, "owner": owner, "project": project_data or {"number": number}, "item": data})


def cmd_project_set(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    _, issue = get_issue(args.issue, repo)
    project_config = config.get("projects") or {}
    owner = args.owner or project_config.get("owner") or issue["repo"].split("/", 1)[0]
    project = args.project or project_config.get("default_project")
    if not project:
        raise PlanError("Pass --project or set planning.projects.default_project")
    result = set_project_fields(
        owner=owner,
        project_ref=project,
        issue_url=issue["html_url"],
        config=config,
        focus=args.focus,
        manager=resolve_required_manager_value(args.manager),
        finish_line=args.finish_line,
        item_id=args.item_id,
        recoverable=True,
    )
    emit({"ok": True, **result})


def cmd_close(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    issue_repo, number = issue_ref(args.issue, repo)
    _, issue = get_issue(args.issue, repo)
    close_comment = read_body(args) if args.body is not None or args.body_file else ""
    plan_labels = config.get("labels") or {}
    edit_args = ["issue", "edit", str(number), "-R", issue_repo]
    if plan_labels.get("active"):
        edit_args.extend(["--remove-label", plan_labels["active"]])
    if plan_labels.get("done"):
        edit_args.extend(["--add-label", plan_labels["done"]])
    actor, _, _ = run_raw(edit_args)
    completed_steps = ["update_labels"]

    comment_result: dict[str, Any] = {}
    if close_comment:
        try:
            route_actor, gh_cmd, expected_actor = comment_route()
            comment_result = github_comment_core.comment(
                "issue",
                number,
                close_comment,
                repo=issue_repo,
                gh_cmd=gh_cmd,
                expected_actor=expected_actor,
                operation=CURRENT_OPERATION,
                completed_steps=completed_steps,
                failed_step="post_close_comment",
            )
            actor = comment_result.get("actor") or route_actor
            completed_steps = list(comment_result.get("completed_steps") or completed_steps)
        except github_comment_core.CommentError as exc:
            raise PlanError(
                str(exc),
                failure=exc.failure,
                api_result=exc.api_result,
                payload=exc.payload,
            ) from exc

    project_result: dict[str, Any] = {}
    project_config = config.get("projects") or {}
    project = args.project or project_config.get("default_project")
    owner = args.owner or project_config.get("owner") or issue_repo.split("/", 1)[0]
    if project:
        try:
            _, project_number, project_data = project_meta(owner, project, recoverable=True)
            fields = project_fields(owner, project_number, recoverable=True)
            item = find_project_item(owner, project_number, issue["html_url"], recoverable=True)
            updated: dict[str, str | None] = {}
            status_field = fields.get("Status")
            if status_field:
                set_project_field(
                    owner=owner,
                    project=project_data,
                    project_number=project_number,
                    item=item,
                    field=status_field,
                    value="Done",
                    recoverable=True,
                )
                updated["Status"] = "Done"
            focus_field = fields.get((config.get("project_fields") or {}).get("focus", "Focus"))
            if focus_field:
                clear_project_field(project=project_data, item=item, field=focus_field, recoverable=True)
                updated["Focus"] = None
            project_result = {"project": project_data.get("title"), "updated": updated}
        except PlanError as exc:
            project_result = project_failure_payload(
                exc,
                owner=owner,
                project=project,
                operation="close_project_sync",
                non_blocking=True,
            )
        else:
            completed_steps.append("sync_project")

    close_args = ["issue", "close", str(number), "-R", issue_repo, "--reason", args.reason]
    actor, _, _ = run_raw(
        close_args,
        failed_step="close_issue",
        completed_steps=completed_steps,
    )
    completed_steps.append("close_issue")

    public_comment = {key: value for key, value in comment_result.items() if key != "completed_steps"}
    expected_actor = (
        comment_result["expected_actor"]
        if "expected_actor" in comment_result
        else EXPECTED_ACTOR
    )

    emit({
        "ok": True,
        "actor": actor,
        "expected_actor": expected_actor,
        "closed": {"repo": issue_repo, "number": number, "reason": args.reason, "url": issue["html_url"]},
        "comment": public_comment or None,
        "project": project_result,
        "completed_steps": completed_steps,
    })


def cmd_project_list(args: argparse.Namespace) -> None:
    owner = args.owner
    ensure_graphql_budget(prefer_active=True, recoverable=True)
    cmd = ["project", "list", "--owner", owner, "--format", "json", "--limit", str(args.limit)]
    if args.closed:
        cmd.append("--closed")
    actor, data = gh_json(
        cmd,
        prefer_active=True,
        recoverable=True,
    )
    emit({"ok": True, "actor": actor, "owner": owner, "projects": data})


def cmd_ensure_labels(args: argparse.Namespace) -> None:
    repo = default_repo(args.repo)
    config = load_config(repo)
    wanted = list(config["labels"].values())
    actor, created = ensure_labels(repo, wanted, config)
    emit({"ok": True, "actor": actor, "repo": repo, "ensured": wanted, "created": created})


def build_parser() -> argparse.ArgumentParser:
    parser = github_api_core.TerminalArgumentParser(description="Compact GitHub issue planning helper")
    parser.add_argument("--repo", help="Default OWNER/REPO for issue refs")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=github_api_core.TerminalArgumentParser,
    )

    p = sub.add_parser("index", help="Compact plan issue index, no bodies")
    p.add_argument("--state", default="open", choices=["open", "closed", "all"])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--label")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", help="Compact issue search, no bodies")
    p.add_argument("query")
    p.add_argument("--state", default="all", choices=["open", "closed", "all"])
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="Show compact issue sections by default")
    p.add_argument("issue")
    p.add_argument("--full", action="store_true")
    p.add_argument("--sections", nargs="+")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("create", help="Create a durable plan issue")
    p.add_argument("title", nargs="?", help="Issue title (positional)")
    p.add_argument("--title", dest="title_flag", help="Issue title (flag)")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.add_argument("--label", action="append")
    p.add_argument("--milestone")
    p.add_argument("--project")
    p.add_argument("--force", action="store_true")
    p.add_argument("--plan-status", choices=["active", "blocked", "waiting", "stale", "done", "none"], default="active")
    p.add_argument("--focus", choices=["Now", "Next", "Waiting", "Later"])
    p.add_argument("--manager")
    p.add_argument("--finish-line")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("update-section", help="Patch one markdown section")
    p.add_argument("issue")
    p.add_argument("section")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.set_defaults(func=cmd_update_section)

    p = sub.add_parser("link", help="Create native issue relationships")
    p.add_argument("issue")
    p.add_argument("relationship", choices=["blocked-by", "blocks", "subissue", "related"])
    p.add_argument("target")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("unlink", help="Remove native issue relationships")
    p.add_argument("issue")
    p.add_argument("relationship", choices=["blocked-by", "blocks", "subissue", "related"])
    p.add_argument("target")
    p.set_defaults(func=cmd_unlink)

    p = sub.add_parser("deps", help="Show dependencies and sub-issues")
    p.add_argument("issue")
    p.set_defaults(func=cmd_deps)

    p = sub.add_parser("close", help="Close a completed plan and update Project state")
    p.add_argument("issue")
    p.add_argument("--reason", default="completed")
    p.add_argument("--comment", dest="body")
    p.add_argument("--comment-file", dest="body_file")
    p.add_argument("--owner")
    p.add_argument("--project")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("project-add", help="Add issue to a personal/org Project")
    p.add_argument("issue")
    p.add_argument("--owner")
    p.add_argument("--project")
    p.set_defaults(func=cmd_project_add)

    p = sub.add_parser("project-set", help="Set human workflow Project fields")
    p.add_argument("issue")
    p.add_argument("--owner")
    p.add_argument("--project")
    p.add_argument("--focus", choices=["Now", "Next", "Waiting", "Later"])
    p.add_argument("--manager")
    p.add_argument("--finish-line")
    p.add_argument("--item-id", help="Project item id returned by project-add; avoids rediscovery")
    p.set_defaults(func=cmd_project_set)

    p = sub.add_parser("project-list", help="List Projects for an owner")
    p.add_argument("--owner", required=True)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--closed", action="store_true")
    p.set_defaults(func=cmd_project_list)

    p = sub.add_parser("ensure-labels", help="Create fixed planning labels when missing")
    p.set_defaults(func=cmd_ensure_labels)

    return parser


def main() -> None:
    global CURRENT_OPERATION, CURRENT_TRANSPORT, CURRENT_BUCKET, CURRENT_IS_WRITE
    parser = build_parser()
    try:
        args = parser.parse_args()
    except github_api_core.ArgumentParsingError as exc:
        command = github_api_core.requested_subcommand(sys.argv[1:], set(PLAN_COMMAND_CONTEXT))
        CURRENT_OPERATION = f"github.plan.{command.replace('-', '_')}"
        CURRENT_TRANSPORT, CURRENT_BUCKET, CURRENT_IS_WRITE = PLAN_COMMAND_CONTEXT.get(
            command,
            ("helper", "unknown", False),
        )
        failure = github_api_core.FailureDetail(
            cause="validation_error",
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="not_started" if CURRENT_IS_WRITE else None,
            failed_step="argument_parsing",
        )
        die(
            str(exc),
            code=2,
            error_code="validation_error",
            failure=failure,
        )
    CURRENT_OPERATION = f"github.plan.{args.command.replace('-', '_')}"
    CURRENT_TRANSPORT, CURRENT_BUCKET, CURRENT_IS_WRITE = PLAN_COMMAND_CONTEXT.get(
        args.command,
        ("helper", "unknown", False),
    )
    try:
        args.func(args)
    except ClassifiedPlanError as exc:
        die(
            str(exc),
            error_code=exc.code,
            retry_at=exc.retry_at,
            failure=exc.failure,
            api_result=exc.api_result,
            extra_payload=exc.payload,
        )
    except PlanError as exc:
        error_code = exc.failure.cause if exc.failure else classify_project_error(str(exc))
        die(
            str(exc),
            error_code=error_code,
            failure=exc.failure,
            api_result=exc.api_result,
            extra_payload=exc.payload,
        )
    except KeyboardInterrupt:
        failure = github_api_core.FailureDetail(
            cause="cancelled",
            message="Interrupted",
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome="unknown" if CURRENT_IS_WRITE else None,
        )
        die("Interrupted", code=130, error_code="cancelled", failure=failure)


if __name__ == "__main__":
    main()
