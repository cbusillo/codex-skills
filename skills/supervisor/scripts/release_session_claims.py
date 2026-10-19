#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Record verified session closure and release its automation claims. Dry run by default."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
import fcntl
import os
from pathlib import Path
from typing import Any
import re
import shlex
import subprocess
import sys
import tempfile
import time

import session_record
import session_liveness
import account_choice
import backfill_inventory
from session_liveness import idle_terminal_command, inventory

GITHUB = Path(__file__).resolve().parents[2] / "github/scripts"
sys.path.insert(0, str(GITHUB))
import github_identity
import github_plan_claim as claim
import github_plan_release as release
import github_plan_closure as closure
from github_read import GitHubReader, GitHubReadError, GitHubReadShapeError, automation_only_gh_prefix_args
from github_comment import OPERATION_MARKER_LINE_RE, CommentError, comment

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
HANDOFF = release.COMMENT_URL


class PartialRelease(ValueError):
    def __init__(self, error, report):
        super().__init__(str(error))
        self.report = report


class ConfirmedIssueRefusal(ValueError):
    def __init__(self, payload):
        super().__init__(payload.get("error", "confirmed issue release refusal"))
        self.payload = payload


def verify_process_exit(entry, *, ended_at=None):
    if not entry.get("_inventory") and (
        type(entry.get("pid")) is not int or entry["pid"] <= 0
        or not isinstance(entry.get("tty"), str) or not entry["tty"]
    ):
        raise ValueError("closure requires the matched process PID and TTY")
    cutoff = release.stamp(entry["tty_exited_at"]) if entry.get("tty_exited_at") else ended_at
    if cutoff is not None and ended_at is not None and cutoff > ended_at:
        raise ValueError("native TTY exit cutoff is newer than closure")

    def independent(pid, argv):
        if closure.guarded_launch_endpoint(pid, argv) is None:
            return False
        # Retain the existing saved-generation identity check. A first or
        # resumed generation instead gets its cutoff from the native pane.
        records = closure.log_rows() if closure.closed_log().exists() else []
        source = next((row for row in records if row.get("session_id") == entry["session_id"]), None)
        if source is not None and not source.get("resumed_at") and source.get("process_exit_verified"):
            identity = lambda row: {key: value for key, value in session_record.normalize_ledger_entry(
                closure.closed_log(), row).items() if not key.startswith("_") and key != "handoff_url"}
            return identity(source) == identity(entry)
        return True

    return session_liveness.verify_process_exit(
        entry, rows=inventory(), ended_at=cutoff, started_at=closure.process_started_at,
        independent_harness=independent)


def verify_closed(entry, supervisor_session, *, ended_at):
    historical = entry.get("_inventory") is True
    if entry["session_id"] == supervisor_session or (
        not historical and entry.get("supervisor_owned") is not True
    ):
        raise ValueError("requires another explicitly Supervisor-owned session")
    if historical and (entry.get("safe_exit_yes") is not True or entry.get("still_running") is not False):
        raise ValueError("inventory closure requires the Supervisor's verified safe exit and inactivity")
    cutoff = release.stamp(entry["tty_exited_at"]) if entry.get("tty_exited_at") else ended_at
    if cutoff is not None and ended_at is not None and cutoff > ended_at:
        raise ValueError("native TTY exit cutoff is newer than closure")
    verify_process_exit(entry, ended_at=cutoff)
    closure.require_fresh_action_verdict(entry)
    status = session_record.session_status(entry)
    session_liveness.require_safe_status(status)
    return status


def validate_closure(entry, supervisor_session):
    if entry.get("resumed_at"):
        raise ValueError("closed generation has resumed; record the new launch generation")
    if entry.get("process_exit_verified") is not True:
        raise ValueError("closed-session log lacks verified process exit")
    if not isinstance(entry.get("ended_at"), str) or re.search(r"\s", entry["ended_at"]):
        raise ValueError("closure requires a single timezone-qualified ISO timestamp")
    ended = release.stamp(entry["ended_at"])
    if ended > datetime.now(timezone.utc):
        raise ValueError("closure timestamp is in the future")
    status = verify_closed(entry, supervisor_session, ended_at=ended)
    if not status["timestamp"] or release.stamp(status["timestamp"]) > ended:
        raise ValueError("transcript resumed after the recorded closure")
    if "verdict_at" in entry and entry["verdict_at"] != status["timestamp"]:
        raise ValueError("recorded verdict time differs from the verified transcript")
    paths = {Path(entry["transcript"]).resolve()}
    for root in entry.get("_transcript_roots", []):
        paths.update(p.resolve() for p in Path(root).rglob(f"*{entry['session_id']}*.jsonl"))
    for path in paths:
        other = session_record.session_status({**entry, "transcript": str(path)})
        records = session_record.read_jsonl(path)
        if not other["safe_verdict"] or any(
            release.stamp(r["timestamp"]) > ended for r in records if r.get("timestamp")
        ):
            raise ValueError("source resumed or has newer activity in a configured native transcript")
        if session_record.session_status({**entry, "transcript": str(path)}) != other:
            raise ValueError("native transcript changed during closure verification")
    # Optional host/boot/account fingerprints belong to idle admission only.
    # Ordinary release still revalidates exit and every required native copy.
    return status


def transcript_roots(harness, configured, explicit):
    if explicit:
        return [str(p.expanduser().resolve()) for p in explicit]
    config = account_choice.load_config(configured)
    roots = []
    for account in config["accounts"]:
        if account["provider"] == ("openai" if harness == "codex" else "anthropic"):
            variable = "CODEX_HOME" if harness == "codex" else "CLAUDE_CONFIG_DIR"
            home = Path(account["env"].get(variable, str(Path.home() / ".claude"))).expanduser()
            roots.extend([str(home / "sessions"), str(home / "archived_sessions")] if harness == "codex" else [str(home / "projects")])
    if not roots:
        raise ValueError("no configured source harness homes; supply --transcript-root for its known native transcript directories")
    return list(dict.fromkeys(roots))


def record_closure(path, entry, supervisor_session, *, persist=True):
    shared = path.resolve() == closure.closed_log().resolve()
    coordination = closure.ownership(entry["session_id"], create=persist) if shared else nullcontext()
    with coordination:
        closed = _record_closure(path, entry, supervisor_session, persist=persist)
        if shared and persist:
            closure.set_action_pending(entry["session_id"], False)
        return closed


def _record_closure(path, entry, supervisor_session, *, persist=True):
    """Observe exit now; never replace an older actual closure time on retry."""
    with closure.store_lock(path, create=persist):
        records = [{k: v for k, v in e.items() if not k.startswith("_")}
                   for e in session_record.load_ledger(path, closed_log=True)] if path.exists() else []
        existing = [r for r in records if r["session_id"] == entry["session_id"]]
        original = [r for r in json.loads(path.read_text()) if r.get("session_id") == entry["session_id"]] if path.exists() else []
    if existing and existing[0].get("resumed_at"):
        resumed = existing[0].get("resumed_generation") or {}
        if not resumed or any(entry.get(k) != resumed.get(k) for k in (
            "launch_pid", "launch_generation", "launch_started_at", "pid", "tty", "codex_home"
        )):
            raise ValueError("finish needs the new launch metadata from codex-session.py status")
        records = [r for r in records if r["session_id"] != entry["session_id"]]
        existing = []
    if existing:
        if any(existing[0].get(k) != entry.get(k) for k in (
            "pid", "tty", "harness", "handoff_url", "supervisor_owned"
        )) or Path(existing[0]["transcript"]).resolve() != Path(entry["transcript"]).resolve():
            raise ValueError("closure log identity changed; preserve the original record")
        closed = {**existing[0], **{k: v for k, v in entry.items() if k.startswith("_")}}
        validate_closure(closed, supervisor_session)
        with closure.store_lock(path):
            current = json.loads(path.read_text())
            if [r for r in current if r.get("session_id") == entry["session_id"]] != original:
                raise ValueError("source closure changed during verification; preserve the current generation")
        return closed
    # Only the native dead-pane observation can excuse a new TTY occupant on
    # first finish. Caller-provided closure/cutoff fields are not observations.
    entry = {k: v for k, v in entry.items() if k != "tty_exited_at"}
    cutoff = session_liveness.native_exit_cutoff(entry)
    if cutoff is not None:
        entry = {**entry, "tty_exited_at": cutoff.isoformat()}
    status = verify_closed(entry, supervisor_session, ended_at=cutoff)
    closed = {**entry, "ended_at": datetime.now(timezone.utc).isoformat(),
              "verdict_at": status["timestamp"], "process_exit_verified": True}
    if entry.get("harness") == "codex" and entry.get("codex_home"):
        try:
            closed["closure_evidence"] = closure.evidence(closed)
        except (ValueError, OSError, ImportError, subprocess.SubprocessError):
            # Optional admission proof must not stop the existing release route.
            closed["closure_proof_unavailable"] = True
    validate_closure(closed, supervisor_session)
    if not persist:
        return closed
    # Slow native/process/transcript verification owns only this source. The
    # shared store lock covers a fresh read/compare/write, preserving launches
    # and closures published for other sources while verification was running.
    with closure.store_lock(path, create=True):
        current = json.loads(path.read_text()) if path.exists() else []
        if [r for r in current if r.get("session_id") == entry["session_id"]] != original:
            raise ValueError("source closure changed during verification; preserve the current generation")
        retained = [r for r in current if r.get("session_id") != entry["session_id"]]
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump([*retained, {k: v for k, v in closed.items() if not k.startswith("_")}], stream)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return closed


def scan_start(entries):
    timestamps = [release.stamp(r["timestamp"]) for e in entries
                  for r in session_record.read_jsonl(Path(e["transcript"])) if r.get("timestamp")]
    if not timestamps:
        raise ValueError("transcripts have no timestamped history for discovery")
    return (min(timestamps) - timedelta(seconds=1)).isoformat()


def belongs_to_session(body, session):
    if session not in body:
        return False
    return any(r["session"] == session for r in claim.records(body)) or (
        session in body and claim.has_ownership_assertion(body)
        and release.related_sessions_match(body, session)
    )


def discover(reader, owner, entries, repos=None):
    """Complete paged comment inventory, including closed issues, since session start."""
    if repos is None:
        repositories = reader.paged_json("/installation/repositories", collection_key="repositories",
                                         step_prefix="claim_repositories")
        repos = [r["full_name"] for r in repositories
                 if r["full_name"].split("/")[0].casefold() == owner.casefold()]
    if not all(REPO.fullmatch(r) and r.split("/")[0].casefold() == owner.casefold() for r in repos):
        raise ValueError("repository scope must belong to the configured owner")
    if not repos:
        raise ValueError("repository inventory contains no repositories for this owner; verify scope and installation coverage")
    sessions = {e["session_id"] for e in entries}
    since = scan_start(entries)
    issues = set()
    for repo in repos:
        comments = reader.paged_json(f"/repos/{repo}/issues/comments", step_prefix="session_claim_inventory",
                                     params={"since": since}, required_query_params={"since": since})
        for c in comments:
            body = c.get("body") or ""
            if not any(s in body for s in sessions):
                continue
            try:
                matching = any(belongs_to_session(body, s) for s in sessions)
            except ValueError as error:
                raise ValueError(f"unreadable claim at {c.get('html_url')}: {error}") from error
            if matching:
                match = re.fullmatch(rf"https://api\.github\.com/repos/{re.escape(repo)}/issues/([1-9]\d*)", c["issue_url"])
                if not match:
                    raise ValueError("claim comment has an unexpected canonical issue URL")
                issues.add((repo, int(match[1])))
        statuses = reader.paged_json(f"/repos/{repo}/issues", step_prefix="session_status_inventory",
                                     params={"state": "all", "since": since}, required_query_params={"since": since})
        for issue in statuses:
            if "pull_request" not in issue and any(belongs_to_session(issue.get("body") or "", s) for s in sessions):
                issues.add((repo, issue["number"]))
    return sorted(issues), repos


def active_claims(reader, repo, number, session, actor):
    comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="session_claim_discussion")
    # Use the same evidence validity and ownership classifier as claim, rather
    # than deciding that any release-shaped text erases ownership.
    for c, receipt in release.receipts(comments):
        match = HANDOFF.fullmatch(receipt["evidence_url"])
        try:
            if not match:
                raise ValueError("invalid evidence URL")
            evidence = reader.get_json(f"/repos/{match[1]}/issues/comments/{match[3]}", step="release_evidence")
            if evidence.get("issue_url") != f"https://api.github.com/repos/{match[1]}/issues/{match[2]}":
                raise ValueError("evidence belongs to another issue")
            release.validate_evidence(receipt, evidence, (c.get("user") or {}).get("login", ""))
        except (ValueError, GitHubReadError) as error:
            if isinstance(error, GitHubReadError) and getattr(error.result, "status", None) not in {403, 404}:
                raise
            comments = [release.without_release(r) if r["id"] == c["id"] else r for r in comments]
    conflicts, _ = claim.discussion_evidence("", comments, {
        "session": "", "worker": "", "branch": "", "claimed_at": ""
    })
    active_ids = {c["id"] for c in conflicts if c.get("source") == "comment"}
    sources = []
    for c in comments:
        records = claim.records(c.get("body") or "")
        if (c["id"] in active_ids and not records
                and belongs_to_session(c.get("body") or "", session)
                and not any(r["session"] == session and re.match(
                    rf"Claimed by {re.escape(r['worker'])}(?:\s|$)", c.get("body") or "")
                    for source in comments if source["id"] in active_ids
                    for r in claim.records(source.get("body") or ""))):
            # The existing release route requires a structured source. Never
            # invent one or release an older hand-written source by worker alias.
            raise ValueError(f"hand-written claim {c['id']} needs its authored exact-ID release")
        if c["id"] in active_ids and any(r["session"] == session for r in records):
            if (c.get("user") or {}).get("login") != actor:
                raise ValueError("session has a claim held by another identity; report without releasing")
            record = release.source_record(c)
            related = [r["id"] for r in comments[comments.index(c) + 1:] if r["id"] in active_ids
                       and not claim.records(r.get("body") or "")
                       and re.match(rf"Claimed by {re.escape(record['worker'])}(?:\s|$)", r.get("body") or "")
                       and (r.get("user") or {}).get("login") == actor
                       and re.search(rf"(?m)^Session: {re.escape(session)}[ \t]*$", r.get("body") or "")]
            unresolved = [r["id"] for r in comments if r["id"] in active_ids and r["id"] not in related
                          and not claim.records(r.get("body") or "")
                          and re.match(rf"Claimed by {re.escape(record['worker'])}(?:\s|$)", r.get("body") or "")
                          and (r.get("user") or {}).get("login") == actor
                          and (belongs_to_session(r.get("body") or "", session)
                               or not re.search(r"(?im)^Session(?:[ _-]*ID)?:", r.get("body") or ""))]
            sources.append((c, related, unresolved))
    return sources


def handoff(reader, entry, actor):
    match = HANDOFF.fullmatch(entry.get("handoff_url") or "")
    if not match:
        raise ValueError("requires an exact transcript-verified issue handoff URL")
    evidence = reader.get_json(f"/repos/{match[1]}/issues/comments/{match[3]}", step="session_handoff")
    if ((evidence.get("user") or {}).get("login") != actor
            or evidence.get("issue_url") != f"https://api.github.com/repos/{match[1]}/issues/{match[2]}"):
        raise ValueError("handoff identity or canonical issue differs")
    return evidence


def discover_handoff(reader, entry, actor):
    """Use native transcript links first; never attribute a shared bot by recency alone."""
    records = session_record.read_jsonl(Path(entry["transcript"]))
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)
    # Shared-bot comments merely read by the source are not its handoff.
    outputs = []
    writes = set()
    # Heredoc bodies are data (often a comment body with apostrophes), not commands.
    heredoc = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", re.S)
    def shell_text(command):
        if isinstance(command, list) and all(isinstance(part, str) for part in command):
            command = command[-1] if len(command) >= 3 and command[-2] in {"-c", "-lc"} and Path(command[0]).name in {"bash", "sh", "zsh"} else shlex.join(command)
        return command if isinstance(command, str) else ""
    def shell_parts(command):
        bodies = {}
        def replace(match):
            key = f"__handoff_stdin_{len(bodies)}__"
            bodies[key] = match[4]
            if match.start() and command[match.start() - 1].isdigit():
                return f" __handoff_fd__ {key}{match[3]}\n"
            return f" < {key}{match[3]}\n"
        header = heredoc.sub(replace, command)
        # Explicit file descriptors are not the helper's default stdin/stdout.
        header = re.sub(r"(?<!\w)\d+(>>?|<)", " __handoff_fd__ ", header)
        # Multiple/unsupported heredoc syntax is not evidence of bytes sent.
        if "<<" in header:
            return [], {}
        try:
            lexer = shlex.shlex(header, posix=True, punctuation_chars=";&|\n<>")
            lexer.whitespace_split = True
            lexer.whitespace = " \t\r"
            parts, tokens = [], []
            for token in lexer:
                if token and all(c in ";&|\n" for c in token):
                    if tokens:
                        parts.append(tokens)
                        tokens = []
                else:
                    tokens.append(token)
            if tokens:
                parts.append(tokens)
            return parts, bodies
        except ValueError:
            return [], {}
    def is_comment_write(tokens):
        for index, token in enumerate(tokens):
            name = Path(token).name
            if name in {"uv", "run", "python", "python3", "env"} or token.startswith("-") or re.match(r"\w+=", token):
                continue
            tail = tokens[index + 1:]
            if name in {"gh-comment", "github_comment.py"}:
                if any(t in {"issue", "pr"} and i + 1 < len(tail) and tail[i + 1].isdigit() for i, t in enumerate(tail)):
                    return True
            if name == "gh-pr.py":
                # The documented form puts the global --repo before the subcommand.
                while tail[:1] in (["--repo"], ["-R"]) or (tail and tail[0].startswith("--repo=")):
                    tail = tail[1 if "=" in tail[0] else 2:]
                if tail[:1] == ["comment"]:
                    return True
            return False
        return False
    # A write's output is often summarized or chained with other commands, so any
    # URL it prints is a candidate. Only a comment whose body is exactly what that
    # call sent binds: a stdin heredoc, or the --body-file the session wrote itself.
    files = {}
    session_cwd = next((r.get("payload", {}).get("cwd") for r in records if r.get("type") == "session_meta"), None)
    session_cwd = session_cwd or next((r.get("cwd") for r in records if r.get("cwd")), None)
    def flat(text):
        return " ".join(text.split())
    def file_identity(path, cwd):
        # Never consult the live host: the transcript is the filesystem evidence.
        if not path or any(c in path for c in "$`~*?["):
            return None
        if not os.path.isabs(path):
            if not cwd or not os.path.isabs(cwd):
                return None
            path = os.path.join(cwd, path)
        return os.path.normpath(path)
    def note_file_writes(tool_input, cwd):
        if not isinstance(tool_input, dict) or not isinstance(tool_input.get("file_path"), str):
            return
        name = file_identity(tool_input["file_path"], cwd)
        if name is None:
            return
        if isinstance(tool_input.get("content"), str):
            files[name] = tool_input["content"]
        for edit in [tool_input, *(tool_input.get("edits") or [])]:
            if name in files and isinstance(edit, dict) and isinstance(edit.get("old_string"), str) and isinstance(edit.get("new_string"), str):
                files[name] = files[name].replace(edit["old_string"], edit["new_string"], -1 if edit.get("replace_all") else 1)
    def sent_bodies(command, cwd):
        command = shell_text(command)
        parts, heredocs = shell_parts(command)
        bodies, variables = set(), {}
        # shlex removes quotes. Do not expand a dollar that was single-quoted.
        expand = not re.search(r"'[^']*\$[^']*'", command)
        def literal(value, values):
            if expand:
                value = re.sub(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))",
                               lambda m: values.get(m[1] or m[2], m[0]), value)
            return value if not any(c in value for c in "$`~*?[") else None
        for tokens in parts:
            local = dict(variables)
            index = 0
            while index < len(tokens) and re.match(r"[A-Za-z_]\w*=", tokens[index]):
                name, value = tokens[index].split("=", 1)
                local[name] = literal(value, local) or "$unknown"
                index += 1
            tokens = tokens[index:]
            if not tokens:
                variables = local
                continue
            # Shell cwd changes cannot be inferred from an attempted cd alone.
            if tokens[0] in {"cd", "pushd", "popd"}:
                cwd = None
            def operand(flag):
                value = None
                for i, token in enumerate(tokens):
                    if token == flag and i + 1 < len(tokens):
                        value = literal(tokens[i + 1], variables)
                    if flag.startswith("--") and token.startswith(flag + "="):
                        value = literal(token.split("=", 1)[1], variables)
                return value
            stdin = operand("<")
            target = file_identity(operand(">") or operand(">>"), cwd)
            if target:
                files.pop(target, None)
                # Only cat with stdin alone copies the heredoc bytes unchanged.
                args, i = [], 1
                while i < len(tokens):
                    if tokens[i] in {"<", ">", ">>"}:
                        i += 2
                    else:
                        args.append(tokens[i])
                        i += 1
                if tokens[0] == "cat" and not args and stdin in heredocs and ">>" not in tokens:
                    files[target] = heredocs[stdin]
            if not is_comment_write(tokens):
                continue
            path = operand("--body-file")
            if path is None and any(t == "--body-file" or t.startswith("--body-file=") for t in tokens):
                continue
            if path == "-" or path is None:
                body = heredocs.get(stdin)
                if body is None:
                    body = files.get(file_identity(stdin, cwd))
            else:
                body = files.get(file_identity(path, cwd))
            if body and flat(body):
                bodies.add(flat(body))
        return bodies
    started = {}
    sent = {}
    printed = []
    def add_printed(call_id, finished_at, value):
        for repo, number, comment_id in re.findall(HANDOFF.pattern, "\n".join(strings(value))):
            printed.append((f"https://github.com/{repo}/issues/{number}#issuecomment-{comment_id}",
                            started.get(call_id), finished_at, sent.get(call_id, set())))
    def start_write(call_id, timestamp, commands):
        if call_id in sent:
            return
        sent[call_id] = set()
        for command, cwd in commands:
            sent[call_id].update(sent_bodies(command, cwd))
            if any(is_comment_write(tokens) for tokens in shell_parts(shell_text(command))[0]):
                writes.add(call_id)
                started[call_id] = started.get(call_id) or timestamp
    for record in records:
        payload = record.get("payload") or {}
        arguments = payload.get("arguments") or payload.get("input") or ""
        try:
            structured = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError:
            structured = None
        command = (structured.get("cmd") or structured.get("command") or "") if isinstance(structured, dict) else ""
        code = structured.get("code", "") if isinstance(structured, dict) else arguments
        cwd = (structured.get("workdir") or structured.get("cwd") or session_cwd) if isinstance(structured, dict) else session_cwd
        commands = [(command, cwd)] if command else []
        if not command and isinstance(code, str):
            # Decode each invocation separately so its workdir is not lost when
            # one orchestration call contains several exec_command invocations.
            objects = re.findall(r'\btools\.exec_command\(\s*\{((?:"(?:\\.|[^"\\])*"|[^{}"])*)\}', code)
            for obj in objects:
                try:
                    values = {key: json.loads(value) for key, value in re.findall(
                        r'\b(cmd|workdir|cwd)\s*:\s*("(?:\\.|[^"\\])*")', obj)}
                except ValueError:
                    continue  # malformed historic tool text is not a write receipt
                if values.get("cmd"):
                    commands.append((values["cmd"], values.get("workdir") or values.get("cwd") or cwd))
        # Historical fixtures/native direct calls may contain the shell command itself.
        if not command and not isinstance(structured, dict) and payload.get("name") in {"exec_command", "functions.exec_command"}:
            commands = [(str(arguments), cwd)]
        if payload.get("type") in {"function_call", "custom_tool_call"}:
            # Use the decoded command, not the orchestration source with its escapes.
            if payload.get("call_id"):
                start_write(payload["call_id"], record.get("timestamp"), commands)
        if payload.get("type") in {"function_call_output", "custom_tool_call_output"} and payload.get("call_id") in writes:
            outputs.extend(strings(payload.get("output")))
            add_printed(payload["call_id"], record.get("timestamp"), payload.get("output"))
        for block in (record.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_input = block.get("input") or {}
                cwd = (tool_input.get("workdir") or tool_input.get("cwd") or record.get("cwd") or session_cwd) if isinstance(tool_input, dict) else session_cwd
                note_file_writes(tool_input, cwd)
                command = (tool_input.get("command") or tool_input.get("cmd") or "") if isinstance(tool_input, dict) else ""
                if block.get("id") and command:
                    start_write(block["id"], record.get("timestamp"), [(command, cwd)])
            if block.get("type") == "tool_result" and block.get("tool_use_id") in writes and block.get("is_error") is not True:
                outputs.extend(strings(block.get("content")))
                add_printed(block["tool_use_id"], record.get("timestamp"), block.get("content"))
    receipt_urls = []
    decoder = json.JSONDecoder()
    for text in outputs:
        # Consume complete envelopes, not URLs embedded in a subsequent read's body.
        cursor = 0
        while (start := text.find("{", cursor)) >= 0:
            try:
                value, length = decoder.raw_decode(text[start:])
                cursor = start + length
            except ValueError:
                cursor = start + 1
                continue
            if isinstance(value, dict) and value.get("comment_action") in {"created", "updated"}:
                url = (value.get("comment") or {}).get("url")
                if isinstance(url, str) and HANDOFF.fullmatch(url):
                    receipt_urls.append(url)
    final_text = session_record.session_status(entry)["last_text"]
    final_urls = [f"https://github.com/{repo}/issues/{number}#issuecomment-{comment_id}"
                  for repo, number, comment_id in re.findall(HANDOFF.pattern, final_text)]
    candidates = [*entry.get("_handoffs", []),
                  *reversed(receipt_urls), *(url for url, _, _, _ in reversed(printed)), *reversed(final_urls)]
    for url in dict.fromkeys(candidates):
        match = HANDOFF.fullmatch(url)
        if not match:
            continue
        try:
            evidence = handoff(reader, {**entry, "handoff_url": url}, actor)
        except ValueError:
            continue
        except GitHubReadError as error:
            if getattr(error.result, "status", None) in {403, 404}:
                continue
            raise
        text = evidence.get("body") or ""
        # GitHub stamps whole seconds; allow that truncation plus small clock skew.
        created = release.stamp(evidence.get("created_at") or "1970-01-01T00:00:00Z")
        slack = timedelta(seconds=2)
        body = flat(OPERATION_MARKER_LINE_RE.sub("", text))
        sent_here = release.related_sessions_match(text, entry["session_id"]) and any(
            u == url and body in bodies and (not start or not end
                                             or release.stamp(start) - slack <= created <= release.stamp(end) + slack)
            for u, start, end, bodies in printed)
        if (release.stamp(evidence.get("updated_at") or evidence.get("created_at")) <= release.stamp(entry["ended_at"])
                and not text.startswith("Claimed by ") and not claim.records(text)
                and (url in entry.get("_handoffs", []) or url in receipt_urls or sent_here or entry["session_id"] in text)):
            provenance = ("Supervisor verified inventory handoff" if url in entry.get("_handoffs", [])
                          else "native comment-write receipt" if url in receipt_urls
                          else "native comment-write body" if sent_here else "exact-ID handoff")
            return {**entry, "handoff_url": url, "_handoff_provenance": provenance}, evidence
    # A summary without a transcript URL must name the exact native ID.
    threads = {(r["repo"], r["issue"]) for r in entry.get("_inventory_claims", [])}
    if entry.get("_handoff_thread"):
        threads.add(entry["_handoff_thread"])
    for repo, number in sorted(threads):
        cache = entry.get("_handoff_cache", {})
        if (repo, number) not in cache:
            try:
                cache[(repo, number)] = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="backfill_handoff")
            except GitHubReadError as error:
                if getattr(error.result, "status", None) in {403, 404}:
                    continue
                raise
        comments = cache[(repo, number)]
        for evidence in reversed(comments):
            text = evidence.get("body") or ""
            if ((evidence.get("user") or {}).get("login") == actor and re.search(rf"(?im)^(?:Native[ _-]*)?Session(?:[ _-]*ID)?[ \t]*[:=][ \t]*{re.escape(entry["session_id"])}[ \t]*$", text)
                    and release.related_sessions_match(text, entry["session_id"])
                    and not text.startswith("Claimed by ") and not claim.records(text)
                    and re.search(r"(?im)^(?:\*\*)?(?:Safe to exit: yes|Ready for the merge train|Engineering ready|(?:Engineering )?handoff\b|handed off\b|engineering complete\b|completed\b)", text)
                    and release.stamp(evidence.get("updated_at") or evidence.get("created_at")) <= release.stamp(entry["ended_at"])):
                url = evidence["html_url"]
                checked = handoff(reader, {**entry, "handoff_url": url}, actor)
                return {**entry, "handoff_url": url, "_handoff_provenance": "exact-ID summary"}, checked
    # Interrupted sessions may have no public closeout. The inventory's original
    # native ownership record is a recovery context, not a fabricated summary.
    for row in entry.get("_inventory_claims", []):
        comments = entry.get("_handoff_cache", {}).get((row["repo"], row["issue"]))
        if comments is None:
            comments = reader.paged_json(f"/repos/{row['repo']}/issues/{row['issue']}/comments", step_prefix="backfill_source_context")
        source = next((c for c in comments if c.get("id") == row["claim_comment_id"]), None)
        if source is None or (source.get("user") or {}).get("login") != actor:
            continue
        parsed = claim.records(source.get("body") or "")
        if (len(parsed) == 1 and parsed[0]["session"] == entry["session_id"]
                and release.stamp(source.get("updated_at") or source.get("created_at")) <= release.stamp(entry["ended_at"])):
            url = source.get("html_url") or f"https://github.com/{row['repo']}/issues/{row['issue']}#issuecomment-{source['id']}"
            checked = handoff(reader, {**entry, "handoff_url": url}, actor)
            return {**entry, "handoff_url": url, "_handoff_provenance": "original native ownership record; closeout verified separately"}, checked
    raise ValueError("no authored handoff bound to the exact native transcript/session; reconcile this session")


def run_release(repo, number, source_id, related, prs, evidence_url, supervisor_session, checkout_root, dry_run):
    checkout = checkout_root / repo.split("/")[1]
    argv = ["uv", "run", str(GITHUB / "gh-plan.py"), "--repo", repo, "release-claim", str(number),
            "--claim-comment", str(source_id), "--evidence-comment", evidence_url,
            "--role", "supervisor", "--session", supervisor_session, "--confirm-session-ended"]
    for value in related:
        argv += ["--related-claim-comment", str(value)]
    for value in prs:
        argv += ["--retained-pr", value]
    if dry_run:
        argv.append("--dry-run")
    result = subprocess.run(argv, cwd=checkout, capture_output=True, text=True, timeout=180)
    payload = json.loads(result.stdout)
    if result.returncode or payload.get("ok") is not True or payload.get("outcome_certainty") not in {"confirmed", "reconciled_applied"}:
        if dry_run and payload.get("release_refusal_scope") == release.ISSUE_REFUSAL_SCOPE and "post_release" not in payload.get("completed_steps", []):
            raise ConfirmedIssueRefusal(payload)
        raise ValueError(f"release-claim did not confirm success; preserve records: {json.dumps(payload)}")
    return payload


def release_session(reader, entry, actor, supervisor_session, issues, checkout_root, apply):
    reader.deadline_at = time.time() + entry.get("_read_deadline_seconds", 600)
    status = validate_closure(entry, supervisor_session)
    if entry.get("_finish"):
        try:
            completed = finished_claims(reader, entry, actor, issues, apply)
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
                GitHubReadError, GitHubReadShapeError):
            completed = None  # Preserve the existing per-issue refusal/release path.
        if completed is not None:
            if validate_closure(entry, supervisor_session) != status:
                raise ValueError("source transcript changed; preserve claims")
            return completed
        reader.deadline_at = time.time() + entry.get("_read_deadline_seconds", 600)
    if entry.get("handoff_url"):
        handoff_record = handoff(reader, entry, actor)
    else:
        entry, handoff_record = discover_handoff(reader, entry, actor)
    plans = []
    retained_status = []
    entry["_releaser_session"] = supervisor_session
    entry["_pr_claim_handoffs"] = {}
    pulls_by_repo = {}
    handoff_prs = set(re.findall(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9]\d*", handoff_record.get("body") or ""))
    refused = []
    for repo, number in issues:
        if entry.get("_inventory") and (repo, number) not in {(r["repo"], r["issue"]) for r in entry["_inventory_claims"]}:
            continue
        try:
            issue_plans, status_claims = plan_issue(reader, repo, number, entry, actor, handoff_prs, pulls_by_repo, checkout_root)
            plans.extend(issue_plans)
            retained_status.extend(status_claims)
        except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, GitHubReadError, GitHubReadShapeError) as error:
            refused.append({"repository": repo, "issue": number, "error": str(error)})
    report = {"session_id": entry["session_id"], "claims": [], "dry_run": not apply,
              "retained_status_claims": retained_status, "issue_refusals": refused,
              "inventory_claims": list(entry.get("_inventory_results", {}).values()),
              "handoff_url": entry["handoff_url"],
              "pr_claim_handoffs": list(entry.get("_pr_claim_handoffs", {}).values()),
              "closure_evidence": "Supervisor verified inventory; original PID/TTY unavailable" if entry.get("_inventory") else "matched PID/TTY log"}
    try:
        return apply_plans(entry, supervisor_session, plans, status, checkout_root, actor, apply, report, reader)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, CommentError) as error:
        raise PartialRelease(error, report) from error


def finished_claims(reader, entry, actor, issues, apply):
    """Recognize completed ownership from original claims, including released ones.

    Only finish's complete discovery can establish that a session never claimed.
    Anything without an exact authored release retains the existing handoff path.
    """
    released = []
    ended = release.stamp(entry["ended_at"])
    for repo, number in issues:
        reader.deadline_at = time.time() + entry.get("_read_deadline_seconds", 600)
        issue = reader.get_json(f"/repos/{repo}/issues/{number}", step="finished_claim_issue")
        comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="finished_claim_history")
        sources = [c for c in comments if belongs_to_session(c.get("body") or "", entry["session_id"])
                   and (claim.records(c.get("body") or "") or re.match(
                       r"Claimed by:?\s+\S+", claim.ownership_text(c.get("body") or "", strip_quotes=False), re.IGNORECASE))]
        if belongs_to_session(issue.get("body") or "", entry["session_id"]):
            body_records = [r for r in claim.records(issue["body"]) if r["session"] == entry["session_id"]]
            source_records = [r for c in sources for r in claim.records(c.get("body") or "")]
            if not body_records or any(r not in source_records for r in body_records):
                return None  # Status-only ownership is not a read-only session.
        exact = {comments[i]["id"]: comments[j] for i, record, j in claim.exact_released_sources(comments)
                 if record["session"] == entry["session_id"]}
        for source in sources:
            parsed = claim.records(source.get("body") or "")
            evidence = exact.get(source["id"])
            if (len(parsed) != 1 or parsed[0]["session"] != entry["session_id"]
                    or (source.get("user") or {}).get("login") != actor or evidence is None
                    or release.receipt_for(evidence) is not None
                    or not release.related_sessions_match(evidence.get("body") or "", entry["session_id"])
                    or release.stamp(evidence.get("updated_at") or evidence.get("created_at")) > ended):
                return None
            released.append({"repository": repo, "issue": number, "claim_comment": source["id"],
                             "handoff_url": f"https://github.com/{repo}/issues/{number}#issuecomment-{evidence['id']}"})
        # Keep the existing classifier's refusal for unresolved legacy follow-ups
        # and claims held by another identity, even alongside a released source.
        if active_claims(reader, repo, number, entry["session_id"], actor):
            return None
    scope = entry["_finish_scope"]
    result_status = "already released by the session" if released else "nothing to release"
    if scope == "explicit scope":
        result_status += " in explicit scope"
    return {"session_id": entry["session_id"], "claims": [], "released_claims": released,
            "status": result_status, "coverage": scope,
            "handoff_url": released[-1]["handoff_url"] if released else None,
            "dry_run": not apply, "retained_status_claims": [], "issue_refusals": [],
            "pr_claim_handoffs": [], "closure_evidence": "matched PID/TTY log"}


def verified_release_checkout(checkout_root, repo):
    checkout = checkout_root / repo.split("/")[1]
    remote = claim.run_read(["git", "remote", "get-url", "origin"], cwd=checkout).strip()
    if not re.search(rf"[:/]{re.escape(repo)}(?:\.git)?$", remote, re.IGNORECASE):
        raise ValueError("release checkout must match the canonical repository")
    return checkout


def plan_issue(reader, repo, number, entry, actor, handoff_prs: set[str], pulls_by_repo, checkout_root):
    plans, retained_status = [], []
    reader.deadline_at = time.time() + entry.get("_read_deadline_seconds", 600)
    issue = reader.get_json(f"/repos/{repo}/issues/{number}", step="release_canonical_issue")
    if "pull_request" in issue:
        sources = active_claims(reader, repo, number, entry["session_id"], actor)
        comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="pr_inventory_source")
        active_ids = {identifier for source, related, unresolved in sources for identifier in [source["id"], *related, *unresolved]}
        for row in entry.get("_inventory_claims", []):
            if row["repo"] != repo or row["issue"] != number:
                continue
            expected = next((c for c in comments if c["id"] == row["claim_comment_id"]), None)
            if expected is None or (expected.get("user") or {}).get("login") != actor or not belongs_to_session(expected.get("body") or "", entry["session_id"]):
                raise ValueError("inventory PR source is missing or lacks exact native-session/author evidence")
            entry.setdefault("_inventory_results", {})[(repo, number, row["claim_comment_id"])] = {
                "repository": repo, "issue": number, "claim_comment": row["claim_comment_id"],
                "status": "manual_release_required" if row["claim_comment_id"] in active_ids else "already_released"}
        checkout = verified_release_checkout(checkout_root, repo) if sources else None
        rows = {}
        for source, related, unresolved in sources:
            if entry.get("_inventory") and not any(row["repo"] == repo and row["issue"] == number
                    and row["claim_comment_id"] == source["id"] for row in entry["_inventory_claims"]):
                raise ValueError("inventory PR source needs its structured claim selected explicitly")
            if unresolved:
                raise ValueError("PR ownership follow-ups need verified exact-ID releases")
            argv = ["uv", "run", str(GITHUB / "gh-plan.py"), "--repo", repo,
                    "release-claim", f"https://github.com/{repo}/pull/{number}",
                    "--claim-comment", str(source["id"]),
                    "--evidence-comment", "<verified-closed-session-attestation-url>",
                    "--role", "supervisor", "--session", entry.get("_releaser_session", "<supervisor-native-session>"),
                    "--confirm-session-ended"]
            for related_id in related:
                argv.extend(["--related-claim-comment", str(related_id)])
            row = {"repository": repo, "pull_request": number,
                   "claim_comments": [source["id"], *related],
                   "status": "manual_release_required", "release_argv": argv,
                   "dry_run_argv": [*argv, "--dry-run"], "checkout": str(checkout),
                   "attestation_body": closure_attestation(entry)}
            rows[(repo, number, source["id"])] = row
        entry.setdefault("_pr_claim_handoffs", {}).update(rows)
        return [], retained_status
    comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="session_issue_filter")
    if not any(belongs_to_session(c.get("body") or "", entry["session_id"]) for c in comments) and not belongs_to_session(issue.get("body") or "", entry["session_id"]):
        if entry.get("_inventory"):
            raise ValueError("inventory source is missing or lacks exact native-session evidence")
        return [], retained_status
    if belongs_to_session(issue.get("body") or "", entry["session_id"]):
        retained_status.append({"repository": repo, "issue": number,
                                "next_action": "reconcile Current Status or use the supported --resume-from handoff route"})
    sources = active_claims(reader, repo, number, entry["session_id"], actor)
    active_ids = {identifier for source, related, unresolved in sources for identifier in [source["id"], *related, *unresolved]}
    for row in entry.get("_inventory_claims", []):
        if row["repo"] != repo or row["issue"] != number:
            continue
        expected = next((c for c in comments if c["id"] == row["claim_comment_id"]), None)
        if expected is None or (expected.get("user") or {}).get("login") != actor or not belongs_to_session(expected.get("body") or "", entry["session_id"]):
            raise ValueError("inventory source is missing or lacks exact native-session/author evidence")
        entry.setdefault("_inventory_results", {})[(repo, number, row["claim_comment_id"])] = {
            "repository": repo, "issue": number, "claim_comment": row["claim_comment_id"],
            "status": "active" if row["claim_comment_id"] in active_ids else "already_released"}
    for source, related, unresolved in sources:
        if entry.get("_inventory") and not any(r["repo"] == repo and r["issue"] == number
                and r["claim_comment_id"] == source["id"] for r in entry["_inventory_claims"]):
            continue
        record = release.source_record(source)
        if repo not in pulls_by_repo:
            pulls_by_repo[repo] = reader.paged_json(f"/repos/{repo}/pulls", params={"state": "open"}, step_prefix="retained_session_prs")
        pulls = list(pulls_by_repo[repo])
        mentioned: set[str] = handoff_prs | ({record["refresh_pr"]} if record.get("refresh_pr") else set())
        inventory_prs = {r["retained_pr_url"] for r in entry.get("_inventory_claims", [])
                         if r["repo"] == repo and r["issue"] == number and r.get("retained_pr_url")}
        if any(not re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/([1-9]\d*)", url) for url in inventory_prs):
            raise ValueError("inventory retained PR must be an exact same-repository URL")
        mentioned.update(inventory_prs)
        for url in sorted(mentioned):
            match = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/([1-9]\d*)", url)
            if match and not any(p["html_url"] == url for p in pulls):
                pulls.append(reader.get_json(f"/repos/{repo}/pulls/{match[1]}", step="handoff_retained_pr"))
        prs = [p["html_url"] for p in pulls
               if p["html_url"] in inventory_prs or (p.get("head") or {}).get("ref") == record["branch"]
               or (p["html_url"] in mentioned and (p.get("user") or {}).get("login") == actor
                   and (p.get("state") == "open" or p.get("merged_at"))
                   and claim.pr_issue_reference(p, number, repo=repo, attestation=True))]
        plans.append((repo, number, source["id"], related, prs))
        if unresolved:
            raise ValueError(f"ownership follow-ups {unresolved} lack a verified source/session relationship; obtain authored exact-ID releases")
    if not plans and retained_status and not any(claim.records(c.get("body") or "") for c in comments):
        raise ValueError("status-only ownership has no structured source; reconcile its authored ownership record")
    covered = {identifier for _, _, source_id, related, _ in plans for identifier in [source_id, *related]}
    if any(r["repo"] == repo and r["issue"] == number and r["claim_comment_id"] in active_ids
           and r["claim_comment_id"] not in covered for r in entry.get("_inventory_claims", [])):
        raise ValueError("active inventory follow-up has no selected structured source; obtain its authored exact-ID release")
    if plans:
        verified_release_checkout(checkout_root, repo)
    return plans, retained_status


def preview_guards(entry, *, reader, repo, number, source_id, related, prs, body, actor, supervisor_session, checkout_root):
    comments = reader.paged_json(f"/repos/{repo}/issues/{number}/comments", step_prefix="preview_source_activity")
    source = next(c for c in comments if c["id"] == source_id)
    related_comments = [c for c in comments if c["id"] in related]
    if len(related_comments) != len(related):
        raise ValueError("related ownership changed during preview")
    local = claim.local_inventory(repo, number, cwd=checkout_root / repo.split("/")[1])
    now = datetime.now(timezone.utc).isoformat()
    prospective = {"body": body, "user": {"login": actor}, "created_at": now, "updated_at": now}
    branches = set()
    issue_refusal = None
    for url in prs:
        match = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/([1-9]\d*)", url)
        if not match:
            raise ValueError("retained PR is not a same-repository canonical URL")
        pull = reader.get_json(f"/repos/{repo}/pulls/{match[1]}", step="preview_retained_pr")
        commit = reader.get_json(f"/repos/{repo}/commits/{pull['head']['sha']}", step="preview_retained_head")
        try:
            release.check_retained_pr(pull, commit, repo=repo, number=number, actor=actor, ended=entry["ended_at"])
        except release.IssueReleaseRefusal as error:
            issue_refusal = error
        branches.add(pull["head"]["ref"])
    # This prospective attestation is never posted or returned as release evidence.
    release.prepare_release(source, prospective, comments, local, actor=actor,
        role="supervisor", releaser_session=supervisor_session, evidence_url="unposted preview",
        retained_prs=prs, related=related_comments, checked_prs=prs, retained_branches=branches)
    if issue_refusal is not None:
        raise issue_refusal
    return {"status": "passed_read_only", "attestation_posted": False,
            "requires_command_preflight_after_posting": True, "session_coverage": local.get("session_coverage")}


def closure_attestation(entry):
    body = (f"Closed session {entry['session_id']}\nEnded at: {entry['ended_at']}\nSafe to exit: yes\n\n"
            f"Handoff context ({entry.get('_handoff_provenance', 'Supervisor transcript-verified ledger')}): {entry['handoff_url']}\n")
    if entry.get("_inventory"):
        body += "Closure evidence: Supervisor verified inventory; original PID/TTY unavailable. The caller confirms actual session inactivity, including unavailable native peers.\n"
    return body


def apply_plans(entry, supervisor_session, plans, status, checkout_root, actor, apply, report, reader):
    if apply:
        # Session-wide refusals must be known before the first claim is released.
        preflight = {"claims": [], "issue_refusals": []}
        apply_plans(entry, supervisor_session, plans, status, checkout_root, actor, False, preflight, reader)
        refused = {(item["repository"], item["issue"], item["claim_comment"])
                   for item in preflight["claims"] if item["release_preflight"].get("status") == "refused"}
        report["issue_refusals"].extend(preflight["issue_refusals"])
        report["claims"].extend(item for item in preflight["claims"]
                                if (item["repository"], item["issue"], item["claim_comment"]) in refused)
        plans = [plan for plan in plans if plan[:3] not in refused]
    for repo, number, source_id, related, prs in plans:
        if validate_closure(entry, supervisor_session) != status:
            raise ValueError("source transcript changed; preserve remaining claims")
        body = closure_attestation(entry)
        item = {"repository": repo, "issue": number, "claim_comment": source_id,
                "related_claim_comments": related,
                "retained_prs": prs, "attestation_body": body,
                "release_preflight": "requires a posted attestation; apply previews release-claim before releasing"}
        report["claims"].append(item)
        if not apply:
            try:
                item["release_preflight"] = preview_guards(entry, reader=reader, repo=repo,
                    number=number, source_id=source_id, related=related, prs=prs, body=body,
                    actor=actor, supervisor_session=supervisor_session, checkout_root=checkout_root)
            except release.IssueReleaseRefusal as error:
                item["release_preflight"] = {"status": "refused", "error": str(error)}
                report["issue_refusals"].append({"repository": repo, "issue": number, "error": str(error)})
            except (ValueError, KeyError, TypeError, StopIteration, OSError, GitHubReadError, GitHubReadShapeError) as error:
                item["release_preflight"] = {"status": "refused", "error": str(error)}
                raise ValueError(str(error)) from error
        if apply:
            result = comment("issue", number, body, repo=repo, expected_actor=actor,
                             operation="github.comment.issue", dedupe_body=True)
            url = result["comment"]["url"]
            item["evidence_url"] = url
            if validate_closure(entry, supervisor_session) != status:
                raise ValueError("source transcript changed after attestation; preserve claims")
            try:
                preview = run_release(repo, number, source_id, related, prs, url, supervisor_session, checkout_root, True)
            except ConfirmedIssueRefusal as error:
                if validate_closure(entry, supervisor_session) != status:
                    raise ValueError("source activity changed after issue refusal; preserve remaining claims") from error
                item["release_refusal"] = error.payload
                report["issue_refusals"].append({"repository": repo, "issue": number, "error": str(error)})
                continue
            item["release_body"] = preview["release_body"]
            if validate_closure(entry, supervisor_session) != status:
                raise ValueError("source transcript changed after preview; preserve claims")
            item["release"] = run_release(repo, number, source_id, related, prs, url, supervisor_session, checkout_root, False)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("finish", "backfill"))
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--closed-log", type=Path,
                        help="shared host-local closure log; an explicit private log is not successor evidence")
    parser.add_argument("--session-id")
    parser.add_argument("--supervisor-session", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", action="append", help="scope disposable trials; omission discovers all owner repositories")
    parser.add_argument("--checkout-root", type=Path, required=True)
    parser.add_argument("--confirm-session-ended", action="store_true")
    parser.add_argument("--verified-handoff", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--deadline-seconds", type=int, default=600)
    parser.add_argument("--account-config", type=Path)
    parser.add_argument("--handoff-thread", help="fallback summary thread OWNER/REPO#NUMBER; default OWNER/codex-skills#884")
    parser.add_argument("--transcript-root", type=Path, action="append", default=[])
    args = parser.parse_args()
    reports = []
    lock_stream = None
    try:
        if args.closed_log is None:
            if args.mode != "finish":
                raise ValueError("backfill requires an explicit --closed-log")
            args.closed_log = closure.closed_log()
        if args.apply:
            args.closed_log.parent.mkdir(parents=True, exist_ok=True)
            lock_stream = os.fdopen(os.open(str(args.closed_log) + ".release.lock", os.O_CREAT | os.O_RDWR, 0o600), "w")
            try:
                fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("another closure/release operation owns this log") from error
        if not args.confirm_session_ended or not args.verified_handoff:
            raise ValueError("Supervisor must verify actual process exit and transcript/issue handoff")
        if args.deadline_seconds <= 0:
            raise ValueError("discovery deadline must be positive")
        actor = github_identity.automation_login()
        if not actor:
            raise ValueError("requires the configured automation identity")
        if args.mode == "finish":
            if not args.ledger or not args.session_id:
                raise ValueError("finish requires --ledger and one --session-id")
            entries = [{k: v for k, v in e.items() if not k.startswith("_")}
                       for e in session_record.load_ledger(args.ledger) if e["session_id"] == args.session_id]
            if len(entries) != 1:
                raise ValueError("native session must match exactly one ledger entry")
            entries[0]["_transcript_roots"] = transcript_roots(entries[0]["harness"], args.account_config, args.transcript_root)
            # record_closure verifies exit against the saved closure on retry,
            # or a fresh observation time for a new closure. A ledger timestamp
            # alone is not proof that this generation has already ended.
            if args.apply:
                entries = [record_closure(args.closed_log, entries[0], args.supervisor_session)]
            else:
                entries = [record_closure(args.closed_log, entries[0], args.supervisor_session, persist=False)]
            entries[0]["_finish"] = True
            entries[0]["_finish_scope"] = "explicit scope" if args.repo else "accessible repositories under the requested owner"
        else:
            rows = json.loads(args.closed_log.read_text())
            if isinstance(rows, list) and rows and isinstance(rows[0], dict) and "safe_exit_yes" in rows[0]:
                roots = {h: transcript_roots(h, args.account_config, args.transcript_root) for h in ("codex", "claude")}
                entries, inventory_reports = backfill_inventory.load(rows, roots)
                reports.extend(inventory_reports)
                owned = []
                for entry in entries:
                    if any(r["repo"].split("/")[0].casefold() != args.owner.casefold() for r in entry["_inventory_claims"]):
                        reports.append({"session_id": entry["session_id"], "error": "inventory group includes a repository outside the requested owner", "released": False})
                    else:
                        owned.append(entry)
                entries = owned
            else:
                entries = [{k: v for k, v in e.items() if not k.startswith("_")}
                           for e in session_record.load_ledger(args.closed_log, closed_log=True)]
            entries = [{**e, "_backfill": True} for e in entries]
            if args.session_id:
                entries = [e for e in entries if e["session_id"] == args.session_id]
            if not entries:
                raise ValueError("closed-session log has no matching records")
        thread = re.fullmatch(r"([^#]+)#([1-9]\d*)", args.handoff_thread or f"{args.owner}/codex-skills#884")
        if not thread or not REPO.fullmatch(thread[1]) or thread[1].split("/")[0].casefold() != args.owner.casefold():
            raise ValueError("handoff thread must be a canonical thread under the requested owner")
        entries = [{**e, "_handoff_thread": (thread[1], int(thread[2])),
                    "_transcript_roots": e.get("_transcript_roots") or transcript_roots(e["harness"], args.account_config, args.transcript_root),
                    "_read_deadline_seconds": args.deadline_seconds} for e in entries]
        verified = []
        handoff_cache = {}
        for entry in entries:
            try:
                entry["_handoff_cache"] = handoff_cache
                validate_closure(entry, args.supervisor_session)
                verified.append(entry)
            except (ValueError, KeyError, TypeError, OSError) as error:
                inventory_rows: list[dict[str, Any]] = entry.get("_inventory_claims", [])
                reports.append({"session_id": entry["session_id"], "error": str(error), "released": False,
                                "inventory_claims": [{"repository": r["repo"], "issue": r["issue"],
                                    "claim_comment": r["claim_comment_id"], "status": "preserved"}
                                    for r in inventory_rows]})
        entries = verified
        reader = GitHubReader(gh_prefix_args=automation_only_gh_prefix_args(), deadline_at=time.time() + args.deadline_seconds)
        if not entries:
            raise ValueError("no verified closed sessions; inspect the per-session reports")
        if all(e.get("_inventory") for e in entries):
            for entry in entries:
                for row in entry["_inventory_claims"]:
                    if args.repo and row["repo"] not in args.repo:
                        reports.append({"session_id": entry["session_id"], "repository": row["repo"], "issue": row["issue"],
                            "claim_comment": row["claim_comment_id"], "status": "out_of_scope", "released": False,
                            "error": "preserved outside explicit --repo scope"})
            issues = sorted({(r["repo"], r["issue"]) for e in entries for r in e["_inventory_claims"]
                             if not args.repo or r["repo"] in args.repo})
            repos = sorted({r for r, _ in issues})
        else:
            issues, repos = discover(reader, args.owner, entries, args.repo)
        for entry in entries:
            try:
                reports.append(release_session(reader, entry, actor, args.supervisor_session, issues,
                                                args.checkout_root, args.apply))
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
                    GitHubReadError, GitHubReadShapeError, CommentError) as error:
                report = error.report if isinstance(error, PartialRelease) else {"session_id": entry["session_id"]}
                reports.append({**report, "error": str(error), "next_action": "inspect records before retry"})
        ok = not any("error" in r or r.get("issue_refusals") or r.get("pr_claim_handoffs") for r in reports)
        print(json.dumps({"ok": ok, "reports": reports, "repositories_checked": repos,
                          "needs_reconciliation": any(r.get("retained_status_claims") or r.get("issue_refusals") or r.get("pr_claim_handoffs") or "error" in r for r in reports),
                          "coverage": "exact verified inventory claims" if all(e.get("_inventory") for e in entries) else "explicit scope" if args.repo else "accessible repositories under the requested owner",
                          "outside_scope": "other repository owners and other automation App identities; use their configured evidence-backed release-claim route"}))
        return 0 if ok else 1
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
            GitHubReadError, GitHubReadShapeError, CommentError) as error:
        if isinstance(error, PartialRelease):
            reports.append(error.report)
        print(json.dumps({"ok": False, "error": str(error), "completed_sessions": reports,
                          "next_action": "inspect posted records and remaining claims; do not replay an uncertain write"}))
        return 1
    finally:
        if lock_stream is not None:
            lock_stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
