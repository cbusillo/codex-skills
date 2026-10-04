# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Evidence-backed release of automation claims from closed sessions."""
from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any

import github_plan_claim as claim

RELEASE_MARKER = "github-plan:abandoned-release "


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
) -> str:
    record = source_record(source)
    if not actor or (source.get("user") or {}).get("login") != actor:
        raise ValueError("Release must use the source claim's configured automation identity")
    if role not in {"supervisor", "direction"} or not releaser_session or releaser_session == record["session"]:
        raise ValueError("Abandoned release requires a distinct direction or Supervisor native session")
    if (evidence.get("user") or {}).get("login") != actor:
        raise ValueError("Session-ended evidence must be authored by the configured automation identity")
    text = evidence.get("body") or ""
    if text.splitlines()[:1] != [f"Closed session {record['session']}"] or "Safe to exit: yes" not in text:
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
    retained_paths = {t["path"] for t in inventory["worktrees"] if t["branch"] == record["branch"]}
    if any(s.get("cwd") in retained_paths for s in inventory["sessions"]):
        raise ValueError("A live peer is using the source claim's retained worktree")
    for comment in related:
        if ((comment.get("user") or {}).get("login") != actor or claim.records(comment.get("body") or "")
                or not re.match(rf"Claimed by {re.escape(record['worker'])}(?:\s|$)", comment.get("body") or "")):
            raise ValueError("Related release must name an unstructured ownership follow-up of this exact source worker")
        if stamp(comment.get("updated_at") or comment.get("created_at")) >= cutoff:
            raise ValueError("Related ownership follow-up is newer than the session-ended evidence")
    receipt = {"source_id": source["id"], "source": record, "source_updated_at": source.get("updated_at") or source["created_at"],
               "evidence_url": evidence_url, "evidence_at": cutoff.isoformat(),
               "evidence_updated_at": evidence.get("updated_at") or evidence["created_at"], "role": role,
               "releaser_session": releaser_session, "retained_prs": retained_prs,
               "related_ids": [c["id"] for c in related]}
    return (f"Released claim {source['id']}\n\n"
            f"Closed-session release by {role}; source worker {record['worker']}, native session {record['session']}.\n"
            f"Session-ended evidence: {evidence_url}\n"
            "The caller verified the session ended; available native sessions and subsequent ownership activity were checked.\n"
            + ("Retained PR handoff: " + ", ".join(retained_prs) + "\n" if retained_prs else "")
            + "\n<!-- " + RELEASE_MARKER + json.dumps(receipt, sort_keys=True) + " -->")


def receipts(comments: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    found = []
    for comment in comments:
        lines = [line for line in (comment.get("body") or "").splitlines() if line.startswith("<!-- " + RELEASE_MARKER)]
        if not lines:
            continue
        if len(lines) != 1 or not lines[0].endswith(" -->"):
            raise ValueError("Malformed abandoned-release receipt")
        try:
            receipt = json.loads(lines[0][len("<!-- " + RELEASE_MARKER):-len(" -->")])
            if not isinstance(receipt, dict) or not all(key in receipt for key in (
                "source_id", "source", "evidence_url", "evidence_at", "evidence_updated_at", "role", "releaser_session")):
                raise ValueError("Incomplete abandoned-release receipt")
            if receipt["role"] not in {"supervisor", "direction"} or not receipt["releaser_session"]:
                raise ValueError("Invalid abandoned-release attester")
            stamp(receipt["evidence_at"])
            stamp(receipt["evidence_updated_at"])
        except (KeyError, TypeError) as exc:
            raise ValueError("Malformed abandoned-release receipt") from exc
        found.append((comment, receipt))
    return found


def validate_evidence(receipt: dict[str, Any], evidence: dict[str, Any], author: str) -> None:
    text = evidence.get("body") or ""
    ended = re.findall(r"(?m)^Ended at: (\S+)$", text)
    if ((evidence.get("user") or {}).get("login") != author
            or text.splitlines()[:1] != [f"Closed session {receipt['source']['session']}"]
            or "Safe to exit: yes" not in text
            or len(ended) != 1 or stamp(ended[0]) != stamp(receipt["evidence_at"])
            or stamp(evidence.get("updated_at") or evidence.get("created_at")) != stamp(receipt["evidence_updated_at"])):
        raise ValueError("Closed-session evidence changed or no longer attests this release")


def validate_releases(comments: list[dict[str, Any]]) -> None:
    """A generated release ceases to be usable after renewed source activity."""
    for release, receipt in receipts(comments):
        text = release.get("body") or ""
        source = next((c for c in comments if c.get("id") == receipt["source_id"]), None)
        if source is None or source_record(source) != receipt["source"]:
            raise ValueError("Abandoned-release source claim changed or is missing")
        if (source.get("user") or {}).get("login") != (release.get("user") or {}).get("login"):
            raise ValueError("Abandoned-release author differs from source automation identity")
        if claim.released_claim_id(text) != source["id"]:
            raise ValueError("Abandoned-release receipt does not match the exact release line")
        check_activity(comments, source, stamp(receipt["evidence_at"]))
