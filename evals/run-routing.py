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


def score_run(host: str, case: str, destination: Path, catalog: Path = ROOT) -> dict[str, Any]:
    """Grade observed loads and first attempted operation; redirects cannot pass."""
    messages = [json.loads(line) for line in (destination / "trace.jsonl").read_text().splitlines() if line.strip()]
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
        elif message.get("type") == "result":
            final = message.get("result", "")
        elif message.get("type") == "item.completed":
            item = message["item"]
            if item.get("type") == "command_execution" and item.get("exit_code") == 0:
                successful_reads += item.get("command", "") + "\n"
            if item.get("type") == "agent_message":
                final = item.get("text", "")
    if host == "codex":
        events = destination / "shell-events.jsonl"
        for line in events.read_text().splitlines() if events.exists() else []:
            event = json.loads(line)
            command = event["command"]
            if event["allowed"]:
                for path in re.findall(r"[^\s'\"]*/skills/[^/\s'\"]+/SKILL\.md", command):
                    if path in successful_reads:
                        credit_skill(path)
            sequence.append(("shell", command))
    operations = [(index, command) for index, (kind, command) in enumerate(sequence) if kind == "shell" and not read_only(command)]
    loaded = [value for kind, value in sequence if kind == "skill"]
    checks: dict[str, bool] = {}
    if case in {"direction-merge", "github-ci-watch"}:
        owner, helper = ("github", "gh-pr.py") if case == "direction-merge" else ("babysit-pr", "gh_pr_watch.py")
        checks["owner_before_first_operation"] = bool(operations) and ("skill", owner) in sequence[:operations[0][0]]
        checks["helper_first"] = bool(operations) and helper in operations[0][1]
        if case == "direction-merge":
            checks["valid_merge_arguments"] = bool(operations) and valid_merge_arguments(catalog, operations[0][1])
    else:
        checks["no_unneeded_skills"] = not loaded
        checks["no_operations"] = not operations
        if case == "adjacent-prose":
            checks["exact_response"] = final.strip() == "The pull request is waiting for review."
    checks["single_protocol_delivery"] = protocol_copies <= 1
    checks["tested_catalog_only"] = not foreign_skill_reads
    return {"passed": all(checks.values()), "checks": checks, "loaded_skills": loaded,
            "first_operation": operations[0][1] if operations else None, "protocol_copies": protocol_copies,
            "skill_paths": skill_paths, "foreign_skill_reads": foreign_skill_reads}


def hook_override(groups: list[dict[str, Any]]) -> str:
    return "[" + ",".join(
        "{matcher=" + json.dumps(group["matcher"]) + ",hooks=[" + ",".join(
            "{type=\"command\",command=" + json.dumps(handler["command"]) + "}"
            for handler in group["hooks"]
        ) + "]}" for group in groups
    ) + "]"


def run_case(host: str, catalog: Path, case: Path, destination: Path, model: str | None) -> dict[str, Any]:
    data = yaml.safe_load(case.read_text())
    destination.mkdir(parents=True)
    fixture = destination / "workspace"
    fixture.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=fixture", str(fixture)], check=True)
    if data["name"] in {"direction-merge", "github-ci-watch"}:
        (fixture / "DIRECTION.md").write_text("# Direction\n\n## Purpose\n\nComplete the owner's repository task.\n")
    gate_command = shlex.join(["uv", "run", "--quiet", str(ROOT / "evals" / "shell_boundary.py"), str(destination / "shell-events.jsonl")])
    start_command = shlex.join(["uv", "run", "--quiet", str(catalog / "hooks" / "direction_check_hook.py")])
    hooks = {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": gate_command}]}],
    }
    prompt = data["execution"]["prompt"]
    if host == "claude":
        settings = destination / "settings.json"
        settings.write_text(json.dumps({"hooks": hooks, "permissions": {"allow": ["Read", "Skill", "Bash"]}}))
        command = ["claude", "-p", "--setting-sources", "", "--settings", str(settings),
                   "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--plugin-dir", str(catalog),
                   "--tools", "Read,Glob,Grep,Skill,Bash", "--permission-mode", "dontAsk", "--output-format", "stream-json",
                   "--verbose", "--no-session-persistence"]
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
        prompt = prompt.replace("shared:direction", "$direction").replace("shared:github", "$github")
    command.append(prompt)
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    marker = destination / "direction-marker.json"
    marker.write_text('{"turn":"2999-01-01T00:00:00Z","audits":{}}')
    env["DIRECTION_MARKER"] = str(marker)
    receipt = {"host": host, "case": data["name"], "catalog": str(catalog), "source_digest": source_digest(catalog),
               "configured_model": model, "command": command, "fixture": str(fixture),
               "harness_digest": hashlib.sha256(Path(__file__).read_bytes() + (ROOT / "evals" / "shell_boundary.py").read_bytes() + case.read_bytes()).hexdigest()}
    with (destination / "trace.jsonl").open("w") as out, (destination / "stderr.log").open("w") as err:
        try:
            result = subprocess.run(command, cwd=fixture, env=env, stdout=out, stderr=err,
                                    text=True, timeout=data["execution"]["timeout_seconds"])
            receipt["exit_code"] = result.returncode
        except subprocess.TimeoutExpired:
            receipt["error"] = "timeout"
    receipt["score"] = score_run(host, data["name"], destination, catalog)
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
    cases = [path for path in sorted((ROOT / "evals" / "owning-skill").glob("*/case.yaml"))
             if Path(yaml.safe_load(path.read_text())["name"]).match(args.case)]
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
