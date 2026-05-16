#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""JetBrains inspection helper for Codex skills.

The helper talks to the JetBrains Inspection API plugin over HTTP. It keeps the
LLM-facing workflow deterministic: resolve a route, trigger inspection, wait for
completion, fetch results, and classify the outcome for readiness.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_PORT_RANGE = range(63340, 63350)
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_WAIT_TIMEOUT_MS = 120_000
DEFAULT_POLL_MS = 1_000
READY_STATUS_VALUES = {"clean", "results_available"}


class InspectError(Exception):
    def __init__(self, message: str, exit_code: int = 2, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.payload = payload or {}


@dataclass
class HttpResult:
    status: int
    body: dict[str, Any]
    url: str


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "list":
            result = command_list(args)
            return emit(result, args.json, 0)
        if args.command == "route":
            context = build_context(args)
            result = command_route(args, context)
            return emit(result, args.json, 0)
        if args.command == "trigger":
            context = build_context(args)
            result = command_trigger(args, context)
            return emit(result, args.json, 0)
        if args.command == "wait":
            context = build_context(args)
            result = command_wait(args, context)
            return emit(result, args.json, classify_wait_exit(result))
        if args.command == "status":
            context = build_context(args)
            result = command_status(args, context)
            return emit(result, args.json, classify_status_exit(result))
        if args.command == "problems":
            context = build_context(args)
            result = command_problems(args, context)
            return emit(result, args.json, classify_problems_exit(result))
        if args.command == "run":
            context = build_context(args)
            result = command_run(args, context)
            return emit(result, args.json, classify_run_exit(result))
    except InspectError as error:
        payload = {"status": "error", "error": str(error), **error.payload}
        return emit(payload, getattr(args, "json", False), error.exit_code)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run JetBrains IDE inspections through the local plugin API.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON only.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_common(subparsers.add_parser("list", help="List discovered IDE projects."), include_scope=False)
    add_common(subparsers.add_parser("route", help="Resolve the target IDE/project route."), include_scope=False)
    add_common(subparsers.add_parser("trigger", help="Trigger an inspection."), include_scope=True)
    add_common(subparsers.add_parser("wait", help="Wait for a triggered inspection."), include_scope=False)
    add_common(subparsers.add_parser("status", help="Fetch current inspection status."), include_scope=False)
    add_common(subparsers.add_parser("problems", help="Fetch inspection problems."), include_scope=False)
    add_common(subparsers.add_parser("run", help="Resolve, trigger, wait, and fetch problems."), include_scope=True)

    for name in ("wait", "run"):
        subparsers.choices[name].add_argument("--timeout-ms", type=int, default=DEFAULT_WAIT_TIMEOUT_MS)
        subparsers.choices[name].add_argument("--poll-ms", type=int, default=DEFAULT_POLL_MS)
    subparsers.choices["problems"].add_argument("--scope", help="Problem scope filter. Defaults from repo config or changed_files.")
    for name in ("problems", "run"):
        subparsers.choices[name].add_argument("--severity", default="all")
        subparsers.choices[name].add_argument("--problem-type", default="all")
        subparsers.choices[name].add_argument("--file-pattern", default="all")
        subparsers.choices[name].add_argument("--limit", type=int, default=100)
        subparsers.choices[name].add_argument("--offset", type=int, default=0)
    return parser


def add_common(command: argparse.ArgumentParser, include_scope: bool) -> None:
    command.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit machine-readable JSON only.")
    command.add_argument("--repo", default=".", help="Repo/worktree path to inspect. Defaults to cwd.")
    command.add_argument("--port", type=int, help="Use a specific IDE built-in server port.")
    command.add_argument("--ide", help="Preferred IDE selector, e.g. PyCharm, IntelliJ, WebStorm.")
    command.add_argument("--project-key", help="Stable project key returned by route/list.")
    command.add_argument("--project-path", help="Project root/path selector.")
    command.add_argument("--worktree-path", help="Worktree path selector.")
    command.add_argument("--cwd", help="Cwd selector passed to the route API.")
    command.add_argument("--project", help="Project name selector. Prefer project-key/path when possible.")
    command.add_argument("--session-id", help="Expected IDE session id for drift detection.")
    command.add_argument("--open", action="store_true", help="Open the repo in the preferred IDE if no route is available, then retry briefly.")
    command.add_argument("--no-worktree-check", action="store_true", help="Allow routes outside the current worktree.")
    if include_scope:
        command.add_argument("--scope", help="Inspection scope. Defaults from repo config or changed_files.")
        command.add_argument("--dir", dest="directory", help="Directory for directory scope.")
        command.add_argument("--file", dest="files", action="append", default=[], help="File for files scope; repeatable.")
        command.add_argument("--include-unversioned", action=argparse.BooleanOptionalAction, default=True)
        command.add_argument("--changed-files-mode", choices=("all", "staged", "unstaged"), default="all")
        command.add_argument("--max-files", type=int)
        command.add_argument("--profile", default="")


def build_context(args: argparse.Namespace) -> dict[str, Any]:
    repo_arg = Path(args.repo).expanduser()
    repo_path = repo_arg if repo_arg.is_absolute() else Path.cwd() / repo_arg
    repo_path = repo_path.resolve()
    worktree_root = git_root(repo_path) or repo_path
    main_worktree = git_common_worktree(worktree_root)
    config = read_repo_config(worktree_root)
    jetbrains = config.get("jetbrains", {}) if isinstance(config.get("jetbrains"), dict) else {}
    quality = config.get("qualityGate", {}) if isinstance(config.get("qualityGate"), dict) else {}
    inspection = quality.get("inspection", {}) if isinstance(quality.get("inspection"), dict) else {}

    main_config = jetbrains.get("mainWorktreePath") or jetbrains.get("main_worktree_path")
    if main_config:
        main_worktree = resolve_config_path(main_config, worktree_root)

    open_project_path = jetbrains.get("openProjectPath") or jetbrains.get("open_project_path")
    configured_project_path = resolve_config_path(open_project_path, worktree_root) if open_project_path else None

    ide = args.ide or inspection.get("ide") or jetbrains.get("ide")
    scope = getattr(args, "scope", None) or first_scope(inspection.get("scopePreference")) or first_scope(jetbrains.get("scopePreference")) or "changed_files"
    worktree_strategy = jetbrains.get("worktreeStrategy") or jetbrains.get("worktree_strategy") or "prefer-current"

    return {
        "repo_path": str(repo_path),
        "worktree_root": str(worktree_root),
        "main_worktree": str(main_worktree) if main_worktree else None,
        "project_path": str(configured_project_path or worktree_root),
        "ide": ide,
        "scope": scope,
        "worktree_strategy": worktree_strategy,
        "config_path": str(worktree_root / ".github" / "github.json") if (worktree_root / ".github" / "github.json").exists() else None,
    }


def command_list(args: argparse.Namespace) -> dict[str, Any]:
    identities = discover_identities(args.port)
    projects: list[dict[str, Any]] = []
    for identity in identities:
        for project in identity.get("open_projects", []) or []:
            projects.append(flatten_project(identity, project))
    return {"status": "ok", "mode": "http", "projects": projects, "count": len(projects)}


def command_route(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    route = resolve_route(args, context)
    return {"status": "resolved", "context": context, "route": route}


def command_trigger(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    route = resolve_route(args, context)
    body = call_endpoint(route, "trigger", trigger_params(args, context, route))
    return {"status": body.get("status", "triggered"), "context": context, "route": body.get("route") or route, "trigger": body}


def command_wait(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    route = resolve_route(args, context)
    timeout_ms = getattr(args, "timeout_ms", DEFAULT_WAIT_TIMEOUT_MS)
    body = call_endpoint(route, "wait", route_params(args, context, route) | {
        "timeout_ms": timeout_ms,
        "poll_ms": getattr(args, "poll_ms", DEFAULT_POLL_MS),
    }, timeout=wait_http_timeout(timeout_ms))
    return {"status": body.get("completion_reason") or body.get("status", "unknown"), "context": context, "route": body.get("route") or route, "wait": body}


def command_status(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    route = resolve_route(args, context)
    body = call_endpoint(route, "status", route_params(args, context, route))
    status = status_label(body)
    return {
        "status": status,
        "clean": classify_status_body_clean(body),
        "context": context,
        "route": body.get("route") or route,
        "is_scanning": body.get("is_scanning", False),
        "has_inspection_results": body.get("has_inspection_results", False),
        "clean_inspection": body.get("clean_inspection", False),
        "capture_incomplete": body.get("capture_incomplete", False),
        "results_may_be_stale": body.get("results_may_be_stale", False),
        "timed_out": body.get("timed_out", False),
        "raw": body,
    }


def command_problems(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    route = resolve_route(args, context)
    body = call_endpoint(route, "problems", problems_params(args, context, route))
    return summarize_problems(context, body.get("route") or route, body)


def command_run(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    route = resolve_route(args, context)
    trigger = call_endpoint(route, "trigger", trigger_params(args, context, route))
    active_route = trigger.get("route") or route
    timeout_ms = getattr(args, "timeout_ms", DEFAULT_WAIT_TIMEOUT_MS)
    wait = call_endpoint(active_route, "wait", route_params(args, context, active_route) | {
        "timeout_ms": timeout_ms,
        "poll_ms": getattr(args, "poll_ms", DEFAULT_POLL_MS),
    }, timeout=wait_http_timeout(timeout_ms))
    problems = call_endpoint(active_route, "problems", problems_params(args, context, active_route))
    summary = summarize_problems(context, problems.get("route") or active_route, problems)
    summary["trigger"] = trigger
    summary["wait"] = wait
    summary["status"] = classify_run_status(wait, problems)
    return summary


def resolve_route(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    attempted_open = False
    while True:
        identities = discover_identities(args.port)
        if not identities and args.open and not attempted_open:
            attempted_open = True
            open_in_ide(context)
            time.sleep(4)
            continue
        if not identities:
            raise InspectError("No JetBrains inspection plugin instances discovered.", 3, {"hint": "Open the repo in the preferred JetBrains IDE with the inspection plugin installed."})

        candidates = []
        for identity in identities:
            port = identity.get("port")
            if not port:
                continue
            params = selector_params(args, context)
            try:
                result = http_get(int(port), "route", params)
                if result.status == 200 and result.body.get("route"):
                    candidates.append(result.body["route"])
            except InspectError:
                continue
        if not candidates and args.open and not attempted_open:
            attempted_open = True
            open_in_ide(context)
            time.sleep(4)
            continue
        if not candidates:
            raise InspectError("No open JetBrains project matched this repo/worktree.", 3, {"selector": selector_params(args, context)})
        route = sorted(candidates, key=lambda item: item.get("score", 0), reverse=True)[0]
        ensure_worktree_safe(route, context, args)
        return route


def discover_identities(port: int | None) -> list[dict[str, Any]]:
    if port:
        return [identity_for_port(port)]
    identities = registry_identities()
    if identities:
        return identities
    found = []
    for candidate_port in configured_ports():
        try:
            found.append(identity_for_port(candidate_port))
        except InspectError:
            pass
    return found


def registry_identities() -> list[dict[str, Any]]:
    instances = registry_dir()
    if not instances.exists():
        return []
    now_ms = int(time.time() * 1000)
    identities = []
    for path in sorted(instances.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        heartbeat = int(data.get("heartbeat_ms") or 0)
        if heartbeat and now_ms - heartbeat > 60_000:
            continue
        pid = data.get("pid")
        if pid and not pid_alive(int(pid)):
            continue
        identities.append(data)
    return identities


def identity_for_port(port: int) -> dict[str, Any]:
    result = http_get(port, "identity", {})
    body = result.body
    if "port" not in body or not body.get("port"):
        body["port"] = port
    return body


def http_get(port: int, endpoint: str, params: dict[str, Any], timeout: float = DEFAULT_TIMEOUT_SECONDS) -> HttpResult:
    clean_params = {key: str(value) for key, value in params.items() if value is not None and value != ""}
    query = urllib.parse.urlencode(clean_params, doseq=True)
    url = f"http://localhost:{port}/api/inspection/{endpoint}"
    if query:
        url = f"{url}?{query}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResult(response.status, parse_json(response.read()), url)
    except urllib.error.HTTPError as error:
        body = parse_json(error.read())
        if error.code == 409 and body.get("session_drift"):
            raise InspectError("IDE session changed; resolve route and re-trigger before trusting results.", 4, body)
        if error.code == 400:
            raise InspectError(body.get("message") or body.get("error") or "Bad inspection request.", 3, body)
        raise InspectError(f"HTTP {error.code} from inspection API", 3, body)
    except (urllib.error.URLError, TimeoutError) as error:
        raise InspectError(f"Inspection API unavailable on port {port}: {error}", 3)


def call_endpoint(
    route: dict[str, Any],
    endpoint: str,
    params: dict[str, Any],
    timeout: float | None = None,
) -> dict[str, Any]:
    port = int(route.get("port") or route.get("ide", {}).get("port") or 0)
    if not port:
        base_url = route.get("base_url") or ""
        parsed = urllib.parse.urlparse(base_url)
        port = parsed.port or 0
    if not port:
        raise InspectError("Route did not include an IDE port.", 3, {"route": route})
    return http_get(port, endpoint, params, timeout=timeout or max(DEFAULT_TIMEOUT_SECONDS, 10.0)).body


def selector_params(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_key": args.project_key,
        "project_path": args.project_path or context.get("project_path"),
        "worktree_path": args.worktree_path or context.get("worktree_root"),
        "cwd": args.cwd or context.get("worktree_root"),
        "project": args.project,
        "ide": args.ide or context.get("ide"),
        "session_id": args.session_id,
    }


def route_params(args: argparse.Namespace, context: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_key": args.project_key or route.get("project_key"),
        "session_id": args.session_id or route.get("session_id"),
        "project_path": args.project_path,
        "worktree_path": args.worktree_path,
        "cwd": args.cwd,
        "project": args.project,
        "ide": args.ide or context.get("ide"),
    }


def trigger_params(args: argparse.Namespace, context: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    params = route_params(args, context, route)
    params.update({
        "scope": getattr(args, "scope", None) or context.get("scope") or "changed_files",
        "include_unversioned": str(getattr(args, "include_unversioned", True)).lower(),
        "changed_files_mode": getattr(args, "changed_files_mode", "all"),
        "profile": getattr(args, "profile", ""),
    })
    directory = getattr(args, "directory", None)
    if directory:
        params["dir"] = directory
    files = getattr(args, "files", []) or []
    if files:
        params["files"] = "\n".join(files)
    max_files = getattr(args, "max_files", None)
    if max_files:
        params["max_files"] = max_files
    return params


def problems_params(args: argparse.Namespace, context: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    params = route_params(args, context, route)
    params.update({
        "scope": getattr(args, "scope", None) or context.get("scope") or "whole_project",
        "severity": getattr(args, "severity", "all"),
        "problem_type": getattr(args, "problem_type", "all"),
        "file_pattern": getattr(args, "file_pattern", "all"),
        "limit": getattr(args, "limit", 100),
        "offset": getattr(args, "offset", 0),
    })
    return params


def wait_http_timeout(timeout_ms: int) -> float:
    return max(DEFAULT_TIMEOUT_SECONDS, (timeout_ms / 1000.0) + 5.0)


def summarize_problems(context: dict[str, Any], route: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    problems = body.get("problems") or []
    status = body.get("status", "unknown")
    return {
        "status": status,
        "clean": status == "results_available" and len(problems) == 0 and not body.get("capture_incomplete") and not body.get("results_may_be_stale"),
        "context": context,
        "route": route,
        "total_problems": body.get("total_problems", len(problems)),
        "problems_shown": body.get("problems_shown", len(problems)),
        "capture_incomplete": body.get("capture_incomplete", False),
        "results_may_be_stale": body.get("results_may_be_stale", False),
        "problems": problems,
        "raw": body,
    }


def classify_run_status(wait: dict[str, Any], problems: dict[str, Any]) -> str:
    if wait.get("timed_out"):
        return "timed_out"
    if wait.get("capture_incomplete") or problems.get("capture_incomplete"):
        return "capture_incomplete"
    if wait.get("results_may_be_stale") or problems.get("results_may_be_stale") or problems.get("status") == "stale_results":
        return "stale_results"
    if problems.get("status") == "results_available" and (problems.get("problems") or []):
        return "findings"
    if problems.get("status") == "results_available":
        return "clean"
    return problems.get("status") or wait.get("completion_reason") or "unknown"


def classify_run_exit(result: dict[str, Any]) -> int:
    return 0 if result.get("status") == "clean" else 1


def classify_wait_exit(result: dict[str, Any]) -> int:
    wait = result.get("wait", {})
    if wait.get("timed_out") or wait.get("capture_incomplete") or wait.get("results_may_be_stale"):
        return 1
    return 0


def classify_problems_exit(result: dict[str, Any]) -> int:
    if result.get("status") != "results_available":
        return 1
    if result.get("capture_incomplete") or result.get("results_may_be_stale"):
        return 1
    return 1 if result.get("problems") else 0


def classify_status_body_clean(body: dict[str, Any]) -> bool:
    if body.get("session_drift") or body.get("ambiguous") or body.get("unavailable"):
        return False
    if body.get("timed_out") or body.get("capture_incomplete") or body.get("results_may_be_stale"):
        return False
    if body.get("is_scanning") or body.get("indexing") or body.get("inspection_in_progress"):
        return False
    status = str(body.get("status") or body.get("completion_reason") or "").lower()
    if status:
        return status in READY_STATUS_VALUES
    return body.get("clean_inspection") is True or body.get("has_inspection_results") is True


def status_label(body: dict[str, Any]) -> str:
    explicit_status = body.get("status") or body.get("completion_reason")
    if explicit_status:
        return str(explicit_status)
    if body.get("session_drift"):
        return "session_drift"
    if body.get("ambiguous"):
        return "ambiguous"
    if body.get("unavailable"):
        return "unavailable"
    if body.get("results_may_be_stale"):
        return "stale_results"
    if body.get("capture_incomplete"):
        return "capture_incomplete"
    if body.get("timed_out"):
        return "timed_out"
    if body.get("indexing"):
        return "indexing"
    if body.get("is_scanning") or body.get("inspection_in_progress"):
        return "running"
    if body.get("clean_inspection") is True:
        return "clean"
    if body.get("has_inspection_results") is True:
        return "results_available"
    return "unknown"


def classify_status_exit(result: dict[str, Any]) -> int:
    return 0 if result.get("clean") else 1


def emit(payload: dict[str, Any], json_only: bool, exit_code: int) -> int:
    if json_only:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return exit_code
    print_human(payload)
    return exit_code


def print_human(payload: dict[str, Any]) -> None:
    route = payload.get("route") or payload.get("trigger", {}).get("route") or {}
    if route:
        print(
            f"ROUTE: {route.get('ide', {}).get('name') or 'JetBrains IDE'} "
            f"project={route.get('project_name')} project_key={route.get('project_key')} "
            f"base_path={route.get('base_path')}"
        )
    status = payload.get("status")
    if status:
        print(f"STATUS: {status}")
    print_result_flags(payload)
    if "total_problems" in payload or "problems_shown" in payload:
        total = payload.get("total_problems", 0)
        shown = payload.get("problems_shown", len(payload.get("problems") or []))
        clean = payload.get("clean")
        print(f"SUMMARY: clean={clean} total_problems={total} problems_shown={shown}")
    problems = payload.get("problems") or []
    if problems:
        print("\nFINDINGS:")
        for problem in problems[:20]:
            location = problem.get("file") or "unknown"
            line = problem.get("line")
            if line:
                location = f"{location}:{line}"
            print(f"- [{problem.get('severity', 'unknown')}] {location} {problem.get('description', '')}")
    if not route and not status:
        print(json.dumps(payload, indent=2, sort_keys=True))


def print_result_flags(payload: dict[str, Any]) -> None:
    flags: list[str] = []
    if payload.get("capture_incomplete"):
        flags.append("capture_incomplete")
    if payload.get("results_may_be_stale"):
        flags.append("results_may_be_stale")
    wait = payload.get("wait") or {}
    if wait.get("timed_out"):
        flags.append("timed_out")
    if payload.get("timed_out"):
        flags.append("timed_out")
    if wait.get("capture_incomplete"):
        flags.append("wait_capture_incomplete")
    if wait.get("results_may_be_stale"):
        flags.append("wait_results_may_be_stale")
    if flags:
        print(f"FLAGS: {', '.join(flags)}")


def git_root(path: Path) -> Path | None:
    try:
        output = subprocess.check_output(["git", "-C", str(path), "rev-parse", "--show-toplevel"], text=True, stderr=subprocess.DEVNULL)
        return Path(output.strip()).resolve()
    except subprocess.CalledProcessError:
        return None


def git_common_worktree(path: Path) -> Path | None:
    try:
        common = subprocess.check_output(["git", "-C", str(path), "rev-parse", "--git-common-dir"], text=True, stderr=subprocess.DEVNULL).strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (path / common_path).resolve()
        if common_path.name == ".git":
            return common_path.parent.resolve()
        return None
    except subprocess.CalledProcessError:
        return None


def read_repo_config(worktree_root: Path) -> dict[str, Any]:
    config_path = worktree_root / ".github" / "github.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text())
    except json.JSONDecodeError as error:
        raise InspectError(f"Invalid JSON in {config_path}: {error}", 2)


def resolve_config_path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def first_scope(value: Any) -> str | None:
    if isinstance(value, list):
        return str(value[0]) if value else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def registry_dir() -> Path:
    override = os.environ.get("JETBRAINS_INSPECTION_REGISTRY_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "jetbrains-inspection-api" / "instances"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "jetbrains-inspection-api" / "instances"


def configured_ports() -> list[int]:
    raw = os.environ.get("JETBRAINS_INSPECTION_PORTS")
    if not raw:
        return list(DEFAULT_PORT_RANGE)
    ports: list[int] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "-" in item:
            start, end = item.split("-", 1)
            ports.extend(range(int(start), int(end) + 1))
        else:
            ports.append(int(item))
    return ports


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def flatten_project(identity: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": identity.get("session_id"),
        "port": identity.get("port"),
        "ide_name": identity.get("ide_name"),
        "ide_product_code": identity.get("ide_product_code"),
        "project_key": project.get("project_key"),
        "name": project.get("name"),
        "base_path": project.get("base_path"),
        "project_file_path": project.get("project_file_path"),
        "focused": bool(project.get("focused")),
    }


def ensure_worktree_safe(route: dict[str, Any], context: dict[str, Any], args: argparse.Namespace) -> None:
    if args.no_worktree_check:
        return
    strategy = str(context.get("worktree_strategy") or "prefer-current")
    if strategy in {"allow-main", "allow-any"}:
        return
    route_base = route.get("base_path")
    worktree_root = context.get("worktree_root")
    if not route_base or not worktree_root:
        return
    try:
        route_path = Path(route_base).resolve()
        worktree_path = Path(worktree_root).resolve()
    except OSError:
        return
    if route_path == worktree_path:
        return
    if worktree_path.is_relative_to(route_path) or route_path.is_relative_to(worktree_path):
        return
    raise InspectError(
        "Resolved JetBrains project is not the current worktree; refusing to inspect the wrong tree.",
        3,
        {"route_base_path": str(route_path), "worktree_root": str(worktree_path), "hint": "Open the current worktree in the preferred IDE or rerun with --no-worktree-check after approval."},
    )


def open_in_ide(context: dict[str, Any]) -> None:
    ide = context.get("ide")
    if not ide or sys.platform != "darwin":
        raise InspectError("Cannot auto-open IDE without a configured macOS IDE name.", 3)
    subprocess.run(["open", "-a", str(ide), str(context.get("project_path") or context.get("worktree_root"))], check=False)


def parse_json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise InspectError(f"Inspection API returned invalid JSON: {error}", 3)
    if not isinstance(value, dict):
        raise InspectError("Inspection API returned non-object JSON.", 3)
    return value


if __name__ == "__main__":
    sys.exit(main())
