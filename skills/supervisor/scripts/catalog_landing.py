#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import argparse
import importlib.util
import json
import math
import os
import subprocess
import time
import uuid
from pathlib import Path
from subprocess import SubprocessError

import account_choice
import iterm_tab
import session_liveness
from check_changes import fingerprint, save_state
from check_operation import (
    GateFailed,
    GitHubReader,
    GitHubReadError,
    GitHubReadShapeError,
    Operation,
    automation_only_gh_prefix_args,
    claim_release,
    closure,
    github_identity,
    receipt,
    release,
)
from codex_session_proxy import read_launch_receipt
from session_closeout import explicit_session, require
from session_record import (
    TranscriptChanged,
    load_ledger,
    read_jsonl,
    session_status,
    terminal_entry,
    text_content,
)


def observe(entry: dict, landing: str, *, after_size: int = 0, final: bool = False) -> dict:
    deadline = time.monotonic() + (900 if final else 120)
    while True:
        try:
            status = session_status(entry)
        except (FileNotFoundError, TranscriptChanged):
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


def reconcile_runtime(config: dict, operation: Operation) -> tuple[Path, str]:
    repo, pr, landing = config["repo"], config["pr"], config["landing_sha"]
    require(len(landing) == 40 and all(char in "0123456789abcdef" for char in landing), "landing", "requires a full landing SHA")
    reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 120)
    pull = reader.get_json(f"/repos/{repo}/pulls/{pr}", step="catalog_landing")
    require(pull.get("merged") is True and pull.get("merge_commit_sha") == landing, "landing", "PR does not prove this landing SHA")
    source, runtime = Path(config["source_worktree"]), Path(config["runtime_worktree"])
    runtime_receipt = operation.run(
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
    return runtime, head


def verify(config: dict, operation: Operation, runtime: Path, head: str, *, skip_smoke: bool = False) -> dict:
    landing = config["landing_sha"]
    if skip_smoke:
        return {
            "ok": True,
            "landing_sha": landing,
            "runtime_sha": head,
            "smoke_handoffs": [],
            "harnesses": {},
            "smoke_skipped": "documentation only",
        }
    handoffs, results, failures = [], {}, []
    for harness, provider in (("codex", "openai"), ("claude", "anthropic")):
        try:
            handoffs.append(verify_smoke(config, operation, harness, provider, runtime))
            results[harness] = {"ok": True, "launch_passed": True}
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            SubprocessError,
            GitHubReadError,
            GitHubReadShapeError,
        ) as error:
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
        "runtime_after_sha": git(runtime, "rev-parse", "HEAD"),
        "smoke_handoffs": handoffs,
        "harnesses": results,
    }


def verify_smoke(config: dict, operation: Operation, harness: str, provider: str, runtime: Path) -> str:
    repo, landing = config["repo"], config["landing_sha"]
    scripts, ledger = runtime / "skills/supervisor/scripts", Path(config["ledger"])
    if operation.state["steps"].get(harness + "_handoff"):
        return close_smoke(config, operation, harness, runtime)
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
            timeout=360,
        )
    if harness == "claude":
        native = json.loads((operation.evidence / "claude-native.json").read_text())
        entry = iterm_tab.register_claude_launch(launch, native, ledger)
    else:
        entries = [row for row in load_ledger(ledger) if row["session_id"] == launch["native_session_id"]]
        require(len(entries) == 1, phase, "fresh Codex native registration is missing")
        entry = entries[0]
    record_smoke_source(operation, harness, entry, ledger)
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
    operation.state["steps"][harness + "_handoff"] = {"url": entry["handoff_url"], "session_id": entry["session_id"]}
    save_state(operation.path, operation.state)
    return close_smoke(config, operation, harness, runtime)


def record_smoke_source(operation: Operation, harness: str, entry: dict, ledger: Path) -> None:
    source = {"session_id": entry["session_id"], "ledger": str(ledger.resolve()),
              "generation": fingerprint({key: value for key, value in release.closure_identity(entry).items() if key != "handoff_url"})}
    require(operation.state["steps"].get(harness + "_source", source) == source,
            "identity", "smoke source generation changed")
    operation.state["steps"][harness + "_source"] = source
    save_state(operation.path, operation.state)


def close_smoke(config: dict, operation: Operation, harness: str, runtime: Path) -> str:
    handoff = operation.state["steps"][harness + "_handoff"]
    completed = operation.state["steps"].get(harness + "_closeout")
    if completed and completed.get("complete") is True and completed["result"].get("ok") is True:
        return handoff["url"]
    close = operation.run(harness + "_closeout", [
        "uv", "run", str(runtime / "skills/supervisor/scripts/session_closeout.py"),
        "--ledger", config["ledger"], "--session-id", handoff["session_id"],
        "--supervisor-session", config["supervisor_session"], "--owner", config["repo"].split("/")[0],
        "--evidence", str(operation.evidence / (harness + "-closeout")), "--handoff-url", handoff["url"],
    ], timeout=3600)
    require(close.get("ok") is True, "smoke_closeout", "smoke launch passed; normal shutdown remains incomplete")
    operation.state["steps"][harness + "_closeout"] = {"complete": True, "result": close}
    save_state(operation.path, operation.state)
    return handoff["url"]


def git(source: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True, check=True, timeout=120).stdout.strip()


def pending_landings(source: Path, previous: str | None, tip: str) -> list[str]:
    if previous is None:
        return [tip]
    history = git(source, "rev-list", "--first-parent", tip).splitlines()
    require(previous in history, "discovery", "last verified landing is absent from default-branch history")
    return list(reversed(history[:history.index(previous)]))


def documentation_only(source: Path, landings: list[str]) -> bool:
    files = git(source, "diff-tree", "--no-commit-id", "--name-only", "-r", landings[0] + "^", landings[-1]).splitlines()
    return bool(files) and all(
        Path(path).suffix.lower() == ".md" and (path == "README.md" or path.startswith("docs/")) for path in files
    )


def landing_pull(reader: GitHubReader, repo: str, landing: str) -> int:
    deadline = reader.deadline_at if reader.deadline_at is not None else time.time() + 120
    while time.time() < deadline:
        pulls = reader.paged_json(f"/repos/{repo}/commits/{landing}/pulls", step_prefix="landing_pulls")
        matches = [
            pull["number"]
            for pull in pulls
            if pull.get("merged_at")
            and pull.get("merge_commit_sha") == landing
            and pull.get("base", {}).get("repo", {}).get("full_name") == repo
        ]
        if matches:
            return max(matches)
        time.sleep(min(2, max(0, deadline - time.time())))
    raise GateFailed("discovery", f"default-branch commit {landing} has no exact merged PR after discovery deadline")


def post_result(config: dict, operation: Operation, result: dict) -> str:
    from skills.github.scripts import github_api, github_comment

    body = operation.evidence / "result.md"
    outcome = "PASSED" if result["ok"] else "FAILED"
    body.write_text(
        f"Catalog runtime verification {outcome}: `{result.get('runtime_sha', 'UNKNOWN')}`\n\n"
        f"Landings requested: {json.dumps(result['landings'])}.\n"
        f"Runtime reconciled to `{result.get('runtime_sha', 'UNKNOWN')}`; "
        f"HEAD observed after smoke: `{result.get('runtime_after_sha', 'not measured')}`.\n"
        "Coverage is containment; per-session loaded SHA was not measured.\n"
        + ("Bootstrap: earlier history is unverified.\n" if result.get("bootstrap") else "")
        + (f"Supersedes failed verification: {result['superseded_url']}\n" if result.get("superseded_url") else "")
        + (f"Smoke skipped: {result['smoke_skipped']}.\n" if result.get("smoke_skipped") else "")
        + "\n".join(result.get("smoke_handoffs", []))
        + (
            f"\nFailed gate: {result.get('gate', 'verification')}. Inspect the retained service evidence.\n"
            if not result["ok"]
            else "\nRuntime reconciliation completed. Live smoke and closeout: "
            + ("skipped for documentation only.\n" if result.get("smoke_skipped") else "both sessions acknowledged the landing and closed normally.\n")
        )
    )
    operation_id = fingerprint(result)
    body.write_text(github_api.body_with_operation_marker(body.read_text(), operation_id))
    phase = "report_" + operation_id
    prior = operation.state["steps"].get(phase)
    if prior and prior.get("complete") is not True:
        reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 120)
        comments = reader.paged_json(f"/repos/{config['repo']}/issues/{config['issue']}/comments", step_prefix="report_reconciliation")
        canonical = lambda text: github_comment.OPERATION_MARKER_LINE_RE.sub("", text).rstrip()
        matches = [row for row in comments
                   if (row.get("user") or {}).get("login") == github_identity.automation_login()
                   and github_api.body_has_operation_marker(row.get("body"), operation_id)
                   and canonical(row["body"]) == canonical(body.read_text())]
        require(len(matches) == 1, "report", "original report is missing or ambiguous; no write replayed")
        operation.state["steps"][phase] = {"complete": True, "result": {"url": matches[0]["html_url"]}}
        save_state(operation.path, operation.state)
    posted = operation.run(
        phase,
        [
            str(Path(config["source_worktree"]) / "skills/github/scripts/gh-comment"),
            "issue",
            str(config["issue"]),
            "--repo",
            config["repo"],
            "--body-file",
            str(body),
        ],
        once=True,
        timeout=360,
    )
    return posted["url"]


def require_closed_smokes(config: dict, operation: Operation) -> None:
    steps = operation.state["steps"]
    require(not any(step.get("complete") is False for step in steps.values()),
            "supersede", "resolve every uncertain mutation in the original journal before superseding")
    for harness in ("codex", "claude"):
        if not steps.get(harness + "_launch"):
            continue
        source = steps.get(harness + "_source")
        if not isinstance(source, dict):
            raise GateFailed("supersede", "original smoke source is missing")
        require(source["ledger"] == str(Path(config["ledger"]).resolve()), "supersede", "original ledger changed")
        close_evidence = operation.evidence / (harness + "-closeout")
        closed = json.loads((close_evidence / "operation.json").read_text())
        require(closed.get("identity") == [source["ledger"], source["session_id"]]
                and closed.get("generation") == source["generation"] == closed.get("retired_generation"),
                "supersede", "exact original smoke generation has not completed normal closeout")
        result = operation.run("supersede_" + harness, [
            "uv", "run", str(Path(config["runtime_worktree"]) / "skills/supervisor/scripts/session_closeout.py"),
            "--ledger", source["ledger"], "--session-id", source["session_id"],
            "--supervisor-session", config["supervisor_session"], "--owner", config["repo"].split("/")[0],
            "--evidence", str(close_evidence),
        ], timeout=3600)
        require(result.get("ok") is True, "supersede", "original smoke closeout revalidation failed")


def require_supervisor_identity(config: dict) -> None:
    require(isinstance(config.get("supervisor_session"), str) and bool(config["supervisor_session"].strip()),
            "identity", "catalog closeout requires supervisor_session")


def poll(config: dict, evidence: Path, *, supersede_failed: bool = False) -> dict:
    require_supervisor_identity(config)
    source = Path(config["source_worktree"])
    state_path = evidence / "cursor.json"
    with closure.store_lock(evidence / "poll", create=True):
        state: dict = json.loads(state_path.read_text()) if state_path.exists() else {"repo": config["repo"], "last_sha": None}
        require(state.get("repo") == config["repo"], "identity", "cursor belongs to another repository")
        discovery = Operation(evidence / "discovery", [config["repo"], "discovery"])
        runtime = discovery.run("runtime", ["uv", "run", str(source / "skills/github/scripts/reconcile-runtime-checkout.py"),
                                           "--repo", config["repo"]], timeout=660)
        require(runtime.get("status") in {"synchronized", "already_current"}, "discovery", "runtime reconciliation incomplete")
        current: dict = dict(config)
        landings = state.get("pending") or pending_landings(source, state["last_sha"], runtime["after_sha"])
        superseded = state.get("superseded")
        superseded_url = state.get("superseded_url")
        if supersede_failed:
            require(bool(state.get("pending") and state.get("attempt")), "supersede", "no failed attempt to supersede")
            old = Operation(evidence / state["attempt"], [config["repo"], *landings])
            old_result = json.loads((old.evidence / "result.json").read_text())
            require(old_result.get("ok") is False,
                    "supersede", "only a failed verification can be superseded")
            with closure.store_lock(old.evidence / "verification", create=True):
                require_closed_smokes(current, old)
            superseded = str(old.evidence)
            superseded_url = old.state["steps"].get("report_" + fingerprint(old_result), {}).get("result", {}).get("url")
            landings += pending_landings(source, landings[-1], runtime["after_sha"])
            state.update(attempt=landings[-1] + "-" + uuid.uuid4().hex,
                         superseded=superseded, superseded_url=superseded_url)
        if not landings:
            return {"ok": True, "landings": []}
        landing = landings[-1]
        state["pending"] = landings
        state.setdefault("attempt", landing)
        save_state(state_path, state)
        with closure.store_lock(evidence / state["attempt"] / "verification", create=True):
            operation = Operation(evidence / state["attempt"], [config["repo"], *landings])
            current["landing_sha"] = landing
            result_path = operation.evidence / "result.json"
            saved: dict = json.loads(result_path.read_text()) if result_path.exists() else {}
            if saved and saved["ok"]:
                result = saved
            else:
                if saved:
                    post_result(current, operation, saved)
                reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 120)
                current["pr"] = landing_pull(reader, config["repo"], landing)
                skip_smoke = documentation_only(source, landings)
                bound, head = reconcile_runtime(current, operation)
                try:
                    result = verify(current, operation, bound, head, skip_smoke=skip_smoke)
                except (OSError, ValueError, KeyError, TypeError, AttributeError, SubprocessError,
                        GitHubReadError, GitHubReadShapeError) as error:
                    result = receipt(landing, operation.evidence, error, gate=operation.gate)
                result.update(landings=landings, bootstrap=state["last_sha"] is None, superseded=superseded, superseded_url=superseded_url)
                save_state(result_path, result)
            url = post_result(current, operation, result)
            if result["ok"]:
                state.update(last_sha=landing, pending=[])
                for key in ("attempt", "superseded", "superseded_url"):
                    state.pop(key, None)
                save_state(state_path, state)
            return {**result, "verification_url": url}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify one catalog landing through both fresh native harnesses.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--poll", action="store_true", help="check new default-branch landings through the existing service runner")
    recovery = parser.add_mutually_exclusive_group()
    recovery.add_argument("--resume-failed", action="store_true", help="compatibility option; polling resumes the existing journal")
    recovery.add_argument("--supersede-failed", action="store_true", help="start fresh only after exact old smoke sessions closed normally")
    args = parser.parse_args()
    if (args.resume_failed or args.supersede_failed) and not args.poll:
        parser.error("recovery flags require --poll; bounded verification reuses its original evidence directly")
    os.umask(0o077)
    identity = "unknown"
    try:
        config = json.loads(args.config.read_text())
        require_supervisor_identity(config)
        if args.poll:
            result = poll(config, args.evidence, supersede_failed=args.supersede_failed)
            print(json.dumps(result, separators=(",", ":")))
            return int(not result["ok"])
        identity = config["landing_sha"]
        with closure.store_lock(args.evidence / "verification", create=True):
            operation = Operation(args.evidence, [config["repo"], identity])
            bound, head = reconcile_runtime(config, operation)
            proof = verify(config, operation, bound, head)
        result = {**receipt(identity, args.evidence), **proof}
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        SubprocessError,
        GitHubReadError,
        GitHubReadShapeError,
    ) as error:
        result = receipt(identity, args.evidence, error, gate=operation.gate if "operation" in locals() else "identity")
    print(json.dumps(result, separators=(",", ":")))
    return int(not result["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
