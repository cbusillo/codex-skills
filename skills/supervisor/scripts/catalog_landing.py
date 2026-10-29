#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import argparse
import importlib.util
import json
import math
import subprocess
import time
import uuid
from pathlib import Path
from subprocess import SubprocessError

import account_choice
import iterm_tab
import session_liveness
from capacity_retry import save_state
from codex_session_proxy import read_launch_receipt
from check_operation import (
    GateFailed,
    GitHubReader,
    GitHubReadError,
    GitHubReadShapeError,
    Operation,
    receipt,
    automation_only_gh_prefix_args,
    claim_release,
    closure,
    github_identity,
    release,
)
from session_closeout import explicit_session, require
from session_record import load_ledger, read_jsonl, session_status, terminal_entry, text_content


def observe(entry: dict, landing: str, *, after_size: int = 0, final: bool = False) -> dict:
    deadline = time.monotonic() + (900 if final else 120)
    while True:
        try:
            status = session_status(entry)
        except FileNotFoundError:
            status = {"state": "starting"}
        if status["state"] == "turn_ended" and status["size"] > after_size:
            require(
                status["safe_verdict"] if final else landing in status["last_text"],
                "smoke_response",
                "completed response does not acknowledge the landing or finish safely",
            )
            return status
        require(
            time.monotonic() < deadline,
            "smoke_response",
            "handoff remains pending after a passed launch"
            if final
            else "no completed acknowledgement within two minutes; session preserved",
        )
        time.sleep(0.5)


def request_seen(entry: dict, text: str, after: int) -> bool:
    for row in read_jsonl(Path(entry["transcript"])):
        if not row.get("timestamp") or claim_release.stamp(row["timestamp"]).timestamp() < after:
            continue
        payload = row.get("payload", {})
        message = payload if row.get("type") == "response_item" else row.get("message", {})
        if (message.get("role") == "user" or row.get("type") == "user") and text_content(message.get("content")) == text:
            return True
        if row.get("type") == "event_msg" and payload.get("type") == "user_message" and payload.get("message") == text:
            return True
    return False


def verify(config: dict, operation: Operation) -> dict:
    repo, pr, landing = config["repo"], config["pr"], config["landing_sha"]
    require(len(landing) == 40 and all(char in "0123456789abcdef" for char in landing), "landing", "requires a full landing SHA")
    reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 120)
    pull = reader.get_json(f"/repos/{repo}/pulls/{pr}", step="catalog_landing")
    require(pull.get("merged") is True and pull.get("merge_commit_sha") == landing, "landing", "PR does not prove this landing SHA")
    source, runtime = Path(config["source_worktree"]), Path(config["runtime_worktree"])
    runtime_receipt = operation.run(
        "runtime",
        [
            "uv", "run",
            str(source / "skills/github/scripts/reconcile-runtime-checkout.py"),
            "--repo", repo,
            "--merged-worktree", str(source),
            "--landing-sha", landing,
        ],
        timeout=660,
    )
    require(
        runtime_receipt.get("status") in {"synchronized", "already_current"},
        "runtime",
        "runtime reconciliation did not prove an updated binding",
    )
    spec = importlib.util.spec_from_file_location(
        "landing_reconciler", source / "skills/github/scripts/reconcile-runtime-checkout.py"
    )
    if spec is None or spec.loader is None:
        raise GateFailed("runtime", "runtime reconciliation implementation is unavailable")
    reconciler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reconciler)
    common = reconciler.git_common_dir(source)
    bindings = [
        reconciler.inspect_binding(path, common)[2]
        for path, label, _ in reconciler.runtime_skills_paths()
        if label == runtime_receipt["runtime_home_source"]
    ]
    require(runtime.resolve() in bindings, "runtime", "configured runtime is not the reconciled binding")
    head = subprocess.run(
        ["git", "-C", str(runtime), "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
    ).stdout.strip()
    require(head == runtime_receipt.get("after_sha"), "runtime", "configured runtime differs from the reconciliation receipt")
    ancestry = subprocess.run(["git", "-C", str(runtime), "merge-base", "--is-ancestor", landing, "HEAD"], timeout=10)
    require(ancestry.returncode == 0, "runtime", "runtime does not contain the landing")
    handoffs, results, failures = [], {}, []
    for harness, provider in (("codex", "openai"), ("claude", "anthropic")):
        try:
            handoffs.append(verify_smoke(config, operation, harness, provider, runtime))
            results[harness] = {"ok": True, "launch_passed": True}
        except (OSError, ValueError, KeyError, TypeError, SubprocessError, GitHubReadError, GitHubReadShapeError) as error:
            failure = error if isinstance(error, GateFailed) else GateFailed(operation.gate, str(error))
            failures.append(failure)
            results[harness] = {
                "ok": False,
                "gate": failure.gate,
                "reason": str(failure),
                "launch_passed": harness + "_acknowledgement" in operation.state["steps"],
            }
    return {
        "ok": not failures,
        "gate": failures[0].gate if failures else "complete",
        "reason": str(failures[0]) if failures else None,
        "landing_sha": landing,
        "runtime_sha": head,
        "smoke_handoffs": handoffs,
        "harnesses": results,
    }


def verify_smoke(config: dict, operation: Operation, harness: str, provider: str, runtime: Path) -> str:
    repo, landing = config["repo"], config["landing_sha"]
    scripts, ledger, owner = runtime / "skills/supervisor/scripts", Path(config["ledger"]), repo.split("/")[0]
    completed = operation.state["steps"].get(harness + "_closeout")
    if completed and completed.get("complete") is True and completed["result"].get("ok") is True:
        return operation.state["steps"][harness + "_handoff"]["url"]
    phase = harness + "_launch"
    brief = operation.evidence / (harness + "-brief.txt")
    brief.write_text(
        f"Read-only anchor: https://github.com/{repo}/issues/{config['issue']}\n"
        f"Rules for this brief: {iterm_tab.BRIEF_RULES_URL}\n"
        f"Landing SHA: {landing}. Reply briefly acknowledging this exact landing as your first reply. "
        "Use no tools, change no files, take no claims, post nothing. Remain idle afterward. "
        "Keep working through compaction. Keep this brief and landing SHA."
    )
    saved: dict = operation.state["steps"].get(phase, {})
    if saved:
        if not saved.get("complete"):
            operation.gate = phase
            failure = json.loads(json.loads((operation.evidence / (phase + ".json")).read_text())["stdout"])
            retained = failure.get("failed_launch", {})
            require(harness == "codex" and retained.get("phase") == "register_ledger", phase,
                    "launch is uncertain; inspect its retained receipt without relaunching")
            launch = retained["launch"]
            entry = terminal_entry(ledger, "tmux_session_id", launch["session_id"])
            native = read_launch_receipt(Path(launch["native_receipt"]))
            expected = {**launch, **native, "harness": "codex", "supervisor_owned": True,
                        "tmux_session_id": launch["session_id"]}
            identity = lambda row: {key: value for key, value in release.closure_identity(row).items() if key != "handoff_url"}
            require(entry["session_id"] == native["session_id"] and identity(entry) == identity(expected), phase,
                    "registered source differs from the retained launch generation")
            saved = {"complete": True, "result": {**launch, "native_session_id": native["session_id"]}}
            operation.state["steps"][phase] = saved
            save_state(operation.path, operation.state)
        launch = saved["result"]
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
            words = iterm_tab.launch_words(text)
            require(
                session_liveness.claude_invocation(words).kind is None
                and "--" not in words
                and not any(word.startswith("--session-id") for word in words),
                phase,
                "smoke requires a fresh native session",
            )
            prepared.write_text(text + " --session-id " + native + "\n")
            command = prepared
            extra = ["--account", choice["name"]]
            home = Path(choice["env"].get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()
            save_state(operation.evidence / "claude-native.json", {"session_id": native, "home": str(home)})
        if config.get("account_config"):
            extra += ["--account-config", config["account_config"]]
        launch = operation.run(
            phase,
            [
                "uv", "run",
                str(scripts / "iterm_tab.py"),
                "--backend", "tmux",
                "new",
                "--window-id", config["window_id"],
                "--command-file", str(command),
                "--brief-file", str(brief),
                "--account-provider", provider,
                "--without-browser", *extra,
            ],
            once=True,
        )
    if harness == "claude":
        native = json.loads((operation.evidence / "claude-native.json").read_text())
        entry = iterm_tab.register_claude_launch(launch, native, ledger)
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
                    block.get("type") == "tool_use" for block in row.get("message", {}).get("content", []) if isinstance(block, dict)
                )
                for row in records
            ),
            "smoke_acknowledgement",
            "first acknowledgement used tools; session preserved",
        )
        operation.state["steps"][harness + "_acknowledgement"] = {"size": acknowledgement["size"]}
        save_state(operation.path, operation.state)
    terminal = ["uv", "run", str(scripts / "iterm_tab.py"), "--backend", "tmux"]
    request = operation.evidence / (harness + "-handoff.txt")
    request.write_text(
        f"Complete normal work-closeout, using its final issue handoff comment on https://github.com/{repo}/issues/{config['issue']} "
        f"naming landing SHA {landing} and your native Session: {entry['session_id']}. "
        "Take no claims and change no files. Then give an unqualified Safe to exit: yes."
    )
    request_gate = harness + "_handoff_request"
    sent = operation.state["steps"].get(request_gate)
    if sent and sent.get("complete") is not True:
        require(request_seen(entry, request.read_text(), operation.state["steps"][harness + "_request_time"]["value"]),
                request_gate, "request submission remains uncertain; inspect the exact source without replay")
        operation.state["steps"][request_gate] = {"complete": True, "result": {"delivery": "native_request_observed"}}
        save_state(operation.path, operation.state)
    if not sent:
        screen = operation.run(harness + "_screen", [*terminal, "read", "--session-id", entry["tmux_session_id"]])
        require(not screen.get("dead") and iterm_tab.empty_agent_input(screen), "smoke_input", "requires a live empty idle prompt")
        operation.state["steps"].setdefault(harness + "_request_time", {"value": math.floor(time.time())})
        save_state(operation.path, operation.state)
        operation.run(request_gate,
                      [*terminal, "send", "--session-id", entry["tmux_session_id"], "--text-file", str(request), "--verified-target"],
                      once=True)
    observe(entry, landing, after_size=operation.state["steps"][harness + "_acknowledgement"]["size"], final=True)
    operation.gate = harness + "_handoff"
    reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 120)
    comments = reader.paged_json(f"/repos/{repo}/issues/{config['issue']}/comments", step_prefix=harness + "_handoff")
    matches = [
        row
        for row in comments
        if (row.get("user") or {}).get("login") == github_identity.automation_login()
        and landing in row.get("body", "")
        and explicit_session(row.get("body", ""), entry["session_id"])
        and claim_release.stamp(row["created_at"]).timestamp() >= operation.state["steps"][harness + "_request_time"]["value"]
    ]
    require(bool(matches), "smoke_handoff", "requires an exact native-session issue handoff")
    entry["handoff_url"] = max(matches, key=lambda row: (row["created_at"], row["id"]))["html_url"]
    operation.state["steps"][harness + "_handoff"] = {"url": entry["handoff_url"]}
    save_state(operation.path, operation.state)
    close = operation.run(
        harness + "_closeout",
        [
            "uv", "run",
            str(scripts / "session_closeout.py"),
            "--ledger", str(ledger),
            "--session-id", entry["session_id"],
            "--supervisor-session", config["supervisor_session"],
            "--owner", owner,
            "--evidence", str(operation.evidence / (harness + "-closeout")),
            "--handoff-url", entry["handoff_url"],
        ],
        timeout=3600,
    )
    require(close.get("ok") is True, "smoke_closeout", "smoke launch passed; normal shutdown remains incomplete")
    operation.state["steps"][harness + "_closeout"] = {"complete": True, "result": close}
    save_state(operation.path, operation.state)
    return entry["handoff_url"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify one catalog landing through both fresh native harnesses.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    identity = "unknown"
    try:
        config = json.loads(args.config.read_text())
        identity = config["landing_sha"]
        with closure.store_lock(args.evidence / "verification", create=True):
            operation = Operation(args.evidence, [config["repo"], identity])
            proof = verify(config, operation)
        result = {**receipt(identity, args.evidence), **proof}
    except (OSError, ValueError, KeyError, TypeError, SubprocessError, GitHubReadError, GitHubReadShapeError) as error:
        result = receipt(identity, args.evidence, error,
                         gate=operation.gate if "operation" in locals() else "identity")
    print(json.dumps(result, separators=(",", ":")))
    return int(not result["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
