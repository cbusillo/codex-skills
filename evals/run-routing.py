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
import shlex
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def source_digest(catalog: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted([*catalog.glob("skills/**/SKILL.md"), *catalog.glob("skills/references/*.md"), *catalog.glob("hooks/*")]):
        if path.is_file():
            digest.update(str(path.relative_to(catalog)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def run_case(host: str, catalog: Path, case: Path, destination: Path, model: str | None) -> dict[str, object]:
    data = yaml.safe_load(case.read_text())
    destination.mkdir(parents=True, exist_ok=False)
    fixture = destination / "workspace"
    fixture.mkdir()
    gate_command = shlex.join(["uv", "run", "--quiet", str(ROOT / "evals" / "shell_boundary.py"), str(destination / "shell-events.jsonl")])
    start_command = shlex.join(["uv", "run", "--quiet", str(catalog / "hooks" / "direction_check_hook.py")])
    hooks = {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": gate_command}]}],
        "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": start_command}]}],
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
                   "-c", "features.hooks=true", "-c", 'model_reasoning_effort="medium"']
        for event, groups in hooks.items():
            handler = groups[0]["hooks"][0]
            value = '[{matcher="Bash",hooks=[{type="command",command=' + json.dumps(handler["command"]) + '}]}]'
            if event == "SessionStart":
                value = value.replace('matcher="Bash"', 'matcher="startup"')
            command.extend(["-c", f"hooks.{event}={value}"])
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
               "configured_model": model, "command": command, "fixture": str(fixture)}
    with (destination / "trace.jsonl").open("w") as out, (destination / "stderr.log").open("w") as err:
        try:
            result = subprocess.run(command, cwd=fixture, env=env, stdout=out, stderr=err,
                                    text=True, timeout=data["execution"]["timeout_seconds"])
            receipt["exit_code"] = result.returncode
        except subprocess.TimeoutExpired:
            receipt["error"] = "timeout"
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
            print(json.dumps({key: receipt.get(key) for key in ("host", "case", "exit_code", "error")}), flush=True)
            failed |= receipt.get("exit_code") != 0
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
