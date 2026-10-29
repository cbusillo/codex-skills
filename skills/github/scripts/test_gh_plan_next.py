#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused regression tests for dependency-aware next-work selection."""

from __future__ import annotations

import importlib.util
import random
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import Mock, patch


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
    module.real_next_graph_reader = module.next_graph_reader
    module.next_graph_reader = lambda _repo: None
    module.real_next_live_breakage = module.next_live_breakage
    module.next_live_breakage = lambda *_args: ([], {"complete": True, "source": "offline_fixture"})
    module.real_api_json = module.api_json
    # Every test starts offline; fixtures opt into the API evidence they own.
    module.api_json = Mock(side_effect=module.PlanError("offline fixture record unavailable"))
    module.real_next_dependabot_work = module.next_dependabot_work
    module.next_dependabot_work = lambda *_args, **_kwargs: {"dependabot_candidates": [], "dependabot_candidate_count": 0}
    module.real_read_next_train_enrollment = module.read_next_train_enrollment
    module.read_next_train_enrollment = lambda _repo: {"source": "launchplane", "status": "not_enrolled"}
    module.real_discover_direction_work = module.discover_direction_work
    module.real_read_next_inbound_blockers = module.read_next_inbound_blockers
    module.read_next_inbound_blockers = lambda *_a, **_kw: ("automation-gh", [], {"complete": True})
    module.real_annotate_next_queue = module.annotate_next_queue
    module.annotate_next_queue = lambda *_a, **_kw: {"source": "offline_fixture"}
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


@contextmanager
def local_next_fixture(
    module: Any,
    plans: list[dict[str, Any]],
    *,
    read_relationships: Callable[..., Any],
    collect_plans: Callable[..., Any] | None = None,
    focus_context: Callable[..., Any] | None = None,
    direction: Callable[..., Any] | None = None,
    waits: dict[int, dict[str, Any]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Capture a local next command with explicit evidence and scoped patches."""
    captured: dict[str, Any] = {}

    def collect(path: str, **kwargs: Any) -> Any:
        if path.endswith("/comments"):
            number = int(path.split("/")[-2])
            record = (waits or {}).get(number)
            if record is None:
                record = module.github_plan_wait.reviewed(evidence="Fixture discussion reviewed; no remaining person or event hold")
            return "automation-gh", [{"user": {"login": "owner"}, "body": module.github_plan_wait.marker(record)}]
        if collect_plans is not None:
            return collect_plans(path, **kwargs)
        return "automation-gh", plans

    with patch.multiple(
        module,
        collect_paged_rest_items=collect,
        next_focus_context=(
            focus_context if focus_context is not None else lambda _repo, _config: (
                "automation-gh", {}, {"available": False, "reason": "project_not_configured"},
            )
        ),
        read_next_issue_relationships=read_relationships,
        load_direction=direction if direction is not None else lambda _repo: DIRECTION,
        emit=captured.update,
    ):
        yield captured


def test_unstatused_scope_is_review_work_without_becoming_a_person_wait() -> None:
    module = load_module()
    record = module.github_plan_wait.reviewed(evidence="No person or event hold; scope still needs review")
    raw = issue(8, labels=[module.DEFAULT_CONFIG["labels"]["plan"]])
    result = module.github_direction_next.evaluate_direction_node(
        raw, config=module.DEFAULT_CONFIG, focus=None, relationships=relationships(), structured_wait=record)
    observed = result["item"]
    assert observed["plan_status"] == "hold"
    assert observed["exclusion"] == "scope_review_required"
    assert not observed.get("wait_review_required")
    assert not result.get("waiting")


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


def test_stale_project_focus_does_not_hide_ready_work() -> None:
    module = load_module()
    for focus in ("Waiting", "Later"):
        ready = issue(5) | {"body": "## Current Status\n\nState: Active.\n"}
        disposition, result = module.evaluate_next_plan(
            ready, config=module.DEFAULT_CONFIG, focus=focus, relationships=relationships(),
        )
        assert disposition == "candidate", result
        assert result["focus"] == focus, result
        node = module.github_direction_next.evaluate_direction_node(
            ready, config=module.DEFAULT_CONFIG, focus=focus, relationships=relationships(),
        )
        assert "exclusion" not in node["item"], node
        assert not node["waiting"], node
        for waiting in (
            ready | {"labels": [{"name": "plan:waiting"}]},
            ready | {"body": "## Current Status\n\nState: Waiting.\nWaiting for: testing.\n"},
        ):
            node = module.github_direction_next.evaluate_direction_node(
                waiting, config=module.DEFAULT_CONFIG, focus=focus, relationships=relationships(),
            )
            assert node["item"]["exclusion"] == "waiting", node


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
        _issue: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, list[dict[str, Any]]], list[str]]:
        if number == 1:
            return "automation-gh", relationships(blocked_by=[related(10)]), []
        return "automation-gh", relationships(), []

    with local_next_fixture(
        module,
        plans,
        collect_plans=fake_collect,
        focus_context=lambda _repo, _config: (
            "automation-gh",
            {plans[0]["html_url"]: "Now", plans[1]["html_url"]: "Next"},
            {"available": True},
        ),
        read_relationships=fake_relationships,
    ) as captured:
        module.cmd_next(
            type(
                "Args",
                (),
                {"repo": "owner/repo", "milestone": None, "limit": 1, "scan_limit": 2},
            )()
        )

    assert observed_query["state"] == "open"
    assert observed_query["sort"] == "created"
    assert observed_query["direction"] == "asc"
    assert captured["truncated"] is True
    assert captured["candidate_count"] == 1
    assert [item["number"] for item in captured["candidates"]] == [3]
    assert captured["excluded"][0]["exclusion"] == "blocked_by_open_dependency"
    assert captured["excluded"][0]["number"] == 1
    assert "scan_limit_truncated_prioritized_plans" in captured["notes"]
    assert any("--scan-limit" in note for note in captured["notes"])
    assert captured["dependency_context"]["complete"] is True


def test_cmd_next_degrades_when_direction_is_unavailable_or_unparsed() -> None:
    module = load_module()
    plan = issue(
        1,
        milestone=milestone_data(9, "Unlisted", created_at="2026-07-01T00:00:00Z"),
    )
    with local_next_fixture(
        module,
        [plan],
        focus_context=lambda *_args, **_kwargs: (
            None, {}, {"available": False, "reason": "project_not_configured"},
        ),
        read_relationships=lambda *_args, **_kwargs: ("automation-gh", relationships(), []),
    ) as captured:
        module.load_direction = lambda _repo: (_ for _ in ()).throw(module.PlanError("temporary 502"))
        module.cmd_next(
            type("Args", (), {"repo": "owner/repo", "milestone": None, "limit": 5, "scan_limit": 5})()
        )
        assert "direction_unavailable" in captured["notes"]
        assert captured["milestone_order"]["source"] == "milestone_created_at"
        assert captured["milestone_order"]["error"] == "temporary 502"

        captured.clear()
        cancelled = module.PlanError("retry wait cancelled", failure=module.github_api_core.FailureDetail(
            cause="cancelled", message="retry wait cancelled", retryable=False,
            fallback_eligible=False, disposition="stop"))
        module.load_direction = lambda _repo: (_ for _ in ()).throw(cancelled)
        try:
            module.cmd_next(
                type("Args", (), {"repo": "owner/repo", "milestone": None, "limit": 5, "scan_limit": 5})()
            )
        except module.PlanError as exc:
            assert exc is cancelled
        else:
            raise AssertionError("Ctrl-C during the direction read became degraded ordering")
        assert not captured

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


def test_cmd_next_surfaces_dependency_degradation_and_skips_cheap_exclusions() -> None:
    module = load_module()
    plans = [issue(1), issue(2, labels=["plan", "plan:stale"])]
    relationship_calls: list[int] = []

    def fake_relationships(_repo: str, number: int, _issue: dict[str, Any] | None = None) -> Any:
        relationship_calls.append(number)
        raise module.PlanError("dependency endpoint unavailable")

    with local_next_fixture(module, plans, read_relationships=fake_relationships) as captured:
        module.cmd_next(
            type(
                "Args",
                (),
                {"repo": "owner/repo", "milestone": None, "limit": 5, "scan_limit": 5},
            )()
        )

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
    visible_blockers = [
        related(number, state="closed")
        for number in range(2, module.NEXT_RELATIONSHIP_LIMIT + 2)
    ]

    with local_next_fixture(
        module,
        [issue(1)],
        read_relationships=lambda *_args, **_kwargs: (
            "automation-gh", relationships(blocked_by=visible_blockers), ["blocked_by"],
        ),
    ) as captured:
        module.cmd_next(
            type(
                "Args",
                (),
                {"repo": "owner/repo", "milestone": None, "limit": 5, "scan_limit": 5},
            )()
        )

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
    failure = module.github_api_core.FailureDetail(
        cause="rate_limited",
        message="retry later",
        retryable=True,
        fallback_eligible=False,
        disposition="retry",
    )
    def failed_relationships(*_args: Any, **_kwargs: Any) -> Any:
        raise module.PlanError("retry later", failure=failure)

    with local_next_fixture(module, [issue(1)], read_relationships=failed_relationships):
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

    with (
        local_next_fixture(
            module,
            [scoped_plan],
            collect_plans=fake_collect,
            focus_context=lambda _repo, _config: (
                None, {}, {"available": False, "reason": "project_focus_unavailable"},
            ),
            read_relationships=lambda *_args, **_kwargs: ("automation-gh", relationships(), []),
            direction=module.load_direction,
        ) as captured,
        patch.object(module, "milestone_route", lambda: ("gh", "automation-gh")),
        patch.object(
            module.github_milestone_core,
            "show_milestone",
            lambda *_args, **_kwargs: {
                "actor": "automation-gh",
                "milestone": {
                    "number": 7,
                    "title": "Release 7",
                    "state": "open",
                    "due_on": "2026-09-01T00:00:00Z",
                },
            },
        ),
    ):
        module.cmd_next(
            type(
                "Args",
                (),
                {"repo": "owner/repo", "milestone": "Release 7", "limit": 5, "scan_limit": 5},
            )()
        )

    assert observed_query["milestone"] == 7
    assert captured["scope"]["kind"] == "milestone"
    assert captured["scope"]["milestone"]["title"] == "Release 7"
    assert "project_focus_unavailable" in captured["notes"]


def global_issue(repo: str, number: int, *, body: str = "", waiting_for: str | None = None,
                 wait_reviewed: bool = True, **kwargs: Any) -> dict[str, Any]:
    # Ordinary graph fixtures start with reviewed absence. Migration cases opt
    # out explicitly; prose and labels never choose the fixture's wait record.
    return {**issue(number, **kwargs), "repo": repo, "html_url": f"https://github.com/{repo}/issues/{number}", "body": body,
            "_fixture_wait_reviewed": wait_reviewed,
            **({"_fixture_waiting_for": waiting_for} if waiting_for is not None else {})}


def track(repo: str, number: int, title: str) -> dict[str, Any]:
    return global_issue(
        repo, number, wait_reviewed=False, title=f"Track: {title}", labels=["plan", "plan:waiting"],
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
    relationship_requests: list[str] | None = None,
    clear_waits: list[dict[str, Any]] | None = None,
) -> Any:
    module = load_module()
    comments = comments if comments is not None else {}
    for raw in [*roots, *nodes, *(discovered or [])]:
        actor = raw.get("_fixture_waiting_for")
        if actor is not None:
            key = (raw["repo"], raw["number"])
            record = module.github_plan_wait.reviewed(evidence="Fixture's person/event hold reviewed in full",
                waiting_for=actor, ends_when="The named actor or event records completion on this issue")
            comments.setdefault(key, []).insert(0, {"id": -8000 - raw["number"],
                "user": {"login": raw["repo"].split("/")[0]}, "body": module.github_plan_wait.marker(record)})
    for raw in [*nodes, *(discovered or [])]:
        if not raw.get("_fixture_wait_reviewed") or raw.get("_fixture_waiting_for") is not None:
            continue
        key = (raw["repo"], raw["number"])
        record = module.github_plan_wait.reviewed(evidence="Graph fixture's complete discussion reviewed; no person/event hold")
        comments.setdefault(key, []).insert(0, {"id": -9000 - raw["number"],
            "user": {"login": raw["repo"].split("/")[0]}, "body": module.github_plan_wait.marker(record)})
    for raw in clear_waits or []:
        # Ranking/Client/incident fixtures explicitly supply their reviewed
        # absence; missing-record migration is tested independently.
        key = (raw["repo"], raw["number"])
        comments.setdefault(key, []).append({
            "id": 9000 + raw["number"], "user": {"login": raw["repo"].split("/")[0]},
            "body": module.github_plan_wait.marker(module.github_plan_wait.reviewed(evidence="Fixture request reviewed; no hold remains")),
        })
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
        if relationship_requests is not None and any(path.endswith("/" + name) for name in ("blocked_by", "blocking", "sub_issues")):
            relationship_requests.append(path)
            parts = path.split("/")
            key = ("/".join(parts[2:4]), int(parts[5]))
            return "automation-gh", edges.get(key, relationships())[parts[-1]]
        if path.endswith("/comments"):
            parts = path.split("/")
            return "automation-gh", (comments or {}).get(("/".join(parts[2:4]), int(parts[5])), [])
        assert path == f"/repos/{repo}/issues"
        assert kwargs["issue_only"] is True
        assert kwargs["query"]["state"] == "open"
        assert kwargs["limit"] == module.NEXT_PLAN_INVENTORY_LIMIT + 1
        return "automation-gh", roots

    def read_relationships(target_repo: str, number: int, _issue: dict[str, Any] | None = None) -> Any:
        return "automation-gh", {
            name: [module.compact_relationship_issue(node, name) for node in values]
            for name, values in edges.get((target_repo, number), relationships()).items()
        }, []

    def wait_api(method: str, path: str, **_kwargs: Any) -> Any:
        if module.NEXT_READS is not None and module.NEXT_READS.observation(path) is not None:
            return module.real_api_json(method, path, **_kwargs)
        if path == "/graphql":
            assert method == "POST" and _kwargs["is_write"] is False
            variables = _kwargs["payload"]["variables"]
            observed_issue = all_nodes[(f"{variables['owner']}/{variables['name']}", variables["number"])]
            return "automation-gh", {"data": {"repository": {"issue": {
                "createdAt": observed_issue["created_at"], "lastEditedAt": observed_issue.get("last_edited_at")}}}}
        assert method == "GET", "global next must remain read-only"
        parts = path.split("?", 1)[0].strip("/").split("/")
        target = "/".join(parts[1:3])
        if len(parts) == 3:
            return "automation-gh", {"default_branch": "main", "archived": False}
        number = int(parts[4])
        key = (target, number)
        if len(parts) == 7 and parts[5:] == ["dependencies", "blocked_by"]:
            return "automation-gh", edges.get(key, relationships())["blocked_by"]
        if parts[3] == "pulls":
            return "automation-gh", {"merged_at": None, "base": {"ref": "main"}}
        if key not in all_nodes:
            raise module.PlanError("not accessible")
        return "automation-gh", all_nodes[key]

    native_reader = module.read_next_issue_relationships
    with patch.multiple(
        module,
        default_repo=lambda explicit: explicit or repo,
        load_config=lambda *_: module.DEFAULT_CONFIG,
        load_direction=lambda *_: DIRECTION,
        collect_paged_rest_items=collect,
        discover_direction_work=lambda *_args, **_kwargs: (discovered or [], {"complete": True, "repositories": [{"repo": name, "direction": None} for name in sorted({item["repo"] for item in discovered or []})]}),
        next_focus_context=lambda *_: (None, {}, {"available": False, "reason": "project_not_configured"}),
        read_next_issue_relationships=native_reader if relationship_requests is not None else read_relationships,
        read_next_parent=lambda target, number: parent_map.get((target.casefold(), number)),
        get_issue=get_node,
        api_json=wait_api,
        milestone_route=lambda: ("gh", "automation-gh"),
        emit=captured.update,
    ), patch.multiple(module.github_milestone_core, list_milestones=lambda *_args, **_kwargs: {"milestones": [root["milestone"] for root in roots if root.get("milestone")]}):
        yield module, captured, reads


def next_failure(module: Any, args: Any, *, code: str) -> dict[str, Any]:
    try:
        module.cmd_next(args)
    except module.ClassifiedPlanError as exc:
        assert exc.code == code
        return exc.payload
    raise AssertionError("Unread required evidence returned success")


def next_args(*, repo: str | None = "someone/direction", scan_limit: int | None = 50, milestone: str | None = None) -> Any:
    return type("Args", (), {"repo": repo, "milestone": milestone, "limit": 10, "scan_limit": scan_limit})()


def test_global_next_follows_cross_owner_blockers_and_reports_partial_waits() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    parent = global_issue("someone/site", 91, body="## Current Status\n\nState: Active.\nWaiting for: Justin's owner review of #92 remains pending. Independent backup work continues.", waiting_for="Justin's owner review of #92 remains pending. Independent backup work continues.")
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
    assert result["recorded_waits"][0]["repo"] == "someone/site"
    assert result["recorded_waits"][0]["number"] == 91
    assert "Justin" in result["recorded_waits"][0]["waiting_for"]
    assert result["recorded_waits"][0]["reported_by"].endswith("/issues/91")
    assert result["dependency_context"]["complete"] is True
    assert len(reads) == len(set(reads)) == 4


def test_global_waiting_milestone_hands_off_to_next_not_unlinked_tooling() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second"), global_issue("someone/direction", 3, title="Improve tools")]
    waiting = global_issue("someone/product", 10, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Customer testing.", waiting_for='Customer testing.')
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
        # Runtime observations vary; repository resolution must preserve the
        # selection and coverage, independently of elapsed phase timings.
        assert {k: v for k, v in result.items() if k != "performance"} == {
            k: v for k, v in implicit.items() if k != "performance"}
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
        assert "--scan-limit" in result["graph_context"]["evaluation_note"]


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
        assert result["all_listed_milestones_have_no_available_item"] is None


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
    waiting = global_issue("someone/product", 3, body="## Objective\nWaiting for: Old decision.\n## Current Status\nState: Waiting.\nWaiting for: Alex to test [PR](https://github.com/other/product/pull/42).", waiting_for='Alex to test [PR](https://github.com/other/product/pull/42).')
    edges = {("someone/direction", 1): relationships(sub_issues=[waiting])}
    with global_fixture(roots, [waiting], edges) as (module, result, reads):
        module.cmd_next(next_args())
    assert result["candidates"] == []
    assert result["waiting"][0]["repo"] == "someone/product"
    assert result["waiting"][0]["number"] == 3
    assert result["waiting"][0]["references"][0]["url"].endswith("/pull/42")
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
    leaf = global_issue("someone/product", 4)
    wait = load_module().github_plan_wait
    held = wait.reviewed(evidence="Customer explicitly holds the whole plan.",
        waiting_for="Customer's sequencing decision before any work on the plan",
        ends_when="Customer releases all work on the plan")
    clear = wait.reviewed(evidence="Customer released the sequencing hold; preparation can proceed")
    for record in (held, None, clear):
        parent = global_issue("someone/product", 3, wait_reviewed=False, labels=["plan", "plan:waiting"],
            body="## Current Status\nState: Waiting.\nWaiting for: Customer's sequencing decision.")
        edges = {
            ("someone/direction", 1): relationships(sub_issues=[parent]),
            ("someone/product", 3): relationships(sub_issues=[leaf], blocked_by=[leaf]),
        }
        comments = {} if record is None else {(parent["repo"], 3): [
            {"user": {"login": "someone"}, "body": wait.marker(record)}]}
        with global_fixture(roots, [parent, leaf], edges, comments=comments) as (module, result, reads):
            module.cmd_next(next_args())
        assert set(reads) == {("someone/product", 3), ("someone/product", 4)}
        parent_view = next(row for row in result["excluded"] if row["number"] == 3)
        if record is clear:
            assert [item["number"] for item in result["candidates"]] == [4]
            assert parent_view["exclusion"] == "blocked_by_open_dependency"
            assert not parent_view["wait_review_required"]
        else:
            assert result["candidates"] == []
            assert any(node.get("number") == 4 and node["exclusion"] == "parent_waiting" for node in result["excluded"])
            assert parent_view["wait_review_required"] is (record is None)
            assert parent_view["exclusion"] == ("wait_review_required" if record is None else "waiting")
        if record is not None:
            assert parent_view["structured_wait"] == record


def test_global_leaf_waiting_on_referenced_review_is_not_actionable() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/site", 91, body="## Current Status\nState: Active.\nWaiting for: Justin's owner review of #92 remains pending.", waiting_for="Justin's owner review of #92 remains pending.")
    edges = {("someone/direction", 1): relationships(sub_issues=[leaf])}
    with global_fixture(roots, [leaf], edges) as (module, result, _reads):
        module.cmd_next(next_args())
    assert result["candidates"] == []
    assert result["waiting"][0]["number"] == 91


def test_global_closed_milestone_is_completed_not_missing() -> None:
    roots = [track("someone/direction", 2, "Second")]
    closed = milestone_data(1, "First", state="closed", created_at="2026-01-01T00:00:00Z")
    with global_fixture(roots, [], {}) as (module, result, _reads):
        with patch.multiple(module.github_milestone_core, list_milestones=lambda *_args, **_kwargs: {"milestones": [closed, roots[0]["milestone"]]}):
            module.cmd_next(next_args())
    assert result["dependency_context"]["complete"] is True
    assert result["completed_milestones"] == ["First"]


def test_global_stale_blocked_parent_is_a_finding_with_complete_coverage() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    parent = global_issue("someone/product", 3, labels=["plan", "plan:blocked"])
    leaf = global_issue("someone/product", 4)
    edges = {
        ("someone/direction", 1): relationships(sub_issues=[parent]),
        ("someone/product", 3): relationships(sub_issues=[leaf]),
    }
    with global_fixture(roots, [parent, leaf], edges) as (module, result, _reads):
        module.cmd_next(next_args())
    assert result["dependency_context"]["complete"] is True
    assert result["dependency_context"]["degraded_count"] == 0
    assert [row["number"] for row in result["candidates"]] == [4]
    parent_view = next(row for row in result["excluded"] if row["number"] == 3)
    assert parent_view["plan_status"] == "active" and parent_view["derived"]["blocked_by"] == []
    assert not result["findings"]


def test_global_empty_tracks_are_not_reported_as_people_waits() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    with global_fixture(roots, [], {}) as (module, result, _reads):
        module.cmd_next(next_args())
    assert result["waiting"] == []
    assert all(item["exclusion"] == "tracking_without_open_work" for item in result["excluded"])


def test_global_service_consumer_uses_the_same_classification_and_sort_as_cli() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    ready = global_issue("other/project", 3)
    waiting = global_issue("someone/site", 91, body="## Current Status\nState: Active.\nWaiting for: Justin's review of #92.\n<!-- operation marker -->", waiting_for="Justin's review of #92.")
    edges = {("someone/direction", 1): relationships(sub_issues=[waiting, ready])}
    comments: dict[tuple[str, int], list[dict[str, Any]]] = {}
    with global_fixture(roots, [ready, waiting], edges, comments=comments) as (module, result, _reads):
        module.cmd_next(next_args())
        shared = module.github_direction_next
        nodes = {(item["repo"], item["number"]): item for item in roots + [ready, waiting]}

        def service_reader(repo: str, number: int) -> dict[str, Any]:
            node = nodes[(repo, number)]
            relations = {
                name: [module.compact_relationship_issue(item, name) for item in items]
                for name, items in edges.get((repo, number), relationships()).items()
            }
            classified = shared.evaluate_direction_node(node, config=module.DEFAULT_CONFIG, focus=None, relationships=relations,
                                                        structured_wait=module.current_wait(node, comments.get((repo, number), []), repo))
            classified["item"]["discussion"] = shared.discussion_snapshot(node, comments.get((repo, number), []), complete=True)
            return classified

        ranked = shared.rank_direction_work(
            [{**shared.compact_list_issue(item["repo"], item), "milestone": shared.next_milestone_context(item)} for item in roots],
            milestone_titles=["First", "Second"], read_node=service_reader, scan_limit=50,
        )
        ranked.update(shared.rank_portfolio_work(ranked, [], milestone_titles=["First", "Second"]))
        for field in ("candidates", "excluded"):
            assert ranked[field] == result[field]
        # The CLI adds live reference-state observations after shared ranking.
        # Classification, actors, conditions and reference identities agree.
        fields = ("repo", "number", "plan_status", "waiting_for", "ends_when", "source")
        assert [{key: row[key] for key in fields} for row in ranked["waiting"]] == [
            {key: row[key] for key in fields} for row in result["waiting"]]
        assert [[(ref["repo"], ref["number"]) for ref in row["references"]] for row in ranked["waiting"]] == [
            [(ref["repo"], ref["number"]) for ref in row["references"]] for row in result["waiting"]]
        assert [item["number"] for item in ranked["candidates"]] == [3]
        assert ranked["waiting"][0]["number"] == 91


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
        parent = global_issue("someone/site", 91, wait_reviewed=False, body="## Current Status\nState: Active.\nWaiting for: " + reason)
        edges = {
            ("someone/direction", 1): relationships(sub_issues=[parent]),
            ("someone/site", 91): relationships(blocked_by=[leaf]),
        }
        with global_fixture(roots, [parent, leaf], edges) as (module, result, _reads):
            module.cmd_next(next_args())
        assert [item["number"] for item in result["candidates"]] == [3]
        assert result["recorded_waits"][0]["waiting_for"] == reason
        if "#92" in reason:
            assert result["recorded_waits"][0]["number"] == 91


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


def test_portfolio_inventory_failure_and_scan_bound_never_claim_full_coverage() -> None:
    module = load_module()
    with patch.multiple(module.github_identity, github_app_config=lambda: {}), patch.multiple(module, collect_paged_rest_items=Mock(side_effect=module.PlanError("inventory denied"))):
        found, coverage = module.discover_direction_work("someone/direction", next_args())
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
    with patch.multiple(module.github_identity, github_app_config=lambda: {}), patch.multiple(module, collect_paged_rest_items=lambda *_args, **_kwargs: ("automation-gh", [{"full_name": "someone/archived", "archived": True}, {"full_name": "someone/unread"}])):
        found, coverage = module.discover_direction_work("someone/direction", args)
        assert found == [] and coverage["inventory_truncated"] and not coverage["complete"]
    failure = module.github_api_core.FailureDetail(cause="rest_primary_rate_limited", message="quota exhausted", retryable=False, fallback_eligible=False, disposition="stop")
    with patch.multiple(module.github_identity, github_app_config=lambda: {}), patch.multiple(module, collect_paged_rest_items=Mock(side_effect=module.PlanError("quota exhausted", failure=failure))):
        try:
            module.discover_direction_work("someone/direction", args)
        except module.PlanError as exc:
            assert exc.failure.cause == "rest_primary_rate_limited"
        else:
            raise AssertionError("quota failures must preserve stop policy")


def test_portfolio_discovery_preserves_parent_waits_and_ancestry_discussions() -> None:
    root = track("someone/direction", 1, "First")
    parent = global_issue("someone/project", 2, wait_reviewed=False, body="## Current Status\nState: Waiting.\nWaiting for: owner sequencing.")
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
        assert not any(wait["number"] == 3 for wait in result["waiting"])
        assert any(wait["number"] == 2 for wait in result["recorded_waits"])


def test_portfolio_empty_milestones_and_unspecified_wait_are_supported() -> None:
    waiting = global_issue("someone/product", 1, wait_reviewed=False, labels=["plan", "plan:waiting"])
    own = global_issue("someone/product", 2)
    with global_fixture([], [], {}, discovered=[waiting, own]) as (module, result, _reads):
        with patch.multiple(module, load_direction=lambda *_: "# Direction\n## Order\nOwn projects.\n## Milestones\n"):
            module.cmd_next(next_args())
        assert [item["number"] for item in result["candidates"]] == [2]
        assert result["unowned"][0]["number"] == 1
        assert result["graph_context"]["complete"] is True


def test_portfolio_nonempty_unparsed_direction_and_local_only_flags_refuse() -> None:
    with global_fixture([], [], {}) as (module, _result, _reads):
        with patch.multiple(module, load_direction=lambda *_: "# Direction\n## Milestones\n- Launch beta proves readiness."):
            try:
                module.cmd_next(next_args())
            except module.PlanError as exc:
                assert "nonempty Milestones" in str(exc)
            else:
                raise AssertionError("Unparsed direction must not become an empty graph")
        args = next_args(repo="someone/product")
        args.repo_limit = 1
        try:
            module.cmd_next(args)
        except module.PlanError as exc:
            assert "OWNER/direction" in str(exc)
        else:
            raise AssertionError("Local next must not silently ignore global selection evidence")


def test_local_next_inbound_scan_preserves_plan_budget_and_waits() -> None:
    module = load_module()
    gate = issue(2554, labels=["plan", "plan:waiting"])
    bug = issue(2555, labels=["bug"])
    for item in (gate, bug):
        item.pop("repo")  # REST issue payload identifies its repository through URLs
        item["issue_dependencies_summary"] = {"blocking": 1}
    plan = issue(1, milestone=milestone_data(1, "First", created_at="2026-07-01"))
    plan["issue_dependencies_summary"] = {"blocking": 0}
    downstream = {**related(141), "repo": "owner/other", "url": "https://github.com/owner/other/issues/141"}
    captured: dict[str, Any] = {}
    calls: list[str] = []

    def collect(path: str, **kwargs: Any) -> Any:
        calls.append(path)
        if path.endswith("/comments"):
            waiting = "/2554/comments" in path
            record = module.github_plan_wait.reviewed(evidence="Fixture's full discussion reviewed",
                waiting_for="Director gate acceptance" if waiting else None,
                ends_when="Director accepts the gate" if waiting else None)
            return "automation-gh", [{"user": {"login": "owner"}, "body": module.github_plan_wait.marker(record)}]
        if path.endswith("/blocking"):
            return "automation-gh", [downstream, related(99), {**downstream, "number": 142, "state": "closed"}]
        if kwargs["query"].get("labels"):
            return "automation-gh", [plan, gate]
        return "automation-gh", [plan, gate, bug]

    with patch.multiple(module, collect_paged_rest_items=collect,
                        next_focus_context=lambda *_: (None, {}, {"available": False}),
                        read_next_issue_relationships=lambda *_: ("automation-gh", relationships(), []),
                        read_next_inbound_blockers=module.real_read_next_inbound_blockers,
                        load_direction=lambda *_: DIRECTION, emit=captured.update):
        module.cmd_next(type("Args", (), {"repo": "owner/repo", "milestone": None, "limit": 1, "scan_limit": 2})())
    assert [item["number"] for item in captured["candidates"]] == [1]
    assert captured["excluded"][0]["exclusion"] == "waiting"
    assert [item["number"] for item in captured["blocking_work_elsewhere"]] == [2554, 2555]
    assert all(item["blocking"] == [downstream] for item in captured["blocking_work_elsewhere"])
    assert captured["blocking_work_elsewhere"][0]["labels"] == ["plan", "plan:waiting"]
    assert captured["blocking_work_elsewhere_context"]["complete"] is True
    assert captured["inventory_count"] == 2  # non-plans cannot consume the plan inventory


def test_inbound_scan_bounds_and_partial_reads_are_explicit() -> None:
    module = load_module()
    gates = [issue(1), issue(2)]  # missing native summaries are conservatively read
    downstream = {**related(141), "repo": "owner/other", "url": "https://github.com/owner/other/issues/141"}
    calls: list[str] = []

    def collect(path: str, **kwargs: Any) -> Any:
        calls.append(path)
        if path.endswith("/blocking"):
            assert kwargs["limit"] == module.NEXT_RELATIONSHIP_LIMIT + 1
            return "automation-gh", [downstream] * kwargs["limit"]
        assert kwargs["limit"] == module.NEXT_PLAN_INVENTORY_LIMIT + 1
        return "automation-gh", gates

    with patch.multiple(module, collect_paged_rest_items=collect):
        _, report, context = module.real_read_next_inbound_blockers("owner/repo", scan_limit=1)
    assert report[0]["number"] == 1
    assert len(report[0]["blocking"]) == module.NEXT_RELATIONSHIP_LIMIT
    assert context["complete"] is False and context["evaluated"] == 1
    assert context["gate_count"] == 2 and context["errors"]
    assert len(calls) == 2
    with patch.multiple(module, NEXT_PLAN_INVENTORY_LIMIT=1), patch.multiple(module, collect_paged_rest_items=collect):
        _, _, context = module.real_read_next_inbound_blockers("owner/repo", scan_limit=3)
    assert context["inventory_truncated"] and not context["complete"]


def test_inbound_unclassified_failure_degrades_report_but_quota_stops() -> None:
    module = load_module()
    captured: dict[str, Any] = {}
    clear_comment = {"user": {"login": "owner"}, "body": module.github_plan_wait.marker(
        module.github_plan_wait.reviewed(evidence="Inbound fixture discussion reviewed; no hold"))}
    with patch.multiple(module, collect_paged_rest_items=lambda path, **_kw: ("automation-gh", [clear_comment] if path.endswith("/comments") else [issue(1)]),
                        next_focus_context=lambda *_: (None, {}, {"available": False}),
                        read_next_issue_relationships=lambda *_: ("automation-gh", relationships(), []),
                        read_next_inbound_blockers=Mock(side_effect=module.PlanError("inbound inventory unavailable")),
                        load_direction=lambda *_: DIRECTION, emit=captured.update):
        module.cmd_next(type("Args", (), {"repo": "owner/repo", "milestone": None, "limit": 1, "scan_limit": 2})())
    assert captured["candidates"][0]["number"] == 1
    assert captured["dependency_context"]["complete"] is True
    assert captured["blocking_work_elsewhere_context"]["complete"] is False
    failure = module.github_api_core.FailureDetail(cause="rest_primary_rate_limited", message="quota exhausted", retryable=False, fallback_eligible=False, disposition="stop")
    with patch.multiple(module, collect_paged_rest_items=Mock(side_effect=module.PlanError("quota exhausted", failure=failure))):
        try:
            module.real_read_next_inbound_blockers("owner/repo", scan_limit=2)
        except module.PlanError as exc:
            assert exc.failure.cause == failure.cause
        else:
            raise AssertionError("quota stop policy must remain authoritative")


def test_live_breakage_beyond_scan_window_ranks_first_without_granting_availability() -> None:
    marker = load_module().github_direction_next.LIVE_BREAKAGE_LABEL
    root = track("someone/direction", 1, "First")
    leaf = global_issue("someone/product", 2)
    ordinary = [global_issue("someone/project", number) for number in range(10, 13)]
    incident = global_issue("someone/quiet", 243, labels=["plan", "plan:active", marker])
    edges = {(root["repo"], 1): relationships(sub_issues=[leaf])}
    with global_fixture([root], [leaf], edges, discovered=[*ordinary, incident]) as (module, result, _reads):
        module.cmd_next(next_args(scan_limit=2))
        assert result["candidates"][0]["number"] == incident["number"]
        assert result["candidates"][0]["availability"] == "needs_review"
        assert result["available_candidates"] == []
        assert {item["number"] for item in result["candidates"]} == {2, 10, 11, 243}
        coverage = result["discovery_context"]
        assert coverage["evaluated"] == 3 and coverage["live_breakage_evaluated"] == 1
        assert coverage["unevaluated_count"] == 1
        assert not result["candidate_coverage"]["complete"]
        assert result["candidate_coverage"]["warning"]


def test_live_breakage_marker_preserves_holds_dependencies_waits_and_review() -> None:
    marker = load_module().github_direction_next.LIVE_BREAKAGE_LABEL
    incidents = [global_issue(f"someone/p{number}", number, labels=["plan", "plan:active", marker]) for number in range(1, 6)]
    incidents[2]["labels"].append({"name": "plan:waiting"})
    incidents[2]["_fixture_waiting_for"] = "Director reports the incident's device test"
    edges = {(incidents[1]["repo"], 2): relationships(blocked_by=[global_issue("someone/gate", 99)])}
    with global_fixture([], [], edges, discovered=incidents, clear_waits=[row for row in incidents if row["number"] != 3]) as (module, result, _reads):
        def select() -> None:
            payload = next_failure(module, next_args(scan_limit=1), code="live_breakage_unread")
            result.clear()
            result.update(payload)

        select()
        assert result["available_candidates"] == []
        assert {item["exclusion"] for item in result["excluded"]} >= {"blocked_by_open_dependency", "waiting"}
        assert not result["candidate_coverage"]["complete"]


def test_live_breakage_inventory_finds_incident_beyond_repository_issue_bound() -> None:
    module = load_module()
    marker = module.github_direction_next.LIVE_BREAKAGE_LABEL
    incident = global_issue("someone/quiet", 243, labels=[marker])
    calls = []

    def collect(path: str, **kwargs: Any) -> Any:
        calls.append((path, kwargs))
        if path == "/installation/repositories":
            return "automation-gh", [{"full_name": "someone/quiet"}]
        if kwargs["query"].get("labels") == marker:
            assert kwargs["limit"] == module.NEXT_PLAN_INVENTORY_LIMIT + 1
            return "automation-gh", [incident]
        return "automation-gh", [global_issue("someone/quiet", 1), global_issue("someone/quiet", 2), incident]

    args = next_args()
    args.repository_issue_limit = 1
    with patch.multiple(module.github_identity, github_app_config=lambda: {}), patch.multiple(module, collect_paged_rest_items=collect, load_direction=lambda *_: DIRECTION):
        inventory, coverage = module.discover_direction_work("someone/direction", args)
    assert {item["number"] for item in inventory} == {1, 243}
    assert not coverage["complete"]  # Ordinary issue inventory remains bounded.
    assert not any(options["query"].get("labels") for _path, options in calls)


def test_milestone_candidate_coverage_is_scoped_to_graph() -> None:
    root = track("someone/direction", 1, "First")
    leaf = global_issue("someone/product", 2)
    with global_fixture([root], [leaf], {(root["repo"], 1): relationships(sub_issues=[leaf])}) as (module, result, _reads):
        with patch.multiple(module.github_milestone_core, show_milestone=lambda *_a, **_kw: {"milestone": root["milestone"]}):
            module.cmd_next(next_args(milestone="First"))
        assert result["candidate_coverage"]["scope"] == "milestone"
        assert result["candidate_coverage"]["complete"]
        assert result["candidate_coverage"]["warning"] is None

def test_discovered_blocker_explains_its_native_link_to_waiting_track_work() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    waiting = global_issue("someone/direction", 7, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Alex to decide.", waiting_for='Alex to decide.')
    blocker = global_issue("someone/product", 12)
    edges = {(roots[0]["repo"], 1): relationships(sub_issues=[waiting]), (waiting["repo"], 7): relationships(blocked_by=[blocker]), (blocker["repo"], 12): relationships(blocking=[waiting])}
    wait_module = load_module().github_plan_wait
    comments = {(waiting["repo"], 7): [{"user": {"login": "someone"}, "body": wait_module.marker(wait_module.reviewed(
        evidence="Parent waits on a decision; native prerequisites are independent records.",
        waiting_for="Alex to decide", ends_when="Alex answers"))}]}
    with global_fixture(roots, [waiting, blocker], edges, discovered=[blocker], comments=comments) as (module, result, _reads):
        module.discover_direction_work = lambda *_a, **_kw: ([blocker], {"complete": True, "repositories": [{"repo": blocker["repo"], "direction": DIRECTION}]})
        module.cmd_next(next_args())
        candidate = result["candidates"][0]
        assert candidate["overall_milestone_context"]["state"] == "matched"
        assert candidate["overall_milestone_context"]["titles"] == ["First"]
        assert candidate["availability"] == "needs_review"
        assert [step["number"] for step in candidate["via"]] == [1, 7, 12]
        summary = {entry["milestone"]: entry for entry in result["milestone_summary"]}
        assert [item["number"] for item in summary["First"]["work"]] == [12]


def test_service_waypoint_evidence_cannot_be_inferred_from_ranking_map() -> None:
    shared = load_module().github_direction_next
    raw = global_issue("someone/product", 1)
    candidate = {**raw, "url": raw["html_url"], "discussion": shared.discussion_snapshot(raw, [], complete=True)}
    candidate = shared.include_parent_context(candidate, [], complete=True, tracking_roots=[])
    graph = {"candidates": [], "dependency_context": {"complete": True}}
    kwargs = {"milestone_titles": [], "repository_milestones": {raw["repo"]: []}}
    result = shared.rank_portfolio_work(graph, [candidate], **kwargs)
    assert result["candidates"][0]["overall_milestone_context"]["state"] == "unknown"
    for evidence, expected in ((None, "unknown"), ([], "none_found")):
        result = shared.rank_portfolio_work(graph, [candidate], **kwargs, repository_waypoints={raw["repo"]: evidence})
        assert result["candidates"][0]["overall_milestone_context"]["state"] == expected


def test_waypoint_evidence_does_not_change_repository_ranking_fallback() -> None:
    older = global_issue("someone/tool", 20, created_at="2026-07-01T00:00:00Z")
    newer = global_issue("someone/tool", 10, created_at="2026-02-01T00:00:00Z")
    older["milestone"] = milestone_data(1, "v1", created_at="2026-01-01T00:00:00Z")
    newer["milestone"] = milestone_data(2, "v2", created_at="2026-06-01T00:00:00Z")
    inventory = [newer, older]
    for direction, expected in ((None, [20, 10]), ("# Direction\n## Purpose\nUseful tools", [10, 20])):
        with global_fixture([], [], {}, discovered=inventory) as (module, result, _reads):
            source = {"complete": True, "repositories": [{"repo": "someone/tool", "direction": direction}]}
            module.discover_direction_work = lambda *_a, **_kw: (inventory, source)
            module.cmd_next(next_args())
            assert [item["number"] for item in result["candidates"]] == expected


def test_global_native_summary_savings_reach_real_reader() -> None:
    root = track("someone/direction", 1, "First")
    leaf = global_issue("someone/product", 2)
    for item in (root, leaf):
        item["issue_dependencies_summary"] = {"total_blocked_by": 0, "total_blocking": 0}
        item["sub_issues_summary"] = {"total": 0}
    root["sub_issues_summary"]["total"] = 1
    calls: list[str] = []
    with global_fixture([root], [leaf], {(root["repo"], 1): relationships(sub_issues=[leaf])}, relationship_requests=calls) as (module, result, _reads):
        module.cmd_next(next_args())
        assert [item["number"] for item in result["candidates"]] == [2]
        assert calls == ["/repos/someone/direction/issues/1/sub_issues"]
    calls.clear()
    # Outside the tracking inventory this is an ordinary active parent;
    # a waiting parent must continue to exclude its discovered child.
    root["labels"] = ["plan", "plan:active"]
    root["_fixture_wait_reviewed"] = True
    with global_fixture([], [root], {(root["repo"], 1): relationships(sub_issues=[leaf])}, discovered=[leaf], relationship_requests=calls) as (module, result, _reads):
        module.cmd_next(next_args())
        assert [item["number"] for item in result["candidates"]] == [2]
        assert calls == ["/repos/someone/direction/issues/1/sub_issues"]


def test_unvisited_track_ancestry_and_unparsed_repository_direction_context() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    first = global_issue("someone/product", 3)
    later = global_issue("someone/product", 4)
    edges = {(roots[0]["repo"], 1): relationships(sub_issues=[first]), (roots[1]["repo"], 2): relationships(sub_issues=[later])}
    with global_fixture(roots, [first, later], edges, discovered=[later]) as (module, result, _reads):
        module.cmd_next(next_args(scan_limit=2))
        candidate = next(item for item in result["candidates"] if item["number"] == 4)
        assert candidate["overall_milestone_context"]["titles"] == ["Second"]
        assert candidate["overall_milestone_context"]["source"] == "native_track_ancestry"
        assert result["graph_context"]["truncated"]
        read = module.repository_direction_milestones
        assert read({"direction": "## Milestones\n- First without supported formatting"}) is None
        assert read({"direction": "## Other waypoints\n- `First`"}) is None
        assert read({"direction": "## Milestones\n"}) == []
        assert read({"direction": None}) == []
        assert read({"exclusion": "repository_held"}) is None
        assert read({"error": "not accessible", "direction": None}) is None
        assert read({"direction": "## Milestones\n- `First`"}) == ["First"]


def test_native_zero_totals_skip_reads_but_closed_history_and_unknowns_do_not() -> None:
    module = load_module()
    raw = issue(1)
    raw["issue_dependencies_summary"] = {"blocked_by": 0, "total_blocked_by": 0, "blocking": 0, "total_blocking": 1}
    raw["sub_issues_summary"] = {"total": 0}
    calls: list[str] = []

    def collect(path: str, **_kwargs: Any) -> Any:
        calls.append(path)
        return "automation-gh", [{**related(2), "state": "closed"}]

    with patch.multiple(module, collect_paged_rest_items=collect):
        actor, values, truncated = module.read_next_issue_relationships("owner/repo", 1, raw)
        assert actor == "automation-gh" and not truncated
        assert calls == ["/repos/owner/repo/issues/1/dependencies/blocking"]
        assert values["blocked_by"] == values["sub_issues"] == []
        assert values["blocking"][0]["state"] == "closed"
        for unknown in (None, False, "0", 0.0, -1):
            calls.clear()
            raw["issue_dependencies_summary"] = {"blocked_by": 0, "blocking": 0, "total_blocked_by": unknown, "total_blocking": unknown}
            raw["sub_issues_summary"] = {"total": unknown}
            module.read_next_issue_relationships("owner/repo", 1, raw)
            assert len(calls) == 3
        calls.clear()
        module.read_next_issue_relationships("owner/repo", 1, {"issue_dependencies_summary": [], "sub_issues_summary": None})
        assert len(calls) == 3


def test_overall_waypoint_context_requires_native_or_listed_direction_evidence() -> None:
    shared = load_module().github_direction_next
    candidate = global_issue("someone/product", 3)
    candidate["discussion"] = {"ancestry_complete": True, "parents": []}
    graph = {"candidates": [], "excluded": [], "dependency_context": {"complete": True}}
    context = shared.overall_milestone_context
    assert context(candidate, graph, ["First"], {"someone/product": []})["state"] == "none_found"
    assert context(candidate, graph, ["First"], {})["state"] == "unknown"
    graph["dependency_context"]["complete"] = False
    assert context(candidate, graph, ["First"], {"someone/product": []})["state"] == "unknown"
    candidate["milestone"] = milestone_data(1, "First", created_at="2026-07-01")
    assert context(candidate, graph, ["First"], {"someone/product": []})["state"] == "unknown"
    matched = context(candidate, graph, ["First"], {"someone/product": ["First"]})
    assert matched["titles"] == ["First"] and matched["source"] == "someone/product:DIRECTION.md"
    ancestor = {"repo": "someone/direction", "number": 1, "milestone": candidate["milestone"], "via": [{"repo": "someone/direction", "number": 1}]}
    graph["excluded"] = [ancestor]
    candidate["discussion"]["parents"] = [ancestor]
    matched = context(candidate, graph, ["First"], {})
    assert matched["titles"] == ["First"] and matched["source"] == "native_track_ancestry"
    candidate["blocking"] = [{"repo": "someone/unrelated", "number": 99, "state": "open"}]
    assert context(candidate, graph, ["First"], {})["source"] == "native_track_ancestry"
    candidate["blocking"].append({**ancestor, "state": "open"})
    assert context(candidate, graph, ["First"], {})["source"] == "native_track_links"
    candidate["via"] = ancestor["via"]
    assert context(candidate, graph, ["First"], {})["source"] == "native_track_path"


def test_skipped_repository_counts_exclude_graph_overlap_and_marked_incidents() -> None:
    root = track("someone/direction", 1, "First")
    linked = global_issue("someone/a", 2)
    ordinary = global_issue("someone/a", 3)
    omitted = global_issue("someone/b", 4)
    incident = global_issue("someone/b", 5, labels=["live-breakage"])
    inventory = [linked, ordinary, omitted, incident]
    with global_fixture([root], [linked], {(root["repo"], 1): relationships(sub_issues=[linked])}, discovered=inventory) as (module, result, _reads):
        module.cmd_next(next_args(scan_limit=2))
        # Two graph reads; ordinary discovery gets its own allowance of two.
        assert result["discovery_context"]["unevaluated_repositories"] == []
        assert result["discovery_context"]["evaluation_note"] is None
        module.cmd_next(next_args(scan_limit=1))
        # The graph bound leaves linked work to discovery; the incident is
        # still evaluated outside the one ordinary slot.
        counts = result["candidate_coverage"]["unevaluated_repositories"]
        assert counts == [{"repo": "someone/a", "issue_count": 1}, {"repo": "someone/b", "issue_count": 1}]
        assert sum(item["issue_count"] for item in counts) == result["discovery_context"]["unevaluated_count"]
        assert not result["candidate_coverage"]["complete"]
        assert all(item["number"] != incident["number"] for item in result["excluded"] if item.get("exclusion") == "outside_direction_tracks")
        assert "--scan-limit" in result["discovery_context"]["evaluation_note"]


def test_global_scan_diagnostic_counts_all_allowances_and_limit_only_caps_results() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    ordinary = [global_issue("someone/product", number) for number in range(3, 6)]
    held = [global_issue("someone/held", number) for number in range(6, 9)]
    other_family = [global_issue("someone/other", number, labels=["agent:claude"]) for number in range(9, 12)]
    incident = global_issue("someone/product", 12, labels=["live-breakage"])
    inventory = [*ordinary, *held, *other_family, incident]
    with global_fixture(roots, [], {}, discovered=inventory) as (module, result, _reads):
        args = next_args(scan_limit=1)
        args.agent = "codex"
        args.limit = 1
        module.cmd_next(args)
        bounded = result["discovery_context"].copy()
        assert bounded["evaluated"] == 2
        assert bounded["unevaluated_count"] == len(inventory) - bounded["evaluated"]
        assert "--scan-limit" in bounded["evaluation_note"]
        assert str(bounded["evaluated"]) in bounded["evaluation_note"]
        assert str(len(inventory)) in bounded["evaluation_note"]
        args.limit = 100
        module.cmd_next(args)
        assert result["discovery_context"] == bounded
        args.scan_limit = len(inventory)
        module.cmd_next(args)
        assert result["discovery_context"]["evaluated"] == len(inventory)
        assert result["discovery_context"]["evaluation_note"] is None
        assert result["discovery_context"]["unevaluated_count"] == 0


def test_client_requests_rank_with_product_without_changing_order_or_authority() -> None:
    shared = load_module().github_direction_next
    def candidate(repo: str, number: int, author: str, title: str | None = None) -> dict[str, Any]:
        raw = {**global_issue(repo, number, labels=[], created_at="2026-01-01T00:00:00Z"), "user": {"login": author}}
        raw["labels"] = []
        raw["milestone"] = {"title": title, "state": "open"} if title else None
        item = {**shared.compact_list_issue(repo, raw), "milestone": raw["milestone"],
                "discussion": shared.discussion_snapshot(raw, [], complete=True), "reasons": [], "direction_category": "own_project", "queue": {"state": "available"}}
        item["discussion"]["ancestry_complete"] = True
        return item
    client = candidate("someone/site", 20, "CLIENT")
    staff = candidate("someone/site", 21, "staff")
    absent = candidate("someone/no-client", 22, "client")
    another = candidate("someone/other", 23, "client")
    first = candidate("someone/business", 10, "bot", "First")
    first["via"] = ["Track"]
    graph = {"candidates": [first], "dependency_context": {"complete": True}}
    records = {"someone/site": {"status": "recorded", "source": "launchplane", "login": "client"},
               "someone/other": {"status": "recorded", "source": "launchplane", "login": "other-client"}}
    kwargs = dict(milestone_titles=["First", "Second"], repository_clients=records, director_owner="someone",
                  repository_waypoints={"someone/site": ["Second"], "someone/other": ["First"], "someone/no-client": ["First"]})
    result = shared.rank_portfolio_work(graph, [client, staff, absent, another], **kwargs)
    assert [item["number"] for item in result["available_candidates"]][:2] == [10, 20]
    assert {item["number"] for item in result["available_candidates"][2:]} == {21, 22, 23}
    selected = result["available_candidates"][1]
    assert selected["category"] == "milestone"
    assert selected["client_request"] == {"source": "launchplane", "milestone": "Second", "ranking_only": True, "basis": "exact_listed_title_match"}
    assert selected["labels"] == []
    assert client["milestone"] is None  # ranker never mutates the source request
    assert all("client_request" not in item for item in result["candidates"] if item["number"] in {21, 22, 23})
    external = {**client, "repo": "another/site"}
    external_kwargs = {**kwargs, "repository_clients": {"another/site": records["someone/site"]},
                       "repository_waypoints": {"another/site": ["First"]}}
    assert "client_request" not in shared.rank_portfolio_work(graph, [external], **external_kwargs)["candidates"][-1]
    completed = {**graph, "completed_milestones": ["Second"]}
    assert "client_request" not in shared.rank_portfolio_work(completed, [client], **kwargs)["candidates"][-1]
    incomplete = {**graph, "dependency_context": {"complete": False}}
    assert "client_request" not in shared.rank_portfolio_work(incomplete, [client], **kwargs)["candidates"][-1]
    assert "client_request" not in shared.rank_portfolio_work(graph, [client], **kwargs, bot_logins=("client",))["candidates"][-1]


def test_client_requests_inherit_native_product_track_and_still_need_reviews() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    product = global_issue("someone/site", 30)
    request = {**global_issue("someone/site", 31, labels=[]), "user": {"login": "client"}}
    with global_fixture(roots, [product], {(roots[1]["repo"], 2): relationships(sub_issues=[product])}, discovered=[request], clear_waits=[request]) as (module, result, _reads):
        with patch.object(module.github_client, "recorded_client", return_value={"status": "recorded", "source": "repo_people_overlay", "login": "client"}):
            module.cmd_next(next_args())
            item = next(item for item in result["candidates"] if item["number"] == 31)
            assert item["client_request"]["milestone"] == "Second"
            assert result["available_candidates"] == []


def test_client_track_precedence_keeps_real_membership_and_completed_assignments() -> None:
    shared = load_module().github_direction_next
    raw = {**global_issue("someone/site", 20), "user": {"login": "client"}}
    request = {**shared.compact_list_issue(raw["repo"], raw), "milestone": None,
               "discussion": {"complete": True, "ancestry_complete": True, "digest": "current"}}
    leaf = {**request, "number": 10, "via": ["Track"], "milestone": {"title": "Second", "state": "open"}}
    graph = {"candidates": [leaf], "dependency_context": {"complete": True}}
    kwargs = dict(milestone_titles=["First", "Second"], director_owner="someone",
                  repository_clients={"someone/site": {"status": "recorded", "source": "launchplane", "login": "client"}},
                  repository_waypoints={"someone/site": ["First", "Product work"]})
    result = shared.rank_portfolio_work(graph, [request], **kwargs)
    item = next(item for item in result["candidates"] if item["number"] == 20)
    assert item["client_request"]["milestone"] == "Second"  # native Track wins over First waypoint
    assert item["milestone"] is None
    local = {**request, "milestone": {"number": 3, "title": "Product work", "state": "open"}}
    item = next(item for item in shared.rank_portfolio_work(graph, [local], **kwargs)["candidates"] if item["number"] == 20)
    assert item["milestone"] == local["milestone"]
    assert item["client_request"]["milestone"] == "Second"
    completed = {**graph, "completed_milestones": ["First"]}
    old = {**request, "milestone": {"title": "First", "state": "open"}}
    item = next(item for item in shared.rank_portfolio_work(completed, [old], **kwargs)["candidates"] if item["number"] == 20)
    assert "client_request" not in item
    assert item["milestone"] == old["milestone"]


def test_global_waits_belong_to_their_own_open_waiting_issue() -> None:
    root = track("someone/direction", 3, "First")
    root["body"] = "## Current Status\nState: Waiting.\nWaiting for: #91\nLast verified: 2026-10-02"
    closed = global_issue("someone/product", 167, state="closed", labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Chris's approval.", waiting_for="Chris's approval.")
    active = global_issue("someone/product", 124, wait_reviewed=False, body="## Current Status\nWaiting for: nothing; this is agent work")
    pending = global_issue("someone/product", 141, wait_reviewed=False, labels=["plan", "plan:waiting"], body="## Current Status\nState: Waiting.\nBlocked by: None\nWaiting for: #167\nLast verified: 2026-10-05")
    edges = {(root["repo"], 3): relationships(sub_issues=[active, pending, closed])}
    with global_fixture([root], [active, pending, closed], edges, clear_waits=[active]) as (module, result, _reads):
        module.cmd_next(next_args())
    assert {(row["repo"], row["number"]) for row in result["recorded_waits"]} == {("someone/direction", 3)}
    own = next(row for row in result["stale_waits"] if row["number"] == 141)
    assert own["last_verified"] == "2026-10-05"
    assert own["closed_references"][0]["number"] == 167
    assert own["stale_wait_evidence"]["evidence"][0]["kind"] == "completed_wait_issue"
    assert own["review_required"] is True
    track_wait = next(row for row in result["recorded_waits"] if row["number"] == 3)
    assert track_wait["references"][0]["repo"] == "someone/direction"
    assert track_wait["references"][0]["state"] == "unknown"
    assert result["stale_wait_report"]["complete"] is False
    assert any(item["number"] == 124 for item in result["candidates"])
    assert not any(row["number"] in {91, 124, 167} for row in result["waiting"])


def test_global_agent_only_waits_are_unowned_and_real_holds_stay_parked() -> None:
    reasons = ["Supervisor routing", "engineering selection", "separately authorized activation", "future work", "None", "live acceptance"]
    items = [global_issue("someone/tools", number, wait_reviewed=False, labels=["plan", "plan:waiting"], body="## Current Status\nState: Waiting.\nBlocked by: None\nWaiting for: " + reason) for number, reason in enumerate(reasons, 1)]
    genuine = global_issue("someone/tools", 20, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Chris to perform the device test after #21 closes.", waiting_for='Chris to perform the device test after #21 closes.')
    closed = global_issue("someone/tools", 21, state="closed")
    with global_fixture([], [closed], {}, discovered=[*items, genuine]) as (module, result, _reads):
        module.cmd_next(next_args())
    assert {row["number"] for row in result["unowned"]} == set(range(1, 7))
    assert [row["number"] for row in result["waiting"]] == [20]
    assert result["waiting"][0]["stale_wait_evidence"] is None
    assert {row["number"] for row in result["stale_wait_report"]["items"]} == {1, 2, 4, 5}
    assert result["candidates"] == []  # Report-only evidence never releases a hold.


def test_global_wait_reference_budget_and_auth_stop_are_explicit() -> None:
    module = load_module()
    item = global_issue("someone/business", 10, wait_reviewed=False, body="## Current Status\nWaiting for: Review " + " ".join(f"#{number}" for number in range(20, 32)))
    reads = []
    def read(method: str, path: str, **_kwargs: Any) -> Any:
        assert method == "GET"
        reads.append(path)
        return "automation-gh", {"state": "open"}
    with patch.multiple(module, api_json=read, load_config=lambda *_: module.DEFAULT_CONFIG):
        report = module.next_wait_context([item], scan_limit=1, inventory_complete=True)
    assert report["complete"] is False
    assert len(reads) == report["read_count"] == report["read_limit"]
    assert report["unavailable"]
    failure = module.PlanError("quota stop", failure=module.github_api_core.FailureDetail(cause="rest_primary_rate_limited", message="quota exhausted", retryable=False, fallback_eligible=False, disposition="stop"))
    with patch.multiple(module, api_json=Mock(side_effect=failure), load_config=lambda *_: module.DEFAULT_CONFIG):
        try:
            module.next_wait_context([item], scan_limit=1, inventory_complete=True)
        except module.PlanError as exc:
            assert exc is failure
        else:
            raise AssertionError("quota failure must retain the shared stop policy")


def test_global_no_wait_notes_do_not_park_active_work() -> None:
    root = track("someone/direction", 1, "First")
    for note in ("\n\nThe behavior tests pass.", "\n- Notes: The next agent can implement this."):
        ready = global_issue("someone/product", 2, body="## Current Status\nState: Active.\nBlocked by: None\nWaiting for: None" + note)
        with global_fixture([root], [ready], {(root["repo"], 1): relationships(sub_issues=[ready])}) as (module, result, _reads):
            module.cmd_next(next_args())
        assert [item["number"] for item in result["candidates"]] == [2]
        assert result["waiting"] == result["recorded_waits"] == []


def test_global_unowned_authorization_holds_never_become_candidates_or_stale() -> None:
    for labels in ([], ["plan:waiting"]):
        item = global_issue("someone/tools", 10, wait_reviewed=False, labels=labels, body="## Current Status\nState: Waiting.\nBlocked by: None\nWaiting for: separately authorized activation")
        with global_fixture([], [], {}, discovered=[item]) as (module, result, _reads):
            module.cmd_next(next_args())
        assert result["candidates"] == []
        assert next(row for row in result["excluded"] if row["number"] == 10)["exclusion"] == "wait_review_required"
        assert result["stale_wait_report"]["items"] == []
        assert result["unowned"][0]["number"] == 10


def test_global_cross_repo_link_label_does_not_create_a_local_reference() -> None:
    pending = global_issue("someone/business", 10, labels=["plan:waiting"], body="## Current Status\nWaiting for: Alex to review [#12](https://github.com/other/product/issues/12)", waiting_for='Alex to review [#12](https://github.com/other/product/issues/12)')
    other = global_issue("other/product", 12)
    wrong = global_issue("someone/business", 12, state="closed")
    with global_fixture([], [other, wrong], {}, discovered=[pending]) as (module, result, _reads):
        base_api = module.api_json
        reads = []
        def api(method: str, path: str, **kwargs: Any) -> Any:
            reads.append(path)
            return base_api(method, path, **kwargs)
        with patch.multiple(module, api_json=api):
            module.cmd_next(next_args())
    assert [(ref["repo"], ref["number"]) for ref in result["waiting"][0]["references"]] == [("other/product", 12)]
    assert result["waiting"][0]["closed_references"] == []
    assert "/repos/someone/business/issues/12" not in reads


def test_global_parent_only_stale_wait_moves_out_of_current_waiting() -> None:
    parent = global_issue("someone/business", 10, wait_reviewed=False, labels=["plan:waiting"], body="## Current Status\nState: Waiting.\nBlocked by: None\nWaiting for: PR #11 to merge")
    child = global_issue("someone/business", 20)
    edges = {(parent["repo"], 10): relationships(sub_issues=[child])}
    with global_fixture([], [parent], edges, discovered=[child]) as (module, result, _reads):
        base_api = module.api_json
        def merged(method: str, path: str, **kwargs: Any) -> Any:
            if path == "/repos/someone/business/issues/11":
                return "automation-gh", {"state": "closed", "pull_request": {}}
            if path == "/repos/someone/business/pulls/11":
                return "automation-gh", {"merged_at": "2026-10-05T12:00:00Z", "base": {"ref": "main"}}
            return base_api(method, path, **kwargs)
        with patch.multiple(module, api_json=merged):
            module.cmd_next(next_args())
    assert result["waiting"] == []
    assert result["stale_waits"][0]["number"] == 10
    assert result["candidates"] == []
    assert next(item for item in result["excluded"] if item["number"] == 20)["exclusion"] == "parent_waiting"


def test_global_adjacent_notes_preserve_absence_and_pending_clauses() -> None:
    root = track("someone/direction", 1, "First")
    for note, selectable in (("PR #12 merged; next agent continues step 2.", True), ("Chris's acceptance is pending.", False)):
        raw = global_issue("someone/product", 2, body="## Current Status\nState: Active.\nWaiting for: Nothing.\n" + note,
                           waiting_for=note if not selectable else None)
        with global_fixture([root], [raw], {(root["repo"], 1): relationships(sub_issues=[raw])}) as (module, result, _reads):
            module.cmd_next(next_args())
        assert bool(result["candidates"]) is selectable
    raw = global_issue("someone/product", 2, wait_reviewed=False, labels=[], body="## Current Status\nWaiting for: provider capacity")
    with global_fixture([], [], {}, discovered=[raw]) as (module, result, _reads):
        module.cmd_next(next_args())
    assert result["candidates"] == []
    assert result["unowned"][0]["waiting_for"] == "provider capacity"
    assert result["excluded"][0]["exclusion"] == "wait_review_required"
    # The Supervisor acting next is agent work, offered with the step it names.
    raw = global_issue("someone/product", 2, labels=[], body="## Current Status\nNext action: Supervisor routes the PR.")
    with global_fixture([], [], {}, discovered=[raw], clear_waits=[raw]) as (module, result, _reads):
        module.cmd_next(next_args())
    assert "Supervisor routes the PR." in result["candidates"][0]["discussion"]["body"]
    assert result["unowned"] == []


def test_global_wait_comments_named_actors_and_wrapped_urls_use_own_evidence() -> None:
    root = track("someone/direction", 1, "First")
    comment = global_issue("someone/product", 2, wait_reviewed=False, labels=["plan:waiting"], body="## Current Status\n<!--\nWaiting for: who or what\n-->")
    named = global_issue("someone/product", 3, wait_reviewed=False, labels=["plan:waiting"], body="## Current Status\nWaiting for: separately authorized activation by Chris")
    wrapped = global_issue("someone/product", 4, labels=["plan:waiting"], body="## Current Status\nWaiting for: approved prerequisite readback\nhttps://github.com/other/product/issues/12",
                           waiting_for="approved prerequisite readback\nhttps://github.com/other/product/issues/12")
    target = global_issue("other/product", 12, state="closed")
    with global_fixture([root], [comment, named, wrapped, target], {(root["repo"], 1): relationships(sub_issues=[comment, named, wrapped])}) as (module, result, _reads):
        module.cmd_next(next_args())
    assert not any(row["number"] == 2 for row in result["waiting"])
    assert any(row["number"] == 2 for row in result["unowned"])
    assert not any(row["number"] == 3 for row in result["unowned"])
    rows = {row["number"]: row for row in result["waiting"]}
    assert rows[4]["closed_references"][0]["repo"] == "other/product"
    assert rows[4]["stale_wait_evidence"] is None
    assert module.github_direction_next.waiting_records({**module.compact_list_issue(comment['repo'], comment), "plan_status": "waiting"}, comment['body']) == []


def test_local_and_global_next_exclude_stale_landing_but_keep_split_work() -> None:
    status = "## Current Status\nState: Source complete.\nNext action: Supervisor lands PR https://github.com/someone/product/pull/71 and reconciles closure.\nWaiting for: None.\n"
    stale = global_issue("someone/product", 10, body=status)
    split = global_issue("someone/product", 11, body=status.replace("Supervisor lands PR", "Test split remainder after PR"))
    person = global_issue("someone/product", 12, body=split["body"].replace("Waiting for: None.", "Waiting for: Chris to test on the phone."),
                          waiting_for="Chris to test on the phone.")
    root = track("someone/direction", 1, "First")
    edges = {(root["repo"], 1): relationships(sub_issues=[stale, split, person])}
    with global_fixture([root], [stale, split, person], edges) as (module, result, _reads):
        real_api = module.api_json
        def merged(method: str, path: str, **kwargs: Any) -> Any:
            if path == "/repos/someone/product/pulls/71":
                return "automation-gh", {"merged_at": "2026-10-05T12:00:00Z", "base": {"ref": "main"}, "body": "Refs #10\nRefs #11\nRefs #12"}
            return real_api(method, path, **kwargs)
        with patch.multiple(module, api_json=merged):
            module.cmd_next(next_args())
            assert [row["number"] for row in result["candidates"]] == [11]
            assert any(row["number"] == 12 and "Chris" in row["waiting_for"] for row in result["waiting"])
            assert not any(row["number"] == 12 for row in result["stale_waits"])
            excluded = next(row for row in result["excluded"] if row["number"] == 10)
            assert excluded["exclusion"] == "landed_status_needs_reconciliation"
            assert not excluded["post_merge_evidence"]["completion_proven"]
            result.clear()
            global_collect = module.collect_paged_rest_items
            with patch.multiple(module, collect_paged_rest_items=lambda path, **kw: global_collect(path, **kw) if path.endswith("/comments") else ("automation-gh", [stale, split])):
                module.cmd_next(next_args(repo="someone/product"))
            assert [row["number"] for row in result["candidates"]] == [11]
            assert result["candidate_count"] == 1
            assert result["candidates"][0]["post_merge_evidence"]["selection_exclusion"] is False
            assert {row["number"] for row in result["stale_wait_report"]["items"]} == {10, 11}


def test_active_post_merge_read_gaps_remain_visible_on_selection_candidates() -> None:
    module = load_module()
    row = global_issue("someone/product", 10, body="## Current Status\nNext action: Supervisor lands PR https://github.com/someone/product/pull/71 and closes this issue.\n")
    with patch.multiple(module, load_config=lambda *_: module.DEFAULT_CONFIG,
                        api_json=Mock(side_effect=module.PlanError("not accessible"))):
        report = module.next_wait_context([row], scan_limit=1, inventory_complete=True)
    candidates = [module.compact_list_issue(row["repo"], row)]
    excluded = []
    module.exclude_landed_candidates(candidates, excluded, report)
    assert not report["complete"] and report["unavailable"]
    assert candidates[0]["post_merge_evidence_complete"] is False and not excluded
    unchecked = global_issue("someone/product", 11)
    candidates = [module.compact_list_issue(unchecked["repo"], unchecked)]
    module.exclude_landed_candidates(candidates, excluded, report)
    assert candidates[0]["post_merge_evidence_complete"] is False


def test_bare_reference_missing_here_keeps_post_merge_evidence_complete() -> None:
    # A bare #104 written for another repository is absent here: read, not unread.
    module = load_module()
    row = global_issue("someone/product", 10, body="## Current Status\nNext action: Website agents finish #104 company identity.\n")
    def fetch(cause: str) -> Any:
        def read(_method: str, path: str, **_kwargs: Any) -> Any:
            if path == "/repos/someone/product":
                return "automation-gh", {"default_branch": "main", "archived": False}
            failure = module.github_api_core.FailureDetail(cause=cause, message="cannot read", retryable=False,
                                                           fallback_eligible=False, disposition="stop")
            raise module.PlanError("cannot read", failure=failure)
        return read
    with patch.multiple(module, load_config=lambda *_: module.DEFAULT_CONFIG, api_json=fetch("not_found")):
        report = module.next_wait_context([row], scan_limit=1, inventory_complete=True)
    candidates = [module.compact_list_issue(row["repo"], row)]
    module.exclude_landed_candidates(candidates, [], report)
    assert report["complete"] and not report["unavailable"]
    assert [ref["reference"] for ref in report["unresolved_references"]] == ["#104"]
    assert "post_merge_evidence_complete" not in candidates[0]
    with patch.multiple(module, load_config=lambda *_: module.DEFAULT_CONFIG, api_json=fetch("permission_denied")):
        report = module.next_wait_context([row], scan_limit=1, inventory_complete=True)
    assert not report["complete"] and report["unavailable"] and not report["unresolved_references"]
    # A parked wait on the same bare reference is read, and its wait stays pending.
    parked = global_issue("someone/product", 11, wait_reviewed=False, labels=["plan", "plan:waiting"],
                          body="## Current Status\nState: Waiting.\nWaiting for: #104 to close.\n")
    def waiting(cause: str) -> Any:
        read = fetch(cause)
        def wrapped(method: str, path: str, **kwargs: Any) -> Any:
            if "/dependencies/blocked_by" in path:
                return "automation-gh", []
            return read(method, path, **kwargs)
        return wrapped
    with patch.multiple(module, load_config=lambda *_: module.DEFAULT_CONFIG, api_json=waiting("not_found")):
        report = module.next_wait_context([parked], scan_limit=1, inventory_complete=True)
    candidates = [module.compact_list_issue(parked["repo"], parked)]
    module.exclude_landed_candidates(candidates, [], report)
    assert report["complete"] and not report["unavailable"] and not report["items"]
    assert {ref["source"] for ref in report["unresolved_references"]} == {"wait_reference"}
    assert report["references"]["https://github.com/someone/product/issues/104"]["state"] == "unknown"
    assert "post_merge_evidence_complete" not in candidates[0]
    named = {**parked, "number": 12, "body": parked["body"].replace("#104", "someone/product#104")}
    with patch.multiple(module, load_config=lambda *_: module.DEFAULT_CONFIG, api_json=waiting("not_found")):
        report = module.next_wait_context([named], scan_limit=1, inventory_complete=True)
    assert not report["complete"] and report["unavailable"] and not report["unresolved_references"]
    # Each occurrence is classified on its own, whichever issue reads the target first.
    prose = [{**row, "body": f"## Current Status\nState: Waiting.\nWaiting for: approval on {ref}.\n"}
             for row, ref in ((parked, "#104"), (named, "someone/product#104"))]
    for order in (prose, prose[::-1]):
        with patch.multiple(module, load_config=lambda *_: module.DEFAULT_CONFIG, api_json=waiting("not_found")):
            report = module.next_wait_context(order, scan_limit=2, inventory_complete=True)
        assert not report["complete"]
        assert {(row["number"], row["source"]) for row in report["unavailable"]} == {(12, "wait_reference")}
        assert {(row["number"], row["source"]) for row in report["unresolved_references"]} == {(11, "wait_reference")}


def test_active_timestamp_uncertainty_and_custom_labels_reach_selection_output() -> None:
    module = load_module()
    row = global_issue("someone/product", 10, labels=["plan", "custom-active"],
                       updated_at="2026-10-06T12:00:00Z",
                       body="## Current Status\nNext action: Supervisor lands PR https://github.com/someone/product/pull/71 and closes this issue.\n")
    def fetch(_method: str, path: str, **_kwargs: Any) -> Any:
        return "automation-gh", ({"default_branch": "main", "archived": False} if path == "/repos/someone/product" else
                                 {"merged_at": "2026-10-05T12:00:00Z", "base": {"ref": "main"}, "body": "Refs #10"})
    config = {**module.DEFAULT_CONFIG, "labels": {**module.DEFAULT_CONFIG["labels"], "active": "custom-active"}}
    with patch.multiple(module, api_json=fetch, load_config=lambda *_: config):
        report = module.next_wait_context([row], scan_limit=1, inventory_complete=True)
    candidates = [module.compact_list_issue(row["repo"], row)]
    excluded = []
    module.exclude_landed_candidates(candidates, excluded, report)
    assert report["checked_issues"] == [{"repo": row["repo"], "number": 10}]
    assert report["complete"] and report["items"][0]["selection_exclusion"]
    assert not candidates and excluded[0]["post_merge_evidence"]["evidence_complete"]
    assert "post_merge_evidence_complete" not in excluded[0]
    row["labels"] = [{"name": "plan"}]
    with patch.multiple(module, api_json=Mock(side_effect=AssertionError("unlabeled record is unexamined")), load_config=lambda *_: config):
        report = module.next_wait_context([row], scan_limit=1, inventory_complete=True)
    candidates = [module.compact_list_issue(row["repo"], row)]
    module.exclude_landed_candidates(candidates, [], report)
    assert not report["checked_issues"]
    assert "post_merge_evidence_complete" not in candidates[0]
    active = {**row, "number": 11, "labels": ["plan", "custom-active"],
              "body": "## Current Status\nNext action: Implement the remaining phone fix.\n"}
    with patch.multiple(module, api_json=Mock(return_value=("automation-gh", {"archived": False})), load_config=lambda *_: config):
        report = module.next_wait_context([row, active], scan_limit=1, inventory_complete=True)
    assert report["complete"] and report["checked_issues"] == [{"repo": row["repo"], "number": 11}]


def test_global_stale_wait_report_reuses_merged_pr_proof_and_refuses_capacity() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    waiting = global_issue("someone/business", 10, wait_reviewed=False, labels=["plan", "plan:waiting"], body="## Current Status\nState: Waiting.\nBlocked by: None\nWaiting for: PR #11 to merge\nLast verified: 2026-10-01")
    tool = global_issue("someone/tools", 20)
    edges = {(root["repo"], root["number"]): relationships(sub_issues=[waiting]) for root in roots}
    with global_fixture(roots, [waiting], edges, discovered=[tool]) as (module, result, _reads):
        base_api = module.api_json
        def merged(method: str, path: str, **kwargs: Any) -> Any:
            assert method == "GET"
            if path == "/repos/someone/business/issues/11":
                return "automation-gh", {"state": "closed", "pull_request": {}}
            if path == "/repos/someone/business/pulls/11":
                return "automation-gh", {"merged_at": "2026-10-05T12:00:00Z", "base": {"ref": "main"}}
            return base_api(method, path, **kwargs)
        with patch.multiple(module, api_json=merged):
            module.cmd_next(next_args())
    assert result["stale_wait_report"]["items"][0]["evidence"][0]["kind"] == "merged_wait_pr"
    assert result["all_listed_milestones_have_no_available_item"] is None
    assert result["available_candidates"] == []
    assert result["waiting"] == []
    assert result["stale_waits"][0]["number"] == 10
    assert result["stale_waits"][0]["last_verified"] == "2026-10-01"


def test_graph_wait_ancestry_keeps_intermediate_nodes_and_one_capacity_frontier() -> None:
    root = track("someone/direction", 1, "First")
    parent = global_issue("someone/product", 10, wait_reviewed=False, labels=["plan", "plan:waiting"])
    middle = global_issue(parent["repo"], 11)
    leaf = global_issue(parent["repo"], 12)
    tool = global_issue("someone/tools", 20)
    edges = {(root["repo"], 1): relationships(sub_issues=[parent]),
             (parent["repo"], 10): relationships(sub_issues=[middle]),
             (middle["repo"], 11): relationships(sub_issues=[leaf])}
    module = load_module()
    marker = module.github_plan_wait.marker(module.github_plan_wait.reviewed(
        evidence="#12 owns independently selectable preparation; #11 remains held.",
        waiting_for="Client sequencing", ends_when="Client answers"))
    comments = {(parent["repo"], 10): [{"user": {"login": "someone"}, "body": marker}]}
    with global_fixture([root], [parent, middle, leaf], edges, comments=comments, discovered=[tool], clear_waits=[tool]) as (module, result, _reads):
        module.load_direction = lambda *_: "# Direction\n## Milestones\n- `First`\n"
        module.cmd_next(next_args())
        assert not any(row["number"] == 12 for row in result["candidates"])
        blocked = next(row for row in result["excluded"] if row["number"] == 12)
        assert blocked["exclusion"] == "parent_waiting"
        assert [row["number"] for row in blocked["discussion"]["parents"]] == [11, 10]


TESTS = [
    test_active_timestamp_uncertainty_and_custom_labels_reach_selection_output,
    test_bare_reference_missing_here_keeps_post_merge_evidence_complete,
    test_active_post_merge_read_gaps_remain_visible_on_selection_candidates,
    test_local_and_global_next_exclude_stale_landing_but_keep_split_work,
    test_global_adjacent_notes_preserve_absence_and_pending_clauses,

    test_global_wait_comments_named_actors_and_wrapped_urls_use_own_evidence,


    test_global_no_wait_notes_do_not_park_active_work,
    test_global_unowned_authorization_holds_never_become_candidates_or_stale,
    test_global_cross_repo_link_label_does_not_create_a_local_reference,
    test_global_parent_only_stale_wait_moves_out_of_current_waiting,

    test_global_waits_belong_to_their_own_open_waiting_issue,
    test_global_agent_only_waits_are_unowned_and_real_holds_stay_parked,
    test_global_stale_wait_report_reuses_merged_pr_proof_and_refuses_capacity,
    test_global_wait_reference_budget_and_auth_stop_are_explicit,


    test_client_track_precedence_keeps_real_membership_and_completed_assignments,
    test_client_requests_rank_with_product_without_changing_order_or_authority,
    test_client_requests_inherit_native_product_track_and_still_need_reviews,
    test_discovered_blocker_explains_its_native_link_to_waiting_track_work,
    test_service_waypoint_evidence_cannot_be_inferred_from_ranking_map,
    test_waypoint_evidence_does_not_change_repository_ranking_fallback,
    test_global_native_summary_savings_reach_real_reader,
    test_unvisited_track_ancestry_and_unparsed_repository_direction_context,
    test_native_zero_totals_skip_reads_but_closed_history_and_unknowns_do_not,
    test_overall_waypoint_context_requires_native_or_listed_direction_evidence,
    test_skipped_repository_counts_exclude_graph_overlap_and_marked_incidents,
    test_milestone_candidate_coverage_is_scoped_to_graph,
    test_live_breakage_beyond_scan_window_ranks_first_without_granting_availability,
    test_live_breakage_marker_preserves_holds_dependencies_waits_and_review,
    test_live_breakage_inventory_finds_incident_beyond_repository_issue_bound,
    test_local_next_inbound_scan_preserves_plan_budget_and_waits,
    test_inbound_scan_bounds_and_partial_reads_are_explicit,
    test_inbound_unclassified_failure_degrades_report_but_quota_stops,
    test_portfolio_nonempty_unparsed_direction_and_local_only_flags_refuse,

    test_portfolio_discovery_preserves_parent_waits_and_ancestry_discussions,

    test_portfolio_empty_milestones_and_unspecified_wait_are_supported,

    test_portfolio_nontrack_direction_issues_and_repository_milestone_order,
    test_portfolio_repository_inventory_truncation_and_auth_failure,


    test_portfolio_inventory_failure_and_scan_bound_never_claim_full_coverage,
    test_next_beta_rc_stable_chain_respects_native_blockers,
    test_next_excludes_non_actionable_states_with_reasons,
    test_stale_project_focus_does_not_hide_ready_work,
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
    test_global_stale_blocked_parent_is_a_finding_with_complete_coverage,
    test_global_empty_tracks_are_not_reported_as_people_waits,
    test_global_service_consumer_uses_the_same_classification_and_sort_as_cli,
    test_global_placeholder_waits_do_not_park_actionable_work,
    test_global_active_parent_partial_wait_need_not_name_an_issue,
    test_global_shared_classifier_preserves_incomplete_relationship_evidence,
]


def test_milestone_order_wait_is_a_finding_and_remains_available() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    for reason in ("starts after milestones 'First' and 'Second'", "", "nothing", "TBD", "TBD; since 2026-08-20", "unknown; since 2026-08-20"):
        leaf = global_issue("someone/business", 120, wait_reviewed=False, labels=["plan", "plan:waiting"],
                            body="## Current Status\nState: Waiting.\nWaiting for: " + reason)
        edges = {(roots[1]["repo"], 2): relationships(sub_issues=[leaf])}
        with global_fixture(roots, [leaf], edges) as (module, result, _reads):
            module.cmd_next(next_args())

            assert result["available_candidates"] == []  # Requires the supported structured wait review first.
            assert any(item["number"] == 120 for item in result["candidates"])
            assert any(item["number"] == 120 and item["kind"] == "milestone_wait_invalid" for item in result["findings"])
            assert not any(item["number"] == 120 for item in result["waiting"])
            assert result["all_listed_milestones_have_no_available_item"] is None


def test_milestone_order_detection_preserves_additional_human_holds() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    for reason in ("First and Chris approves the testing date.", "starts after First and Chris to sign the contract.",
                   "First\nParked until: Alex approves the copy.",
                   "First\nBlocked by: Chris's contract signature.", "First\nState: Waiting on Alex."):
        leaf = global_issue("someone/business", 120, wait_reviewed=False, labels=["plan", "plan:waiting"],
                            body="## Current Status\nWaiting for: " + reason)
        with global_fixture(roots, [leaf], {(roots[1]["repo"], 2): relationships(sub_issues=[leaf])}) as (module, result, _reads):
            module.cmd_next(next_args())
            assert not result["candidates"] and not result["findings"]
            assert result["recorded_waits"]
    shared = load_module().github_direction_next
    item = {"repo": "someone/business", "number": 120, "url": "https://github.com/someone/business/issues/120"}
    for reason in ("First to ship", "Second is done", "earlier milestones to finish", "the previous milestone", "the next milestone", "First closes", "First is complete", "First wraps up", "First to be completed", "‘First’", "v2_beta"):
        assert not shared.milestone_wait_evidence(item, "Waiting for: " + reason, ["First", "Second", "v2_beta"])["valid"]
    assert shared.milestone_wait_evidence(item, "Waiting for: Alex to retest the build shipped since 2026-07-01", ["First"])["since"] is None
    assert shared.milestone_wait_evidence(item, "Waiting for: Alex to test; since 2026-08-20", ["First"])["since"] == "2026-08-20"


TESTS.extend([test_milestone_order_detection_preserves_additional_human_holds,  test_milestone_order_wait_is_a_finding_and_remains_available,
              ])


def test_discovered_milestone_wait_remains_available_and_cannot_admit_tooling() -> None:
    leaf = global_issue("someone/business", 120, wait_reviewed=False, labels=["plan", "plan:waiting"],
                        milestone=milestone_data(7, "First", created_at="2026-01-01"),
                        body="## Current Status\nWaiting for: milestone Second.")
    with global_fixture([], [], {}, discovered=[leaf]) as (module, result, _reads):
        module.discover_direction_work = lambda *_a, **_kw: ([leaf], {"complete": True,
            "repositories": [{"repo": leaf["repo"], "direction": DIRECTION}]})
        module.cmd_next(next_args())
        item = result["candidates"][0]
        assert item["discussion"]["ancestry_complete"] is True

        assert result["available_candidates"] == []  # Requires the supported structured wait review first.
        assert any(item["number"] == 120 for item in result["candidates"])
        assert result["findings"][0]["reason"] == "wait_names_another_milestone"
        assert result["all_listed_milestones_have_no_available_item"] is None


def test_supported_wait_forms_and_person_work_do_not_become_ordering_findings() -> None:
    roots = [track("someone/direction", 1, "First")]
    for status in ("Parked until: 2026-12-01 holiday freeze.", "**Waiting for:** Alex to test.", "**Waiting for**: Alex to test.",
                   "Blocked by: No native issue blocker; waiting for Alex to test.\nWaiting for:\nLast verified: 2026-10-01",
                   "Blocked by: No native issue blocker; waiting for Alex to test.",
                   "Waiting for: Alex to approve the beta copy."):
        leaf = global_issue("someone/business", 120, wait_reviewed=False, labels=["plan:waiting"],
                            body="## Current Status\nState: Parked.\n" + status)
        with global_fixture(roots, [leaf], {(roots[0]["repo"], 1): relationships(sub_issues=[leaf])}) as (module, result, _reads):
            module.load_direction = lambda *_: DIRECTION + "- `Beta`\n"
            module.cmd_next(next_args())
            assert not result["candidates"]
            assert not result["findings"]
        evidence = module.github_direction_next.milestone_wait_evidence(
            {"repo": "someone/business", "number": 120, "url": leaf["html_url"], "milestone": {"title": "Dogfood week"}}, "Waiting for: Chris to start dogfood week.", ["Dogfood week"])
        assert evidence["valid"] is True


def test_unmatched_local_milestone_keeps_its_recorded_wait() -> None:
    leaf = global_issue("someone/business", 120, wait_reviewed=False, labels=["plan:waiting"],
                        milestone=milestone_data(7, "First", created_at="2026-01-01"),
                        body="## Current Status\nWaiting for: TBD.")
    with global_fixture([], [], {}, discovered=[leaf]) as (module, result, _reads):
        module.discover_direction_work = lambda *_a, **_kw: ([leaf], {"complete": True,
            "repositories": [{"repo": leaf["repo"], "direction": "# Direction\n## Milestones\n"}]})
        module.cmd_next(next_args())
        assert not result["candidates"] and not result["findings"]
        assert next(item for item in result["excluded"] if item["number"] == 120)["exclusion"] == "wait_review_required"


TESTS.extend([test_discovered_milestone_wait_remains_available_and_cannot_admit_tooling,
              test_supported_wait_forms_and_person_work_do_not_become_ordering_findings,
              test_unmatched_local_milestone_keeps_its_recorded_wait,
              ])


def test_invalid_discovered_parent_wait_delegates_to_its_child() -> None:
    parent = global_issue("someone/business", 120, wait_reviewed=False, labels=["plan:waiting"],
                          milestone=milestone_data(7, "First", created_at="2026-01-01"),
                          body="## Current Status\nWaiting for: milestone Second.")
    child = global_issue("someone/business", 121)
    edges = {(parent["repo"], parent["number"]): relationships(sub_issues=[child])}
    with global_fixture([], [parent], edges, discovered=[parent, child]) as (module, result, _reads):
        module.discover_direction_work = lambda *_a, **_kw: ([parent, child], {"complete": True,
            "repositories": [{"repo": parent["repo"], "direction": DIRECTION}]})
        module.cmd_next(next_args())
        assert [item["number"] for item in result["candidates"]] == [121]
        assert next(item for item in result["excluded"] if item["number"] == 120)["exclusion"] == "delegated_to_open_sub_issues"
        assert not result["waiting"]


TESTS.append(test_invalid_discovered_parent_wait_delegates_to_its_child)


def test_multiline_wait_and_literal_milestone_wait_have_opposite_results() -> None:
    shared = load_module().github_direction_next
    for reason in ("Waiting for:\n- Alex to approve the copy.", "Waiting for: milestone review call with Alex on 2026-10-10.",
                   "Waiting for: once launch partners sign."):
        assert shared.milestone_wait_evidence({"repo": "someone/business", "number": 10, "url": "https://github.com/someone/business/issues/10"}, reason, ["Launch", "Dogfood week", "Thin fork decision"])["valid"]
    for reason in ("Waiting for: Dogfood week to finish.", "Waiting for: the Thin fork decision milestone."):
        assert not shared.milestone_wait_evidence({"repo": "someone/business", "number": 10, "url": "https://github.com/someone/business/issues/10"}, reason, ["Launch", "Dogfood week", "Thin fork decision"])["valid"]
    assert not shared.milestone_wait_evidence({"repo": "someone/business", "number": 10, "url": "https://github.com/someone/business/issues/10"}, "State: Waiting on Alex", ["Launch"])["valid"]


TESTS.extend([
              test_multiline_wait_and_literal_milestone_wait_have_opposite_results])


def test_milestone_scope_still_recognizes_other_listed_milestone_waits() -> None:
    roots = [track("someone/direction", 1, "First")]
    leaf = global_issue("someone/business", 120, wait_reviewed=False, labels=["plan:waiting"],
                        body="## Current Status\nWaiting for: Second to finish.")
    with global_fixture(roots, [leaf], {(roots[0]["repo"], 1): relationships(sub_issues=[leaf])}) as (module, result, _reads):
        with patch.object(module.github_milestone_core, "show_milestone", return_value={"milestone": roots[0]["milestone"]}):
            module.cmd_next(next_args(milestone="First"))
        assert [item["number"] for item in result["candidates"]] == [120]
        assert result["findings"][0]["reason"] == "wait_names_another_milestone"


TESTS.append(test_milestone_scope_still_recognizes_other_listed_milestone_waits)


def test_discovery_preserves_valid_waits_above_invalid_child_or_parent_waits() -> None:
    grand = global_issue("someone/business", 40, labels=["plan:waiting"],
                         body="## Current Status\nWaiting for: Alex to sign the contract.", waiting_for='Alex to sign the contract.')
    parent = global_issue("someone/business", 41, wait_reviewed=False, labels=["plan:waiting"],
                          body="## Current Status\nWaiting for: TBD.")
    for chain in ([grand], [parent, grand], [parent]):
        child = global_issue("someone/business", 50, wait_reviewed=False, labels=["plan:waiting"],
                             milestone=milestone_data(7, "First", created_at="2026-01-01"),
                             body="## Current Status\nWaiting for: TBD.")
        edges = {(ancestor["repo"], ancestor["number"]): relationships(sub_issues=[descendant])
                 for ancestor, descendant in zip(chain, [child, *chain[:-1]])}
        with global_fixture([], list(chain), edges, discovered=[child]) as (module, result, _reads):
            module.discover_direction_work = lambda *_a, **_kw: ([child], {"complete": True,
                "repositories": [{"repo": child["repo"], "direction": DIRECTION}]})
            module.cmd_next(next_args())
            assert not any(row["number"] == 41 for row in result["waiting"])
            if grand in chain:
                assert not result["candidates"]
                item = next(item for item in result["excluded"] if item["number"] == 50)
                assert item["exclusion"] == "parent_waiting" and item["waiting_on_parent"] == grand["html_url"]
            else:
                assert [item["number"] for item in result["candidates"]] == [50]
                assert any(finding["number"] == 41 for finding in result["findings"])


def test_blocked_wait_and_unknown_start_do_not_become_available_or_dated() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/business", 120, wait_reviewed=False, labels=["plan:blocked"], body="## Current Status\nState: Waiting.")
    edges = {(root["repo"], root["number"]): relationships(sub_issues=[leaf]) for root in roots}
    with global_fixture(roots, [leaf], edges) as (module, result, _reads):
        module.cmd_next(next_args())
        assert not result["candidates"]
        assert next(item for item in result["excluded"] if item["number"] == 120)["exclusion"] == "wait_review_required"
        assert result["dependency_context"]["complete"]
        assert result["candidate_coverage"]["complete"]
        for value in ("unknown", "TBD", "2026-99-99"):
            evidence = module.github_direction_next.milestone_wait_evidence({"repo": "someone/business", "number": 10, "url": "https://github.com/someone/business/issues/10"}, "Waiting for: Alex to test.\nWaiting since: " + value, ["First"])
            assert evidence["since"] is None
        for status in ("Waiting for:\n- Alex: approve the copy.", "Waiting for: none\nParked until: 2026-12-01 holiday freeze."):
            assert module.github_direction_next.milestone_wait_evidence({"repo": "someone/business", "number": 10, "url": "https://github.com/someone/business/issues/10"}, status, ["First"])["valid"]
        evidence = module.github_direction_next.milestone_wait_evidence({"repo": "someone/business", "number": 10, "url": "https://github.com/someone/business/issues/10"}, "Waiting for: Alex to test.\nWaiting since: 2026-08-20 (Alex confirmed)", ["First"])
        assert evidence["since"] == "2026-08-20"


TESTS.extend([test_discovery_preserves_valid_waits_above_invalid_child_or_parent_waits,
              test_blocked_wait_and_unknown_start_do_not_become_available_or_dated])


def test_excluded_unrelated_wait_failure_preserves_ordinary_coverage() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/business", 10, labels=["plan:waiting"], body="## Current Status\nWaiting for: Alex to test.", waiting_for='Alex to test.')
    unrelated = global_issue("someone/other", 50, wait_reviewed=False, labels=["plan:waiting"], body="## Current Status\nWaiting for: TBD.")
    edges = {(root["repo"], root["number"]): relationships(sub_issues=[leaf]) for root in roots}
    for own_milestone in (None, milestone_data(7, "First", created_at="2026-01-01")):
        discovery = {**unrelated, "milestone": own_milestone}
        with global_fixture(roots, [leaf], edges, discovered=[discovery]) as (module, result, _reads):
            def denied_parent(target: str, _number: int) -> None:
                if target == unrelated["repo"]:
                    raise module.PlanError("parent unavailable")
                return None
            module.read_next_parent = denied_parent
            module.cmd_next(next_args())
            assert result["candidate_coverage"]["complete"] == (own_milestone is None)
            assert result["discovery_context"]["complete"] == (own_milestone is None)
            assert result["all_listed_milestones_have_no_available_item"] is None
            assert next(item for item in result["excluded"] if item["number"] == 50)["exclusion"] == "unknown_ancestry"


def test_unmatched_parent_wait_does_not_emit_internal_exclusion() -> None:
    parent = global_issue("someone/other", 40, labels=["plan:waiting"], body="## Current Status\nWaiting for: Alex to test.", waiting_for='Alex to test.')
    child = global_issue("someone/other", 50)
    edges = {(parent["repo"], parent["number"]): relationships(sub_issues=[child])}
    with global_fixture([], [parent], edges, discovered=[child]) as (module, result, _reads):
        module.cmd_next(next_args())
        item = next(item for item in result["excluded"] if item["number"] == 50)
        assert item["exclusion"] == "parent_waiting"
        assert "_own_exclusion" not in item


TESTS.extend([test_excluded_unrelated_wait_failure_preserves_ordinary_coverage,
              test_unmatched_parent_wait_does_not_emit_internal_exclusion])


def test_waiting_pr_does_not_become_an_invalid_issue_wait() -> None:
    root = track("someone/direction", 1, "First")
    pull = {**global_issue("someone/business", 120, wait_reviewed=False, labels=["plan:waiting"]), "pull_request": {}}
    with global_fixture([root], [pull], {(root["repo"], 1): relationships(sub_issues=[pull])}) as (module, result, _reads):
        module.cmd_next(next_args())
        assert not result["findings"] and not result["candidates"]


TESTS.append(test_waiting_pr_does_not_become_an_invalid_issue_wait)


def active_track(repo: str, number: int, title: str, status: str) -> dict[str, Any]:
    return global_issue(
        repo, number, title=f"Track: {title}", labels=["plan", "plan:active"], body="## Current Status\n" + status,
        milestone=milestone_data(number, title, created_at="2026-01-01T00:00:00Z"),
    )


def test_milestone_agent_work_behind_bookkeeping_blocker_ranks_first() -> None:
    # 2026-10-07 shape (codex-skills#1378): milestone trackers say agent work is
    # available, but every path reads as a wait and unrelated work ranks first.
    first = active_track("someone/direction", 1, "First", "State: Active.\nNext action: another/platform#2682, then someone/site#99 and #91.\nWaiting for: Nothing for agent work.")
    second = active_track("someone/direction", 2, "Second", "State: Active.\nNext action: An admitted agent scopes the first work issue.\nWaiting for: None for planning/scoping; physical completion remains Chris’s work.")
    site = global_issue("someone/site", 91, labels=["plan", "plan:waiting"], body="## Current Status\nState: Waiting.\nWaiting for: Justin's release acceptance.", waiting_for="Justin's release acceptance.")
    release = global_issue("another/platform", 2682, labels=["plan", "plan:blocked"])
    notice = global_issue("another/platform", 3073, body="## Current Status\nState: Active.\nNext action: Supervisor delivery closeout; Justin's acceptance belongs to #2682. No new Director decision is open.")
    credentials = global_issue("someone/tenant", 124, wait_reviewed=False, body="## Current Status\nState: Active.\nWaiting for: None for bounded preparation; no live apply or grant is authorized by this audit.")
    unrelated = global_issue("someone/tooling", 10)
    edges = {
        ("someone/direction", 1): relationships(sub_issues=[site, release, credentials]),
        ("another/platform", 2682): relationships(blocked_by=[notice]),
        ("another/platform", 3073): relationships(blocking=[release]),
    }
    with global_fixture([first, second], [site, release, notice, credentials], edges, discovered=[unrelated],
                        clear_waits=[first, second, notice, credentials]) as (module, result, _reads):
        module.cmd_next(next_args())
    ranked = [(item["repo"], item["number"]) for item in result["candidates"]]
    assert ranked[:3] == [("another/platform", 3073), ("someone/tenant", 124), ("someone/direction", 2)]
    assert ranked[-1] == ("someone/tooling", 10)
    assert "Supervisor delivery closeout" in result["candidates"][0]["discussion"]["body"]
    assert [step["number"] for step in result["candidates"][0]["via"]] == [1, 2682, 3073]
    summary = {entry["milestone"]: entry for entry in result["milestone_summary"]}
    assert summary["First"]["state"] == "work_listed"
    assert [wait["number"] for wait in summary["First"]["waits"]] == [91]
    assert [ref["number"] for ref in summary["First"]["next_action_outside_graph"]] == [99]
    assert [item["number"] for item in summary["Second"]["work"]] == [2]
    assert result["candidate_coverage"]["milestone_ranking_complete"] is True


def test_milestone_summary_names_each_wait_when_no_milestone_work_is_listed() -> None:
    first = active_track("someone/direction", 1, "First", "State: Active.\nNext action: someone/site#91.")
    site = global_issue("someone/site", 91, labels=["plan", "plan:waiting"], body="## Current Status\nState: Waiting.\nWaiting for: Justin's release acceptance.", waiting_for="Justin's release acceptance.")
    unrelated = global_issue("someone/tooling", 10)
    with global_fixture([first, track("someone/direction", 2, "Second")], [site], {("someone/direction", 1): relationships(sub_issues=[site])}, discovered=[unrelated]) as (module, result, _reads):
        module.cmd_next(next_args())
    assert [(item["repo"], item["number"]) for item in result["candidates"]] == [("someone/tooling", 10)]
    summary = {entry["milestone"]: entry for entry in result["milestone_summary"]}
    assert summary["First"]["state"] == "waiting"
    assert summary["First"]["waits"][0]["waiting_for"] == "Justin's release acceptance."
    assert summary["First"]["next_action_outside_graph"] == []
    assert summary["Second"]["state"] == "no_linked_work"


def test_track_whose_next_action_belongs_to_a_person_is_not_agent_work() -> None:
    track_issue = active_track("someone/direction", 1, "First", "State: Active.\nNext action: Director selects the launch date.\nWaiting for: None; next actor is an agent.")
    with global_fixture([track_issue], [], {}) as (module, result, _reads):
        module.cmd_next(next_args())
    assert result["candidates"] == []
    assert result["excluded"][0]["exclusion"] == "tracking_without_open_work"


def test_named_next_actor_or_absence_keeps_a_stated_precondition() -> None:
    root = track("someone/direction", 1, "First")
    for reason in ("Supervisor to route the PR; Director approval is required first.",
                   "None; Chris must sign off before work starts.",
                   "Supervisor routing.\nNote: only after Justin approves",
                   "Supervisor to route the PR; Director authorization is a prerequisite.",
                   "None for release; Client acceptance is a prerequisite.",
                   "None; Chris’s sign-off is outstanding."):
        raw = global_issue("someone/product", 2, wait_reviewed=False, body="## Current Status\nState: Active.\nWaiting for: " + reason)
        with global_fixture([root], [raw], {(root["repo"], 1): relationships(sub_issues=[raw])}) as (module, result, _reads):
            module.cmd_next(next_args())
        assert result["candidates"] == [], reason
        assert next(item for item in result["excluded"] if item["number"] == 2)["exclusion"] == "wait_review_required"


TESTS.extend([test_milestone_agent_work_behind_bookkeeping_blocker_ranks_first,
              test_global_scan_diagnostic_counts_all_allowances_and_limit_only_caps_results,
              test_milestone_summary_names_each_wait_when_no_milestone_work_is_listed,
              test_named_next_actor_or_absence_keeps_a_stated_precondition,
              test_track_whose_next_action_belongs_to_a_person_is_not_agent_work])


def test_incident_leaves_beyond_ordinary_scan_keep_native_priority_and_review() -> None:
    marker = load_module().github_direction_next.LIVE_BREAKAGE_LABEL
    incident = global_issue("someone/live", 90, labels=["plan", "plan:active", marker])
    child = global_issue("someone/live", 91)
    blocker = global_issue("someone/repair", 92)
    ordinary = global_issue("someone/project", 1)
    for relation in ("sub_issues", "blocked_by"):
        leaf = child if relation == "sub_issues" else blocker
        edges = {("someone/live", 90): relationships(**{relation: [leaf]})}
        with global_fixture([], [leaf], edges, discovered=[ordinary, incident, leaf], clear_waits=[leaf]) as (module, result, _reads):
            module.load_direction = lambda *_: "# Direction\n## Milestones\n"
            module.cmd_next(next_args(scan_limit=1))
            candidate = next(item for item in result["candidates"] if item["number"] == leaf["number"])
            assert result["candidates"][0]["number"] == leaf["number"]
            assert [step["number"] for step in candidate["incident_via"]] == [90, leaf["number"]]
            assert candidate["incident_via"][-1]["relationship"] == ("sub_issue" if relation == "sub_issues" else "blocked_by")
            assert not result["available_candidates"]


def test_incident_paths_stop_whole_parent_waits_and_report_bounds_and_cycles() -> None:
    marker = load_module().github_direction_next.LIVE_BREAKAGE_LABEL
    root = global_issue("someone/live", 90, labels=["plan", "plan:active", marker])
    leaf = global_issue("someone/live", 91)
    later = global_issue("someone/live", 92)
    edges = {("someone/live", 90): relationships(sub_issues=[leaf]),
             ("someone/live", 91): relationships(blocked_by=[later]),
             ("someone/live", 92): relationships(blocked_by=[root])}
    with global_fixture([], [leaf, later], edges, discovered=[root]) as (module, result, _reads):
        result.update(next_failure(module, next_args(scan_limit=1), code="live_breakage_unread"))
        assert result["discovery_context"]["incident_context"]["truncated"]
        assert not result["candidate_coverage"]["complete"]
        result.update(next_failure(module, next_args(scan_limit=3), code="live_breakage_unread"))
        assert result["discovery_context"]["incident_context"]["cycles"]
        assert not result["candidate_coverage"]["complete"]
        assert not result["candidates"]
    for reason in ("State: Waiting.\nWaiting for: Client acceptance.", "State: Parked until Client acceptance."):
        root["body"] = "## Current Status\n" + reason
        root["_fixture_wait_reviewed"] = False
        with global_fixture([], [leaf], {("someone/live", 90): relationships(sub_issues=[leaf])}, discovered=[root]) as (module, result, reads):
            module.cmd_next(next_args(scan_limit=1))
            assert not result["candidates"]
            assert not any(item["number"] == 91 for item in result["candidates"])
            assert ("someone/live", 91) not in reads


TESTS.extend([test_incident_leaves_beyond_ordinary_scan_keep_native_priority_and_review,
              test_incident_paths_stop_whole_parent_waits_and_report_bounds_and_cycles])


def test_incident_leaf_in_direction_inventory_is_not_a_track_exclusion() -> None:
    root = global_issue("someone/live", 90, labels=["plan", "plan:active", "Live-Breakage"])
    leaf = global_issue("someone/direction", 91)
    ordinary = global_issue("someone/project", 1)
    edges = {("someone/live", 90): relationships(blocked_by=[leaf])}
    with global_fixture([leaf], [], edges, discovered=[ordinary, root, leaf]) as (module, result, _reads):
        module.load_direction = lambda *_: "# Direction\n## Milestones\n"
        module.cmd_next(next_args(scan_limit=1))
        assert result["candidates"][0]["number"] == 91
        assert [step["number"] for step in result["candidates"][0]["incident_via"]] == [90, 91]
        assert not any(item["number"] == 91 for item in result["excluded"])


TESTS.append(test_incident_leaf_in_direction_inventory_is_not_a_track_exclusion)


def test_identity_refused_wait_keeps_independent_candidates() -> None:
    root = track("someone/direction", 1, "First")
    ready = global_issue("someone/business", 2)
    pending = global_issue("someone/business", 10, labels=["plan:waiting"],
                           body="## Current Status\nWaiting for: SDK recovery under outside/tools#475",
                           waiting_for="SDK recovery under outside/tools#475")
    with global_fixture([root], [ready], {(root["repo"], 1): relationships(sub_issues=[ready])},
                        discovered=[pending]) as (module, result, _reads):
        base_api = module.api_json
        refused = module.github_api_core._identity_failure(
            module.github_identity.GitHubAppError("Client App is not installed on outside/tools"),
            operation="github.plan.next", is_write=False, actor="client-agent[bot]",
            expected_actor="client-agent[bot]", host=None, bucket="rest_core")
        calls = []

        def api(method: str, path: str, **kwargs: Any) -> Any:
            calls.append((method, path))
            if path.startswith("/repos/outside/tools/"):
                raise module.PlanError(refused.failure.message, failure=refused.failure,
                                       api_result=refused.as_dict())
            return base_api(method, path, **kwargs)

        with patch.object(module, "api_json", side_effect=api):
            module.cmd_next(next_args())
        assert 2 in [row["number"] for row in result["candidates"]]
        assert 10 not in [row["number"] for row in result["candidates"]]
        assert not result["stale_wait_report"]["complete"]
        assert any(row.get("url") == "https://github.com/outside/tools/issues/475"
                   for row in result["stale_wait_report"]["unavailable"])
        assert result["waiting"][0]["references"][0]["state"] == "unknown"
        assert all(method == "GET" for method, _path in calls)


TESTS.append(test_identity_refused_wait_keeps_independent_candidates)


def test_identity_refused_graph_node_keeps_other_milestone_candidates() -> None:
    root = track("someone/direction", 1, "First")
    ready = global_issue("someone/business", 2)
    unread = global_issue("outside/tools", 475)
    edges = {(root["repo"], 1): relationships(sub_issues=[ready, unread])}
    with global_fixture([root], [ready], edges) as (module, result, _reads):
        base_get = module.get_issue
        failure = module.github_api_core.FailureDetail(
            cause="identity_refused", message="Client App is not installed on outside/tools",
            retryable=False, fallback_eligible=False, disposition="stop")

        def get(ref: str, repo: str) -> Any:
            if repo == unread["repo"]:
                raise module.PlanError(failure.message, failure=failure)
            return base_get(ref, repo)

        with patch.object(module, "get_issue", side_effect=get):
            module.cmd_next(next_args())
        assert [row["number"] for row in result["candidates"]] == [2]
        assert not result["graph_context"]["complete"]
        assert any(row["repo"] == unread["repo"] and row["exclusion"] == "unknown_dependencies"
                   for row in result["excluded"])


def test_local_identity_refused_relationships_degrade_and_credentials_stop() -> None:
    for cause in ("identity_refused", "invalid_credentials", "actor_mismatch"):
        module = load_module()
        failure = module.github_api_core.FailureDetail(
            cause=cause, message="read refused", retryable=False, fallback_eligible=False, disposition="stop")
        error = module.PlanError(failure.message, failure=failure)

        def read(_repo: str, number: int, _issue: Any = None) -> Any:
            if number == 1:
                raise error
            return "automation-gh", relationships(), []

        with local_next_fixture(module, plans=[issue(1), issue(2)], read_relationships=read) as result:
            if cause == "identity_refused":
                module.cmd_next(next_args(repo="owner/repo"))
                assert [row["number"] for row in result["candidates"]] == [2]
                assert result["excluded"][0]["exclusion"] == "unknown_dependencies"
            else:
                try:
                    module.cmd_next(next_args(repo="owner/repo"))
                except module.PlanError as exc:
                    assert exc is error
                else:
                    raise AssertionError("credential/actor failures must still stop selection")


TESTS.extend([test_identity_refused_graph_node_keeps_other_milestone_candidates,
              test_local_identity_refused_relationships_degrade_and_credentials_stop])


def test_parallel_global_graph_preserves_selection_waits_and_coverage() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaves = [global_issue("someone/MyProject", number) for number in range(10, 18)]
    edges = {("someone/direction", 1): relationships(sub_issues=leaves)}
    with global_fixture(roots, leaves, edges, clear_waits=leaves) as (module, result, _reads):
        args = next_args()
        module.cmd_next(args)
        serial = {key: value for key, value in result.items() if key != "performance"}
        result.clear()
        args.read_workers = 3
        module.cmd_next(args)
        parallel = {key: value for key, value in result.items() if key != "performance"}
        assert serial == parallel
        assert module.NEXT_READS is None


def test_global_zero_comment_summary_avoids_only_proven_empty_discussion_read() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/project", 10)
    edges = {("someone/direction", 1): relationships(sub_issues=[leaf])}
    for count in (0, None, "0", False, -1, 1):
        leaf["comments"] = count
        with global_fixture(roots, [leaf], edges) as (module, result, _reads):
            collect = module.collect_paged_rest_items
            comment_reads = []
            def observed(path: str, **kwargs: Any) -> Any:
                if path.endswith(f"/{leaf['number']}/comments"):
                    comment_reads.append(path)
                return collect(path, **kwargs)
            module.collect_paged_rest_items = observed
            module.cmd_next(next_args())
            assert bool(comment_reads) is not (type(count) is int and count == 0)
            assert result["graph_context"]["complete"]


def test_global_reserves_final_evidence_time_without_claiming_a_complete_scan() -> None:
    import time
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/project", 10)
    optional = global_issue("someone/optional", 20)
    edges = {("someone/direction", 1): relationships(sub_issues=[leaf])}
    with global_fixture(roots, [leaf], edges, clear_waits=[leaf]) as (module, result, _reads):
        def slow_inventory(*_args, **_kwargs):
            module.NEXT_READS.deadline = time.time() + module.NEXT_READS.final_evidence_reserve / 2
            return [optional], {"complete": True, "repositories": [{"repo": optional["repo"], "direction": DIRECTION}]}
        module.discover_direction_work = slow_inventory
        def observed_ownership(items: list[dict[str, Any]], _args: Any) -> dict[str, Any]:
            for item in items:
                item["queue"] = {"state": "review", "claim_required": True}
            return {"repositories": {}}
        module.annotate_next_queue = observed_ownership
        args = next_args()
        args.time_budget = 50
        module.cmd_next(args)
        assert result["discovery_context"]["time_budget_limited"]
        assert result["discovery_context"]["unevaluated_count"] == 1
        assert not result["performance"]["deadline_exhausted"]
        assert result["candidate_coverage"]["milestone_ranking_complete"]
        assert result["recommended_next_action"] == "review_partial_coverage"
        assert result["graph_context"]["complete"]
        reported = next(row for row in result["candidates"] if row["number"] == leaf["number"])
        assert reported["queue"]["claim_required"]


def test_parallel_wait_reports_preserve_stale_frozen_and_unavailable_evidence() -> None:
    rows = [global_issue("someone/project", number, wait_reviewed=False, labels=["plan", "plan:waiting"],
        body="## Current Status\nState: Waiting.\nWaiting for: #99 to complete.\nBlocked by: None.") for number in range(1, 7)]
    for disposition in (False, True, None):
        module = load_module()
        module.load_config = lambda *_: module.DEFAULT_CONFIG
        def read(_method: str, path: str, **_kwargs: Any) -> Any:
            if path.rstrip("/") == "/repos/someone/project":
                if disposition is None:
                    raise module.PlanError("repository metadata unavailable")
                return "automation-gh", {"archived": disposition, "default_branch": "main"}
            if "dependencies/blocked_by" in path:
                return "automation-gh", []
            return "automation-gh", {"state": "closed", "state_reason": "completed"}
        module.api_json = read
        serial = module.next_wait_context(rows, scan_limit=len(rows), inventory_complete=True)
        with module.github_next_reads.NextReads(workers=3, seconds=0) as reads:
            module.NEXT_READS = reads
            try:
                parallel = module.next_wait_context(rows, scan_limit=len(rows), inventory_complete=True)
            finally:
                module.NEXT_READS = None
        assert serial == parallel


def test_global_read_wrapper_coalesces_gets_and_classifies_actual_deadlines() -> None:
    import time
    module = load_module()
    module.api_json = module.real_api_json
    transport = Mock(return_value=("automation-gh", {"state": "open"}))
    module.api_json_uncached = transport
    with module.github_next_reads.NextReads(workers=1, seconds=60) as reads:
        module.NEXT_READS = reads
        try:
            module.api_json("GET", "/observed", failed_step="first")
            module.api_json("GET", "/observed", failed_step="another_phase")
            assert transport.call_count == 1
            # A reset/backoff beyond the parent deadline refuses immediately,
            # before the wall clock actually expires.
            failure = module.github_api_core.FailureDetail(cause="deadline_exceeded", message="reset beyond deadline",
                retryable=False, fallback_eligible=False, disposition="stop")
            refused = module.PlanError(failure.message, failure=failure, api_result={
                "retry_exhausted_reason": "deadline_exceeded", "effective_deadline": reads.deadline,
                "retry_at": reads.deadline + 60})
            module.next_source_error(refused)
            module.next_source_error(refused)
            assert not reads.expired
            assert reads.receipt()["budget_stops"][failure.cause] == 1
            assert reads.receipt()["budget_context"][0]["retry_at"] > reads.deadline
            network_timeout = module.PlanError("TLS handshake timeout", failure=failure,
                api_result={"retry_exhausted_reason": "attempt_budget"})
            try:
                module.next_source_error(network_timeout)
            except module.PlanError as exc:
                assert exc is network_timeout
            else:
                raise AssertionError("network failure was mislabeled as a budget refusal")
            reads.deadline = time.time() - 1
            try:
                module.api_json("GET", "/unread")
            except module.PlanError as exc:
                assert isinstance(exc.__cause__, module.github_next_reads.ReadDeadline)
                module.next_source_error(exc)
            else:
                raise AssertionError("expired wrapper issued a new request")
            assert transport.call_count == 1
            malformed = module.PlanError("malformed response")
            assert module.next_source_error(malformed) == str(malformed)
            classified = module.ClassifiedPlanError("rate_limited", "quota refused")
            try:
                module.next_source_error(classified)
            except module.ClassifiedPlanError as exc:
                assert exc is classified
            else:
                raise AssertionError("classified quota failure was disguised as deadline")
        finally:
            module.NEXT_READS = None
    with module.github_next_reads.NextReads(workers=1, seconds=60) as reads:
        module.NEXT_READS = reads
        try:
            module.api_json("GET", "/observed")
            assert transport.call_count == 2
        finally:
            module.NEXT_READS = None


def test_one_worker_discovery_reads_each_discussion_once() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    optional = global_issue("someone/optional", 20)
    with global_fixture(roots, [], {}, discovered=[optional]) as (module, _result, _reads):
        collect = module.collect_paged_rest_items
        comment_reads = []
        def observed(path: str, **kwargs: Any) -> Any:
            if path.endswith(f"/{optional['number']}/comments"):
                comment_reads.append(path)
            return collect(path, **kwargs)
        module.collect_paged_rest_items = observed
        args = next_args()
        args.read_workers = 1
        module.cmd_next(args)
        assert len(comment_reads) == 1


def test_graph_read_ahead_is_bounded_beyond_a_deep_scan_frontier() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    chain = [global_issue("someone/project", number) for number in range(10, 16)]
    siblings = [global_issue("someone/project", number) for number in range(100, 120)]
    edges = {("someone/direction", 1): relationships(sub_issues=[chain[0], *siblings])}
    for parent, child in zip(chain, chain[1:]):
        edges[(parent["repo"], parent["number"])] = relationships(sub_issues=[child])
    with global_fixture(roots, [*chain, *siblings], edges, clear_waits=[*chain, *siblings]) as (module, result, _reads):
        collect = module.collect_paged_rest_items
        comment_reads = set()
        def observed(path: str, **kwargs: Any) -> Any:
            if path.endswith("/comments"):
                comment_reads.add(path)
            return collect(path, **kwargs)
        module.collect_paged_rest_items = observed
        args = next_args(scan_limit=3)
        args.read_workers = 2
        module.cmd_next(args)
        assert result["graph_context"]["truncated"]
        assert len(comment_reads) <= result["graph_context"]["evaluated"] + args.read_workers
        assert result["performance"]["read_ahead_unevaluated"] <= args.read_workers


def test_enrollment_helper_does_not_launch_after_inherited_deadline() -> None:
    module = load_module()
    with patch.object(module.github_api_core, "remaining_retry_timeout_seconds", return_value=0), patch.object(
            module.subprocess, "run") as run:
        result = module.real_read_next_train_enrollment("someone/project")
        assert result["status"] == "unknown"
        run.assert_not_called()


def test_required_proof_root_preserves_provider_and_credential_stop_policy() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/project", 10)
    edges = {("someone/direction", 1): relationships(sub_issues=[leaf])}
    for cause in ("network_provider_failure", "invalid_credentials"):
        with global_fixture(roots, [leaf], edges, clear_waits=[leaf]) as (module, _result, _reads):
            failure = module.github_api_core.FailureDetail(cause=cause, message="fixture rejected",
                retryable=False, fallback_eligible=False, disposition="stop")
            error = module.PlanError(failure.message, failure=failure)
            collect = module.collect_paged_rest_items
            def observed(path: str, **kwargs: Any) -> Any:
                if path.endswith("/2/comments"):
                    raise error
                return collect(path, **kwargs)
            module.collect_paged_rest_items = observed
            args = next_args(scan_limit=2)
            args.read_workers = 2
            try:
                module.cmd_next(args)
            except module.PlanError as exc:
                assert exc is error
            else:
                raise AssertionError("A required proof-root read failure was hidden")


def test_global_milestone_backend_receipt_contributes_actual_attempts() -> None:
    import time
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    with global_fixture(roots, [], {}) as (module, _result, _reads):
        backend = module.github_api_core.RetrySummary(attempts=3, elapsed_wait=0.5, retry_eligible=True,
            last_actor="automation-gh", last_bucket="rest_core", outcome_certainty="confirmed",
            reconciliation=None, recommended_next_action="none", effective_deadline=time.time()+60)
        module.github_milestone_core.list_milestones = lambda *_a, **_kw: {
            "milestones": [root["milestone"] for root in roots], **backend.as_dict()}
        module.cmd_next(next_args())
        assert module.CURRENT_RETRY_SUMMARY.attempts == backend.attempts
        assert module.CURRENT_RETRY_SUMMARY.elapsed_wait == backend.elapsed_wait


TESTS.extend([test_required_proof_root_preserves_provider_and_credential_stop_policy,
              test_global_milestone_backend_receipt_contributes_actual_attempts,
              test_graph_read_ahead_is_bounded_beyond_a_deep_scan_frontier,
              test_enrollment_helper_does_not_launch_after_inherited_deadline,
              test_global_read_wrapper_coalesces_gets_and_classifies_actual_deadlines,
              test_one_worker_discovery_reads_each_discussion_once,
              test_parallel_wait_reports_preserve_stale_frozen_and_unavailable_evidence,
              test_global_reserves_final_evidence_time_without_claiming_a_complete_scan,
              test_global_zero_comment_summary_avoids_only_proven_empty_discussion_read,
              test_parallel_global_graph_preserves_selection_waits_and_coverage])


def test_waiting_and_stale_nodes_preserve_independent_native_work() -> None:
    root = track("cbusillo/direction", 5, "First")
    parent = global_issue("cbusillo/odoo-tenant-cm", 120, wait_reviewed=False, labels=["plan", "plan:waiting"])
    child = global_issue(parent["repo"], 164)
    source = global_issue("cbusillo/launchplane", 3699, labels=["plan", "plan:blocked"])
    leaves = [global_issue(source["repo"], n) for n in (3708, 3709)]
    partial = global_issue(source["repo"], 3440, wait_reviewed=False, labels=["plan", "plan:waiting"])
    independent = global_issue(source["repo"], 3777)
    edges = {(root["repo"], 5): relationships(sub_issues=[parent, partial]),
             (parent["repo"], 120): relationships(sub_issues=[child]),
             (child["repo"], 164): relationships(blocked_by=[source]),
             (source["repo"], 3699): relationships(sub_issues=leaves),
             (partial["repo"], 3440): relationships(sub_issues=[independent])}
    nodes = [parent, child, source, *leaves, partial, independent]
    module = load_module()
    wait = module.github_plan_wait
    comments = {(parent["repo"], 120): [{"user": {"login": "cbusillo"}, "body": wait.marker(wait.reviewed(
                    evidence="Client confirmations remain on the parent; native prerequisites own source preparation.",
                    waiting_for="Client inventory answers", ends_when="Client records answers"))}],
                (partial["repo"], 3440): [{"user": {"login": "cbusillo"}, "body": wait.marker(wait.reviewed(
                    evidence="Native #3777 owns independently selectable HTTPS/WSS preparation.",
                    waiting_for="Q362", ends_when="Director answers Q362"))}]}
    with global_fixture([root], nodes, edges, repo=root["repo"], comments=comments, clear_waits=[child, source, *leaves, independent]) as (module, result, reads):
        module.load_direction = lambda *_: "# Direction\n## Milestones\n- `First`\n"
        module.cmd_next(next_args(repo=root["repo"]))
        assert {row["number"] for row in result["candidates"]} == {3708, 3709, 3777}
        assert result["dependency_context"]["complete"]
        assert result["candidate_coverage"]["milestone_ranking_complete"]
        source_view = next(row for row in result["excluded"] if row["number"] == 3699)
        assert source_view["exclusion"] == "delegated_to_open_sub_issues"
        assert source_view["plan_status"] == "active" and not source_view["derived"]["blocked_by"]
        assert not any(row["number"] == 3699 for row in result["findings"])
        assert len(reads) == len(set(reads)) == len(nodes)
        assert {row["number"] for row in result["waiting"]} == {120, 3440}


def test_waited_route_does_not_hide_an_independently_linked_leaf() -> None:
    root = track("someone/direction", 1, "First")
    held = global_issue("someone/product", 10, body="## Current Status\nState: Waiting.\nWaiting for: Customer sequencing.", waiting_for='Customer sequencing.')
    leaf = global_issue(held["repo"], 11)
    edges = {(root["repo"], 1): relationships(sub_issues=[held, leaf]),
             (held["repo"], 10): relationships(sub_issues=[leaf])}
    with global_fixture([root], [held, leaf], edges) as (module, result, reads):
        module.cmd_next(next_args())
        assert [row["number"] for row in result["candidates"]] == [11]
        assert [step["number"] for step in result["candidates"][0]["via"]] == [1, 11]
        assert reads.count((leaf["repo"], 11)) == 1
        assert not any(row["number"] == 11 for row in result["excluded"])


def test_newly_reached_unread_relationships_degrade_coverage_with_issue_evidence() -> None:
    module = load_module()
    shared = module.github_direction_next
    root = track("someone/direction", 1, "First")
    stale = global_issue("someone/product", 2, labels=["plan", "plan:blocked"])
    leaf = global_issue(stale["repo"], 3)
    raw = {(row["repo"], row["number"]): row for row in [root, stale, leaf]}
    edges = {(root["repo"], 1): relationships(sub_issues=[stale]),
             (stale["repo"], 2): relationships(sub_issues=[leaf])}
    def read(repo: str, number: int) -> dict[str, Any]:
        return shared.evaluate_direction_node(raw[(repo, number)], config=module.DEFAULT_CONFIG,
            focus=None, relationships={name: [module.compact_relationship_issue(row, name) for row in rows]
                                       for name, rows in edges.get((repo, number), relationships()).items()},
            truncated_relationships=["blocked_by"] if number == 3 else None)
    roots = [{**shared.compact_list_issue(root["repo"], root), "milestone": root["milestone"]}]
    result = shared.rank_direction_work(roots, milestone_titles=["First"], read_node=read, scan_limit=10)
    assert not result["dependency_context"]["complete"]
    failures = result["dependency_context"]["unavailable_relationships"]
    assert failures[0]["number"] == 3 and failures[0]["truncated_relationships"] == ["blocked_by"]
    bounded = shared.rank_direction_work(roots, milestone_titles=["First"], read_node=read, scan_limit=2)
    assert bounded["truncated"] and not bounded["dependency_context"]["complete"]


TESTS.extend([test_waiting_and_stale_nodes_preserve_independent_native_work,
              test_waited_route_does_not_hide_an_independently_linked_leaf,
              test_newly_reached_unread_relationships_degrade_coverage_with_issue_evidence])


def test_explicit_independent_child_keeps_other_waits_and_ancestry() -> None:
    shared = load_module().github_direction_next
    child = {"repo": "other/source", "number": 3777, "url": "https://github.com/other/source/issues/3777"}
    parent = {"repo": "someone/product", "number": 3440, "url": "https://github.com/someone/product/issues/3440",
              "exclusion": "waiting", "discussion": {"complete": True}}
    for evidence in [f"{child['url']} is independently selectable preparation.",
                     f"Native [#3777]({child['url']}) owns independently selectable preparation.",
                     f"{child['url']} can proceed independently."]:
        parent["structured_wait"] = {"evidence": evidence}
        assert not shared.parent_holds_child(parent, child)
        assert shared.parent_holds_child(parent, {**child, "number": 3778})
        held = shared.include_parent_context({**child, "exclusion": "waiting", "discussion": {"complete": True}},
                                             [parent], complete=True, tracking_roots=[])
        assert held["exclusion"] == "waiting"  # The child's own wait survives.
    for evidence in [f"{child['url']} is not independently selectable.",
                     f"{child['url']} is independently selectable only after Q362."]:
        parent["structured_wait"] = {"evidence": evidence}
        assert shared.parent_holds_child(parent, child)


TESTS.append(test_explicit_independent_child_keeps_other_waits_and_ancestry)


def test_explicit_whole_plan_wait_survives_prerequisites_of_held_children() -> None:
    root = track("someone/direction", 1, "First")
    parent = global_issue("someone/product", 10, wait_reviewed=False, labels=["plan", "plan:waiting"])
    child = global_issue(parent["repo"], 11)
    prep = global_issue(parent["repo"], 12)
    ready = global_issue(parent["repo"], 20)
    edges = {(root["repo"], 1): relationships(sub_issues=[parent, ready]),
             (parent["repo"], 10): relationships(sub_issues=[child]),
             (child["repo"], 11): relationships(blocked_by=[prep])}
    module = load_module()
    marker = module.github_plan_wait.marker(module.github_plan_wait.reviewed(
        evidence="Client explicitly holds the whole plan.", waiting_for="Client confirms scope before any preparation",
        ends_when="Client releases all work on the plan"))
    comments = {(parent["repo"], 10): [{"user": {"login": "someone"}, "body": marker}]}
    with global_fixture([root], [parent, child, prep, ready], edges, comments=comments) as (module, result, reads):
        module.cmd_next(next_args())
        assert [row["number"] for row in result["candidates"]] == [20]
        assert any(row["number"] == 12 and row["exclusion"] == "parent_waiting" for row in result["excluded"])
        assert len(reads) == len(set(reads)) == 4
        module.cmd_next(next_args(scan_limit=3))
        assert [row["number"] for row in result["candidates"]] == [20]
        assert result["truncated"]


TESTS.append(test_explicit_whole_plan_wait_survives_prerequisites_of_held_children)


def test_direct_blockers_preserve_legacy_and_explicit_whole_plan_holds() -> None:
    root = track("someone/direction", 1, "First")
    parent = global_issue("someone/product", 3, labels=["plan", "plan:waiting"],
        body="## Current Status\nState: Waiting.\nWaiting for: Client sign-off before any work on this plan.", waiting_for='Client sign-off before any work on this plan.')
    blocker = global_issue(parent["repo"], 4)
    edges = {(root["repo"], 1): relationships(sub_issues=[parent]),
             (parent["repo"], 3): relationships(blocked_by=[blocker])}
    wait = load_module().github_plan_wait
    for scope in ("legacy", "whole", "partial"):
        comments = {} if scope == "legacy" else {(parent["repo"], 3): [{"user": {"login": "someone"}, "body": wait.marker(wait.reviewed(
            evidence="Reviewed current wait scope.",
            waiting_for="Client sign-off before any preparation" if scope == "whole" else "Client signs the final result",
            ends_when="Client records sign-off"))}]}
        with global_fixture([root], [parent, blocker], edges, comments=comments) as (module, result, reads):
            module.cmd_next(next_args())
            assert {row["number"] for row in result["candidates"]} == ({4} if scope == "partial" else set())
            assert reads.count((blocker["repo"], 4)) == 1


def test_held_subtree_blocker_keeps_earliest_milestone_and_prefetch_order() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    parent = global_issue("someone/product", 10, wait_reviewed=False, labels=["plan", "plan:waiting"])
    child = global_issue(parent["repo"], 11)
    blocker = global_issue(parent["repo"], 12)
    edges = {(roots[0]["repo"], 1): relationships(sub_issues=[parent]),
             (roots[1]["repo"], 2): relationships(sub_issues=[blocker]),
             (parent["repo"], 10): relationships(sub_issues=[child]),
             (child["repo"], 11): relationships(blocked_by=[blocker])}
    wait = load_module().github_plan_wait
    comments = {(parent["repo"], 10): [{"user": {"login": "someone"}, "body": wait.marker(wait.reviewed(
        evidence="Final result waits; independent prerequisites remain their own work.",
        waiting_for="Client final review", ends_when="Client records acceptance"))}]}
    with global_fixture(roots, [parent, child, blocker], edges, comments=comments) as (module, result, reads):
        args = next_args()
        args.read_workers = 2
        module.cmd_next(args)
        selected = next(row for row in result["candidates"] if row["number"] == 12)
        assert selected["milestone"]["title"] == "First"
        assert [step["number"] for step in selected["via"]] == [1, 10, 11, 12]
        assert reads.count((blocker["repo"], 12)) == 1


TESTS.extend([test_direct_blockers_preserve_legacy_and_explicit_whole_plan_holds,
              test_held_subtree_blocker_keeps_earliest_milestone_and_prefetch_order])


def test_tracking_wait_summary_does_not_hold_its_direct_blocker() -> None:
    root = track("someone/direction", 1, "First")
    leaf = global_issue("someone/product", 2)
    with global_fixture([root], [leaf], {(root["repo"], 1): relationships(blocked_by=[leaf])}) as (module, result, reads):
        module.cmd_next(next_args())
        assert [row["number"] for row in result["candidates"]] == [2]
        assert reads == [(leaf["repo"], 2)]


TESTS.append(test_tracking_wait_summary_does_not_hold_its_direct_blocker)


def test_local_next_uses_current_wait_and_native_graph_over_old_labels() -> None:
    module = load_module()
    plans = [issue(21, labels=["plan", "plan:blocked", "plan:waiting"]), issue(22)]
    person_wait = module.github_plan_wait.reviewed(evidence="Director's latest request still awaits a device result",
        waiting_for="Director device test", ends_when="Director records the hands-on result")
    with local_next_fixture(module, plans, waits={22: person_wait}, read_relationships=lambda *_: (
            "automation-gh", relationships(blocked_by=[related(20, state="closed")]), [])) as result:
        module.cmd_next(type("Args", (), {"repo": "owner/repo", "milestone": None, "limit": 5, "scan_limit": 5})())
    assert [item["number"] for item in result["candidates"]] == [21], result
    assert result["candidates"][0]["plan_status"] == "active", result
    assert result["candidates"][0]["blocked_by"] == [], result
    waiting = next(item for item in result["excluded"] if item["number"] == 22)
    assert waiting["exclusion"] == "waiting", waiting
    assert waiting["derived"]["waits"] == person_wait["waits"], waiting


TESTS.extend([test_local_next_uses_current_wait_and_native_graph_over_old_labels,
              test_unstatused_scope_is_review_work_without_becoming_a_person_wait])


def test_local_next_comment_limit_keeps_unread_discussion_under_review() -> None:
    module = load_module()
    raw = issue(21)
    clear = module.github_plan_wait.reviewed(evidence="Earlier review found no holds")
    held = module.github_plan_wait.reviewed(evidence="Client subsequently requested a test",
        waiting_for="Client acceptance", ends_when="Client accepts")
    comments = [{"user": {"login": "owner"}, "body": module.github_plan_wait.marker(record)}
                for record in (clear, held)]
    with local_next_fixture(module, [raw], read_relationships=lambda *_: (
            "automation-gh", relationships(), [])) as result:
        collector = module.collect_paged_rest_items
        def bounded_comments(path: str, **kwargs: Any) -> Any:
            if path.endswith("/comments"):
                return "automation-gh", comments[:kwargs.get("limit", len(comments))]
            return collector(path, **kwargs)
        with patch.object(module, "collect_paged_rest_items", bounded_comments):
            args = next_args(repo="owner/repo")
            args.comment_limit = 1
            module.cmd_next(args)
            assert not result["candidates"], result
            observed = result["excluded"][0]
            assert observed["exclusion"] == "wait_review_required", observed
            assert observed["wait_evidence_complete"] is False, observed
            assert result["dependency_context"]["complete"], result
            args.comment_limit = len(comments)
            module.cmd_next(args)
            observed = result["excluded"][0]
            assert observed["exclusion"] == "waiting", observed
            assert observed["derived"]["waits"] == held["waits"], observed


def test_local_next_comment_read_failure_is_wait_review_not_unknown_dependencies() -> None:
    module = load_module()
    with local_next_fixture(module, [issue(21)], read_relationships=lambda *_: (
            "automation-gh", relationships(), [])) as result:
        collector = module.collect_paged_rest_items
        failure = module.PlanError("Fixture discussion transport unavailable")
        def unavailable_comments(path: str, **kwargs: Any) -> Any:
            if path.endswith("/comments"):
                raise failure
            return collector(path, **kwargs)
        with patch.object(module, "collect_paged_rest_items", unavailable_comments):
            module.cmd_next(next_args(repo="owner/repo"))
    assert not result["candidates"], result
    observed = result["excluded"][0]
    assert observed["exclusion"] == "wait_review_required", observed
    assert observed["wait_evidence_complete"] is False, observed
    assert observed["wait_error"], observed
    assert result["dependency_context"]["complete"], result
    assert not observed["blocked_by"], observed


TESTS.extend([test_local_next_comment_limit_keeps_unread_discussion_under_review,
              test_local_next_comment_read_failure_is_wait_review_not_unknown_dependencies])


def test_global_next_reads_milestone_proofs_before_a_descendant_uses_the_scan_budget() -> None:
    root = track("someone/direction", 3, "First")
    last = track("someone/direction", 102, "Second")
    proof = "The site serves real visitors after backup and tested rollback."
    root["body"] = f"## Objective\n{proof}\n\n## Current Status\nState: The accepted release is complete.\n"
    last["body"] = "## Finish Line\nThe original issues close and the accepted release reaches production.\n"
    last["body"] += "\n## Current Status\nState: Active.\n"
    last["updated_at"] = "2027-01-02T00:00:00Z"
    later = global_issue("someone/product", 560)
    comments: dict[tuple[str, int], list[dict[str, Any]]] = {("someone/direction", 102): [{"id": 1, "user": {"login": "someone"},
        "body": "## Current Status\nState: The accepted release is complete.", "created_at": "2027-01-01T00:00:00Z"}]}
    with global_fixture([root, last], [later], {("someone/direction", 3): relationships(blocked_by=[later])},
                        clear_waits=[root, last], comments=comments) as (module, result, _reads):
        module.cmd_next(next_args(scan_limit=1))
        reviews = result["milestone_closure_reviews"]
        assert len(reviews) == 2, result
        assert reviews[0]["track_proof"] == proof, reviews
        assert reviews[1]["recorded_status"] == comments[("someone/direction", 102)][0]["body"].split("\n", 1)[1], reviews
        assert result["graph_context"]["truncated"], result


TESTS.append(test_global_next_reads_milestone_proofs_before_a_descendant_uses_the_scan_budget)


def test_source_discovery_precedes_optional_proof_warmup() -> None:
    import time
    roots = [track('someone/direction', 1, 'First')]
    noise = global_issue('someone/project', 20)
    source = global_issue('someone/project', 21, milestone=roots[0]['milestone'],
                      body='## Current Status\nState: Active.\nNext action: Implement the missing resolver.')
    with global_fixture(roots, [], {}, discovered=[noise, source]) as (module, result, _reads):
        module.discover_direction_work = lambda *_args, **_kwargs: ([noise, source], {
            'complete': True, 'repositories': [{'repo': source['repo'], 'direction': DIRECTION}]})
        original = module.next_wait_context
        def expensive_proof(*proof_args: Any, **proof_options: Any) -> Any:
            module.NEXT_READS.deadline = time.time() + module.NEXT_READS.final_evidence_reserve / 2
            return original(*proof_args, **proof_options)
        module.next_wait_context = expensive_proof
        def ownership(items: list[dict[str, Any]], _args: Any) -> dict[str, Any]:
            for row in items:
                row['queue'] = {'state': 'available' if row['number'] == source['number'] else 'review',
                                'claim_required': True, 'observed_ownership_complete': True}
            return {'repositories': {}}
        module.annotate_next_queue = ownership
        args = next_args()
        args.time_budget = 50
        module.cmd_next(args)
        assert [row['number'] for row in result['available_candidates']] == [source['number']], result


TESTS.append(test_source_discovery_precedes_optional_proof_warmup)

def main() -> None:
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
