#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import errno
import fnmatch
import hashlib
import http.client
import importlib.util
import io
import json
import os
import plistlib
import re
import stat
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import redirect_stderr, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from xml.parsers.expat import ExpatError

if sys.platform == "win32":
    import msvcrt

    fcntl = None
else:
    import fcntl

    msvcrt = None


DEFAULT_PORT_RANGE = range(63340, 63350)
DEFAULT_TIMEOUT_SECONDS = 3.0
DEFAULT_WAIT_TIMEOUT_MS = 120_000
DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS = 300_000
DEFAULT_OUTCOME_ROUTING_LOCK_TIMEOUT_MS = 300_000
DEFAULT_OUTCOME_APPEND_LOCK_TIMEOUT_MS = 300_000
LOOPBACK_HOST = "127.0.0.1"
REDACTED = "<redacted>"
UNKNOWN_LOG_ENV = "JB_INSPECT_UNKNOWN_LOG"
OUTCOME_LOG_ENV = "JB_INSPECT_OUTCOME_LOG"
LIFECYCLE_OWNERSHIP_PROTOCOL = "lease_bound_v1"
OUTCOME_LOG_SCHEMA_VERSION = 2
AGENT_RESULT_SCHEMA_VERSION = 1
INSPECTION_LANE_SCHEMA_VERSION = 1
REPOSITORY_PREPARATION_SOURCE = "qualityGate.inspection.prepare"
REPOSITORY_PREPARATION_NOT_CONFIGURED = "not_configured"
REPOSITORY_PREPARATION_NOT_RUN = "not_run"
MAX_REPOSITORY_PREPARATION_COMMAND_LENGTH = 1024
MAX_REPOSITORY_PREPARATION_GENERATED_STATE_PATHS = 25
MAX_REPOSITORY_PREPARATION_TEST_ROOTS = 10
MAX_REPOSITORY_PREPARATION_EXTRAS = 10
MAX_REPOSITORY_PREPARATION_PEP723_SCRIPTS = 25
REPOSITORY_PREPARATION_RECEIPT_DIRNAME = "repository-preparation"
INSPECTION_LANE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
AGENT_USAGE_ERROR_REASON = "helper_usage_error"
AGENT_USAGE_NEXT_ACTION = (
    "Use only documented agent-inspect arguments, report this compatibility failure, "
    "and do not run another inspection command."
)
POTENTIAL_OPEN_LEASE_STATES = frozenset({"open_requesting", "open_registered", "cleanup_pending"})
SETTLED_LEASE_STATES = frozenset(
    {"cleanup_pending", "prepared", "kept_warm_after_indexing_timeout", "cleanup_failed", "cleanup_skipped"}
)
FAILURE_REASONS_RAISED_BEFORE_OPEN_REQUEST = frozenset(
    {"timeout", "connectionreseterror", "identity_port_mismatch", "invalid_identity_port", "inspection_api_unavailable"}
)
VERDICT_SOURCE_KEYS = (
    "inspection_verdict",
    "inspection_verdict_reason",
    "inspection_verdict_message",
    "inspection_verdict_next_action",
    "inspection_attribution",
    "proof_failures",
    "inspection_proof",
)
INSPECTION_STAGE_EVIDENCE_KEYS = (
    "inspection_stage",
    "inspection_stage_elapsed_ms",
    "inspection_run_elapsed_ms",
    "inspection_stage_history",
    "inspection_terminal_outcome",
    "inspection_failure_diagnostic",
    "inspection_failure_history",
)
SENSITIVE_KEY_PARTS = ("token", "secret", "password", "credential", "authorization")
SENSITIVE_OUTPUT_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(token|secret|password|credential|authorization|api[_-]?key)\b(\s*[:=]\s*)([^\s,;]+)"
)
SENSITIVE_OUTPUT_BEARER_PATTERN = re.compile(r"(?i)(\bBearer\s+)([^\s,;]+)")
DURABLE_POSIX_PATH_ROOTS = frozenset(
    {
        "Applications",
        "Library",
        "System",
        "Users",
        "Volumes",
        "bin",
        "dev",
        "etc",
        "home",
        "lib",
        "lib64",
        "media",
        "mnt",
        "nix",
        "opt",
        "private",
        "proc",
        "root",
        "run",
        "sbin",
        "srv",
        "sys",
        "tmp",
        "usr",
        "var",
        "workspace",
        "workspaces",
    }
)
DURABLE_QUOTED_PATH_PATTERN = re.compile(
    r'''(?P<quote>["'])(?P<path>(?:file://(?:localhost)?/|(?:path|file):/|[A-Za-z]:[\\/]|\\\\|~/|/(?!/))[^"'\r\n]+)(?P=quote)'''
)
DURABLE_SPACED_FILE_PATH_PATTERN = re.compile(
    r'''(?<![A-Za-z0-9._/\\-])(?P<path>
        (?:file://(?:localhost)?/|(?:path|file):/|[A-Za-z]:[\\/]|\\\\|~/|/(?!/))
        (?:[^\\/"'<>\r\n|,;!?]+[\\/])+
        [^\\/"'<>\r\n|,;!?]*?\.[A-Za-z0-9][A-Za-z0-9._-]{0,15}
        (?::\d+(?::\d+)*)?
    )''',
    re.VERBOSE,
)
DURABLE_SPACED_PATH_TAIL_PATTERN = re.compile(
    rf'''(?<![A-Za-z0-9._/\\-])(?P<path>
        (?:
            file://(?:localhost)?/
            |(?:path|file):/
            |[A-Za-z]:[\\/]
            |\\\\
            |~/
            |/(?:{"|".join(re.escape(root) for root in sorted(DURABLE_POSIX_PATH_ROOTS))})(?:/|(?=\s|$))
        )
        [^"'<>\r\n|]+
    )''',
    re.VERBOSE,
)
DURABLE_PATH_TOKEN_PATTERN = re.compile(
    r'''(?<![A-Za-z0-9._/\\-])(?P<path>(?:file://(?:localhost)?/|(?:path|file):/|[A-Za-z]:[\\/]|\\\\|~/|/(?!/))[^\s"'<>|]+)'''
)
_HELPER_REVISION: str | None = None
_ACTIVE_CLIENT_RUN_ID: str | None = None


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
    aliases: tuple[str, ...]
    launcher_names: tuple[str, ...]
    env_var_base_name: str

    @property
    def vm_options_name(self) -> str:
        return f"{self.launcher_names[0]}.vmoptions"


@dataclass(frozen=True)
class IdeCandidate:
    product_key: str
    name: str
    path: Path | None
    version: tuple[int, ...]
    channel: str
    source: str


@dataclass(frozen=True)
class InspectionLane:
    lane_id: str
    ide: str
    required: bool
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    project_path: str | None

    def public(self) -> dict[str, Any]:
        payload = {
            "id": self.lane_id,
            "ide": self.ide,
            "required": self.required,
            "include": list(self.include),
            "exclude": list(self.exclude),
        }
        if self.project_path is not None:
            payload["projectPath"] = self.project_path
        return payload


IDE_PRODUCTS = {
    "intellij": IdeProduct(
        key="intellij",
        display_name="IntelliJ IDEA",
        config_prefixes=("IntelliJIdea", "IdeaIC"),
        product_codes=("IU", "IC"),
        aliases=("intellijidea", "intellij", "idea", "iu", "ic"),
        launcher_names=("idea",),
        env_var_base_name="IDEA",
    ),
    "pycharm": IdeProduct(
        key="pycharm",
        display_name="PyCharm",
        config_prefixes=("PyCharm",),
        product_codes=("PY", "PC"),
        aliases=("pycharm", "pycharmce", "py", "pc"),
        launcher_names=("pycharm",),
        env_var_base_name="PYCHARM",
    ),
    "webstorm": IdeProduct(
        key="webstorm",
        display_name="WebStorm",
        config_prefixes=("WebStorm",),
        product_codes=("WS",),
        aliases=("webstorm", "ws"),
        launcher_names=("webstorm",),
        env_var_base_name="WEBIDE",
    ),
}
ALL_IDE_LAUNCHER_NAMES = tuple(name for product in IDE_PRODUCTS.values() for name in product.launcher_names)
IDE_PRODUCT_BY_ALIAS = {
    alias: product
    for product in IDE_PRODUCTS.values()
    for alias in product.aliases
}


INSPECT_COMMAND_ALIASES = frozenset({"agent-inspect", "inspect", "inspect-closeout"})
RETIRED_COMMAND_MESSAGE = (
    "{command} belonged to the retired desktop IDE route. agent-inspect prepares and inspects a private snapshot "
    "on an isolated native worker, so no separate open or status step is needed."
)
RETIRED_COMMANDS = frozenset({"open-worktree", "prepare-worktree", "prepare"})
RETIRED_OPTION_MESSAGE = (
    "{option} belonged to the retired desktop IDE route; agent-inspect runs only isolated native workers "
    "(see references/native-backend.md)."
)
RETIRED_OPTIONS = ("--ide", "--ide-channel", "--ide-version", "--prepare-timeout-ms")
ISOLATED_BACKEND_UNAVAILABLE_HINT = (
    "Configure the isolated native worker on macOS (jetbrains.nativeWorkers.pycharm, .webstorm or .intellij in the "
    "shared helper config; see references/native-backend.md) and select files it supports. Keep unsupported coverage UNKNOWN."
)


class RetiredOption(argparse.Action):
    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace, values: Any, option_string: str | None = None) -> None:
        parser.error(RETIRED_OPTION_MESSAGE.format(option=option_string))


def main() -> int:
    global _ACTIVE_CLIENT_RUN_ID
    previous_client_run_id = _ACTIVE_CLIENT_RUN_ID
    raw_argv = list(sys.argv[1:])
    command_index = command_token_index(raw_argv)
    command = raw_argv[command_index] if command_index is not None else None
    parser = build_parser()
    if command in RETIRED_COMMANDS:
        print(RETIRED_COMMAND_MESSAGE.format(command=command), file=sys.stderr)
        return 2
    if command_index is not None and command in INSPECT_COMMAND_ALIASES:
        parser_error = io.StringIO()
        try:
            with redirect_stderr(parser_error):
                args = parser.parse_args([*raw_argv[:command_index], "agent-inspect", *raw_argv[command_index + 1:]])
        except SystemExit as error:
            if error.code == 0:
                raise
            return emit_agent_usage_error(parser_error.getvalue())
    else:
        args = parser.parse_args(raw_argv)
    args.client_run_id = str(uuid.uuid4())
    _ACTIVE_CLIENT_RUN_ID = args.client_run_id
    try:
        if args.command == "agent-inspect":
            return run_agent_inspect(args)
        if args.command == "cleanup-helper-leases":
            result = command_cleanup_leases(args)
            # Lease cleanup is not an inspection; its exit code and per-lease reasons carry the outcome.
            return emit(result, args.json, classify_cleanup_leases_exit(result), args.command)
        if args.command in {"remove-worktree", "cleanup-helper-sdks"}:
            return emit(command_retire_sdks(args), args.json, 0, args.command)
        if args.command == "summarize-outcomes":
            return emit(command_summarize_outcomes(args), args.json, 0, args.command)
    except InspectError as error:
        return emit(error_payload(error, args), getattr(args, "json", False), error.exit_code, args.command)
    finally:
        _ACTIVE_CLIENT_RUN_ID = previous_client_run_id
    return 2


def command_token_index(argv: list[str]) -> int | None:
    for index, token in enumerate(argv):
        if token == "--":
            return None
        if not token.startswith("-"):
            return index
    return None


def classify_cleanup_leases_exit(result: dict[str, Any]) -> int:
    return 1 if result.get("failed") or result.get("unresolved") else 0


def run_agent_inspect(args: argparse.Namespace) -> int:
    try:
        selection = "explicit"
        if args.backend == "auto":
            native_backend = native_backend_module()
            backend, selection = native_backend.select_backend(args, sys.modules[__name__])
            if backend != "native":
                native_backend.common.scope_selection(args, sys.modules[__name__])
                raise InspectError(
                    "Automatic inspection has no isolated native worker for this selection.",
                    payload={"error_reason": "isolated_backend_unavailable", "backend_selection": selection,
                             "hint": ISOLATED_BACKEND_UNAVAILABLE_HINT},
                )
        result = command_native(args)
        result["backend_selection"] = selection
        if not args.native_recover_only:
            log_assessment(result)
    except InspectError as error:
        payload = error_payload(error, args)
        reason = str(payload["error_reason"])
        next_action = payload.get("hint") or payload.get("next_action") or "Resolve the reported prerequisite before a fresh assessment."
        result = unknown_agent_result(reason, str(error), str(next_action),
                                      {key: value for key, value in payload.items()
                                       if key not in {"status", "hint", "next_action", "error", "command", "exit_code"}})
        log_assessment(result)
    except Exception as error:
        result = unknown_agent_result(normalize_reason(error.__class__.__name__), str(error) or error.__class__.__name__,
                                      "Report this helper failure with its diagnostic; do not run another inspection command.", bucket="tool_bug")
        log_assessment(result)
    print(public_json(result))
    return 0


def unknown_agent_result(reason: str, message: str, next_action: str, diagnostic: dict[str, Any] | None = None,
                         bucket: str = "environment_blocked") -> dict[str, Any]:
    return {
        "schema_version": AGENT_RESULT_SCHEMA_VERSION, "command": "agent-inspect", "status": "error",
        "agent_result": {"verdict": "UNKNOWN", "bucket": bucket, "terminal": True,
                         "retry_policy": {"retry": False, "max_attempts": 0, "wait_ms": 0},
                         "next_action": next_action, "agent_report": f"UNKNOWN: {reason}; {message}"},
        "inspection_outcome": {"verdict": "UNKNOWN", "reason": reason},
        "diagnostic": {"error_reason": reason, "error_message": message, **(diagnostic or {})},
    }


def native_backend_module() -> Any:
    spec = importlib.util.spec_from_file_location("jb_native_backend", Path(__file__).with_name("native_backend.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("Native backend unavailable")
    backend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backend)
    return backend


def command_native(args: argparse.Namespace) -> dict[str, Any]:
    return native_backend_module().run(args, sys.modules[__name__])


def log_assessment(result: dict[str, Any]) -> None:
    agent_result = dict_field(result, "agent_result")
    outcome = dict_field(result, "inspection_outcome")
    diagnostic = dict_field(result, "diagnostic")
    verdict = agent_result.get("verdict") or outcome.get("verdict")
    started = verdict in {"GREEN", "RED"} or "engine_exit_code" in dict_field(diagnostic, "native")
    payload = {
        **result,
        "client_run_id": result.get("client_run_id") or _ACTIVE_CLIENT_RUN_ID,
        "verdict": verdict,
        "bucket": agent_result.get("bucket"),
        "verdict_reason": outcome.get("reason"),
        "verdict_next_action": agent_result.get("next_action"),
        "retry_policy": agent_result.get("retry_policy"),
        "inspection_started": started,
        "total_problems": result.get("finding_count") if started else None,
    }
    log_assessment_records(payload)
    if isinstance(result.get("diagnostic"), dict):
        diagnostic.update({key: value for key, value in payload.items() if key.endswith(("_log_path", "_log_error"))})


def error_payload(error: InspectError, args: argparse.Namespace | None = None) -> dict[str, Any]:
    payload = dict(error.payload)
    payload["status"] = "error"
    payload.setdefault("error", str(error))
    payload.setdefault("error_message", str(error))
    payload.setdefault("error_reason", infer_error_reason(error, payload))
    payload.setdefault("exit_code", error.exit_code)
    if args is not None:
        payload.setdefault("command", args.command)
        payload.setdefault("client_run_id", getattr(args, "client_run_id", None))
    return payload


def emit_agent_usage_error(parser_error: str) -> int:
    lines = [line.strip() for line in parser_error.splitlines() if line.strip()]
    message = lines[-1] if lines else "The helper rejected the agent-inspect arguments."
    if ": error: " in message:
        message = message.split(": error: ", 1)[1]
    hint = None
    if message.startswith("unrecognized arguments: --file "):
        hint = ('If --file options were assembled in one shell string, the shell may have passed it as one argument. '
                'Use an array, for example args=(--file "a.py" --file "b.py"), then "${args[@]}".')
    result = unknown_agent_result(AGENT_USAGE_ERROR_REASON, message, AGENT_USAGE_NEXT_ACTION,
                                  {"hint": hint} if hint else None, bucket="tool_bug")
    result["usage_error"] = True
    result["client_run_id"] = str(uuid.uuid4())
    log_assessment(result)
    print(public_json(result))
    return 0


def normalize_reason(value: Any) -> str:
    reason = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return reason or "inspection_helper_error"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run JetBrains inspections on isolated native workers and retire helper-owned IDE state.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON only.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    agent = subparsers.add_parser("agent-inspect", help="Inspect once on an isolated native worker and emit a compact terminal result envelope.")
    agent.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Accepted for compatibility; output is always JSON.")
    agent.add_argument("--repo", default=".", help="Repo/worktree path to inspect. Defaults to cwd.")
    agent.add_argument("--ide-app", help="Exact macOS application bundle for the native worker; defaults to the configured worker app.")
    agent.add_argument("--scope", help="Inspection scope. Defaults from repo config or changed_files.")
    agent.add_argument("--dir", dest="directory", help="Directory for directory scope.")
    agent.add_argument("--file", dest="files", action="append", default=[], help="File for files scope; repeatable.")
    agent.add_argument("--include-unversioned", action=argparse.BooleanOptionalAction, default=True)
    agent.add_argument("--changed-files-mode", choices=("all", "staged", "unstaged"), default="all")
    agent.add_argument("--max-files", type=int)
    agent.add_argument("--profile", default="")
    agent.add_argument("--timeout-ms", type=int, default=DEFAULT_WAIT_TIMEOUT_MS)
    agent.add_argument("--lifecycle-lock-timeout-ms", type=int, default=DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS)
    agent.add_argument("--severity", default="all")
    agent.add_argument("--problem-type", default="all")
    agent.add_argument("--file-pattern", default="all")
    agent.add_argument("--offset", type=int, default=0)
    agent.add_argument("--include-stale", "--allow-stale", dest="include_stale", action="store_true",
                       help="Return cached stale findings for diagnostics. Stale results still exit non-zero.")
    agent.add_argument("--allow-text-only-coverage", action="store_true",
                       help="Allow TextMate/PlainText-only scoped files instead of failing semantic coverage closed.")
    agent.add_argument("--backend", choices=("auto", "native"), default="auto", help="auto selects an eligible isolated native worker or returns UNKNOWN.")
    agent.add_argument("--native-worker", help="Independent Director-activated worker directory; config and credentials remain in place.")
    agent.add_argument("--native-recover-only", action="store_true", help="Clean only the journaled native run and exact helper SDK, without inspection or clearing activation stops.")
    agent.add_argument("--native-reconcile-result", help="Saved terminal native result for the same interrupted run; requires --native-recover-only and never clears licensing stops.")
    agent.add_argument("--native-release-run", metavar="UUID", help="Release only this recovered run's helper metadata after recorded Director UI confirmation; requires --native-recover-only and never starts inspection.")
    agent.add_argument("--native-framework-root", action="append", default=[], help="Existing Odoo framework source root, including odoo/release.py; repeat for explicit roots.")
    for option in RETIRED_OPTIONS:
        agent.add_argument(option, action=RetiredOption, help=argparse.SUPPRESS)

    for name in ("remove-worktree", "cleanup-helper-sdks"):
        command = subparsers.add_parser(name, help="Retire SDKs and caches the retired desktop route registered; contacts only an already-running IDE.")
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        command.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
        command.add_argument("--lifecycle-lock-timeout-ms", type=int, default=DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS)
    subparsers.choices["cleanup-helper-sdks"].add_argument("--worktree-path", action="append", help="Exact orphan path from the reviewed dry-run; repeat for each path to apply.")
    subparsers.choices["remove-worktree"].add_argument("--repo", required=True, help="Exact eligible linked worktree to remove; establish cleanup disposition first.")
    subparsers.choices["remove-worktree"].add_argument("--offline-only", action="store_true", help="Use only local cleanup proof without discovering or contacting an IDE; retain the worktree if proof is unavailable.")
    leases = subparsers.add_parser("cleanup-helper-leases", help="Remove stale lifecycle leases the retired desktop route left behind.")
    leases.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    leases.add_argument("--max-age-ms", type=int, default=24 * 60 * 60 * 1000)
    leases.add_argument("--lease-id", action="append", metavar="UUID", help="Limit cleanup to one exact stale lease; specify this option only once.")
    leases.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    leases.add_argument("--lifecycle-lock-timeout-ms", type=int, default=DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS)
    outcomes = subparsers.add_parser("summarize-outcomes", help="Summarize helper outcome JSONL logs without inspecting.")
    outcomes.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    outcomes.add_argument("--log", dest="log_path", help="Outcome JSONL log path. Defaults to JB_INSPECT_OUTCOME_LOG or the standard Code log path.")
    outcomes.add_argument("--limit", type=int, default=10, help="Maximum number of recent events to include.")
    return parser


def log_assessment_records(payload: dict[str, Any]) -> None:
    unknown_enabled = payload.get("verdict") == "UNKNOWN"
    outcome_enabled = payload.get("usage_error") is not True
    try:
        with outcome_routing_lock():
            for enabled, path, key, record in (
                (unknown_enabled, unknown_log_path(), "unknown_log", unknown_log_record),
                (outcome_enabled, outcome_log_path(), "outcome_log", outcome_log_record),
            ):
                if not enabled or path is None:
                    continue
                try:
                    append_jsonl_record(path, record(payload))
                    payload[f"{key}_path"] = str(path)
                except OSError as error:
                    payload[f"{key}_error"] = str(error)
    except OSError as error:
        payload["outcome_log_error"] = str(error)


def outcome_record_base(payload: dict[str, Any]) -> dict[str, Any]:
    public = public_payload(payload)
    timestamp_ms = int(time.time() * 1000)
    return {
        "schema_version": OUTCOME_LOG_SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "event_kind": "inspection_assessment",
        "assessment_id": public.get("client_run_id"),
        "client_run_id": public.get("client_run_id"),
        "timestamp": utc_timestamp(timestamp_ms),
        "timestamp_ms": timestamp_ms,
        "command": public.get("command") or "agent-inspect",
        "backend": public.get("backend"),
        "backend_selection": public.get("backend_selection"),
        "verdict": public.get("verdict"),
        "bucket": public.get("bucket"),
        "verdict_reason": public.get("verdict_reason"),
        "status": public.get("status"),
        "scope": public.get("scope"),
        "repo_head_sha": dict_field(dict_field(public, "diagnostic"), "native").get("repo_head_sha"),
        "helper_revision": helper_revision(),
        "inspection_started": public.get("inspection_started"),
        "inspection_run_id": dict_field(public, "identity").get("inspection_run_id"),
        "cleanup_status": dict_field(public, "cleanup").get("status"),
        "total_problems": public.get("total_problems"),
        "preexisting_finding_count": public.get("preexisting_finding_count"),
        "advisory_finding_count": public.get("advisory_finding_count"),
        "proof_failure_count": len(public["proof_failures"]) if isinstance(public.get("proof_failures"), list) else None,
        "uncovered_inspection_count": (
            len(public["uncovered_inspections"]) if isinstance(public.get("uncovered_inspections"), list) else None
        ),
        "retry": dict_field(public, "retry_policy").get("retry"),
        "inspection_outcome": public.get("inspection_outcome"),
    }


def unknown_log_record(payload: dict[str, Any]) -> dict[str, Any]:
    record = outcome_record_base(payload)
    record["verdict_next_action"] = payload.get("verdict_next_action")
    return redact_durable_log({key: value for key, value in record.items() if value not in (None, {}, [])})


def outcome_log_record(payload: dict[str, Any]) -> dict[str, Any]:
    record = outcome_record_base(payload)
    agent_result = dict_field(payload, "agent_result")
    record.update(exit_code=0, next_action=agent_result.get("next_action"), agent_report=agent_result.get("agent_report"))
    return redact_durable_log({key: value for key, value in record.items() if value not in (None, {}, [])})


def command_summarize_outcomes(args: argparse.Namespace) -> dict[str, Any]:
    log_path = Path(args.log_path).expanduser().resolve() if getattr(args, "log_path", None) else outcome_log_path()
    if log_path is None:
        return {"status": "disabled", "message": f"Outcome logging is disabled by {OUTCOME_LOG_ENV}.", "events": 0, "invalid_lines": 0}
    return summarize_outcome_log(log_path, limit=max(0, int(getattr(args, "limit", 10))))


def emit(payload: dict[str, Any], json_only: bool, exit_code: int, command: str) -> int:
    payload = public_payload({**payload, "command": command})
    if json_only:
        print(public_json(payload))
        return exit_code
    if "stale" in payload and "removed" in payload:
        mode = "PREVIEW" if payload.get("dry_run") else "APPLIED"
        print(f"LEASE_CLEANUP: {mode} stale={len(payload['stale'])} "
              f"would_remove={len(payload.get('would_remove') or [])} removed={len(payload['removed'])} "
              f"unresolved={len(payload.get('unresolved') or [])} failed={len(payload.get('failed') or [])}")
        for item in [*(payload.get("unresolved") or []), *(payload.get("failed") or [])]:
            if isinstance(item, dict):
                next_action = item.get("next_action") or item.get("message")
                print(safe_text("LEASE_UNRESOLVED: lease_id={lease_id} reason={reason}"
                                + (" next_action={next_action}" if next_action else ""), {
                    "lease_id": item.get("lease_id"),
                    "reason": item.get("reason"),
                    "next_action": next_action,
                }))
        if payload.get("dry_run"):
            print("No leases changed. Apply with cleanup-helper-leases --no-dry-run; retain any unresolved leases.")
        return exit_code
    if payload.get("status") == "error":
        print(safe_text("ERROR: reason={reason} message={message} command={command} exit_code={exit_code}", {
            "reason": payload.get("error_reason"), "message": payload.get("error_message"),
            "command": command, "exit_code": exit_code,
        }))
        if payload.get("hint"):
            print(safe_text("HINT: {hint}", {"hint": payload.get("hint")}))
        return exit_code
    print(public_json(payload))
    return exit_code


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


def parse_repository_preparation(inspection: dict[str, Any]) -> dict[str, Any]:
    if "prepare" not in inspection:
        return {
            "configured": False,
            "command": None,
            "source": REPOSITORY_PREPARATION_SOURCE,
            "execution_state": REPOSITORY_PREPARATION_NOT_CONFIGURED,
        }

    raw_prepare = inspection.get("prepare")
    if isinstance(raw_prepare, dict):
        return parse_structured_repository_preparation(raw_prepare, inspection)
    if not isinstance(raw_prepare, str):
        raise repository_preparation_config_error(
            "qualityGate.inspection.prepare must be a non-empty command string or supported preparation object when configured."
        )
    command = raw_prepare.strip()
    if not command:
        raise repository_preparation_config_error(
            "qualityGate.inspection.prepare must be a non-empty command string when configured."
        )
    if len(command) > MAX_REPOSITORY_PREPARATION_COMMAND_LENGTH:
        raise repository_preparation_config_error(
            "qualityGate.inspection.prepare exceeds the bounded command length."
        )
    if any(ord(character) < 32 and character not in {"\t"} for character in command):
        raise repository_preparation_config_error(
            "qualityGate.inspection.prepare must not contain control characters."
        )
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as error:
        raise repository_preparation_config_error(
            f"qualityGate.inspection.prepare is not a valid shell-style command: {error}"
        ) from error
    if not argv:
        raise repository_preparation_config_error(
            "qualityGate.inspection.prepare must contain at least one command argument."
        )
    generated_state = normalize_repository_preparation_paths(
        inspection.get("requiredGeneratedState", inspection.get("required_generated_state", [])),
        field_name="qualityGate.inspection.requiredGeneratedState",
        maximum=MAX_REPOSITORY_PREPARATION_GENERATED_STATE_PATHS,
    )
    return {
        "configured": True,
        "command": command,
        "source": REPOSITORY_PREPARATION_SOURCE,
        "execution_state": REPOSITORY_PREPARATION_NOT_RUN,
        "required_generated_state": generated_state,
        "_argv": argv,
    }


def parse_structured_repository_preparation(
    raw_prepare: dict[str, Any],
    inspection: dict[str, Any],
) -> dict[str, Any]:
    if set(raw_prepare) != {"python"} or not isinstance(raw_prepare.get("python"), dict):
        raise repository_preparation_config_error(
            "Structured qualityGate.inspection.prepare must contain exactly one 'python' object."
        )
    if "requiredGeneratedState" in inspection or "required_generated_state" in inspection:
        raise repository_preparation_config_error(
            "Structured preparation must place requiredGeneratedState inside qualityGate.inspection.prepare.python."
        )
    python = raw_prepare["python"]
    allowed_keys = {
        "version",
        "moduleName",
        "testRoots",
        "sync",
        "extras",
        "pep723Scripts",
        "projectPath",
        "requiredGeneratedState",
    }
    unknown_keys = sorted(set(python) - allowed_keys)
    if unknown_keys:
        raise repository_preparation_config_error(
            f"Structured Python preparation contains unsupported fields: {', '.join(unknown_keys)}."
        )
    project_path = "."
    if "projectPath" in python:
        try:
            project_path = parse_inspection_lane_project_path(python["projectPath"], "Python preparation")
        except InspectError as error:
            raise repository_preparation_config_error(
                "prepare.python.projectPath must be a safe relative project directory."
            ) from error
        if project_path is None:
            raise repository_preparation_config_error("Python preparation projectPath must name a relative directory.")
    version = python.get("version")
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+", version) is None:
        raise repository_preparation_config_error(
            "qualityGate.inspection.prepare.python.version must use major.minor form, for example '3.13'."
        )
    module_name = python.get("moduleName")
    if (
        not isinstance(module_name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", module_name) is None
    ):
        raise repository_preparation_config_error(
            "qualityGate.inspection.prepare.python.moduleName must contain only letters, numbers, '-' or '_'."
        )
    test_roots = normalize_repository_preparation_paths(
        python.get("testRoots", []),
        field_name="qualityGate.inspection.prepare.python.testRoots",
        maximum=MAX_REPOSITORY_PREPARATION_TEST_ROOTS,
    )
    generated_state = normalize_repository_preparation_paths(
        python.get("requiredGeneratedState", []),
        field_name="qualityGate.inspection.prepare.python.requiredGeneratedState",
        maximum=MAX_REPOSITORY_PREPARATION_GENERATED_STATE_PATHS,
    )
    sync = python.get("sync", False)
    if not isinstance(sync, bool):
        raise repository_preparation_config_error(
            "qualityGate.inspection.prepare.python.sync must be a boolean."
        )
    scripts = normalize_repository_preparation_paths(
        python.get("pep723Scripts", []),
        field_name="qualityGate.inspection.prepare.python.pep723Scripts",
        maximum=MAX_REPOSITORY_PREPARATION_PEP723_SCRIPTS,
    )
    if scripts and sync:
        raise repository_preparation_config_error(
            "qualityGate.inspection.prepare.python.pep723Scripts and sync are separate dependency sources."
        )
    extras = python.get("extras", [])
    if not isinstance(extras, list) or len(extras) > MAX_REPOSITORY_PREPARATION_EXTRAS:
        raise repository_preparation_config_error(
            "qualityGate.inspection.prepare.python.extras must be a bounded array."
        )
    normalized_extras: list[str] = []
    for extra in extras:
        if not isinstance(extra, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", extra) is None:
            raise repository_preparation_config_error(
                "qualityGate.inspection.prepare.python.extras must contain valid package extra names."
            )
        normalized_extras.append(extra)
    if normalized_extras and not sync:
        raise repository_preparation_config_error(
            "qualityGate.inspection.prepare.python.extras requires sync=true."
        )
    script = Path(__file__).resolve().parent / "prepare-python-project.py"
    argv = [
        "uv",
        "run",
        str(script),
        "--repo",
        project_path,
        "--python",
        version,
        "--module-name",
        module_name,
    ]
    for test_root in test_roots:
        argv.extend(("--test-root", test_root))
    if sync:
        argv.append("--sync")
    for extra in normalized_extras:
        argv.extend(("--extra", extra))
    for script_path in scripts:
        argv.extend(("--pep723-script", script_path))
    validate_script_preparation_command(argv)
    generated_state = list(dict.fromkeys([
        *generated_state, ".venv", f".idea/{module_name}.iml", ".idea/modules.xml", ".idea/misc.xml",
    ]))
    generated_state = [(Path(project_path) / path).as_posix() for path in generated_state]
    return {
        "configured": True,
        "command": shlex.join(argv),
        "source": REPOSITORY_PREPARATION_SOURCE,
        "execution_state": REPOSITORY_PREPARATION_NOT_RUN,
        "kind": "python",
        "required_generated_state": generated_state,
        "_argv": argv,
    }


def validate_script_preparation_command(argv: list[str]) -> None:
    if "--pep723-script" in argv and len(shlex.join(argv)) > MAX_REPOSITORY_PREPARATION_COMMAND_LENGTH:
        error = repository_preparation_config_error(
            "The PEP 723 preparation command exceeds the command reporting limit. "
            "Choose fewer representative scripts covering the inspected dependencies, or shorten their paths, "
            "so the complete manual rerun command, including the selected IDE SDK-table path, can be reported."
        )
        error.payload["next_action"] = (
            "Reduce the representative script set or shorten its paths; the selected IDE SDK-table path "
            "also counts toward the command reporting bound."
        )
        raise error


def normalize_repository_preparation_paths(
    generated_state: Any,
    *,
    field_name: str,
    maximum: int,
) -> list[str]:
    if generated_state is None:
        generated_state = []
    if not isinstance(generated_state, list) or len(generated_state) > maximum:
        raise repository_preparation_config_error(
            f"{field_name} must be a bounded array of relative paths."
        )
    normalized_generated_state: list[str] = []
    for value in generated_state:
        if not isinstance(value, str) or not value.strip():
            raise repository_preparation_config_error(
                f"{field_name} must contain non-empty relative paths."
            )
        path = Path(value.strip())
        if path.is_absolute() or ".." in path.parts:
            raise repository_preparation_config_error(
                f"{field_name} paths must stay inside the target worktree."
            )
        normalized_generated_state.append(path.as_posix())
    return normalized_generated_state


def repository_preparation_config_error(message: str) -> InspectError:
    return InspectError(
        message,
        2,
        {
            "error_reason": "repository_preparation_config_invalid",
            "failure_phase": "configuration",
            "repository_preparation": {
                "configured": False,
                "command": None,
                "source": REPOSITORY_PREPARATION_SOURCE,
                "execution_state": REPOSITORY_PREPARATION_NOT_CONFIGURED,
                "configuration_status": "invalid",
            },
            "next_action": (
                "Fix qualityGate.inspection.prepare in .github/github.json so it is a valid non-empty command, "
                "then rerun the inspection command."
            ),
        },
    )


def repository_preparation_target(context: dict[str, Any]) -> Path:
    value = context.get("worktree_root") or context.get("repo_path")
    if not isinstance(value, str) or not value.strip():
        raise InspectError(
            "Repository preparation cannot determine the exact target worktree.",
            3,
            {"error_reason": "repository_preparation_untrusted", "failure_phase": "preparation_policy"},
        )
    return Path(value).expanduser().resolve()


def repository_preparation_receipt_path(context: dict[str, Any]) -> Path:
    target = repository_preparation_target(context)
    return cache_dir() / REPOSITORY_PREPARATION_RECEIPT_DIRNAME / f"{stable_value_hash(str(target))}.json"


def parse_inspection_lanes(inspection: dict[str, Any]) -> tuple[InspectionLane, ...]:
    raw_lanes = inspection.get("lanes")
    if raw_lanes is None:
        return ()
    if not isinstance(raw_lanes, list) or not raw_lanes:
        raise inspection_lane_config_error("qualityGate.inspection.lanes must be a non-empty array when configured.")

    lanes: list[InspectionLane] = []
    lane_ids: set[str] = set()
    for index, raw_lane in enumerate(raw_lanes):
        if not isinstance(raw_lane, dict):
            raise inspection_lane_config_error(f"Inspection lane at index {index} must be an object.")
        raw_lane_id = raw_lane.get("id")
        lane_id = clean_optional(raw_lane_id) if isinstance(raw_lane_id, str) else None
        if lane_id is None or INSPECTION_LANE_ID_PATTERN.fullmatch(lane_id) is None:
            raise inspection_lane_config_error(
                f"Inspection lane at index {index} must have an id containing only letters, numbers, '.', '_', or '-'."
            )
        if lane_id in lane_ids:
            raise inspection_lane_config_error(f"Inspection lane id is duplicated: {lane_id}")
        lane_ids.add(lane_id)

        raw_ide = raw_lane.get("ide")
        ide = clean_optional(raw_ide) if isinstance(raw_ide, str) else None
        if ide is None:
            raise inspection_lane_config_error(f"Inspection lane {lane_id} must name an ide.")
        required = raw_lane.get("required", True)
        if not isinstance(required, bool):
            raise inspection_lane_config_error(f"Inspection lane {lane_id} required must be true or false.")
        include = parse_inspection_lane_patterns(raw_lane.get("include"), lane_id, "include", required=True)
        exclude = parse_inspection_lane_patterns(raw_lane.get("exclude", []), lane_id, "exclude", required=False)
        project_path = parse_inspection_lane_project_path(raw_lane.get("projectPath"), lane_id)
        unsupported = sorted(set(raw_lane) - {"id", "ide", "required", "include", "exclude", "projectPath"})
        if unsupported:
            raise inspection_lane_config_error(
                f"Inspection lane {lane_id} has unsupported fields: {', '.join(unsupported)}"
            )
        lanes.append(
            InspectionLane(
                lane_id=lane_id,
                ide=ide,
                required=required,
                include=include,
                exclude=exclude,
                project_path=project_path,
            )
        )
    return tuple(lanes)


def parse_inspection_lane_project_path(value: Any, lane_id: str) -> str | None:
    if value is None:
        return None
    project_path = clean_optional(value) if isinstance(value, str) else None
    if project_path is None:
        raise inspection_lane_config_error(f"Inspection lane {lane_id} projectPath must be a non-empty string.")
    normalized = project_path.replace("\\", "/")
    segments = normalized.split("/")
    drive_path = re.match(r"^[A-Za-z]:/", normalized) is not None
    if (
        project_path != normalized
        or len(normalized) > 1024
        or normalized.startswith(("/", "~/"))
        or drive_path
        or "." in segments
        or ".." in segments
        or "" in segments
        or "\x00" in normalized
        or any(character in normalized for character in "*?[]")
    ):
        raise inspection_lane_config_error(
            f"Inspection lane {lane_id} projectPath must be a safe repository-relative POSIX directory: {project_path}"
        )
    return normalized


def parse_inspection_lane_patterns(
    value: Any,
    lane_id: str,
    field: str,
    required: bool,
) -> tuple[str, ...]:
    if not isinstance(value, list) or (required and not value):
        qualifier = "a non-empty array" if required else "an array"
        raise inspection_lane_config_error(f"Inspection lane {lane_id} {field} must be {qualifier} of patterns.")
    patterns: list[str] = []
    for raw_pattern in value:
        pattern = clean_optional(raw_pattern) if isinstance(raw_pattern, str) else None
        if pattern is None:
            raise inspection_lane_config_error(f"Inspection lane {lane_id} {field} contains an empty pattern.")
        validate_inspection_lane_pattern(pattern, lane_id, field)
        patterns.append(pattern)
    return tuple(patterns)


def validate_inspection_lane_pattern(pattern: str, lane_id: str, field: str) -> None:
    normalized = pattern.replace("\\", "/")
    drive_path = re.match(r"^[A-Za-z]:/", normalized) is not None
    segments = normalized.split("/")
    if (
        pattern != normalized
        or normalized.startswith(("/", "~/"))
        or drive_path
        or ".." in segments
        or "." in segments
        or "" in segments
        or "\x00" in pattern
    ):
        raise inspection_lane_config_error(
            f"Inspection lane {lane_id} {field} pattern must be a safe repository-relative POSIX glob: {pattern}"
        )
    if pattern.count("[") != pattern.count("]"):
        raise inspection_lane_config_error(f"Inspection lane {lane_id} {field} pattern has unbalanced brackets: {pattern}")


def inspection_lane_config_error(message: str) -> InspectError:
    return InspectError(
        message,
        2,
        {
            "error_reason": "inspection_lane_config_invalid",
            "failure_phase": "configuration",
            "next_action": "Fix qualityGate.inspection.lanes in .github/github.json, then rerun the inspection command.",
        },
    )


def configured_inspection_lanes(context: dict[str, Any]) -> tuple[InspectionLane, ...]:
    lanes = context.get("_inspection_lanes")
    if isinstance(lanes, tuple) and all(isinstance(lane, InspectionLane) for lane in lanes):
        return lanes
    return ()


def resolve_inspection_lane_selection(
    args: argparse.Namespace,
    context: dict[str, Any],
) -> dict[str, Any]:
    lanes = configured_inspection_lanes(context)
    if not lanes:
        return {}
    worktree_root = Path(str(context.get("worktree_root"))).expanduser().resolve()
    scope = str(context.get("scope") or "changed_files").strip().lower()
    raw_paths = inspection_scope_paths(args, worktree_root, scope)
    selected_files: list[dict[str, str]] = []
    skipped_files: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        normalized = normalize_inspection_lane_path(raw_path, worktree_root)
        relative_path = normalized.relative_to(worktree_root).as_posix()
        if relative_path in seen:
            continue
        seen.add(relative_path)
        if not normalized.exists():
            skipped_files.append({"file": relative_path, "reason": "missing_or_deleted"})
            continue
        if not normalized.is_file():
            skipped_files.append({"file": relative_path, "reason": "not_a_file"})
            continue
        selected_files.append({"file": relative_path, "absolute_path": str(normalized)})

    max_files = getattr(args, "max_files", None)
    if max_files is not None and len(selected_files) > int(max_files):
        raise InspectError(
            f"Resolved lane scope contains {len(selected_files)} files, exceeding --max-files={max_files}.",
            2,
            {
                "error_reason": "inspection_lane_scope_too_large",
                "failure_phase": "scope_resolution",
                "selected_file_count": len(selected_files),
                "max_files": int(max_files),
            },
        )

    lane_files: dict[str, list[dict[str, str]]] = {lane.lane_id: [] for lane in lanes}
    excluded_files: list[dict[str, str]] = []
    explicit_exclusion_overrides: list[dict[str, str]] = []
    unmatched_files: list[str] = []
    for selected in selected_files:
        relative_path = selected["file"]
        matched_lane: InspectionLane | None = None
        for lane in lanes:
            if any(inspection_lane_pattern_matches(relative_path, pattern) for pattern in lane.include):
                matched_lane = lane
                break
        if matched_lane is None:
            unmatched_files.append(relative_path)
            continue
        excluding_pattern = next(
            (
                pattern
                for pattern in matched_lane.exclude
                if inspection_lane_pattern_matches(relative_path, pattern)
            ),
            None,
        )
        if excluding_pattern is not None and scope != "files":
            excluded_files.append(
                {
                    "file": relative_path,
                    "lane_id": matched_lane.lane_id,
                    "pattern": excluding_pattern,
                }
            )
            continue
        if excluding_pattern is not None:
            explicit_exclusion_overrides.append(
                {
                    "file": relative_path,
                    "lane_id": matched_lane.lane_id,
                    "pattern": excluding_pattern,
                }
            )
        lane_files[matched_lane.lane_id].append(selected)

    return {
        "schema_version": INSPECTION_LANE_SCHEMA_VERSION,
        "scope": scope,
        "selected_file_count": len(selected_files),
        "selected_files": [selected["file"] for selected in selected_files],
        "skipped_files": skipped_files,
        "excluded_files": excluded_files,
        "explicit_exclusion_overrides": explicit_exclusion_overrides,
        "unmatched_files": unmatched_files,
        "lane_files": lane_files,
    }


def inspection_scope_paths(args: argparse.Namespace, worktree_root: Path, scope: str) -> list[str]:
    if scope == "files":
        files = [str(path) for path in (getattr(args, "files", []) or []) if clean_optional(path)]
        if not files:
            raise InspectError(
                "files scope requires at least one --file argument when inspection lanes are configured.",
                2,
                {"error_reason": "inspection_lane_scope_empty", "failure_phase": "scope_resolution"},
            )
        return files
    if scope == "changed_files":
        return changed_inspection_scope_paths(args, worktree_root)
    if scope in {"directory", "whole_project"}:
        pathspec: list[str] = []
        if scope == "directory":
            directory = clean_optional(getattr(args, "directory", None))
            if directory is None:
                raise InspectError(
                    "directory scope requires --dir when inspection lanes are configured.",
                    2,
                    {"error_reason": "inspection_lane_scope_empty", "failure_phase": "scope_resolution"},
                )
            normalized_directory = normalize_inspection_lane_path(directory, worktree_root)
            pathspec = [normalized_directory.relative_to(worktree_root).as_posix()]
        command = ["git", "-C", str(worktree_root), "ls-files", "-z", "--cached"]
        if getattr(args, "include_unversioned", True):
            command.extend(["--others", "--exclude-standard"])
        if pathspec:
            command.extend(["--", *pathspec])
        return git_null_paths(command, worktree_root)
    raise InspectError(
        f"Inspection lanes do not support scope: {scope}",
        2,
        {
            "error_reason": "inspection_lane_scope_unsupported",
            "failure_phase": "scope_resolution",
            "scope": scope,
        },
    )


def changed_inspection_scope_paths(args: argparse.Namespace, worktree_root: Path) -> list[str]:
    mode = str(getattr(args, "changed_files_mode", "all") or "all").strip().lower()
    commands: list[list[str]] = []
    if mode in {"all", "staged"}:
        commands.append(
            [
                "git",
                "-C",
                str(worktree_root),
                "diff",
                "--cached",
                "--name-only",
                "-z",
                "--diff-filter=ACMRTUXB",
            ]
        )
    if mode in {"all", "unstaged"}:
        commands.append(
            [
                "git",
                "-C",
                str(worktree_root),
                "diff",
                "--name-only",
                "-z",
                "--diff-filter=ACMRTUXB",
            ]
        )
    paths: list[str] = []
    for command in commands:
        paths.extend(git_null_paths(command, worktree_root))
    if getattr(args, "include_unversioned", True) and mode in {"all", "unstaged"}:
        paths.extend(
            git_null_paths(
                ["git", "-C", str(worktree_root), "ls-files", "--others", "--exclude-standard", "-z"],
                worktree_root,
            )
        )
    return paths


def git_null_paths(command: list[str], worktree_root: Path) -> list[str]:
    try:
        completed = subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        raise InspectError(
            "Could not resolve the repository file scope for inspection lanes.",
            3,
            {
                "error_reason": "inspection_lane_scope_resolution_failed",
                "failure_phase": "scope_resolution",
                "git_exit_code": error.returncode,
                "worktree_root": str(worktree_root),
            },
        ) from error
    return [os.fsdecode(path) for path in completed.stdout.split(b"\0") if path]


def normalize_inspection_lane_path(value: Any, worktree_root: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = worktree_root / path
    normalized = path.resolve()
    if normalized != worktree_root and not normalized.is_relative_to(worktree_root):
        raise InspectError(
            "Inspection lane scope contains a path outside the exact worktree.",
            2,
            {
                "error_reason": "inspection_lane_path_outside_worktree",
                "failure_phase": "scope_resolution",
                "path": str(path),
                "worktree_root": str(worktree_root),
            },
        )
    return normalized


def inspection_lane_pattern_matches(relative_path: str, pattern: str) -> bool:
    path_segments = tuple(relative_path.split("/"))
    pattern_segments = tuple(pattern.split("/"))
    memo: dict[tuple[int, int], bool] = {}

    def matches(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_segments):
            result = path_index == len(path_segments)
        elif pattern_segments[pattern_index] == "**":
            result = matches(pattern_index + 1, path_index) or (
                path_index < len(path_segments) and matches(pattern_index, path_index + 1)
            )
        else:
            result = (
                path_index < len(path_segments)
                and fnmatch.fnmatchcase(path_segments[path_index], pattern_segments[pattern_index])
                and matches(pattern_index + 1, path_index + 1)
            )
        memo[key] = result
        return result

    return matches(0, 0)


def dict_field(mapping: dict[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_selector(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


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
    return 2000 + build_major // 10, build_major % 10


def candidate_channel(name: str, bundle_id: str | None = None) -> str:
    text = f"{name} {bundle_id or ''}".lower()
    return "eap" if "eap" in text else "stable"


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


def copy_verdict_evidence(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in (
        "total_problems",
        "problems_shown",
        "cached_total_problems",
        "cached_problems_shown",
        "capture_incomplete_reason",
        "capture_diagnostic",
        *INSPECTION_STAGE_EVIDENCE_KEYS,
        *VERDICT_SOURCE_KEYS,
    ):
        if key in source:
            target[key] = source[key]


def is_inspection_in_progress_conflict(payload: dict[str, Any]) -> bool:
    return (
        payload.get("status") == "inspection_in_progress"
        or payload.get("error") == "inspection_in_progress"
        or payload.get("inspection_in_progress") is True
    )


def project_open_was_not_requested(attempts: Any) -> bool:
    if not isinstance(attempts, list):
        return False
    for attempt in attempts:
        if not isinstance(attempt, dict) or any(key in attempt for key in (
            "identity", "lease_id", "ownership_registered", "lifecycle_ownership_protocol",
            "open_outcome", "request_may_have_been_accepted", "endpoint_status", "opening_scheduled",
        )):
            return False
        if attempt.get("method") == "bootstrap_ide":
            continue
        if attempt.get("reason") == "no_matching_running_ide" and attempt.get("accepted") is False:
            continue
        return False
    return True


def lease_proves_open_not_attempted(lease: dict[str, Any]) -> bool:
    return (
        lease.get("state") in POTENTIAL_OPEN_LEASE_STATES
        and (
            (
                lease.get("preparation_failure_stage") == "project_open"
                and lease.get("preparation_failure_reason") in FAILURE_REASONS_RAISED_BEFORE_OPEN_REQUEST
            )
            or lease.get("open_not_attempted") is True
        )
        and lease.get("opened_by_helper") is False
        and lease.get("open_request_may_have_been_accepted") is False
        and (
            lease.get("open_attempts") == []
            or lease.get("open_not_attempted") is True and project_open_was_not_requested(lease.get("open_attempts"))
        )
        and not lease.get("session_id")
        and not lease.get("ide_port")
        and not lease.get("project_instance_id")
        and not lease.get("project_key")
        and not lease.get("route")
    )


def lease_may_own_open_project(lease: dict[str, Any]) -> bool:
    if lease_proves_open_not_attempted(lease):
        return False
    return (
        lease.get("opened_by_helper") is True
        or lease.get("open_request_may_have_been_accepted") is True
        or lease.get("state") in POTENTIAL_OPEN_LEASE_STATES
    )


def persist_claimed_cleanup_ownership(
    lease: dict[str, Any],
    route: dict[str, Any],
    claim_metadata: dict[str, Any],
) -> None:
    lease.update(
        {
            "opened_by_helper": True,
            "route": route,
            "project_instance_id": route.get("project_instance_id"),
            "project_key": route.get("project_key"),
            "session_id": route.get("session_id"),
            "plugin_claim": claim_metadata,
            "preparation_stage": "cleanup_pending",
        }
    )
    mark_lease_state(lease, "cleanup_pending")


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
        "plugin_build_dirty": identity.get("plugin_build_dirty"),
        "inspection_execution_proof_version": identity.get("inspection_execution_proof_version"),
        "lifecycle_ownership_protocol": identity.get("lifecycle_ownership_protocol"),
        "lifecycle_open_diagnostic_version": identity.get("lifecycle_open_diagnostic_version"),
        "session_id": identity.get("session_id"),
        "port": identity.get("port"),
        "pid": identity.get("pid"),
        "open_project_count": len(identity.get("open_projects", []) or []),
    }


def ensure_claim_route_matches(expected_route: dict[str, Any], claimed_route: dict[str, Any]) -> None:
    for field in ("session_id", "project_instance_id", "project_key"):
        expected = expected_route.get(field)
        actual = claimed_route.get(field)
        if expected and actual and expected != actual:
            raise InspectError(
                "Lifecycle claim resolved a different project route.",
                3,
                {
                    "error_reason": "session_drift",
                    "session_drift": True,
                    "field": field,
                    "expected": expected,
                    "actual": actual,
                },
            )


def lifecycle_claim_ownership(claim: dict[str, Any], lease: dict[str, Any]) -> tuple[bool | None, str | None]:
    close_proof = claim.pop("close_" + "token", None)
    protocol_matches = claim.get("lifecycle_ownership_protocol") == LIFECYCLE_OWNERSHIP_PROTOCOL
    lease_matches = claim.get("lease_id") == lease.get("lease_id")
    if claim.get("ownership_proven") is True and protocol_matches and lease_matches:
        return True, str(close_proof) if close_proof else None
    if claim.get("ownership_proven") is False and protocol_matches:
        return False, None
    return None, None


def cleanup_lifecycle(lease: dict[str, Any], route: dict[str, Any], close_proof: str | None = None) -> dict[str, Any]:
    if not lease.get("opened_by_helper"):
        reason = "open_not_attempted" if lease_proves_open_not_attempted(lease) or lease.get("open_not_attempted") is True else "project_preexisted"
        mark_lease_state(lease, "released")
        remove_lease(lease)
        return {"status": "not_needed", "reason": reason}
    project_instance_id = lease.get("project_instance_id")
    if not close_proof or not project_instance_id:
        mark_lease_state(lease, "cleanup_skipped")
        return {"status": "skipped", "cleanup_skipped": True, "reason": "missing_close_token"}
    try:
        close_params = {
            "client_run_id": lease.get("client_run_id"),
            "project_key": lease.get("project_key") or route.get("project_key"),
            "project_path": route.get("base_path"),
            "worktree_path": route.get("base_path"),
            "session_id": lease.get("session_id") or route.get("session_id"),
            "project_instance_id": project_instance_id,
            "lease_id": lease.get("lease_id"),
            "close_" + "token": close_proof,
        }
        close_result = call_lifecycle_close(route, close_params)
    except InspectError as error:
        mark_lease_state(lease, "cleanup_failed")
        result = {
            "status": "failed",
            "cleanup_failed": True,
            "reason": public_cleanup_reason(error),
        }
        for key in ("endpoint", "http_status", "response_code", "client_run_id"):
            if error.payload.get(key) is not None:
                result[key] = error.payload[key]
        copy_verdict_evidence(result, error.payload)
        return result
    status = str(close_result.get("status") or "")
    mark_lease_state(lease, "closed" if status == "closed" else "cleanup_skipped")
    if status == "closed":
        remove_lease(lease)
    return close_result


def call_lifecycle_close(route: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    port = route_port(route)
    try:
        body = private_http_get_body(port, "lifecycle/close", params, timeout=35.0)
    except InspectError as error:
        body = error.payload if isinstance(error.payload, dict) else {}
        attribution = dict_field(body, "inspection_attribution")
        try:
            http_status = int(body.get("http_status") or attribution.get("http_status") or 0)
        except (TypeError, ValueError):
            http_status = 0
        if (
            body.get("session_drift")
            or not body
            or "status" not in body
            or str(body.get("status") or "") == "error"
            or http_status >= 500
            or attribution.get("code") == "inspection_api_http_error"
        ):
            raise
    status = str(body.get("status") or "closed")
    if status == "closed":
        result = {
            "status": "closed",
            "reason": body.get("reason"),
            "cleanup_skipped": False,
            "cleanup_failed": False,
        }
    else:
        result = {
            "status": status,
            "reason": body.get("reason") or status,
            "cleanup_skipped": True,
            "cleanup_failed": False,
            "message": body.get("message"),
            "close_attempts": body.get("close_attempts"),
        }
    for field in (
        "project_instance_id",
        "project_key",
        "lease_id",
        "session_id",
        "closed_at_ms",
        "inspection_run_id",
        "inspection_in_progress",
        "close_attempts",
        "inspection_attribution",
        "http_status",
        "response_code",
        "client_run_id",
    ):
        if body.get(field) is not None:
            result[field] = body[field]
    return result


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
    return http_get(
        port,
        endpoint,
        params,
        timeout=timeout or max(DEFAULT_TIMEOUT_SECONDS, 10.0),
    ).body


def inspection_transport_error(port: int, endpoint: str, error: BaseException) -> InspectError:
    reason = getattr(error, "reason", None)
    timed_out = isinstance(error, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in str(error).lower()
    error_reason = "inspection_api_timeout" if timed_out else "inspection_api_unavailable"
    condition = "timed out" if timed_out else "unavailable"
    return InspectError(
        f"Inspection API {condition} on port {port}: {error}",
        3,
        {"error_reason": error_reason, "endpoint": endpoint, "port": port},
    )


def discover_identities(port: int | None) -> list[dict[str, Any]]:
    if port:
        return [identity_for_port(port)]
    return merged_registry_and_port_identities()


def registry_identities() -> list[dict[str, Any]]:
    instances = registry_dir()
    if not instances.exists():
        return []
    current_ms = int(time.time() * 1000)
    identities = []
    for path in sorted(instances.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        heartbeat = int(data.get("heartbeat_ms") or 0)
        if heartbeat and current_ms - heartbeat > 60_000:
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
    return http_request(port, endpoint, params, timeout, method="GET")


def http_post(port: int, endpoint: str, params: dict[str, Any], timeout: float = DEFAULT_TIMEOUT_SECONDS) -> HttpResult:
    return http_request(port, endpoint, params, timeout, method="POST")


def http_request(port: int, endpoint: str, params: dict[str, Any], timeout: float, *, method: str) -> HttpResult:
    clean_params = {key: str(value) for key, value in params.items() if value is not None and value != ""}
    if _ACTIVE_CLIENT_RUN_ID and "client_run_id" not in clean_params:
        clean_params["client_run_id"] = _ACTIVE_CLIENT_RUN_ID
    query = urllib.parse.urlencode(clean_params, doseq=True)
    display_query = urllib.parse.urlencode(redact_payload(clean_params), doseq=True)
    # The plugin's built-in server is loopback-only and does not expose TLS.
    # noinspection HttpUrlsUsage
    base_url = f"http://{LOOPBACK_HOST}:{port}/api/inspection/{endpoint}"
    request_url = f"{base_url}?{query}" if query else base_url
    display_url = f"{base_url}?{display_query}" if display_query else base_url
    request = urllib.request.Request(
        request_url, headers={"Accept": "application/json"}, method=method,
        data=b"" if method == "POST" else None,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = parse_http_json(response.read(), endpoint, response.status, clean_params.get("client_run_id"))
            return HttpResult(response.status, body, display_url)
    except urllib.error.HTTPError as error:
        body = parse_http_json(error.read(), endpoint, error.code, clean_params.get("client_run_id"))
        if method == "GET" and error.code == 409 and is_inspection_in_progress_conflict(body):
            return HttpResult(error.code, body, display_url)
        payload = dict(body)
        payload.setdefault("endpoint", endpoint)
        payload.setdefault("http_status", error.code)
        if clean_params.get("client_run_id"):
            payload.setdefault("client_run_id", clean_params["client_run_id"])
        attribution = dict_field(payload, "inspection_attribution")
        payload.setdefault(
            "response_code",
            attribution.get("code") or payload.get("error_reason") or payload.get("reason") or payload.get("status") or "inspection_api_http_error",
        )
        if error.code == 409 and payload.get("session_drift"):
            raise InspectError("IDE session changed; resolve route and re-trigger before trusting results.", 4, payload)
        if error.code == 400:
            raise InspectError(payload.get("message") or payload.get("error") or "Bad inspection request.", 3, payload)
        raise InspectError(f"HTTP {error.code} from inspection API", 3, payload)
    except (urllib.error.URLError, TimeoutError) as error:
        failure = inspection_transport_error(port, endpoint, error)
        failure.payload.setdefault("client_run_id", clean_params.get("client_run_id"))
        raise failure from error


def parse_http_json(raw: bytes, endpoint: str, http_status: int, client_run_id: str | None) -> dict[str, Any]:
    try:
        return parse_json(raw)
    except InspectError as error:
        error.payload.setdefault("endpoint", endpoint)
        error.payload.setdefault("http_status", http_status)
        error.payload.setdefault("client_run_id", client_run_id)
        error.payload.setdefault("response_code", "invalid_api_response")
        raise


def helper_revision() -> str:
    global _HELPER_REVISION
    if _HELPER_REVISION is None:
        try:
            digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            _HELPER_REVISION = f"sha256:{digest}"
        except OSError:
            _HELPER_REVISION = "unavailable"
    return _HELPER_REVISION


def stable_value_hash(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text_sha256(text)


def text_sha256(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def unknown_log_path() -> Path | None:
    return configured_log_path(UNKNOWN_LOG_ENV, "unknown-verdicts.jsonl")


def outcome_log_path() -> Path | None:
    return configured_log_path(OUTCOME_LOG_ENV, "outcomes.jsonl")


def configured_log_path(env_name: str, default_name: str) -> Path | None:
    configured = os.environ.get(env_name)
    if configured is not None:
        value = configured.strip()
        if value.lower() in {"", "0", "false", "no", "off"}:
            return None
        return Path(value).expanduser().resolve()
    return Path(code_home()) / "jetbrains-inspection" / default_name


def code_home() -> Path:
    return Path(os.environ.get("CODE_HOME") or os.environ.get("CODEX_HOME") or str(Path.home() / ".code")).expanduser()


def append_jsonl_record(
    log_path: Path,
    record: dict[str, Any],
    lock_timeout_ms: int = DEFAULT_OUTCOME_APPEND_LOCK_TIMEOUT_MS,
) -> None:
    payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    original_size: int | None = None
    locked = False
    try:
        if fcntl is not None:
            timeout_ms = max(0, int(lock_timeout_ms))
            deadline = time.monotonic() + (timeout_ms / 1000.0)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError as error:
                    if timeout_ms == 0 or time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for the JetBrains inspection outcome log lock: {log_path}"
                        ) from error
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        elif msvcrt is not None:
            timeout_ms = max(0, int(lock_timeout_ms))
            deadline = time.monotonic() + (timeout_ms / 1000.0)
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError as error:
                    if timeout_ms == 0 or time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"Timed out waiting for the JetBrains inspection outcome log lock: {log_path}"
                        ) from error
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        else:
            raise OSError("Unsupported platform for JetBrains inspection outcome log locking")
        original_size = os.lseek(descriptor, 0, os.SEEK_END)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("JSONL append made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except BaseException:
        if original_size is not None:
            try:
                os.ftruncate(descriptor, original_size)
                os.fsync(descriptor)
            except OSError:
                pass
        raise
    finally:
        if locked:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif msvcrt is not None:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


def summarize_outcome_log(log_path: Path, limit: int = 10) -> dict[str, Any]:
    if not log_path.exists():
        return {
            "status": "missing",
            "path": str(log_path),
            "events": 0,
            "invalid_lines": 0,
            "summary": empty_outcome_summary(),
            "recent": [],
        }

    events: list[dict[str, Any]] = []
    invalid_lines = 0
    try:
        with log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    invalid_lines += 1
                    continue
                if isinstance(entry, dict):
                    events.append(public_outcome_event(entry))
                else:
                    invalid_lines += 1
    except OSError as error:
        return {
            "status": "error",
            "path": str(log_path),
            "error_reason": "outcome_log_unreadable",
            "error_message": str(error),
            "events": 0,
            "invalid_lines": invalid_lines,
            "summary": empty_outcome_summary(),
            "recent": [],
        }

    return {
        "status": "ok",
        "path": str(log_path),
        "events": len(events),
        "invalid_lines": invalid_lines,
        "summary": summarize_outcome_events(events),
        "recent": events[-limit:] if limit else [],
    }


def public_outcome_event(event: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "timestamp",
        "command",
        "exit_code",
        "verdict",
        "bucket",
        "verdict_reason",
        "retry",
        "retry_max_attempts",
        "retry_wait_ms",
        "next_action",
        "agent_report",
        "status",
        "scope",
        "ide",
        "ide_channel",
        "ide_version",
        "eap_explicit",
        "cleanup_status",
        "cleanup_reason",
        "total_problems",
        "problems_shown",
    )
    return {key: event[key] for key in allowed if key in event and event[key] not in (None, {}, [])}


def summarize_outcome_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    retryable_unknowns = sum(1 for event in events if event.get("verdict") == "UNKNOWN" and event.get("retry") is True)
    return {
        "by_verdict": count_by(events, "verdict"),
        "by_bucket": count_by(events, "bucket"),
        "by_command": count_by(events, "command"),
        "by_retry": count_by(events, "retry"),
        "by_ide_channel": count_by(events, "ide_channel"),
        "by_cleanup_status": count_by(events, "cleanup_status"),
        "retryable_unknowns": retryable_unknowns,
    }


def empty_outcome_summary() -> dict[str, Any]:
    return {
        "by_verdict": {},
        "by_bucket": {},
        "by_command": {},
        "by_retry": {},
        "by_ide_channel": {},
        "by_cleanup_status": {},
        "retryable_unknowns": 0,
    }


def count_by(events: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        value = event.get(key)
        if value in (None, {}, []):
            label = "unknown"
        elif isinstance(value, bool):
            label = "true" if value else "false"
        else:
            label = str(value)
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def utc_timestamp(timestamp_ms: int | None = None) -> str:
    instant = datetime.fromtimestamp((timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)) / 1000, tz=timezone.utc)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


def split_durable_path_suffix(value: str) -> tuple[str, str]:
    path = value
    suffix = ""
    while path and path[-1] in '.,;!?)]}':
        suffix = path[-1] + suffix
        path = path[:-1]
    line_suffix = re.fullmatch(r"(.+?)(:\d+(?::\d+)*)", path)
    if line_suffix is not None:
        path = line_suffix.group(1)
        suffix = line_suffix.group(2) + suffix
    return path, suffix


def is_durable_path_candidate(value: str) -> bool:
    lowered = value.casefold()
    if lowered.startswith(("file://", "file:/", "path:/", "~/")):
        return True
    if re.match(r"^[a-z]:[\\/]", value, re.IGNORECASE) or value.startswith("\\\\"):
        return True
    if not value.startswith("/") or value.startswith("//"):
        return False
    parts = [part for part in value.split("/") if part]
    if not parts:
        return False
    return parts[0] in DURABLE_POSIX_PATH_ROOTS


def redact_durable_path_candidate(value: str) -> str:
    path, suffix = split_durable_path_suffix(value)
    if not is_durable_path_candidate(path):
        return value
    return f"<path:{stable_value_hash(path)}>{suffix}"


def redact_ambiguous_spaced_path_tail(value: str) -> str:
    path, suffix = split_durable_path_suffix(value)
    if not any(character.isspace() for character in path):
        return value
    first_token, _, remainder = path.partition(" ")
    if not is_durable_path_candidate(first_token):
        return value
    non_posix_prefix = first_token.casefold().startswith(("file://", "file:/", "path:/", "~/")) or bool(
        re.match(r"^[a-z]:[\\/]", first_token, re.IGNORECASE)
    ) or first_token.startswith("\\\\")
    root = next((part for part in first_token.split("/") if part), "")
    likely_space_bearing_root = root in {
        "Users",
        "Volumes",
        "home",
        "media",
        "mnt",
        "private",
        "tmp",
        "var",
        "workspace",
        "workspaces",
    }
    if not non_posix_prefix and not likely_space_bearing_root and "/" not in remainder and "\\" not in remainder:
        return value
    return f"<path:{stable_value_hash(path)}>{suffix}"


def redact_durable_text(value: str) -> str:
    def replace_quoted(match: re.Match[str]) -> str:
        quote = match.group("quote")
        path = match.group("path")
        return f"{quote}{redact_durable_path_candidate(path)}{quote}"

    def replace_token(match: re.Match[str]) -> str:
        return redact_durable_path_candidate(match.group("path"))

    def replace_ambiguous_tail(match: re.Match[str]) -> str:
        return redact_ambiguous_spaced_path_tail(match.group("path"))

    sensitive_redacted = SENSITIVE_OUTPUT_BEARER_PATTERN.sub(
        lambda match: f"{match.group(1)}{REDACTED}",
        value,
    )
    sensitive_redacted = SENSITIVE_OUTPUT_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}",
        sensitive_redacted,
    )
    quoted_redacted = DURABLE_QUOTED_PATH_PATTERN.sub(replace_quoted, sensitive_redacted)
    spaced_redacted = DURABLE_SPACED_FILE_PATH_PATTERN.sub(replace_token, quoted_redacted)
    ambiguous_redacted = DURABLE_SPACED_PATH_TAIL_PATTERN.sub(replace_ambiguous_tail, spaced_redacted)
    return DURABLE_PATH_TOKEN_PATTERN.sub(replace_token, ambiguous_redacted)


def redact_durable_log(value: Any) -> Any:
    if isinstance(value, dict):
        durable: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            lowered = text_key.lower()
            if is_sensitive_key(text_key):
                durable[text_key] = REDACTED
            elif not lowered.endswith("_hash") and (
                lowered in {
                    "path",
                    "file",
                    "directory",
                    "cwd",
                    "project_key",
                    "repo_path",
                    "worktree_root",
                    "base_path",
                    "rollout_file",
                    "scope_directory_requested",
                    "target_worktree",
                    "inspection_file",
                }
                or lowered.endswith("_path")
                or lowered.endswith("_root")
            ):
                serialized = json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else item
                durable[f"{text_key}_hash"] = stable_value_hash(serialized)
            else:
                durable[text_key] = redact_durable_log(item)
        return durable
    if isinstance(value, list):
        return [redact_durable_log(item) for item in value]
    if isinstance(value, str):
        return redact_durable_text(value)
    return value


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return redact_payload(strip_private_fields(payload))


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


def git_root(path: Path) -> Path | None:
    try:
        output = subprocess.check_output(["git", "-C", str(path), "rev-parse", "--show-toplevel"], text=True, stderr=subprocess.DEVNULL)
        return Path(output.strip()).resolve()
    except subprocess.CalledProcessError:
        return None


def git_head_sha(path: Path) -> str | None:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return output if re.fullmatch(r"[0-9a-fA-F]{40,64}", output) else None


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
    shared_config = Path.home() / ".config" / "jetbrains-inspection" / "config.json"
    if shared_config.exists():
        return shared_config
    code_home = os.environ.get("CODE_HOME") or os.environ.get("CODEX_HOME") or str(Path.home() / ".code")
    legacy_config = Path(code_home).expanduser() / "jetbrains-inspection.json"
    return legacy_config if legacy_config.exists() else shared_config


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


def outcome_routing_lock_path() -> Path:
    return cache_dir() / "outcome-routing.lock"


class outcome_routing_lock:
    def __init__(self, timeout_ms: int = DEFAULT_OUTCOME_ROUTING_LOCK_TIMEOUT_MS):
        self.timeout_ms = max(0, int(timeout_ms))
        self.handle = None

    def __enter__(self):
        path = outcome_routing_lock_path()
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
                        raise TimeoutError(
                            f"Timed out waiting for the JetBrains inspection outcome routing lock: {path}"
                        ) from error
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        elif msvcrt is not None:
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write("\0")
                self.handle.flush()
            deadline = time.monotonic() + (self.timeout_ms / 1000.0)
            while True:
                try:
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if self.timeout_ms == 0 or time.monotonic() >= deadline:
                        self.handle.close()
                        raise TimeoutError(
                            f"Timed out waiting for the JetBrains inspection outcome routing lock: {path}"
                        ) from error
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        else:
            self.handle.close()
            raise OSError("Unsupported platform for JetBrains inspection outcome routing locking")
        return self

    def __exit__(self, exc_type, exc, tb):
        if fcntl is not None and self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None and self.handle is not None:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        if self.handle is not None:
            self.handle.close()


LIFECYCLE_QUEUE_DIRNAME = "lifecycle-queue"
LIFECYCLE_TICKET_GLOB = "*.ticket"


class fifo_turn:
    """Hold one kernel-locked FIFO ticket in `directory` until exit."""

    def __init__(self, directory: Path, label: str = "Inspection lifecycle", on_wait=None):
        self.directory = directory
        self.label = label
        self.on_wait = on_wait
        self.ticket = None
        self.ticket_path = None
        self.queued_at = None
        self.waited_ms = 0

    @staticmethod
    def try_lock(handle):
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write("\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                raise OSError("Unsupported platform for lifecycle file locking")
        except BlockingIOError:
            return False
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True

    @staticmethod
    def unlock(handle):
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _register_lock(self, gate):
        while not self.try_lock(gate):
            time.sleep(0.05)

    def __enter__(self):
        try:
            self._queue_turn()
        except BaseException:
            self._release_ticket()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        self._release_ticket()

    def _queue_turn(self):
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True)
        registration = directory / "registration.lock"
        self.queued_at = time.monotonic()
        with registration.open("a+", encoding="utf-8") as gate:
            self._register_lock(gate)
            try:
                tickets = list(directory.glob(LIFECYCLE_TICKET_GLOB))
                sequence = max((int(path.stem) for path in tickets), default=0) + 1
                self.ticket_path = directory / f"{sequence:020d}.ticket"
                self.ticket = self.ticket_path.open("x+", encoding="utf-8")
                if not self.try_lock(self.ticket):
                    raise OSError("New lifecycle ticket is already locked")
                if tickets:
                    print(f"{self.label} queued behind {len(tickets)} caller(s).", file=sys.stderr, flush=True)
            finally:
                self.unlock(gate)
        while True:
            with registration.open("a+", encoding="utf-8") as gate:
                self._register_lock(gate)
                try:
                    predecessor = next((path for path in sorted(directory.glob(LIFECYCLE_TICKET_GLOB))
                                        if path < self.ticket_path), None)
                    if predecessor is None:
                        self.waited_ms = round((time.monotonic() - self.queued_at) * 1000)
                        return
                    with predecessor.open("r+", encoding="utf-8") as holder:
                        abandoned = self.try_lock(holder)
                    if abandoned:
                        # Kernel ownership, rather than a PID or age, proves an abandoned ticket.
                        predecessor.unlink()
                finally:
                    self.unlock(gate)
            if self.on_wait is not None:
                self.on_wait()
            time.sleep(0.05)

    def _release_ticket(self):
        ticket_path = self.ticket_path
        if self.ticket is not None and ticket_path is not None:
            with ticket_path.with_name("registration.lock").open("a+", encoding="utf-8") as gate:
                self._register_lock(gate)
                try:
                    self.ticket.close()
                    self.ticket = None
                    ticket_path.unlink(missing_ok=True)
                finally:
                    self.unlock(gate)


class lifecycle_lock:
    try_lock = staticmethod(fifo_turn.try_lock)
    unlock = staticmethod(fifo_turn.unlock)

    def __init__(self, timeout_ms: int = DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS):
        self.timeout_ms = max(0, int(timeout_ms))
        self.handle = None
        self.turn = None
        self.waited_ms = 0

    def __enter__(self):
        path = lifecycle_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+", encoding="utf-8")
        if fcntl is not None or msvcrt is not None:
            self.turn = fifo_turn(path.with_name(LIFECYCLE_QUEUE_DIRNAME))
            try:
                self.turn.__enter__()
                self.waited_ms = self.turn.waited_ms
                deadline = time.monotonic() + (self.timeout_ms / 1000.0)
                while True:
                    if self.try_lock(self.handle):
                        break
                    if self.timeout_ms == 0 or time.monotonic() >= deadline:
                        raise InspectError(
                            "Timed out waiting for the JetBrains inspection lifecycle lock.",
                            3,
                            {
                                "error_reason": "lifecycle_lock_timeout",
                                "lock_path": str(path),
                                "timeout_ms": self.timeout_ms,
                                "hint": "A caller using the older lifecycle lock is still running. Wait for it to finish or increase --lifecycle-lock-timeout-ms.",
                            },
                        )
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            except BaseException:
                self.handle.close()
                self.turn.__exit__(None, None, None)
                raise
        else:
            self.handle.close()
            raise InspectError("Lifecycle serialization is unavailable on this platform.", 3)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle is not None:
            self.unlock(self.handle)
        if self.handle is not None:
            self.handle.close()
        if self.turn is not None:
            self.turn.__exit__(exc_type, exc, tb)


def now_ms() -> int:
    return int(time.time() * 1000)


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


def read_local_leases() -> list[tuple[Path, dict[str, Any]]]:
    directory = lease_dir()
    if not directory.exists():
        return []
    leases: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            lease = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        leases.append((path, lease))
    return leases


def lease_has_exact_route_identity(lease: dict[str, Any]) -> bool:
    lease_target = lease.get("lifecycle_target_path") or lease.get("worktree_root") or lease.get("repo_path")
    return bool(lease_target and lease.get("project_instance_id") and lease.get("session_id"))


def paths_same(left: Any, right: Any) -> bool:
    if not left or not right:
        return False
    try:
        return Path(str(left)).resolve() == Path(str(right)).resolve()
    except OSError:
        return str(left) == str(right)


def retirement_worktree(path: Path, dry_run: bool = False, offline_only: bool = False) -> Path:
    root = path.expanduser().resolve()
    if git_root(root) != root:
        raise InspectError("Removal requires the exact linked worktree root.", 3)
    common = Path(subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"], text=True,
    ).strip()).resolve()
    git_dir = Path(subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "--absolute-git-dir"], text=True,
    ).strip()).resolve()
    if git_dir == common or (not dry_run and (git_dir / "locked").exists()):
        raise InspectError("Primary or locked worktrees cannot be removed; finish the cleanup ownership checks first.", 3)
    if git_common_worktree(root) is None:
        raise InspectError("Cannot resolve the primary checkout for worktree removal.", 3)
    if subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True).strip():
        raise InspectError("Preserve uncommitted work before removing this worktree.", 3)
    reconcile_retirement_leases(root, dry_run, offline_only)
    return root


def reconcile_retirement_leases(root: Path, dry_run: bool, offline_only: bool = False) -> None:
    leases = [(path, lease) for path, lease in read_local_leases()
              if paths_same(lease.get("worktree_root"), root)]
    if not leases:
        return
    result = {} if offline_only else cleanup_stale_helper_leases(argparse.Namespace(
        max_age_ms=24 * 60 * 60 * 1000, dry_run=dry_run, worktree_root=root,
    ))
    released = set(result.get("would_remove" if dry_run else "removed") or [])
    blockers = [(path, lease) for path, lease in leases if str(path) not in released]
    if blockers:
        ids = [str(lease.get("lease_id") or path.stem) for path, lease in blockers]
        commands = [] if offline_only else [f"uv run {shlex.quote(str(Path(__file__).resolve()))} cleanup-helper-leases "
                    f"--lease-id {shlex.quote(lease_id)} --no-dry-run" for lease_id in ids]
        raise InspectError(
            (f"IDE leases block offline retirement: {', '.join(ids)}. Retain the worktree until a session authorized to contact the IDE resolves them."
             if offline_only else f"IDE leases block retirement: {', '.join(ids)}. After resolving ownership/activity, run: " + "; ".join(commands)), 3,
            {"blocking_lease_ids": ids, "cleanup_commands": commands, "lease_cleanup": result},
        )


FINDER_METADATA_NAME = ".DS_Store"
FINDER_RACE_ATTEMPTS = 3


def entry_signature(status: os.stat_result, is_directory: bool) -> tuple[int, ...]:
    # Deleting children changes a folder's mtime, so folders are matched by inode alone.
    return (status.st_ino,) if is_directory else (status.st_ino, status.st_size, status.st_mtime_ns)


def worktree_entries(root: Path) -> dict[str, tuple[int, ...]]:
    """Map every entry below root to its identity and content signature without following symlinks."""
    entries: dict[str, tuple[int, ...]] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as listing:
            for entry in listing:
                path = Path(entry.path)
                is_directory = entry.is_dir(follow_symlinks=False)
                entries[str(path.relative_to(root))] = entry_signature(entry.stat(follow_symlinks=False), is_directory)
                if is_directory:
                    pending.append(path)
    return entries


def delete_verified_entries(root: Path, directory: Path, before: dict[str, tuple[int, ...]], unexpected: list[str],
                            delete: bool = True) -> None:
    """Delete entries unchanged since the snapshot, and Finder metadata, unless only checking; collect every other entry."""
    try:
        with os.scandir(directory) as listing:
            entries = list(listing)
    except FileNotFoundError:
        return
    for entry in entries:
        path = Path(entry.path)
        relative = str(path.relative_to(root))
        try:
            # Check each entry immediately before deleting it so a file written mid-pass is kept.
            is_directory = entry.is_dir(follow_symlinks=False)
            status = os.lstat(path)
            if (not is_directory and entry.name == FINDER_METADATA_NAME) or before.get(relative) == entry_signature(status, is_directory):
                if is_directory:
                    delete_verified_entries(root, path, before, unexpected, delete)
                    if not delete:
                        continue
                    with suppress(OSError):
                        # Finder may have refilled it; the next attempt retries.
                        os.rmdir(path)
                elif delete:
                    os.unlink(path)
            else:
                unexpected.append(relative)
        except FileNotFoundError:
            continue


def finish_raced_worktree_removal(common: Path, root: Path, before: dict[str, tuple[int, ...]], git_error: str) -> None:
    """Finish a removal Git unregistered but could not empty, keeping anything it did not verify."""
    # Git deletes the checkout before its registration, so a registered path was refused rather than raced.
    listed = subprocess.run(["git", "-C", str(common), "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout.splitlines()
    if any(line.startswith("worktree ") and paths_same(line.removeprefix("worktree "), root) for line in listed):
        raise InspectError("Git worktree removal failed; SDK cleanup may already have completed.", 3, {"git_error": git_error})
    # Finder writes .DS_Store into folders while they are deleted; every other leftover must be unchanged since the snapshot.
    for _ in range(FINDER_RACE_ATTEMPTS):
        if not root.exists() and not root.is_symlink():
            return
        if root.is_symlink() or not root.is_dir():
            break
        unexpected: list[str] = []
        # Check the whole tree first so a hold keeps every leftover, then recheck each entry as it is deleted.
        delete_verified_entries(root, root, before, unexpected, delete=False)
        if not unexpected:
            delete_verified_entries(root, root, before, unexpected)
        if unexpected:
            unexpected.sort()
            raise InspectError("Worktree removal left files that were not in the verified checkout; they are kept.", 3,
                               {"git_error": git_error, "retained_entry_count": len(unexpected), "retained_entries": unexpected[:50]})
        with suppress(OSError):
            os.rmdir(root)
    if root.exists() or root.is_symlink():
        remaining: list[str] = []
        if root.is_dir() and not root.is_symlink():
            delete_verified_entries(root, root, before, remaining, delete=False)
        raise InspectError("Git unregistered the worktree but its directory could not be emptied; inspect what remains.", 3,
                           {"git_error": git_error, "retained_entry_count": len(remaining), "retained_entries": sorted(remaining)[:50]})


HELPER_SDK_RECORD_FILENAME = "inspection-helper-sdks.xml"
HELPER_PROJECT_CACHE_MARKER = ".inspection-helper-owned.json"
HELPER_SEALED_CACHE_PREFIX = ".inspection-retired-"
HELPER_FINALIZING_CACHE_PREFIX = ".inspection-finalizing-"


def offline_ide_processes() -> list[int]:
    """A missing plugin identity does not prove the IDE process has exited."""
    try:
        return ide_launcher_pids(ALL_IDE_LAUNCHER_NAMES)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise InspectError("Cannot prove IDE processes have exited; restore process discovery or use live IDE cleanup.", 3) from error


def ide_launcher_pids(launcher_names: tuple[str, ...]) -> list[int]:
    launcher = re.compile(rf"\.app/Contents/MacOS/(?:{'|'.join(map(re.escape, launcher_names))})$", re.IGNORECASE)
    output = subprocess.check_output(["ps", "-axo", "pid=,comm="], text=True, timeout=5)
    pids = []
    for line in output.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and launcher.search(parts[1]):
            pids.append(int(parts[0]))
    return pids


def offline_retirement_records(root: Path) -> list[str]:
    """Prove the no-cleanup case from disk; never edit an offline IDE's state."""
    if sys.platform != "darwin":
        raise InspectError("Offline retirement record discovery is only supported on macOS; use a live IDE for cleanup.", 3)
    if pids := offline_ide_processes():
        raise InspectError("An IDE is running; retain the worktree until an authorized session can use live cleanup or close the IDE before offline retirement.", 3,
                           {"offline_retirement_ide_pids": pids})
    checked: list[str] = []

    def present(path: Path) -> bool:
        try:
            path.lstat()
            return True
        except FileNotFoundError:
            return False

    def children(path: Path) -> list[Path]:
        try:
            return list(path.iterdir())
        except FileNotFoundError:
            return []

    def refers_to_worktree(value: str) -> bool:
        value = urllib.parse.unquote(value.removeprefix("file://"))
        value = value.replace("$USER_HOME$", str(Path.home()))
        path = Path(os.path.expanduser(value))
        return path.is_absolute() and (Path(os.path.normpath(path)).is_relative_to(root) or path.resolve().is_relative_to(root))

    def require_unrelated(path: Path, values: list[str]) -> None:
        checked.append(str(path))
        if any(refers_to_worktree(value) for value in values):
            raise InspectError("IDE records reference this worktree; use a live IDE to prove cleanup.", 3,
                               {"offline_retirement_record": str(path)})

    try:
        # Shared inspection profiles/vcs.xml are copied by Git and do not prove
        # this worktree was prepared. A virtualenv alone is not a registration either.
        project_state = [path for path in children(root / ".idea")
                         if path.name in {"misc.xml", "modules.xml", "workspace.xml", ".name"} or path.suffix == ".iml"]
        for path in [*project_state, repository_preparation_receipt_path({"worktree_root": str(root)})]:
            if present(path):
                raise InspectError("IDE preparation records exist; use a live IDE to prove cleanup.", 3,
                                   {"offline_retirement_record": str(path)})
        configs = children(Path.home() / "Library" / "Application Support" / "JetBrains")
        override = os.environ.get("JETBRAINS_INSPECTION_IDE_CONFIG_DIR")
        if override:
            # Add the override, never let it hide other installed IDE records.
            configs.append(Path(override).expanduser())
        for config in configs:
            if not stat.S_ISDIR(config.stat().st_mode):
                continue
            for name in ("jdk.table.xml", HELPER_SDK_RECORD_FILENAME, "recentProjects.xml", "recentProjectDirectories.xml"):
                path = config / "options" / name
                if not present(path):
                    continue
                document = ElementTree.parse(path)
                if document.getroot().tag != "application":
                    raise ValueError("Invalid IDE application records")
                require_unrelated(path, [value for element in document.iter() for value in element.attrib.values()])
            # Custom system paths cannot be proved from the standard cache root.
            properties = config / "idea.properties"
            if present(properties) and any(re.match(r"\s*idea\.(system|config)\.path\s*=", line)
                                           for line in properties.read_text().splitlines()):
                raise InspectError("Custom IDE storage paths require live IDE cleanup proof.", 3,
                                   {"offline_retirement_record": str(properties)})
        for system in children(Path.home() / "Library" / "Caches" / "JetBrains"):
            if not stat.S_ISDIR(system.stat().st_mode):
                continue
            for project in children(system / "projects"):
                if (not project.name.startswith((HELPER_FINALIZING_CACHE_PREFIX, HELPER_SEALED_CACHE_PREFIX))
                        and not stat.S_ISDIR(project.stat().st_mode)):
                    continue
                marker = project if project.name.startswith(HELPER_FINALIZING_CACHE_PREFIX) else project / HELPER_PROJECT_CACHE_MARKER
                if not present(marker):
                    if project.name.startswith(HELPER_SEALED_CACHE_PREFIX):
                        raise InspectError("Unresolved IDE cache ownership requires live IDE cleanup proof.", 3,
                                           {"offline_retirement_record": str(marker)})
                    continue
                record = json.loads(marker.read_text())
                if (not isinstance(record, dict) or not isinstance(record.get("worktree_path"), str)
                        or not Path(record["worktree_path"]).is_absolute()):
                    raise InspectError("Invalid IDE cache ownership requires live IDE cleanup proof.", 3,
                                       {"offline_retirement_record": str(marker)})
                require_unrelated(marker, [record["worktree_path"]])
        return checked
    except (OSError, ValueError, ElementTree.ParseError) as error:
        raise InspectError("Cannot read offline IDE records; retain the worktree and restore record access or use a live IDE.", 3,
                           {"offline_retirement_error": type(error).__name__,
                            "offline_retirement_record": getattr(error, "filename", None)}) from error


def unregister_helper_sdks(identities: list[dict[str, Any]], root: Path | None, dry_run: bool,
                           offline_records: list[str] | None = None) -> list[dict[str, Any]]:
    if not identities:
        if root is not None:
            records = offline_retirement_records(root)
            if offline_records is not None:
                offline_records.extend(records)
            return []
        raise InspectError("No live IDE is available to prove SDK cleanup; start the prepared worktree's IDE first.", 3)
    # Validate every session before sending any mutating request.
    for identity in identities:
        if identity.get("helper_sdk_lifecycle_version") != 1 or not identity.get("session_id") or not identity.get("port"):
            raise InspectError("Install the plugin with helper-owned SDK lifecycle support in every discovered IDE before cleanup.", 3)
    if root is not None:
        for identity in identities:
            for project in identity.get("open_projects") or []:
                base = project.get("base_path")
                if not base or Path(base).resolve().is_relative_to(root.resolve()):
                    raise InspectError("Close every IDE project at or inside the worktree before removal.", 3)
    results = []
    seen: set[tuple[Any, Any]] = set()
    for identity in identities:
        key = (identity["port"], identity["session_id"])
        if key in seen:
            continue
        seen.add(key)
        params = {"session_id": identity["session_id"], "dry_run": str(dry_run).lower()}
        params.update({"worktree_path": str(root)} if root is not None else {"orphans": "true"})
        try:
            body = http_post(identity["port"], "lifecycle/unregister-python-sdk", params, timeout=15.0).body
        except InspectError as error:
            # A refused orphan preview still contains the candidates and held records.
            if error.payload.get("http_status") == 409 and error.payload.get("status") == "refused" and not error.payload.get("reason") and (root is not None or dry_run):
                body = error.payload
            else:
                error.payload["completed_sdk_cleanup"] = results
                raise
        entries = body.get("sdks")
        orphan_preview = root is None and dry_run
        valid_statuses = {"would_remove", "absent"} if dry_run else {"removed", "absent"}
        valid_reasons = {"helper_owned", "already_absent"}
        if root is not None:
            valid_statuses.add("refused")
            valid_reasons.add("not_helper_owned")
        if orphan_preview:
            valid_statuses.add("refused")
            valid_reasons.update({"sdk_in_use", "ownership_mismatch", "ambiguous_sdk"})
        if (body.get("session_id") != identity["session_id"] or body.get("reason") or body.get("status") not in {"ok", "refused"}
            or body.get("sdk_lifecycle_version") != 1 or body.get("dry_run") is not dry_run
            or not isinstance(entries, list) or any(
                not isinstance(entry, dict) or entry.get("status") not in valid_statuses
                or entry.get("reason") not in valid_reasons
                or (entry.get("status") == "refused" and not orphan_preview and entry.get("reason") != "not_helper_owned")
                or not isinstance(entry.get("sdk_name"), str) or not entry.get("sdk_name")
                or not isinstance(entry.get("worktree_path"), str)
                or not Path(entry.get("worktree_path") or "").is_absolute()
                or (root is not None and not paths_same(entry.get("worktree_path"), root))
                for entry in entries)):
            raise InspectError("SDK cleanup did not prove a successful helper-owned result; retain the worktree.", 3,
                               {"sdk_cleanup": public_payload(body), "completed_sdk_cleanup": results, "http_status": body.get("http_status")})
        results.append({"ide": public_identity_summary(identity), "result": public_payload(body)})
    return results


def retire_helper_project_caches(identities: list[dict[str, Any]], root: Path, dry_run: bool) -> list[dict[str, Any]]:
    results = []
    for identity in identities:
        if identity.get("helper_project_cache_lifecycle_version") != 1:
            results.append({"ide": public_identity_summary(identity), "result": {
                "status": "unsupported", "reason": "plugin_capability_unavailable", "caches": [],
            }})
            continue
        params = {"worktree_path": str(root), "session_id": identity["session_id"],
                  "dry_run": "true" if dry_run else "false"}
        try:
            body = http_post(identity["port"], "lifecycle/retire-project-caches", params, timeout=120.0).body
        except InspectError as error:
            error.payload.setdefault("completed_project_cache_preview" if dry_run else "completed_project_cache_cleanup", results)
            raise
        except (OSError, http.client.HTTPException) as error:
            raise InspectError("Project-cache transport failed; retain the worktree.", 3,
                               {"error_reason": "inspection_api_transport_error",
                                "completed_project_cache_preview" if dry_run else "completed_project_cache_cleanup": results}) from error
        entries = body.get("caches")
        allowed_statuses = {"would_remove", "absent"} if dry_run else {"removed", "absent"}
        if (body.get("session_id") != identity["session_id"] or body.get("status") != "ok"
            or body.get("project_cache_lifecycle_version") != 1 or body.get("dry_run") is not dry_run
            or not paths_same(body.get("worktree_path"), root) or not isinstance(entries, list)
            or any(not isinstance(entry, dict) or entry.get("status") not in allowed_statuses
                   or entry.get("reason") != "helper_owned_closed_project"
                   or not paths_same(entry.get("worktree_path"), root)
                   for entry in entries)):
            raise InspectError("Project-cache retirement was not proved; retain the worktree.", 3,
                               {"project_cache_retirement": public_payload(body), "completed_project_cache_preview" if dry_run else "completed_project_cache_cleanup": results})
        results.append({"ide": public_identity_summary(identity), "result": public_payload(body)})
    return results


def command_retire_sdks(args: argparse.Namespace) -> dict[str, Any]:
    preview: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    cache_preview: list[dict[str, Any]] = []
    cache_cleanup: list[dict[str, Any]] = []
    offline_records: list[str] = []
    with lifecycle_lock(args.lifecycle_lock_timeout_ms):
        try:
            offline_only = getattr(args, "offline_only", False)
            root = retirement_worktree(Path(args.repo), args.dry_run, offline_only) if args.command == "remove-worktree" else None
            identities = [] if offline_only else discover_identities(None)
            proof_identities = identities
            preview = unregister_helper_sdks(identities, root, True, offline_records)
            if root is not None:
                cache_preview = retire_helper_project_caches(identities, root, True)
            if not args.dry_run:
                if root is not None:
                    # Only IDEs with a helper-owned SDK need the mutating call; the final preview rechecks every IDE.
                    for item in preview:
                        if any(entry["status"] == "would_remove" for entry in item["result"]["sdks"]):
                            identity = next(identity for identity in identities if
                                            identity["session_id"] == item["ide"]["session_id"] and identity["port"] == item["ide"]["port"])
                            applied.extend(unregister_helper_sdks([identity], root, False))
                        else:
                            skipped.append(item)
                else:
                    # Explicit paths bind apply to the earlier reviewed dry-run.
                    selected = {str(Path(path).expanduser().resolve()) for path in (getattr(args, "worktree_path", None) or [])}
                    refused_roots = {str(Path(entry["worktree_path"]).resolve()) for item in preview for entry in item["result"]["sdks"] if entry["status"] == "refused"}
                    candidates = {str(Path(entry["worktree_path"]).resolve()) for item in preview for entry in item["result"]["sdks"]} - refused_roots
                    if candidates and not selected:
                        raise InspectError("Repeat each reviewed orphan path with --worktree-path before applying cleanup.", 3)
                    if not selected.issubset(candidates):
                        raise InspectError("Selected orphan paths are absent from the current helper-owned preview.", 3)
                    for candidate in selected:
                        path = Path(candidate)
                        if path.exists() or not path.parent.is_dir() or path.is_symlink():
                            raise InspectError("Orphan removal requires a missing worktree and accessible parent.", 3)
                    for item in preview:
                        identity = next(identity for identity in identities if
                                        identity["session_id"] == item["ide"]["session_id"] and identity["port"] == item["ide"]["port"])
                        roots = {str(Path(entry["worktree_path"]).resolve()) for entry in item["result"]["sdks"]} & selected
                        for candidate in sorted(roots):
                            path = Path(candidate)
                            if path.exists() or not path.parent.is_dir() or path.is_symlink():
                                raise InspectError("Orphan path changed before SDK removal.", 3)
                            applied.extend(unregister_helper_sdks([identity], path, False))
                    selected_sdk_names = {entry["sdk_name"] for item in preview for entry in item["result"]["sdks"]
                                          if str(Path(entry["worktree_path"]).resolve()) in selected}
                    remaining = unregister_helper_sdks(identities, None, True)
                    if any(entry.get("sdk_name") in selected_sdk_names and entry["status"] != "absent"
                           for item in remaining for entry in item["result"]["sdks"]):
                        raise InspectError("Reviewed orphan SDKs remain registered after apply.", 3, {"sdk_remaining": remaining})
                if root is not None:
                    # Recheck local state and live projects after SDK retirement.
                    retirement_worktree(root, False, offline_only)
                    current = [] if offline_only else discover_identities(None)
                    proof_identities = current
                    final_preview = unregister_helper_sdks(current, root, True, offline_records)
                    if any(entry["status"] != "absent" and entry["reason"] != "not_helper_owned" for item in final_preview for entry in item["result"]["sdks"]):
                        raise InspectError("An SDK still needs retirement; retain the worktree and repeat the preview.", 3,
                                           {"sdk_remaining": final_preview})
                    common = git_common_worktree(root)
                    if common is None:
                        raise InspectError("Cannot resolve the primary checkout for worktree removal.", 3)
                    cache_cleanup = retire_helper_project_caches(current, root, False)
                    remaining_caches = retire_helper_project_caches(current, root, True)
                    if any(entry["status"] != "absent" for item in remaining_caches for entry in item["result"]["caches"]):
                        raise InspectError("Project caches still need retirement; retain the worktree.", 3,
                                           {"project_cache_remaining": remaining_caches})
                    before = worktree_entries(root)
                    completed = subprocess.run(["git", "-C", str(common), "worktree", "remove", str(root)],
                                               capture_output=True, text=True)
                    if completed.returncode or root.exists():
                        finish_raced_worktree_removal(common, root, before, completed.stderr)
            return {"status": "ok", "dry_run": args.dry_run, "worktree_path": str(root) if root else None,
                    "offline_cleanup_proved": root is not None and not proof_identities,
                    "offline_records_checked": sorted(set(offline_records)),
                    "project_cache_preview": cache_preview, "project_cache_cleanup": cache_cleanup,
                    "sdk_preview": preview, "sdk_cleanup": applied, "sdk_apply_skipped": [item["ide"] for item in skipped],
                    "worktree_removed": root is not None and not args.dry_run,
                    "refused_preview_sdk_count": sum(entry["status"] == "refused" for item in preview for entry in item["result"]["sdks"]),
                    "preserved_unowned_sdk_count": sum(entry["reason"] == "not_helper_owned" for item in (preview if args.dry_run else applied + skipped) for entry in item["result"]["sdks"])}
        except (subprocess.CalledProcessError, OSError) as error:
            raise InspectError("Worktree retirement failed; retain the worktree and inspect the Git/filesystem error.", 3,
                               {"sdk_preview": preview, "sdk_cleanup": applied, "project_cache_preview": cache_preview,
                                "completed_project_cache_cleanup": cache_cleanup, "error": str(error)}) from error
        except InspectError as error:
            error.payload.setdefault("project_cache_preview", cache_preview)
            error.payload["completed_project_cache_cleanup"] = cache_cleanup + error.payload.get("completed_project_cache_cleanup", [])
            error.payload.update({"sdk_preview": preview, "completed_sdk_cleanup": applied + error.payload.get("completed_sdk_cleanup", [])})
            raise


def command_cleanup_leases(args: argparse.Namespace) -> dict[str, Any]:
    with lifecycle_lock(getattr(args, "lifecycle_lock_timeout_ms", DEFAULT_LIFECYCLE_LOCK_TIMEOUT_MS)):
        return cleanup_stale_helper_leases(args)


def cleanup_stale_helper_leases(args: argparse.Namespace) -> dict[str, Any]:
    removed: list[str] = []
    stale: list[str] = []
    closed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    local_leases = read_local_leases()
    if getattr(args, "worktree_root", None) is not None:
        local_leases = [(path, lease) for path, lease in local_leases
                        if paths_same(lease.get("worktree_root"), args.worktree_root)]
        ids = [lease.get("lease_id") for _, lease in local_leases]
        try:
            valid = len(set(ids)) == len(ids) and all(
                str(uuid.UUID(lease["lease_id"])) == path.stem for path, lease in local_leases
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            valid = False
        if not valid:
            return {"status": "incomplete", "dry_run": args.dry_run, "stale": [], "removed": [],
                    "closed": [], "unresolved": [], "failed": [{"reason": "lease_identity_mismatch"}]}
    selectors = getattr(args, "lease_id", None)
    selected_id = None
    if selectors is not None:
        reason = None
        try:
            if len(selectors) != 1:
                raise ValueError("Exactly one lease selector is required.")
            selected_id = str(uuid.UUID(selectors[0]))
        except (ValueError, TypeError, AttributeError):
            reason = "invalid_lease_selector"
        if reason is None:
            local_leases = [(path, lease) for path, lease in local_leases if lease.get("lease_id") == selected_id]
            if not local_leases:
                reason = "lease_not_found"
            elif len(local_leases) != 1:
                reason = "lease_identity_ambiguous"
            elif local_leases[0][0].stem != selected_id:
                reason = "lease_identity_mismatch"
        if reason is not None:
            return {
                "status": "incomplete", "dry_run": args.dry_run,
                "stale": [], "removed": [], "closed": [], "unresolved": [],
                "failed": [{"status": "failed", "reason": reason, "lease_id": selected_id}],
            }
    if not local_leases:
        return {"status": "ok", "dry_run": args.dry_run, "would_remove": [],
                "removed": [], "stale": [], "closed": [], "failed": [], "unresolved": []}
    cutoff = now_ms() - int(args.max_age_ms)
    stale_leases: list[tuple[Path, dict[str, Any]]] = []
    for path, lease in local_leases:
        updated = int(lease.get("updated_at_ms") or lease.get("created_at_ms") or 0)
        pid = lease.get("pid")
        # Age is useful for legacy records without a PID; it never overrides a live helper.
        is_stale = not pid_alive(int(pid)) if pid else updated < cutoff
        if not is_stale:
            continue
        stale.append(str(path))
        stale_leases.append((path, lease))
    if selected_id is not None and not stale_leases:
        return {
            "status": "incomplete", "dry_run": args.dry_run,
            "stale": [], "removed": [], "closed": [], "failed": [],
            "unresolved": [{"status": "skipped", "reason": "lease_not_stale", "lease_id": selected_id}],
        }
    if not stale_leases:
        return {
            "status": "ok",
            "dry_run": args.dry_run,
            "stale": stale,
            "removed": [],
            "closed": [],
            "failed": [],
            "unresolved": [],
        }
    routes: list[dict[str, Any]] = []
    observed_sessions: set[str] = set()
    if any(lease_may_own_open_project(lease) for _, lease in stale_leases):
        try:
            routes, observed_sessions = discover_routes_for_cleanup(args)
        except (InspectError, OSError) as error:
            return {
                "status": "incomplete",
                "dry_run": args.dry_run,
                "stale": stale,
                "removed": [],
                "closed": [],
                "failed": [
                    {
                        "status": "failed",
                        "reason": "route_discovery_failed",
                        "message": str(error),
                    }
                ],
                "unresolved": [],
            }
    for path, lease in stale_leases:
        cleanup_result = (cleanup_stale_helper_lease(lease, routes, observed_sessions, dry_run=True)
                          if args.dry_run else cleanup_stale_helper_lease(lease, routes, observed_sessions))
        if cleanup_result.get("status") == "closed":
            closed.append(cleanup_result)
            if not args.dry_run:
                path.unlink(missing_ok=True)
            removed.append(str(path))
        elif cleanup_result.get("status") == "failed":
            failed.append(cleanup_result)
        elif cleanup_result.get("status") == "skipped":
            unresolved.append(cleanup_result)
        elif cleanup_result.get("reason") in {
            "open_not_attempted",
            "project_not_open",
            "project_preexisted",
            "ownership_not_proven",
            "original_ide_process_dead_no_route",
            "original_ide_reports_project_not_open",
        }:
            if not args.dry_run:
                path.unlink(missing_ok=True)
            removed.append(str(path))
    return {
        "status": "incomplete" if failed or unresolved else "ok",
        "dry_run": args.dry_run,
        "stale": stale,
        "removed": [] if args.dry_run else removed,
        "would_remove": removed if args.dry_run else [],
        "closed": closed,
        "failed": failed,
        "unresolved": unresolved,
    }


def discover_routes_for_cleanup(args: argparse.Namespace) -> tuple[list[dict[str, Any]], set[str]]:
    routes: list[dict[str, Any]] = []
    observed_sessions: set[str] = set()
    identities = discover_identities(getattr(args, "port", None))
    for identity in identities:
        port = identity.get("port")
        session_id = identity.get("session_id")
        if session_id:
            observed_sessions.add(str(session_id))
        if not port:
            continue
        for project in identity.get("open_projects") or []:
            if isinstance(project, dict):
                route = project.copy()
                route.setdefault("port", port)
                route.setdefault("session_id", session_id)
                route.setdefault("ide", {
                    "name": identity.get("ide_name") or identity.get("name"),
                    "product_code": identity.get("ide_product_code") or identity.get("product_code"),
                    "version": identity.get("ide_version") or identity.get("version"),
                    "plugin_version": identity.get("plugin_version"),
                    "plugin_build_fingerprint": identity.get("plugin_build_fingerprint"),
                    "lifecycle_ownership_protocol": identity.get("lifecycle_ownership_protocol"),
                })
                routes.append(route)
    return routes, observed_sessions


def cleanup_stale_helper_lease(
    lease: dict[str, Any],
    routes: list[dict[str, Any]],
    observed_sessions: set[str] | None = None,
    *, dry_run: bool = False,
) -> dict[str, Any]:
    if lease_proves_open_not_attempted(lease):
        return {"status": "not_needed", "reason": "open_not_attempted", "lease_id": lease.get("lease_id")}
    if not lease_may_own_open_project(lease):
        if lease.get("opened_by_helper") is not False:
            return {"status": "skipped", "reason": "ownership_evidence_missing", "lease_id": lease.get("lease_id")}
        return {"status": "not_needed", "reason": "project_preexisted", "lease_id": lease.get("lease_id")}
    sessions = observed_sessions if observed_sessions is not None else {
        str(route.get("session_id")) for route in routes if route.get("session_id")
    }
    route = matching_route_for_lease(lease, routes) if lease_has_exact_route_identity(lease) else matching_route_for_ownership_probe(lease, routes)
    if route is None:
        if lease_has_exact_route_identity(lease) and lease.get("session_id") in sessions:
            return {"status": "not_needed", "reason": "project_not_open", "lease_id": lease.get("lease_id")}
        if (can_release_dead_original_ide_lease(lease, routes, observed_sessions)
                or timed_out_open_died_with_original_ide(lease, routes, observed_sessions)):
            return {
                "status": "not_needed",
                "reason": "original_ide_process_dead_no_route",
                "lease_id": lease.get("lease_id"),
                "released_without_close": True,
            }
        probe = original_session_open_probe(lease, sessions)
        if probe_reports_project_not_open(lease, probe):
            return {
                "status": "not_needed",
                "reason": "original_ide_reports_project_not_open",
                "lease_id": lease.get("lease_id"),
                "released_without_close": True,
            }
        unresolved = {
            "status": "skipped",
            "reason": "ownership_route_unavailable",
            "lease_id": lease.get("lease_id"),
            "next_action": "Retry cleanup after the original IDE session and exact route are discoverable.",
        }
        if probe is not None:
            unresolved["open_probe"] = {"status": probe.get("status"), "reason": probe.get("reason")}
            if not lease_open_acknowledged_by_session(lease):
                unresolved["next_action"] = (
                    "The original IDE session did not acknowledge every open request for this lease, so a late "
                    "open cannot be ruled out; retry cleanup after the exact route is discoverable."
                )
            elif probe.get("status") != "not_open":
                unresolved["next_action"] = (
                    f"The original IDE session reports this worktree {probe.get('status')} "
                    f"({probe.get('reason')}); retry cleanup after that open settles."
                )
        return unresolved
    if dry_run:
        return {"status": "skipped", "reason": "ownership_preview_requires_apply",
                "lease_id": lease.get("lease_id"),
                "next_action": f"Run cleanup-helper-leases --lease-id {lease.get('lease_id')} --no-dry-run to prove ownership and settled activity."}
    try:
        ownership_proven, close_proof, claim_metadata, claimed_route = reclaim_lifecycle_claim(lease, route)
    except InspectError as error:
        return {
            "status": "failed",
            "reason": public_cleanup_reason(error),
            "lease_id": lease.get("lease_id"),
        }
    if ownership_proven is False:
        lease["opened_by_helper"] = False
        return {"status": "not_needed", "reason": "ownership_not_proven", "lease_id": lease.get("lease_id")}
    if ownership_proven is not True or not close_proof:
        return {
            "status": "skipped",
            "reason": "ownership_unresolved",
            "lease_id": lease.get("lease_id"),
            "claim": claim_metadata,
        }
    persist_claimed_cleanup_ownership(lease, claimed_route, claim_metadata)
    result = cleanup_lifecycle(lease, claimed_route, str(close_proof))
    result.setdefault("lease_id", lease.get("lease_id"))
    return result


def original_session_open_probe(lease: dict[str, Any], observed_sessions: set[str]) -> dict[str, Any] | None:
    """Ask the lease's still-running IDE session whether it has the worktree open, opening, or unresolved."""
    session_id = lease.get("session_id")
    port = lease.get("ide_port")
    target = lease.get("lifecycle_target_path") or lease.get("worktree_root") or lease.get("repo_path")
    if not session_id or str(session_id) not in observed_sessions or not isinstance(port, int) or not target:
        return None
    try:
        identity = identity_for_port(port)
    except (InspectError, OSError):
        return None
    # Plugins without lifecycle open diagnostics would treat the probe as a real open request.
    if identity.get("session_id") != session_id or int(identity.get("lifecycle_open_diagnostic_version") or 0) < 1:
        return None
    try:
        body = http_get(port, "lifecycle/open", {
            "worktree_path": target,
            "session_id": session_id,
            "lease_id": lease.get("lease_id"),
            "probe": "true",
        }).body
    except InspectError as error:
        return {"status": "error", "reason": infer_error_reason(error, error.payload)}
    except OSError as error:
        return {"status": "error", "reason": type(error).__name__.lower()}
    return public_payload(body)


def lease_open_acknowledged_by_session(lease: dict[str, Any]) -> bool:
    """Whether the lease's IDE session registered every open request, so none can still arrive unseen."""
    if lease.get("state") not in SETTLED_LEASE_STATES:
        return False
    if lease.get("open_request_may_have_been_accepted") is not False:
        return False
    attempts = lease.get("open_attempts")
    if not isinstance(attempts, list):
        return False
    # App startup and "no matching IDE" entries sent no project-open request.
    requests = [attempt for attempt in attempts if not project_open_was_not_requested([attempt])]
    if not requests:
        return False
    for attempt in requests:
        if not isinstance(attempt, dict) or attempt.get("request_may_have_been_accepted") is True:
            return False
        identity = dict_field(attempt, "identity")
        if not (
            attempt.get("accepted") is True
            and attempt.get("ownership_registered") is True
            and attempt.get("lease_id") == lease.get("lease_id")
            and (identity.get("session_id") or attempt.get("session_id")) == lease.get("session_id")
        ):
            return False
    return True


def probe_reports_project_not_open(lease: dict[str, Any], probe: dict[str, Any] | None) -> bool:
    return (
        lease_open_acknowledged_by_session(lease)
        and isinstance(probe, dict)
        and probe.get("probe") is True
        and probe.get("status") == "not_open"
        and probe.get("reason") == "project_not_open"
        and probe.get("opened") is False
        and probe.get("opening_scheduled") is False
        and bool(lease.get("session_id"))
        and probe.get("session_id") == lease.get("session_id")
    )


def original_session_gone_and_worktree_closed(
    lease: dict[str, Any],
    routes: list[dict[str, Any]],
    observed_sessions: set[str] | None,
) -> bool:
    if observed_sessions is None or lease.get("state") not in SETTLED_LEASE_STATES:
        return False
    session_id = lease.get("session_id")
    target = lease.get("lifecycle_target_path") or lease.get("worktree_root") or lease.get("repo_path")
    if not lease.get("lease_id") or not session_id or not target or str(session_id) in observed_sessions:
        return False
    for route in routes:
        route_path = route.get("base_path") or route.get("project_file_path")
        if not route_path or paths_same(target, route_path):
            return False
    return True


def can_release_dead_original_ide_lease(
    lease: dict[str, Any],
    routes: list[dict[str, Any]],
    observed_sessions: set[str] | None,
) -> bool:
    if lease.get("opened_by_helper") is not True or lease.get("open_request_may_have_been_accepted") is True:
        return False
    if not original_session_gone_and_worktree_closed(lease, routes, observed_sessions):
        return False
    lease_id = lease.get("lease_id")
    session_id = lease.get("session_id")
    attempts = lease.get("open_attempts")
    if not isinstance(attempts, list) or any(
        isinstance(attempt, dict) and attempt.get("request_may_have_been_accepted") is True
        for attempt in attempts
    ):
        return False
    accepted_identities: list[tuple[str, int]] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        # App startup carries no project-open or lease ownership evidence.
        if attempt.get("method") == "bootstrap_ide" and not any(
            key in attempt for key in ("identity", "lease_id", "ownership_registered", "lifecycle_ownership_protocol")
        ):
            continue
        accepted = attempt.get("accepted") is True
        ownership_registered = attempt.get("ownership_registered") is True
        if not accepted and not ownership_registered:
            continue
        if not accepted or not ownership_registered:
            return False
        if attempt.get("lease_id") != lease_id or attempt.get("lifecycle_ownership_protocol") != LIFECYCLE_OWNERSHIP_PROTOCOL:
            return False
        identity = dict_field(attempt, "identity")
        attempt_session = identity.get("session_id") or attempt.get("session_id")
        ide_pid = identity.get("pid")
        if attempt_session != session_id or not isinstance(ide_pid, int) or ide_pid <= 0:
            return False
        lease_port = lease.get("ide_port")
        identity_port = identity.get("port")
        if lease_port is not None and identity_port != lease_port:
            return False
        accepted_identities.append((str(attempt_session), ide_pid))
    if len(accepted_identities) != 1:
        return False
    return pid_definitively_dead(accepted_identities[0][1])


def timed_out_open_died_with_original_ide(
    lease: dict[str, Any],
    routes: list[dict[str, Any]],
    observed_sessions: set[str] | None,
) -> bool:
    if lease.get("opened_by_helper") is True or not original_session_gone_and_worktree_closed(lease, routes, observed_sessions):
        return False
    attempts = lease.get("open_attempts")
    if not isinstance(attempts, list):
        return False
    sent = [attempt for attempt in attempts if isinstance(attempt, dict) and attempt.get("identity")]
    if not sent or not project_open_was_not_requested([attempt for attempt in attempts if attempt not in sent]):
        return False
    session_id = lease.get("session_id")
    ide_pids: set[int] = set()
    for attempt in sent:
        identity = dict_field(attempt, "identity")
        ide_pid = identity.get("pid")
        if (attempt.get("accepted") is not False or attempt.get("request_may_have_been_accepted") is not True
                or identity.get("session_id") != session_id or identity.get("port") != lease.get("ide_port")
                or not isinstance(ide_pid, int)):
            return False
        ide_pids.add(ide_pid)
    return len(ide_pids) == 1 and pid_definitively_dead(ide_pids.pop())


def reclaim_lifecycle_claim(
    lease: dict[str, Any],
    route: dict[str, Any],
) -> tuple[bool | None, str | None, dict[str, Any], dict[str, Any]]:
    project_instance_id = lease.get("project_instance_id") or route.get("project_instance_id")
    if not project_instance_id:
        return None, None, {"status": "missing_project_instance"}, route
    claim = private_http_get_body(route_port(route), "lifecycle/claim", {
        "project_key": lease.get("project_key") or route.get("project_key"),
        "project_path": route.get("base_path"),
        "worktree_path": route.get("base_path"),
        "session_id": lease.get("session_id") or route.get("session_id"),
        "project_instance_id": project_instance_id,
        "lease_id": lease.get("lease_id"),
    })
    claimed_route = claim.get("route")
    if not isinstance(claimed_route, dict):
        claimed_route = route
    ensure_claim_route_matches(route, claimed_route)
    lease_target = lease.get("lifecycle_target_path") or lease.get("worktree_root") or lease.get("repo_path")
    claimed_path = claimed_route.get("base_path") or claimed_route.get("project_file_path")
    if lease_target and claimed_path and not paths_same(lease_target, claimed_path):
        raise InspectError(
            "Lifecycle ownership claim resolved a different path.",
            3,
            {"error_reason": "route_mismatch", "expected_path": lease_target, "actual_path": claimed_path},
        )
    ownership_proven, close_proof = lifecycle_claim_ownership(claim, lease)
    claim_metadata = {
        "status": claim.get("status") or "unknown",
        "ownership_proven": ownership_proven is True,
        "reason": claim.get("reason"),
        "lifecycle_ownership_protocol": claim.get("lifecycle_ownership_protocol"),
        "lease_id": lease.get("lease_id"),
    }
    return ownership_proven, close_proof, claim_metadata, claimed_route


def matching_route_for_ownership_probe(lease: dict[str, Any], routes: list[dict[str, Any]]) -> dict[str, Any] | None:
    lease_target = lease.get("lifecycle_target_path") or lease.get("worktree_root") or lease.get("repo_path")
    for route in routes:
        route_base = route.get("base_path") or route.get("project_file_path")
        if not paths_same(lease_target, route_base):
            continue
        if lease.get("session_id") and route.get("session_id") != lease.get("session_id"):
            continue
        return route
    return None


def matching_route_for_lease(lease: dict[str, Any], routes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not lease_has_exact_route_identity(lease):
        return None
    for route in routes:
        if route.get("project_instance_id") != lease.get("project_instance_id"):
            continue
        if route.get("session_id") != lease.get("session_id"):
            continue
        if lease.get("project_key") and route.get("project_key") and route.get("project_key") != lease.get("project_key"):
            continue
        route_base = route.get("base_path") or route.get("project_file_path")
        lease_target = lease.get("lifecycle_target_path") or lease.get("worktree_root") or lease.get("repo_path")
        if route_base and lease_target and not paths_same(route_base, lease_target):
            continue
        return route
    return None


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
    except ProcessLookupError:
        return False
    except OSError:
        return True


def pid_definitively_dead(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except OSError:
        return False


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
