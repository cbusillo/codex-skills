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
import plistlib
import re
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
from xml.parsers.expat import ExpatError

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
LOOPBACK_HOST = "127.0.0.1"
READY_STATUS_VALUES = {"clean", "results_available"}
USABLE_STATUS_VALUES = READY_STATUS_VALUES | {"findings"}
REDACTED = "<redacted>"
UNKNOWN_LOG_ENV = "JB_INSPECT_UNKNOWN_LOG"
UNKNOWN_LOG_ASSESSMENT_COMMANDS = frozenset({"run", "closeout", "wait", "status", "problems"})
UNKNOWN_LOG_INFORMATIONAL_STATUSES = frozenset({"ok", "prepared", "resolved", "triggered", "claimed"})
PREFERRED_COMMANDS = {
    "list": "list-projects",
    "route": "resolve-route",
    "trigger": "start-inspection",
    "wait": "wait-for-inspection",
    "status": "get-status",
    "problems": "get-problems",
    "claim": "claim-worktree",
    "prepare": "prepare-worktree",
    "open-worktree": "prepare-worktree",
    "closeout": "inspect-closeout",
    "run": "inspect",
    "cleanup-leases": "cleanup-helper-leases",
}
COMMAND_ALIASES = {
    "list-projects": "list",
    "resolve-route": "route",
    "prepare-worktree": "prepare",
    "open-worktree": "prepare",
    "inspect": "run",
    "inspect-closeout": "closeout",
    "get-status": "status",
    "get-problems": "problems",
    "start-inspection": "trigger",
    "wait-for-inspection": "wait",
    "claim-worktree": "claim",
    "cleanup-helper-leases": "cleanup-leases",
}
ROLLOUT_FILE_ENVS = (
    "JB_INSPECT_ROLLOUT_FILE",
    "CODE_ROLLOUT_FILE",
    "CODEX_ROLLOUT_FILE",
    "CODE_SESSION_FILE",
    "CODEX_SESSION_FILE",
)
VERDICT_SOURCE_KEYS = (
    "inspection_verdict",
    "inspection_verdict_reason",
    "inspection_verdict_message",
    "inspection_verdict_next_action",
    "proof_failures",
)
SENSITIVE_KEY_PARTS = ("token", "secret", "password", "credential", "authorization")
PROJECT_OPEN_BLOCKED_REASON = "jetbrains_project_open_blocked"
PROJECT_OPEN_BLOCKED_HINT = (
    "JetBrains may be waiting on a Trust Project, safe-mode, or open-project prompt "
    "in a foreground or background IDE window. Bring the IDE forward, answer the prompt, "
    "then retry inspection."
)


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


@dataclass(frozen=True)
class IdeProduct:
    key: str
    display_name: str
    config_prefixes: tuple[str, ...]
    product_codes: tuple[str, ...]
    app_names: tuple[str, ...]
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class IdeCandidate:
    product_key: str
    name: str
    path: Path | None
    version: tuple[int, ...]
    channel: str
    source: str


@dataclass(frozen=True)
class IdeSelection:
    requested: str | None
    product_key: str | None
    product: str | None
    mode: str
    channel: str
    version: tuple[int, ...]
    app_name: str | None
    app_path: Path | None
    config_dir: Path | None
    source: str
    exact: bool

    def public(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "product_key": self.product_key,
            "product": self.product,
            "mode": self.mode,
            "channel": self.channel,
            "version": format_version(self.version),
            "app_name": self.app_name,
            "app_path": str(self.app_path) if self.app_path else None,
            "config_dir": str(self.config_dir) if self.config_dir else None,
            "source": self.source,
            "exact": self.exact,
        }


IDE_PRODUCTS = {
    "intellij": IdeProduct(
        key="intellij",
        display_name="IntelliJ IDEA",
        config_prefixes=("IntelliJIdea", "IdeaIC"),
        product_codes=("IU", "IC"),
        app_names=("IntelliJ IDEA",),
        aliases=("intellijidea", "intellij", "idea", "iu", "ic"),
    ),
    "pycharm": IdeProduct(
        key="pycharm",
        display_name="PyCharm",
        config_prefixes=("PyCharm",),
        product_codes=("PY", "PC"),
        app_names=("PyCharm", "PyCharm CE"),
        aliases=("pycharm", "pycharmce", "py", "pc"),
    ),
    "webstorm": IdeProduct(
        key="webstorm",
        display_name="WebStorm",
        config_prefixes=("WebStorm",),
        product_codes=("WS",),
        app_names=("WebStorm",),
        aliases=("webstorm", "ws"),
    ),
}
IDE_PRODUCT_BY_ALIAS = {
    alias: product
    for product in IDE_PRODUCTS.values()
    for alias in product.aliases
}


def main() -> int:
    parser = build_parser()
    args = parse_cli_args(parser)
    args.command = canonical_command(args.command)
    try:
        if args.command == "list":
            result = command_list(args)
            return emit(result, args.json, 0, command=args.command_input)
        if args.command == "route":
            context = build_context(args)
            result = command_route(args, context)
            return emit(result, args.json, 0, command=args.command_input)
        if args.command == "trigger":
            context = build_context(args)
            result = command_trigger(args, context)
            return emit(result, args.json, 0, command=args.command_input)
        if args.command == "wait":
            context = build_context(args)
            result = command_wait(args, context)
            return emit(result, args.json, classify_wait_exit(result), command=args.command_input)
        if args.command == "status":
            context = build_context(args)
            result = command_status(args, context)
            return emit(result, args.json, classify_status_exit(result), command=args.command_input)
        if args.command == "problems":
            context = build_context(args)
            result = command_problems(args, context)
            return emit(result, args.json, classify_problems_exit(result), command=args.command_input)
        if args.command == "claim":
            context = build_context(args)
            result = command_claim(args, context)
            return emit(result, args.json, 0, command=args.command_input)
        if args.command == "prepare":
            context = build_context(args)
            result = command_prepare(args, context)
            return emit(result, args.json, classify_prepare_exit(result), command=args.command_input)
        if args.command == "closeout":
            context = build_context(args)
            result = command_closeout(args, context)
            return emit(result, args.json, classify_closeout_exit(result), command=args.command_input)
        if args.command == "cleanup-leases":
            result = command_cleanup_leases(args)
            return emit(result, args.json, 0, command=args.command_input)
        if args.command == "run":
            context = build_context(args)
            result = command_run(args, context)
            return emit(result, args.json, classify_run_exit(result), command=args.command_input)
    except InspectError as error:
        payload = error_payload(error, args)
        return emit(payload, getattr(args, "json", False), error.exit_code, command=getattr(args, "command_input", getattr(args, "command", None)))
    return 2


def error_payload(error: InspectError, args: argparse.Namespace | None = None) -> dict[str, Any]:
    payload = dict(error.payload)
    message = str(error)
    prior_status = payload.pop("status", None)
    if prior_status and prior_status != "error":
        if isinstance(prior_status, dict):
            payload.setdefault("last_status", prior_status)
        else:
            payload.setdefault("reason", prior_status)
    payload["status"] = "error"
    payload.setdefault("error", message)
    payload.setdefault("error_message", message)
    payload.setdefault("error_reason", infer_error_reason(error, payload))
    payload.setdefault("exit_code", error.exit_code)
    command = getattr(args, "command_input", None) or getattr(args, "command", None)
    if command:
        payload.setdefault("command", preferred_command(str(command)))
    if "hint" not in payload:
        hint = hint_for_error_reason(str(payload.get("error_reason") or ""))
        if hint:
            payload["hint"] = hint
    return payload


def canonical_command(command: str) -> str:
    return COMMAND_ALIASES.get(command, command)


def preferred_command(command: str) -> str:
    return PREFERRED_COMMANDS.get(command, command)


def normalize_command_argv(argv: list[str]) -> list[str]:
    normalized = list(argv)
    for index, token in enumerate(normalized):
        if token == "--":
            break
        if token.startswith("-"):
            continue
        normalized[index] = preferred_command(token)
        break
    return normalized


def parse_cli_args(parser: argparse.ArgumentParser, argv: list[str] | None = None) -> argparse.Namespace:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    normalized_argv = normalize_command_argv(raw_argv)
    args = parser.parse_args(normalized_argv)
    args.command_input = args.command
    return args


def infer_error_reason(error: InspectError, payload: dict[str, Any]) -> str:
    for key in ("error_reason", "reason", "status"):
        value = payload.get(key)
        if value and value != "error":
            return normalize_reason(value)
    message = str(error).lower()
    if "invalid json" in message or "non-object json" in message:
        return "invalid_api_response"
    if "http " in message and "inspection api" in message:
        return "inspection_api_http_error"
    if "unavailable" in message or "no jetbrains inspection plugin" in message:
        return "inspection_api_unavailable"
    if "timed out" in message or "timeout" in message:
        return "timeout"
    if "wrong tree" in message or "exact current worktree" in message:
        return "worktree_route_mismatch"
    if "exact worktree is not open" in message:
        return "target_project_not_open"
    if "no open jetbrains project matched" in message:
        return "target_project_not_open"
    if "trusted" in message:
        return "untrusted_auto_open_root"
    if "launch" in message or "open" in message:
        return "ide_open_failed"
    return "inspection_helper_error"


def normalize_reason(value: Any) -> str:
    reason = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return reason or "inspection_helper_error"


def hint_for_error_reason(reason: str) -> str | None:
    return {
        "inspection_api_unavailable": "Open the repo in the configured JetBrains IDE with the inspection plugin installed, or allow lifecycle open to start it.",
        "invalid_api_response": "Check the installed inspection plugin version and IDE logs; the helper could not parse the API response.",
        "inspection_api_http_error": "Inspect the API error body and IDE logs for the failing endpoint.",
        "timeout": "Increase the timeout or check whether the IDE is indexing, opening, or blocked by a modal dialog.",
        "worktree_route_mismatch": "Open the exact worktree in the IDE or use inspect-closeout so the helper can claim the correct project.",
        "target_project_not_open": "Use inspect or prepare-worktree to lifecycle-open the exact worktree, or open that worktree manually in the configured IDE.",
        "untrusted_auto_open_root": "Move the worktree under a trusted auto-open root or update the repo/global trusted roots configuration.",
        "ide_open_failed": "Check the configured JetBrains app name and whether macOS can launch it with open -a.",
        "ide_selection_required": "Add preferred JetBrains IDE metadata to .github/github.json, or pass --ide for this one run.",
        "ide_config_ambiguous": "Add preferred JetBrains IDE metadata to .github/github.json so the helper updates the intended JetBrains config.",
        "ide_config_missing": "Launch the selected JetBrains IDE once, or choose an installed IDE/version in .github/github.json.",
    }.get(reason)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run JetBrains IDE inspections through the local plugin API.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON only.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    command_specs = {
        "list-projects": ("List discovered IDE projects without inspecting.", False),
        "resolve-route": ("Resolve an already-open IDE/project route; does not open projects by default.", False),
        "start-inspection": ("Start an inspection run without waiting for results.", True),
        "wait-for-inspection": ("Wait for a previously triggered inspection.", False),
        "get-status": ("Read current route-pinned inspection status.", False),
        "get-problems": ("Fetch current inspection problem details.", False),
        "claim-worktree": ("Claim an already-open exact worktree without opening an IDE.", False),
        "prepare-worktree": ("Open and claim the exact worktree; does not inspect.", False),
        "inspect-closeout": ("Readiness inspection: open if needed, inspect, and clean up helper-opened projects.", True),
        "inspect": ("Inspect now: open if needed, trigger, wait, fetch problems, and clean up helper-opened projects.", True),
    }
    for name, (help_text, include_scope) in command_specs.items():
        add_common(subparsers.add_parser(name, help=help_text), include_scope=include_scope)
    subparsers.add_parser("cleanup-helper-leases", help="Remove stale local helper lifecycle leases.")

    for name in ("wait-for-inspection", "inspect", "inspect-closeout"):
        subparsers.choices[name].add_argument("--timeout-ms", type=int, default=DEFAULT_WAIT_TIMEOUT_MS)
        subparsers.choices[name].add_argument("--poll-ms", type=int, default=DEFAULT_POLL_MS)
    for name in ("prepare-worktree", "inspect", "inspect-closeout"):
        subparsers.choices[name].set_defaults(open=True)
        subparsers.choices[name].add_argument("--no-open", dest="open", action="store_false", help="Do not open the IDE if the exact worktree is not already open.")
        subparsers.choices[name].add_argument("--background-open", dest="background_open", action="store_true", default=True, help="Launch the target IDE hidden/background before lifecycle opens. Default for lifecycle opens.")
        subparsers.choices[name].add_argument("--foreground-open", dest="background_open", action="store_false", help="Allow the IDE to take focus while launching.")
        subparsers.choices[name].add_argument("--prepare-timeout-ms", type=int, default=DEFAULT_PREPARE_TIMEOUT_MS)
        subparsers.choices[name].add_argument("--lifecycle-lock-timeout-ms", type=int, default=DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS)
        subparsers.choices[name].add_argument("--keep-warm", action="store_true", help="Leave helper-opened projects open after inspect or inspect-closeout.")
    subparsers.choices["cleanup-helper-leases"].add_argument("--max-age-ms", type=int, default=24 * 60 * 60 * 1000)
    subparsers.choices["cleanup-helper-leases"].add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    subparsers.choices["get-problems"].add_argument("--scope", help="Problem scope filter. Defaults from repo config or changed_files.")
    for name in ("get-problems", "inspect", "inspect-closeout"):
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
    command.add_argument("--ide-app", help="Exact macOS application bundle name to launch, e.g. WebStorm 2026.2 EAP. Defaults to --ide.")
    command.add_argument("--ide-channel", choices=("stable", "eap", "any"), help="IDE channel for product-level selection. Defaults to stable for product selectors.")
    command.add_argument("--ide-version", help="Exact IDE version selector, e.g. 2026.2.")
    command.add_argument("--project-key", help="Stable project key returned by resolve-route or list-projects.")
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
    explicit_project_path = repo_path if repo_path != worktree_root and has_project_markers(repo_path) else None
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
    ide_app = getattr(args, "ide_app", None) or jetbrains.get("ideApp") or jetbrains.get("ide_app")
    ide_channel = (
        getattr(args, "ide_channel", None)
        or jetbrains.get("ideChannel")
        or jetbrains.get("ide_channel")
    )
    ide_version = (
        getattr(args, "ide_version", None)
        or jetbrains.get("ideVersion")
        or jetbrains.get("ide_version")
    )
    scope = getattr(args, "scope", None) or first_scope(inspection.get("scopePreference")) or first_scope(jetbrains.get("scopePreference")) or "changed_files"
    worktree_strategy = jetbrains.get("worktreeStrategy") or jetbrains.get("worktree_strategy") or "prefer-current"

    lifecycle_target_path = explicit_project_path or configured_project_path or worktree_root

    context = {
        "repo_path": str(repo_path),
        "worktree_root": str(worktree_root),
        "main_worktree": str(main_worktree) if main_worktree else None,
        "project_path": str(lifecycle_target_path),
        "exact_route_path": str(lifecycle_target_path),
        "lifecycle_target_path": str(lifecycle_target_path),
        "ide": ide,
        "ide_app": ide_app,
        "ide_channel": ide_channel,
        "ide_version": str(ide_version) if ide_version else None,
        "scope": scope,
        "worktree_strategy": worktree_strategy,
        "config_path": str(worktree_root / ".github" / "github.json") if (worktree_root / ".github" / "github.json").exists() else None,
    }
    selection = resolve_ide_selection(context)
    if selection:
        context["ide_selection"] = selection.public()
        if not context.get("ide") and selection.product:
            context["ide"] = selection.product
        if not context.get("ide_app") and selection.app_name:
            context["ide_app"] = selection.app_name
        if selection.config_dir:
            context["ide_config_dir"] = str(selection.config_dir)
        if selection.app_path:
            context["ide_app_path"] = str(selection.app_path)
    return context


def resolve_ide_selection(context: dict[str, Any]) -> IdeSelection | None:
    requested = clean_optional(context.get("ide"))
    explicit_app = clean_optional(context.get("ide_app"))
    channel = normalize_ide_channel(context.get("ide_channel"))
    version = parse_version_tuple(clean_optional(context.get("ide_version")))
    selector = explicit_app or requested
    product = product_for_selector(selector) or product_for_selector(requested)
    if not selector and not product and not channel and not version:
        return None
    exact = bool(explicit_app or version or channel == "eap" or selector_contains_exact_marker(selector))
    app_candidates = discover_ide_app_candidates()
    app = select_ide_candidate(app_candidates, product, explicit_app or selector, channel, version, exact)
    selected_product = product or product_for_candidate(app)
    config_candidates = discover_ide_config_candidates()
    config_version = app.version if app and app.version else version
    config_channel = channel if channel and channel != "eap" and not app else None
    config = select_ide_candidate(config_candidates, selected_product, selector, config_channel, config_version, exact)
    selected_product = selected_product or product_for_candidate(config)
    app_name = explicit_app or (app.name if app else None) or (selected_product.display_name if selected_product and not exact else None) or (requested if not exact else None)
    app_path = app.path if app else exact_app_path(explicit_app)
    resolved_channel = (app.channel if app else None) or (config.channel if config else None) or channel or "unknown"
    resolved_version = (app.version if app and app.version else ()) or (config.version if config and config.version else ()) or version
    mode = "exact" if exact else ("product" if selected_product else "selector")
    return IdeSelection(
        requested=requested,
        product_key=selected_product.key if selected_product else None,
        product=selected_product.display_name if selected_product else requested,
        mode=mode,
        channel=resolved_channel,
        version=resolved_version,
        app_name=app_name,
        app_path=app_path,
        config_dir=config.path if config else None,
        source="repo_or_cli",
        exact=exact,
    )


def clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_selector(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def normalize_ide_channel(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"stable", "release", "current"}:
        return "stable"
    if text in {"eap", "preview", "early-access", "early_access"}:
        return "eap"
    if text == "any":
        return "any"
    return text


def selector_contains_exact_marker(selector: str | None) -> bool:
    if not selector:
        return False
    return bool(parse_version_tuple(selector)) or "eap" in selector.lower() or selector.endswith(".app") or os.sep in selector


def product_for_selector(selector: str | None) -> IdeProduct | None:
    normalized = normalize_selector(selector)
    if not normalized:
        return None
    if normalized in IDE_PRODUCT_BY_ALIAS:
        return IDE_PRODUCT_BY_ALIAS[normalized]
    for product in IDE_PRODUCTS.values():
        if any(normalized.startswith(normalize_selector(prefix)) for prefix in product.config_prefixes):
            return product
        long_aliases = [alias for alias in product.aliases if len(alias) > 2]
        if any(alias in normalized for alias in long_aliases):
            return product
    return None


def product_for_candidate(candidate: IdeCandidate | None) -> IdeProduct | None:
    if not candidate:
        return None
    return IDE_PRODUCTS.get(candidate.product_key)


def parse_version_tuple(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    match = re.search(r"(20\d{2})(?:[.\-](\d+))?(?:[.\-](\d+))?", value)
    if not match:
        return ()
    return tuple(int(part) for part in match.groups() if part is not None)


def version_from_jetbrains_text(value: str | None) -> tuple[int, ...]:
    parsed = parse_version_tuple(value)
    if parsed:
        return parsed
    match = re.search(r"\b(?:IU|IC|PY|PC|WS)-(\d{3})\.", str(value or ""))
    if not match:
        return ()
    build_major = int(match.group(1))
    return (2000 + build_major // 10, build_major % 10)


def format_version(version: tuple[int, ...]) -> str | None:
    return ".".join(str(part) for part in version) if version else None


def candidate_channel(name: str, bundle_id: str | None = None) -> str:
    text = f"{name} {bundle_id or ''}".lower()
    return "eap" if "eap" in text else "stable"


def discover_ide_config_candidates() -> list[IdeCandidate]:
    if sys.platform != "darwin":
        return []
    base = Path.home() / "Library" / "Application Support" / "JetBrains"
    if not base.exists():
        return []
    candidates: list[IdeCandidate] = []
    for path in base.iterdir():
        if not path.is_dir() or not (path / "options").exists():
            continue
        product = product_for_selector(path.name)
        if not product:
            continue
        candidates.append(
            IdeCandidate(
                product_key=product.key,
                name=path.name,
                path=path,
                version=version_from_jetbrains_text(path.name),
                channel=candidate_channel(path.name),
                source="config",
            )
        )
    return candidates


def discover_ide_app_candidates() -> list[IdeCandidate]:
    if sys.platform != "darwin":
        return []
    candidates: list[IdeCandidate] = []
    for base in (Path("/Applications"), Path.home() / "Applications"):
        if not base.exists():
            continue
        for path in base.glob("*.app"):
            candidate = ide_app_candidate(path)
            if candidate:
                candidates.append(candidate)
    return candidates


def ide_app_candidate(path: Path) -> IdeCandidate | None:
    app_name = path.stem
    info_path = path / "Contents" / "Info.plist"
    bundle_id = None
    short_version = ""
    if info_path.exists():
        try:
            with info_path.open("rb") as handle:
                info = plistlib.load(handle)
            app_name = str(info.get("CFBundleName") or info.get("CFBundleDisplayName") or app_name)
            bundle_id = str(info.get("CFBundleIdentifier") or "")
            short_version = str(info.get("CFBundleShortVersionString") or "")
        except (OSError, plistlib.InvalidFileException, ValueError, ExpatError):
            pass
    product = product_for_selector(" ".join(part for part in (app_name, path.stem, bundle_id) if part))
    if not product:
        return None
    display_name = path.stem if path.stem != app_name and "eap" in path.stem.lower() else app_name
    return IdeCandidate(
        product_key=product.key,
        name=display_name,
        path=path,
        version=version_from_jetbrains_text(" ".join((path.stem, short_version))),
        channel=candidate_channel(" ".join((path.stem, short_version)), bundle_id),
        source="app",
    )


def select_ide_candidate(
    candidates: list[IdeCandidate],
    product: IdeProduct | None,
    selector: str | None,
    channel: str | None,
    version: tuple[int, ...],
    exact: bool,
) -> IdeCandidate | None:
    selected = candidates
    if product:
        selected = [candidate for candidate in selected if candidate.product_key == product.key]
    if channel and channel != "any":
        selected = [candidate for candidate in selected if candidate.channel == channel]
    elif not exact:
        stable = [candidate for candidate in selected if candidate.channel == "stable"]
        if stable:
            selected = stable
    if version:
        selected = [candidate for candidate in selected if versions_match(candidate.version, version)]
    if selector and selector_contains_exact_marker(selector):
        selector_norm = normalize_selector(selector)
        exact_matches = [candidate for candidate in selected if selector_norm in normalize_selector(candidate.name) or (candidate.path and selector_norm in normalize_selector(str(candidate.path)))]
        selected = exact_matches
    if not selected:
        return None
    return sorted(selected, key=ide_candidate_sort_key, reverse=True)[0]


def versions_match(candidate: tuple[int, ...], requested: tuple[int, ...]) -> bool:
    if not candidate or not requested:
        return True
    size = min(len(candidate), len(requested))
    return candidate[:size] == requested[:size]


def ide_candidate_sort_key(candidate: IdeCandidate) -> tuple[int, tuple[int, ...], str]:
    channel_score = 1 if candidate.channel == "stable" else 0
    return (channel_score, candidate.version, candidate.name.lower())


def exact_app_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if path.suffix == ".app" or path.is_absolute():
        return path
    return None


def has_project_markers(path: Path) -> bool:
    markers = (
        ".idea",
        "settings.gradle",
        "settings.gradle.kts",
        "build.gradle",
        "build.gradle.kts",
        "pom.xml",
        "package.json",
        "pyproject.toml",
        "Cargo.toml",
        "go.mod",
    )
    return any((path / marker).exists() for marker in markers)


def command_list(args: argparse.Namespace) -> dict[str, Any]:
    identities = discover_identities(args.port)
    projects: list[dict[str, Any]] = []
    for identity in identities:
        for project in identity.get("open_projects", []) or []:
            projects.append(flatten_project(identity, project))
    result = {"status": "ok", "mode": "http", "projects": projects, "count": len(projects)}
    if identities and not projects:
        result["zero_project_hint"] = zero_project_hint()
        result["identities"] = [public_identity_summary(identity) for identity in identities]
    return result


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
    result = {
        "status": body.get("completion_reason") or body.get("status", "unknown"),
        "context": public_context(context),
        "route": body.get("route") or route,
        "wait": body,
    }
    copy_verdict_evidence(result, body)
    apply_verdict(result)
    return result


def command_status(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    route = resolve_route(args, context)
    body = call_endpoint(route, "status", route_params(args, context, route))
    status = status_label(body)
    result = {
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
    copy_verdict_evidence(result, body)
    apply_verdict(result)
    return result


def copy_verdict_evidence(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in (
        "total_problems",
        "problems_shown",
        "cached_total_problems",
        "cached_problems_shown",
        "capture_incomplete_reason",
        "capture_diagnostic",
        *VERDICT_SOURCE_KEYS,
    ):
        if key in source:
            target[key] = source[key]


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
    result: dict[str, Any] = {}
    with lifecycle_lock(getattr(args, "lifecycle_lock_timeout_ms", DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS)):
        prepared, lease, close_proof = prepare_lifecycle_details(args, context)
        try:
            result = run_inspection_on_route(args, context, prepared["route"])
        finally:
            if not getattr(args, "keep_warm", False):
                if should_defer_lifecycle_cleanup(result, lease):
                    cleanup = defer_lifecycle_cleanup(lease, result)
                else:
                    cleanup = cleanup_lifecycle(lease, prepared.get("route") or {}, close_proof)
        result["prepared"] = public_payload(prepared)
        result["cleanup"] = cleanup
        if cleanup.get("status") == "deferred":
            result["cleanup_deferred"] = True
        elif cleanup.get("status") not in {"closed", "not_needed", "skipped"}:
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
    apply_verdict(summary)
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
        open_method = open_project_for_lifecycle(args, context)
        opened_by_helper = True
        exact_route = wait_for_exact_route(args, context, getattr(args, "prepare_timeout_ms", DEFAULT_PREPARE_TIMEOUT_MS))
    else:
        open_method = "preexisting"
    ensure_exact_worktree(exact_route, context, args)
    wait_until_route_ready(args, context, exact_route, getattr(args, "prepare_timeout_ms", DEFAULT_PREPARE_TIMEOUT_MS))
    claim_metadata, close_proof = claim_lifecycle(args, context, exact_route, lease)
    lease.update(
        {
            "state": "prepared",
            "opened_by_helper": opened_by_helper,
            "open_method": open_method,
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
        "open_method": open_method,
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
    payload["error_reason"] = "project_open_blocked"
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
    payload = {
        "context": public_context(context),
        "ide": context.get("ide"),
        "worktree_root": context.get("worktree_root"),
        "target_worktree": lifecycle_target_path(context),
        "selected_trusted_root": selected_trusted_root_for_payload(context),
        "global_config": str(global_config_path()),
        "background_open": getattr(args, "background_open", False),
        "prepare_timeout_ms": timeout_ms,
        "blocked_diagnostic": project_open_blocked_diagnostic(args, context, timeout_ms),
        "likely_causes": causes,
        "hint": "Run again with --foreground-open, trust the project if prompted, set JetBrains project opening to New Window, or open the worktree manually once.",
    }
    payload.update(route_diagnostic_payload(args, context))
    return payload


def project_open_blocked_diagnostic(args: argparse.Namespace, context: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    return {
        "reason": PROJECT_OPEN_BLOCKED_REASON,
        "message": PROJECT_OPEN_BLOCKED_HINT,
        "background_open": getattr(args, "background_open", False),
        "prepare_timeout_ms": timeout_ms,
        "requested_ide": context.get("ide"),
        "requested_ide_app": context.get("ide_app"),
        "target_worktree": lifecycle_target_path(context),
        "selected_trusted_root": selected_trusted_root_for_payload(context),
    }


def selected_trusted_root_for_payload(context: dict[str, Any]) -> str | None:
    try:
        return str(trusted_root_for_worktree(context))
    except InspectError:
        return None


def lifecycle_target_path(context: dict[str, Any]) -> str | None:
    return context.get("lifecycle_target_path") or context.get("exact_route_path") or context.get("project_path") or context.get("worktree_root")


def zero_project_hint() -> str:
    return (
        "Discovered a JetBrains inspection plugin identity but zero open projects. "
        "A pending Trust Project, safe-mode, or open-project prompt may be preventing project loading."
    )


def route_diagnostic_payload(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any]:
    try:
        identities = discover_diagnostic_identities(getattr(args, "port", None))
    except InspectError as error:
        return {"route_diagnostic": {"discovery_error": str(error)}}
    target_ide = context.get("ide")
    projects = [
        flatten_project(identity, project)
        for identity in identities
        for project in identity.get("open_projects", []) or []
    ]
    matching_identities = [identity for identity in identities if identity_matches_context(identity, context)]
    matching_projects = [
        project
        for project in projects
        if flat_project_matches_context(project, context)
    ]
    other_projects = [
        project
        for project in projects
        if project not in matching_projects
    ]
    diagnostic = {
        "requested_ide": target_ide,
        "target_worktree": lifecycle_target_path(context),
        "target_project_path": context.get("project_path"),
        "discovered_identity_count": len(identities),
        "matching_identity_count": len(matching_identities),
        "discovered_project_count": len(projects),
        "matching_project_count": len(matching_projects),
        "identities": [public_identity_summary(identity) for identity in identities],
        "matching_projects": matching_projects[:10],
        "other_projects": other_projects[:10],
    }
    if identities and not matching_identities and target_ide:
        diagnostic["reason"] = "different_jetbrains_product_running"
        diagnostic["next_action"] = f"Open the worktree in {target_ide} with the inspection plugin installed and up to date for that IDE, or update repo config/--ide to one of the discovered JetBrains products."
    elif matching_identities and not matching_projects:
        diagnostic["reason"] = "target_ide_running_without_target_project"
        diagnostic["next_action"] = "Open the exact worktree in the configured IDE, check for a pending Trust Project, safe-mode, or open-project prompt, verify that IDE has the inspection plugin installed and up to date, or allow inspect-closeout to open it under a trusted root."
    elif not identities:
        diagnostic["reason"] = "no_plugin_instances_discovered"
        diagnostic["next_action"] = "Launch the configured JetBrains IDE with the inspection plugin installed; if an IDE was launched hidden/background, also check whether it is blocked before plugin registration by a Trust Project, safe-mode, or open-project prompt."
    return {"route_diagnostic": diagnostic}


def discover_diagnostic_identities(port: int | None) -> list[dict[str, Any]]:
    if port:
        return discover_identities(port)
    return merged_registry_and_port_identities()


def merged_registry_and_port_identities() -> list[dict[str, Any]]:
    identities_by_key: dict[str, dict[str, Any]] = {}
    for identity in registry_identities():
        identities_by_key[identity_key(identity)] = identity
    for candidate_port in configured_ports():
        try:
            identity = identity_for_port(candidate_port)
        except InspectError:
            continue
        key = identity_key(identity)
        identities_by_key[key] = merge_identity(identities_by_key.get(key), identity)
    return list(identities_by_key.values())


def merge_identity(existing: dict[str, Any] | None, live: dict[str, Any]) -> dict[str, Any]:
    if not existing:
        return live
    merged = existing.copy()
    for key, value in live.items():
        if value not in (None, "", []):
            merged[key] = value
    return merged


def identity_key(identity: dict[str, Any]) -> str:
    session_id = identity.get("session_id")
    if session_id:
        return f"session:{session_id}"
    port = identity.get("port")
    if port:
        return f"port:{port}"
    return json.dumps(public_identity_summary(identity), sort_keys=True)


def public_identity_summary(identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "ide_name": identity.get("ide_name") or identity.get("name"),
        "ide_product_code": identity.get("ide_product_code") or identity.get("product_code"),
        "ide_version": identity.get("ide_version") or identity.get("version"),
        "plugin_version": identity.get("plugin_version"),
        "plugin_build_fingerprint": identity.get("plugin_build_fingerprint"),
        "session_id": identity.get("session_id"),
        "port": identity.get("port"),
        "pid": identity.get("pid"),
        "open_project_count": len(identity.get("open_projects", []) or []),
    }


def plugin_identity_label(identity: dict[str, Any]) -> str:
    version = identity.get("plugin_version") or "unknown"
    fingerprint = identity.get("plugin_build_fingerprint")
    if fingerprint:
        return f"{version}@{fingerprint}"
    return str(version)


def flat_project_matches_context(project: dict[str, Any], context: dict[str, Any]) -> bool:
    if not identity_matches_context(
        {
            "ide_name": project.get("ide_name"),
            "ide_product_code": project.get("ide_product_code"),
        },
        context,
    ):
        return False
    target = context.get("exact_route_path") or context.get("project_path") or context.get("worktree_root")
    base_path = project.get("base_path")
    if not target or not base_path:
        return False
    try:
        return Path(str(base_path)).resolve() == Path(str(target)).resolve()
    except OSError:
        return str(base_path) == str(target)


def wait_until_route_ready(args: argparse.Namespace, context: dict[str, Any], route: dict[str, Any], timeout_ms: int) -> None:
    deadline = now_ms() + timeout_ms
    last_status: dict[str, Any] | None = None
    stable_ready_count = 0
    while now_ms() <= deadline:
        body = call_endpoint(route, "status", route_params(args, context, route))
        last_status = body
        if route_status_ready(body):
            stable_ready_count += 1
            if stable_ready_count >= 2:
                return
        else:
            stable_ready_count = 0
        time.sleep(max(DEFAULT_POLL_MS, 1_000) / 1000.0)
    raise InspectError(
        "Timed out waiting for JetBrains indexing/scanning to settle.",
        3,
        {
            "status": "timeout",
            "error_reason": "ide_not_ready_timeout",
            "last_status": last_status or {},
            "route": route,
        }
        | route_diagnostic_payload(args, context),
    )


def route_status_ready(body: dict[str, Any]) -> bool:
    if body.get("session_drift") or body.get("ambiguous") or body.get("unavailable"):
        return False
    if body.get("indexing") or body.get("is_scanning") or body.get("inspection_in_progress"):
        return False
    status = str(body.get("status") or body.get("completion_reason") or "").lower()
    if status in {"indexing", "running", "timed_out", "session_drift", "ambiguous", "unavailable"}:
        return False
    return True


def open_via_running_ide(args: argparse.Namespace, context: dict[str, Any]) -> bool:
    try:
        identities = discover_open_identities(args, context)
    except InspectError as error:
        reason = infer_error_reason(error, error.payload)
        if getattr(args, "port", None) and reason in {"inspection_api_unavailable", "timeout"}:
            return False
        raise
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
                    "worktree_path": lifecycle_target_path(context),
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


def open_project_for_lifecycle(args: argparse.Namespace, context: dict[str, Any]) -> str:
    if open_via_running_ide(args, context):
        return "running_ide"
    bootstrap_ide_app(context, background=getattr(args, "background_open", False))
    timeout_ms = getattr(args, "prepare_timeout_ms", DEFAULT_PREPARE_TIMEOUT_MS)
    wait_for_matching_ide_identity(args, context, timeout_ms)
    if wait_for_lifecycle_open(args, context, timeout_ms):
        return "bootstrapped_ide"
    raise InspectError(
        "Bootstrapped JetBrains IDE did not accept the lifecycle open request.",
        3,
        auto_open_timeout_payload(args, context, timeout_ms),
    )


def wait_for_lifecycle_open(args: argparse.Namespace, context: dict[str, Any], timeout_ms: int) -> bool:
    deadline = now_ms() + timeout_ms
    while now_ms() <= deadline:
        if open_via_running_ide(args, context):
            return True
        time.sleep(1)
    return False


def wait_for_matching_ide_identity(args: argparse.Namespace, context: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    deadline = now_ms() + timeout_ms
    last_error: InspectError | None = None
    while now_ms() <= deadline:
        try:
            identities = discover_open_identities(args, context)
            for identity in identities:
                if identity_matches_context(identity, context):
                    return identity
        except InspectError as error:
            last_error = error
        time.sleep(1)
    payload = auto_open_timeout_payload(args, context, timeout_ms)
    if last_error:
        payload["last_error"] = str(last_error)
    raise InspectError("Timed out waiting for the target JetBrains IDE plugin after hidden bootstrap.", 3, payload)


def discover_open_identities(args: argparse.Namespace, context: dict[str, Any]) -> list[dict[str, Any]]:
    identities = discover_identities(args.port)
    if args.port or not context.get("ide") or any(identity_matches_context(identity, context) for identity in identities):
        return identities
    return discover_diagnostic_identities(None)


def identity_matches_context(identity: dict[str, Any], context: dict[str, Any]) -> bool:
    selection = context.get("ide_selection") if isinstance(context.get("ide_selection"), dict) else {}
    product_key = selection.get("product_key")
    ide = str(context.get("ide") or "").lower().replace(" ", "")
    if not ide and not product_key:
        return True
    values = [
        str(identity.get("ide_name") or "").lower().replace(" ", ""),
        str(identity.get("ide_product_code") or "").lower().replace(" ", ""),
        str(identity.get("product_code") or "").lower().replace(" ", ""),
        str(identity.get("ide_version") or "").lower().replace(" ", ""),
        str(identity.get("version") or "").lower().replace(" ", ""),
        str(identity.get("build_number") or "").lower().replace(" ", ""),
    ]
    if product_key and product_key in IDE_PRODUCTS:
        product = IDE_PRODUCTS[product_key]
        needles = tuple(alias.replace(" ", "") for alias in product.aliases) + tuple(code.lower() for code in product.product_codes)
    else:
        product = product_for_selector(context.get("ide"))
        needles = tuple(alias.replace(" ", "") for alias in product.aliases) + tuple(code.lower() for code in product.product_codes) if product else (ide,)
    identity_text = " ".join(values)
    selection_channel = selection.get("channel")
    if selection.get("version"):
        identity_version = version_from_jetbrains_text(identity_text)
        requested_version = parse_version_tuple(str(selection.get("version")))
        if requested_version and selection.get("exact") and not identity_version:
            return False
        if identity_version and requested_version and not versions_match(identity_version, requested_version):
            return False
    if selection_channel == "eap" and "eap" not in identity_text:
        return False
    if selection_channel == "stable" and "eap" in identity_text:
        return False
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
            "Inspection plugin route does not include project_instance_id; install the updated plugin before inspect-closeout.",
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
    body = private_http_get_body(port, "lifecycle/close", params, timeout=35.0)
    status = str(body.get("status") or "closed")
    if status == "closed":
        return {
            "status": "closed",
            "reason": body.get("reason"),
            "cleanup_skipped": False,
            "cleanup_failed": False,
        }
    return {
        "status": status,
        "reason": body.get("reason") or status,
        "cleanup_skipped": True,
        "cleanup_failed": False,
    }


def public_cleanup_reason(error: InspectError) -> str:
    reason = error.payload.get("reason") or error.payload.get("error_reason") or error.payload.get("status")
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


def private_http_get_body(port: int, endpoint: str, params: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
    clean_params = {key: str(value) for key, value in params.items() if value is not None and value != ""}
    query = urllib.parse.urlencode(clean_params, doseq=True)
    base_url = f"http://{LOOPBACK_HOST}:{port}/api/inspection/{endpoint}"
    request_url = f"{base_url}?{query}" if query else base_url
    request = urllib.request.Request(request_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout or max(DEFAULT_TIMEOUT_SECONDS, 10.0)) as response:
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
            open_project_for_lifecycle(args, context)
            time.sleep(4)
            continue
        if not identities:
            raise InspectError("No JetBrains inspection plugin instances discovered.", 3, {"hint": "Open the repo in the preferred JetBrains IDE with the inspection plugin installed."})

        candidates = []
        for identity in identities:
            if not identity_matches_context(identity, context):
                continue
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
            open_project_for_lifecycle(args, context)
            time.sleep(4)
            continue
        if not candidates:
            diagnostic_payload = route_diagnostic_payload(args, context)
            diagnostic = diagnostic_payload.get("route_diagnostic")
            if not isinstance(diagnostic, dict):
                diagnostic = {}
            matching_project_count = int(diagnostic.get("matching_project_count") or 0)
            error_reason = "matching_project_route_unavailable" if matching_project_count else "target_project_not_open"
            message = (
                "Matching JetBrains project is visible but route resolution is unavailable."
                if matching_project_count
                else "No open JetBrains project matched this repo/worktree."
            )
            raise InspectError(
                message,
                3,
                {"selector": selector_params(args, context), "error_reason": error_reason}
                | diagnostic_payload,
            )
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
    return merged_registry_and_port_identities()


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
    try:
        reported_port = int(body.get("port") or 0)
    except (TypeError, ValueError) as error:
        raise InspectError(
            f"Inspection API identity on port {port} reported invalid port {body.get('port')!r}.",
            3,
            {"error_reason": "invalid_identity_port", "requested_port": port, "reported_port": body.get("port")},
        ) from error
    if reported_port and reported_port != port:
        raise InspectError(
            f"Inspection API identity on port {port} reported port {reported_port}.",
            3,
            {"error_reason": "identity_port_mismatch", "requested_port": port, "reported_port": reported_port},
        )
    if "port" not in body or not body.get("port"):
        body["port"] = port
    return body


def http_get(port: int, endpoint: str, params: dict[str, Any], timeout: float = DEFAULT_TIMEOUT_SECONDS) -> HttpResult:
    clean_params = {key: str(value) for key, value in params.items() if value is not None and value != ""}
    query = urllib.parse.urlencode(clean_params, doseq=True)
    display_query = urllib.parse.urlencode(redact_payload(clean_params), doseq=True)
    base_url = f"http://{LOOPBACK_HOST}:{port}/api/inspection/{endpoint}"
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
        "worktree_path": args.worktree_path or lifecycle_target_path(context),
        "cwd": args.cwd or lifecycle_target_path(context),
        "project": args.project,
        "ide": args.ide or context.get("ide"),
        "session_id": args.session_id,
    }


def route_params(args: argparse.Namespace, context: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    params = {
        "project_key": args.project_key or route.get("project_key"),
        "session_id": args.session_id or route.get("session_id"),
        "project_path": args.project_path or context.get("project_path"),
        "worktree_path": args.worktree_path or lifecycle_target_path(context),
        "cwd": args.cwd or lifecycle_target_path(context),
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
    directory = getattr(args, "directory", None)
    if directory:
        params["dir"] = directory
    files = getattr(args, "files", []) or []
    if files:
        params["files"] = "\n".join(files)
    max_files = getattr(args, "max_files", None)
    if max_files:
        params["max_files"] = max_files
    if getattr(args, "include_stale", False):
        params["include_stale"] = "true"
    return params


def wait_http_timeout(timeout_ms: int) -> float:
    return max(DEFAULT_TIMEOUT_SECONDS, (timeout_ms / 1000.0) + 5.0)


def summarize_problems(context: dict[str, Any], route: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    problems = body.get("problems") or []
    status = body.get("status", "unknown")
    results_may_be_stale = body.get("results_may_be_stale", False) or status == "stale_results"
    total_problems = body.get("total_problems")
    has_explicit_zero_result = isinstance(total_problems, int) and total_problems == 0
    summary: dict[str, Any] = {
        "status": status,
        "clean": status == "results_available" and has_explicit_zero_result and not body.get("capture_incomplete") and not results_may_be_stale,
        "context": public_context(context),
        "route": route,
        "capture_incomplete": body.get("capture_incomplete", False),
        "results_may_be_stale": results_may_be_stale,
        "problems": problems,
        "raw": body,
    }
    if "total_problems" in body:
        summary["total_problems"] = body["total_problems"]
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
        *VERDICT_SOURCE_KEYS,
    ):
        if key in body:
            summary[key] = body[key]
    apply_verdict(summary)
    return summary


def classify_run_status(wait: dict[str, Any], problems: dict[str, Any]) -> str:
    if wait.get("timed_out"):
        return "timed_out"
    if wait.get("capture_incomplete") or problems.get("capture_incomplete"):
        return "capture_incomplete"
    if wait.get("results_may_be_stale") or problems.get("results_may_be_stale") or problems.get("status") == "stale_results":
        return "stale_results"
    total = problems.get("total_problems")
    if problems.get("status") == "results_available" and (
        (problems.get("problems") or []) or (isinstance(total, int) and total > 0)
    ):
        return "findings"
    if problems.get("status") == "results_available" and (isinstance(total, int) and total == 0 or problems.get("clean") is True):
        return "clean"
    return problems.get("status") or wait.get("completion_reason") or "unknown"


def should_defer_lifecycle_cleanup(result: dict[str, Any], lease: dict[str, Any]) -> bool:
    if not lease.get("opened_by_helper"):
        return False
    if result.get("verdict") in {"GREEN", "RED"}:
        return False
    wait = result.get("wait") if isinstance(result.get("wait"), dict) else {}
    return active_ide_churn(result) or active_ide_churn(wait)


def active_ide_churn(payload: dict[str, Any]) -> bool:
    if not payload:
        return False
    status = str(payload.get("status") or payload.get("completion_reason") or "").lower()
    proof_failures = payload.get("proof_failures") if isinstance(payload.get("proof_failures"), list) else []
    return (
        payload.get("timed_out") is True
        and (
            payload.get("indexing") is True
            or payload.get("is_scanning") is True
            or payload.get("inspection_in_progress") is True
            or status in {"indexing", "running", "timed_out"}
            or any(reason in {"indexing", "inspection_still_running", "timeout"} for reason in proof_failures)
        )
    )


def defer_lifecycle_cleanup(lease: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    mark_lease_state(lease, "kept_warm_after_indexing_timeout")
    return {
        "status": "deferred",
        "reason": "indexing_or_inspection_still_running",
        "cleanup_deferred": True,
        "lease_id": lease.get("lease_id"),
        "project_key": lease.get("project_key"),
        "project_instance_id": lease.get("project_instance_id"),
        "next_action": "Rerun inspect-closeout after indexing/scanning settles; use cleanup-helper-leases if the warm project becomes stale.",
        "verdict_reason": result.get("verdict_reason"),
    }


def verdict_for_payload(payload: dict[str, Any]) -> dict[str, str]:
    wait = payload.get("wait") if isinstance(payload.get("wait"), dict) else {}
    cleanup = payload.get("cleanup") if isinstance(payload.get("cleanup"), dict) else {}
    blocker_reason = blocking_unknown_reason(payload, wait)
    if blocker_reason is not None:
        return {
            "verdict": "UNKNOWN",
            "verdict_reason": blocker_reason,
            "verdict_message": "Inspection did not produce a trustworthy GREEN or RED result.",
            "verdict_next_action": next_action_for_unknown(blocker_reason, payload),
        }

    if cleanup.get("status") in {"failed", "skipped"} or payload.get("cleanup_failed") or payload.get("cleanup_skipped"):
        reason = unknown_reason(payload, wait, cleanup)
        return {
            "verdict": "UNKNOWN",
            "verdict_reason": reason,
            "verdict_message": "Readiness inspection did not complete cleanly after inspection.",
            "verdict_next_action": next_action_for_unknown(reason, payload),
        }

    if payload.get("status") == "error":
        reason = str(payload.get("error_reason") or "helper_error")
        return {
            "verdict": "UNKNOWN",
            "verdict_reason": reason,
            "verdict_message": "Inspection tooling failed before it could prove GREEN or RED.",
            "verdict_next_action": next_action_for_unknown(reason, payload),
        }

    if payload.get("proof_failures"):
        return {
            "verdict": "UNKNOWN",
            "verdict_reason": "inspection_proof_failed",
            "verdict_message": "Inspection returned contradictory proof and did not establish a trustworthy GREEN or RED result.",
            "verdict_next_action": next_action_for_unknown("inspection_proof_failed", payload),
        }

    plugin_verdict = payload.get("inspection_verdict")
    if plugin_verdict in {"GREEN", "RED", "UNKNOWN"}:
        return {
            "verdict": str(plugin_verdict),
            "verdict_reason": str(payload.get("inspection_verdict_reason") or "plugin_verdict"),
            "verdict_message": str(payload.get("inspection_verdict_message") or "Inspection plugin provided the verdict."),
            "verdict_next_action": str(payload.get("inspection_verdict_next_action") or "Follow the inspection plugin verdict."),
        }

    problems = payload.get("problems") or []
    total = payload.get("total_problems")
    has_current_findings = (problems or (isinstance(total, int) and total > 0)) and not payload.get("capture_incomplete") and not payload.get("results_may_be_stale")
    if payload.get("status") == "findings" or has_current_findings:
        return {
            "verdict": "RED",
            "verdict_reason": "actionable_findings",
            "verdict_message": "Inspection worked and returned actionable findings.",
            "verdict_next_action": "Fix the reported findings, then rerun inspection.",
        }

    has_explicit_zero_result = isinstance(total, int) and total == 0
    if (
        (payload.get("status") == "results_available" and (has_explicit_zero_result or payload.get("clean") is True))
        or payload.get("status") == "clean"
        or payload.get("clean") is True
    ):
        return {
            "verdict": "GREEN",
            "verdict_reason": "no_matching_findings" if payload.get("status") == "results_available" else "clean_confirmed",
            "verdict_message": "Inspection worked and found no actionable findings for the selected scope/filter.",
            "verdict_next_action": "No inspection action required for this scope/filter.",
        }

    reason = unknown_reason(payload, wait, cleanup)
    return {
        "verdict": "UNKNOWN",
        "verdict_reason": reason,
        "verdict_message": "Inspection did not produce a trustworthy GREEN or RED result.",
        "verdict_next_action": next_action_for_unknown(reason, payload),
    }


def blocking_unknown_reason(payload: dict[str, Any], wait: dict[str, Any]) -> str | None:
    if payload.get("session_drift") or wait.get("session_drift"):
        return "session_drift"
    if payload.get("ambiguous"):
        return "ambiguous_route"
    if payload.get("unavailable"):
        return "inspection_api_unavailable"
    if payload.get("results_may_be_stale") or wait.get("results_may_be_stale") or payload.get("status") == "stale_results":
        return "stale_results"
    if payload.get("capture_incomplete") or wait.get("capture_incomplete") or payload.get("status") == "capture_incomplete":
        return str(payload.get("capture_incomplete_reason") or wait.get("capture_incomplete_reason") or "capture_incomplete")
    if payload.get("timed_out") or wait.get("timed_out") or payload.get("status") == "timed_out":
        return "timeout"
    if payload.get("indexing") or payload.get("is_scanning") or payload.get("inspection_in_progress") or payload.get("status") in {"indexing", "running"}:
        return "inspection_still_running"
    return None


def unknown_reason(payload: dict[str, Any], wait: dict[str, Any], cleanup: dict[str, Any]) -> str:
    if cleanup.get("status") in {"failed", "skipped"} or payload.get("cleanup_failed") or payload.get("cleanup_skipped"):
        return f"cleanup_{cleanup.get('status') or 'failed'}"
    blocker_reason = blocking_unknown_reason(payload, wait)
    if blocker_reason is not None:
        return blocker_reason
    if payload.get("status") == "no_results" or wait.get("completion_reason") == "no_results":
        return "no_results"
    return str(payload.get("status") or wait.get("completion_reason") or "unknown")


def next_action_for_unknown(reason: str, payload: dict[str, Any]) -> str:
    diagnostic = payload.get("capture_diagnostic") if isinstance(payload.get("capture_diagnostic"), dict) else {}
    if reason in {"non_empty_unmapped_tree", "extractor_failure", "helper_plugin_error"}:
        return "Treat this as a plugin/helper bug: capture the diagnostic payload, update the inspection plugin or helper skill, and rerun."
    if reason in {"view_not_ready", "view_updating_unreadable", "unreadable_tree", "no_results"}:
        return "Open the IDE Inspection Results or Problems view for the exact worktree, then rerun inspection."
    if reason == "current_run_psi_churn":
        return "Save documents and rerun inspection after the IDE finishes updating PSI state."
    if reason == "stale_results":
        return "Rerun inspection; stale cached findings must not be treated as current."
    if reason == "timeout":
        return "Wait for indexing/scanning to settle or rerun with a larger timeout."
    if reason == "inspection_still_running":
        return "Wait for indexing/scanning to finish, then rerun inspection."
    if reason == "inspection_api_unavailable":
        return "Open the exact worktree in the configured JetBrains IDE with the inspection plugin installed."
    if reason == "ambiguous_route":
        return "Pass project_key, project_path, or worktree_path so the helper can inspect the exact project."
    if reason == "session_drift":
        return "Resolve the route again and rerun; the IDE/plugin session changed."
    if reason.startswith("cleanup_"):
        return "Inspect lifecycle cleanup output; close helper-opened IDE projects or rerun inspect-closeout after cleanup succeeds."
    if diagnostic.get("observed_non_empty_inspection_tree") is True:
        return "Treat this as a plugin/helper capture bug and include capture_diagnostic when reporting it."
    return "Do not report GREEN or RED. Rerun inspection and include helper diagnostics if it remains UNKNOWN."


def apply_verdict(payload: dict[str, Any]) -> dict[str, Any]:
    payload.update(verdict_for_payload(payload))
    return payload


def log_unknown_verdict(payload: dict[str, Any]) -> None:
    if payload.get("verdict") != "UNKNOWN":
        return
    if not should_log_unknown_verdict(payload):
        return
    log_path = unknown_log_path()
    if log_path is None:
        return
    record = unknown_log_record(payload)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as error:
        payload["unknown_log_error"] = str(error)
        return
    payload["unknown_log_path"] = str(log_path)


def should_log_unknown_verdict(payload: dict[str, Any]) -> bool:
    command = canonical_command(str(payload.get("command") or ""))
    has_failure_evidence = any(
        payload.get(key)
        for key in (
            "error_reason",
            "capture_incomplete",
            "results_may_be_stale",
            "timed_out",
            "session_drift",
            "ambiguous",
            "unavailable",
            "indexing",
            "is_scanning",
            "inspection_in_progress",
            "cleanup_failed",
            "cleanup_skipped",
            "capture_diagnostic",
            "route_diagnostic",
            "blocked_diagnostic",
        )
    )
    if payload.get("status") in UNKNOWN_LOG_INFORMATIONAL_STATUSES and not has_failure_evidence:
        return False
    if command and command not in UNKNOWN_LOG_ASSESSMENT_COMMANDS and not has_failure_evidence:
        return False
    return True


def unknown_log_path() -> Path | None:
    configured = os.environ.get(UNKNOWN_LOG_ENV)
    if configured is not None:
        value = configured.strip()
        if value.lower() in {"", "0", "false", "no", "off"}:
            return None
        return Path(value).expanduser().resolve()
    return Path(code_home()) / "jetbrains-inspection" / "unknown-verdicts.jsonl"


def code_home() -> Path:
    return Path(os.environ.get("CODE_HOME") or os.environ.get("CODEX_HOME") or str(Path.home() / ".code")).expanduser()


def unknown_log_record(payload: dict[str, Any]) -> dict[str, Any]:
    public = public_payload(payload)
    command = public.get("command")
    context = public.get("context") if isinstance(public.get("context"), dict) else {}
    route = public.get("route") if isinstance(public.get("route"), dict) else {}
    wait = public.get("wait") if isinstance(public.get("wait"), dict) else {}
    cleanup = public.get("cleanup") if isinstance(public.get("cleanup"), dict) else {}
    record: dict[str, Any] = {
        "timestamp": utc_timestamp(),
        "command": preferred_command(str(command)) if command else None,
        "verdict": public.get("verdict"),
        "verdict_reason": public.get("verdict_reason"),
        "verdict_message": public.get("verdict_message"),
        "verdict_next_action": public.get("verdict_next_action"),
        "status": public.get("status"),
        "repo_path": context.get("repo_path") or public.get("repo_path"),
        "worktree_root": context.get("worktree_root") or public.get("worktree_root"),
        "scope": context.get("scope") or public.get("scope"),
        "ide": route.get("ide", {}).get("name") if isinstance(route.get("ide"), dict) else public.get("ide"),
        "project_name": route.get("project_name"),
        "project_key": route.get("project_key"),
        "base_path": route.get("base_path"),
        "total_problems": public.get("total_problems"),
        "problems_shown": public.get("problems_shown"),
        "capture_incomplete_reason": public.get("capture_incomplete_reason") or wait.get("capture_incomplete_reason"),
        "snapshot_change_kind": public.get("snapshot_change_kind"),
        "cleanup_status": cleanup.get("status"),
        "cleanup_reason": cleanup.get("reason"),
    }
    rollout_file = discover_rollout_file()
    if rollout_file:
        record["rollout_file"] = rollout_file
    for key in ("capture_diagnostic", "route_diagnostic", "blocked_diagnostic"):
        if key in public:
            record[key] = public[key]
    return {key: value for key, value in record.items() if value not in (None, {}, [])}


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def discover_rollout_file() -> str | None:
    for env_name in ROLLOUT_FILE_ENVS:
        value = os.environ.get(env_name)
        if value:
            return str(Path(value).expanduser())
    candidates: list[Path] = []
    home = Path(code_home())
    for root in (home / "sessions", home / "rollouts"):
        if root.exists():
            candidates.extend(root.rglob("rollout-*.jsonl"))
    for catalog in (home / "sessions" / "index" / "catalog.jsonl", home / "rollouts" / "index" / "catalog.jsonl"):
        candidates.extend(rollout_candidates_from_catalog(catalog))
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime if path.exists() else 0)
    return str(newest)


def rollout_candidates_from_catalog(catalog: Path) -> list[Path]:
    if not catalog.exists():
        return []
    candidates: list[Path] = []
    try:
        with catalog.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                for key in ("path", "file", "rollout_file", "session_file"):
                    value = entry.get(key)
                    if isinstance(value, str) and "rollout-" in value:
                        path = Path(value).expanduser()
                        candidates.append(path if path.is_absolute() else (catalog.parent / path).resolve())
    except OSError:
        return []
    return candidates


def verdict_exit_code(payload: dict[str, Any], success_verdicts: set[str]) -> int:
    verdict = payload.get("verdict")
    if verdict not in {"GREEN", "RED", "UNKNOWN"}:
        verdict = verdict_for_payload(payload).get("verdict")
    return 0 if verdict in success_verdicts else 1


def classify_run_exit(result: dict[str, Any]) -> int:
    return verdict_exit_code(result, {"GREEN"})


def classify_prepare_exit(result: dict[str, Any]) -> int:
    return 0 if result.get("status") == "prepared" else 1


def classify_closeout_exit(result: dict[str, Any]) -> int:
    if result.get("cleanup_failed") or result.get("cleanup_skipped"):
        return 1
    return classify_run_exit(result)


def classify_wait_exit(result: dict[str, Any]) -> int:
    return verdict_exit_code(result, {"GREEN", "RED"})


def classify_problems_exit(result: dict[str, Any]) -> int:
    return verdict_exit_code(result, {"GREEN"})


def classify_status_body_clean(body: dict[str, Any]) -> bool:
    if body.get("session_drift") or body.get("ambiguous") or body.get("unavailable"):
        return False
    if body.get("timed_out") or body.get("capture_incomplete") or body.get("results_may_be_stale"):
        return False
    if body.get("is_scanning") or body.get("indexing") or body.get("inspection_in_progress"):
        return False
    status = str(body.get("status") or body.get("completion_reason") or "").lower()
    if status:
        return status == "clean"
    return body.get("clean_inspection") is True


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
    return verdict_exit_code(result, {"GREEN", "RED"})


def emit(payload: dict[str, Any], json_only: bool, exit_code: int, command: str | None = None) -> int:
    if command is not None:
        payload["command"] = preferred_command(command)
    elif payload.get("command"):
        payload["command"] = preferred_command(str(payload["command"]))
    apply_verdict(payload)
    log_unknown_verdict(payload)
    payload = public_payload(payload)
    if json_only:
        # codeql[py/clear-text-logging-sensitive-data]
        print(public_json(payload))
        return exit_code
    print_human(payload)
    return exit_code


def print_human(payload: dict[str, Any]) -> None:
    apply_verdict(payload)
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
    verdict = payload.get("verdict")
    if verdict:
        print(safe_text("VERDICT: {verdict} reason={reason} message={message}", {
            "verdict": verdict,
            "reason": payload.get("verdict_reason"),
            "message": payload.get("verdict_message"),
        }))
        if payload.get("verdict_next_action"):
            print(safe_text("NEXT_ACTION: {action}", {"action": payload.get("verdict_next_action")}))
    if payload.get("unknown_log_path"):
        print(safe_text("UNKNOWN_LOG: {path}", {"path": payload.get("unknown_log_path")}))
    if payload.get("unknown_log_error"):
        print(safe_text("UNKNOWN_LOG_ERROR: {error}", {"error": payload.get("unknown_log_error")}))
    if payload.get("zero_project_hint"):
        print(safe_text("PROJECT_OPEN_HINT: {hint}", {"hint": payload.get("zero_project_hint")}))
    print_ide_selection(payload.get("ide_selection") or (payload.get("context") or {}).get("ide_selection"))
    if status == "error":
        print_error_details(payload)
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


def print_error_details(payload: dict[str, Any]) -> None:
    details = {
        "reason": payload.get("error_reason") or payload.get("reason"),
        "message": payload.get("error_message") or payload.get("error"),
        "command": payload.get("command"),
        "exit_code": payload.get("exit_code"),
    }
    print(safe_text("ERROR: reason={reason} message={message} command={command} exit_code={exit_code}", details))
    context = payload.get("context") or {}
    route = payload.get("route") or {}
    identity = payload.get("identity") or {}
    context_details = {
        "repo": context.get("repo_path") or payload.get("repo_path"),
        "worktree": context.get("worktree_root") or payload.get("worktree_root"),
        "ide": context.get("ide") or identity.get("name") or route.get("ide", {}).get("name") or payload.get("ide"),
        "endpoint": payload.get("endpoint"),
        "url": payload.get("url"),
    }
    if any(value is not None for value in context_details.values()):
        print(safe_text("CONTEXT: repo={repo} worktree={worktree} ide={ide} endpoint={endpoint} url={url}", context_details))
    print_blocked_diagnostic(payload.get("blocked_diagnostic"))
    print_route_diagnostic(payload.get("route_diagnostic"))
    if payload.get("hint"):
        print(safe_text("HINT: {hint}", {"hint": payload.get("hint")}))
    if payload.get("next_action"):
        print(safe_text("NEXT_ACTION: {action}", {"action": payload.get("next_action")}))


def print_ide_selection(selection: Any) -> None:
    if not isinstance(selection, dict) or not selection:
        return
    print(
        safe_text(
            "IDE_SELECTION: requested={requested} product={product} mode={mode} channel={channel} version={version} app={app} config={config}",
            {
                "requested": selection.get("requested"),
                "product": selection.get("product"),
                "mode": selection.get("mode"),
                "channel": selection.get("channel"),
                "version": selection.get("version"),
                "app": selection.get("app_path") or selection.get("app_name"),
                "config": selection.get("config_dir"),
            },
        )
    )


def print_blocked_diagnostic(diagnostic: Any) -> None:
    if not isinstance(diagnostic, dict):
        return
    print(
        safe_text(
            "PROJECT_OPEN_BLOCKED: reason={reason} requested_ide={requested_ide} target_worktree={target_worktree} background_open={background_open} prepare_timeout_ms={prepare_timeout_ms} selected_trusted_root={selected_trusted_root}",
            {
                "reason": diagnostic.get("reason"),
                "requested_ide": diagnostic.get("requested_ide"),
                "target_worktree": diagnostic.get("target_worktree"),
                "background_open": diagnostic.get("background_open"),
                "prepare_timeout_ms": diagnostic.get("prepare_timeout_ms"),
                "selected_trusted_root": diagnostic.get("selected_trusted_root"),
            },
        )
    )
    if diagnostic.get("message"):
        print(safe_text("PROJECT_OPEN_BLOCKED_HINT: {message}", {"message": diagnostic.get("message")}))


def print_route_diagnostic(diagnostic: Any) -> None:
    if not isinstance(diagnostic, dict):
        return
    print(
        safe_text(
            "ROUTE_DIAGNOSTIC: requested_ide={requested_ide} target_worktree={target_worktree} identities={identities} matching_identities={matching_identities} projects={projects} matching_projects={matching_projects} reason={reason}",
            {
                "requested_ide": diagnostic.get("requested_ide"),
                "target_worktree": diagnostic.get("target_worktree"),
                "identities": diagnostic.get("discovered_identity_count"),
                "matching_identities": diagnostic.get("matching_identity_count"),
                "projects": diagnostic.get("discovered_project_count"),
                "matching_projects": diagnostic.get("matching_project_count"),
                "reason": diagnostic.get("reason"),
            },
        )
    )
    for identity in (diagnostic.get("identities") or [])[:5]:
        print(
            safe_text(
                "ROUTE_IDENTITY: ide={ide} product={product} port={port} projects={projects} plugin={plugin}",
                {
                    "ide": identity.get("ide_name"),
                    "product": identity.get("ide_product_code"),
                    "port": identity.get("port"),
                    "projects": identity.get("open_project_count"),
                    "plugin": plugin_identity_label(identity),
                },
            )
        )
    for project in (diagnostic.get("other_projects") or [])[:5]:
        print(
            safe_text(
                "ROUTE_OTHER_PROJECT: ide={ide} product={product} plugin={plugin} name={name} base_path={base_path}",
                {
                    "ide": project.get("ide_name"),
                    "product": project.get("ide_product_code"),
                    "name": project.get("project_name") or project.get("name"),
                    "base_path": project.get("base_path"),
                    "plugin": plugin_identity_label(project),
                },
            )
        )
    if diagnostic.get("next_action"):
        print(safe_text("ROUTE_NEXT_ACTION: {next_action}", {"next_action": diagnostic.get("next_action")}))


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
    code_home = os.environ.get("CODE_HOME") or os.environ.get("CODEX_HOME") or str(Path.home() / ".code")
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
    worktree = lifecycle_target_path(context)
    roots = trusted_auto_open_roots()
    if not worktree:
        raise InspectError("Cannot auto-open IDE because the worktree path is unknown.", 3)
    if not roots:
        raise InspectError(
            "Exact worktree is not open and no trusted auto-open roots are configured.",
            3,
            {
                "worktree_root": worktree,
                "lifecycle_target_path": lifecycle_target_path(context),
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
            "lifecycle_target_path": lifecycle_target_path(context),
            "trusted_auto_open_root_count": len(trusted),
            "global_config": str(global_config_path()),
            "hint": "Move the worktree under a trusted root, add a trusted root globally, or open/trust the project manually once.",
        },
    )


def ensure_jetbrains_trusted_locations(context: dict[str, Any]) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"status": "skipped", "reason": "unsupported_platform"}
    worktree = lifecycle_target_path(context)
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
                "lifecycle_target_path": lifecycle_target_path(context),
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
    worktree_path = Path(str(lifecycle_target_path(context))).expanduser().resolve()
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
    configured_dir = context.get("ide_config_dir")
    if configured_dir:
        path = Path(str(configured_dir)).expanduser().resolve()
        return [path] if path.exists() else []
    selection = resolve_ide_selection(context)
    if selection and selection.config_dir:
        return [selection.config_dir]
    candidates = discover_ide_config_candidates()
    candidate_paths = [candidate.path for candidate in candidates if candidate.path]
    ide = str(context.get("ide") or "")
    if ide:
        product = product_for_selector(ide)
        channel = normalize_ide_channel(context.get("ide_channel"))
        version = parse_version_tuple(clean_optional(context.get("ide_version")))
        match = select_ide_candidate(candidates, product, ide, channel, version, bool(version or channel == "eap" or selector_contains_exact_marker(ide)))
        if match and match.path:
            return [match.path]
        available = [candidate.name for candidate in sorted(candidates, key=lambda item: item.name)]
        raise InspectError(
            "Cannot seed JetBrains trusted locations because no installed IDE config matched the requested IDE.",
            3,
            {
                "ide": context.get("ide"),
                "ide_selection": selection.public() if selection else None,
                "available_config_dirs": available,
                "error_reason": "ide_config_missing",
                "next_action": "Launch the selected JetBrains IDE once, or update .github/github.json to name an installed JetBrains IDE/version.",
                "hint": "Use product-level metadata such as jetbrains.ide = WebStorm for latest stable, or exact metadata such as jetbrains.ideChannel = eap and jetbrains.ideVersion = 2026.2.",
                "matched_product": product.display_name if product else None,
            },
        )
    if len(candidate_paths) == 1:
        return candidate_paths
    raise InspectError(
        "Cannot seed JetBrains trusted locations because multiple IDE config directories exist and no IDE was selected.",
        3,
        {
            "available_config_dirs": [candidate.name for candidate in sorted(candidates, key=lambda item: item.name)],
            "error_reason": "ide_selection_required",
            "next_action": "Add preferred JetBrains IDE metadata to .github/github.json, for example jetbrains.ide = WebStorm, PyCharm, or IntelliJ IDEA. Use --ide only for a one-off run.",
            "hint": "Set jetbrains.ide in repo metadata so the helper updates the intended JetBrains product instead of guessing across installed IDEs.",
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
                                "hint": "Another lifecycle inspection is running. Wait for it to finish, increase --lifecycle-lock-timeout-ms, or run lifecycle inspections sequentially.",
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
        "lifecycle_target_path": lifecycle_target_path(context),
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
        "plugin_version": identity.get("plugin_version"),
        "plugin_build_fingerprint": identity.get("plugin_build_fingerprint"),
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
    exact_route_path = context.get("exact_route_path") or context.get("worktree_root")
    try:
        route_path = Path(str(route_base)).resolve() if route_base else None
        worktree_path = Path(str(exact_route_path)).resolve() if exact_route_path else None
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
    exact_route_path = context.get("exact_route_path") or context.get("worktree_root")
    if not route_base or not exact_route_path:
        raise InspectError("Cannot verify exact worktree route; route or worktree path is missing.", 3, {"route": route, "context": public_context(context)})
    try:
        route_path = Path(route_base).resolve()
        worktree_path = Path(exact_route_path).resolve()
    except OSError as error:
        raise InspectError(f"Cannot verify exact worktree route: {error}", 3, {"route": route, "context": public_context(context)}) from error
    if route_path != worktree_path:
        raise InspectError(
            "Lifecycle inspection requires the exact current worktree to be open in the IDE.",
            3,
            {"route_base_path": str(route_path), "worktree_root": str(worktree_path)},
        )


def open_in_ide(context: dict[str, Any], background: bool = False) -> None:
    ide_app = resolved_ide_app_name(context)
    ide_app_path = resolved_ide_app_path(context)
    if not (ide_app or ide_app_path) or sys.platform != "darwin":
        raise InspectError("Cannot auto-open IDE without a configured macOS IDE name.", 3)
    target = lifecycle_target_path(context)
    command = ["open"]
    if background:
        command.append("-g")
    if ide_app_path:
        command.extend(["-a", str(ide_app_path), str(target)])
    else:
        command.extend(["-a", str(ide_app), str(target)])
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
                "ide": context.get("ide"),
                "ide_app": ide_app,
                "ide_app_path": str(ide_app_path) if ide_app_path else None,
                "worktree_root": context.get("worktree_root"),
                "lifecycle_target_path": lifecycle_target_path(context),
                "background_open": background,
                "hint": "Check the configured JetBrains app name/path; product metadata uses the latest stable app, while exact EAP/version selection may require jetbrains.ideApp.",
            },
        )


def bootstrap_ide_app(context: dict[str, Any], background: bool = True) -> None:
    ide_app = resolved_ide_app_name(context)
    ide_app_path = resolved_ide_app_path(context)
    if not (ide_app or ide_app_path) or sys.platform != "darwin":
        raise InspectError("Cannot auto-open IDE without a configured macOS IDE name.", 3)
    command = ["open"]
    if background:
        command.extend(["-g", "-j"])
    if ide_app_path:
        command.extend(["-a", str(ide_app_path)])
    else:
        command.extend(["-a", str(ide_app)])
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise InspectError(
            "Failed to launch the JetBrains IDE for lifecycle open.",
            3,
            {
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout.strip(),
                "stderr": completed.stderr.strip(),
                "ide": context.get("ide"),
                "ide_app": ide_app,
                "ide_app_path": str(ide_app_path) if ide_app_path else None,
                "worktree_root": context.get("worktree_root"),
                "background_open": background,
                "hint": "Check the configured JetBrains app name/path; product metadata uses the latest stable app, while exact EAP/version selection may require jetbrains.ideApp.",
            },
        )


def resolved_ide_app_name(context: dict[str, Any]) -> str | None:
    selection = context.get("ide_selection") if isinstance(context.get("ide_selection"), dict) else {}
    return clean_optional(selection.get("app_name")) or clean_optional(context.get("ide_app")) or clean_optional(context.get("ide"))


def resolved_ide_app_path(context: dict[str, Any]) -> Path | None:
    selection = context.get("ide_selection") if isinstance(context.get("ide_selection"), dict) else {}
    value = selection.get("app_path") or context.get("ide_app_path")
    return Path(str(value)).expanduser() if value else None


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
