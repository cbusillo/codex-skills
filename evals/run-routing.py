# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Run the plugin's routing prompts on a real CLI with operational shell calls blocked.

This complements claude plugin eval on hosts where its Bash sandbox cannot
start. It records raw traces, source hashes, model configuration, and exits;
grade actual skill loads and attempted commands, not a model's self-report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml
from shell_boundary import read_only

ROOT = Path(__file__).resolve().parents[1]
GRADER_VERSION = 4


def recorded_shell_command(command: str) -> str:
    """Unwrap the shell invocation Codex records around a hook's raw command."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command
    if len(tokens) == 3 and Path(tokens[0]).name in {"sh", "bash", "zsh"} and tokens[1] in {"-c", "-lc"}:
        return tokens[2]
    return command


def normalize_answer(text: str) -> str:
    text = text.replace(chr(96), "").replace("*", "")
    text = re.sub(r"(?<!\w)_|_(?!\w)", "", text)
    return " ".join(text.casefold().split()).rstrip(".!?")


def source_digest(catalog: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted([*catalog.glob("skills/**/*"), *catalog.glob("hooks/*")]):
        relative = path.relative_to(catalog)
        if path.is_file() and not any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            digest.update(str(path.relative_to(catalog)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def valid_merge_arguments(catalog: Path, command: str) -> bool:
    tokens = shlex.split(command)
    index = next((i for i, token in enumerate(tokens) if Path(token).name == "gh-pr.py"), None)
    if index is None:
        return False
    # Exercise the maintained parser without dispatching its selected command.
    program = """import runpy, sys
from pathlib import Path
path = Path(sys.argv[1])
sys.path.insert(0, str(path.parent))
sys.argv = [str(path), *sys.argv[2:]]
namespace = runpy.run_path(str(path))
args = namespace['parse_args']()
raise SystemExit(0 if args.command == 'merge' else 1)
"""
    result = subprocess.run([sys.executable, "-c", program, str(catalog / "skills/github/scripts/gh-pr.py"), *tokens[index + 1:]],
                            capture_output=True, text=True, timeout=15, check=False)
    return result.returncode == 0


def reads_content(command: str) -> bool:
    """A shell read that returns file content, not only names."""
    return any(re.match(r"\s*(?:cat|head|tail|sed -n|rg(?!.*--files))\b", part) for part in re.split(r"&&|\|\||;|\||\n", command))


def shell_without_comments(command: str) -> str:
    """Remove unquoted word-start comments without consuming line separators."""
    output: list[str] = []
    quote = None
    escaped, comment, word_start = False, False, True
    for character in command:
        if comment:
            if character == "\n":
                output.append(character)
                comment, word_start = False, True
        elif escaped:
            escaped = False
            if character == "\n":
                output.pop()  # The shell removes continuations outside single quotes.
            else:
                output.append(character)
                word_start = False
        elif character == "\\" and quote != "'":
            output.append(character)
            escaped = True
        elif quote:
            output.append(character)
            if character == quote:
                quote = None
        elif character in {"'", '"'}:
            output.append(character)
            quote, word_start = character, False
        elif character == "#" and word_start:
            comment = True
        else:
            output.append(character)
            word_start = character.isspace() or character in ";&|()<>"
    return "".join(output)


def shell_read_paths(command: str) -> list[str]:
    """Extract literal reader arguments without executing the recorded shell."""
    lexer = shlex.shlex(shell_without_comments(command), posix=True, punctuation_chars=";&|\n")
    lexer.commenters = ""
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    paths, segment = [], []
    try:
        for token in [*lexer, ";"]:
            if token and all(character in ";&|\n" for character in token):
                if segment and reads_content(shlex.join(segment)):
                    arguments = segment[1:]
                    reader = Path(segment[0]).name
                    operands = []
                    skip_next = False
                    pattern_seen = False
                    for argument in arguments:
                        if skip_next:
                            skip_next = False
                            continue
                        if argument in {"-n", "-c", "--max-count", "--glob", "-g", "--type", "-t", "-e", "--regexp"}:
                            skip_next = argument not in {"-n", "-c"} or reader in {"head", "tail"}
                            if argument in {"-e", "--regexp"}:
                                pattern_seen = True
                            continue
                        if argument.startswith("-"):
                            continue
                        if reader in {"rg", "sed"} and not pattern_seen:
                            pattern_seen = True
                            continue
                        operands.append(argument)
                    paths.extend(operands)
                segment = []
            else:
                segment.append(token)
    except ValueError:
        return []
    return paths


def delivered_paths(command: str, output: str, destination: Path, catalog: Path) -> list[str]:
    """Prove delivery before a later nonzero exit using the pinned input text.

    Only fixture and catalog files may be read by the grader. An error message,
    nonempty stdout, or a path in the command alone cannot prove a partial read.
    """
    delivered = []
    roots = [(destination / "workspace").resolve(), (catalog / "skills").resolve()]
    for path_text in shell_read_paths(command):
        path = Path(path_text)
        if not path.is_absolute():
            path = destination / "workspace" / path
        path = path.resolve()
        if not any(path.is_relative_to(root) for root in roots):
            continue
        try:
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                continue
            content = path.read_text().strip()
        except (OSError, UnicodeError):
            continue
        if content and content in output:
            delivered.append(path_text)
    return delivered


def load_trace(destination: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    messages = [json.loads(line) for line in (destination / "trace.jsonl").read_text().splitlines() if line.strip()]
    events_path = destination / "shell-events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()] if events_path.exists() else []
    return messages, events


def load_read_observations(host: str, messages: list[dict[str, Any]], destination: Path,
                           catalog: Path, confirmed: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Keep invocation choice and delivery separate from the legacy sequence."""
    invocations: dict[str, dict[str, Any]] = {}
    batches: dict[str, int] = {}
    skills, reads = [], []
    confirmed = set(confirmed)

    def skill_name(path_text: str) -> str | None:
        path = Path(path_text)
        if not path.is_absolute():
            path = destination / "workspace" / path
        path = path.resolve()
        root = (catalog / "skills").resolve()
        relative = path.relative_to(root) if path.is_relative_to(root) else None
        if relative and len(relative.parts) == 2 and relative.name == "SKILL.md" and not relative.parts[0].startswith("."):
            return relative.parts[0]
        return None

    def complete_skill(source_name: str, record: dict[str, Any], completed_position: int) -> None:
        observation_id = {key: record.get(key) for key in ("invocation", "batch", "chosen_at")}
        skills.append({**observation_id, "value": source_name, "completed_at": completed_position})
        confirmed.add(source_name)

    def deliver(read_paths: list[str], record: dict[str, Any], delivered_position: int) -> None:
        for path in read_paths:
            observation_id = {key: record.get(key) for key in ("invocation", "batch", "chosen_at")}
            reads.append({**observation_id, "value": path, "delivered_at": delivered_position})
            read_skill = skill_name(path)
            if read_skill:
                complete_skill(read_skill, record, delivered_position)

    for position, message in enumerate(messages):
        if host == "codex" and message.get("type") in {"item.started", "item.completed"}:
            item = message["item"]
            if item.get("type") != "command_execution":
                continue
            identity = item.get("id")
            command = recorded_shell_command(item.get("command", ""))
            if message["type"] == "item.started":
                if identity:
                    invocations[identity] = {"invocation": identity, "batch": identity, "chosen_at": position}
            else:
                invocation = invocations.pop(identity, {"invocation": identity, "batch": None, "chosen_at": None})
                paths = (shell_read_paths(command) if item.get("exit_code") == 0 else
                         delivered_paths(command, item.get("aggregated_output", ""), destination, catalog))
                deliver(paths, invocation, position)
        elif host == "claude" and message.get("type") == "assistant":
            payload = message.get("message", {})
            batch = payload.get("id", f"message-{position}")
            chosen = batches.setdefault(batch, position)
            for block in payload.get("content", []):
                if not isinstance(block, dict) or block.get("type") != "tool_use" or not block.get("id"):
                    continue
                arguments = block.get("input", {})
                invocations[block["id"]] = {"invocation": block["id"], "batch": batch,
                    "chosen_at": chosen, "tool": block.get("name"), "arguments": arguments}
        elif host == "claude" and message.get("type") == "user":
            for block in message.get("message", {}).get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    match = re.match(r"Base directory for this skill: ([^\n]+)", block.get("text", ""))
                    name = skill_name(str(Path(match[1]) / "SKILL.md")) if match else None
                    if name:
                        invocation = next((entry for entry in reversed(list(invocations.values()))
                            if entry["tool"] == "Skill" and entry["arguments"].get("skill", "").split(":")[-1] == name), {})
                        complete_skill(name, invocation, position)
                elif block.get("type") == "tool_result":
                    invocation = invocations.get(block.get("tool_use_id"))
                    if invocation is None:
                        continue
                    tool, arguments = invocation["tool"], invocation["arguments"]
                    if tool == "Bash":
                        command = arguments.get("command", "")
                        paths = (delivered_paths(command, str(block.get("content", "")), destination, catalog)
                                 if block.get("is_error") else shell_read_paths(command))
                        deliver(paths, invocation, position)
                    elif not block.get("is_error"):
                        if tool == "Read":
                            deliver([arguments.get("file_path", "")], invocation, position)
                        elif tool == "Grep" and arguments.get("output_mode") == "content":
                            deliver([arguments.get("path", "")], invocation, position)
                        elif tool == "Skill":
                            launched = re.fullmatch(r"Launching skill: (?:[\w-]+:)?([\w-]+)", str(block.get("content", "")).strip())
                            if launched and launched[1] in confirmed:
                                complete_skill(launched[1], invocation, position)
    return {"skills": skills, "reads": reads}


def observe(host: str, messages: list[dict[str, Any]], events: list[dict[str, Any]], destination: Path,
            catalog: Path, confirmed: set[str] | None = None) -> dict[str, Any]:
    """Order skill loads and shell commands as the host recorded them.

    Claude Code does not inject a skill's text again while it is still in
    context; a repeated invocation counts once `confirmed` shows that skill came
    from the tested catalog earlier in the same run.
    """
    confirmed = set() if confirmed is None else confirmed
    load_order = load_read_observations(host, messages, destination, catalog, confirmed)
    sequence: list[tuple[str, str]] = []
    failed: set[str] = set()
    commands_by_id: dict[str, str] = {}
    final = ""
    protocol_copies = 0
    successful_reads: set[str] = set()
    partial_reads: dict[str, list[str]] = {}
    skill_paths: list[str] = []
    foreign_skill_reads: list[str] = []
    pending_reads: dict[str, str] = {}
    read_attempts: list[str] = []

    def credit_skill(path_text: str) -> None:
        skill_path = Path(path_text)
        if not skill_path.is_absolute():
            skill_path = destination / "workspace" / skill_path
        resolved = skill_path.resolve()
        skill_paths.append(str(resolved))
        relative = resolved.relative_to((catalog / "skills").resolve()) if resolved.is_relative_to((catalog / "skills").resolve()) else None
        if relative and len(relative.parts) == 2 and not relative.parts[0].startswith("."):
            sequence.append(("skill", resolved.parent.name))
            confirmed.add(resolved.parent.name)
        else:
            foreign_skill_reads.append(str(resolved))

    for message in messages:
        if message.get("subtype") == "hook_response":
            protocol_copies += message.get("output", "").count("# Using Skills")
        if message.get("type") == "assistant":
            for block in message.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    if block.get("name") == "Bash":
                        sequence.append(("shell", block["input"]["command"]))
                        commands_by_id[block.get("id", "")] = block["input"]["command"]
                        if reads_content(block["input"]["command"]):
                            pending_reads[block.get("id")] = block["input"]["command"]
                            read_attempts.extend(shell_read_paths(block["input"]["command"]))
                    elif block.get("name") == "Read":
                        pending_reads[block.get("id")] = block["input"].get("file_path", "")
                        read_attempts.append(block["input"].get("file_path", ""))
                    elif block.get("name") == "Grep":
                        path = block["input"].get("path", ".")
                        read_attempts.append(str(Path(path) / block["input"].get("glob", "")))
                        if block["input"].get("output_mode") == "content":
                            pending_reads[block.get("id")] = block["input"].get("path", "")
        elif message.get("type") == "user":
            for block in message.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    match = re.match(r"Base directory for this skill: ([^\n]+)", block.get("text", ""))
                    if match:
                        credit_skill(str(Path(match[1]) / "SKILL.md"))
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    command = commands_by_id.get(block.get("tool_use_id", ""), "")
                    failed.add(command)
                    for path in delivered_paths(command, str(block.get("content", "")), destination, catalog):
                        sequence.append(("read", path))
                        if Path(path).name == "SKILL.md":
                            credit_skill(path)
                if isinstance(block, dict) and block.get("type") == "tool_result" and not block.get("is_error"):
                    if block.get("tool_use_id") in pending_reads:
                        read = pending_reads.pop(block["tool_use_id"])
                        sequence.append(("read", read))
                        for path in shell_read_paths(read) if block.get("tool_use_id") in commands_by_id else [read]:
                            if Path(path).name == "SKILL.md":
                                credit_skill(path)
                    launched = re.fullmatch(r"Launching skill: (?:[\w-]+:)?([\w-]+)", str(block.get("content", "")).strip())
                    if launched and launched[1] in confirmed:
                        sequence.append(("skill", launched[1]))
        elif message.get("type") == "result":
            final = message.get("result", "")
        elif message.get("type") == "item.completed":
            item = message["item"]
            if item.get("type") == "command_execution":
                command = recorded_shell_command(item.get("command", ""))
                if item.get("exit_code") == 0:
                    successful_reads.add(command)
                else:
                    partial_reads[command] = delivered_paths(command, item.get("aggregated_output", ""), destination, catalog)
            if item.get("type") == "agent_message":
                final = item.get("text", "")
    if host == "codex":
        for event in events:
            command = event["command"]
            if reads_content(command):
                read_attempts.extend(shell_read_paths(command))
            if event["allowed"]:
                paths = shell_read_paths(command) if command in successful_reads else partial_reads.get(command, [])
                for path in paths:
                    if Path(path).name == "SKILL.md":
                        credit_skill(path)
            sequence.append(("shell", command))
            if event["allowed"] and command in successful_reads and reads_content(command):
                sequence.append(("read", command))
            elif event["allowed"]:
                sequence.extend(("read", path) for path in partial_reads.get(command, []))
    operations = [(index, command) for index, (kind, command) in enumerate(sequence) if kind == "shell" and not read_only(command)]
    def succeeded(proof_command: str) -> bool:
        return proof_command in successful_reads if host == "codex" else proof_command not in failed

    return {"sequence": sequence, "operations": operations, "succeeded": succeeded, "loaded": [value for kind, value in sequence if kind == "skill"],
            "final": final, "protocol_copies": protocol_copies, "skill_paths": skill_paths,
            "foreign_skill_reads": foreign_skill_reads,
            "read_attempts": read_attempts,
            "load_order": load_order,
            "workspace": destination / "workspace",
            "compacted": any(message.get("subtype") == "compact_boundary" for message in messages)}


def owner_checks(seen: dict[str, Any], owner: str | list[str], helper: str | None = None) -> dict[str, bool]:
    """`owner` may list several skills when any of them properly owns the step."""
    operations, sequence = seen["operations"], seen["sequence"]
    owners = [owner] if isinstance(owner, str) else owner
    if not owners and helper is None:
        return {"owner_before_first_operation": not seen["loaded"]}
    if helper is None:
        # Without a single right helper, reads may come first; the owner must precede the
        # turn's first operation, or appear in the turn when it attempts none.
        before = sequence[:operations[0][0]] if operations else sequence
        return {"owner_before_first_operation": any(("skill", name) in before for name in owners)}
    before = sequence[:operations[0][0]] if operations else []
    return {"owner_before_first_operation": any(("skill", name) in before for name in owners),
            "helper_first": bool(operations) and helper in operations[0][1]}


def decision_checks(seen: dict[str, Any], expect: dict[str, Any]) -> dict[str, bool]:
    """Optional grades for what the turn decided, beyond which skill owned it."""
    commands = [command for _, command in seen["operations"]]
    checks = {}
    if "operation" in expect:
        checks["first_operation_matches"] = bool(commands) and re.search(expect["operation"], commands[0]) is not None
    if "require" in expect:
        checks["required_operation"] = any(re.search(expect["require"], command) for command in commands)
    if "prior" in expect:
        # A proof that must be gathered before the turn's first operation.
        sequence, operations = seen["sequence"], seen["operations"]
        before = sequence[:operations[0][0]] if operations else sequence
        patterns = [expect["prior"]] if isinstance(expect["prior"], str) else expect["prior"]
        checks["prior_read"] = all(any(kind == "shell" and re.search(pattern, command) and seen["succeeded"](command)
                                       for kind, command in before) for pattern in patterns)
    if "forbid" in expect:
        checks["no_forbidden_operation"] = not any(re.search(expect["forbid"], command) for command in commands)
    if "forbid_read" in expect:
        checks["no_forbidden_read"] = not any(re.search(expect["forbid_read"], path) for path in seen["read_attempts"])
    if "read" in expect:
        # A reference must be delivered before the turn acts on it; a listing or a failed read is not enough.
        operations, sequence = seen["operations"], seen["sequence"]
        before = sequence[:operations[0][0]] if operations else sequence
        checks["read_before_operation"] = any(kind == "read" and re.search(expect["read"], value) for kind, value in before)
    if "owner_before_read" in expect:
        requirement = expect["owner_before_read"]
        observations = seen["load_order"]
        matching_reads = [read for read in observations["reads"]
                          if re.search(requirement["read"], read["value"])]
        checks["owner_before_read"] = bool(matching_reads) and all(read["chosen_at"] is not None
            and any(skill["value"] == requirement["owner"]
                    and skill["completed_at"] < read["chosen_at"]
                    and skill["batch"] != read["batch"] for skill in observations["skills"])
            for read in matching_reads)
    if "final" in expect:
        checks["final_matches"] = re.search(expect["final"], seen["final"], re.IGNORECASE) is not None
    if "final_any" in expect:
        checks["final_matches_any"] = any(re.search(pattern, seen["final"], re.IGNORECASE) for pattern in expect["final_any"])
    if "final_none" in expect:
        checks["final_excludes"] = not any(re.search(pattern, seen["final"], re.IGNORECASE) for pattern in expect["final_none"])
    if "answer_from_fixture" in expect:
        answer = expect["answer_from_fixture"]
        workspace = seen["workspace"].resolve()
        path = (workspace / answer["file"]).resolve()
        match = None
        if path.is_relative_to(workspace):
            try:
                match = re.search(answer["capture"], path.read_text())
            except (OSError, UnicodeError):
                pass
        forms = [form.format(value=match[1]) for form in answer["forms"]] if match else []
        checks["fixture_answer"] = any(normalize_answer(form) == normalize_answer(seen["final"]) for form in forms)
    if expect.get("quiet"):
        checks["no_operations"] = not commands
        checks["no_unneeded_skills"] = not seen["loaded"]
    return checks


def score_run(host: str, case: str, destination: Path, catalog: Path = ROOT) -> dict[str, Any]:
    """Grade observed loads and first attempted operation; redirects cannot pass."""
    seen = observe(host, *load_trace(destination), destination, catalog)
    operations, loaded = seen["operations"], seen["loaded"]
    checks: dict[str, bool] = {}
    if case in {"direction-merge", "github-ci-watch"}:
        checks |= owner_checks(seen, *(("github", "gh-pr.py") if case == "direction-merge" else ("babysit-pr", "gh_pr_watch.py")))
        if case == "direction-merge":
            checks["valid_merge_arguments"] = bool(operations) and valid_merge_arguments(catalog, operations[0][1])
    else:
        checks["no_unneeded_skills"] = not loaded
        checks["no_operations"] = not operations
        if case == "adjacent-prose":
            checks["exact_response"] = seen["final"].strip() == "The pull request is waiting for review."
    checks["single_protocol_delivery"] = seen["protocol_copies"] <= 1
    checks["tested_catalog_only"] = not seen["foreign_skill_reads"]
    return {"grader_version": GRADER_VERSION, "passed": all(checks.values()), "checks": checks, "loaded_skills": loaded,
            "first_operation": operations[0][1] if operations else None, "protocol_copies": seen["protocol_copies"],
            "skill_paths": seen["skill_paths"], "foreign_skill_reads": seen["foreign_skill_reads"]}


def score_turns(host: str, turns: list[dict[str, Any]], destination: Path, catalog: Path = ROOT) -> dict[str, Any]:
    """Grade each turn on its own: a load from an earlier turn does not cover a later step."""
    messages, events = load_trace(destination)
    markers = [index for index, message in enumerate(messages) if message.get("type") == "turn_marker"]
    checks: dict[str, bool] = {"all_turns_ran": len(markers) == len(turns)}
    reports = []
    protocol_copies, foreign = 0, []
    confirmed: set[str] = set()
    for number, (turn, start) in enumerate(zip(turns, markers), 1):
        end = markers[number] if number < len(markers) else len(messages)
        event_start = messages[start].get("events", 0)
        event_end = messages[end].get("events", len(events)) if end < len(messages) else len(events)
        seen = observe(host, messages[start + 1:end], events[event_start:event_end], destination, catalog, confirmed)
        protocol_copies += seen["protocol_copies"]
        foreign += seen["foreign_skill_reads"]
        expect = turn["expect"]
        if expect == "quiet":
            turn_checks = {"no_unneeded_skills": not seen["loaded"], "no_operations": not seen["operations"]}
        elif expect == "compacted":
            turn_checks = {"compacted": seen["compacted"]}
        else:
            turn_checks = owner_checks(seen, expect["owner"], expect.get("helper")) | decision_checks(seen, expect)
        checks |= {f"turn{number}_{name}": value for name, value in turn_checks.items()}
        reports.append({"turn": number, "loaded_skills": seen["loaded"],
                        "first_operation": seen["operations"][0][1] if seen["operations"] else None,
                        "load_order": seen["load_order"]})
    # Compaction starts a fresh context, and the startup hook delivers the guide to it again.
    compactions = sum(message.get("subtype") == "compact_boundary" for message in messages)
    checks["single_protocol_delivery"] = protocol_copies <= 1 + compactions
    checks["tested_catalog_only"] = not foreign
    return {"grader_version": GRADER_VERSION, "passed": all(checks.values()), "checks": checks, "turns": reports,
            "protocol_copies": protocol_copies, "foreign_skill_reads": foreign}


def usage(messages: list[dict[str, Any]]) -> dict[str, int]:
    """Sum the tokens each host reports; input includes cached input."""
    totals = {"input": 0, "cached_input": 0, "output": 0}
    for message in messages:
        reported = message.get("usage", {})
        if message.get("type") == "result":
            totals["input"] += sum(reported.get(key, 0) for key in
                                   ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            totals["cached_input"] += reported.get("cache_read_input_tokens", 0)
            totals["output"] += reported.get("output_tokens", 0)
        elif message.get("type") == "turn.completed":
            totals["input"] += reported.get("input_tokens", 0)
            totals["cached_input"] += reported.get("cached_input_tokens", 0)
            totals["output"] += reported.get("output_tokens", 0)
    return totals


def hook_override(groups: list[dict[str, Any]]) -> str:
    return "[" + ",".join(
        "{matcher=" + json.dumps(group["matcher"]) + ",hooks=[" + ",".join(
            "{type=\"command\",command=" + json.dumps(handler["command"]) + "}"
            for handler in group["hooks"]
        ) + "]}" for group in groups
    ) + "]"


def claude_turns(command: list[str], prompts: list[str], fixture: Path, env: dict[str, str], out: Any, err: Any,
                 timeout: int) -> int | None:
    """Send each prompt as a user message and wait for its result before the next, in one process."""
    process = subprocess.Popen(command, cwd=fixture, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=err, text=True)
    watchdog = threading.Timer(timeout * len(prompts), process.kill)
    watchdog.start()
    assert process.stdin and process.stdout
    try:
        for number, prompt in enumerate(prompts, 1):
            out.write(json.dumps({"type": "turn_marker", "turn": number}) + "\n")
            process.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            process.stdin.flush()
            for line in process.stdout:
                out.write(line)
                if line.startswith("{") and json.loads(line).get("type") == "result":
                    break
            else:
                return None
        process.stdin.close()
        return process.wait()
    finally:
        watchdog.cancel()
        if process.poll() is None:
            process.kill()


def codex_turns(command: list[str], prompts: list[str], fixture: Path, env: dict[str, str], out: Any, err: Any,
                timeout: int, events: Path) -> int | None:
    """Resume the recorded session for each later prompt, marking where its shell events begin."""
    thread = None
    exit_code = None
    for number, prompt in enumerate(prompts, 1):
        count = len(events.read_text().splitlines()) if events.exists() else 0
        out.write(json.dumps({"type": "turn_marker", "turn": number, "events": count}) + "\n")
        out.flush()
        if number == 1:
            argv = [*command, prompt]
        else:
            if thread is None:
                return None
            options = [part for part in command[2:] if part not in {"--sandbox", "read-only"}]
            argv = ["codex", "exec", "resume", *options, "-c", 'sandbox_mode="read-only"', thread, prompt]
        try:
            result = subprocess.run(argv, cwd=fixture, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err,
                                    text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return None
        out.write(result.stdout)
        exit_code = result.returncode
        for line in result.stdout.splitlines():
            if line.strip():
                message = json.loads(line)
                if message.get("type") == "thread.started":
                    thread = message.get("thread_id")
        if exit_code != 0:
            return exit_code
    return exit_code


def run_case(host: str, catalog: Path, case: Path, destination: Path, model: str | None) -> dict[str, Any]:
    data = yaml.safe_load(case.read_text())
    turns = data.get("turns")
    destination.mkdir(parents=True)
    fixture = destination / "workspace"
    fixture.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=fixture", str(fixture)], check=True)
    # The Codex skill link is harness plumbing, not fixture state for the agent to judge.
    (fixture / ".git" / "info" / "exclude").write_text(".agents/\n")
    if data["name"] in {"direction-merge", "github-ci-watch"} or data.get("direction_fixture"):
        (fixture / "DIRECTION.md").write_text("# Direction\n\n## Purpose\n\nComplete the owner's repository task.\n")
    # Setup builds real Git state (commits, an upstream, dirty files) with a fixed
    # identity and clock, so the fixture is reproducible.
    setup_env = {**os.environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                 "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                 "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    for script in data.get("setup", []):
        subprocess.run(["sh", "-ec", script], cwd=fixture, env=setup_env, check=True, capture_output=True)
    for name, text in data.get("fixture_files", {}).items():
        (fixture / name).parent.mkdir(parents=True, exist_ok=True)
        (fixture / name).write_text(text)
    events = destination / "shell-events.jsonl"
    gate_command = shlex.join(["uv", "run", "--quiet", str(ROOT / "evals" / "shell_boundary.py"), str(events)])
    start_command = shlex.join(["uv", "run", "--quiet", str(catalog / "hooks" / "direction_check_hook.py")])
    hooks = {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": gate_command}]}],
    }
    prompts = [turn["prompt"] for turn in turns] if turns else [data["execution"]["prompt"]]
    env = dict(os.environ)
    # Disposable eval sessions cannot be verified by the real supervisor.
    env["SESSION_ALERTS_DISABLED"] = "1"
    env.pop("CLAUDECODE", None)
    if host == "claude":
        settings = destination / "settings.json"
        settings.write_text(json.dumps({"hooks": hooks, "permissions": {"allow": ["Read", "Skill", "Bash"]}}))
        command = ["claude", "-p", "--setting-sources", "", "--settings", str(settings),
                   "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--plugin-dir", str(catalog),
                   "--tools", "Read,Glob,Grep,Skill,Bash", "--permission-mode", "dontAsk", "--output-format", "stream-json",
                   "--verbose", "--no-session-persistence"]
        if turns:
            command.extend(["--input-format", "stream-json"])
        if model:
            command.extend(["--model", model])
    else:
        skills = fixture / ".agents" / "skills"
        skills.parent.mkdir()
        skills.symlink_to(catalog / "skills", target_is_directory=True)
        # Codex always scans $HOME/.agents/skills, so an installed catalog there
        # would compete with the tested one. Give each run an empty home and
        # keep the shared uv cache so hooks still start quickly.
        env.setdefault("UV_CACHE_DIR", str(Path.home() / ".cache" / "uv"))
        env["HOME"] = str(destination / "home")
        (destination / "home").mkdir()
        # Codex runs commands in a login shell; without the caller's PATH, macOS
        # path_helper puts the system Git shim first, which fails in the sandbox.
        (destination / "home" / ".zprofile").write_text(f"export PATH={shlex.quote(os.environ['PATH'])}\n")
        # The test sources are authored and inspected here. Trust bypass applies
        # only to these per-invocation test hooks; shell sandboxing stays read-only.
        command = ["codex", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                   "--sandbox", "read-only", "--json", "--dangerously-bypass-hook-trust",
                   "-c", 'model_reasoning_effort="medium"']
        # A per-run Codex home keeps the owner's sessions and installed skills
        # out of the run; only the login is shared.
        home = destination / "codex-home"
        home.mkdir()
        source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        (home / "auth.json").symlink_to(source_home / "auth.json")
        env["CODEX_HOME"] = str(home)
        if turns:
            # Resuming needs a recorded session, kept in that per-run home.
            command.remove("--ephemeral")
        declarations = json.loads((catalog / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"]
        for group in declarations:
            for handler in group["hooks"]:
                handler["command"] = shlex.join(["env", f"CLAUDE_PLUGIN_ROOT={catalog}", "sh", "-c", handler["command"]])
        hooks["PreToolUse"].extend(declarations)
        hooks["SessionStart"] = [{"matcher": "startup", "hooks": [{"type": "command", "command": start_command}]}]
        for event, groups in hooks.items():
            command.extend(["-c", f"hooks.{event}={hook_override(groups)}"])
        if model:
            command.extend(["--model", model])
        prompts = [re.sub(r"shared:([\w-]+)", r"$\1", prompt) for prompt in prompts]
    marker = destination / "direction-marker.json"
    marker.write_text('{"turn":"2999-01-01T00:00:00Z","audits":{}}')
    env["DIRECTION_MARKER"] = str(marker)
    timeout = data["execution"]["timeout_seconds"]
    receipt = {"host": host, "case": data["name"], "catalog": str(catalog), "source_digest": source_digest(catalog),
               "configured_model": model, "command": command, "prompts": prompts, "fixture": str(fixture),
               "harness_digest": hashlib.sha256(Path(__file__).read_bytes() + (ROOT / "evals" / "shell_boundary.py").read_bytes() + case.read_bytes()).hexdigest()}
    started = time.monotonic()
    with (destination / "trace.jsonl").open("w") as out, (destination / "stderr.log").open("w") as err:
        if turns:
            run = claude_turns if host == "claude" else codex_turns
            extra = () if host == "claude" else (events,)
            receipt["exit_code"] = run(command, prompts, fixture, env, out, err, timeout, *extra)
            if receipt["exit_code"] is None:
                receipt["error"] = "timeout or missing turn"
        else:
            try:
                result = subprocess.run([*command, prompts[0]], cwd=fixture, env=env, stdout=out, stderr=err,
                                        text=True, timeout=timeout, check=False)
                receipt["exit_code"] = result.returncode
            except subprocess.TimeoutExpired:
                receipt["error"] = "timeout"
    receipt["elapsed_seconds"] = round(time.monotonic() - started, 1)
    receipt["usage"] = usage(load_trace(destination)[0])
    receipt["score"] = (score_turns(host, turns, destination, catalog) if turns
                        else score_run(host, data["name"], destination, catalog))
    (destination / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=["claude", "codex"], required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case", default="*")
    parser.add_argument("--model")
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    # Multi-turn cases use turns.yaml so the native plugin eval does not load them.
    candidates = [*sorted((ROOT / "evals" / "owning-skill").glob("*/case.yaml")),
                  *sorted((ROOT / "evals" / "multi-turn").glob("*/turns.yaml")),
                  *sorted((ROOT / "evals" / "pr-monitoring").glob("*/turns.yaml")),
                  *sorted((ROOT / "evals" / "closeout").glob("*/turns.yaml")),
                  *sorted((ROOT / "evals" / "github-execution").glob("*/turns.yaml"))]
    cases = []
    for path in candidates:
        data = yaml.safe_load(path.read_text())
        if Path(data["name"]).match(args.case) and args.host in data.get("hosts", ["claude", "codex"]):
            cases.append(path)
    if not cases or args.runs < 1:
        parser.error("select at least one case and a positive run count")
    failed = False
    for case in cases:
        name = yaml.safe_load(case.read_text())["name"]
        for index in range(args.runs):
            receipt = run_case(args.host, args.catalog.resolve(), case, args.out.resolve() / f"{name}-{index + 1}", args.model)
            print(json.dumps({key: receipt.get(key) for key in ("host", "case", "exit_code", "error", "elapsed_seconds", "usage", "score")}), flush=True)
            failed |= receipt.get("exit_code") != 0 or not receipt["score"]["passed"]
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
