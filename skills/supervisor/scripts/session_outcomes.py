# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any


def record(entry: dict, records: list[dict], final_text: str) -> dict:
    models, efforts = set(), set()
    for row in records:
        if entry["harness"] == "codex":
            if row.get("type") != "turn_context":
                continue
            context = row.get("payload") or {}
            model, effort = context.get("model"), context.get("effort")
        else:
            model = (row.get("message") or {}).get("model") if row.get("type") == "assistant" else None
            effort = row.get("effort")
        if isinstance(model, str) and model and model != "<synthetic>":
            models.add(model)
        if isinstance(effort, str) and effort:
            efforts.add(effort)
    outcome: dict[str, Any] = {"model": ", ".join(sorted(models)) or "unknown",
                               "effort": ", ".join(sorted(efforts)) or "unknown",
                               "landed_without_rescue": None, "review_findings_acted_on": None}
    reports = re.findall(r"^Session outcome: (\{[^\n]*\})\s*$", final_text, re.MULTILINE)
    if len(reports) != 1:
        return outcome
    try:
        report = json.loads(reports[0])
    except ValueError:
        return outcome
    if type(report.get("landed_without_rescue")) is bool:
        outcome["landed_without_rescue"] = report["landed_without_rescue"]
    count = report.get("review_findings_acted_on")
    if type(count) is int and count >= 0:
        outcome["review_findings_acted_on"] = count
    return outcome


def weekly(path: Path, *, owner: str, since: dt.datetime, until: dt.datetime) -> dict:
    result: dict[str, Any] = {"status": "complete", "rows": [], "basis": "closed_sessions_on_this_host"}
    try:
        entries = json.loads(path.read_text())
        if not isinstance(entries, list) or not all(isinstance(row, dict) for row in entries):
            raise ValueError("closed-session log must contain objects")
        groups: dict[tuple[str, str, str], dict] = {}
        for entry in entries:
            if entry.get("supervisor_owned") is not True or not entry.get("ended_at") or entry.get("resumed_at"):
                continue
            ended = dt.datetime.fromisoformat(entry["ended_at"].replace("Z", "+00:00"))
            if not since <= ended < until:
                continue
            match = re.fullmatch(r"https://github.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/\d+#issuecomment-\d+",
                                 entry.get("handoff_url") or "")
            repo = match[1].casefold() if match else "unknown"
            if match and repo.split("/")[0] != owner.casefold():
                continue
            outcome = entry.get("session_outcome") or {}
            model, effort = outcome.get("model") or "unknown", outcome.get("effort") or "unknown"
            row = groups.setdefault((repo, model, effort), {
                "repository": repo, "model": model, "effort": effort, "sessions": 0,
                "landed_without_rescue": 0, "other_outcomes": 0, "outcome_unknown": 0,
                "review_findings_acted_on": 0, "review_findings_unknown": 0,
            })
            row["sessions"] += 1
            landed = outcome.get("landed_without_rescue")
            row["landed_without_rescue" if landed is True else "other_outcomes" if landed is False else "outcome_unknown"] += 1
            count = outcome.get("review_findings_acted_on")
            if type(count) is int and count >= 0:
                row["review_findings_acted_on"] += count
            else:
                row["review_findings_unknown"] += 1
        result["rows"] = [groups[key] for key in sorted(groups)]
    except (OSError, ValueError, TypeError, AttributeError) as error:
        result.update(status="unavailable", reason=str(error))
    return result
