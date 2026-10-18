# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read maintained claim/check receipts from native tool outputs, never chat prose."""
from __future__ import annotations

import json
import re

CHECK_OPERATION = "work-closeout.claims"
CLAIM_OPERATION = "github.plan.claim"
COMMENT = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(?:issues|pull)/([1-9]\d*)#issuecomment-([1-9]\d*)")


def envelopes(value, depth=0):
    """Unwrap native text blocks and exec results, stopping at helper envelopes.

    Do not interpret issue bodies, quoted receipts or assistant text as writes.
    Both native harnesses and Codex's composed exec output use these wrappers.
    """
    if depth > 16:
        raise ValueError("tool output nesting exceeds receipt coverage")
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
                item, length = decoder.raw_decode(value[start:])
                cursor = start + length
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


def check_outputs(records, harness, *, receipts=None):
    """Only a check invocation (or its process poll) can attest disposition.

    Reading a saved JSON report is not a fresh check. Track native call IDs and
    exec process handles so Codex orchestration and long-running reads work.
    """
    calls, processes = {}, set()
    parsed = {}
    for index, value in outputs(records, harness) if receipts is None else receipts:
        parsed.setdefault(index, []).append(value)
    for index, record in enumerate(records):
        payload = record.get("payload") or {}
        message = record.get("message")
        blocks = (message.get("content") or []) if isinstance(message, dict) else []
        invocations = []
        results = []
        if harness == "codex":
            if payload.get("type") in {"function_call", "custom_tool_call"}:
                invocations.append((payload.get("call_id"), payload.get("arguments") or payload.get("input")))
            if payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
                results.append(payload.get("call_id"))
        else:
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    invocations.append((block.get("id"), block.get("input")))
                if block.get("type") == "tool_result":
                    results.append(block.get("tool_use_id"))
        for call_id, arguments in invocations:
            text = arguments if isinstance(arguments, str) else json.dumps(arguments)
            # JSON arguments can contain escaped shell commands; decode one
            # envelope before matching the maintained helper invocation.
            try:
                value = json.loads(text)
                if isinstance(value, dict):
                    text = value.get("cmd") or value.get("command") or value.get("code") or text
            except ValueError:
                pass
            invoked = bool(re.search(r"\b(?:uv\s+run|python[0-9.]*)\s+(?:[^\s\"']*/)?check_claims\.py\b", text))
            polls = {int(n) for n in re.findall(r"session_id[\"']?\s*:\s*([0-9]+)", text)}
            calls[call_id] = invoked or bool(polls & processes)
            if invoked:
                yield index, {"operation": CHECK_OPERATION, "_attempt": True}
        if any(calls.get(call_id) for call_id in results):
            for value in parsed.get(index, []):
                if isinstance(value.get("session_id"), int):
                    processes.add(value["session_id"])
                if value.get("operation") == CHECK_OPERATION and not value.get("_tool_error"):
                    yield index, value


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
    return {value["url"] for _, value in outputs(records, harness)
            if value.get("operation") == "github.pr.create" and value.get("ok") is True
            and isinstance(value.get("url"), str)
            and re.fullmatch(r"https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9]\d*", value["url"])}


def acceptance(records, harness, session):
    """A claimed session needs fresh passing evidence for all its source claims.

    Plain safe-exit prose stays recognizable; it is insufficient for shutdown
    acceptance when this native history contains claim ownership.
    """
    parsed = list(outputs(records, harness))
    expected, errors = sources(records, harness, session, parsed=parsed)
    if errors:
        return {"clear": False, "reason": "incomplete_claim_receipts"}
    if not expected:
        return {"clear": True, "reason": "no_claim_receipts"}
    keys = {(r["repository"], r["record"], r["claim_comment"]) for r in expected}
    last_claim = max(r["index"] for r in expected)
    last_claim = max([last_claim, *(i for i, v in parsed if v.get("operation") == "github.pr.create" and v.get("ok"))])
    checks = list(check_outputs(records, harness, receipts=parsed))
    last_attempt = max((i for i, v in checks if v.get("_attempt")), default=-1)
    for index, value in reversed(checks):
        if (value.get("operation") != CHECK_OPERATION or value.get("session_id") != session
                or value.get("harness") != harness or index <= max(last_claim, last_attempt)):
            continue
        coverage = value.get("coverage") or {}
        observed = {(r.get("repository"), r.get("record"), r.get("claim_comment"))
                    for r in value.get("released_claims", []) if isinstance(r, dict)}
        clear = (value.get("ok") is True and value.get("claims_clear") is True
                 and coverage.get("complete") is True and keys <= observed)
        return {"clear": clear, "reason": "verified_claim_disposition" if clear else "claim_disposition_not_clear"}
    return {"clear": False, "reason": "claim_disposition_not_checked"}
