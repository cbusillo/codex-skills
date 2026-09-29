#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
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
from pathlib import Path
from typing import Any

import yaml
from shell_boundary import read_only

ROOT = Path(__file__).resolve().parents[1]


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
                            capture_output=True, text=True, timeout=15)
    return result.returncode == 0


def load_trace(destination: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    messages = [json.loads(line) for line in (destination / "trace.jsonl").read_text().splitlines() if line.strip()]
    events_path = destination / "shell-events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()] if events_path.exists() else []
    return messages, events


def observe(host: str, messages: list[dict[str, Any]], events: list[dict[str, Any]], destination: Path,
            catalog: Path, confirmed: set[str] | None = None) -> dict[str, Any]:
    """Order skill loads and shell commands as the host recorded them.

    Claude Code does not inject a skill's text again while it is still in
    context; a repeated invocation counts once `confirmed` shows that skill came
    from the tested catalog earlier in the same run.
    """
    confirmed = set() if confirmed is None else confirmed
    sequence: list[tuple[str, str]] = []
    final = ""
    protocol_copies = 0
    successful_reads = ""
    skill_paths: list[str] = []
    foreign_skill_reads: list[str] = []

    def credit_skill(path_text: str) -> None:
        skill_path = Path(path_text)
        if not skill_path.is_absolute():
            skill_path = destination / "workspace" / skill_path
        resolved = skill_path.resolve()
        skill_paths.append(str(resolved))
        if resolved.is_relative_to((catalog / "skills").resolve()):
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
        elif message.get("type") == "user":
            for block in message.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    match = re.match(r"Base directory for this skill: ([^\n]+)", block.get("text", ""))
                    if match:
                        credit_skill(str(Path(match[1]) / "SKILL.md"))
                if isinstance(block, dict) and block.get("type") == "tool_result" and not block.get("is_error"):
                    launched = re.fullmatch(r"Launching skill: (?:[\w-]+:)?([\w-]+)", str(block.get("content", "")).strip())
                    if launched and launched[1] in confirmed:
                        sequence.append(("skill", launched[1]))
        elif message.get("type") == "result":
            final = message.get("result", "")
        elif message.get("type") == "item.completed":
            item = message["item"]
            if item.get("type") == "command_execution" and item.get("exit_code") == 0:
                successful_reads += item.get("command", "") + "\n"
            if item.get("type") == "agent_message":
                final = item.get("text", "")
    if host == "codex":
        for event in events:
            command = event["command"]
            if event["allowed"]:
                for path in re.findall(r"[^\s'\"]*/skills/[^/\s'\"]+/SKILL\.md", command):
                    if path in successful_reads:
                        credit_skill(path)
            sequence.append(("shell", command))
    operations = [(index, command) for index, (kind, command) in enumerate(sequence) if kind == "shell" and not read_only(command)]
    return {"sequence": sequence, "operations": operations, "loaded": [value for kind, value in sequence if kind == "skill"],
            "final": final, "protocol_copies": protocol_copies, "skill_paths": skill_paths,
            "foreign_skill_reads": foreign_skill_reads,
            "compacted": any(message.get("subtype") == "compact_boundary" for message in messages)}


def owner_checks(seen: dict[str, Any], owner: str, helper: str) -> dict[str, bool]:
    operations, sequence = seen["operations"], seen["sequence"]
    return {"owner_before_first_operation": bool(operations) and ("skill", owner) in sequence[:operations[0][0]],
            "helper_first": bool(operations) and helper in operations[0][1]}


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
    return {"passed": all(checks.values()), "checks": checks, "loaded_skills": loaded,
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
            turn_checks = owner_checks(seen, expect["owner"], expect["helper"])
        checks |= {f"turn{number}_{name}": value for name, value in turn_checks.items()}
        reports.append({"turn": number, "loaded_skills": seen["loaded"],
                        "first_operation": seen["operations"][0][1] if seen["operations"] else None})
    # Compaction starts a fresh context, and the startup hook delivers the guide to it again.
    compactions = sum(message.get("subtype") == "compact_boundary" for message in messages)
    checks["single_protocol_delivery"] = protocol_copies <= 1 + compactions
    checks["tested_catalog_only"] = not foreign
    return {"passed": all(checks.values()), "checks": checks, "turns": reports,
            "protocol_copies": protocol_copies, "foreign_skill_reads": foreign}


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
                                    text=True, timeout=timeout)
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
    if data["name"] in {"direction-merge", "github-ci-watch"} or data.get("direction_fixture"):
        (fixture / "DIRECTION.md").write_text("# Direction\n\n## Purpose\n\nComplete the owner's repository task.\n")
    events = destination / "shell-events.jsonl"
    gate_command = shlex.join(["uv", "run", "--quiet", str(ROOT / "evals" / "shell_boundary.py"), str(events)])
    start_command = shlex.join(["uv", "run", "--quiet", str(catalog / "hooks" / "direction_check_hook.py")])
    hooks = {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": gate_command}]}],
    }
    prompts = [turn["prompt"] for turn in turns] if turns else [data["execution"]["prompt"]]
    env = dict(os.environ)
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
        # The test sources are authored and inspected here. Trust bypass applies
        # only to these per-invocation test hooks; shell sandboxing stays read-only.
        command = ["codex", "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                   "--sandbox", "read-only", "--json", "--dangerously-bypass-hook-trust",
                   "-c", 'model_reasoning_effort="medium"']
        if turns:
            # Resuming needs a recorded session; keep it in a per-run home so the
            # owner's session history is untouched. Only the login is shared.
            command.remove("--ephemeral")
            home = destination / "codex-home"
            home.mkdir()
            source_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            (home / "auth.json").symlink_to(source_home / "auth.json")
            env["CODEX_HOME"] = str(home)
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
        prompts = [prompt.replace("shared:direction", "$direction").replace("shared:github", "$github") for prompt in prompts]
    marker = destination / "direction-marker.json"
    marker.write_text('{"turn":"2999-01-01T00:00:00Z","audits":{}}')
    env["DIRECTION_MARKER"] = str(marker)
    timeout = data["execution"]["timeout_seconds"]
    receipt = {"host": host, "case": data["name"], "catalog": str(catalog), "source_digest": source_digest(catalog),
               "configured_model": model, "command": command, "prompts": prompts, "fixture": str(fixture),
               "harness_digest": hashlib.sha256(Path(__file__).read_bytes() + (ROOT / "evals" / "shell_boundary.py").read_bytes() + case.read_bytes()).hexdigest()}
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
                                        text=True, timeout=timeout)
                receipt["exit_code"] = result.returncode
            except subprocess.TimeoutExpired:
                receipt["error"] = "timeout"
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
                  *sorted((ROOT / "evals" / "multi-turn").glob("*/turns.yaml"))]
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
            print(json.dumps({key: receipt.get(key) for key in ("host", "case", "exit_code", "error", "score")}), flush=True)
            failed |= receipt.get("exit_code") != 0 or not receipt["score"]["passed"]
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
