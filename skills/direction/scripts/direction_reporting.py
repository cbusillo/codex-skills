"""Optional weekly PR reporting rules, independent of work admission."""

from __future__ import annotations

import json
import re
from typing import Any


def reporting_map(text: str) -> list[dict[str, Any]] | None:
    """Read one explicit JSON map; absence preserves the legacy report."""
    sections = re.split(r"(?m)^##\s+", text)
    matches = [part.partition("\n")[2] for part in sections[1:]
               if part.partition("\n")[0].strip() == "Reporting map"]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Reporting map heading must occur once")
    block = re.fullmatch(r"\s*```json\s*\n(.*?)\n```\s*", matches[0], re.S)
    if not block:
        raise ValueError("Reporting map must contain one JSON code block")

    def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate reporting map key: {key}")
            result[key] = value
        return result

    rules = json.loads(block[1], object_pairs_hook=unique_keys)
    if not isinstance(rules, list) or not rules:
        raise ValueError("Reporting map must be a nonempty array of rules")
    names: set[str] = set()
    evidence: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or set(rule) not in ({"name", "labels"}, {"name", "evidence"}):
            raise ValueError("Each reporting rule needs name and either labels or evidence")
        name = rule["name"]
        if not isinstance(name, str) or not name.strip() or name != name.strip() or name.casefold() in names:
            raise ValueError("Reporting names must be nonempty and unique")
        names.add(name.casefold())
        if "labels" in rule:
            labels = rule["labels"]
            if (not isinstance(labels, list) or not labels
                    or any(not isinstance(label, str) or not label.strip() or label != label.strip() for label in labels)):
                raise ValueError("Rule labels must be a nonempty array of label names")
        else:
            selector = rule["evidence"]
            if not isinstance(selector, str) or selector not in {"direction-only", "milestone", "own-project"} or selector in evidence:
                raise ValueError("Evidence must be direction-only, milestone or own-project, each used once")
            if selector == "direction-only" and index != 0:
                raise ValueError("The direction-only rule must come first")
            evidence.add(selector)
    if "direction-only" not in evidence:
        raise ValueError("The first rule must classify direction-only changes")
    return rules


def classify(
    rules: list[dict[str, Any]], pull: dict[str, Any], *, milestone: bool, own: bool,
) -> tuple[str | None, str | None, list[str]]:
    """Return one kind or one review reason; never infer a tooling fallback."""
    files = pull.get("_report_files")
    if files is None:
        return None, "files_unavailable", []
    paths = {path for row in files for path in (row.get("filename"), row.get("previous_filename")) if path}
    if "DIRECTION.md" in paths:
        if paths == {"DIRECTION.md"}:
            return rules[0]["name"], None, []
        return None, "mixed_direction_change", []
    if not pull.get("_report_links_complete", False) or pull.get("_report_labels") is None:
        return None, "attribution_unavailable", []
    labels = pull["_report_labels"]
    label_matches = [rule["name"] for rule in rules if "labels" in rule
                     and labels & {label.casefold() for label in rule["labels"]}]
    for rule in rules:
        selector = rule.get("evidence")
        if "labels" in rule and rule["name"] in label_matches:
            if len(label_matches) > 1:
                return None, "conflicting_labels", label_matches
            return rule["name"], None, []
        if (selector == "milestone" and milestone) or (selector == "own-project" and own):
            return rule["name"], None, []
    return None, "unmatched", []


def report(
    rules: list[dict[str, Any]], rows: list[tuple[str, dict[str, Any], bool, bool]], *,
    capacity: str, source: str, incomplete: bool, error: str | None = None,
) -> dict[str, Any]:
    counts = {rule["name"]: 0 for rule in rules}
    unresolved: list[dict[str, Any]] = []
    for repo, pull, milestone, own in rows:
        kind, reason, matches = classify(rules, pull, milestone=milestone, own=own) if not error else (None, "invalid_map", [])
        if kind is not None:
            counts[kind] += 1
        else:
            unresolved.append({"repo": repo, "number": pull["number"], "title": pull.get("title"),
                               "url": f"https://github.com/{repo}/pull/{pull['number']}",
                               "reason": reason, "matching_kinds": matches})
    total = len(rows)
    unknown = incomplete or any(row["reason"].endswith("unavailable") for row in unresolved)
    status = ("invalid" if error else "unknown" if unknown else "needs_review" if unresolved
              else "complete" if total else "no_merges")
    return {"status": status, "basis": "merged_pull_requests", "source": source,
            "policy_window": "map_read_at_audit_start_applies_to_whole_window",
            "capacity": capacity, "rules": rules, "merged_total": total,
            "merged_by_kind": counts,
            "share_by_kind": {kind: round(count / total, 3) if total else None for kind, count in counts.items()},
            "unresolved": unresolved, "error": error}
