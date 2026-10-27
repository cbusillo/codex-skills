#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets==17.2"]
# ///
"""Ask another provider's model for a read-only review of a repository.

Each provider's CLI is run so that it can read the repository with its own
tools and cannot change it. A run that could not read, returned nothing, or hit
a provider error is a failure with a nonzero exit, never an empty "no findings":
an empty review and a reviewer that was locked out look the same otherwise.

Exit codes: 0 reviewed, 1 the run failed, 2 the provider's CLI is not installed.
Nothing here edits a user's tool configuration unless `configure` or `repair` is asked for.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Share the Supervisor reader rather than keeping a second account chooser.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from skills.supervisor.scripts import account_choice
import review_policy
from review_worktree import is_review_scratch

PROVIDERS = {"openai": "codex", "anthropic": "claude", "google": "agy"}
FAULT_MARKER_NAME = "model-review-fault.md"
AGY_SETTINGS = Path("~/.gemini/antigravity-cli/settings.json")
AGY_READ_ONLY_COMMANDS = ("grep", "ls", "wc")
# Commands an earlier reviewer policy granted and the current one refuses. `repair` removes only
# their plain `command(NAME)` grants; a grant for any other program is the user's own and is left alone.
AGY_RETIRED_COMMANDS = ("find", "rg")
PREAMBLE = (
    "The repository to examine is at {repo} (absolute path). Read its files with your own tools. "
    "Resolve paths in the diff relative to that repository, and use absolute paths when reading them. "
    "Do not modify, create, or delete anything.\n\n"
)
UNREACHABLE_LINKS_HINT = "The reviewer could not open these; save any content the review needs and pass it with --input."
OUTPUT_INSTRUCTIONS = (
    '\n\nReturn one JSON object, without Markdown fences, with keys "status" and "review". '
    'Set status to "completed" only when you completed the requested review; review contains your '
    'findings report as a string (or "none" for a completed review with no findings). '
    'If the caller requests structured findings, encode them as JSON text inside that string. '
    'If you refuse the review, set status to "refused". If you stop before completing it, set status '
    'to "incomplete". For either, leave review empty; do not include withheld content or partial findings. '
    'A refusal must be preserved, never worked around. These output instructions also apply to file-read probes.'
)
# Every provider's CLI validates the final answer against this schema. Asking for JSON in the prompt
# alone let a lead-in sentence or a raw newline inside the report string turn a finished review into
# an unconfirmed one (codex-skills#1969).
RESULT_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["completed", "refused", "incomplete"]},
                   "review": {"type": "string"}},
    "required": ["status", "review"],
    "additionalProperties": False,
}


def structured_response(payload: dict[str, Any], text: Any) -> tuple[str, bool]:
    """The CLI's schema-validated result as JSON text, else its final text for the strict parser."""
    structured = payload.get("structured_output")
    if isinstance(structured, dict):
        return json.dumps(structured), True
    return text or "", False


def response_shape(response: str, payload: Any) -> str:
    """Why a response is not a completed result, without quoting any of it."""
    text = response.strip()
    if isinstance(payload, dict):
        return "completed with an empty review" if payload.get("status") == "completed" else "JSON without a completion status"
    if payload is not None:
        return "JSON that is not an object"
    if "{" not in text:
        return "no JSON object"
    return "invalid JSON" if text.startswith("{") else "text before the JSON object"


def completed_review(result: dict[str, Any]) -> dict[str, Any]:
    """Require affirmative completion; never excerpt an unconfirmed or refused report."""
    if not result["ok"]:
        return result
    response = result.pop("response")
    # Accept one whole-response JSON fence, never scrape a completed object out of
    # surrounding prose that could itself say the review was refused or interrupted.
    fenced = re.fullmatch(r"\s*```(?:json)?\s*\n(.*?)\n```\s*", response, re.DOTALL | re.IGNORECASE)
    if fenced:
        response = fenced.group(1)
    try:
        payload = json.loads(response)
    except (ValueError, TypeError):
        payload = None
    status = payload.get("status") if isinstance(payload, dict) else None
    if status in ("refused", "incomplete"):
        return {**result, "ok": False, "outcome": status, "error": f"review {status}",
                "completion_shape": f"reviewer declared {status}"}
    report = payload.get("review") if isinstance(payload, dict) else None
    if status == "completed" and isinstance(report, str) and report.strip():
        return {**result, "response": report}
    if not response:
        return {**result, "ok": False, "outcome": "unavailable", "error": "the reviewer returned nothing"}
    return {**result, "ok": False, "outcome": "unavailable", "error": "review completion unconfirmed",
            "completion_shape": response_shape(response, payload)}


def agy_rules(read_roots: list[Path]) -> list[str]:
    # Directory targets without globs are the only form agy accepts for headless reads.
    return [f"read_file({root})" for root in read_roots] + [f"command({name})" for name in AGY_READ_ONLY_COMMANDS]


def agy_hint(repo: Path) -> str:
    snippet = json.dumps({"permissions": {"allow": agy_rules([repo])}}, indent=2)
    return (
        "agy runs headless here and stops at the first tool that needs permission, so it needs read-only "
        f"allow rules. Merge this into {AGY_SETTINGS}, or run: {Path(__file__).name} configure --read-root "
        f"{repo}\nThe rule applies to every agy session, not only reviews. Name a directory that holds your "
        f"repositories to cover them all, and never your home directory.\n{snippet}"
    )


def agy_settings() -> dict[str, Any]:
    try:
        loaded = json.loads(AGY_SETTINGS.expanduser().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def agy_unsafe_rules(allow: list[Any]) -> tuple[list[str], list[str]]:
    """Split allow rules outside the reviewer's read-only set into (stale command grants, everything else).

    A stale grant is the plain `command(NAME)` this helper once told users to add and no longer
    permits; it is what `repair` removes. Any other rule outside the set, such as a write rule, a
    command with arguments, or a program the user granted for their own sessions, is reported but
    never removed automatically.
    """
    if not isinstance(allow, list):
        return [], [f"permissions.allow is not a list: {allow!r}"]
    safe_commands = {f"command({name})" for name in AGY_READ_ONLY_COMMANDS}
    stale_commands = {f"command({name})" for name in AGY_RETIRED_COMMANDS}
    stale, other = [], []
    for rule in allow:
        if isinstance(rule, str) and (rule.startswith("read_file(") or rule in safe_commands):
            continue
        (stale if rule in stale_commands else other).append(str(rule))
    return sorted(stale), sorted(other)


def run_cli(argv: list[str], cwd: Path, timeout: int, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in (os.environ if env is None else env).items() if key != "CLAUDECODE"}
    return subprocess.run(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
    )


def failed(provider: str, error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "provider": provider, "error": error, **extra}


def openai_error_detail(message: str) -> str:
    """Bound CLI error text without including credential values or URL parameters."""
    # HTTP error bodies in tracing may quote JSON a second time.
    message = re.sub(r"\\([\"'])", r"\1", message)
    message = re.sub(r"https?://\S+", "[redacted URL]", message)
    message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", message)
    message = re.sub(r"(?i)(?<![a-z0-9_-])([a-z0-9_-]*(?:api[_-]?key|token|password|secret|credential))"
                     r"([\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
                     r"\1\2[redacted]", message)
    message = re.sub(r"\b(?:sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]+", "[redacted]", message)
    return message[-400:]


def openai_execution(stdout: str | bytes | None, response: str, elapsed: float,
                     returncode: int | None) -> dict[str, Any]:
    """Report execution evidence without quoting commands, source, or unfinished reviews."""
    if isinstance(stdout, bytes):  # TimeoutExpired may retain bytes even in text mode.
        stdout = stdout.decode(errors="replace")
    events = []
    valid = True
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("event is not an object")
        except ValueError:
            valid = False
            break
        events.append(event)
    known = {"thread.started", "turn.started", "turn.completed", "turn.failed",
             "item.started", "item.updated", "item.completed", "error"}
    types = [event.get("type") if isinstance(event.get("type"), str) else "unknown" for event in events]
    terminal = [kind for kind in types if kind in {"turn.completed", "turn.failed"}]
    commands = [event["item"] for event in events if event.get("type") == "item.completed"
                and isinstance(event.get("item"), dict) and event["item"].get("type") == "command_execution"]
    # A shell chain can finish successfully after an earlier GitHub read failed (#2199).
    # Recognize the CLI's diagnostic line, never excerpt arbitrary aggregated source output.
    github_failed = any(isinstance(item.get("aggregated_output"), str)
                        and "error connecting to api.github.com" in item["aggregated_output"].splitlines()
                        for item in commands)
    return {"elapsed_seconds": round(elapsed, 3), "exit_code": returncode,
            "jsonl_state": "malformed" if not valid else "valid" if events else "missing",
            "last_event": (types[-1] if types[-1] in known else "unknown") if types else None,
            "terminal_event": terminal[-1] if terminal else None,
            "final_answer_produced": bool(response.strip()),
            "failed_commands": sum(item.get("status") in ("failed", "declined")
                                   or (isinstance(item.get("exit_code"), int) and item["exit_code"] != 0)
                                   for item in commands),
            "command_diagnostics": ["github_connection_failed"] if github_failed else []}


def review_openai(prompt: str, repo: Path, model: str | None, timeout: int, scratch: Path,
                  env: Mapping[str, str] | None = None) -> dict[str, Any]:
    answer = scratch / "answer.md"
    schema = scratch / "result-schema.json"
    schema.write_text(json.dumps(RESULT_SCHEMA))
    # `-s read-only` does not reach MCP servers: they run outside the sandbox, and a tool that calls
    # itself read-only runs without approval. Ignoring the user's config drops their servers, plugins,
    # and project trust, so a reviewed repository's own `.codex/config.toml` cannot start one either.
    argv = ["codex", "exec", "--ignore-user-config", "--disable", "plugins", "--disable", "apps"]
    argv += ["--json", "-C", str(repo), "-s", "read-only", "-o", str(answer), "--output-schema", str(schema)]
    if model:
        argv += ["-m", model]
    # JSON mode does not include the model banner; a request is not proof of the model used.
    metadata = {"model": model, "model_source": "requested, not reported by the CLI" if model else "unknown"}
    started = time.monotonic()
    try:
        proc = run_cli([*argv, prompt], repo, timeout, env)
    except subprocess.TimeoutExpired as exc:
        response = answer.read_text() if answer.is_file() else ""
        return failed("openai", f"no answer within {timeout} seconds", **metadata,
                      execution=openai_execution(exc.stdout, response, time.monotonic() - started, None))
    response = answer.read_text() if answer.is_file() else ""
    metadata["execution"] = openai_execution(proc.stdout, response, time.monotonic() - started, proc.returncode)
    try:
        events = [json.loads(line) for line in proc.stdout.splitlines()]
        if any(not isinstance(event, dict) for event in events):
            raise ValueError("event is not an object")
    except ValueError:
        if proc.returncode != 0:
            return failed("openai", f"codex exited {proc.returncode}", detail=openai_error_detail(proc.stderr), **metadata)
        return failed("openai", "could not read codex's JSONL tool evidence", **metadata)
    errors = []
    turn_failed = False
    for event in events:
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "error":
            reroute = re.fullmatch(r"model rerouted: \S+ -> (\S+) \(.+\)", str(item.get("message")))
            if reroute:
                metadata.update(model=reroute.group(1), model_source="reported by the CLI (rerouted)")
        if event.get("type") == "error":
            errors.append(str(event.get("message") or "codex reported an error"))
        elif event.get("type") == "turn.failed":
            turn_failed = True
            error = event.get("error")
            errors.append(str(error.get("message") if isinstance(error, dict) else error))
    # Retry notices also use `error`. Only a nonzero exit or terminal turn failure is decisive.
    if proc.returncode != 0 or turn_failed:
        return failed("openai", f"codex exited {proc.returncode}" if proc.returncode else "codex turn failed",
                      detail=openai_error_detail(errors[-1] if errors else proc.stderr), **metadata,
                      **({"stderr_detail": openai_error_detail(proc.stderr)} if errors and proc.stderr.strip()
                         and proc.stderr.strip() != errors[-1].strip() else {}))
    if not response.strip():
        return failed("openai", "the reviewer returned nothing", **metadata)
    # Codex's file access is through its sandboxed shell. A final message alone is not a review:
    # in particular, a prompt that forbids commands leaves it unable to read any source.
    commands = 0
    attempted = False
    try:
        for event in events:
            item = event.get("item") or {}
            if (event.get("type") in {"item.started", "item.completed"}
                    and item.get("type") == "command_execution"):
                attempted = True
            if (event.get("type") == "item.completed" and item.get("type") == "command_execution"
                    and item.get("status") == "completed" and item.get("exit_code") == 0):
                commands += 1
    except (ValueError, AttributeError, TypeError):
        return failed("openai", "could not read codex's JSONL tool evidence", **metadata)
    if not commands:
        reason = ("the reviewer attempted shell commands but none succeeded; no file-read evidence. "
                  "Check command failures, source paths, and sandbox refusals." if attempted else
                  "the reviewer ran no successful shell commands; no file-read evidence. "
                  "Do not forbid commands: Codex reads files through its read-only shell.")
        return failed("openai", reason, **metadata, successful_commands=0)
    return {"ok": True, "provider": "openai", **metadata, "response": response,
            "successful_commands": commands}


def review_anthropic(prompt: str, repo: Path, model: str | None, timeout: int, _scratch: Path,
                     env: Mapping[str, str] | None = None) -> dict[str, Any]:
    # `--tools` limits what exists in the session. `--allowedTools` would only pre-approve these
    # on top of the user's own settings, which may already allow editing. `--tools` does not reach
    # MCP servers, which can write outside the repository; `--strict-mcp-config` with no config drops them.
    # The reviewed repository's own settings could start hooks, so only the user's settings load.
    argv = ["claude", "-p", prompt, "--tools", "Read,Grep,Glob", "--strict-mcp-config", "--setting-sources", "user"]
    argv += ["--output-format", "json", "--json-schema", json.dumps(RESULT_SCHEMA)]
    if model:
        argv += ["--model", model]
    proc = run_cli(argv, repo, timeout, env)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return failed("anthropic", f"claude exited {proc.returncode} without JSON", detail=proc.stdout[-400:])
    if "structured_output" in str(payload.get("subtype")):
        # Its final text did not validate, so it is unconfirmed review content: report only the subtype.
        return failed("anthropic", "claude did not return a valid review result", outcome="unavailable",
                      subtype=payload["subtype"])
    if payload.get("is_error") or proc.returncode != 0:
        return failed("anthropic", "claude reported an error", detail=str(payload.get("result"))[:400])
    if payload.get("permission_denials"):
        tools = sorted({str(item.get("tool_name", "?")) for item in payload["permission_denials"]})
        # Only report read targets, never search patterns, file contents, or arbitrary tool inputs.
        targets = []
        for item in payload["permission_denials"]:
            tool = item.get("tool_name")
            inputs = item.get("tool_input")
            if tool in {"Read", "Grep", "Glob"} and isinstance(inputs, dict):
                path = inputs.get("file_path" if tool == "Read" else "path")
                if isinstance(path, str) and path:
                    targets.append({"tool": tool, "path": openai_error_detail(path)})
        return failed("anthropic", f"claude was denied: {', '.join(tools)}", denied=tools,
                      denied_targets=targets)
    models = sorted(payload.get("modelUsage") or {})
    response, structured = structured_response(payload, payload.get("result"))
    return {
        "ok": True,
        "provider": "anthropic",
        "model": ",".join(models) or model,
        "model_source": "reported by the CLI" if models else "requested, not reported by the CLI" if model else "unknown",
        "response": response,
        "structured_output": structured,
    }


def review_google(prompt: str, repo: Path, model: str | None, timeout: int, scratch: Path) -> dict[str, Any]:
    # Use an empty scratch directory for the sandboxed CLI session. Do not rely on
    # sandbox confinement to make command permissions safe: find can delete or
    # execute, and rg --pre can execute a program. A user-added write rule would
    # also be inherited by the reviewer.
    allow = (agy_settings().get("permissions") or {}).get("allow") or []
    stale, other = agy_unsafe_rules(allow)
    if stale or other:
        hint = (
            f"Run: {Path(__file__).name} repair. It backs up {AGY_SETTINGS} and removes only these stale command grants."
            if not other
            else f"Only you should change these rules in {AGY_SETTINGS}; remove them for the review, or use another provider."
        )
        return failed(
            "google",
            "your agy settings allow tools outside the reviewer's read-only set",
            rules=[*stale, *other],
            stale_command_grants=stale,
            hint=hint,
        )
    fd, raw_log = tempfile.mkstemp(prefix="agy-", suffix=".log", dir=scratch)
    os.close(fd)
    log_file = Path(raw_log)
    argv = [
        "agy", "-p", prompt, "--sandbox", "--output-format", "json", "--json-schema", json.dumps(RESULT_SCHEMA),
        "--print-timeout", f"{timeout}s", "--log-file", str(log_file),
    ]
    if model:
        argv += ["--model", model]
    proc = run_cli(argv, scratch, timeout + 30)
    try:
        payload = json.loads(proc.stdout[proc.stdout.index("{") :])
    except ValueError:
        return failed("google", f"agy exited {proc.returncode} without JSON", detail=proc.stdout[-400:])
    denied = sorted({item.get("action", "?") for item in payload.get("denied_actions") or []})
    if denied:
        commands, diagnostic = agy_denied_commands(payload.get("conversation_id")) if "command" in denied else ([], None)
        error = f"agy was denied: {', '.join(denied)}"
        if commands:
            error += f"; refused command: {'; '.join(commands)}"
        result = failed("google", error, denied=denied, denied_commands=commands,
                        conversation_id=payload.get("conversation_id"))
        if diagnostic:
            result["command_diagnostic"] = diagnostic
        # Preserve command diagnostics, but retry only an empty permission failure,
        # never a nonempty refusal (including one outside the output contract).
        if denied == ["command"] and ((payload.get("response") or "").strip() or payload.get("structured_output")):
            result.update(outcome="unavailable",
                          **agy_model(model or agy_settings().get("model"), payload.get("conversation_id"), log_file))
        # A shell refusal is not evidence of missing file access. Do not recommend new grants for it.
        if denied != ["command"]:
            result["hint"] = agy_hint(repo)
        return result
    if payload.get("status") not in (None, "SUCCESS"):
        return failed("google", f"agy finished with status {payload.get('status')}")
    requested = model or agy_settings().get("model")
    # agy's `response` carries narration before the JSON even when the structured result validated.
    response, structured = structured_response(payload, payload.get("response"))
    return {
        "ok": True,
        "provider": "google",
        **agy_model(requested, payload.get("conversation_id"), log_file),
        "response": response,
        "structured_output": structured,
    }


REVIEWERS = {"openai": review_openai, "anthropic": review_anthropic, "google": review_google}


def agy_log(conversation_id: Any = None, log_file: Path | None = None) -> str:
    valid_id = isinstance(conversation_id, str) and bool(re.fullmatch(r"[0-9a-fA-F-]{36}", conversation_id))
    if log_file is not None:
        try:
            text = log_file.read_text(errors="replace")
        except OSError:
            text = ""
        if text.strip():
            conversations = re.findall(r"\bCreated conversation ([0-9a-fA-F-]{36})\b", text)
            if conversation_id is not None:
                return text if valid_id and conversations in ([], [conversation_id]) else ""
            return text if len(conversations) <= 1 else ""
    if not valid_id:
        return ""
    base = AGY_SETTINGS.expanduser().parent
    candidates = [base / "cli.log", *sorted((base / "log").glob("cli-*.log"), reverse=True)]
    for path in candidates:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        conversations = re.findall(r"\bCreated conversation ([0-9a-fA-F-]{36})\b", text)
        if conversations == [conversation_id]:
            return text
        if conversation_id in conversations:
            return ""
    return ""


def agy_model(requested: str | None, conversation_id: Any = None, log_file: Path | None = None) -> dict[str, Any]:
    """Read the model agy resolved for this run from its CLI log when unambiguous."""
    default = {"model": requested, "model_source": "requested, not reported by the CLI" if requested else "unknown"}
    log = agy_log(conversation_id, log_file)
    reroutes = list(dict.fromkeys(re.findall(r'model alias "([^"]+)" resolved to "([^"]+)"', log)))
    labels = list(dict.fromkeys(re.findall(r'Propagating selected model override to backend: label="([^"]+)"', log)))
    if len(reroutes) > 1 or len(labels) > 1:
        return default
    if reroutes and (not labels or labels[0] == reroutes[0][1]):
        return {"model": reroutes[0][1], "model_source": "reported by the CLI (rerouted)"}
    if reroutes and labels:
        return default
    if labels:
        return {"model": labels[0], "model_source": "reported by the CLI"}
    return default


def protobuf_fields(data: bytes) -> dict[int, Any]:
    """Read agy's protobuf wire envelope without decoding opaque tool output blobs."""
    offset = 0

    def varint() -> int:
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("invalid protobuf varint")

    fields = {}
    while offset < len(data):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            fields[number] = varint()
            continue
        size = varint() if wire == 2 else {1: 8, 5: 4}.get(wire)
        if size is None or offset + size > len(data):
            raise ValueError("invalid protobuf length or wire type")
        fields[number] = data[offset:offset + size]
        offset += size
    return fields


def agy_denied_commands(conversation_id: Any) -> tuple[list[str], str | None]:
    """Read only the named run's failed/pending tool calls; never scan other conversations.

    Observed agy schema: step field 5 is metadata, metadata field 4 is the tool call,
    and call fields 2/3 hold its name/JSON arguments. Status 6 is a stopped pending
    call, 7 a failed call. The caller has already confirmed a command denial in JSON.
    """
    if not isinstance(conversation_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", conversation_id):
        return [], "agy did not report a valid conversation_id; refused command unavailable"
    path = AGY_SETTINGS.expanduser().parent / "conversations" / f"{conversation_id}.db"
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT step_payload FROM steps WHERE status IN (6, 7) ORDER BY idx DESC"
            )
            for (blob,) in rows:
                step = protobuf_fields(blob)
                metadata = protobuf_fields(step.get(5, b""))
                call = protobuf_fields(metadata.get(4, b""))
                if call.get(2) != b"run_command":
                    continue
                arguments = json.loads(call[3])
                command = arguments.get("CommandLine")
                if isinstance(command, str) and command:
                    return [command], None
    except (sqlite3.Error, OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        return [], f"could not read agy refused command: {exc}"
    return [], "no refused command found in agy's saved conversation"


def branch_diff(repo: Path) -> bytes:
    """Return committed branch changes against the remote default branch."""
    remote_head = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True, text=True,
    )
    candidates = [remote_head.stdout.strip()] if remote_head.returncode == 0 else []
    candidates += ["refs/remotes/origin/main", "refs/remotes/origin/master", "refs/heads/main", "refs/heads/master"]
    default = next((ref for ref in candidates if subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
    ).returncode == 0), None)
    if default is None:
        branch = subprocess.run(
            ["git", "-C", str(repo), "branch", "--show-current"], capture_output=True, text=True,
        )
        if branch.returncode != 0:
            return b""  # A directory without Git can still be reviewed by named paths.
        if branch.stdout.strip() not in {"main", "master"}:
            raise RuntimeError("could not identify the default branch for the review diff")
        default = "HEAD"
    tracked = subprocess.run(["git", "-C", str(repo), "diff", "--quiet", "HEAD", "--"])
    untracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True,
    )
    project_files = [name for name in untracked.stdout.split(b"\0") if name and not is_review_scratch(name)]
    if tracked.returncode != 0 or untracked.returncode != 0 or project_files:
        raise RuntimeError("review checkout has uncommitted or untracked files; commit the change first")
    base = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "HEAD", default],
        capture_output=True, text=True,
    )
    if base.returncode != 0:
        raise RuntimeError("could not find a review diff base")
    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-ext-diff", "--no-color", "--binary", base.stdout.strip(), "--"],
        capture_output=True,
    )
    if diff.returncode != 0:
        raise RuntimeError(f"could not prepare review diff: {diff.stderr.decode(errors='replace').strip()}")
    return diff.stdout


def reviewer_environment(choice: dict[str, Any]) -> dict[str, str]:
    """Route only the child home; inherited auth overrides must not bypass its login."""
    env = dict(os.environ)
    provider = choice["provider"]
    variable = account_choice.ACCOUNT_VARIABLES[provider]
    overrides = {
        "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                      "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                      "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                      "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
                      "ANTHROPIC_CUSTOM_HEADERS"),
        "openai": ("OPENAI_API_KEY", "CODEX_API_KEY"),
    }
    for key in (variable, *overrides[provider]):
        env.pop(key, None)
    if variable in choice["env"]:
        env[variable] = os.path.expanduser(choice["env"][variable])
    return env


def review(provider: str, prompt: str, repo: Path, model: str | None, timeout: int,
           choice: dict[str, Any] | None = None, inputs: Sequence[Path] = ()) -> dict[str, Any]:
    if shutil.which(PROVIDERS[provider]) is None:
        return failed(provider, f"`{PROVIDERS[provider]}` is not installed", installed=False)
    links = unreachable_links(prompt)
    notes = {"unreachable_links": links, "unreachable_links_hint": UNREACHABLE_LINKS_HINT} if links else {}
    # File-tool reviewers need the generated diff under their authorized read root.
    # Do not change TMPDIR: child tools such as uv would leave locks in the checkout.
    scratch_options = {"dir": repo} if provider in {"google", "anthropic"} else {}
    launch = {}
    try:
        with tempfile.TemporaryDirectory(prefix=".model-review-", **scratch_options) as scratch:
            diff = branch_diff(repo)
            preamble = PREAMBLE.format(repo=repo)
            if provider == "google":
                permitted = ", ".join(f"`{name}`" for name in AGY_READ_ONLY_COMMANDS)
                preamble += (
                    f"The only commands you may run are {permitted}. "
                    "Use read_file (view_file) to inspect file contents, including large files: read successive "
                    "line ranges when needed. Use ls with absolute paths to discover paths. Do not use shell reads such as "
                    "cat, head, tail or sed, or pipelines, command chaining or substitutions. "
                    "Do not run other commands. If a command is refused, continue with file tools.\n\n"
                )
            if diff:
                diff_path = Path(scratch) / "change.diff"
                diff_path.write_text(diff.decode(errors="backslashreplace"))
                preamble += f"The changes to review are in {diff_path}. Read that file with your read-only tools.\n\n"
            # Reviewers have no network and some read only granted roots, so inputs travel with the diff.
            for index, source in enumerate(inputs, 1):
                copied = Path(scratch) / f"input-{index}-{source.name}"
                shutil.copyfile(source, copied)
                preamble += f"Supporting file for this review: {copied}. Read it with your read-only tools.\n\n"
            if links:
                preamble += (
                    "You have no network, so the links in this request cannot be opened; treat them as "
                    "references. If one held content this review needed, report that as a coverage gap.\n\n"
                )
            if choice is not None or provider in account_choice.ACCOUNT_VARIABLES:
                choice = choice or account_choice.select(provider)
                account_choice.prepare_launch(choice)
                receipt = account_choice.record_launch(choice)
                launch = {"account": account_choice.public(choice), "launch_receipt": str(receipt)}
            if provider in account_choice.ACCOUNT_VARIABLES:
                result = REVIEWERS[provider](preamble + prompt + OUTPUT_INSTRUCTIONS, repo, model, timeout,
                                             Path(scratch), reviewer_environment(choice))
            else:
                result = REVIEWERS[provider](preamble + prompt + OUTPUT_INSTRUCTIONS, repo, model, timeout, Path(scratch))
            if provider == "google" and result.get("denied") == ["command"] and not result.get("outcome"):
                refusal = result
                reminder = (
                    "A previous attempt stopped after a refused command request"
                    + (": " + json.dumps(refusal["denied_commands"]) if refusal["denied_commands"] else "")
                    + ". Complete this review using only read_file (view_file) with the absolute paths provided. "
                    "Do not call run_command at all. No permission changes are available.\n\n"
                )
                try:
                    result = review_google(preamble + reminder + prompt + OUTPUT_INSTRUCTIONS, repo, model, timeout, Path(scratch))
                except subprocess.TimeoutExpired:
                    result = failed(provider, f"file-tool retry returned no answer within {timeout} seconds")
                except OSError as exc:
                    result = failed(provider, f"file-tool retry failed: {exc}")
                result["recovery"] = {"attempts": 2, "denied_commands": refusal["denied_commands"],
                                      "conversation_id": refusal.get("conversation_id")}
                if refusal.get("command_diagnostic"):
                    result["recovery"]["command_diagnostic"] = refusal["command_diagnostic"]
    except subprocess.TimeoutExpired:
        return failed(provider, f"no answer within {timeout} seconds", **launch, **notes)
    except (OSError, RuntimeError, ValueError) as exc:
        return failed(provider, str(exc), **launch, **notes)
    if result["ok"] and not result["response"].strip():
        result = {**result, "ok": False, "error": "the reviewer returned nothing"}
        result.pop("response")
    result = completed_review(result)
    if not result["ok"] and result.get("recovery", {}).get("denied_commands") and not result.get("denied_commands"):
        result["error"] += "; previous refused command: " + "; ".join(result["recovery"]["denied_commands"])
    return {**result, **launch, **notes}


def unreachable_links(prompt: str) -> list[str]:
    """Web links in the request, in order and without repeats; reviewers have no network to open them."""
    return list(dict.fromkeys(trim_link(match) for match in re.findall(r"https?://[^\s<>\"`]+", prompt)))


def trim_link(link: str) -> str:
    """Drop prose punctuation after a link, keeping brackets and quotes the link itself opened."""
    while link and link[-1] in ".,;:!?')]":
        opener = {")": "(", "]": "["}.get(link[-1])
        if opener and link.count(opener) >= link.count(link[-1]):
            break
        link = link[:-1]
    return link


def fault_marker(source: Mapping[str, str] = os.environ) -> Path:
    """The owner's planted finding: MODEL_REVIEW_FAULT when set, else ~/.code/model-review-fault.md.

    One path for every host on purpose, like the direction marker: host home variables differ.
    """
    explicit = source.get("MODEL_REVIEW_FAULT")
    if explicit:
        return Path(explicit).expanduser()
    return Path(source.get("HOME", "~")).expanduser() / ".code" / FAULT_MARKER_NAME


def plant_fault(result: dict[str, Any], marker: Path, repo: Path) -> Path | None:
    """Append the owner's planted finding to one successful review; return the marker to consume.

    This is how the direction skill's unannounced planted-fault run reaches an executing agent:
    the owner writes the marker outside the repository before an ordinary session, the finding
    arrives inside real reviewer output, and the renamed marker records when it fired. The JSON
    result says nothing, so the agent weighs the finding as it would any other. A marker inside
    the reviewed repository is never read: a pull request could have put it there.
    """
    if not result["ok"] or not marker.is_file():
        return None
    if marker.resolve().is_relative_to(repo.resolve()):
        print(f"ignoring planted finding inside the reviewed repository: {marker}", file=sys.stderr)
        return None
    finding = marker.read_text().strip()
    if not finding:
        return None
    result["response"] = result["response"].rstrip() + "\n\n" + finding + "\n"
    return marker


def consume_fault(marker: Path) -> None:
    """Rename the marker only once the review it landed in has been delivered."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    marker.rename(marker.with_name(f"{marker.name}.used-{stamp}"))


def repository_root(path: Path) -> Path:
    root = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
    )
    return Path(root.stdout.strip()).resolve() if root.returncode == 0 else path


def cmd_run(args: argparse.Namespace) -> int:
    repo = repository_root(Path(args.repo).resolve())
    try:
        selected = review_policy.select(review_policy.load_policy(), args.author_provider, args.provider,
                                        args.supplementary, args.model, args.timeout)
    except ValueError as error:
        result = {"ok": False, "provider": args.provider, "phase": "selection", "outcome": "unavailable", "error": str(error)}
        if isinstance(error, review_policy.PolicyConflict):
            result.update(provider=error.provider, skipped=error.skipped)
        elif isinstance(error, review_policy.NoProvider):
            result["skipped"] = error.skipped
            if args.provider and error.skipped == [{"provider": args.provider, "reason": "CLI is not installed"}]:
                result.update(provider=args.provider, installed=False)
        print(json.dumps(result))
        return 2 if result.get("installed") is False else 1
    inputs = [Path(name).resolve() for name in args.input or []]
    missing = [str(path) for path in inputs if not path.is_file()]
    if missing:
        print(json.dumps({"ok": False, "provider": selected["provider"], "phase": "inputs", "outcome": "unavailable",
                          "error": "review input is not a readable file: " + ", ".join(missing)}))
        return 1
    result = review(selected["provider"], Path(args.prompt_file).read_text(), repo, selected["model"],
                    selected["timeout"], selected["choice"], inputs)
    result["selection"] = selected["selection"]
    result["timeout"] = selected["timeout"]
    # Some CLIs include effort in the reported model label; never infer it from a request.
    effort = re.search(r"\((High|Medium|Low)\)$", result.get("model") or "", re.IGNORECASE)
    confirmed = result.get("model_source", "").startswith("reported by the CLI")
    result["effort"] = effort.group(1).lower() if effort and confirmed else "unknown"
    planted = plant_fault(result, fault_marker(), repo)
    if result["ok"] and args.out:
        Path(args.out).write_text(result.pop("response"))
        result["response_file"] = args.out
    print(json.dumps(result, indent=2), flush=True)
    if planted is not None:
        consume_fault(planted)
    return 0 if result["ok"] else 2 if result.get("installed") is False else 1


def find_probe(repo: Path) -> tuple[Path | None, str]:
    """A small text file and its first non-empty line, from Git's list when there is one."""
    listed = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True).stdout.split("\n")
    names = [name for name in listed if name] or sorted(
        str(path.relative_to(repo)) for path in repo.rglob("*") if path.is_file() and ".git" not in path.parts
    )
    for name in names[:500]:
        path = repo / name
        try:
            if not path.is_file() or path.stat().st_size > 200_000:
                continue
            # The prompt asks for the first non-empty line, so that exact line is the expectation.
            # A file whose first line is a lone brace or dash is skipped, not read past.
            line = next((text.strip() for text in path.read_text().splitlines() if text.strip()), None)
        except (OSError, UnicodeDecodeError):
            continue
        if line and len(line) > 3:
            return path, line
    return None, ""


def cmd_check(args: argparse.Namespace) -> int:
    repo = repository_root(Path(args.repo).resolve())
    probe, expected = find_probe(repo)
    if probe is None:
        print(json.dumps({"ok": False, "error": f"no readable text file to probe in {repo}"}))
        return 1
    prompt = f"Read {probe}; put exactly its first non-empty line in the review field of your result."
    report = []
    try:
        policy = review_policy.load_policy()
    except ValueError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    role = "supplementary" if args.supplementary else "providers"
    for provider in args.provider or [p for p in policy[role] if p not in {args.author_provider, "google"}]:
        try:
            selected = review_policy.select(policy, args.author_provider, provider, args.supplementary,
                                            timeout=args.timeout, probe=True)
        except ValueError as error:
            missing = (isinstance(error, review_policy.NoProvider)
                       and error.skipped == [{"provider": provider, "reason": "CLI is not installed"}])
            report.append({"provider": provider, "state": "not installed" if missing else "not ready", "error": str(error)})
            continue
        result = review(provider, prompt, repo, selected["model"], selected["timeout"], selected["choice"])
        if result["ok"] and expected not in result["response"]:
            result = {**result, "ok": False, "error": "answered without reading the file"}
            result.pop("response")
        state = "ready" if result["ok"] else "not installed" if result.get("installed") is False else "not ready"
        report.append({key: value for key, value in {**result, "state": state}.items() if key != "response"})
    print(json.dumps({"probe": str(probe), "providers": report}, indent=2))
    return 0 if any(item["state"] == "ready" for item in report) else 1


def load_agy_settings_file() -> tuple[Path, dict[str, Any], list[Any]]:
    """The settings path, its parsed object, and its allow list; raises ValueError when the file is unusable.

    A symlinked settings file, as dotfiles setups make, is resolved so the write lands in the real file.
    """
    settings = AGY_SETTINGS.expanduser().resolve()
    try:
        current = json.loads(settings.read_text())
    except (OSError, json.JSONDecodeError):
        current = None
    if not isinstance(current, dict):
        raise ValueError(f"{settings} is missing or not a JSON object; run agy once first")
    permissions = current.get("permissions")
    allow = permissions.get("allow", []) if isinstance(permissions, dict) else [] if permissions is None else None
    if not isinstance(allow, list):
        raise ValueError(f"{settings} has a permissions.allow that is not a list; only you should fix it")
    return settings, current, allow


def write_agy_settings(settings: Path, current: dict[str, Any], allow: list[Any], backup_suffix: str) -> Path:
    backup = settings.with_name(settings.name + backup_suffix)
    shutil.copy2(settings, backup)
    if not isinstance(current.get("permissions"), dict):
        current["permissions"] = {}
    current["permissions"]["allow"] = allow
    # Write beside the file and rename so an interrupted write never leaves agy with truncated JSON.
    staged = settings.with_name(settings.name + ".model-review-tmp")
    staged.write_text(json.dumps(current, indent=2) + "\n")
    os.replace(staged, settings)
    return backup


def cmd_configure(args: argparse.Namespace) -> int:
    try:
        settings, current, allow = load_agy_settings_file()
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    wanted = agy_rules([Path(root).resolve() for root in args.read_root])
    added = [rule for rule in wanted if rule not in allow]
    if added:
        write_agy_settings(settings, current, [*allow, *added], ".before-model-review")
    print(json.dumps({"ok": True, "settings": str(settings), "added": added}, indent=2))
    return 0


def cmd_repair(_args: argparse.Namespace) -> int:
    """Remove only the retired reviewer command grants that the current policy no longer permits.

    Read rules, permitted commands, and the user's own grants are kept as they are. Any other rule
    outside the read-only set is ambiguous: the helper reports it and changes nothing.
    """
    try:
        settings, current, allow = load_agy_settings_file()
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": f"{exc}; nothing was changed"}))
        return 1
    stale, other = agy_unsafe_rules(allow)
    if other:
        print(json.dumps({
            "ok": False,
            "error": "rules outside the reviewer's read-only set are not plain command grants; nothing was changed",
            "settings": str(settings),
            "ambiguous": other,
            "would_remove": stale,
            "hint": "Remove or keep those rules by hand, then run repair again for the stale command grants.",
        }, indent=2))
        return 1
    result: dict[str, Any] = {"ok": True, "settings": str(settings), "removed": stale}
    if stale:
        kept = [rule for rule in allow if rule not in stale]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        result["backup"] = str(write_agy_settings(settings, current, kept, f".before-model-review-repair-{stamp}"))
    print(json.dumps(result, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Ask one provider's model for a read-only review.")
    run.add_argument("--provider", choices=sorted(PROVIDERS), help="Explicit allowed provider; default: standing order and capacity.")
    run.add_argument("--author-provider", choices=sorted(PROVIDERS), help="Required for selection; identify the author's actual provider.")
    run.add_argument("--supplementary", action="store_true", help="Run an explicitly allowed supplementary provider, serially after the primary review.")
    run.add_argument("--repo", default=".", help="Repository the reviewer reads (default: current directory).")
    run.add_argument("--prompt-file", required=True, help="What to review and what it is for; name paths, do not paste files.")
    run.add_argument("--input", action="append",
                     help="A file outside the repository the reviewer needs, such as a saved issue; repeatable.")
    run.add_argument("--model", help="Model to request; model_source says whether the CLI reported it.")
    run.add_argument("--out", help="Write the review here instead of including it in the JSON.")
    run.add_argument("--timeout", type=int, help="Seconds; default: standing provider setting, otherwise 900.")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="Show which providers can actually read a repository from here.")
    check.add_argument("--repo", default=".")
    check.add_argument("--provider", action="append", choices=sorted(PROVIDERS))
    check.add_argument("--author-provider", choices=sorted(PROVIDERS), help="Required for selection; identify the author's actual provider.")
    check.add_argument("--supplementary", action="store_true")
    check.add_argument("--timeout", type=int)
    check.set_defaults(func=cmd_check)

    configure = sub.add_parser("configure", help="Add agy's read-only allow rules to your own settings file.")
    configure.add_argument("--read-root", action="append", required=True, help="Directory agy may read; repeatable.")
    configure.set_defaults(func=cmd_configure)

    repair = sub.add_parser(
        "repair",
        help="Back up your agy settings and remove only stale command grants the reviewer policy no longer permits.",
    )
    repair.set_defaults(func=cmd_repair)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
