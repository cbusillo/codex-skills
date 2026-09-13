#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run one isolated cleanup-behavior case through the installed Codex CLI.

The case JSON contains ``name``, absolute ``workspace``, ``catalog``, ``outcome``,
one or two ``prompts``, optional ``source_receipt``, and optional nonsecret
``environment`` entries. The runner writes sanitized evidence beside ``outcome``.
It never includes evaluator or scorer files in the model-visible skill catalog.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

CAPTURE_LIMIT = 16 * 1024 * 1024
DEFAULT_ARTIFACT_ROOT = Path("/Volumes/Developer-Artifacts")
FIXTURE_MARKER = ".cleanup-fixture"
ENV_ALLOWLIST = {
    "CLEANUP_FIXTURE_PROVIDER_STATE",
    "CODE_HOME",
    "CODEX_AUTOMATION_LOGIN",
    "CODEX_CLEANUP_FIXTURE_MODE",
    "CODEX_CLEANUP_FIXTURE_STATE",
    "CODE_EXEC_HARNESS_GH_STATE",
    "GITHUB_API_GH",
    "GITHUB_READ_GH",
    "GH_COMMENT_GH",
    "GH_ISSUE_GH",
    "GH_WITH_ENV_TOKEN_EXPECTED_LOGIN",
    "UV_PYTHON_INSTALL_DIR",
}
REQUIRED_FLAGS = {
    "--ignore-user-config", "--ignore-rules", "--json", "--output-last-message",
    "--sandbox", "--skip-git-repo-check", "--strict-config",
}
CONFIG = {
    "approval_policy": "never",
    "model_provider": "openai",
    "sandbox_mode": "workspace-write",
    "sandbox_workspace_write.network_access": False,
    "sandbox_workspace_write.exclude_slash_tmp": True,
    "sandbox_workspace_write.exclude_tmpdir_env_var": True,
    "skills.bundled.enabled": False,
    "web_search": "disabled",
}
NATIVE_TOOL_TYPES = {
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
}


class RunnerError(RuntimeError):
    pass


class ProcessCleanupError(RunnerError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path, help="Case JSON file")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--private-root", type=Path)
    return parser.parse_args()


def absolute_dir(value: Any, label: str) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise RunnerError(f"{label} must be an existing absolute directory")
    return path.resolve()


def under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise RunnerError(f"catalog contains a symlink: {relative}")
        digest.update(relative.encode() + b"\0")
        if path.is_file():
            digest.update(file_hash(path).encode())
    return digest.hexdigest()


def validate_catalog(catalog: Path) -> None:
    forbidden_files = {"run_cleanup_cases.py", "test_run_cleanup_cases.py", "score.py", "scorer.py"}
    for path in catalog.rglob("*"):
        relative = path.relative_to(catalog)
        if "evaluations" in relative.parts or path.name in forbidden_files or "expected-answer" in path.name:
            raise RunnerError(f"catalog exposes evaluator material: {relative}")


def open_fixture_file(workspace: Path, relative: str, *, write: bool = False) -> int:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RunnerError("between-turn path must be a normalized relative file")
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory
            )
            os.close(directory)
            directory = child
        descriptor = os.open(
            path.parts[-1], (os.O_RDWR if write else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory
        )
    except OSError as exc:
        raise RunnerError("between-turn target must remain a contained regular file") from exc
    finally:
        os.close(directory)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RunnerError("between-turn target must remain a regular file")
    return descriptor


def load_case(path: Path, artifact_root: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RunnerError("case must be a JSON object")
    prompts = value.get("prompts")
    if not isinstance(prompts, list) or not 1 <= len(prompts) <= 2 or not all(
        isinstance(prompt, str) and prompt.strip() for prompt in prompts
    ):
        raise RunnerError("prompts must contain one or two nonblank strings")
    if not isinstance(value.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9._-]+", value["name"]):
        raise RunnerError("name must be a safe path component")
    changes = value.get("between_turn_files", {})
    if not isinstance(changes, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in changes.items()
    ):
        raise RunnerError("between_turn_files must map relative paths to UTF-8 text")
    if changes and len(prompts) != 2:
        raise RunnerError("between_turn_files requires exactly two prompts")
    environment = value.get("environment", {})
    if not isinstance(environment, dict) or any(key not in ENV_ALLOWLIST for key in environment):
        raise RunnerError("environment contains a key outside the nonsecret allowlist")
    if not all(isinstance(item, str) and "\n" not in item and "\r" not in item for item in environment.values()):
        raise RunnerError("environment values must be single-line strings")
    value["workspace"] = absolute_dir(value.get("workspace"), "workspace")
    value["catalog"] = absolute_dir(value.get("catalog"), "catalog")
    outcome = Path(str(value.get("outcome"))).expanduser()
    if not outcome.is_absolute() or outcome.exists() and not outcome.is_file():
        raise RunnerError("outcome must be an absolute file path")
    value["outcome"] = outcome.resolve()
    if not value["outcome"].parent.is_dir() or not under(value["outcome"], artifact_root):
        raise RunnerError("outcome parent must exist under the artifact root")
    if under(value["catalog"], value["workspace"]) or under(value["workspace"], value["catalog"]):
        raise RunnerError("workspace and catalog must be disjoint")
    if under(value["outcome"], value["workspace"]) or under(value["outcome"], value["catalog"]):
        raise RunnerError("outcome must be outside workspace and catalog")
    if under(path, value["workspace"]) or under(path, value["catalog"]):
        raise RunnerError("case JSON must be outside workspace and catalog")
    if value.get("source_receipt") is not None:
        receipt = Path(str(value["source_receipt"])).expanduser()
        if (
            not receipt.is_absolute() or receipt.is_symlink() or not receipt.is_file()
            or under(receipt.resolve(), value["workspace"])
            or under(receipt.resolve(), value["catalog"])
        ):
            raise RunnerError("source_receipt must be a regular external absolute file")
        value["source_receipt"] = receipt.resolve()
    for key, item in environment.items():
        if key.endswith("_GH"):
            executable = Path(item)
            if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
                raise RunnerError(f"{key} must name an absolute executable fake adapter")
            if under(executable.resolve(), value["workspace"]) or under(executable.resolve(), value["catalog"]):
                raise RunnerError(f"{key} must be outside workspace and catalog")
        if key in {"CODE_HOME", "CLEANUP_FIXTURE_PROVIDER_STATE", "CODE_EXEC_HARNESS_GH_STATE"}:
            fixture_path = Path(item)
            if not fixture_path.is_absolute() or not under(fixture_path.resolve(), value["workspace"]):
                raise RunnerError(f"{key} must resolve inside the fixture workspace")
    marker = value["workspace"] / FIXTURE_MARKER
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
        raise RunnerError(f"workspace requires a regular bounded {FIXTURE_MARKER}")
    marker_value = json.loads(marker.read_text(encoding="utf-8"))
    required_marker = {
        "schema_version": 1,
        "purpose": "cleanup-behavior-fixture",
        "case": value["name"],
    }
    if not isinstance(marker_value, dict) or any(
        marker_value.get(key) != item for key, item in required_marker.items()
    ):
        raise RunnerError("workspace fixture marker does not match the case")
    for relative in changes:
        os.close(open_fixture_file(value["workspace"], relative))
    validate_catalog(value["catalog"])
    return value


def sanitized_env(run_root: Path, workspace: Path, injected: dict[str, str]) -> dict[str, str]:
    runtime = workspace / ".cleanup-runner"
    (runtime / "tmp").mkdir(parents=True, exist_ok=True)
    (runtime / "uv-cache").mkdir(exist_ok=True)
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "USER", "LOGNAME") if key in os.environ}
    env.update({
        "HOME": str(run_root / "home"),
        "CODEX_HOME": str(run_root / "codex-home"),
        "TMPDIR": str(runtime / "tmp"),
        "UV_CACHE_DIR": str(runtime / "uv-cache"),
        "UV_OFFLINE": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    configured_uv = injected.get("UV_PYTHON_INSTALL_DIR") or os.environ.get("UV_PYTHON_INSTALL_DIR")
    if not configured_uv:
        uv = shutil.which("uv")
        if uv:
            result = subprocess.run(
                [uv, "python", "dir"], text=True, capture_output=True,
                check=False, timeout=10
            )
            configured_uv = result.stdout.strip() if result.returncode == 0 else None
    if not configured_uv or not Path(configured_uv).is_absolute() or not Path(configured_uv).is_dir():
        raise RunnerError("an existing UV_PYTHON_INSTALL_DIR is required for offline helpers")
    env["UV_PYTHON_INSTALL_DIR"] = str(Path(configured_uv).resolve())
    env.update({key: item for key, item in injected.items() if key != "UV_PYTHON_INSTALL_DIR"})
    return env


def copy_auth(run_root: Path) -> list[str]:
    source = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    destination = run_root / "codex-home"
    destination.mkdir(mode=0o700)
    auth = source / "auth.json"
    if not auth.is_file():
        raise RunnerError("active Codex auth.json is unavailable")
    target = destination / "auth.json"
    shutil.copy2(auth, target)
    target.chmod(0o600)
    data = json.loads(target.read_text(encoding="utf-8"))
    strings: list[str] = []
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str) and len(item) >= 8:
            strings.append(item)
    return strings


def redact(text: str, secrets: list[str], private_root: Path) -> str:
    text = text.replace(str(private_root), "[private-run]")
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[redacted-auth]")
    return text


def signal_group(process: subprocess.Popen[bytes], sig: int, deadline: float) -> bool:
    while True:
        process.poll()
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            if time.monotonic() >= deadline:
                raise ProcessCleanupError(
                    "owned Codex process group cleanup remained permission-denied"
                ) from exc
            time.sleep(0.025)
        else:
            return True


def stop_group(process: subprocess.Popen[bytes]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + 0.75
        if not signal_group(process, sig, deadline):
            process.wait(timeout=0.75)
            return
        while time.monotonic() < deadline:
            if not signal_group(process, 0, deadline):
                process.wait(timeout=0.75)
                return
            time.sleep(0.025)
    raise ProcessCleanupError("owned Codex process group did not stop")


def invoke(command: list[str], prompt: str, env: dict[str, str], cwd: Path, timeout: float, capture: Path) -> tuple[int, str, str, float]:
    started = time.monotonic()
    capture.mkdir(mode=0o700)
    input_path = capture / "stdin"
    input_path.write_text(prompt, encoding="utf-8")
    with (
        (capture / "raw.stdout").open("wb") as stdout,
        (capture / "raw.stderr").open("wb") as stderr,
        input_path.open("rb") as stdin,
    ):
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
            start_new_session=True
        )
        deadline = started + timeout
        failure = None
        while process.poll() is None:
            size = (capture / "raw.stdout").stat().st_size + (capture / "raw.stderr").stat().st_size
            if size > CAPTURE_LIMIT:
                failure = "Codex output exceeded 16 MiB"
                break
            if time.monotonic() >= deadline:
                failure = f"Codex timed out after {timeout:g}s"
                break
            time.sleep(0.025)
        returncode = process.returncode
        stop_group(process)
        if failure and failure.startswith("Codex output"):
            raise RunnerError(failure)
        if failure:
            returncode = 124
    if sum((capture / name).stat().st_size for name in ("raw.stdout", "raw.stderr")) > CAPTURE_LIMIT:
        raise RunnerError("Codex output exceeded 16 MiB")
    return returncode, (capture / "raw.stdout").read_text(errors="replace"), (capture / "raw.stderr").read_text(errors="replace"), time.monotonic() - started


def json_events(text: str) -> list[dict[str, Any]]:
    events = []
    for line in text.splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise RunnerError("Codex emitted a non-object JSONL event")
            events.append(value)
    return events


def apply_between_turn_files(workspace: Path, changes: dict[str, str]) -> list[dict[str, str]]:
    receipts = []
    for relative, replacement in sorted(changes.items()):
        path = workspace / relative
        descriptor = open_fixture_file(workspace, relative, write=True)
        with os.fdopen(descriptor, "r+b") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise RunnerError("between-turn target changed from a regular file")
            before = handle.read()
            handle.seek(0)
            handle.truncate()
            handle.write(replacement.encode())
            handle.flush()
            os.fsync(handle.fileno())
        receipts.append({
            "path": relative,
            "sha256_before": hashlib.sha256(before).hexdigest(),
            "sha256_after": file_hash(path),
        })
    return receipts


def rollout_attribution(home: Path, thread_id: str | None) -> dict[str, Any]:
    attribution: dict[str, Any] = {"served_model": None, "served_model_evidence": "not exposed by codex exec or rollout"}
    for path in home.glob("sessions/**/*.jsonl"):
        items = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        matching = False
        for item in items:
            payload = item.get("payload", {})
            if item.get("type") == "session_meta" and payload.get("id") == thread_id:
                matching = True
                attribution.update({key: payload.get(key) for key in ("cli_version", "model_provider", "source")})
        if not matching:
            continue
        contexts = []
        for item in items:
            payload = item.get("payload", {})
            if item.get("type") == "turn_context" and isinstance(payload, dict):
                contexts.append({key: payload.get(key) for key in (
                    "model", "effort", "approval_policy", "sandbox_policy", "workspace_roots"
                )})
        attribution["turn_contexts"] = contexts
        if contexts:
            attribution["turn_context"] = contexts[-1]
    return attribution


def native_tool_evidence(
    home: Path,
    thread_id: str | None,
    secrets: list[str],
    private_root: Path,
) -> tuple[str, int]:
    if not thread_id:
        return "", 0
    matching: list[Path] = []
    for path in home.glob("sessions/**/*.jsonl"):
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                payload = item.get("payload", {})
                if item.get("type") == "session_meta" and payload.get("id") == thread_id:
                    matching.append(path)
                    break
    if len(matching) > 1:
        raise RunnerError("multiple native rollout files match the Codex thread")
    if not matching:
        return "", 0

    lines: list[str] = []
    size = 0
    with matching[0].open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            payload = item.get("payload", {})
            if item.get("type") != "response_item" or not isinstance(payload, dict):
                continue
            if payload.get("type") not in NATIVE_TOOL_TYPES:
                continue
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
            size += len(encoded.encode())
            if size > CAPTURE_LIMIT:
                raise RunnerError("native tool evidence exceeded 16 MiB")
            lines.append(redact(encoded, secrets, private_root))
    return "".join(lines), len(lines)


def main() -> int:
    args = parse_args()
    runner_before = file_hash(Path(__file__))
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise RunnerError("--timeout must be positive")
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", args.model) or not re.fullmatch(
        r"[A-Za-z0-9._+-]+", args.effort
    ):
        raise RunnerError("model and effort must be safe nonblank identifiers")
    artifact_root = args.artifact_root.expanduser()
    if (
        not artifact_root.is_absolute()
        or artifact_root.is_symlink()
        or not artifact_root.is_dir()
    ):
        raise RunnerError("--artifact-root must be an existing absolute directory")
    artifact_root = artifact_root.resolve()
    case = load_case(args.case.resolve(), artifact_root)
    private_root = (args.private_root or case["outcome"].parent / ".cleanup-runner-private").resolve()
    if not under(private_root, artifact_root) or under(private_root, case["workspace"]) or under(private_root, case["catalog"]):
        raise RunnerError("private root must be external to inputs and under the artifact root")
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_root = private_root / f"{case['name']}-{uuid.uuid4().hex}"
    artifacts = case["outcome"].parent / f"{case['outcome'].stem}.artifacts"
    if not under(artifacts.resolve(), artifact_root) or under(artifacts.resolve(), case["workspace"]):
        raise RunnerError("artifacts must be outside the workspace and under the artifact root")
    artifacts.mkdir(parents=True, exist_ok=False, mode=0o700)
    binary = Path(shutil.which(args.codex_bin) or args.codex_bin).resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RunnerError("--codex-bin must resolve to an executable file")
    catalog_before = tree_hash(case["catalog"])
    receipt_before = file_hash(case["source_receipt"]) if case.get("source_receipt") else None
    run_root.mkdir(mode=0o700)
    cleanup_safe = True
    try:
        (run_root / "home" / ".agents").mkdir(parents=True, mode=0o700)
        (run_root / "home" / ".agents" / "skills").symlink_to(
            case["catalog"], target_is_directory=True
        )
        secrets = copy_auth(run_root)
        env = sanitized_env(run_root, case["workspace"], case.get("environment", {}))
        version = subprocess.run(
            [binary, "--version"], env=env, text=True, capture_output=True,
            check=True, timeout=10
        ).stdout.strip()
        help_text = subprocess.run(
            [binary, "exec", "--help"], env=env, text=True,
            capture_output=True, check=True, timeout=10
        ).stdout
        if not version.startswith("codex-cli ") or any(
            flag not in help_text for flag in REQUIRED_FLAGS
        ):
            raise RunnerError("selected binary is not a compatible Codex CLI")
        config = dict(CONFIG, model_reasoning_effort=args.effort)
        overrides = [f"{key}={json.dumps(value)}" for key, value in config.items()]
        turn_results, thread_id, between_turn_receipts = [], None, []
        for index, prompt in enumerate(case["prompts"], 1):
            turn_dir = artifacts / f"turn-{index:02d}"
            turn_dir.mkdir(mode=0o700)
            last_message = run_root / f"last-message-{index:02d}.txt"
            command = [str(binary), "exec", "--strict-config", "--ignore-user-config", "--ignore-rules", "--json", "--color", "never", "--skip-git-repo-check", "-m", args.model]
            for override in overrides:
                command.extend(["-c", override])
            if thread_id is None:
                command.extend(["-C", str(case["workspace"]), "-s", "workspace-write"])
            else:
                command.extend(["resume", thread_id])
            command.extend(["-o", str(last_message), "-"])
            (turn_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
            (turn_dir / "command.json").write_text(json.dumps([redact(item, [], run_root) for item in command], indent=2) + "\n", encoding="utf-8")
            returncode, stdout, stderr, elapsed = invoke(
                command, prompt, env, case["workspace"], args.timeout,
                run_root / f"capture-{index:02d}"
            )
            events = json_events(stdout)
            if thread_id is None:
                thread_id = next((event.get("thread_id") for event in events if event.get("type") == "thread.started"), None)
            clean_stdout, clean_stderr = redact(stdout, secrets, run_root), redact(stderr, secrets, run_root)
            (turn_dir / "stdout.jsonl").write_text(clean_stdout, encoding="utf-8")
            (turn_dir / "stderr.log").write_text(clean_stderr, encoding="utf-8")
            final = redact(last_message.read_text(encoding="utf-8", errors="replace"), secrets, run_root) if last_message.is_file() else ""
            (turn_dir / "last-message.txt").write_text(final, encoding="utf-8")
            turn_results.append({"index": index, "returncode": returncode, "elapsed_seconds": round(elapsed, 3), "event_count": len(events), "completed": any(event.get("type") == "turn.completed" for event in events)})
            if returncode != 0 or not turn_results[-1]["completed"] or index < len(case["prompts"]) and not thread_id:
                break
            if index == 1 and case.get("between_turn_files"):
                between_turn_receipts = apply_between_turn_files(
                    case["workspace"], case["between_turn_files"]
                )
        receipt_path = case.get("source_receipt")
        receipt = {
            "path": str(receipt_path), "sha256_before": receipt_before,
            "sha256_after": file_hash(receipt_path),
        } if receipt_path else None
        attribution = rollout_attribution(run_root / "codex-home", thread_id)
        tool_evidence, tool_evidence_count = native_tool_evidence(
            run_root / "codex-home", thread_id, secrets, run_root
        )
        tool_evidence_path = artifacts / "native-tool-evidence.jsonl"
        tool_evidence_path.write_text(tool_evidence, encoding="utf-8")
        tool_evidence_path.chmod(0o600)
        contexts = attribution.get("turn_contexts", [])
        attribution_matches = (
            attribution.get("model_provider") == "openai"
            and len(contexts) >= len(turn_results)
            and all(
                context.get("model") == args.model
                and context.get("effort") == args.effort
                and context.get("approval_policy") == "never"
                and isinstance(context.get("sandbox_policy"), dict)
                and context["sandbox_policy"].get("type") == "workspace-write"
                and context["sandbox_policy"].get("network_access") is False
                and context["sandbox_policy"].get("exclude_slash_tmp") is True
                and context["sandbox_policy"].get("exclude_tmpdir_env_var") is True
                and not context["sandbox_policy"].get("writable_roots")
                and context.get("workspace_roots") == [str(case["workspace"])]
                for context in contexts[-len(turn_results):]
            )
        )
        outcome = {
            "schema_version": 1, "case": case["name"], "workspace": str(case["workspace"]),
            "runner": {"sha256_before": runner_before, "sha256_after": file_hash(Path(__file__))},
            "catalog": {"path": str(case["catalog"]), "sha256_before": catalog_before, "sha256_after": tree_hash(case["catalog"])},
            "source_receipt": receipt,
            "host": {"binary": str(binary), "version": version, "sha256": file_hash(binary)},
            "configuration": {"model_requested": args.model, "effort_requested": args.effort, "sandbox": "workspace-write", "overrides": config, "injected_environment": case.get("environment", {})},
            "attribution": attribution, "attribution_matches_request": attribution_matches,
            "native_tool_evidence": {
                "path": str(tool_evidence_path),
                "event_count": tool_evidence_count,
                "included_types": sorted(NATIVE_TOOL_TYPES),
            },
            "thread_id": thread_id, "turns": turn_results,
            "between_turn_files": between_turn_receipts, "artifacts": str(artifacts),
        }
        temporary = case["outcome"].with_name(f".{case['outcome'].name}.tmp")
        temporary.write_text(json.dumps(outcome, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, case["outcome"])
        receipt_unchanged = (receipt is None or receipt["sha256_before"] == receipt["sha256_after"]) and outcome["runner"]["sha256_before"] == outcome["runner"]["sha256_after"]
        result_code = 0 if len(turn_results) == len(case["prompts"]) and all(item["returncode"] == 0 and item["completed"] for item in turn_results) and outcome["catalog"]["sha256_before"] == outcome["catalog"]["sha256_after"] and receipt_unchanged and attribution_matches else 1
    except ProcessCleanupError as exc:
        cleanup_safe = False
        raise ProcessCleanupError(
            f"{exc}; private auth home retained at {run_root}"
        ) from exc
    finally:
        if cleanup_safe:
            try:
                shutil.rmtree(run_root)
            except OSError as exc:
                raise RunnerError(
                    f"private auth home cleanup failed at {run_root}"
                ) from exc
            if run_root.exists():
                raise RunnerError(f"private auth home remains at {run_root}")
    return result_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RunnerError, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"cleanup-runner: {exc}", file=sys.stderr)
        raise SystemExit(2)
