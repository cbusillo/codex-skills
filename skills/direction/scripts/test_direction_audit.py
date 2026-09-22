#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Tests for the read-only direction audit. Each case plants a real drift."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).with_name("direction_audit.py")
NOW = dt.datetime(2026, 9, 21, 12, tzinfo=dt.timezone.utc)

DIRECTION = """# Direction

## Purpose
Ship it.

## Stop Boundaries
- spending money

## Journey
One change lands end to end.

## Retired
- the old thing

## Milestones
- `Thin fork decision` proves the engine choice; ends if the spikes fail.
- `Dogfood week` proves daily use; ends if the owner stops using it.
"""


def load() -> Any:
    spec = importlib.util.spec_from_file_location("direction_audit_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def milestone(number: int, title: str, *, state: str = "open", creator: str = "owner") -> dict[str, Any]:
    return {"number": number, "title": title, "state": state, "creator": {"login": creator}}


def issue(number: int, title: str, *, body: str = "", labels: tuple[str, ...] = (), created: str = "2026-09-10T00:00:00Z") -> dict[str, Any]:
    return {"number": number, "title": title, "body": body, "labels": [{"name": name} for name in labels], "created_at": created}


def run(module: Any, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "direction_text": DIRECTION,
        "milestones": [milestone(1, "Thin fork decision"), milestone(2, "Dogfood week")],
        "issues": [],
        "owner": "owner",
        "automation": "bot",
        "now": NOW,
    }
    params.update(overrides)
    return module.audit(**params)


def kinds(result: dict[str, Any]) -> list[str]:
    return [item["kind"] for item in result["findings"]]


def test_clean_state_has_no_findings() -> None:
    module = load()
    result = run(module)
    assert result["ok"] is True, result
    assert result["listed_milestones"] == ["Thin fork decision", "Dogfood week"]


def test_parse_reads_only_backticked_titles_under_milestones() -> None:
    module = load()
    parsed = module.parse_direction(DIRECTION + "\n## Notes\n- `Not a milestone` here\n")
    assert parsed["milestones"] == ["Thin fork decision", "Dogfood week"]
    assert parsed["missing_headings"] == []


def test_missing_file_and_missing_heading() -> None:
    module = load()
    assert kinds(run(module, direction_text=None)) == ["direction_missing"]
    broken = DIRECTION.replace("## Retired", "## History")
    result = run(module, direction_text=broken)
    assert kinds(result) == ["direction_shape"]
    assert result["findings"][0]["headings"] == ["Retired"]


def test_unlisted_pending_and_foreign_creator() -> None:
    module = load()
    result = run(
        module,
        milestones=[
            milestone(1, "Thin fork decision"),
            milestone(3, "Agent invented this", creator="some-agent"),
            milestone(4, "Closed and gone", state="closed"),
            milestone(2, "Dogfood week", state="closed"),
        ],
    )
    found = {(item["kind"], item.get("title")) for item in result["findings"]}
    assert ("milestone_unlisted", "Agent invented this") in found
    assert ("milestone_creator", "Agent invented this") in found
    assert ("milestone_closed_listed", "Dogfood week") in found, "a shipped milestone still listed asks for line removal"
    assert ("milestone_pending", "Dogfood week") not in found, "a closed milestone must never be reported as not yet created"
    assert ("milestone_unlisted", "Closed and gone") not in found, "closed unlisted milestones are not drift"
    assert ("milestone_pending", "Thin fork decision") not in found


def test_configured_bot_logins_are_trusted_creators() -> None:
    module = load()
    milestones = [
        milestone(1, "Thin fork decision", creator="app[bot]"),
        milestone(2, "Dogfood week", creator="Legacy-Bot"),
        milestone(3, "Old phase", state="closed", creator="stranger"),
    ]
    result = run(module, milestones=milestones, automation="app[bot]", bot_logins=("legacy-bot",))
    creators = [item["creator"] for item in result["findings"] if item["kind"] == "milestone_creator"]
    assert creators == [], "closed milestones do not need creator trust; both open creators are trusted"
    result = run(module, milestones=milestones, automation="other", bot_logins=("legacy-bot",))
    creators = [item["creator"] for item in result["findings"] if item["kind"] == "milestone_creator"]
    assert creators == ["app[bot]"], "the automation override replaces the acting identity only for open milestones"


def test_milestone_creator_finding_applies_only_to_open_milestones() -> None:
    module = load()
    result = run(
        module,
        milestones=[
            milestone(1, "Open unknown creator", creator="stranger"),
            milestone(2, "Closed unknown creator", state="closed", creator="stranger"),
        ],
    )
    creator_titles = [item["title"] for item in result["findings"] if item["kind"] == "milestone_creator"]
    assert creator_titles == ["Open unknown creator"]


def test_listed_but_never_created_is_pending() -> None:
    module = load()
    result = run(module, milestones=[milestone(1, "Thin fork decision")])
    assert kinds(result) == ["milestone_pending"]
    assert result["findings"][0]["title"] == "Dogfood week"


def test_escalation_age_and_gate_phrases() -> None:
    module = load()
    result = run(
        module,
        issues=[
            issue(10, "Reviewer proposes retiring the policy engine", labels=("direction",), created="2026-09-14T12:00:00Z"),
            issue(11, "Plan", body="Opus/Gemini final planning findings are resolved before close."),
            issue(16, "Plan", body="- [ ] all findings resolved"),
            issue(12, "Plan", body="Finish line: Opus, Gemini Pro, and Sol independently approve the same final evidence bundle."),
            issue(13, "Plan", body="Both reviewers approve the plan before merge."),
            issue(14, "Plan", body="Reviewers may propose; findings are hypotheses and can be declined."),
            {"number": 15, "title": "PR with gate text", "body": "both reviewers approve", "labels": [], "pull_request": {}},
        ],
    )
    by_number = {item.get("number"): item for item in result["findings"]}
    assert by_number[10]["kind"] == "escalation_open" and by_number[10]["age_days"] == 7
    assert by_number[11]["phrases"] == ["all findings resolved"]
    assert by_number[16]["phrases"] == ["all findings resolved"], "the bare checklist literal must be caught"
    assert by_number[12]["phrases"] == ["named reviewer sign-off"]
    assert by_number[13]["phrases"] == ["both reviewers approve"]
    assert 14 not in by_number, "declinable findings are not a gate"
    assert 15 not in by_number, "pull requests are skipped"


def test_milestone_descriptions_are_checked_for_gate_phrases() -> None:
    module = load()
    result = run(module, milestones=[
        {**milestone(1, "Thin fork decision"), "description": "Closes when both reviewers approve the evidence bundle."},
        {**milestone(2, "Dogfood week"), "description": "Closes after seven days of installed use."},
    ])
    assert kinds(result) == ["gate_phrase"]
    assert result["findings"][0]["milestone"] == 1


def test_direction_pull_requests_and_truncation_are_reported() -> None:
    module = load()
    result = run(
        module,
        direction_pulls=[{"number": 40, "title": "Retire the policy engine", "created_at": "2026-09-19T12:00:00Z"}],
        truncated=["issues"],
    )
    assert kinds(result) == ["coverage_incomplete", "escalation_open"]
    assert result["ok"] is False
    assert result["findings"][1]["pull_request"] is True and result["findings"][1]["age_days"] == 2


def test_standard_rulesets_are_required_for_adopted_repositories() -> None:
    module = load()
    standard = [
        {"name": name, "target": "branch", "enforcement": "active"}
        for name in module.github_rulesets.required_ruleset_names()
    ]
    assert run(module, rulesets=standard)["ok"] is True

    result = run(module, rulesets=[standard[0]])
    assert kinds(result) == ["ruleset_missing"]
    assert result["findings"][0]["name"] == standard[1]["name"]

    disabled = [{**item, "enforcement": "disabled"} for item in standard]
    result = run(module, rulesets=disabled)
    assert kinds(result) == ["ruleset_missing", "ruleset_missing"]


def test_rulesets_are_not_required_before_direction_is_adopted() -> None:
    module = load()
    result = run(module, direction_text=None, rulesets=[])
    assert kinds(result) == ["direction_missing"]


def test_direction_pull_request_discovery_uses_label_or_changed_file() -> None:
    module = load()
    files = {
        1: [{"filename": "DIRECTION.md"}],
        2: [{"filename": "README.md"}],
    }

    def fetch(args: list[str]) -> Any:
        number = int(args[1].split("/pulls/")[1].split("/")[0])
        return files[number]

    pulls = [
        {"number": 1, "labels": []},
        {"number": 2, "labels": []},
        {"number": 3, "labels": [{"name": "direction"}]},
    ]
    matched = module.direction_pull_requests("o/r", pulls, fetch=fetch)
    assert [pull["number"] for pull in matched] == [1, 3]


def test_merged_direction_reads_the_default_branch_and_treats_404_as_not_adopted() -> None:
    import base64

    module = load()
    encoded = {"content": base64.b64encode(DIRECTION.encode()).decode(), "encoding": "base64"}
    assert module.merged_direction("o/r", fetch=lambda args: encoded) == DIRECTION

    def missing(_args: list[str]) -> Any:
        raise module.AuditError("contents read failed: HTTP 404: Not Found")

    assert module.merged_direction("o/r", fetch=missing) is None

    def broken(_args: list[str]) -> Any:
        raise module.AuditError("contents read failed: HTTP 502")

    try:
        module.merged_direction("o/r", fetch=broken)
    except module.AuditError:
        pass
    else:
        raise AssertionError("an unreadable file must not be reported as not adopted")


def test_fetch_paginated_follows_full_pages_and_flags_the_cap() -> None:
    module = load()
    pages = {1: [{"n": i} for i in range(100)], 2: [{"n": 100}]}
    calls: list[str] = []

    def fetch(args: list[str]) -> Any:
        calls.append(args[1])
        page = int(args[1].rsplit("page=", 1)[1])
        return pages.get(page, [])

    items, cut = module.fetch_paginated("repos/o/r/milestones?state=all", fetch=fetch)
    assert (len(items), cut) == (101, False)
    assert calls == ["repos/o/r/milestones?state=all&per_page=100&page=1", "repos/o/r/milestones?state=all&per_page=100&page=2"]

    full = lambda args: [{"n": 0}] * 100  # noqa: E731
    items, cut = module.fetch_paginated("repos/o/r/issues?state=open", fetch=full)
    assert (len(items), cut) == (100 * module.MAX_PAGES, True), "a listing that never shortens must report the cap"


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
