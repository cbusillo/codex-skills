# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Evidence-backed release of automation claims from closed sessions."""
from __future__ import annotations

from datetime import datetime
import json
import pathlib
import re
from typing import Any

import github_plan_claim as claim

RELEASE_MARKER = "github-plan:abandoned-release "


def related_sessions_match(text: str, session: str) -> bool:
    """Compare explicit session fields outside fenced examples."""
    prose = claim.ownership_text(text, strip_quotes=False)
    fence = None
    for line in prose.splitlines():
        line = re.sub(r"^\s*(?:>\s*)+", "", line)
        opener = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*", line):
                fence = None
            continue
        if opener:
            fence = opener.group(1)
            continue
        for field in re.finditer(r"\b(?:Native[ \t]+)?Session(?:[ \t]+ID)?[ \t]*[:=][ \t]*([^\n]+)", line, re.IGNORECASE):
            if not re.fullmatch(rf"{re.escape(session)}\.?(?:[ \t]+\([^()]*\))?", field.group(1).strip()):
                return False
    return True


def stamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Release evidence requires timezone-qualified timestamps")
    return parsed


def source_record(source: dict[str, Any]) -> dict[str, str]:
    parsed = claim.records(source.get("body") or "")
    if len(parsed) != 1:
        raise ValueError("Abandoned release requires one structured source claim")
    return parsed[0]


def check_activity(comments: list[dict[str, Any]], source: dict[str, Any], cutoff: datetime) -> None:
    record = source_record(source)
    source_at = stamp(source.get("updated_at") or source.get("created_at"))
    if max(source_at, stamp(record["claimed_at"])) >= cutoff:
        raise ValueError("Source claim changed after the cited session-ended evidence")
    for comment in comments:
        if comment.get("id") == source.get("id"):
            continue
        text = comment.get("body") or ""
        # Shared bot identity is not session identity. Only explicit ownership
        # activity can be attributed to this native session.
        matching = any(r["session"] == record["session"] for r in claim.records(text))
        matching |= bool(re.match(rf"Claimed by {re.escape(record['worker'])}(?:\s|$)", text))
        if matching and stamp(comment.get("updated_at") or comment.get("created_at")) >= cutoff:
            raise ValueError("Source session shows ownership activity after the cited evidence")


def prepare_release(
    source: dict[str, Any], evidence: dict[str, Any], comments: list[dict[str, Any]],
    inventory: dict[str, Any], *, actor: str, role: str, releaser_session: str,
    evidence_url: str, retained_prs: list[str], related: list[dict[str, Any]],
    checked_prs: list[str] | None = None, retained_branches: set[str] | None = None,
) -> str:
    record = source_record(source)
    if not actor or (source.get("user") or {}).get("login") != actor:
        raise ValueError("Release must use the source claim's configured automation identity")
    if role not in {"supervisor", "direction"} or not releaser_session or releaser_session == record["session"]:
        raise ValueError("Abandoned release requires a distinct direction or Supervisor native session")
    if (evidence.get("user") or {}).get("login") != actor:
        raise ValueError("Session-ended evidence must be authored by the configured automation identity")
    text = evidence.get("body") or ""
    if text.splitlines()[:1] != [f"Closed session {record['session']}"] or "Safe to exit: yes" not in text.splitlines():
        raise ValueError("Evidence must be a closed-session attestation naming the exact native session and Safe to exit: yes")
    ended = re.findall(r"(?m)^Ended at: (\S+)$", text)
    if len(ended) != 1:
        raise ValueError("Closed-session attestation must record the verified Ended at timestamp")
    cutoff = stamp(ended[0])
    if cutoff > stamp(evidence.get("created_at")):
        raise ValueError("Session-ended timestamp is newer than its attestation")
    check_activity(comments, source, cutoff)
    if any(s.get("sessionId") == record["session"] for s in inventory["sessions"]):
        raise ValueError("Source session is still visible in the active native session inventory")
    retained_paths = {pathlib.Path(t["path"]).resolve() for t in inventory["worktrees"] if t["branch"] in {record["branch"], *(retained_branches or set())}}
    if any(cwd == root or root in cwd.parents for s in inventory["sessions"]
           for cwd in [pathlib.Path(s.get("cwd") or "/").resolve()] for root in retained_paths):
        raise ValueError("A live peer is using the source claim's retained worktree")
    for comment in related:
        if ((comment.get("user") or {}).get("login") != actor or claim.records(comment.get("body") or "")
                or not re.match(rf"Claimed by {re.escape(record['worker'])}(?:\s|$)", comment.get("body") or "")):
            raise ValueError("Related release must name an unstructured ownership follow-up of this exact source worker")
        if not related_sessions_match(comment.get("body") or "", record["session"]):
            raise ValueError("Related ownership follow-up names a different native session; obtain that session's own closure evidence")
        if stamp(comment.get("updated_at") or comment.get("created_at")) >= cutoff:
            raise ValueError("Related ownership follow-up is newer than the session-ended evidence")
    receipt = {"source_id": source["id"], "source": record, "source_updated_at": source.get("updated_at") or source["created_at"],
               "evidence_url": evidence_url, "evidence_at": cutoff.isoformat(),
               "evidence_updated_at": evidence.get("updated_at") or evidence["created_at"], "role": role,
               "releaser_session": releaser_session, "retained_prs": retained_prs,
               "checked_prs": checked_prs or [],
               "related_ids": [c["id"] for c in related]}
    return (f"Released claim {source['id']}\n\n"
            f"Closed-session release by {role}; source worker {record['worker']}, native session {record['session']}.\n"
            f"Session-ended evidence: {evidence_url}\n"
            "The caller verified the session ended; available native sessions and subsequent ownership activity were checked.\n"
            + ("Retained PR handoff: " + ", ".join(retained_prs) + "\n" if retained_prs else "")
            + "\n<!-- " + RELEASE_MARKER + json.dumps(receipt, sort_keys=True) + " -->")


def receipt_for(comment: dict[str, Any]) -> dict[str, Any] | None:
    text = re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", comment.get("body") or "")
    lines = [line for line in text.splitlines() if line.startswith("<!-- " + RELEASE_MARKER)]
    if len(lines) != 1 or not lines[0].endswith(" -->"):
        return None
    try:
        receipt = json.loads(lines[0][len("<!-- " + RELEASE_MARKER):-len(" -->")])
        if not isinstance(receipt, dict) or not all(key in receipt for key in (
            "source_id", "source", "evidence_url", "evidence_at", "evidence_updated_at", "role", "releaser_session", "checked_prs")):
            return None
        if (type(receipt["source_id"]) is not int or not isinstance(receipt["source"], dict)
                or not all(isinstance(receipt["source"].get(k), str) and receipt["source"][k] for k in ("worker", "session", "branch", "claimed_at"))
                or receipt["role"] not in {"supervisor", "direction"} or not receipt["releaser_session"]
                or receipt["releaser_session"] == receipt["source"]["session"]
                or not isinstance(receipt["evidence_url"], str)
                or not isinstance(receipt["checked_prs"], list)
                or not all(isinstance(url, str) for url in receipt["checked_prs"])):
            return None
        stamp(receipt["evidence_at"])
        stamp(receipt["evidence_updated_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return receipt


def receipts(comments: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [(c, r) for c in comments if (r := receipt_for(c)) is not None]


def without_release(comment: dict[str, Any]) -> dict[str, Any]:
    # Keep any ownership assertions: an invalid receipt is not a release,
    # rather than a permanent error or a way to hide a structured claim.
    return {**comment, "body": "\n".join(line for line in (comment.get("body") or "").splitlines()
        if not line.startswith("Released claim ") and not line.startswith("<!-- " + RELEASE_MARKER))}


def validate_evidence(receipt: dict[str, Any], evidence: dict[str, Any], author: str) -> None:
    text = evidence.get("body") or ""
    ended = re.findall(r"(?m)^Ended at: (\S+)$", text)
    if ((evidence.get("user") or {}).get("login") != author
            or text.splitlines()[:1] != [f"Closed session {receipt['source']['session']}"]
            or "Safe to exit: yes" not in text.splitlines()
            or len(ended) != 1 or stamp(ended[0]) != stamp(receipt["evidence_at"])
            or stamp(evidence.get("updated_at") or evidence.get("created_at")) != stamp(receipt["evidence_updated_at"])):
        raise ValueError("Closed-session evidence changed or no longer attests this release")


def effective_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Invalid releases restore source ownership; they never poison an issue."""
    result = []
    for comment in comments:
        text = re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", comment.get("body") or "")
        if not any(line.startswith("<!-- " + RELEASE_MARKER) for line in text.splitlines()):
            result.append(comment)
            continue
        receipt = receipt_for(comment)
        try:
            if receipt is None:
                raise ValueError("Invalid receipt")
            source = next((c for c in comments if c.get("id") == receipt["source_id"]), None)
            if (source is None or source_record(source) != receipt["source"]
                    or not (source.get("user") or {}).get("login")
                    or (source.get("user") or {}).get("login") != (comment.get("user") or {}).get("login")
                    or claim.released_claim_id(comment.get("body") or "") != source["id"]):
                raise ValueError("Source no longer matches receipt")
            check_activity(comments, source, stamp(receipt["evidence_at"]))
        except (KeyError, TypeError, ValueError):
            comment = without_release(comment)
        result.append(comment)
    return result
