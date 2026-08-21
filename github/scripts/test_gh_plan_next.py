#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused regression tests for dependency-aware next-work selection."""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).with_name("gh-plan.py")


def load_module() -> Any:
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("gh_plan_next_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def issue(
    number: int,
    *,
    title: str | None = None,
    state: str = "open",
    labels: list[str] | None = None,
    milestone: dict[str, Any] | None = None,
    updated_at: str = "2026-08-21T00:00:00Z",
) -> dict[str, Any]:
    return {
        "repo": "owner/repo",
        "number": number,
        "title": title or f"Plan {number}",
        "state": state,
        "updated_at": updated_at,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "labels": [{"name": name} for name in (labels or ["plan", "plan:active"])],
        "milestone": milestone,
    }


def related(number: int, *, state: str = "open") -> dict[str, Any]:
    return {
        "repo": "owner/repo",
        "number": number,
        "title": f"Plan {number}",
        "state": state,
        "url": f"https://github.com/owner/repo/issues/{number}",
    }


def relationships(
    *,
    blocked_by: list[dict[str, Any]] | None = None,
    blocking: list[dict[str, Any]] | None = None,
    sub_issues: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    return {
        "blocked_by": blocked_by or [],
        "blocking": blocking or [],
        "sub_issues": sub_issues or [],
    }


def test_next_beta_rc_stable_chain_respects_native_blockers() -> None:
    module = load_module()
    config = module.DEFAULT_CONFIG
    beta = module.evaluate_next_plan(
        issue(1, title="Beta"),
        config=config,
        focus="Now",
        relationships=relationships(blocking=[related(2)]),
    )
    rc = module.evaluate_next_plan(
        issue(2, title="RC"),
        config=config,
        focus="Next",
        relationships=relationships(blocked_by=[related(1)], blocking=[related(3)]),
    )
    stable = module.evaluate_next_plan(
        issue(3, title="Stable"),
        config=config,
        focus="Waiting",
        relationships=relationships(blocked_by=[related(2)]),
    )

    assert beta[0] == "candidate"
    assert beta[1]["reasons"] == ["no_open_blockers", "focus_now", "unblocks_1_open_plan(s)"]
    assert rc[0] == "excluded"
    assert rc[1]["exclusion"] == "blocked_by_open_dependency"
    assert rc[1]["evidence"] == ["#1"]
    assert stable[1]["exclusion"] == "blocked_by_open_dependency"


def test_next_excludes_non_actionable_states_with_reasons() -> None:
    module = load_module()
    config = module.DEFAULT_CONFIG
    cases = [
        (issue(1, state="closed"), None, relationships(), "completed"),
        (issue(2, labels=["plan", "plan:done"]), None, relationships(), "completed"),
        (issue(3, labels=["plan", "plan:stale"]), None, relationships(), "stale_needs_review"),
        (issue(4, labels=["plan", "plan:waiting"]), None, relationships(), "waiting"),
        (issue(5), "Later", relationships(), "later_focus"),
        (issue(6, labels=["plan", "plan:blocked"]), None, relationships(), "label_blocked_without_native_edge"),
        (issue(7), None, relationships(sub_issues=[related(70)]), "delegated_to_open_sub_issues"),
    ]
    for plan, focus, plan_relationships, expected in cases:
        disposition, result = module.evaluate_next_plan(
            plan,
            config=config,
            focus=focus,
            relationships=plan_relationships,
        )
        assert disposition == "excluded"
        assert result["exclusion"] == expected

    _, unknown = module.evaluate_next_plan(
        issue(8),
        config=config,
        focus=None,
        relationships=None,
        relationship_error="dependency endpoint unavailable",
    )
    assert unknown["exclusion"] == "unknown_dependencies"


def test_next_closed_milestone_is_context_not_exclusion() -> None:
    module = load_module()
    plan = issue(
        9,
        milestone={
            "number": 3,
            "title": "Stable",
            "state": "closed",
            "due_on": "2026-09-01T00:00:00Z",
            "html_url": "https://github.com/owner/repo/milestone/3",
        },
    )
    disposition, result = module.evaluate_next_plan(
        plan,
        config=module.DEFAULT_CONFIG,
        focus="Next",
        relationships=relationships(),
    )
    assert disposition == "candidate"
    assert result["notes"] == ["milestone_closed_but_plan_open"]


def test_next_ranking_is_deterministic_and_dependency_aware() -> None:
    module = load_module()
    original = [
        {**issue(1, updated_at="2026-08-20T00:00:00Z"), "focus": "Next", "blocking": [related(10)], "milestone": None},
        {**issue(2, updated_at="2026-08-21T00:00:00Z"), "focus": "Now", "blocking": [], "milestone": None},
        {**issue(3, updated_at="2026-08-21T00:00:00Z"), "focus": "Next", "blocking": [related(11), related(12)], "milestone": None},
    ]
    expected = None
    for seed in range(5):
        candidates = [dict(item) for item in original]
        random.Random(seed).shuffle(candidates)
        module.rank_next_candidates(candidates)
        numbers = [item["number"] for item in candidates]
        expected = expected or numbers
        assert numbers == expected
    assert expected == [2, 3, 1]


def test_cmd_next_is_bounded_read_only_and_explainable() -> None:
    module = load_module()
    captured: dict[str, Any] = {}
    observed_query: dict[str, Any] = {}
    plans = [issue(1), issue(2), issue(3)]

    def fake_collect(
        _path: str,
        *,
        query: dict[str, Any],
        bucket: str,
        step_prefix: str,
        limit: int,
        issue_only: bool,
        **_kwargs: Any,
    ) -> tuple[str, list[dict[str, Any]]]:
        observed_query.update(query)
        assert bucket == "rest_core"
        assert step_prefix == "next_plan_issues"
        assert limit == 3
        assert issue_only is True
        return "automation-gh", plans

    def fake_relationships(
        _repo: str,
        number: int,
        *,
        include_blocking: bool,
    ) -> tuple[str, dict[str, list[dict[str, Any]]]]:
        assert include_blocking is True
        if number == 2:
            return "automation-gh", relationships(blocked_by=[related(1)])
        return "automation-gh", relationships()

    original_collect = module.collect_paged_rest_items
    original_focus = module.next_focus_context
    original_relationships = module.read_issue_relationships
    original_emit = module.emit
    module.collect_paged_rest_items = fake_collect
    module.next_focus_context = lambda _repo, _config: (
        "automation-gh",
        {plans[0]["html_url"]: "Now", plans[1]["html_url"]: "Next"},
        {"available": True},
    )
    module.read_issue_relationships = fake_relationships
    module.emit = captured.update
    try:
        module.cmd_next(
            type(
                "Args",
                (),
                {"repo": "owner/repo", "milestone": None, "limit": 1, "scan_limit": 2},
            )()
        )
    finally:
        module.collect_paged_rest_items = original_collect
        module.next_focus_context = original_focus
        module.read_issue_relationships = original_relationships
        module.emit = original_emit

    assert observed_query["state"] == "open"
    assert captured["truncated"] is True
    assert captured["candidate_count"] == 1
    assert [item["number"] for item in captured["candidates"]] == [1]
    assert captured["excluded"][0]["exclusion"] == "blocked_by_open_dependency"
    assert "scan_limit_truncated_open_plans" in captured["notes"]


def test_cmd_next_supports_milestone_scope_and_focus_degradation() -> None:
    module = load_module()
    captured: dict[str, Any] = {}
    observed_query: dict[str, Any] = {}
    scoped_plan = issue(10)

    def fake_collect(
        _path: str,
        *,
        query: dict[str, Any],
        **_kwargs: Any,
    ) -> tuple[str, list[dict[str, Any]]]:
        observed_query.update(query)
        return "automation-gh", [scoped_plan]

    original_collect = module.collect_paged_rest_items
    original_focus = module.next_focus_context
    original_relationships = module.read_issue_relationships
    original_emit = module.emit
    original_route = module.milestone_route
    original_show = module.github_milestone_core.show_milestone
    module.collect_paged_rest_items = fake_collect
    module.next_focus_context = lambda _repo, _config: (
        None,
        {},
        {"available": False, "reason": "project_focus_unavailable"},
    )
    module.read_issue_relationships = lambda *_args, **_kwargs: (
        "automation-gh",
        relationships(),
    )
    module.milestone_route = lambda: ("gh", "automation-gh")
    module.github_milestone_core.show_milestone = lambda *_args, **_kwargs: {
        "actor": "automation-gh",
        "milestone": {
            "number": 7,
            "title": "Release 7",
            "state": "open",
            "due_on": "2026-09-01T00:00:00Z",
        },
    }
    module.emit = captured.update
    try:
        module.cmd_next(
            type(
                "Args",
                (),
                {"repo": "owner/repo", "milestone": "Release 7", "limit": 5, "scan_limit": 5},
            )()
        )
    finally:
        module.collect_paged_rest_items = original_collect
        module.next_focus_context = original_focus
        module.read_issue_relationships = original_relationships
        module.emit = original_emit
        module.milestone_route = original_route
        module.github_milestone_core.show_milestone = original_show

    assert observed_query["milestone"] == 7
    assert captured["scope"]["kind"] == "milestone"
    assert captured["scope"]["milestone"]["title"] == "Release 7"
    assert "project_focus_unavailable" in captured["notes"]


TESTS = [
    test_next_beta_rc_stable_chain_respects_native_blockers,
    test_next_excludes_non_actionable_states_with_reasons,
    test_next_closed_milestone_is_context_not_exclusion,
    test_next_ranking_is_deterministic_and_dependency_aware,
    test_cmd_next_is_bounded_read_only_and_explainable,
    test_cmd_next_supports_milestone_scope_and_focus_degradation,
]


def main() -> None:
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
