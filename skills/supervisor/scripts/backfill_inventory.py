# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Normalize the Supervisor's verified STALE-CLAIMS inventory without inventing exit metadata."""

from pathlib import Path
from typing import Any
import re
import session_record


def load(rows, roots_by_harness) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, dict[str, Any]] = {}
    reports: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("inventory rows must be objects")
        try:
            session = row["session_id"]
            if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session):
                raise ValueError("inventory requires an exact native session ID")
            if row.get("safe_exit_yes") is not True or row.get("still_running") is not False:
                raise ValueError("inventory must attest safe exit and actual session inactivity")
            if not isinstance(row.get("repo"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", row["repo"]):
                raise ValueError("inventory requires a canonical repository")
            if any(type(row.get(k)) is not int or row[k] <= 0 for k in ("issue", "claim_comment_id")):
                raise ValueError("inventory requires positive issue and exact claim-comment IDs")
            entry = grouped.setdefault(session, {"session_id": session, "ended_at": row["ended_at"],
                "safe_exit_yes": True, "still_running": False, "process_exit_verified": True,
                "_inventory": True, "_inventory_claims": []})
            if entry["ended_at"] != row["ended_at"]:
                raise ValueError("inventory has conflicting original closure timestamps")
            if row not in entry["_inventory_claims"]:
                entry["_inventory_claims"].append(row)
            if row.get("handoff_url"):
                if entry.get("handoff_url") and entry["handoff_url"] != row["handoff_url"]:
                    raise ValueError("inventory has conflicting explicit handoff URLs; preserve the original rows and use each source's evidence-backed release-claim route, or record all retained PRs with retained_pr_url for source-context backfill")
                entry["handoff_url"] = row["handoff_url"]
        except (KeyError, TypeError, ValueError) as error:
            reports.append({"session_id": row.get("session_id"), "repository": row.get("repo"),
                            "issue": row.get("issue"), "claim_comment": row.get("claim_comment_id"),
                            "error": str(error), "released": False})
    # Reject the whole native session if any of its rows lacks consistent evidence.
    for report in reports:
        grouped.pop(report["session_id"], None)
    entries = []
    for session, entry in grouped.items():
        paths = {path.resolve() for roots in roots_by_harness.values()
                 for root in roots for path in Path(root).rglob(f"*{session}*.jsonl")}
        try:
            candidates = []
            for path in sorted(paths):
                records = session_record.read_jsonl(path)
                harness = "codex" if any(r.get("type") == "session_meta" for r in records) else "claude"
                session_record.session_status({"session_id": session, "harness": harness, "transcript": str(path)})
                candidates.append((harness, path))
            if len({h for h, _ in candidates}) != 1:
                raise ValueError("native transcript missing or ambiguous across harnesses")
            harness, path = candidates[0]
            entries.append({**entry, "harness": harness, "transcript": str(path),
                            "_transcript_roots": list(dict.fromkeys(root for roots in roots_by_harness.values() for root in roots))})
        except (OSError, ValueError, TypeError) as error:
            reports.append({"session_id": session, "error": str(error), "released": False})
    return entries, reports
