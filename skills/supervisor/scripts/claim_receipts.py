# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read maintained claim/check receipts from native tool outputs, never chat prose."""
from __future__ import annotations

import json
import re
import shlex
import ast
from pathlib import Path

CHECK_OPERATION = "work-closeout.claims"
CLAIM_OPERATION = "github.plan.claim"
COMMENT = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/([1-9]\d*)#issuecomment-([1-9]\d*)")


class ReceiptCoverageError(ValueError):
    pass


def envelopes(value, depth=0):
    """Unwrap native text blocks and exec results, stopping at helper envelopes.

    Do not interpret issue bodies, quoted receipts or assistant text as writes.
    Both native harnesses and Codex's composed exec output use these wrappers.
    """
    if depth > 16:
        raise ReceiptCoverageError("tool output nesting exceeds receipt coverage")
    if isinstance(value, dict):
        if "operation" in value:
            yield value
            return
        if isinstance(value.get("session_id"), int):
            yield value  # exec process handle; links a later write_stdin result
        for key in ("output", "text", "content", "value", "result"):
            if key in value:
                yield from envelopes(value[key], depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from envelopes(child, depth + 1)
    elif isinstance(value, str):
        decoder, cursor = json.JSONDecoder(), 0
        while (start := value.find("{", cursor)) >= 0:
            try:
                item, cursor = decoder.raw_decode(value, start)
            except ValueError:
                cursor = start + 1
                continue
            yield from envelopes(item, depth + 1)


def outputs(records, harness):
    for index, record in enumerate(records):
        if harness == "codex":
            payload = record.get("payload") or {}
            if payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
                for value in envelopes(payload.get("output")):
                    yield index, value
        else:
            message = record.get("message")
            for block in (message.get("content") or []) if isinstance(message, dict) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    for value in envelopes(block.get("content")):
                        yield index, {**value, "_tool_error": block.get("is_error") is True}


def calls_and_results(record, harness):
    payload = record.get("payload") or {}
    if harness == "codex":
        calls = [(payload.get("call_id"), payload.get("name"), payload.get("arguments") or payload.get("input"))] if payload.get("type") in {"function_call", "custom_tool_call"} else []
        results = [payload.get("call_id")] if payload.get("type") in {"function_call_output", "custom_tool_call_output"} else []
    else:
        message = record.get("message")
        blocks = (message.get("content") or []) if isinstance(message, dict) else []
        calls = [(b.get("id"), b.get("name"), b.get("input")) for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        results = [b.get("tool_use_id") for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
    return calls, results


def arguments(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def shell_commands(name, value):
    """Only shell tools execute commands; tool prompts and echo operands do not."""
    value = arguments(value)
    if name in {"exec_command", "Bash"} and isinstance(value, dict):
        texts = [value.get("cmd") or value.get("command") or ""]
    elif name in {"exec", "functions.exec"} and isinstance(value, str):
        texts = []
        for m in re.finditer(r'''\b(?:cmd|command)\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')''', value):
            try:
                texts.append(ast.literal_eval(m[1]))
            except (ValueError, SyntaxError):
                pass
    else:
        return
    for text in texts:
        if "<<" in text:
            continue  # Heredoc bodies are data, not checker invocations.
        try:
            lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|\n")
            lexer.whitespace = " \t\r"
            lexer.whitespace_split = True
            command = []
            for token in lexer:
                if token and all(c in ";&|\n" for c in token):
                    if command:
                        yield command
                    command = []
                else:
                    command.append(token)
            if command:
                yield command
        except ValueError:
            continue


def script(argv):
    if not argv:
        return ""
    name = Path(argv[0]).name
    if name == "uv" and len(argv) > 2 and argv[1] == "run":
        return Path(argv[2]).name
    if re.fullmatch(r"python[0-9.]*", name) and len(argv) > 1:
        return Path(argv[1]).name
    return ""


def ownership_activity(records, harness):
    """Missing/redirected ownership writes still require readback."""
    for index, record in enumerate(records):
        calls, results = calls_and_results(record, harness)
        for identifier, name, value in calls:
            for argv in shell_commands(name, value):
                if (script(argv) == "gh-plan.py" and "claim" in argv
                        or script(argv) == "gh-pr.py" and "create" in argv
                        or argv[:3] == ["gh", "pr", "create"]):
                    yield index


def target_repositories(records, harness):
    repos = set()
    for record in records:
        calls, _ = calls_and_results(record, harness)
        for _, name, value in calls:
            for argv in shell_commands(name, value):
                if script(argv) not in {"gh-plan.py", "gh-pr.py"} and argv[:2] not in (["gh", "pr"], ["gh", "issue"]):
                    continue
                for i, token in enumerate(argv):
                    repo = argv[i + 1] if token in {"--repo", "-R"} and i + 1 < len(argv) else token.removeprefix("--repo=") if token.startswith("--repo=") else ""
                    if re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
                        repos.add(repo)
                    match = re.match(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/", token)
                    if match:
                        repos.add(match[1])
    return repos


def target_records(records, harness):
    return {(target["repository"], target["number"]) for target in claim_targets(records, harness)}


def claim_targets(records, harness):
    """Resolve numeric operands with their own invocation's canonical receipts."""
    def claiming(name, value):
        return any(script(argv) == "gh-plan.py" and "claim" in argv for argv in shell_commands(name, value))
    canonical = {}
    for _, source, value in bound_outputs(records, harness, claiming):
        if value.get("operation") != CLAIM_OPERATION:
            continue
        issue = value.get("issue") or {}
        repo, number = issue.get("repo"), issue.get("number")
        url = ((value.get("claim_comment") or {}).get("comment") or {}).get("url") or (value.get("claim_comment") or {}).get("url")
        match = COMMENT.fullmatch(url or "")
        if match:
            repo, number = match[1], int(match[2])
        if isinstance(repo, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repo) and type(number) is int and number > 0:
            canonical.setdefault(source, set()).add((repo, number))
    targets = []
    for record in records:
        calls, _ = calls_and_results(record, harness)
        for identifier, name, value in calls:
            commands = [argv for argv in shell_commands(name, value) if script(argv) == "gh-plan.py" and "claim" in argv]
            for argv in commands:
                target = argv[argv.index("claim") + 1:]
                if not target:
                    continue
                url = re.fullmatch(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/([1-9]\d*)", target[0])
                if url:
                    resolved = {(url[1], int(url[2]))}
                elif re.fullmatch(r"[1-9]\d*", target[0]):
                    repos = {argv[i + 1] for i, token in enumerate(argv[:-1]) if token in {"--repo", "-R"}}
                    repos.update(token.removeprefix("--repo=") for token in argv if token.startswith("--repo="))
                    # A native invocation can contain several shell commands.
                    # Its receipts do not identify which command produced them.
                    own = canonical.get(identifier, set()) if len(commands) == 1 else set()
                    resolved = {(repo, int(target[0])) for repo in repos} or {
                        pair for pair in own if pair[1] == int(target[0])} or {(None, int(target[0]))}
                else:
                    continue
                for repo, number in resolved:
                    targets.append({"invocation": identifier, "repository": repo, "number": number})
    return targets


def bound_outputs(records, harness, predicate, *, receipts=None):
    """Bind native outputs and process polls to an actual tool invocation."""
    calls, processes = {}, {}
    parsed = {}
    for index, value in outputs(records, harness) if receipts is None else receipts:
        parsed.setdefault(index, []).append(value)
    for index, record in enumerate(records):
        invocations, results = calls_and_results(record, harness)
        for call_id, name, value in invocations:
            invoked = predicate(name, value)
            value = arguments(value)
            polls = set()
            if name in {"write_stdin", "wait", "functions.wait"} and isinstance(value, dict):
                polls.update(str(value[k]) for k in ("session_id", "cell_id") if k in value)
            elif name in {"exec", "functions.exec"} and isinstance(value, str):
                polls.update(re.findall(r'''\b(?:session_id|cell_id)\s*:\s*["']?([\w-]+)''', value))
            source = call_id if invoked else next((processes[p] for p in polls if p in processes), None)
            calls[call_id] = source
            if invoked:
                yield index, source, {"_attempt": True}
        source = next((calls[result] for result in results if calls.get(result)), None)
        if source is not None:
            yield index, source, {"_result": True}
            for value in parsed.get(index, []):
                if isinstance(value.get("session_id"), int):
                    processes[str(value["session_id"])] = source
                yield index, source, value
            # Stock shell/orchestration tools may expose their handle as text.
            raw = json.dumps(record)
            for handle in re.findall(r"(?:Process running with session ID|Script running with cell ID) ([\w-]+)", raw):
                processes[handle] = source


def check_outputs(records, harness, *, receipts=None):
    """A saved report or tool prompt cannot attest a fresh executing check."""
    predicate = lambda name, value: any(script(argv) == "check_claims.py" for argv in shell_commands(name, value))
    for index, _, value in bound_outputs(records, harness, predicate, receipts=receipts):
        if value.get("_attempt"):
            yield index, {"operation": CHECK_OPERATION, "_attempt": True}
        elif value.get("operation") == CHECK_OPERATION and not value.get("_tool_error"):
            yield index, value


def unscoped_claim_invocations(records, harness):
    """A different claim's receipt cannot fill a redirected invocation's gap."""
    return [target["invocation"] for target in claim_targets(records, harness) if target["repository"] is None]


def sources(records, harness, session, *, parsed=None):
    """Return every source claim actually reported by the maintained claim helper."""
    found, errors = {}, []
    for index, value in outputs(records, harness) if parsed is None else parsed:
        if value.get("operation") != CLAIM_OPERATION:
            continue
        record = value.get("claim") or {}
        if session is None and record.get("session"):
            errors.append("claim receipt lacks unambiguous native transcript identity")
            continue
        if record.get("session") != session:
            continue
        receipt = value.get("claim_comment") or {}
        url = (receipt.get("comment") or {}).get("url") or receipt.get("url")
        match = COMMENT.fullmatch(url or "")
        if match:
            key = (match[1], int(match[2]), int(match[3]))
            found[key] = {"repository": key[0], "record": key[1], "claim_comment": key[2], "index": index}
        elif value.get("ok") or value.get("write_outcome") in {"unknown", "confirmed"} or "post_claim" in value.get("completed_steps", []):
            issue = value.get("issue") or {}
            if (value.get("ok") and "post_claim" not in value.get("completed_steps", [])
                    and any(key[:2] == (issue.get("repo"), issue.get("number")) for key in found)):
                continue  # Confirmed renewal of an already recorded source.
            errors.append("claim write has no complete source receipt")
    return list(found.values()), errors


def created_prs(records, harness):
    found = {value["url"] for _, value in outputs(records, harness)
            if value.get("operation") == "github.pr.create" and value.get("ok") is True
            and isinstance(value.get("url"), str)
            and re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9]\d*", value["url"])}
    def creating(name, value):
        return any(argv[:3] == ["gh", "pr", "create"] for argv in shell_commands(name, value))
    for index, _, value in bound_outputs(records, harness, creating):
        if value.get("_result"):
            found.update(re.findall(r"https://github\.com/[^/\s\"\\]+/[^/\s\"\\]+/pull/[1-9]\d*", json.dumps(records[index])))
    return found


def acceptance(records, harness, session, *, histories=None, history_error=None):
    try:
        if history_error:
            return {"clear": False, "reason": "child_history_unavailable", "detail": history_error}
        import claim_history
        if histories is None and claim_history.delegated(records, harness):
            return {"clear": False, "reason": "child_history_unavailable"}
        return _acceptance(records, harness, session, histories)
    except (ReceiptCoverageError, RecursionError):
        return {"clear": False, "reason": "receipt_coverage_exceeded"}


def _acceptance(records, harness, session, histories):
    """A claimed session needs fresh passing evidence for all its source claims.

    Plain safe-exit prose stays recognizable; it is insufficient for shutdown
    acceptance when this native history contains claim ownership.
    """
    parsed = list(outputs(records, harness))
    histories = histories or [{"session_id": session, "harness": harness, "records": records}]
    sessions = {h["session_id"] for h in histories}
    expected, errors = [], []
    root_expected = []
    child_activity = False
    for node in histories:
        for native in sessions:
            found, missing = sources(node["records"], node["harness"], native)
            expected.extend(found)
            errors.extend(missing)
            if node is histories[0]:
                root_expected.extend(found)
        if node is not histories[0] and list(ownership_activity(node["records"], node["harness"])):
            child_activity = True
    activity = list(ownership_activity(records, harness))
    if not expected and not errors and not activity and not child_activity:
        return {"clear": True, "reason": "no_claim_receipts"}
    keys = {(r["repository"], r["record"], r["claim_comment"]) for r in expected}
    last_claim = max([r["index"] for r in root_expected] + activity + [-1])
    last_claim = max([last_claim, *(i for i, v in parsed if v.get("operation") == CLAIM_OPERATION
                                  or v.get("operation") == "github.pr.create" and v.get("ok"))])
    checks = list(check_outputs(records, harness, receipts=parsed))
    last_attempt = max((i for i, v in checks if v.get("_attempt")), default=-1)
    for index, value in reversed(checks):
        if (value.get("operation") != CHECK_OPERATION or value.get("session_id") != session
                or value.get("harness") != harness or index <= max(last_claim, last_attempt)):
            continue
        coverage = value.get("coverage") or {}
        observed = {(r.get("repository"), r.get("record"), r.get("claim_comment"))
                    for r in value.get("released_claims", []) if isinstance(r, dict)}
        required = {(h["session_id"], h["transcript"], h["digest"]) for h in histories[1:]}
        covered = {(h.get("session_id"), h.get("transcript"), h.get("digest")) for h in coverage.get("histories", [])}
        clear = (value.get("ok") is True and value.get("claims_clear") is True
                 and coverage.get("complete") is True and keys <= observed
                 and required <= covered and (not required or sessions <= set(coverage.get("session_ids", [])))
                 and (not errors or value.get("receipt_reconciliation") is True))
        return {"clear": clear, "reason": "verified_claim_disposition" if clear else "claim_disposition_not_clear"}
    return {"clear": False, "reason": "claim_disposition_not_checked"}
