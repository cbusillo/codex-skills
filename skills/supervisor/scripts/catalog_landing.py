#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import argparse
import hashlib
import importlib.util
import json
import subprocess
import time
import uuid
from pathlib import Path

import account_choice
import iterm_tab
import release_session_claims as release
import session_liveness
from capacity_retry import save_state
from check_operation import GateFailed, Operation
from session_closeout import require
from session_record import load_ledger, read_jsonl, session_status


def observe(entry: dict, landing: str, *, after_size: int = 0, final: bool = False) -> dict:
    deadline = time.monotonic() + 120
    while True:
        try:
            status = session_status(entry)
        except FileNotFoundError:
            status = {"state": "starting"}
        if status["state"] == "turn_ended" and status["size"] > after_size:
            require(
                landing in status["last_text"] and (not final or status["safe_verdict"]),
                "smoke_response",
                "completed response does not acknowledge the landing or finish safely",
            )
            return status
        require(time.monotonic() < deadline, "smoke_response", "no completed acknowledgement within two minutes; session preserved")
        time.sleep(0.5)


def verify(config: dict, operation: Operation) -> dict:
    repo, pr, landing = config["repo"], config["pr"], config["landing_sha"]
    require(len(landing) == 40 and all(char in "0123456789abcdef" for char in landing), "landing", "requires a full landing SHA")
    reader = release.GitHubReader(gh_prefix_args=release.automation_only_gh_prefix_args(), deadline_at=time.time() + 120)
    pull = reader.get_json(f"/repos/{repo}/pulls/{pr}", step="catalog_landing")
    require(pull.get("merged") is True and pull.get("merge_commit_sha") == landing, "landing", "PR does not prove this landing SHA")
    source, runtime = Path(config["source_worktree"]), Path(config["runtime_worktree"])
    receipt = operation.run(
        "runtime",
        [
            "uv",
            "run",
            str(source / "skills/github/scripts/reconcile-runtime-checkout.py"),
            "--repo",
            repo,
            "--merged-worktree",
            str(source),
            "--landing-sha",
            landing,
        ],
        once=True,
    )
    require(
        receipt.get("status") in {"synchronized", "already_current"},
        "runtime",
        "runtime reconciliation did not prove an updated binding",
    )
    spec = importlib.util.spec_from_file_location(
        "landing_reconciler", source / "skills/github/scripts/reconcile-runtime-checkout.py"
    )
    reconciler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reconciler)
    common = reconciler.git_common_dir(source)
    bindings = [
        reconciler.inspect_binding(path, common)[2]
        for path, label, _ in reconciler.runtime_skills_paths()
        if label == receipt["runtime_home_source"]
    ]
    require(runtime.resolve() in bindings, "runtime", "configured runtime is not the reconciled binding")
    head = subprocess.run(
        ["git", "-C", str(runtime), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()
    require(head == receipt.get("after_sha"), "runtime", "configured runtime differs from the reconciliation receipt")
    ancestry = subprocess.run(["git", "-C", str(runtime), "merge-base", "--is-ancestor", landing, "HEAD"], timeout=10)
    require(ancestry.returncode == 0, "runtime", "runtime does not contain the landing")
    scripts = runtime / "skills/supervisor/scripts"
    ledger = Path(config["ledger"])
    owner = repo.split("/")[0]
    handoffs = []
    for harness, provider in (("codex", "openai"), ("claude", "anthropic")):
        completed = operation.state["steps"].get(harness + "_closeout")
        if completed and completed.get("complete") is True and completed["result"].get("ok") is True:
            handoffs.append(operation.state["steps"][harness + "_handoff"]["url"])
            continue
        phase = harness + "_launch"
        brief = operation.evidence / (harness + "-brief.txt")
        brief.write_text(
            f"Catalog landing smoke for https://github.com/{repo}/issues/{config['issue']}. "
            f"Landing SHA: {landing}. Reply briefly acknowledging this exact landing as your first reply. "
            "Use no tools, change no files, take no claims, post nothing. Remain idle afterward. "
            "Keep working through compaction. Keep this brief and landing SHA."
        )
        saved = operation.state["steps"].get(phase)
        if saved:
            require(saved.get("complete") is True, phase, "launch is uncertain; inspect evidence without relaunching")
            launch = saved["result"][0]
        else:
            command = Path(config["command_files"][harness])
            extra = ["--ledger", str(ledger)] if harness == "codex" else []
            if harness == "claude":
                choice = account_choice.select(
                    provider, config_path=Path(config["account_config"]) if config.get("account_config") else None
                )
                native = str(uuid.uuid4())
                prepared = operation.evidence / "claude-command.txt"
                text = command.read_text().strip()
                require("--resume" not in text and "--session-id" not in text, phase, "smoke requires a fresh native session")
                prepared.write_text(text + " --session-id " + native + "\n")
                command = prepared
                extra = ["--account", choice["name"]]
                home = Path(choice["env"].get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
                save_state(operation.evidence / "claude-native.json", {"session_id": native, "home": str(home)})
            if config.get("account_config"):
                extra += ["--account-config", config["account_config"]]
            launched = operation.run(
                phase,
                [
                    "uv",
                    "run",
                    str(scripts / "iterm_tab.py"),
                    "--backend",
                    "tmux",
                    "new",
                    "--window-id",
                    config["window_id"],
                    "--command-file",
                    str(command),
                    "--brief-file",
                    str(brief),
                    "--account-provider",
                    provider,
                    "--without-browser",
                    *extra,
                ],
                once=True,
            )
            require(len(launched) == 1, phase, "expected one terminal launch")
            launch = launched[0]
        if harness == "claude":
            native = json.loads((operation.evidence / "claude-native.json").read_text())
            paths = list((Path(native["home"]) / "projects").glob("*/" + native["session_id"] + ".jsonl"))
            require(len(paths) == 1, phase, "exact fresh Claude transcript is unavailable; session preserved")
            processes = [
                row
                for row in session_liveness.inventory()
                if row[1] == launch["tty"].removeprefix("/dev/")
                and session_liveness.harness_family((session_liveness.harness_arguments(row[2]) or [""])[0]) == "claude"
            ]
            require(len(processes) == 1, phase, "fresh Claude process identity is ambiguous")
            entry = {
                **launch,
                "session_id": native["session_id"],
                "tmux_session_id": launch["session_id"],
                "transcript": str(paths[0]),
                "pid": processes[0][0],
                "harness": harness,
                "supervisor_owned": True,
            }
            if not any(row["session_id"] == entry["session_id"] for row in load_ledger(ledger)):
                entry_file = operation.evidence / "claude-entry.json"
                save_state(entry_file, entry)
                operation.run(
                    "claude_register",
                    [
                        "uv",
                        "run",
                        str(scripts / "iterm_tab.py"),
                        "register",
                        "--entry-file",
                        str(entry_file),
                        "--ledger",
                        str(ledger),
                    ],
                    once=True,
                )
        else:
            entries = [row for row in load_ledger(ledger) if row["session_id"] == launch["native_session_id"]]
            require(len(entries) == 1, phase, "fresh Codex native registration is missing")
            entry = entries[0]
        if not operation.state["steps"].get(harness + "_handoff_request"):
            acknowledgement = observe(entry, landing)
            records = read_jsonl(Path(entry["transcript"]))
            require(
                not any(
                    row.get("payload", {}).get("type") in {"function_call", "custom_tool_call"}
                    or any(
                        block.get("type") == "tool_use"
                        for block in row.get("message", {}).get("content", [])
                        if isinstance(block, dict)
                    )
                    for row in records
                ),
                "smoke_acknowledgement",
                "first acknowledgement used tools; session preserved",
            )
            operation.state["steps"][harness + "_acknowledgement"] = {"size": acknowledgement["size"]}
            save_state(operation.path, operation.state)
        terminal = ["uv", "run", str(scripts / "iterm_tab.py"), "--backend", "tmux"]
        screen = operation.run(harness + "_screen", [*terminal, "read", "--session-id", entry["tmux_session_id"]])
        require(not screen.get("dead"), phase, "fresh smoke terminal ended prematurely")
        request = operation.evidence / (harness + "-handoff.txt")
        request.write_text(
            f"Post one final issue handoff comment on https://github.com/{repo}/issues/{config['issue']} "
            f"naming landing SHA {landing} and your native Session: {entry['session_id']}. "
            "Take no claims and change no files. Complete normal work-closeout, then give an unqualified Safe to exit: yes."
        )
        line = screen.get("input_line") or {}
        require(
            line.get("style") == "empty"
            or harness == "codex"
            and line.get("style") == "dim"
            and line.get("text") == "Ask Codex to do anything",
            "smoke_input",
            "nonempty or uncertain smoke input; preserve it",
        )
        operation.run(
            harness + "_handoff_request",
            [*terminal, "send", "--session-id", entry["tmux_session_id"], "--text-file", str(request), "--verified-target"],
            once=True,
        )
        observe(entry, landing, after_size=operation.state["steps"][harness + "_acknowledgement"]["size"], final=True)
        comments = reader.paged_json(f"/repos/{repo}/issues/{config['issue']}/comments", step_prefix=harness + "_handoff")
        matches = [
            row
            for row in comments
            if (row.get("user") or {}).get("login") == release.github_identity.automation_login()
            and landing in row.get("body", "")
            and release.release.related_sessions_match(row.get("body", ""), entry["session_id"])
        ]
        require(len(matches) == 1, "smoke_handoff", "requires one exact native-session issue handoff")
        entry["handoff_url"] = matches[0]["html_url"]
        operation.state["steps"][harness + "_handoff"] = {"url": entry["handoff_url"]}
        save_state(operation.path, operation.state)
        with release.closure.store_lock(ledger, create=True):
            rows = json.loads(ledger.read_text())
            for row in rows:
                if row.get("session_id") == entry["session_id"]:
                    row["handoff_url"] = entry["handoff_url"]
            save_state(ledger, rows)
        close = operation.run(
            harness + "_closeout",
            [
                "uv",
                "run",
                str(scripts / "session_closeout.py"),
                "--ledger",
                str(ledger),
                "--session-id",
                entry["session_id"],
                "--supervisor-session",
                config["supervisor_session"],
                "--owner",
                owner,
                "--evidence",
                str(operation.evidence / (harness + "-closeout")),
            ],
            once=True,
            timeout=900,
        )
        require(close.get("ok") is True, "smoke_closeout", "smoke launch passed; normal shutdown remains incomplete")
        handoffs.append(entry["handoff_url"])
    return {"landing_sha": landing, "runtime_sha": head, "smoke_handoffs": handoffs}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify one catalog landing through both fresh native harnesses.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    identity = "unknown"
    try:
        config = json.loads(args.config.read_text())
        identity = config["landing_sha"]
        with release.closure.store_lock(args.evidence / "verification", create=True):
            operation = Operation(args.evidence, [identity, hashlib.sha256(args.config.read_bytes()).hexdigest()])
            proof = verify(config, operation)
        result = {**operation.receipt(identity), **proof}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        result = {
            "ok": False,
            "identity": identity,
            "gate": error.gate if isinstance(error, GateFailed) else "verification",
            "reason": str(error),
            "evidence": str(args.evidence),
        }
    print(json.dumps(result, separators=(",", ":")))
    return int(not result["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
