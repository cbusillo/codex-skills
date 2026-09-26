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
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("gh-plan.py")
DIRECTION = """# Direction

## Milestones

- `First`
- `Second`
"""


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
    created_at: str = "2026-08-01T00:00:00Z",
    updated_at: str = "2026-08-21T00:00:00Z",
) -> dict[str, Any]:
    return {
        "repo": "owner/repo",
        "number": number,
        "title": title or f"Plan {number}",
        "state": state,
        "created_at": created_at,
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


def milestone_data(
    number: int,
    title: str,
    *,
    created_at: str,
    state: str = "open",
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "state": state,
        "created_at": created_at,
        "html_url": f"https://github.com/owner/repo/milestone/{number}",
    }


def save_next_helpers(module: Any) -> dict[str, Any]:
    return {
        name: getattr(module, name)
        for name in (
            "collect_paged_rest_items",
            "next_focus_context",
            "read_next_issue_relationships",
            "load_direction",
            "emit",
        )
    }


def restore_next_helpers(module: Any, originals: dict[str, Any]) -> None:
    for name, value in originals.items():
        setattr(module, name, value)


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


def test_next_ranking_uses_direction_then_dependencies_then_oldest_created() -> None:
    module = load_module()
    original = [
        {
            **issue(1, created_at="2026-07-01T00:00:00Z", updated_at="2026-09-30T00:00:00Z"),
            "focus": "Now",
            "blocking": [related(10), related(11), related(12)],
            "milestone": None,
        },
        {
            **issue(2, created_at="2026-08-10T00:00:00Z", updated_at="2026-09-29T00:00:00Z"),
            "focus": "Next",
            "blocking": [related(20)],
            "milestone": milestone_data(1, "First", created_at="2026-07-10T00:00:00Z"),
        },
        {
            **issue(3, created_at="2026-07-20T00:00:00Z"),
            "focus": None,
            "blocking": [],
            "milestone": milestone_data(2, "Second", created_at="2026-07-01T00:00:00Z"),
        },
        {
            **issue(4),
            "focus": None,
            "blocking": [],
            "milestone": milestone_data(1, "First", created_at="2026-07-10T00:00:00Z"),
        },
    ]
    expected = None
    for seed in range(5):
        candidates = [dict(item) for item in original]
        random.Random(seed).shuffle(candidates)
        module.rank_next_candidates(candidates, direction_milestones=["First", "Second"])
        numbers = [item["number"] for item in candidates]
        expected = expected or numbers
        assert numbers == expected
    assert expected == [2, 4, 3, 1]


def test_next_ranking_without_direction_uses_milestone_creation_order() -> None:
    module = load_module()
    candidates = [
        {**issue(1, created_at="2026-07-01T00:00:00Z"), "blocking": [], "milestone": None},
        {
            **issue(2),
            "blocking": [],
            "milestone": milestone_data(2, "Newer milestone", created_at="2026-07-01T00:00:00Z"),
        },
        {
            **issue(3, created_at="2026-08-02T00:00:00Z"),
            "blocking": [],
            "milestone": milestone_data(1, "Older milestone", created_at="2026-07-01T00:00:00Z"),
        },
    ]
    module.rank_next_candidates(candidates)
    assert [item["number"] for item in candidates] == [3, 2, 1]


def test_next_focus_context_normalizes_keys_and_reports_truncation() -> None:
    module = load_module()
    config = {
        **module.DEFAULT_CONFIG,
        "projects": {"enabled": True, "owner": "owner", "default_project": "4"},
        "project_fields": {"focus": "Next Up"},
    }
    items = [
        {
            "content": {"url": f"https://github.com/owner/repo/issues/{number}"},
            "nextUp": "Later" if number == 1 else "Next",
        }
        for number in range(1, module.NEXT_PROJECT_ITEM_LIMIT + 2)
    ]

    original_meta = module.project_meta
    original_items = module.project_items
    module.project_meta = lambda *_args, **_kwargs: (
        "automation-gh",
        4,
        {"title": "Roadmap"},
    )

    def fake_items(*_args: Any, limit: int, **_kwargs: Any) -> list[dict[str, Any]]:
        assert limit == module.NEXT_PROJECT_ITEM_LIMIT + 1
        return items

    module.project_items = fake_items
    try:
        actor, focus_by_url, context = module.next_focus_context("owner/repo", config)
    finally:
        module.project_meta = original_meta
        module.project_items = original_items

    assert actor == "automation-gh"
    assert focus_by_url["https://github.com/owner/repo/issues/1"] == "Later"
    assert len(focus_by_url) == module.NEXT_PROJECT_ITEM_LIMIT
    assert context["truncated"] is True
    assert context["item_limit"] == module.NEXT_PROJECT_ITEM_LIMIT


def test_cmd_next_is_bounded_read_only_and_explainable() -> None:
    module = load_module()
    captured: dict[str, Any] = {}
    observed_query: dict[str, Any] = {}
    plans = [
        issue(1, created_at="2026-07-01T00:00:00Z"),
        issue(2, created_at="2026-07-02T00:00:00Z"),
        issue(
            3,
            created_at="2026-09-01T00:00:00Z",
            milestone=milestone_data(1, "First", created_at="2026-07-10T00:00:00Z"),
        ),
    ]

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
        assert limit == module.NEXT_PLAN_INVENTORY_LIMIT + 1
        assert issue_only is True
        return "automation-gh", plans

    def fake_relationships(
        _repo: str,
        number: int,
    ) -> tuple[str, dict[str, list[dict[str, Any]]], list[str]]:
        if number == 1:
            return "automation-gh", relationships(blocked_by=[related(10)]), []
        return "automation-gh", relationships(), []

    originals = save_next_helpers(module)
    module.collect_paged_rest_items = fake_collect
    module.next_focus_context = lambda _repo, _config: (
        "automation-gh",
        {plans[0]["html_url"]: "Now", plans[1]["html_url"]: "Next"},
        {"available": True},
    )
    module.read_next_issue_relationships = fake_relationships
    module.load_direction = lambda _repo: DIRECTION
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
        restore_next_helpers(module, originals)

    assert observed_query["state"] == "open"
    assert observed_query["sort"] == "created"
    assert observed_query["direction"] == "asc"
    assert captured["truncated"] is True
    assert captured["candidate_count"] == 1
    assert [item["number"] for item in captured["candidates"]] == [3]
    assert captured["excluded"][0]["exclusion"] == "blocked_by_open_dependency"
    assert captured["excluded"][0]["number"] == 1
    assert "scan_limit_truncated_prioritized_plans" in captured["notes"]
    assert captured["dependency_context"]["complete"] is True


def test_cmd_next_degrades_when_direction_is_unavailable_or_unparsed() -> None:
    module = load_module()
    captured: dict[str, Any] = {}
    plan = issue(
        1,
        milestone=milestone_data(9, "Unlisted", created_at="2026-07-01T00:00:00Z"),
    )
    originals = save_next_helpers(module)
    module.collect_paged_rest_items = lambda *_args, **_kwargs: ("automation-gh", [plan])
    module.next_focus_context = lambda *_args, **_kwargs: (
        None,
        {},
        {"available": False, "reason": "project_not_configured"},
    )
    module.read_next_issue_relationships = lambda *_args, **_kwargs: (
        "automation-gh",
        relationships(),
        [],
    )
    module.emit = captured.update
    try:
        module.load_direction = lambda _repo: (_ for _ in ()).throw(module.PlanError("temporary 502"))
        module.cmd_next(
            type("Args", (), {"repo": "owner/repo", "milestone": None, "limit": 5, "scan_limit": 5})()
        )
        assert "direction_unavailable" in captured["notes"]
        assert captured["milestone_order"]["source"] == "milestone_created_at"
        assert captured["milestone_order"]["error"] == "temporary 502"

        captured.clear()
        module.load_direction = lambda _repo: "# Direction\n\n## Milestones\n\n- Unparseable title\n"
        module.cmd_next(
            type("Args", (), {"repo": "owner/repo", "milestone": None, "limit": 5, "scan_limit": 5})()
        )
        assert "direction_milestones_unparsed" in captured["notes"]
        assert captured["milestone_order"]["source"] == "milestone_created_at"

        captured.clear()
        module.load_direction = lambda _repo: DIRECTION
        module.cmd_next(
            type("Args", (), {"repo": "owner/repo", "milestone": None, "limit": 5, "scan_limit": 5})()
        )
        assert captured["candidates"][0]["notes"] == ["milestone_unlisted_from_direction"]
    finally:
        restore_next_helpers(module, originals)


def test_cmd_next_surfaces_dependency_degradation_and_skips_cheap_exclusions() -> None:
    module = load_module()
    captured: dict[str, Any] = {}
    plans = [issue(1), issue(2, labels=["plan", "plan:stale"])]
    relationship_calls: list[int] = []

    originals = save_next_helpers(module)
    module.collect_paged_rest_items = lambda *_args, **_kwargs: ("automation-gh", plans)
    module.next_focus_context = lambda _repo, _config: (
        "automation-gh",
        {},
        {"available": False, "reason": "project_not_configured"},
    )

    def fake_relationships(_repo: str, number: int) -> Any:
        relationship_calls.append(number)
        raise module.PlanError("dependency endpoint unavailable")

    module.read_next_issue_relationships = fake_relationships
    module.load_direction = lambda _repo: DIRECTION
    module.emit = captured.update
    try:
        module.cmd_next(
            type(
                "Args",
                (),
                {"repo": "owner/repo", "milestone": None, "limit": 5, "scan_limit": 5},
            )()
        )
    finally:
        restore_next_helpers(module, originals)

    assert relationship_calls == [1]
    assert [item["exclusion"] for item in captured["excluded"]] == [
        "unknown_dependencies",
        "stale_needs_review",
    ]
    assert captured["dependency_context"]["complete"] is False
    assert captured["dependency_context"]["degraded_count"] == 1
    assert "dependency_reads_degraded" in captured["notes"]


def test_cmd_next_excludes_truncated_dependencies_with_only_closed_visible_blockers() -> None:
    module = load_module()
    captured: dict[str, Any] = {}
    visible_blockers = [
        related(number, state="closed")
        for number in range(2, module.NEXT_RELATIONSHIP_LIMIT + 2)
    ]

    originals = save_next_helpers(module)
    module.collect_paged_rest_items = lambda *_args, **_kwargs: (
        "automation-gh", [issue(1)],
    )
    module.next_focus_context = lambda _repo, _config: (
        "automation-gh",
        {},
        {"available": False, "reason": "project_not_configured"},
    )
    module.read_next_issue_relationships = lambda *_args, **_kwargs: (
        "automation-gh",
        relationships(blocked_by=visible_blockers),
        ["blocked_by"],
    )
    module.load_direction = lambda _repo: DIRECTION
    module.emit = captured.update
    try:
        module.cmd_next(
            type(
                "Args",
                (),
                {"repo": "owner/repo", "milestone": None, "limit": 5, "scan_limit": 5},
            )()
        )
    finally:
        restore_next_helpers(module, originals)

    assert captured["candidate_count"] == 0
    assert captured["candidates"] == []
    assert len(captured["excluded"]) == 1
    assert captured["excluded"][0]["number"] == 1
    assert captured["excluded"][0]["exclusion"] == "unknown_dependencies"
    assert captured["excluded"][0]["truncated_relationships"] == ["blocked_by"]
    assert captured["dependency_context"]["complete"] is False
    assert captured["dependency_context"]["degraded_count"] == 1
    assert "dependency_reads_degraded" in captured["notes"]


def test_cmd_next_reraises_dependency_api_failures() -> None:
    module = load_module()
    originals = save_next_helpers(module)
    module.collect_paged_rest_items = lambda *_args, **_kwargs: (
        "automation-gh",
        [issue(1)],
    )
    module.next_focus_context = lambda _repo, _config: (
        "automation-gh",
        {},
        {"available": False, "reason": "project_not_configured"},
    )
    failure = module.github_api_core.FailureDetail(
        cause="rate_limited",
        message="retry later",
        retryable=True,
        fallback_eligible=False,
        disposition="retry",
    )
    module.read_next_issue_relationships = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        module.PlanError("retry later", failure=failure)
    )
    module.load_direction = lambda _repo: DIRECTION
    try:
        try:
            module.cmd_next(
                type(
                    "Args",
                    (),
                    {"repo": "owner/repo", "milestone": None, "limit": 5, "scan_limit": 5},
                )()
            )
        except module.PlanError as exc:
            assert exc.failure is not None
            assert exc.failure.cause == "rate_limited"
        else:
            raise AssertionError("expected classified dependency failure")
    finally:
        restore_next_helpers(module, originals)


def test_next_relationship_reads_are_bounded_and_report_truncation() -> None:
    module = load_module()
    observed_limits: list[int] = []

    def fake_collect(path: str, *, limit: int, **_kwargs: Any) -> tuple[str, list[dict[str, Any]]]:
        observed_limits.append(limit)
        if path.endswith("/blocked_by"):
            return "automation-gh", [related(number) for number in range(1, limit + 1)]
        return "automation-gh", []

    original_collect = module.collect_paged_rest_items
    module.collect_paged_rest_items = fake_collect
    try:
        actor, plan_relationships, truncated = module.read_next_issue_relationships(
            "owner/repo",
            1,
        )
    finally:
        module.collect_paged_rest_items = original_collect

    assert actor == "automation-gh"
    assert observed_limits == [module.NEXT_RELATIONSHIP_LIMIT + 1] * 3
    assert len(plan_relationships["blocked_by"]) == module.NEXT_RELATIONSHIP_LIMIT
    assert truncated == ["blocked_by"]


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

    originals = save_next_helpers(module)
    original_route = module.milestone_route
    original_show = module.github_milestone_core.show_milestone
    module.collect_paged_rest_items = fake_collect
    module.next_focus_context = lambda _repo, _config: (
        None,
        {},
        {"available": False, "reason": "project_focus_unavailable"},
    )
    module.read_next_issue_relationships = lambda *_args, **_kwargs: (
        "automation-gh",
        relationships(),
        [],
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
        restore_next_helpers(module, originals)
        module.milestone_route = original_route
        module.github_milestone_core.show_milestone = original_show

    assert observed_query["milestone"] == 7
    assert captured["scope"]["kind"] == "milestone"
    assert captured["scope"]["milestone"]["title"] == "Release 7"
    assert "project_focus_unavailable" in captured["notes"]


def global_issue(repo: str, number: int, *, body: str = "", **kwargs: Any) -> dict[str, Any]:
    return {**issue(number, **kwargs), "repo": repo, "html_url": f"https://github.com/{repo}/issues/{number}", "body": body}


def track(repo: str, number: int, title: str) -> dict[str, Any]:
    return global_issue(
        repo, number, title=f"Track: {title}", labels=["plan", "plan:waiting"],
        milestone=milestone_data(number, title, created_at="2026-01-01T00:00:00Z"),
    )


@contextmanager
def global_fixture(
    roots: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    edges: dict[tuple[str, int], dict[str, list[dict[str, Any]]]],
    *,
    repo: str = "someone/direction",
    discovered: list[dict[str, Any]] | None = None,
    comments: dict[tuple[str, int], list[dict[str, Any]]] | None = None,
) -> Any:
    module = load_module()
    captured: dict[str, Any] = {}
    reads: list[tuple[str, int]] = []
    by_key = {(node["repo"], node["number"]): node for node in nodes}
    all_nodes = {(node["repo"], node["number"]): node for node in [*roots, *nodes, *(discovered or [])]}
    parent_map = {(child["repo"].casefold(), child["number"]): all_nodes[key]
                  for key, values in edges.items() for child in values["sub_issues"]}

    def get_node(ref: str, target_repo: str) -> Any:
        key = (target_repo, int(ref))
        reads.append(key)
        if key not in by_key:
            raise module.PlanError("not accessible")
        return "automation-gh", by_key[key]

    def collect(path: str, **kwargs: Any) -> Any:
        if path.endswith("/comments"):
            parts = path.split("/")
            return "automation-gh", (comments or {}).get(("/".join(parts[2:4]), int(parts[5])), [])
        assert path == f"/repos/{repo}/issues"
        assert kwargs["issue_only"] is True
        assert kwargs["query"]["state"] == "open"
        assert kwargs["limit"] == module.NEXT_PLAN_INVENTORY_LIMIT + 1
        return "automation-gh", roots

    def read_relationships(target_repo: str, number: int) -> Any:
        return "automation-gh", {
            name: [module.compact_relationship_issue(node, name) for node in values]
            for name, values in edges.get((target_repo, number), relationships()).items()
        }, []

    with patch.multiple(
        module,
        default_repo=lambda explicit: explicit or repo,
        load_config=lambda *_: module.DEFAULT_CONFIG,
        load_direction=lambda *_: DIRECTION,
        collect_paged_rest_items=collect,
        discover_direction_work=lambda *_args, **_kwargs: (discovered or [], {"complete": True, "repositories": []}),
        next_focus_context=lambda *_: (None, {}, {"available": False, "reason": "project_not_configured"}),
        read_next_issue_relationships=read_relationships,
        read_next_parent=lambda target, number: parent_map.get((target.casefold(), number)),
        get_issue=get_node,
        milestone_route=lambda: ("gh", "automation-gh"),
        emit=captured.update,
    ), patch.multiple(module.github_milestone_core, list_milestones=lambda *_args, **_kwargs: {"milestones": [root["milestone"] for root in roots if root.get("milestone")]}):
        yield module, captured, reads


def next_args(*, repo: str | None = "someone/direction", scan_limit: int = 50, milestone: str | None = None) -> Any:
    return type("Args", (), {"repo": repo, "milestone": milestone, "limit": 10, "scan_limit": scan_limit})()


def test_global_next_follows_cross_owner_blockers_and_reports_partial_waits() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    parent = global_issue("someone/site", 91, body="## Current Status\n\nState: Active.\nWaiting for: Justin's owner review of #92 remains pending. Independent backup work continues.")
    backup = global_issue("another/platform", 2309, labels=["plan", "plan:blocked"])
    provider = global_issue("another/platform", 2307, created_at="2026-07-01T00:00:00Z")
    later = global_issue("someone/product", 8, created_at="2025-01-01T00:00:00Z")
    edges = {
        ("someone/direction", 1): relationships(sub_issues=[parent]),
        ("someone/direction", 2): relationships(sub_issues=[later]),
        ("someone/site", 91): relationships(blocked_by=[backup]),
        ("another/platform", 2309): relationships(blocked_by=[provider]),
        ("another/platform", 2307): relationships(blocking=[backup]),
    }
    with global_fixture(roots, [parent, backup, provider, later], edges) as (module, result, reads):
        module.cmd_next(next_args())
    assert [(node["repo"], node["number"]) for node in result["candidates"]] == [("another/platform", 2307), ("someone/product", 8)]
    first = result["candidates"][0]
    assert first["milestone"]["title"] == "First"
    assert [node["number"] for node in first["via"]] == [1, 91, 2309, 2307]
    assert first["via"][-1]["relationship"] == "blocked_by"
    assert first["reasons"][0] == "direction_milestone_1"
    assert first["issue_milestone"] is None
    assert result["waiting"][0]["repo"] == "someone/site"
    assert result["waiting"][0]["number"] == 92
    assert "Justin" in result["waiting"][0]["waiting_for"]
    assert result["waiting"][0]["reported_by"].endswith("/issues/91")
    assert result["dependency_context"]["complete"] is True
    assert len(reads) == len(set(reads)) == 4


def test_global_waiting_milestone_hands_off_to_next_not_unlinked_tooling() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second"), global_issue("someone/direction", 3, title="Improve tools")]
    waiting = global_issue("someone/product", 10, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Customer testing.")
    ready = global_issue("someone/product", 11)
    edges = {
        ("someone/direction", 1): relationships(sub_issues=[waiting]),
        ("someone/direction", 2): relationships(sub_issues=[ready]),
    }
    with global_fixture(roots, [waiting, ready], edges) as (module, result, _reads):
        module.cmd_next(next_args())
    assert [node["number"] for node in result["candidates"]] == [11]
    assert result["waiting"][0]["waiting_for"] == "Customer testing."
    assert any(node.get("number") == 3 and node["exclusion"] == "outside_direction_tracks" for node in result["excluded"])


def test_global_next_implicit_repository_and_explicit_repository_match() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/project", 3)
    edges = {("someone/direction", 1): relationships(sub_issues=[leaf])}
    with global_fixture(roots, [leaf], edges) as (module, result, _reads):
        module.cmd_next(next_args(repo=None))
        implicit = dict(result)
        result.clear()
        module.cmd_next(next_args())
        assert result == implicit
        assert result["scope"]["kind"] == "direction"
        assert result["scope"]["owner"] == "someone"
    assert module.github_direction_next.is_direction_repository("Other/Direction")
    assert not module.github_direction_next.is_direction_repository("someone/product")
    assert not module.github_direction_next.is_direction_repository("direction")


def test_global_cycles_unreadable_edges_and_scan_limits_are_incomplete() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    a = global_issue("someone/product", 3)
    b = global_issue("someone/product", 4)
    missing = global_issue("elsewhere/private", 5)
    edges = {
        ("someone/direction", 1): relationships(sub_issues=[a, missing]),
        ("someone/product", 3): relationships(blocked_by=[b]),
        ("someone/product", 4): relationships(blocked_by=[a]),
    }
    with global_fixture(roots, [a, b], edges) as (module, result, _reads):
        module.cmd_next(next_args())
        assert result["candidates"] == []
        assert result["dependency_context"]["complete"] is False
        assert result["dependency_context"]["degraded_count"] == 2
        assert {node["exclusion"] for node in result["excluded"]} >= {"dependency_cycle", "unknown_dependencies"}
        result.clear()
        module.cmd_next(next_args(scan_limit=2))
        assert result["truncated"] is True
        assert result["evaluated"] == 2
        assert result["dependency_context"]["complete"] is False


def test_global_shared_leaf_is_read_once_and_keeps_earliest_milestone() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/tooling", 3, milestone=milestone_data(8, "Product milestone", created_at="2025-01-01T00:00:00Z"))
    edges = {(root["repo"], root["number"]): relationships(sub_issues=[leaf]) for root in roots}
    with global_fixture(roots, [leaf], edges) as (module, result, reads):
        module.cmd_next(next_args())
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["milestone"]["title"] == "First"
    assert result["candidates"][0]["issue_milestone"]["title"] == "Product milestone"
    assert reads == [("someone/tooling", 3)]


def test_global_missing_direction_refuses_instead_of_local_fallback() -> None:
    with global_fixture([], [], {}) as (module, result, _reads):
        for text in (None, "# Direction\n\nNo ordered milestones"):
            module.load_direction = lambda *_: text
            try:
                module.cmd_next(next_args())
            except module.PlanError as exc:
                assert "ordered milestones" in str(exc)
            else:
                raise AssertionError("global ranking must not guess a milestone order")
        assert not result


def test_global_missing_tracks_and_inventory_truncation_remain_visible() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    with global_fixture(roots, [], {}) as (module, result, _reads):
        module.NEXT_PLAN_INVENTORY_LIMIT = 1
        module.cmd_next(next_args())
        assert result["truncated"] is True
        assert result["dependency_context"]["complete"] is False
        assert result["dependency_context"]["missing_tracking_milestones"] == ["Second"]
        assert result["candidates"] == []


def test_global_relationship_truncation_and_permissions_do_not_create_candidates() -> None:
    roots = [track("someone/direction", 1, "First")]
    with global_fixture(roots, [], {}) as (module, result, _reads):
        module.read_next_issue_relationships = lambda *_: ("automation-gh", relationships(), ["blocked_by"])
        module.cmd_next(next_args())
        assert result["excluded"][0]["truncated_relationships"] == ["blocked_by"]
        assert result["candidates"] == []
        for cause in ("not_found", "permission_denied", "rest_primary_rate_limited"):
            failure = module.github_api_core.FailureDetail(cause=cause, message="cannot read", retryable=False, fallback_eligible=False, disposition="stop")
            module.read_next_issue_relationships = lambda *_: (_ for _ in ()).throw(module.PlanError("cannot read", failure=failure))
            result.clear()
            try:
                module.cmd_next(next_args())
            except module.PlanError:
                assert cause == "rest_primary_rate_limited"
            else:
                assert cause != "rest_primary_rate_limited"
                assert result["candidates"] == []
                assert result["dependency_context"]["degraded_count"] == 1


def test_global_waits_use_current_status_and_never_reclassify_mentioned_work() -> None:
    roots = [track("someone/direction", 1, "First")]
    waiting = global_issue("someone/product", 3, body="## Objective\nWaiting for: Old decision.\n## Current Status\nState: Waiting.\nWaiting for: Alex to test [PR](https://github.com/other/product/pull/42).")
    edges = {("someone/direction", 1): relationships(sub_issues=[waiting])}
    with global_fixture(roots, [waiting], edges) as (module, result, reads):
        module.cmd_next(next_args())
    assert result["candidates"] == []
    assert result["waiting"][0]["repo"] == "other/product"
    assert result["waiting"][0]["number"] == 42
    assert result["waiting"][0]["url"].endswith("/pull/42")
    assert "Old" not in result["waiting"][0]["waiting_for"]
    assert reads == [("someone/product", 3)]


def test_global_milestone_scope_and_direction_order_capacity_context() -> None:
    roots = [track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/product", 3)
    edges = {("someone/direction", 2): relationships(sub_issues=[leaf])}
    with global_fixture(roots, [leaf], edges) as (module, result, _reads):
        module.load_direction = lambda *_: DIRECTION + "\n## Order\n\nLive breakage first; other tooling needs two linked stops.\n\n## Capacity\n\nAt least 20% of weekly merged PRs are own projects; audit, not quota.\n"
        with patch.multiple(module.github_milestone_core, show_milestone=lambda *_args, **_kwargs: {"milestone": roots[0]["milestone"]}):
            module.cmd_next(next_args(milestone="Second"))
        assert result["milestone_order"]["titles"] == ["Second"]
        assert result["scope"]["milestone"]["title"] == "Second"
        assert result["dependency_context"]["missing_tracking_milestones"] == []
        assert "Live breakage first" in result["direction_context"]["Order"]
        assert "20%" in result["direction_context"]["Capacity"]


def test_global_whole_parent_wait_does_not_select_its_children_or_dependencies() -> None:
    roots = [track("someone/direction", 1, "First")]
    parent = global_issue("someone/product", 3)
    leaf = global_issue("someone/product", 4)
    edges = {
        ("someone/direction", 1): relationships(sub_issues=[parent]),
        ("someone/product", 3): relationships(sub_issues=[leaf], blocked_by=[leaf]),
    }
    for body, labels in [
        ("## Current Status\nState: Waiting.\nWaiting for: Customer's sequencing decision.", ["plan", "plan:active"]),
        ("", ["plan", "plan:waiting"]),
    ]:
        parent["body"] = body
        parent["labels"] = labels
        with global_fixture(roots, [parent, leaf], edges) as (module, result, reads):
            module.cmd_next(next_args())
        assert result["candidates"] == []
        assert reads == [("someone/product", 3)]
        assert any(node.get("number") == 3 and node["exclusion"] == "waiting" for node in result["excluded"])


def test_global_leaf_waiting_on_referenced_review_is_not_actionable() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/site", 91, body="## Current Status\nState: Active.\nWaiting for: Justin's owner review of #92 remains pending.")
    edges = {("someone/direction", 1): relationships(sub_issues=[leaf])}
    with global_fixture(roots, [leaf], edges) as (module, result, _reads):
        module.cmd_next(next_args())
    assert result["candidates"] == []
    assert result["waiting"][0]["number"] == 92


def test_global_closed_milestone_is_completed_not_missing() -> None:
    roots = [track("someone/direction", 2, "Second")]
    closed = milestone_data(1, "First", state="closed", created_at="2026-01-01T00:00:00Z")
    with global_fixture(roots, [], {}) as (module, result, _reads):
        with patch.multiple(module.github_milestone_core, list_milestones=lambda *_args, **_kwargs: {"milestones": [closed, roots[0]["milestone"]]}):
            module.cmd_next(next_args())
    assert result["dependency_context"]["complete"] is True
    assert result["completed_milestones"] == ["First"]


def test_global_inconsistent_blocked_parent_makes_evidence_incomplete() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    parent = global_issue("someone/product", 3, labels=["plan", "plan:blocked"])
    leaf = global_issue("someone/product", 4)
    edges = {
        ("someone/direction", 1): relationships(sub_issues=[parent]),
        ("someone/product", 3): relationships(sub_issues=[leaf]),
    }
    with global_fixture(roots, [parent, leaf], edges) as (module, result, _reads):
        module.cmd_next(next_args())
    assert result["dependency_context"]["complete"] is False
    assert result["dependency_context"]["degraded_count"] == 1


def test_global_empty_tracks_are_not_reported_as_people_waits() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    with global_fixture(roots, [], {}) as (module, result, _reads):
        module.cmd_next(next_args())
    assert result["waiting"] == []
    assert all(item["exclusion"] == "tracking_without_open_work" for item in result["excluded"])


def test_global_service_consumer_uses_the_same_classification_and_sort_as_cli() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    ready = global_issue("other/project", 3)
    waiting = global_issue("someone/site", 91, body="## Current Status\nState: Active.\nWaiting for: Justin's review of #92.\n<!-- operation marker -->")
    edges = {("someone/direction", 1): relationships(sub_issues=[waiting, ready])}
    with global_fixture(roots, [ready, waiting], edges) as (module, result, _reads):
        module.cmd_next(next_args())
        shared = module.github_direction_next
        nodes = {(item["repo"], item["number"]): item for item in roots + [ready, waiting]}

        def service_reader(repo: str, number: int) -> dict[str, Any]:
            node = nodes[(repo, number)]
            relations = {
                name: [module.compact_relationship_issue(item, name) for item in items]
                for name, items in edges.get((repo, number), relationships()).items()
            }
            classified = shared.evaluate_direction_node(node, config=module.DEFAULT_CONFIG, focus=None, relationships=relations)
            classified["item"]["discussion"] = shared.discussion_snapshot(node, [], complete=True)
            return classified

        ranked = shared.rank_direction_work(
            [{**shared.compact_list_issue(item["repo"], item), "milestone": shared.next_milestone_context(item)} for item in roots],
            milestone_titles=["First", "Second"], read_node=service_reader, scan_limit=50,
        )
        ranked.update(shared.rank_portfolio_work(ranked, [], milestone_titles=["First", "Second"]))
        for field in ("candidates", "waiting", "excluded"):
            assert ranked[field] == result[field]
        assert [item["number"] for item in ranked["candidates"]] == [3]
        assert ranked["waiting"][0]["number"] == 92


def test_global_placeholder_waits_do_not_park_actionable_work() -> None:
    roots = [track("someone/direction", 1, "First")]
    for value in ("None.", "n/a", "nothing", "-"):
        leaf = global_issue("someone/product", 3, body="## Current Status\nState: Active.\nWaiting for: " + value)
        edges = {("someone/direction", 1): relationships(sub_issues=[leaf])}
        with global_fixture(roots, [leaf], edges) as (module, result, _reads):
            module.cmd_next(next_args())
        assert [item["number"] for item in result["candidates"]] == [3]
        assert result["waiting"] == []


def test_global_active_parent_partial_wait_need_not_name_an_issue() -> None:
    roots = [track("someone/direction", 1, "First")]
    leaf = global_issue("someone/platform", 3)
    for reason in ("Justin to point DNS at the new host.", "Justin's review. See #92."):
        parent = global_issue("someone/site", 91, body="## Current Status\nState: Active.\nWaiting for: " + reason)
        edges = {
            ("someone/direction", 1): relationships(sub_issues=[parent]),
            ("someone/site", 91): relationships(blocked_by=[leaf]),
        }
        with global_fixture(roots, [parent, leaf], edges) as (module, result, _reads):
            module.cmd_next(next_args())
        assert [item["number"] for item in result["candidates"]] == [3]
        assert result["waiting"][0]["waiting_for"] == reason
        if "#92" in reason:
            assert result["waiting"][0]["number"] == 92


def test_global_shared_classifier_preserves_incomplete_relationship_evidence() -> None:
    module = load_module()
    shared = module.github_direction_next
    leaf = global_issue("someone/product", 3)
    for kwargs in ({"truncated_relationships": ["blocked_by"]}, {"relationship_error": "Not accessible"}):
        node = shared.evaluate_direction_node(leaf, config=module.DEFAULT_CONFIG, focus=None, relationships=relationships(), **kwargs)
        assert node["item"]["exclusion"] == "unknown_dependencies"
        if "truncated_relationships" in kwargs:
            assert node["item"]["truncated_relationships"] == ["blocked_by"]
        root = track("someone/direction", 1, "First")
        root["url"] = root["html_url"]
        result = shared.rank_direction_work([root], milestone_titles=["First"], read_node=lambda *_: node, scan_limit=50)
        assert result["candidates"] == []
        assert result["dependency_context"]["complete"] is False


def reviewed(item: dict[str, Any], state: str = "available", category: str = "own_project", **extra: Any) -> dict[str, Any]:
    return {
        "state": state, "category": category, "reason": "Current evidence reviewed",
        "evidence": ["current issue discussion and supported worker inventory"],
        "discussion_digest": item["discussion"]["digest"], "ownership_complete": True, **extra,
    }


def test_portfolio_empty_business_graph_discovers_available_own_project() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    own = global_issue("someone/context-panel", 42, body="## Objective\nImprove the viewer.")
    with global_fixture(roots, [], {}, discovered=[own]) as (module, result, _reads):
        module.cmd_next(next_args())
        assert result["graph_context"]["complete"] is True
        assert result["candidate_count"] == 1
        assert result["available_candidate_count"] == 0
        candidate = result["candidates"][0]
        assert candidate["source"] == "repository_discovery"
        assert candidate["discussion"]["body"] == own["body"]
        context = {"issues": {"someone/context-panel#42": reviewed(candidate)}}
        with patch.object(module, "next_selection_context", return_value=context):
            module.cmd_next(next_args())
        assert [item["number"] for item in result["available_candidates"]] == [42]


def test_portfolio_mediaforce_hold_overrides_active_unowned_issue_and_background_work() -> None:
    leaf = global_issue("someone/mediaforce", 545)
    roots = [track("someone/direction", 1, "First")]
    context = {"repository_holds": {"someone/mediaforce": {
        "reason": "Parked until temporary encoding finishes and direction is revisited; existing encodes may continue.",
        "evidence": ["owner correction in #817"],
    }}}
    with global_fixture(roots, [leaf], {("someone/direction", 1): relationships(sub_issues=[leaf])}, discovered=[leaf]) as (module, result, _reads):
        with patch.object(module, "next_selection_context", return_value=context):
            module.cmd_next(next_args())
        assert result["candidates"] == []
        assert any(item.get("number") == 545 and item["exclusion"] == "repository_held" for item in result["excluded"])
        assert result["repository_holds"] == context["repository_holds"]


def test_portfolio_occupied_and_comment_only_wait_leave_no_available_work() -> None:
    occupied = global_issue("someone/codex-lab", 979)
    waiting = global_issue("someone/BD_to_AVP", 769)
    comments = {(waiting["repo"], 769): [{"id": 10, "body": "Implementation is settled. Signed-app screenshot demonstration at the next beta.", "updated_at": "2026-09-26T12:00:00Z"}]}
    with global_fixture([], [], {}, discovered=[occupied, waiting], comments=comments) as (module, result, _reads):
        module.cmd_next(next_args())
        by_number = {item["number"]: item for item in result["candidates"]}
        assert "next beta" in by_number[769]["discussion"]["comments"][-1]["body"]
        context = {"issues": {
            "someone/codex-lab#979": reviewed(by_number[979], "underway"),
            "someone/BD_to_AVP#769": reviewed(by_number[769], "waiting", reason="Screenshot proof waits for next beta"),
        }}
        with patch.object(module, "next_selection_context", return_value=context):
            module.cmd_next(next_args())
        assert result["candidates"] == result["available_candidates"] == []
        assert result["underway"][0]["number"] == 979
        assert result["waiting"][0]["number"] == 769
        assert result["discovery_context"]["complete"] is True


def test_portfolio_partial_ownership_and_stale_or_truncated_discussions_are_not_available() -> None:
    own = global_issue("someone/product", 42)
    comments = {(own["repo"], 42): [{"id": 1, "body": "Earlier comment"}, {"id": 2, "body": "Later owner correction"}]}
    with global_fixture([], [], {}, discovered=[own], comments=comments) as (module, result, _reads):
        module.cmd_next(next_args())
        candidate = result["candidates"][0]
        for extra in ({"ownership_complete": False}, {"discussion_digest": "older snapshot"}):
            context = {"issues": {"someone/product#42": reviewed(candidate, **extra)}}
            with patch.object(module, "next_selection_context", return_value=context):
                module.cmd_next(next_args())
            assert result["available_candidates"] == []
        args = next_args()
        args.comment_limit = 1
        module.cmd_next(args)
        assert result["candidates"][0]["review_required"] == "complete_issue_discussion"
        assert result["discovery_context"]["complete"] is False


def test_portfolio_priority_requires_incident_and_repeated_stop_evidence() -> None:
    nodes = [global_issue("someone/project", number) for number in range(1, 5)]
    with global_fixture([], [], {}, discovered=nodes) as (module, result, _reads):
        module.cmd_next(next_args())
        by_number = {item["number"]: item for item in result["candidates"]}
        context = {"issues": {
            "someone/project#1": reviewed(by_number[1], category="own_project"),
            "someone/project#2": reviewed(by_number[2], category="repeated_stop_tooling"),
            "someone/project#3": reviewed(by_number[3], category="live_incident"),
            "someone/project#4": reviewed(by_number[4], category="repeated_stop_tooling", stop_occurrences=["https://github.com/someone/project/issues/20", "https://github.com/someone/project/issues/21"]),
        }}
        with patch.object(module, "next_selection_context", return_value=context):
            module.cmd_next(next_args())
        assert [item["number"] for item in result["available_candidates"]] == [3, 4, 1]
        assert next(item for item in result["candidates"] if item["number"] == 2)["review_required"] == "two_linked_stop_occurrences"


def test_portfolio_inventory_sources_exclusions_round_robin_and_read_bounds() -> None:
    module = load_module()
    repositories = [{"full_name": name, **extra} for name, extra in [
        ("someone/direction", {}), ("other/foreign", {}), ("someone/archived", {"archived": True}),
        ("someone/empty", {"size": 0, "open_issues_count": 0}), ("someone/disabled", {"has_issues": False}),
        ("someone/a", {}), ("someone/b", {}), ("someone/held", {}), ("someone/private", {}),
    ]]
    calls: list[str] = []

    def collect(path: str, **kwargs: Any) -> Any:
        calls.append(path)
        if path in {"/installation/repositories", "/user/repos"}:
            assert kwargs["limit"] == 101
            return "automation-gh", repositories
        assert kwargs["query"]["state"] == "open" and "labels" not in kwargs["query"]
        assert kwargs["issue_only"] is True and kwargs["limit"] == 3
        if path == "/repos/someone/direction/issues":
            return "automation-gh", []
        if path == "/repos/someone/private/issues":
            raise module.PlanError("not accessible")
        name = "/".join(path.split("/")[2:4])
        return "automation-gh", [global_issue(name, number) for number in range(1, 4)]

    context = {"repository_holds": {"someone/held": {"reason": "owner parked", "evidence": ["current owner instruction"]}}}
    args = next_args()
    args.repository_issue_limit = 2
    with patch.multiple(module, collect_paged_rest_items=collect, load_direction=lambda *_: DIRECTION):
        for configured in ({"app": "configured"}, None):
            calls.clear()
            with patch.object(module.github_identity, "github_app_config", return_value=configured):
                found, coverage = module.discover_direction_work("someone/direction", args, selection_context=context)
            assert calls[0] == ("/installation/repositories" if configured else "/user/repos")
            assert [(item["repo"], item["number"]) for item in found] == [("someone/a", 1), ("someone/b", 1), ("someone/a", 2), ("someone/b", 2)]
            assert coverage["complete"] is False
            assert len(calls) == 5
            reasons = {source.get("exclusion") for source in coverage["repositories"]}
            assert reasons >= {"repository_held", "source_unavailable", "other_owner", "archived_or_disabled", "empty_without_open_issues", "issues_disabled"}


def test_portfolio_inventory_failure_and_scan_bound_never_claim_full_coverage() -> None:
    module = load_module()
    with patch.object(module.github_identity, "github_app_config", return_value={}), patch.object(module, "collect_paged_rest_items", side_effect=module.PlanError("inventory denied")):
        found, coverage = module.discover_direction_work("someone/direction", next_args(), selection_context={})
        assert found == [] and coverage["complete"] is False
        assert coverage["inventory_count"] is None
    with global_fixture([], [], {}, discovered=[global_issue("someone/p", 1), global_issue("someone/p", 2)]) as (module, result, _reads):
        module.cmd_next(next_args(scan_limit=1))
        assert result["truncated"] is True
        assert result["discovery_context"]["unevaluated_count"] == 1
        assert result["discovery_context"]["complete"] is False


def test_portfolio_nontrack_direction_issues_and_repository_milestone_order() -> None:
    unlinked = global_issue("someone/direction", 10)
    with global_fixture([unlinked], [], {}, discovered=[unlinked]) as (module, result, _reads):
        module.cmd_next(next_args())
        assert [item["number"] for item in result["candidates"]] == [10]
        assert result["available_candidates"] == []
        assert "category" not in result["candidates"][0]
        assert not any(item["number"] == 10 for item in result["excluded"])
        shared = module.github_direction_next
        discovered = [{
            **global_issue("someone/product", number),
            "milestone": milestone_data(number, title, created_at=date),
        } for number, title, date in [(1, "Later", "2025-01-01"), (2, "Now", "2026-01-01")]]
        ranked = shared.rank_portfolio_work(
            {"candidates": []}, discovered, milestone_titles=["Business"],
            repository_milestones={"someone/product": ["Now", "Later"]},
        )
        assert [item["number"] for item in ranked["candidates"]] == [2, 1]


def test_portfolio_repository_inventory_truncation_and_auth_failure() -> None:
    module = load_module()
    args = next_args()
    args.repo_limit = 1
    with patch.object(module.github_identity, "github_app_config", return_value={}), patch.object(module, "collect_paged_rest_items", return_value=("automation-gh", [{"full_name": "someone/archived", "archived": True}, {"full_name": "someone/unread"}])):
        found, coverage = module.discover_direction_work("someone/direction", args, selection_context={})
        assert found == [] and coverage["inventory_truncated"] and not coverage["complete"]
    failure = module.github_api_core.FailureDetail(cause="rest_primary_rate_limited", message="quota exhausted", retryable=False, fallback_eligible=False, disposition="stop")
    with patch.object(module.github_identity, "github_app_config", return_value={}), patch.object(module, "collect_paged_rest_items", side_effect=module.PlanError("quota exhausted", failure=failure)):
        try:
            module.discover_direction_work("someone/direction", args, selection_context={})
        except module.PlanError as exc:
            assert exc.failure.cause == "rest_primary_rate_limited"
        else:
            raise AssertionError("quota failures must preserve stop policy")


def test_portfolio_discovery_preserves_parent_waits_and_ancestry_discussions() -> None:
    root = track("someone/direction", 1, "First")
    parent = global_issue("someone/project", 2, body="## Current Status\nState: Waiting.\nWaiting for: owner sequencing.")
    child = global_issue("someone/project", 3)
    edges = {(root["repo"], 1): relationships(sub_issues=[parent]), (parent["repo"], 2): relationships(sub_issues=[child])}
    for roots in ([root], []):
        active_edges = edges if roots else {(parent["repo"], 2): edges[(parent["repo"], 2)]}
        with global_fixture(roots, [parent, child], active_edges, discovered=[child]) as (module, result, _reads):
            module.cmd_next(next_args())
        assert result["available_candidates"] == result["candidates"] == []
        blocked = next(item for item in result["excluded"] if item.get("number") == 3)
        assert blocked["exclusion"] == "parent_waiting"
        assert blocked["discussion"]["parents"][0]["number"] == 2
        assert any(wait["number"] == 3 for wait in result["waiting"])


def test_portfolio_hold_preserves_other_repository_milestone_work_and_service_parity() -> None:
    root = track("someone/direction", 1, "First")
    parent = global_issue("someone/mediaforce", 5)
    child = global_issue("elsewhere/independent", 6)
    edges = {(root["repo"], 1): relationships(sub_issues=[parent]), (parent["repo"], 5): relationships(sub_issues=[child])}
    context = {"repository_holds": {parent["repo"]: {"reason": "Owner parked new development here", "evidence": ["owner instruction"]}}}
    with global_fixture([root], [parent, child], edges) as (module, result, _reads):
        module.cmd_next(next_args())
        service = module.github_direction_next.rank_portfolio_work(
            result, [], milestone_titles=["First", "Second"], selection_context=context,
        )
        with patch.object(module, "next_selection_context", return_value=context):
            module.cmd_next(next_args())
        assert result["candidates"] == service["candidates"]
        assert [item["number"] for item in result["candidates"]] == [6]
        assert result["candidates"][0]["milestone"]["title"] == "First"
        assert len(result["candidates"][0]["via"]) == 3
        assert result["graph_context"]["complete"] is False  # Second has no Track.


def test_portfolio_empty_milestones_and_unspecified_wait_are_supported() -> None:
    waiting = global_issue("someone/product", 1, labels=["plan", "plan:waiting"])
    own = global_issue("someone/product", 2)
    with global_fixture([], [], {}, discovered=[waiting, own]) as (module, result, _reads):
        with patch.object(module, "load_direction", return_value="# Direction\n## Order\nOwn projects.\n## Milestones\n"):
            module.cmd_next(next_args())
        assert [item["number"] for item in result["candidates"]] == [2]
        assert result["waiting"][0]["number"] == 1
        assert result["graph_context"]["complete"] is True


def test_portfolio_unreadable_parent_and_changed_parent_comment_require_review() -> None:
    parent = global_issue("someone/product", 1)
    child = global_issue("someone/product", 2)
    comments = {(parent["repo"], 1): [{"body": "Work may proceed."}]}
    with global_fixture([], [parent], {(parent["repo"], 1): relationships(sub_issues=[child])}, discovered=[child], comments=comments) as (module, result, _reads):
        module.cmd_next(next_args())
        first = result["candidates"][0]
        context = {"issues": {"someone/product#2": reviewed(first)}}
        comments[(parent["repo"], 1)].append({"body": "Wait for the next release."})
        with patch.object(module, "next_selection_context", return_value=context):
            module.cmd_next(next_args())
        assert result["available_candidates"] == []
        assert result["candidates"][0]["discussion"]["digest"] != first["discussion"]["digest"]
        with patch.object(module, "read_next_parent", side_effect=module.PlanError("parent inaccessible")):
            module.cmd_next(next_args())
        assert result["candidates"] == []
        assert result["discovery_context"]["complete"] is False


TESTS = [
    test_portfolio_discovery_preserves_parent_waits_and_ancestry_discussions,
    test_portfolio_hold_preserves_other_repository_milestone_work_and_service_parity,
    test_portfolio_empty_milestones_and_unspecified_wait_are_supported,
    test_portfolio_unreadable_parent_and_changed_parent_comment_require_review,
    test_portfolio_nontrack_direction_issues_and_repository_milestone_order,
    test_portfolio_repository_inventory_truncation_and_auth_failure,
    test_portfolio_empty_business_graph_discovers_available_own_project,
    test_portfolio_mediaforce_hold_overrides_active_unowned_issue_and_background_work,
    test_portfolio_occupied_and_comment_only_wait_leave_no_available_work,
    test_portfolio_partial_ownership_and_stale_or_truncated_discussions_are_not_available,
    test_portfolio_priority_requires_incident_and_repeated_stop_evidence,
    test_portfolio_inventory_sources_exclusions_round_robin_and_read_bounds,
    test_portfolio_inventory_failure_and_scan_bound_never_claim_full_coverage,
    test_next_beta_rc_stable_chain_respects_native_blockers,
    test_next_excludes_non_actionable_states_with_reasons,
    test_next_closed_milestone_is_context_not_exclusion,
    test_next_ranking_uses_direction_then_dependencies_then_oldest_created,
    test_next_ranking_without_direction_uses_milestone_creation_order,
    test_next_focus_context_normalizes_keys_and_reports_truncation,
    test_cmd_next_is_bounded_read_only_and_explainable,
    test_cmd_next_degrades_when_direction_is_unavailable_or_unparsed,
    test_cmd_next_surfaces_dependency_degradation_and_skips_cheap_exclusions,
    test_cmd_next_excludes_truncated_dependencies_with_only_closed_visible_blockers,
    test_cmd_next_reraises_dependency_api_failures,
    test_next_relationship_reads_are_bounded_and_report_truncation,
    test_cmd_next_supports_milestone_scope_and_focus_degradation,
    test_global_next_follows_cross_owner_blockers_and_reports_partial_waits,
    test_global_waiting_milestone_hands_off_to_next_not_unlinked_tooling,
    test_global_next_implicit_repository_and_explicit_repository_match,
    test_global_cycles_unreadable_edges_and_scan_limits_are_incomplete,
    test_global_shared_leaf_is_read_once_and_keeps_earliest_milestone,
    test_global_missing_direction_refuses_instead_of_local_fallback,
    test_global_missing_tracks_and_inventory_truncation_remain_visible,
    test_global_relationship_truncation_and_permissions_do_not_create_candidates,
    test_global_waits_use_current_status_and_never_reclassify_mentioned_work,
    test_global_milestone_scope_and_direction_order_capacity_context,
    test_global_whole_parent_wait_does_not_select_its_children_or_dependencies,
    test_global_leaf_waiting_on_referenced_review_is_not_actionable,
    test_global_closed_milestone_is_completed_not_missing,
    test_global_inconsistent_blocked_parent_makes_evidence_incomplete,
    test_global_empty_tracks_are_not_reported_as_people_waits,
    test_global_service_consumer_uses_the_same_classification_and_sort_as_cli,
    test_global_placeholder_waits_do_not_park_actionable_work,
    test_global_active_parent_partial_wait_need_not_name_an_issue,
    test_global_shared_classifier_preserves_incomplete_relationship_evidence,
]


def main() -> None:
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
