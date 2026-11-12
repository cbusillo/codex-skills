#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["zstandard==0.25.0"]
# ///
import argparse
import importlib.util
import json
import math
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import SubprocessError

import iterm_tab
import session_liveness
from check_changes import fingerprint, save_state
from check_operation import (
    GateFailed,
    GitHubReader,
    GitHubReadError,
    GitHubReadShapeError,
    Operation,
    RetryableGate,
    automation_only_gh_prefix_args,
    claim_release,
    closure,
    github_identity,
    receipt,
    release,
)
from codex_session_proxy import read_launch_receipt
from session_closeout import closeout_mutations_settled, explicit_session, require, validate_retired_closeout
from session_record import (
    TranscriptChanged,
    load_ledger,
    read_jsonl,
    session_status,
    terminal_entry,
    text_content,
)

from skills.github.scripts.github_comment import RECONCILIATION_CLOCK_SKEW_SECONDS, canonical_comment_body


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


def reconcile_catalog_runtime(source: Path, operation: Operation, *arguments: str, repo: str) -> dict:
    return operation.run(
        "runtime", ["uv", "run", str(source / "skills/github/scripts/reconcile-runtime-checkout.py"),
                    "--repo", repo, "--timeout-seconds", "120", *arguments],
        timeout=660, retry_read=True,
    )


def reconcile_runtime(config: dict, operation: Operation, *, bind_landing: bool = True) -> tuple[Path, str]:
    repo, landing = config["repo"], config["landing_sha"]
    require(len(landing) == 40 and all(char in "0123456789abcdef" for char in landing), "landing", "requires a full landing SHA")
    reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 120)
    landing_pull(reader, repo, landing, expected_pr=config.get("pr"))
    source, runtime = Path(config["source_worktree"]), Path(config["runtime_worktree"])
    runtime_receipt = reconcile_catalog_runtime(
        source, operation, "--merged-worktree", str(source),
        *(["--landing-sha", landing] if bind_landing else []), repo=repo,
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
    try:
        contains_landing = reconciler.git_is_first_parent(runtime, landing, head)
    except reconciler.GitCommandError as error:
        raise GateFailed("runtime", "first-parent history could not be verified") from error
    require(contains_landing, "runtime", "runtime does not contain the landing on its first-parent history")
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
            require(retained.get("phase") == "register_ledger", phase,
                    "launch is uncertain; inspect its retained receipt without relaunching")
            launch = retained["launch"]
            if harness == "claude":
                native = iterm_tab.register_file_launch(launch, launch.get("file_native") or launch["claude_native"], ledger)
            else:
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
        extra = ["--ledger", str(ledger)]
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
    entries = [row for row in load_ledger(ledger) if row["session_id"] == launch["native_session_id"]]
    require(len(entries) == 1, phase, "fresh native registration is missing")
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
    since = comment_read_since(datetime.fromtimestamp(operation.state["steps"][harness + "_request_time"]["value"], UTC))
    comments = reader.paged_json(f"/repos/{repo}/issues/{config['issue']}/comments", step_prefix=harness + "_handoff",
                                 params={"since": since}, required_query_params={"since": since})
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


def landing_pull(reader: GitHubReader, repo: str, landing: str, *, expected_pr: int | None = None) -> int:
    deadline = reader.deadline_at if reader.deadline_at is not None else time.time() + 120
    while time.time() < deadline:
        pulls = reader.paged_json(f"/repos/{repo}/commits/{landing}/pulls", step_prefix="landing_pulls")
        matches = [
            pull["number"]
            for pull in pulls
            if pull.get("merged_at")
            and pull.get("merge_commit_sha") == landing
            and pull.get("base", {}).get("repo", {}).get("full_name") == repo
            and (expected_pr is None or pull["number"] == expected_pr)
        ]
        if matches:
            return max(matches)
        time.sleep(min(2, max(0, deadline - time.time())))
    configured = f" matching configured PR {expected_pr}" if expected_pr is not None else ""
    raise GateFailed("discovery", f"default-branch commit {landing} has no exact merged PR{configured} after discovery deadline")


def comment_read_since(started: datetime) -> str:
    return (started - timedelta(seconds=RECONCILIATION_CLOCK_SKEW_SECONDS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def post_result(config: dict, operation: Operation, result: dict) -> str:
    body = operation.evidence / "result.md"
    operation_id = fingerprint(result)
    phase = "report_" + operation_id
    prior = operation.state["steps"].get(phase)
    retries = operation.state.get("report_retries", {}).get(phase, [])
    if prior and prior.get("complete") is True:
        return prior["result"]["url"]
    if prior or retries:
        require(body.is_file(), "report", "original report body is missing; no write replayed")
    else:
        outcome = "PASSED" if result["ok"] else "FAILED"
        superseded = Path(result["superseded"]) if result.get("superseded") else None
        previous = json.loads((superseded / "result.json").read_text()) if superseded else {}
        body.write_text(
            f"Catalog runtime verification {outcome}: `{result.get('runtime_sha', 'UNKNOWN')}`\n\n"
            f"Landings requested: {json.dumps(result['landings'])}.\n"
            f"Runtime reconciled to `{result.get('runtime_sha', 'UNKNOWN')}`; "
            f"HEAD observed after smoke: `{result.get('runtime_after_sha', 'not measured')}`.\n"
            "Coverage is containment; per-session loaded SHA was not measured.\n"
            + ("Bootstrap: earlier history is unverified.\n" if result.get("bootstrap") else "")
            + (f"Supersedes failed verification: {result['superseded_url']}\n" if result.get("superseded_url") else "")
            + (
                f"Superseded attempt: `{Path(result['superseded']).name}`; "
                f"failed gate: `{previous.get('gate', 'verification')}`; commit: `{previous['landings'][-1]}`.\n"
                if result.get("superseded")
                else ""
            )
            + (f"Smoke skipped: {result['smoke_skipped']}.\n" if result.get("smoke_skipped") else "")
            + "\n".join(result.get("smoke_handoffs", []))
            + (
                f"\nFailed gate: {result.get('gate', 'verification')}. Inspect the retained service evidence.\n"
                if not result["ok"]
                else "\nRuntime reconciliation completed. Live smoke and closeout: "
                + (
                    "skipped for documentation only.\n"
                    if result.get("smoke_skipped")
                    else "both sessions acknowledged the landing and closed normally.\n"
                )
            )
        )

    def reconcile_report() -> str | None:
        active = operation.state["steps"].get(phase)
        saved = active or retries[-1]["step"]
        reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + 120)
        started = claim_release.stamp(retries[0]["step"]["started_at"] if retries else saved["started_at"])
        since = comment_read_since(started)
        comments = reader.paged_json(
            f"/repos/{config['repo']}/issues/{config['issue']}/comments",
            step_prefix="report_reconciliation",
            params={"since": since},
            required_query_params={"since": since},
        )
        matches = [
            row
            for row in comments
            if (row.get("user") or {}).get("login") == github_identity.automation_login()
            and canonical_comment_body(row["body"]) == canonical_comment_body(body.read_text())
            and claim_release.stamp(row["created_at"]) >= started - timedelta(seconds=RECONCILIATION_CLOCK_SKEW_SECONDS)
        ]
        if active:
            saved["delivery"] = "not_posted" if not matches and saved.get("process_exited") is True else "unknown"
            save_state(operation.path, operation.state)
        if not matches and not active:
            return None
        if not matches and saved.get("process_exited") is True:
            helper = json.loads((operation.evidence / (phase + ".json")).read_text())
            payload = json.loads(helper["stdout"]) if helper.get("stdout") else {}
            expected = github_identity.automation_login()
            if (
                isinstance(payload, dict)
                and payload.get("ok") is False
                and payload.get("retry_exhausted_reason") == "deadline_exceeded"
                and (payload.get("failure") or {}).get("cause") == "deadline_exceeded"
                and payload.get("write_outcome") in {"not_started", "rejected"}
                and payload.get("outcome_certainty") in {"not_applicable", "confirmed_not_applied"}
                and expected
                and payload.get("expected_actor") == expected
                and payload.get("actor") in {None, expected}
                and not (payload.get("reconciliation") or {}).get("result")
                and not {"create_comment", "reconcile_create_comment"}.intersection(payload.get("completed_steps", []))
            ):
                operation.state.setdefault("report_retries", {}).setdefault(phase, []).append({"step": saved, "helper": helper})
                operation.state["steps"].pop(phase)
                save_state(operation.path, operation.state)
                raise RetryableGate("report", "report deadline expired with confirmed no write and complete absent readback")
        require(len(matches) == 1, "report", "original report is not posted or ambiguous; no write replayed")
        operation.state["steps"][phase] = {"complete": True, "result": {"url": matches[0]["html_url"]}}
        save_state(operation.path, operation.state)
        return matches[0]["html_url"]

    if prior or retries:
        require(
            bool((prior or retries[0]["step"]).get("started_at")), "report", "legacy report has unknown delivery; no write replayed"
        )
        reconciled = reconcile_report()
        if reconciled:
            return reconciled
    try:
        timeout = 360
        deadline = min(time.time() + timeout - 30, float(os.environ.get("GITHUB_RETRY_DEADLINE_AT", "inf")))
        posted = operation.run(
            phase,
            [
                "env",
                f"GITHUB_RETRY_DEADLINE_AT={deadline}",
                str(Path(config["source_worktree"]) / "skills/github/scripts/gh-comment"),
                "issue",
                str(config["issue"]),
                "--repo",
                config["repo"],
                "--body-file",
                str(body),
            ],
            once=True,
            timeout=timeout,
        )
    except GateFailed:
        if operation.state["steps"].get(phase, {}).get("complete") is False:
            reconciled = reconcile_report()
            if reconciled:
                return reconciled
        raise
    return posted["url"]


def superseded_closeout(operation: Operation, harness: str, source: dict) -> tuple[Path, str]:
    evidence = operation.evidence / (harness + "-closeout")
    path = evidence / "operation.json"
    original = json.loads(path.read_text()) if path.exists() else {}
    if original:
        require(
            original.get("identity") == [source["ledger"], source["session_id"]]
            and original.get("generation") == source["generation"],
            "supersede",
            "original closeout identity or generation changed",
        )
        require(closeout_mutations_settled(original), "supersede", "original closeout has an uncertain mutation")
    else:
        require(harness + "_closeout" not in operation.state["steps"], "supersede", "recorded closeout journal is missing")
    if original.get("retired_generation"):
        require(original["retired_generation"] == source["generation"], "supersede", "original retired generation changed")
        return evidence, source["generation"]
    require(harness == "codex", "supersede", "original smoke has not completed normal closeout")
    launch = operation.state["steps"]["codex_launch"]["result"]
    native = read_launch_receipt(Path(launch["native_receipt"])) if not launch.get("transcript") else {}
    entry = {**launch, **native, "session_id": launch["native_session_id"], "harness": "codex", "supervisor_owned": True,
             "tmux_session_id" if launch.get("backend") == "tmux" else "iterm_session_id": launch["session_id"]}
    identity = release.closure_identity(entry)
    require(entry["session_id"] == source["session_id"]
            and fingerprint({key: value for key, value in identity.items() if key != "handoff_url"}) == source["generation"],
            "supersede", "retained launch does not prove the original smoke generation")
    require(session_liveness.generation_ended(entry), "supersede", "original smoke process remains")
    with closure.ownership(source["session_id"], create=True):
        closed = closure.source_records({source["session_id"]}).get(source["session_id"])
        if not isinstance(closed, dict):
            raise GateFailed("supersede", "same native smoke has no canonical closure")
        current = release.closure_identity(closed)
        require(all(current[key] == identity[key] for key in ("harness", "supervisor_owned", "transcript", "codex_home")),
                "supersede", "closed source differs from the original native smoke")
        require(not original or closed.get("handoff_url") == original.get("handoff", {}).get("url"),
                "supersede", "closed source does not preserve the original smoke handoff")
        generation = fingerprint({key: value for key, value in current.items() if key != "handoff_url"})
    if original and generation == source["generation"]:
        try:
            finish = json.loads((evidence / "finish.json").read_text())
            result = json.loads(finish.get("stdout", "null"))
        except (OSError, ValueError, TypeError) as error:
            raise GateFailed("supersede", "original normal finish evidence is unavailable") from error
        require(finish.get("exit_code") == 0 and isinstance(result, dict) and result.get("ok") is True
                and result.get("needs_reconciliation") is False
                and [row.get("session_id") for row in result.get("reports", [])] == [source["session_id"]],
                "supersede", "original smoke has not completed normal finish")
        return evidence, generation
    matches = []
    for path in operation.evidence.glob(harness + "-closeout-*/operation.json"):
        journal = json.loads(path.read_text())
        if journal.get("identity") != [source["ledger"], source["session_id"]]:
            continue
        require(closeout_mutations_settled(journal),
                "supersede", "recovery closeout has an uncertain mutation")
        if journal.get("generation") == generation == journal.get("retired_generation"):
            matches.append(path.parent)
    require(len(matches) == 1, "supersede", "requires one exact retired recovery closeout journal")
    return matches[0], generation


def require_closed_smokes(config: dict, operation: Operation, result: dict) -> str | None:
    steps = operation.state["steps"]
    incomplete = {name for name, step in steps.items() if step.get("complete") is False}
    reported_url = steps.get("report_" + fingerprint(result), {}).get("result", {}).get("url")
    if incomplete:
        require(incomplete == {"report_" + fingerprint(result)},
                "supersede", "resolve every uncertain mutation other than this failed result's report")
    for harness in ("codex", "claude"):
        if not steps.get(harness + "_launch"):
            continue
        source = steps.get(harness + "_source")
        if not isinstance(source, dict):
            raise GateFailed("supersede", "original smoke source is missing")
        require(source["ledger"] == str(Path(config["ledger"]).resolve()), "supersede", "original ledger changed")
        close_evidence, generation = superseded_closeout(operation, harness, source)
        closed = Operation(close_evidence, [source["ledger"], source["session_id"]])
        require(closed.state.get("generation") == generation, "supersede", "exact smoke generation changed")
        try:
            validate_retired_closeout(argparse.Namespace(ledger=Path(source["ledger"]), session_id=source["session_id"],
                                                         supervisor_session=config["supervisor_session"]), closed,
                                     retain_terminal=closed.state.get("retired_generation") != generation)
        except ValueError as error:
            raise GateFailed("supersede", str(error)) from error
    return reported_url


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
        runtime = reconcile_catalog_runtime(source, discovery, repo=config["repo"])
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
                superseded_url = require_closed_smokes(current, old, old_result)
            superseded = str(old.evidence)
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
                skip_smoke = documentation_only(source, landings)
                bound, head = reconcile_runtime(current, operation, bind_landing=False)
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


def retryable_poll(evidence: Path, error: RetryableGate) -> dict:
    with closure.store_lock(evidence / "poll", create=True):
        cursor = evidence / "cursor.json"
        state = json.loads(cursor.read_text()) if cursor.exists() else {}
        if state.get("attempt"):
            attempt = evidence / state["attempt"]
            with closure.store_lock(attempt / "verification", create=True):
                for path in attempt.rglob("operation.json"):
                    journal = json.loads(path.read_text())
                    settled = (closeout_mutations_settled(journal) if journal.get("generation") else
                               not any(step.get("complete") is False for step in journal["steps"].values()))
                    require(settled, "reconciliation", "pending attempt has an uncertain mutation")
        return receipt((state.get("pending") or ["discovery"])[-1], evidence, error)


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
            try:
                result = poll(config, args.evidence, supersede_failed=args.supersede_failed)
            except GitHubReadError as error:
                if not (error.result.failure and error.result.failure.cause == "deadline_exceeded"
                        and error.result.retry_summary and error.result.retry_summary.exhausted_reason == "deadline_exceeded"):
                    raise
                result = retryable_poll(args.evidence, RetryableGate("identity", str(error)))
            except RetryableGate as error:
                result = retryable_poll(args.evidence, error)
            print(json.dumps(result, separators=(",", ":")))
            return int(not result["ok"] and not result.get("retryable"))
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
