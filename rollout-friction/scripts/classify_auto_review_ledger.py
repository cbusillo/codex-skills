#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Classify auto-review ledger runs against the active checkout.

The ledger printed by Every Code can include detached auto-review worktrees whose
findings still match their own snapshot even after the active checkout has moved
on. This helper keeps that distinction mechanical: it parses ledger text and
labels each run as current-target, detached proposal, stale snapshot, or unknown.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


RUN_RE = re.compile(r"^run\s+(?P<fields>.+)$")
FINDING_RE = re.compile(r"^\s+finding\s+(?P<fields>.+)$")
FIELD_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)")
AUTO_REVIEW_BRANCH_RE = re.compile(r"^auto-review-[0-9a-f]{7,40}$", re.I)
AUTO_REVIEW_PATH_RE = re.compile(r"/(?:branches|worktrees)/auto-review[^/]*(?:/|$)|/branches/auto-review[^/]*(?:/|$)", re.I)
HEX_RE = re.compile(r"^[0-9a-f]{7,40}$", re.I)


@dataclass
class Finding:
    fields: dict[str, str]


@dataclass
class LedgerRun:
    fields: dict[str, str]
    findings: list[Finding] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify Every Code auto-review ledger entries.")
    parser.add_argument("ledger", nargs="?", type=Path, help="Ledger text file. Reads stdin when omitted.")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Active repo checkout used for HEAD/default-branch comparison.")
    parser.add_argument("--trusted-local-details", action="store_true", help="Include raw local finding locations and titles in JSON output.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ledger_text = read_ledger(args.ledger)
    repo = args.repo.expanduser().resolve()
    git_state = collect_git_state(repo)
    runs = parse_ledger(ledger_text)
    classifications = [classify_run(run, git_state, trusted_local_details=args.trusted_local_details) for run in runs]
    payload = {
        "ok": True,
        "repo": str(repo),
        "git": git_state,
        "summary": summarize(classifications),
        "runs": classifications,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        emit_text(payload)
    return 0


def read_ledger(path: Path | None) -> str:
    if path is None:
        return sys.stdin.read()
    try:
        return path.expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SystemExit(f"error: unable to read ledger: {exc}") from exc


def parse_ledger(text: str) -> list[LedgerRun]:
    runs: list[LedgerRun] = []
    current: LedgerRun | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        run_match = RUN_RE.match(line)
        if run_match:
            current = LedgerRun(parse_fields(run_match.group("fields")))
            runs.append(current)
            continue
        finding_match = FINDING_RE.match(line)
        if finding_match and current is not None:
            current.findings.append(Finding(parse_fields(finding_match.group("fields"))))
    return runs


def parse_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in FIELD_RE.finditer(text):
        fields[match.group("key")] = match.group("value").strip()
    return fields


def collect_git_state(repo: Path) -> dict[str, Any]:
    return {
        "root": str(repo),
        "head": git(repo, "rev-parse", "HEAD"),
        "branch": git(repo, "branch", "--show-current"),
        "default_branch": default_branch(repo),
        "origin_default_head": git(repo, "rev-parse", "refs/remotes/origin/HEAD"),
    }


def default_branch(repo: Path) -> str | None:
    symbolic = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if symbolic and symbolic.startswith("origin/"):
        return symbolic.removeprefix("origin/")
    return None


def git(repo: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def classify_run(
    run: LedgerRun,
    git_state: dict[str, Any],
    *,
    trusted_local_details: bool = False,
) -> dict[str, Any]:
    branch = run.fields.get("branch", "")
    snapshot = run.fields.get("snapshot", "")
    target = run.fields.get("target", "")
    finding_locations = [finding.fields.get("location", "") for finding in run.findings]
    detached_branch = is_auto_review_branch(branch)
    detached_locations = [location for location in finding_locations if is_auto_review_location(location)]
    snapshot_matches_head = sha_prefix_matches(snapshot, git_state.get("head"))
    snapshot_matches_default = sha_prefix_matches(snapshot, git_state.get("origin_default_head"))
    current_target = snapshot_matches_head or target == "active_checkout"
    if current_target:
        classification = "current_target"
        actionable = bool(run.findings)
        recommended_action = "address_or_explicitly_defer_findings" if run.findings else "no_action"
    elif detached_branch or detached_locations:
        classification = "detached_auto_review"
        actionable = False
        recommended_action = "verify_against_active_target_before_fixing"
        if not snapshot_matches_head and not snapshot_matches_default:
            recommended_action = "ignore_or_summarize_as_stale_detached_noise"
    elif snapshot_matches_default:
        classification = "default_branch_target"
        actionable = bool(run.findings)
        recommended_action = "check_if_default_branch_findings_are_relevant"
    else:
        classification = "unknown_or_stale"
        actionable = False
        recommended_action = "verify_target_before_acting"
    return {
        "id": run.fields.get("id"),
        "status": run.fields.get("status"),
        "freshness": run.fields.get("freshness"),
        "target": target,
        "branch": branch,
        "snapshot": snapshot,
        "finding_count": len(run.findings),
        "classification": classification,
        "actionable_by_default": actionable,
        "recommended_action": recommended_action,
        "detached_auto_review_branch": detached_branch,
        "detached_finding_count": len(detached_locations),
        "snapshot_matches_head": snapshot_matches_head,
        "snapshot_matches_origin_default": snapshot_matches_default,
        "findings": [finding_to_json(finding, trusted_local_details=trusted_local_details) for finding in run.findings],
    }


def finding_to_json(finding: Finding, *, trusted_local_details: bool) -> dict[str, Any]:
    location = finding.fields.get("location", "")
    payload: dict[str, Any] = {
        "id": finding.fields.get("id"),
        "priority": finding.fields.get("priority"),
        "location_kind": "detached_auto_review_worktree" if is_auto_review_location(location) else "active_or_unknown",
        "location_id": stable_text_id(location) if location else None,
    }
    if trusted_local_details:
        payload["location"] = location
        payload["title"] = finding.fields.get("title")
    return payload


def stable_text_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def is_auto_review_branch(branch: str) -> bool:
    return bool(AUTO_REVIEW_BRANCH_RE.search(branch))


def is_auto_review_location(location: str) -> bool:
    return bool(AUTO_REVIEW_PATH_RE.search(location))


def sha_prefix_matches(prefix: str | None, full: Any) -> bool:
    if not prefix or not isinstance(full, str) or not HEX_RE.match(prefix):
        return False
    return full.startswith(prefix.lower()) or full.startswith(prefix.upper())


def summarize(runs: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(run.get("classification")) for run in runs)
    counts["total_runs"] = len(runs)
    counts["actionable_by_default"] = sum(1 for run in runs if run.get("actionable_by_default"))
    return dict(sorted(counts.items()))


def emit_text(payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    print(
        "Auto-review ledger classification: "
        f"{summary.get('total_runs', 0)} run(s), "
        f"{summary.get('actionable_by_default', 0)} actionable by default."
    )
    for run in payload["runs"]:
        print(
            f"- {run.get('id') or '<unknown>'}: {run['classification']} "
            f"branch={run.get('branch') or '<none>'} findings={run['finding_count']} "
            f"action={run['recommended_action']}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
