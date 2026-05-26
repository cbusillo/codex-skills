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
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.platform != "win32":
    import fcntl
else:
    fcntl = None


DEFAULT_PORT_RANGE = range(63340, 63350)
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_WAIT_TIMEOUT_MS = 120_000
DEFAULT_POLL_MS = 1_000
DEFAULT_PREPARE_TIMEOUT_MS = 300_000
DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS = 300_000
READY_STATUS_VALUES = {"clean", "results_available"}
USABLE_STATUS_VALUES = READY_STATUS_VALUES | {"findings"}
REDACTED = "<redacted>"
SENSITIVE_KEY_PARTS = ("token", "secret", "password", "credential", "authorization")


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
        if args.command == "claim":
            context = build_context(args)
            result = command_claim(args, context)
            return emit(result, args.json, 0)
        if args.command == "prepare":
            context = build_context(args)
            result = command_prepare(args, context)
            return emit(result, args.json, classify_prepare_exit(result))
        if args.command == "closeout":
            context = build_context(args)
            result = command_closeout(args, context)
            return emit(result, args.json, classify_closeout_exit(result))
        if args.command == "cleanup-leases":
            result = command_cleanup_leases(args)
            return emit(result, args.json, 0)
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
    add_common(subparsers.add_parser("claim", help="Create a cheap local lifecycle lease without opening an IDE."), include_scope=False)
    add_common(subparsers.add_parser("prepare", help="Open and claim the exact worktree when needed."), include_scope=False)
    add_common(subparsers.add_parser("closeout", help="Prepare, inspect, and clean up helper-opened projects."), include_scope=True)
    subparsers.add_parser("cleanup-leases", help="Remove stale local helper leases.")
    add_common(subparsers.add_parser("run", help="Resolve, trigger, wait, and fetch problems."), include_scope=True)

    for name in ("wait", "run", "closeout"):
        subparsers.choices[name].add_argument("--timeout-ms", type=int, default=DEFAULT_WAIT_TIMEOUT_MS)
        subparsers.choices[name].add_argument("--poll-ms", type=int, default=DEFAULT_POLL_MS)
    for name in ("prepare", "run", "closeout"):
        subparsers.choices[name].set_defaults(open=True)
        subparsers.choices[name].add_argument("--no-open", dest="open", action="store_false", help="Do not open the IDE if the exact worktree is not already open.")
        subparsers.choices[name].add_argument("--background-open", dest="background_open", action="store_true", default=True, help="Ask macOS to open the IDE without activating it. Default for lifecycle opens.")
        subparsers.choices[name].add_argument("--foreground-open", dest="background_open", action="store_false", help="Allow the IDE to take focus while opening.")
        subparsers.choices[name].add_argument("--prepare-timeout-ms", type=int, default=DEFAULT_PREPARE_TIMEOUT_MS)
        subparsers.choices[name].add_argument("--lifecycle-lock-timeout-ms", type=int, default=DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS)
        subparsers.choices[name].add_argument("--keep-warm", action="store_true", help="Leave helper-opened projects open after closeout.")
    subparsers.choices["cleanup-leases"].add_argument("--max-age-ms", type=int, default=24 * 60 * 60 * 1000)
    subparsers.choices["cleanup-leases"].add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    subparsers.choices["problems"].add_argument("--scope", help="Problem scope filter. Defaults from repo config or changed_files.")
    for name in ("problems", "run", "closeout"):
        subparsers.choices[name].add_argument("--severity", default="all")
        subparsers.choices[name].add_argument("--problem-type", default="all")
        subparsers.choices[name].add_argument("--file-pattern", default="all")
        subparsers.choices[name].add_argument("--limit", type=int, default=100)
        subparsers.choices[name].add_argument("--offset", type=int, default=0)
        subparsers.choices[name].add_argument(
            "--include-stale",
            "--allow-stale",
            dest="include_stale",
            action="store_true",
            help="Return cached stale findings for diagnostics. Stale results still exit non-zero.",
        )
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
    return {"status": "resolved", "context": public_context(context), "route": route}


def command_trigger(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    route = resolve_route(args, context)
    body = call_endpoint(route, "trigger", trigger_params(args, context, route))
    return {"status": body.get("status", "triggered"), "context": public_context(context), "route": body.get("route") or route, "trigger": body}


def command_wait(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    route = resolve_route(args, context)
    timeout_ms = getattr(args, "timeout_ms", DEFAULT_WAIT_TIMEOUT_MS)
    body = call_endpoint(route, "wait", route_params(args, context, route) | {
        "timeout_ms": timeout_ms,
        "poll_ms": getattr(args, "poll_ms", DEFAULT_POLL_MS),
    }, timeout=wait_http_timeout(timeout_ms))
    return {"status": body.get("completion_reason") or body.get("status", "unknown"), "context": public_context(context), "route": body.get("route") or route, "wait": body}


def command_status(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    route = resolve_route(args, context)
    body = call_endpoint(route, "status", route_params(args, context, route))
    status = status_label(body)
    return {
        "status": status,
        "clean": classify_status_body_clean(body),
        "context": public_context(context),
        "route": body.get("route") or route,
        "is_scanning": body.get("is_scanning", False),
        "indexing": body.get("indexing", False),
        "inspection_in_progress": body.get("inspection_in_progress", False),
        "has_inspection_results": body.get("has_inspection_results", False),
        "clean_inspection": body.get("clean_inspection", False),
        "session_drift": body.get("session_drift", False),
        "ambiguous": body.get("ambiguous", False),
        "unavailable": body.get("unavailable", False),
        "capture_incomplete": body.get("capture_incomplete", False),
        "results_may_be_stale": body.get("results_may_be_stale", False),
        "timed_out": body.get("timed_out", False),
        "raw": body,
    }


def command_problems(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    route = resolve_route(args, context)
    body = call_endpoint(route, "problems", problems_params(args, context, route))
    if getattr(args, "include_stale", False):
        body.setdefault("include_stale", True)
    return summarize_problems(context, body.get("route") or route, body)


def command_claim(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    lease = create_local_lease(context, state="claimed")
    return {"status": "claimed", "context": public_context(context), "lease": public_lease(lease)}


def command_prepare(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    with lifecycle_lock(getattr(args, "lifecycle_lock_timeout_ms", DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS)):
        prepared = prepare_lifecycle(args, context)
    return prepared


def command_closeout(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    return run_prepared_inspection(args, context)


def command_run(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    return run_prepared_inspection(args, context)


def run_prepared_inspection(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    cleanup: dict[str, Any] = {"status": "not_needed"}
    with lifecycle_lock(getattr(args, "lifecycle_lock_timeout_ms", DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS)):
        prepared, lease, close_proof = prepare_lifecycle_details(args, context)
        try:
            result = run_inspection_on_route(args, context, prepared["route"])
        finally:
            if not getattr(args, "keep_warm", False):
                cleanup = cleanup_lifecycle(lease, prepared.get("route") or {}, close_proof)
        result["prepared"] = public_payload(prepared)
        result["cleanup"] = cleanup
        if cleanup.get("status") not in {"closed", "not_needed", "skipped"}:
            result["cleanup_failed"] = True
        if cleanup.get("cleanup_skipped"):
            result["cleanup_skipped"] = True
    return result


def run_inspection_on_route(args: argparse.Namespace, context: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    trigger = call_endpoint(route, "trigger", trigger_params(args, context, route))
    active_route = trigger.get("route") or route
    timeout_ms = getattr(args, "timeout_ms", DEFAULT_WAIT_TIMEOUT_MS)
    wait = call_endpoint(active_route, "wait", route_params(args, context, active_route) | {
        "timeout_ms": timeout_ms,
        "poll_ms": getattr(args, "poll_ms", DEFAULT_POLL_MS),
    }, timeout=wait_http_timeout(timeout_ms))
    problems = call_endpoint(active_route, "problems", problems_params(args, context, active_route))
    if getattr(args, "include_stale", False):
        problems.setdefault("include_stale", True)
    summary = summarize_problems(context, problems.get("route") or active_route, problems)
    summary["trigger"] = trigger
    summary["wait"] = wait
    summary["status"] = classify_run_status(wait, problems)
    return summary


def prepare_lifecycle(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    prepared, _, _ = prepare_lifecycle_details(args, context)
    return prepared


def prepare_lifecycle_details(args: argparse.Namespace, context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    lease = create_local_lease(context, state="preparing")
    exact_route = find_exact_route(args, context)
    opened_by_helper = False
    if exact_route is None:
        if not getattr(args, "open", False):
            raise InspectError(
                "Exact worktree is not open in a JetBrains IDE.",
                3,
                {"context": public_context(context), "lease": public_lease(lease)},
            )
        ensure_trusted_auto_open_root(context)
        ensure_jetbrains_trusted_locations(context)
        open_via_running_ide(args, context) or open_in_ide(context, background=getattr(args, "background_open", False))
        opened_by_helper = True
        exact_route = wait_for_exact_route(args, context, getattr(args, "prepare_timeout_ms", DEFAULT_PREPARE_TIMEOUT_MS))
    ensure_exact_worktree(exact_route, context, args)
    wait_until_route_ready(args, context, exact_route, getattr(args, "prepare_timeout_ms", DEFAULT_PREPARE_TIMEOUT_MS))
    claim_metadata, close_proof = claim_lifecycle(args, context, exact_route, lease)
    lease.update(
        {
            "state": "prepared",
            "opened_by_helper": opened_by_helper,
            "route": exact_route,
            "plugin_claim": claim_metadata,
            "project_instance_id": exact_route.get("project_instance_id"),
            "project_key": exact_route.get("project_key"),
            "session_id": exact_route.get("session_id"),
            "prepared_at_ms": now_ms(),
        }
    )
    write_lease(lease)
    prepared = {
        "status": "prepared",
        "context": public_context(context),
        "route": exact_route,
        "lease": public_lease(lease),
        "opened_by_helper": opened_by_helper,
        "claim": claim_metadata,
    }
    return prepared, lease, close_proof


def find_exact_route(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any] | None:
    probe_args = copy_args(args, open=False)
    try:
        route = resolve_route(probe_args, context)
        ensure_exact_worktree(route, context, args)
    except InspectError:
        return None
    return route


def wait_for_exact_route(args: argparse.Namespace, context: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    deadline = now_ms() + timeout_ms
    last_error: InspectError | None = None
    while now_ms() <= deadline:
        try:
            route = find_exact_route(args, context)
            if route is not None:
                return route
        except InspectError as error:
            last_error = error
        time.sleep(2)
    payload = auto_open_timeout_payload(args, context, timeout_ms)
    if last_error:
        payload["last_error"] = str(last_error)
    raise InspectError("Timed out waiting for JetBrains IDE to open the exact worktree.", 3, payload)


def auto_open_timeout_payload(args: argparse.Namespace, context: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    causes = [
        "JetBrains trust or safe-mode prompt is waiting for confirmation.",
        "The IDE is asking whether to open the project in a new window, current window, or attach mode.",
        "The configured macOS app name did not launch the IDE product that has this plugin installed.",
        "The inspection plugin is disabled, missing, or has not written its registry heartbeat yet.",
    ]
    return {
        "context": public_context(context),
        "ide": context.get("ide"),
        "worktree_root": context.get("worktree_root"),
        "global_config": str(global_config_path()),
        "background_open": getattr(args, "background_open", False),
        "prepare_timeout_ms": timeout_ms,
        "likely_causes": causes,
        "hint": "Run again with --foreground-open, trust the project if prompted, set JetBrains project opening to New Window, or open the worktree manually once.",
    }


def wait_until_route_ready(args: argparse.Namespace, context: dict[str, Any], route: dict[str, Any], timeout_ms: int) -> None:
    deadline = now_ms() + timeout_ms
    last_status: dict[str, Any] | None = None
    while now_ms() <= deadline:
        body = call_endpoint(route, "status", route_params(args, context, route))
        last_status = body
        if not body.get("indexing") and not body.get("is_scanning"):
            return
        time.sleep(max(DEFAULT_POLL_MS, 1_000) / 1000.0)
    raise InspectError("Timed out waiting for JetBrains indexing/scanning to settle.", 3, {"status": last_status or {}, "route": route})


def open_via_running_ide(args: argparse.Namespace, context: dict[str, Any]) -> bool:
    identities = discover_identities(args.port)
    matching = [identity for identity in identities if identity_matches_context(identity, context)]
    for identity in matching:
        port = identity.get("port")
        if not port:
            continue
        try:
            http_get(
                int(port),
                "lifecycle/open",
                {
                    "worktree_path": context.get("worktree_root"),
                    "project_path": context.get("project_path"),
                    "ide": context.get("ide"),
                    "session_id": identity.get("session_id"),
                },
                timeout=max(DEFAULT_TIMEOUT_SECONDS, 30.0),
            )
            return True
        except InspectError:
            continue
    return False


def identity_matches_context(identity: dict[str, Any], context: dict[str, Any]) -> bool:
    ide = str(context.get("ide") or "").lower().replace(" ", "")
    if not ide:
        return True
    values = [
        str(identity.get("ide_name") or "").lower().replace(" ", ""),
        str(identity.get("ide_product_code") or "").lower().replace(" ", ""),
        str(identity.get("product_code") or "").lower().replace(" ", ""),
    ]
    aliases = {
        "intellijidea": ("intellijidea", "idea", "iu", "ic"),
        "intellij": ("intellijidea", "idea", "iu", "ic"),
        "idea": ("intellijidea", "idea", "iu", "ic"),
        "pycharm": ("pycharm", "py", "pc"),
        "pycharmce": ("pycharm", "py", "pc"),
        "webstorm": ("webstorm", "ws"),
    }
    needles = aliases.get(ide, (ide,))
    return any(needle in value for needle in needles for value in values)


def claim_lifecycle(
    args: argparse.Namespace,
    context: dict[str, Any],
    route: dict[str, Any],
    lease: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    project_instance_id = route.get("project_instance_id")
    if not project_instance_id:
        raise InspectError(
            "Inspection plugin route does not include project_instance_id; install the updated plugin before lifecycle closeout.",
            3,
            {"route": route},
        )
    claim = call_endpoint(route, "lifecycle/claim", route_params(args, context, route) | {
        "project_instance_id": project_instance_id,
        "lease_id": lease.get("lease_id"),
    })
    close_proof = claim.pop("close_" + "token", None)
    claim_metadata = {
        "status": "claimed",
        "project_key": route.get("project_key"),
        "project_instance_id": project_instance_id,
        "session_id": route.get("session_id"),
        "lease_id": lease.get("lease_id"),
        "claimed_at_ms": now_ms(),
    }
    return claim_metadata, str(close_proof) if close_proof else None


def cleanup_lifecycle(lease: dict[str, Any], route: dict[str, Any], close_proof: str | None = None) -> dict[str, Any]:
    if not lease.get("opened_by_helper"):
        mark_lease_state(lease, "released")
        remove_lease(lease)
        return {"status": "not_needed", "reason": "project_preexisted"}
    project_instance_id = lease.get("project_instance_id")
    if not close_proof or not project_instance_id:
        mark_lease_state(lease, "cleanup_skipped")
        return {"status": "skipped", "cleanup_skipped": True, "reason": "missing_close_token"}
    try:
        close_params = {
            "project_key": lease.get("project_key") or route.get("project_key"),
            "project_path": route.get("base_path"),
            "worktree_path": route.get("base_path"),
            "session_id": lease.get("session_id") or route.get("session_id"),
            "project_instance_id": project_instance_id,
            "lease_id": lease.get("lease_id"),
        }
        close_params["close_" + "token"] = close_proof
        close_result = call_lifecycle_close(route, close_params)
    except InspectError as error:
        mark_lease_state(lease, "cleanup_failed")
        return {
            "status": "failed",
            "cleanup_failed": True,
            "reason": public_cleanup_reason(error),
        }
    status = str(close_result.get("status") or "")
    mark_lease_state(lease, "closed" if status == "closed" else "cleanup_skipped")
    if status == "closed":
        remove_lease(lease)
    return close_result


def call_lifecycle_close(route: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    port = route_port(route)
    private_http_get_body(port, "lifecycle/close", params)
    return {
        "status": "closed",
        "reason": None,
        "cleanup_skipped": False,
        "cleanup_failed": False,
    }


def public_cleanup_reason(error: InspectError) -> str:
    reason = error.payload.get("reason") or error.payload.get("status")
    if isinstance(reason, str) and reason:
        return reason
    return "close_failed"


def route_port(route: dict[str, Any]) -> int:
    port = int(route.get("port") or route.get("ide", {}).get("port") or 0)
    if port:
        return port
    base_url = route.get("base_url") or ""
    parsed = urllib.parse.urlparse(base_url)
    port = parsed.port or 0
    if not port:
        raise InspectError("Route did not include an IDE port.", 3, {"route": route})
    return port


def private_http_get_body(port: int, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    clean_params = {key: str(value) for key, value in params.items() if value is not None and value != ""}
    query = urllib.parse.urlencode(clean_params, doseq=True)
    base_url = f"http://localhost:{port}/api/inspection/{endpoint}"
    request_url = f"{base_url}?{query}" if query else base_url
    request = urllib.request.Request(request_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=max(DEFAULT_TIMEOUT_SECONDS, 10.0)) as response:
            return parse_json(response.read())
    except urllib.error.HTTPError as error:
        body = parse_json(error.read())
        if error.code == 409 and body.get("session_drift"):
            raise InspectError("IDE session changed; resolve route and re-trigger before trusting results.", 4, body)
        if error.code == 400:
            raise InspectError(body.get("message") or body.get("error") or "Bad inspection request.", 3, body)
        raise InspectError(f"HTTP {error.code} from inspection API", 3, body)
    except (urllib.error.URLError, TimeoutError) as error:
        raise InspectError(f"Inspection API unavailable on port {port}: {error}", 3)


def resolve_route(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    attempted_open = False
    while True:
        identities = discover_identities(args.port)
        if not identities and args.open and not attempted_open:
            attempted_open = True
            ensure_trusted_auto_open_root(context)
            ensure_jetbrains_trusted_locations(context)
            open_via_running_ide(args, context) or open_in_ide(context, background=getattr(args, "background_open", False))
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
            ensure_trusted_auto_open_root(context)
            ensure_jetbrains_trusted_locations(context)
            open_via_running_ide(args, context) or open_in_ide(context, background=getattr(args, "background_open", False))
            time.sleep(4)
            continue
        if not candidates:
            raise InspectError("No open JetBrains project matched this repo/worktree.", 3, {"selector": selector_params(args, context)})
        route = sorted(candidates, key=lambda item: route_sort_key(item, context), reverse=True)[0]
        ensure_worktree_safe(route, context, args)
        return route


def copy_args(args: argparse.Namespace, **overrides: Any) -> argparse.Namespace:
    values = vars(args).copy()
    values.update(overrides)
    return argparse.Namespace(**values)


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
    display_query = urllib.parse.urlencode(redact_payload(clean_params), doseq=True)
    base_url = f"http://localhost:{port}/api/inspection/{endpoint}"
    request_url = f"{base_url}?{query}" if query else base_url
    display_url = f"{base_url}?{display_query}" if display_query else base_url
    request = urllib.request.Request(request_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResult(response.status, parse_json(response.read()), display_url)
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
    port = route_port(route)
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
    params = {
        "project_key": args.project_key or route.get("project_key"),
        "session_id": args.session_id or route.get("session_id"),
        "project_path": args.project_path,
        "worktree_path": args.worktree_path,
        "cwd": args.cwd,
        "project": args.project,
        "ide": args.ide or context.get("ide"),
    }
    if route.get("project_instance_id"):
        params["project_instance_id"] = route.get("project_instance_id")
    return params


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
    if getattr(args, "include_stale", False):
        params["include_stale"] = "true"
    return params


def wait_http_timeout(timeout_ms: int) -> float:
    return max(DEFAULT_TIMEOUT_SECONDS, (timeout_ms / 1000.0) + 5.0)


def summarize_problems(context: dict[str, Any], route: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    problems = body.get("problems") or []
    status = body.get("status", "unknown")
    results_may_be_stale = body.get("results_may_be_stale", False) or status == "stale_results"
    summary: dict[str, Any] = {
        "status": status,
        "clean": status == "results_available" and len(problems) == 0 and not body.get("capture_incomplete") and not results_may_be_stale,
        "context": public_context(context),
        "route": route,
        "capture_incomplete": body.get("capture_incomplete", False),
        "results_may_be_stale": results_may_be_stale,
        "problems": problems,
        "raw": body,
    }
    if "total_problems" in body:
        summary["total_problems"] = body["total_problems"]
    elif not results_may_be_stale:
        summary["total_problems"] = len(problems)
    if "problems_shown" in body:
        summary["problems_shown"] = body["problems_shown"]
    elif not results_may_be_stale:
        summary["problems_shown"] = len(problems)
    for key in (
        "cached_total_problems",
        "cached_problems_shown",
        "include_stale",
        "snapshot_outcome",
        "snapshot_change_kind",
        "snapshot_run_id",
        "snapshot_trigger_time_ms",
        "results_source",
        "results_timestamp_ms",
        "stale_reasons",
        "capture_diagnostic",
    ):
        if key in body:
            summary[key] = body[key]
    return summary


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


def classify_prepare_exit(result: dict[str, Any]) -> int:
    return 0 if result.get("status") == "prepared" else 1


def classify_closeout_exit(result: dict[str, Any]) -> int:
    if result.get("cleanup_failed") or result.get("cleanup_skipped"):
        return 1
    return classify_run_exit(result)


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
    if (
        result.get("session_drift")
        or result.get("ambiguous")
        or result.get("unavailable")
        or result.get("capture_incomplete")
        or result.get("results_may_be_stale")
        or result.get("timed_out")
        or result.get("is_scanning")
        or result.get("indexing")
        or result.get("inspection_in_progress")
    ):
        return 1
    status = str(result.get("status") or "").lower()
    return 0 if status in USABLE_STATUS_VALUES else 1


def emit(payload: dict[str, Any], json_only: bool, exit_code: int) -> int:
    payload = public_payload(payload)
    if json_only:
        # codeql[py/clear-text-logging-sensitive-data]
        print(public_json(payload))
        return exit_code
    print_human(payload)
    return exit_code


def print_human(payload: dict[str, Any]) -> None:
    route = payload.get("route") or payload.get("trigger", {}).get("route") or {}
    if route:
        print(safe_text("ROUTE: {ide_name} project={project_name} project_key={project_key} base_path={base_path}", {
            "ide_name": route.get("ide", {}).get("name") or "JetBrains IDE",
            "project_name": route.get("project_name"),
            "project_key": route.get("project_key"),
            "base_path": route.get("base_path"),
        }))
    status = payload.get("status")
    if status:
        print(safe_text("STATUS: {status}", {"status": status}))
    print_result_flags(payload)
    if "total_problems" in payload or "problems_shown" in payload:
        total = payload.get("total_problems", 0)
        shown = payload.get("problems_shown", len(payload.get("problems") or []))
        clean = payload.get("clean")
        print(safe_text("SUMMARY: clean={clean} total_problems={total} problems_shown={shown}", {
            "clean": clean,
            "total": total,
            "shown": shown,
        }))
    if "cached_total_problems" in payload or "cached_problems_shown" in payload:
        total = payload.get("cached_total_problems", "unknown")
        shown = payload.get("cached_problems_shown", len(payload.get("problems") or []))
        print(safe_text("CACHED: total_problems={total} problems_shown={shown}", {"total": total, "shown": shown}))
    cleanup = payload.get("cleanup") or {}
    if cleanup:
        print(safe_text("CLEANUP: status={status} reason={reason}", {
            "status": cleanup.get("status"),
            "reason": cleanup.get("reason"),
        }))
    if payload.get("status") == "stale_results" and not payload.get("include_stale"):
        print("STALE: cached findings withheld; re-run inspection or pass --include-stale for diagnostics.")
    if payload.get("snapshot_change_kind"):
        print(safe_text("SNAPSHOT: change_kind={kind}", {"kind": payload["snapshot_change_kind"]}))
    print_capture_diagnostic(payload.get("capture_diagnostic"))
    wait = payload.get("wait") or {}
    if wait:
        print_capture_diagnostic(wait.get("capture_diagnostic"))
    problems = payload.get("problems") or []
    if problems:
        print("\nFINDINGS:")
        for problem in problems[:20]:
            location = problem.get("file") or "unknown"
            line = problem.get("line")
            if line:
                location = f"{location}:{line}"
            print(safe_text("- [{severity}] {location} {description}", {
                "severity": problem.get("severity", "unknown"),
                "location": location,
                "description": problem.get("description", ""),
            }))
    if not route and not status:
        # codeql[py/clear-text-logging-sensitive-data]
        print(public_json(payload))


def public_json(payload: dict[str, Any]) -> str:
    return json.dumps(public_payload(payload), indent=2, sort_keys=True)


def safe_text(template: str, values: dict[str, Any]) -> str:
    clean_values = {key: safe_scalar(value) for key, value in public_payload(values).items()}
    return template.format(**clean_values)


def safe_scalar(value: Any) -> str:
    if value is None:
        return "None"
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(public_payload({"value": value})["value"], sort_keys=True)


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): REDACTED if is_sensitive_key(str(key)) else redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return redact_payload(strip_private_fields(payload))


def public_context(context: dict[str, Any]) -> dict[str, Any]:
    public = dict(context)
    return public


def strip_private_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): strip_private_fields(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [strip_private_fields(item) for item in value]
    return value


def print_capture_diagnostic(diagnostic: Any) -> None:
    if not isinstance(diagnostic, dict) or not diagnostic:
        return
    parts: list[str] = []
    for key in (
        "exit_reason",
        "view_ready_ok",
        "observed_inspection_view",
        "inspection_view_updating",
        "observed_settled_empty_inspection_view",
        "observed_stable_readable_empty_inspection_view",
        "observed_stable_empty_results_without_inspection_view",
        "successful_extraction_count",
        "extraction_failure_count",
        "polling_elapsed_ms",
    ):
        if key in diagnostic:
            parts.append(f"{key}={diagnostic[key]}")
    if parts:
        # codeql[py/clear-text-logging-sensitive-data]
        print(f"CAPTURE_DIAGNOSTIC: {' '.join(parts)}")


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
    if payload.get("cleanup_failed"):
        flags.append("cleanup_failed")
    if payload.get("cleanup_skipped"):
        flags.append("cleanup_skipped")
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


def global_config_path() -> Path:
    override = os.environ.get("JETBRAINS_INSPECTION_GLOBAL_CONFIG")
    if override:
        return Path(override).expanduser()
    code_home = os.environ.get("CODEX_HOME") or os.environ.get("CODE_HOME") or str(Path.home() / ".code")
    return Path(code_home).expanduser() / "jetbrains-inspection.json"


def read_global_config() -> dict[str, Any]:
    path = global_config_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise InspectError(f"Invalid JSON in {path}: {error}", 2)
    if not isinstance(value, dict):
        raise InspectError(f"Global inspection config must be a JSON object: {path}", 2)
    return value


def trusted_auto_open_roots() -> list[str]:
    env = os.environ.get("JETBRAINS_INSPECTION_TRUSTED_AUTO_OPEN_ROOTS")
    raw_roots: Any = None
    if env:
        raw_roots = [part for part in env.split(os.pathsep) if part.strip()]
    else:
        config = read_global_config()
        jetbrains = config.get("jetbrains", {}) if isinstance(config.get("jetbrains"), dict) else {}
        raw_roots = (
            jetbrains.get("trustedAutoOpenRoots")
            or jetbrains.get("trusted_auto_open_roots")
            or config.get("trustedAutoOpenRoots")
            or config.get("trusted_auto_open_roots")
        )
    if not raw_roots:
        return []
    if isinstance(raw_roots, str):
        raw_roots = [raw_roots]
    roots: list[str] = []
    if isinstance(raw_roots, list):
        for root in raw_roots:
            try:
                roots.append(str(Path(str(root)).expanduser().resolve()))
            except OSError:
                continue
    return roots


def trusted_auto_open_root_count() -> int:
    return len(trusted_auto_open_roots())


def ensure_trusted_auto_open_root(context: dict[str, Any]) -> None:
    worktree = context.get("worktree_root")
    roots = trusted_auto_open_roots()
    if not worktree:
        raise InspectError("Cannot auto-open IDE because the worktree path is unknown.", 3)
    if not roots:
        raise InspectError(
            "Exact worktree is not open and no trusted auto-open roots are configured.",
            3,
            {
                "worktree_root": worktree,
                "global_config": str(global_config_path()),
                "hint": "Add jetbrains.trustedAutoOpenRoots to the global inspection config, or open/trust the worktree manually once.",
            },
        )
    worktree_path = Path(str(worktree)).expanduser().resolve()
    trusted = []
    for root in roots:
        root_path = Path(str(root)).expanduser().resolve()
        trusted.append(str(root_path))
        if worktree_path == root_path or worktree_path.is_relative_to(root_path):
            return
    raise InspectError(
        "Exact worktree is not open and is outside trusted auto-open roots.",
        3,
        {
            "worktree_root": str(worktree_path),
        "trusted_auto_open_root_count": len(trusted),
            "global_config": str(global_config_path()),
            "hint": "Move the worktree under a trusted root, add a trusted root globally, or open/trust the project manually once.",
        },
    )


def ensure_jetbrains_trusted_locations(context: dict[str, Any]) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"status": "skipped", "reason": "unsupported_platform"}
    worktree = context.get("worktree_root")
    if not worktree:
        raise InspectError("Cannot seed JetBrains trusted locations because the worktree path is unknown.", 3)
    trust_root = trusted_root_for_worktree(context)
    config_dirs = jetbrains_config_dirs(context)
    if not config_dirs:
        raise InspectError(
            "Cannot seed JetBrains trusted locations because no matching IDE config directory was found.",
            3,
            {
                "ide": context.get("ide"),
                "worktree_root": str(Path(str(worktree)).expanduser().resolve()),
                "hint": "Launch the target JetBrains IDE once, install the inspection plugin, or set jetbrains.ide to an installed app name.",
            },
        )
    results = []
    for config_dir in config_dirs:
        results.append(
            {
                "config_dir": str(config_dir),
                "trusted_locations": ensure_trusted_location_file(config_dir, trust_root),
                "project_opening": ensure_project_opening_policy(config_dir),
            }
        )
    return {
        "status": "trusted",
        "path": trust_path_token(trust_root),
        "config_updates": results,
    }


def trusted_root_for_worktree(context: dict[str, Any]) -> Path:
    worktree_path = Path(str(context.get("worktree_root"))).expanduser().resolve()
    matches: list[Path] = []
    for root in trusted_auto_open_roots():
        root_path = Path(str(root)).expanduser().resolve()
        if worktree_path == root_path or worktree_path.is_relative_to(root_path):
            matches.append(root_path)
    if not matches:
        raise InspectError("Worktree is outside trusted auto-open roots.", 3, {"worktree_root": str(worktree_path)})
    return sorted(matches, key=lambda path: len(path.parts), reverse=True)[0]


def jetbrains_config_dirs(context: dict[str, Any]) -> list[Path]:
    override = os.environ.get("JETBRAINS_INSPECTION_IDE_CONFIG_DIR")
    if override:
        return [Path(override).expanduser().resolve()]
    if sys.platform != "darwin":
        return []
    base = Path.home() / "Library" / "Application Support" / "JetBrains"
    if not base.exists():
        return []
    ide = str(context.get("ide") or "").lower()
    candidates = [path for path in base.iterdir() if path.is_dir() and (path / "options").exists()]
    matches = [path for path in candidates if ide and ide_config_matches(path.name, ide)]
    if matches:
        return sorted(matches, key=lambda path: path.stat().st_mtime, reverse=True)[:1]
    if ide:
        raise InspectError(
            "Cannot seed JetBrains trusted locations because no installed IDE config matched the requested IDE.",
            3,
            {
                "ide": context.get("ide"),
                "available_config_dirs": [path.name for path in sorted(candidates)],
                "hint": "Use the exact JetBrains app/config family name, such as IntelliJ IDEA, PyCharm, PyCharm CE, or WebStorm.",
            },
        )
    if len(candidates) == 1:
        return candidates
    raise InspectError(
        "Cannot seed JetBrains trusted locations because multiple IDE config directories exist and no IDE was selected.",
        3,
        {
            "available_config_dirs": [path.name for path in sorted(candidates)],
            "hint": "Set jetbrains.ide in the repo config or pass --ide so the helper updates the intended JetBrains product.",
        },
    )


def ide_config_matches(config_name: str, ide: str) -> bool:
    name = config_name.lower()
    if not ide:
        return True
    normalized = ide.replace(" ", "")
    aliases = {
        "intellijidea": ("intellijidea", "idea"),
        "intellij": ("intellijidea", "idea"),
        "idea": ("intellijidea", "idea"),
        "pycharm": ("pycharm",),
        "pycharmce": ("pycharm",),
        "webstorm": ("webstorm",),
    }
    needles = aliases.get(normalized, (normalized,))
    return any(needle in name for needle in needles)


def ensure_trusted_location_file(config_dir: Path, trust_root: Path) -> dict[str, Any]:
    options_dir = config_dir / "options"
    options_dir.mkdir(parents=True, exist_ok=True)
    path = options_dir / "trusted-paths.xml"
    token = trust_path_token(trust_root)
    created = not path.exists()
    if path.exists():
        tree = ET.parse(path)
        root = tree.getroot()
    else:
        root = ET.Element("application")
        tree = ET.ElementTree(root)
    trusted_settings = ensure_component(root, "Trusted.Paths.Settings")
    trusted_option = ensure_option(trusted_settings, "TRUSTED_PATHS")
    trusted_list = ensure_child(trusted_option, "list")
    changed = ensure_list_option(trusted_list, token)
    trusted_projects = ensure_component(root, "Trusted.Paths")
    projects_option = ensure_option(trusted_projects, "TRUSTED_PROJECT_PATHS")
    projects_map = ensure_child(projects_option, "map")
    changed = ensure_map_entry(projects_map, token, "true") or changed
    if changed or created:
        backup = backup_file(path) if path.exists() else None
        indent_xml(root)
        tree.write(path, encoding="utf-8", xml_declaration=False)
        return {"path": str(path), "changed": True, "created": created, "backup": str(backup) if backup else None}
    return {"path": str(path), "changed": False, "created": False}


def ensure_project_opening_policy(config_dir: Path) -> dict[str, Any]:
    options_dir = config_dir / "options"
    options_dir.mkdir(parents=True, exist_ok=True)
    path = options_dir / "ide.general.xml"
    created = not path.exists()
    if path.exists():
        tree = ET.parse(path)
        root = tree.getroot()
    else:
        root = ET.Element("application")
        tree = ET.ElementTree(root)
    settings = ensure_component(root, "GeneralSettings")
    option = ensure_option(settings, "confirmOpenNewProject2")
    changed = option.get("value") != "-1"
    if changed:
        option.set("value", "-1")
    if changed or created:
        backup = backup_file(path) if path.exists() else None
        indent_xml(root)
        tree.write(path, encoding="utf-8", xml_declaration=False)
        return {"path": str(path), "changed": True, "created": created, "backup": str(backup) if backup else None}
    return {"path": str(path), "changed": False, "created": False}


def trust_path_token(path: Path) -> str:
    home = Path.home().resolve()
    resolved = path.expanduser().resolve()
    if resolved == home:
        return "$USER_HOME$"
    if resolved.is_relative_to(home):
        return "$USER_HOME$/" + str(resolved.relative_to(home))
    return str(resolved)


def ensure_component(root: ET.Element, name: str) -> ET.Element:
    for child in root.findall("component"):
        if child.get("name") == name:
            return child
    return ET.SubElement(root, "component", {"name": name})


def ensure_option(parent: ET.Element, name: str) -> ET.Element:
    for child in parent.findall("option"):
        if child.get("name") == name:
            return child
    return ET.SubElement(parent, "option", {"name": name})


def ensure_child(parent: ET.Element, tag: str) -> ET.Element:
    child = parent.find(tag)
    if child is not None:
        return child
    return ET.SubElement(parent, tag)


def ensure_list_option(parent: ET.Element, value: str) -> bool:
    for child in parent.findall("option"):
        if child.get("value") == value:
            return False
    ET.SubElement(parent, "option", {"value": value})
    return True


def ensure_map_entry(parent: ET.Element, key: str, value: str) -> bool:
    for child in parent.findall("entry"):
        if child.get("key") == key:
            if child.get("value") == value:
                return False
            child.set("value", value)
            return True
    ET.SubElement(parent, "entry", {"key": key, "value": value})
    return True


def backup_file(path: Path) -> Path:
    backup = path.with_suffix(path.suffix + f".bak-{now_ms()}")
    shutil.copy2(path, backup)
    return backup


def indent_xml(element: ET.Element, level: int = 0) -> None:
    indent = "\n" + level * "  "
    child_indent = "\n" + (level + 1) * "  "
    children = list(element)
    if children:
        if not element.text or not element.text.strip():
            element.text = child_indent
        for child in children:
            indent_xml(child, level + 1)
        if not element.tail or not element.tail.strip():
            element.tail = indent
    elif level and (not element.tail or not element.tail.strip()):
        element.tail = indent


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


def cache_dir() -> Path:
    override = os.environ.get("JETBRAINS_INSPECTION_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "jetbrains-inspection-api"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "jetbrains-inspection-api"


def lease_dir() -> Path:
    return cache_dir() / "leases"


def lifecycle_lock_path() -> Path:
    return cache_dir() / "lifecycle.lock"


class lifecycle_lock:
    def __init__(self, timeout_ms: int = DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS):
        self.timeout_ms = max(0, int(timeout_ms))
        self.handle = None

    def __enter__(self):
        path = lifecycle_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+", encoding="utf-8")
        if fcntl is not None:
            deadline = time.monotonic() + (self.timeout_ms / 1000.0)
            while True:
                try:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as error:
                    if self.timeout_ms == 0 or time.monotonic() >= deadline:
                        self.handle.close()
                        raise InspectError(
                            "Timed out waiting for the JetBrains inspection lifecycle lock.",
                            3,
                            {
                                "lock_path": str(path),
                                "timeout_ms": self.timeout_ms,
                                "hint": "Another prepare/closeout is running. Wait for it to finish, increase --lifecycle-lock-timeout-ms, or run lifecycle closeouts sequentially.",
                            },
                        ) from error
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return self

    def __exit__(self, exc_type, exc, tb):
        if fcntl is not None and self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        if self.handle is not None:
            self.handle.close()


def now_ms() -> int:
    return int(time.time() * 1000)


def create_local_lease(context: dict[str, Any], state: str) -> dict[str, Any]:
    lease = {
        "lease_id": str(uuid.uuid4()),
        "state": state,
        "repo_path": context.get("repo_path"),
        "worktree_root": context.get("worktree_root"),
        "created_at_ms": now_ms(),
        "updated_at_ms": now_ms(),
        "pid": os.getpid(),
    }
    write_lease(lease)
    return lease


def lease_path(lease: dict[str, Any]) -> Path:
    return lease_dir() / f"{lease['lease_id']}.json"


def write_lease(lease: dict[str, Any]) -> None:
    directory = lease_dir()
    directory.mkdir(parents=True, exist_ok=True)
    lease["updated_at_ms"] = now_ms()
    path = lease_path(lease)
    temp = path.with_suffix(".json.tmp")
    # codeql[py/clear-text-storage-sensitive-data]
    temp.write_text(public_json(public_lease(lease)), encoding="utf-8")
    temp.replace(path)


def mark_lease_state(lease: dict[str, Any], state: str) -> None:
    if not lease.get("lease_id"):
        return
    lease["state"] = state
    write_lease(lease)


def remove_lease(lease: dict[str, Any]) -> None:
    if not lease.get("lease_id"):
        return
    try:
        lease_path(lease).unlink()
    except FileNotFoundError:
        pass


def public_lease(lease: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in lease.items()
        if not str(key).startswith("_")
    }


def command_cleanup_leases(args: argparse.Namespace) -> dict[str, Any]:
    directory = lease_dir()
    removed: list[str] = []
    stale: list[str] = []
    if not directory.exists():
        return {"status": "ok", "removed": [], "stale": []}
    cutoff = now_ms() - int(args.max_age_ms)
    for path in sorted(directory.glob("*.json")):
        try:
            lease = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        updated = int(lease.get("updated_at_ms") or lease.get("created_at_ms") or 0)
        pid = lease.get("pid")
        is_stale = updated < cutoff or (pid and not pid_alive(int(pid)))
        if not is_stale:
            continue
        stale.append(str(path))
        if not args.dry_run:
            path.unlink(missing_ok=True)
            removed.append(str(path))
    return {"status": "ok", "dry_run": args.dry_run, "stale": stale, "removed": removed}


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
        "project_instance_id": project.get("project_instance_id"),
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


def route_sort_key(route: dict[str, Any], context: dict[str, Any]) -> tuple[int, int, int]:
    route_base = route.get("base_path")
    worktree_root = context.get("worktree_root")
    try:
        route_path = Path(str(route_base)).resolve() if route_base else None
        worktree_path = Path(str(worktree_root)).resolve() if worktree_root else None
    except OSError:
        route_path = None
        worktree_path = None

    exact = int(route_path is not None and worktree_path is not None and route_path == worktree_path)
    depth = len(route_path.parts) if route_path is not None else 0
    return (int(route.get("score") or 0), exact, depth)


def ensure_exact_worktree(route: dict[str, Any], context: dict[str, Any], args: argparse.Namespace) -> None:
    if args.no_worktree_check:
        return
    route_base = route.get("base_path")
    worktree_root = context.get("worktree_root")
    if not route_base or not worktree_root:
        raise InspectError("Cannot verify exact worktree route; route or worktree path is missing.", 3, {"route": route, "context": public_context(context)})
    try:
        route_path = Path(route_base).resolve()
        worktree_path = Path(worktree_root).resolve()
    except OSError as error:
        raise InspectError(f"Cannot verify exact worktree route: {error}", 3, {"route": route, "context": public_context(context)}) from error
    if route_path != worktree_path:
        raise InspectError(
            "Lifecycle closeout requires the exact current worktree to be open in the IDE.",
            3,
            {"route_base_path": str(route_path), "worktree_root": str(worktree_path)},
        )


def open_in_ide(context: dict[str, Any], background: bool = False) -> None:
    ide = context.get("ide")
    if not ide or sys.platform != "darwin":
        raise InspectError("Cannot auto-open IDE without a configured macOS IDE name.", 3)
    target = context.get("worktree_root") or context.get("project_path")
    command = ["open"]
    if background:
        command.append("-g")
    command.extend(["-a", str(ide), str(target)])
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise InspectError(
            "Failed to ask macOS to open the JetBrains IDE.",
            3,
            {
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
                "ide": ide,
                "worktree_root": context.get("worktree_root"),
                "background_open": background,
                "hint": "Check the configured JetBrains app name; macOS open -a requires the application bundle name, such as IntelliJ IDEA, PyCharm, or PyCharm CE.",
            },
        )


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
