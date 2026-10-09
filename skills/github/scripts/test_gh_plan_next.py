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
    # Every test starts offline; fixtures opt into the API evidence they own.
    module.api_json = Mock(side_effect=module.PlanError("offline fixture record unavailable"))
    module.real_next_dependabot_work = module.next_dependabot_work
    module.next_dependabot_work = lambda *_args, **_kwargs: {"dependabot_candidates": [], "dependabot_candidate_count": 0}
    module.real_read_next_train_enrollment = module.read_next_train_enrollment
    module.read_next_train_enrollment = lambda _repo: {"source": "launchplane", "status": "not_enrolled"}
    module.real_discover_direction_work = module.discover_direction_work
    module.real_read_next_inbound_blockers = module.read_next_inbound_blockers
    module.read_next_inbound_blockers = lambda *_a, **_kw: ("automation-gh", [], {"complete": True})
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
) -> Iterator[dict[str, Any]]:
    """Capture a local next command with explicit evidence and scoped patches."""
    captured: dict[str, Any] = {}
    with patch.multiple(
        module,
        collect_paged_rest_items=(
            collect_plans if collect_plans is not None
            else lambda *_args, **_kwargs: ("automation-gh", plans)
        ),
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
    relationship_requests: list[str] | None = None,
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
        assert method == "GET", "global next must remain read-only"
        parts = path.split("?", 1)[0].strip("/").split("/")
        target = "/".join(parts[1:3])
        if len(parts) == 3:
            return "automation-gh", {"default_branch": "main"}
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
    assert result["recorded_waits"][0]["repo"] == "someone/site"
    assert result["recorded_waits"][0]["number"] == 91
    assert "Justin" in result["recorded_waits"][0]["waiting_for"]
    assert result["recorded_waits"][0]["reported_by"].endswith("/issues/91")
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
        refusal = result["tooling_capacity_context"]
        assert refusal["admitted"] is False
        assert refusal["reason"] == "incomplete_milestone_coverage"
        assert "Track" in refusal["detail"]
        for title in result["dependency_context"]["missing_tracking_milestones"]:
            assert title in refusal["detail"]


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
    assert result["recorded_waits"][0]["repo"] == "someone/product"
    assert result["recorded_waits"][0]["number"] == 3
    assert result["recorded_waits"][0]["references"][0]["url"].endswith("/pull/42")
    assert "Old" not in result["recorded_waits"][0]["waiting_for"]
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
        if body:
            assert result["candidates"] == []
            assert reads == [("someone/product", 3)]
            assert any(node.get("number") == 3 and node["exclusion"] == "waiting" for node in result["excluded"])
        else:
            assert [item["number"] for item in result["candidates"]] == [4]
            assert reads == [("someone/product", 3), ("someone/product", 4)]
            assert any(finding["number"] == 3 for finding in result["findings"])


def test_global_leaf_waiting_on_referenced_review_is_not_actionable() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/site", 91, body="## Current Status\nState: Active.\nWaiting for: Justin's owner review of #92 remains pending.")
    edges = {("someone/direction", 1): relationships(sub_issues=[leaf])}
    with global_fixture(roots, [leaf], edges) as (module, result, _reads):
        module.cmd_next(next_args())
    assert result["candidates"] == []
    assert result["recorded_waits"][0]["number"] == 91


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
        assert ranked["recorded_waits"][0]["number"] == 91


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
        with patch.multiple(module, next_selection_context=lambda _args: context):
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
        with patch.multiple(module, next_selection_context=lambda _args: context):
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
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
        assert result["candidates"] == result["available_candidates"] == []
        assert result["underway"][0]["number"] == 979
        assert next(item for item in result["excluded"] if item["number"] == 769)["exclusion"] == "reviewed_wait"
        assert result["discovery_context"]["complete"] is True


def test_portfolio_partial_ownership_and_stale_or_truncated_discussions_are_not_available() -> None:
    own = global_issue("someone/product", 42)
    comments = {(own["repo"], 42): [{"id": 1, "body": "Earlier comment"}, {"id": 2, "body": "Later owner correction"}]}
    with global_fixture([], [], {}, discovered=[own], comments=comments) as (module, result, _reads):
        module.cmd_next(next_args())
        candidate = result["candidates"][0]
        for extra in ({"ownership_complete": False}, {"discussion_digest": "older snapshot"}):
            context = {"issues": {"someone/product#42": reviewed(candidate, **extra)}}
            with patch.multiple(module, next_selection_context=lambda _args: context):
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
            "someone/project#1": reviewed(by_number[1]),
            "someone/project#2": reviewed(by_number[2], category="repeated_stop_tooling"),
            "someone/project#3": reviewed(by_number[3], category="live_incident"),
            "someone/project#4": reviewed(by_number[4], category="repeated_stop_tooling", stop_occurrences=["https://github.com/someone/project/issues/20", "https://github.com/someone/project/issues/21"]),
        }}
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
        assert [item["number"] for item in result["available_candidates"]] == [3, 4, 1]
        assert next(item for item in result["candidates"] if item["number"] == 2)["review_required"] == "two_linked_stop_occurrences"


def test_portfolio_capacity_admission_ranks_stops_then_age_and_keeps_own_projects() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    waits = [global_issue("someone/business", n, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Customer testing.") for n in (10, 11)]
    tools = [global_issue("someone/tools", n, created_at=date) for n, date in ((20, "2026-02-01"), (21, "2026-01-01"), (22, "2026-03-01"), (23, "2026-03-01"))]
    own = global_issue("someone/product", 42)
    edges = {(root["repo"], root["number"]): relationships(sub_issues=[wait]) for root, wait in zip(roots, waits)}
    with global_fixture(roots, waits, edges, discovered=[*tools, own]) as (module, result, _reads):
        module.cmd_next(next_args())
        items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
        context = {"issues": {
            **{f"someone/business#{n}": reviewed(items[n], "waiting", waiting_on="person") for n in (10, 11)},
            **{f"someone/tools#{n}": reviewed(items[n], category="repeated_stop_tooling", stop_occurrences=[f"https://example.com/stop/{i}" for i in range(count)]) for n, count in ((20, 0), (21, 0), (22, 1), (23, 3))},
            "someone/product#42": reviewed(items[42]),
        }}
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is True
            assert [item["number"] for item in result["available_candidates"]] == [23, 42, 22, 21, 20]
            tools_by_number = {item["number"]: item for item in result["available_candidates"] if item["repo"] == "someone/tools"}
            assert tools_by_number[23]["tooling_admission_rule"] == "repeated_stops"
            assert tools_by_number[22]["tooling_admission_rule"] == "all_milestones_waiting_on_people"
            assert tools_by_number[22]["recorded_stop_count"] == 1
            args = next_args()
            args.limit = 2
            module.cmd_next(args)
            assert [item["number"] for item in result["available_candidates"]] == [23, 42]


def test_portfolio_capacity_needs_current_person_waits_and_complete_graph() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/business", 10, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Alex to test.\nWaiting since: 2026-08-20")
    tool = global_issue("someone/tools", 20)
    edges = {(roots[0]["repo"], 1): relationships(sub_issues=[leaf])}
    with global_fixture(roots, [leaf], edges, discovered=[tool]) as (module, result, _reads):
        module.cmd_next(next_args())
        items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
        base = {"issues": {
            "someone/business#10": reviewed(items[10], "waiting", waiting_on="person"),
            "someone/tools#20": reviewed(items[20], category="repeated_stop_tooling"),
        }}
        # An empty listed Track cannot establish a named person wait.
        with patch.multiple(module, next_selection_context=lambda _args: base):
            module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is False
            assert result["tooling_capacity_context"]["reason"] == "milestone_names_no_person"
        module.load_direction = lambda *_: "# Direction\n\n## Milestones\n\n- `First`\n"
        for extra in ({"state": "available"}, {"state": "underway"}, {"waiting_on": "event"}, {"waiting_on": None}, {"discussion_digest": "stale"}, {"ownership_complete": False}):
            context = {"issues": {**base["issues"], "someone/business#10": {**base["issues"]["someone/business#10"], **extra}}}
            with patch.multiple(module, next_selection_context=lambda _args: context):
                module.cmd_next(next_args())
                assert result["tooling_capacity_context"]["admitted"] is False
                assert not any(item["number"] == 20 for item in result["available_candidates"])
        with patch.multiple(module, next_selection_context=lambda _args: base):
            module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is True
            with patch.multiple(module.github_milestone_core, show_milestone=lambda *_a, **_kw: {"milestone": roots[0]["milestone"]}):
                module.cmd_next(next_args(milestone="First"))
                assert result["tooling_capacity_context"]["admitted"] is False
            module.discover_direction_work = lambda *_a, **_kw: ([tool], {"complete": False, "repositories": []})
            module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is True
            assert [item["number"] for item in result["available_candidates"]] == [20]
            assert result["candidate_coverage"]["complete"] is False


def test_portfolio_capacity_truncated_discovery_admits_tooling_but_not_incomplete_graph() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    wait = global_issue("someone/business", 10, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Alex to test.\nWaiting since: 2026-08-20")
    tools = [global_issue("someone/tools", number) for number in range(20, 25)]
    edges = {(root["repo"], root["number"]): relationships(sub_issues=[wait]) for root in roots}
    with global_fixture(roots, [wait], edges, discovered=tools) as (module, result, _reads):
        module.cmd_next(next_args())
        items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
        context = {"issues": {
            "someone/business#10": reviewed(items[10], "waiting", waiting_on="person"),
            "someone/tools#20": reviewed(items[20], category="repeated_stop_tooling"),
        }}
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args(scan_limit=3))
            assert result["graph_context"]["complete"] is True
            assert result["tooling_capacity_context"]["admitted"] is True
            admitted = result["available_candidates"]
            assert [item["number"] for item in admitted] == [20]
            assert admitted[0]["tooling_admission_rule"] == "all_milestones_waiting_on_people"
            assert result["discovery_context"]["capacity_complete"] is False
            assert result["discovery_context"]["unevaluated_count"] == 2
            assert result["candidate_coverage"]["complete"] is False
            assert result["candidate_coverage"]["warning"]
            assert result["truncated"] is True
            module.cmd_next(next_args(scan_limit=1))
            assert result["graph_context"]["complete"] is False
            assert result["tooling_capacity_context"]["admitted"] is False
            assert result["available_candidates"] == []


def test_portfolio_capacity_reports_reviews_before_unread_excluded_ancestry() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    wait = global_issue("someone/business", 10, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Customer testing.")
    tool = global_issue("someone/tools", 20)
    unrelated = global_issue("someone/product", 439, milestone=milestone_data(14, "Encrypted Disc Relay", created_at="2026-08-31T00:00:00Z"))
    blocker = global_issue("someone/product", 711)
    edges = {
        **{(root["repo"], root["number"]): relationships(sub_issues=[wait]) for root in roots},
        (unrelated["repo"], 439): relationships(blocked_by=[blocker]),
    }
    with global_fixture(roots, [wait], edges, discovered=[unrelated, tool]) as (module, result, _reads):
        module.cmd_next(next_args())
        capacity = result["tooling_capacity_context"]
        assert capacity["issue"] == "someone/business#10"
        assert capacity["required"] == "current_complete_person_wait_review"
        items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
        assert "ancestry_complete" not in items[439]["discussion"]
        context = {"issues": {
            "someone/business#10": reviewed(items[10], "waiting", waiting_on="person"),
            "someone/tools#20": reviewed(items[20], category="repeated_stop_tooling"),
        }}
        read_parent = module.read_next_parent
        def unavailable_parent(repo: str, number: int) -> Any:
            if repo == unrelated["repo"] and number == unrelated["number"]:
                raise module.PlanError("Parent unavailable")
            return read_parent(repo, number)
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is True
            assert [item["number"] for item in result["available_candidates"]] == [20]
            with patch.multiple(module, read_next_parent=unavailable_parent):
                module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is False
            assert result["tooling_capacity_context"]["reason"] == "unknown_milestone_context"
            assert result["tooling_capacity_context"]["issue"] == "someone/product#439"


def test_portfolio_capacity_unscanned_known_milestone_prevents_admission() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    wait = global_issue("someone/business", 10, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Alex to test.\nWaiting since: 2026-08-20")
    tools = [global_issue("someone/tools", number) for number in range(20, 23)]
    unscanned = global_issue("someone/business", 30, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Alex to test.", milestone=milestone_data(7, "Second", created_at="2026-01-01"))
    edges = {(root["repo"], root["number"]): relationships(sub_issues=[wait]) for root in roots}
    with global_fixture(roots, [wait], edges, discovered=[*tools, unscanned]) as (module, result, _reads):
        module.discover_direction_work = lambda *_a, **_kw: ([*tools, unscanned], {
            "complete": True, "repositories": [
                {"repo": "someone/tools", "direction": None},
                {"repo": "someone/business", "direction": DIRECTION},
            ],
        })
        module.cmd_next(next_args())
        items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
        context = {"issues": {
            "someone/business#10": reviewed(items[10], "waiting", waiting_on="person"),
            "someone/tools#20": reviewed(items[20], category="repeated_stop_tooling"),
        }}
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args(scan_limit=3))
            assert result["graph_context"]["complete"] is True
            assert result["tooling_capacity_context"]["admitted"] is False
            assert result["available_candidates"] == []
            assert result["discovery_context"]["unevaluated_milestone_issues"][0]["number"] == 30
            # An unparsed repository direction cannot make the known title disappear.
            original_discovery = module.discover_direction_work
            def unparsed_discovery(*args: Any, **kwargs: Any) -> Any:
                inventory, coverage = original_discovery(*args, **kwargs)
                coverage["repositories"][1]["direction"] = "# Direction\nNo parsed waypoints."
                return inventory, coverage
            with patch.multiple(module, discover_direction_work=unparsed_discovery):
                module.cmd_next(next_args(scan_limit=3))
                assert result["tooling_capacity_context"]["admitted"] is False
                assert result["discovery_context"]["unevaluated_milestone_issues"][0]["number"] == 30
            # Once inspected, its current person-wait review settles this frontier.
            module.cmd_next(next_args())
            current = next(item for item in result["excluded"] if item["number"] == 30)
            context["issues"]["someone/business#30"] = reviewed(current, "waiting", waiting_on="person")
            module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is True
            assert [item["number"] for item in result["available_candidates"]] == [20]


def test_portfolio_capacity_partial_milestone_source_differs_from_unrelated_source() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    wait = global_issue("someone/business", 10, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Alex to test.\nWaiting since: 2026-08-20")
    tool = global_issue("someone/tools", 20)
    edges = {(root["repo"], root["number"]): relationships(sub_issues=[wait]) for root in roots}
    with global_fixture(roots, [wait], edges, discovered=[tool]) as (module, result, _reads):
        module.cmd_next(next_args())
        items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
        context = {"issues": {
            "someone/business#10": reviewed(items[10], "waiting", waiting_on="person"),
            "someone/tools#20": reviewed(items[20], category="repeated_stop_tooling"),
        }}
        for failure in ({"truncated": True}, {"error": "Issue inventory unavailable"}):
            for source_direction, admitted in ((DIRECTION, False), ("# Direction\n\n## Milestones\n", True)):
                coverage = {"complete": False, "capacity_complete": False, "repositories": [
                    {"repo": "someone/extra", "direction": source_direction, **failure},
                    {"repo": "someone/tools", "direction": None},
                ]}
                with patch.multiple(module, next_selection_context=lambda _args: context,
                                    discover_direction_work=lambda *_a, **_kw: ([tool], coverage)):
                    module.cmd_next(next_args())
                assert result["tooling_capacity_context"]["admitted"] is admitted
                assert bool(result["discovery_context"]["incomplete_milestone_repositories"]) is not admitted
                assert result["candidate_coverage"]["complete"] is False
                assert result["candidate_coverage"]["warning"]


def test_portfolio_capacity_waiting_discovery_milestone_blocks_admission() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    wait = global_issue("someone/business", 10, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Alex to test.\nWaiting since: 2026-08-20")
    tool = global_issue("someone/tools", 20)
    unlinked = global_issue("someone/other", 30, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Alex to test.", milestone=milestone_data(7, "First", created_at="2026-01-01"))
    edges = {(roots[0]["repo"], 1): relationships(sub_issues=[wait]), (roots[1]["repo"], 2): relationships(sub_issues=[wait])}
    with global_fixture(roots, [wait], edges, discovered=[tool, unlinked]) as (module, result, _reads):
        module.discover_direction_work = lambda *_a, **_kw: ([tool, unlinked], {"complete": True, "repositories": [{"repo": "someone/other", "direction": DIRECTION}, {"repo": "someone/tools", "direction": None}]})
        module.cmd_next(next_args())
        items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
        context = {"issues": {
            "someone/business#10": reviewed(items[10], "waiting", waiting_on="person"),
            "someone/tools#20": reviewed(items[20], category="repeated_stop_tooling"),
        }}
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is False
            assert not result["available_candidates"]
        current = next(item for item in result["excluded"] if item["number"] == 30)
        context["issues"]["someone/other#30"] = reviewed(current, "waiting", waiting_on="person")
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is True
            assert [item["number"] for item in result["available_candidates"]] == [20]
        context["repository_holds"] = {"someone/business": {"reason": "Owner hold", "evidence": ["owner instruction"]}}
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is True
            assert [item["number"] for item in result["available_candidates"]] == [20]
        # A hold does not substitute for the actual milestone person-wait proof.
        context["issues"].pop("someone/business#10")
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is False


def test_portfolio_capacity_shared_blocker_service_parity_and_unknown_context() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    parents = [global_issue("someone/business", n) for n in (10, 11)]
    wait = global_issue("someone/business", 12, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Customer testing.")
    tool = global_issue("someone/tools", 20)
    incident = global_issue("someone/product", 30, labels=["live-breakage"])
    edges = {(root["repo"], root["number"]): relationships(sub_issues=[parent]) for root, parent in zip(roots, parents)}
    edges.update({(parent["repo"], parent["number"]): relationships(blocked_by=[wait]) for parent in parents})
    with global_fixture(roots, [*parents, wait], edges, discovered=[tool, incident]) as (module, result, _reads):
        module.cmd_next(next_args())
        items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
        context = {"issues": {
            "someone/business#12": reviewed(items[12], "waiting", waiting_on="person"),
            "someone/tools#20": reviewed(items[20], category="repeated_stop_tooling"),
            "someone/product#30": reviewed(items[30], category="live_incident"),
        }}
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is True
            assert [item["number"] for item in result["available_candidates"]] == [30, 20]
        graph = module.github_direction_next.rank_direction_work(
            [{**root, "url": root["html_url"]} for root in roots], milestone_titles=["First", "Second"],
            read_node=lambda repo, number: {
                "item": items[number],
                "blockers": [module.compact_relationship_issue(wait, "blocked_by")] if number in (10, 11) else [],
                "children": [module.compact_relationship_issue(parents[number - 1], "sub_issue")] if repo == "someone/direction" else [],
            }, scan_limit=50,
        )
        # Reuse the CLI's full snapshots to compare the shared service surface.
        discoveries = [items[20], items[30]]
        kwargs: dict[str, Any] = dict(milestone_titles=["First", "Second"], selection_context=context,
                      repository_waypoints={"someone/tools": [], "someone/product": []})
        service = module.github_direction_next.rank_portfolio_work(graph, discoveries, **kwargs, coverage_complete=True)
        assert [item["number"] for item in service["available_candidates"]] == [30, 20]
        service = module.github_direction_next.rank_portfolio_work(graph, discoveries, **kwargs)
        assert [item["number"] for item in service["available_candidates"]] == [30]
        kwargs["repository_waypoints"]["someone/tools"] = None
        discoveries[0] = {**discoveries[0], "milestone": milestone_data(5, "First", created_at="2026-01-01")}
        service = module.github_direction_next.rank_portfolio_work(graph, discoveries, **kwargs, coverage_complete=True)
        assert service["tooling_capacity_context"]["reason"] == "unknown_milestone_context"
        assert service["tooling_capacity_context"]["issue"] == "someone/tools#20"


def test_portfolio_capacity_ignores_unrelated_waits_and_blocking_tooling() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    wait = global_issue("someone/business", 10, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Customer testing.")
    tool = global_issue("someone/tools", 20)
    unrelated_wait = global_issue("someone/product", 30, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Customer testing.")
    dependent = global_issue("someone/tools", 21)
    edges = {(root["repo"], root["number"]): relationships(sub_issues=[wait]) for root in roots}
    edges[(tool["repo"], 20)] = relationships(blocking=[dependent])
    edges[(dependent["repo"], 21)] = relationships(blocked_by=[tool])
    with global_fixture(roots, [wait], edges, discovered=[tool, dependent, unrelated_wait]) as (module, result, _reads):
        module.cmd_next(next_args())
        items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
        context = {"issues": {
            "someone/business#10": reviewed(items[10], "waiting", waiting_on="person"),
            "someone/tools#20": reviewed(items[20], category="repeated_stop_tooling"),
        }}
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
            assert result["tooling_capacity_context"]["admitted"] is True
            assert [item["number"] for item in result["available_candidates"]] == [20]
            assert next(item for item in result["excluded"] if item["number"] == 30)["discussion"]["ancestry_complete"] is True


def test_portfolio_capacity_preserves_wait_reports_when_ancestry_is_unavailable() -> None:
    waiting = global_issue("someone/product", 30, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Customer testing.")
    with global_fixture([], [], {}, discovered=[waiting]) as (module, result, _reads):
        module.load_direction = lambda *_: "# Direction\n\n## Milestones\n"
        module.cmd_next(next_args())
        item = next(item for item in result["excluded"] if item["number"] == 30)
        context = {"issues": {"someone/product#30": reviewed(item, "waiting", waiting_on="person")}}
        with patch.multiple(module, next_selection_context=lambda _args: context, read_next_parent=Mock(side_effect=module.PlanError("Parent unavailable"))):
            module.cmd_next(next_args())
        assert result["discovery_context"]["complete"] is True
        assert result["tooling_capacity_context"]["admitted"] is False
        assert next(item for item in result["excluded"] if item["number"] == 30)["exclusion"] == "waiting"
        assert result["waiting"][0]["number"] == 30
        assert result["tooling_capacity_context"]["admitted"] is False


def test_portfolio_capacity_includes_discovered_milestone_dependencies() -> None:
    for relationship in ("blocked_by", "sub_issues"):
        roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
        wait = global_issue("someone/business", 10, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Alex to test.\nWaiting since: 2026-08-20")
        container = global_issue("someone/extra", 30, milestone=milestone_data(5, "First", created_at="2026-01-01"))
        leaf = global_issue("someone/extra", 31, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Alex to test.")
        tool = global_issue("someone/tools", 20)
        edges = {(root["repo"], root["number"]): relationships(sub_issues=[wait]) for root in roots}
        edges[(container["repo"], 30)] = relationships(**{relationship: [leaf]})
        if relationship == "blocked_by":
            edges[(leaf["repo"], 31)] = relationships(blocking=[container])
        with global_fixture(roots, [wait], edges, discovered=[container, leaf, tool]) as (module, result, _reads):
            module.discover_direction_work = lambda *_a, **_kw: ([container, leaf, tool], {"complete": True, "repositories": [{"repo": "someone/extra", "direction": DIRECTION}, {"repo": "someone/tools", "direction": None}]})
            module.cmd_next(next_args())
            items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
            context = {"issues": {
                "someone/business#10": reviewed(items[10], "waiting", waiting_on="person"),
                "someone/tools#20": reviewed(items[20], category="repeated_stop_tooling"),
            }}
            with patch.multiple(module, next_selection_context=lambda _args: context):
                module.cmd_next(next_args())
                assert result["tooling_capacity_context"]["admitted"] is False
                assert result["tooling_capacity_context"]["issue"] == "someone/extra#31"
            current = next(item for item in result["excluded"] if item["number"] == 31)
            context["issues"]["someone/extra#31"] = reviewed(current, "waiting", waiting_on="person")
            with patch.multiple(module, next_selection_context=lambda _args: context):
                module.cmd_next(next_args())
                assert result["tooling_capacity_context"]["admitted"] is True
                assert [item["number"] for item in result["available_candidates"]] == [20]
            if relationship == "blocked_by":
                leaf["labels"] = ["plan", "plan:active"]
                edges[(leaf["repo"], 31)] = relationships(blocked_by=[container], blocking=[container])
                with patch.multiple(module, next_selection_context=lambda _args: context):
                    module.cmd_next(next_args())
                    assert result["tooling_capacity_context"]["reason"] == "milestone_dependency_cycle"


def test_portfolio_held_reads_do_not_consume_ordinary_scan_allowance() -> None:
    shared = load_module().github_direction_next
    held = [global_issue("someone/held", number) for number in range(1, 41)]
    ordinary = [global_issue("someone/product", number) for number in range(1, 61)]
    context = {"repository_holds": {"someone/held": {"reason": "Owner hold", "evidence": ["owner instruction"]}}}
    selected = shared.discovery_scan([*held, *ordinary], 50, context)
    assert [item["number"] for item in selected if item["repo"] == "someone/product"] == list(range(1, 51))
    assert len([item for item in selected if item["repo"] == "someone/held"]) == 40
    module = load_module()
    with patch.multiple(module.github_identity, github_app_config=lambda: {}), patch.multiple(
        module, load_direction=lambda *_: None,
        collect_paged_rest_items=lambda path, **_kw: ("automation-gh", [{"full_name": "someone/held"}]) if path == "/installation/repositories" else (_ for _ in ()).throw(module.PlanError("held inventory unavailable")),
    ):
        found, coverage = module.discover_direction_work("someone/direction", next_args(), selection_context=context, capacity_evidence=True)
        assert found == [] and coverage["complete"] is True
        assert coverage["capacity_complete"] is False
        assert coverage["repositories"][0]["exclusion"] == "repository_held"


def test_portfolio_available_milestone_skips_capacity_only_reads() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    leaf = global_issue("someone/business", 10)
    waiting = global_issue("someone/product", 30, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Customer testing.")
    edges = {(roots[0]["repo"], 1): relationships(sub_issues=[leaf])}
    with global_fixture(roots, [leaf], edges, discovered=[waiting]) as (module, result, _reads):
        modes = []
        def discover(_repo: str, _args: Any, **kwargs: Any) -> Any:
            modes.append(kwargs["capacity_evidence"])
            return [waiting], {"complete": True, "repositories": [{"repo": "someone/product", "direction": None}]}
        with patch.multiple(module, discover_direction_work=discover, read_next_parent=Mock(side_effect=AssertionError("excluded ancestry must not be read"))):
            module.cmd_next(next_args())
            candidate = next(item for item in result["candidates"] if item["number"] == 10)
            context = {"issues": {"someone/business#10": reviewed(candidate, category="milestone")}}
            with patch.multiple(module, next_selection_context=lambda _args: context):
                module.cmd_next(next_args())
        assert modes == [False, False]
        assert [item["number"] for item in result["available_candidates"]] == [10]
        assert result["waiting"][0]["number"] == 30


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
        if kwargs["query"].get("labels") == module.github_direction_next.LIVE_BREAKAGE_LABEL:
            assert kwargs["limit"] == module.NEXT_PLAN_INVENTORY_LIMIT + 1
            return "automation-gh", []
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
            with patch.multiple(module.github_identity, github_app_config=lambda: configured):
                found, coverage = module.discover_direction_work("someone/direction", args, selection_context=context, capacity_evidence=True)
            assert calls[0] == ("/installation/repositories" if configured else "/user/repos")
            assert [(item["repo"], item["number"]) for item in found] == [("someone/a", 1), ("someone/b", 1), ("someone/held", 1), ("someone/a", 2), ("someone/b", 2), ("someone/held", 2)]
            assert coverage["complete"] is False
            assert len(calls) == 9
            reasons = {source.get("exclusion") for source in coverage["repositories"]}
            assert reasons >= {"repository_held", "source_unavailable", "other_owner", "archived_or_disabled", "empty_without_open_issues", "issues_disabled"}


def test_portfolio_inventory_failure_and_scan_bound_never_claim_full_coverage() -> None:
    module = load_module()
    with patch.multiple(module.github_identity, github_app_config=lambda: {}), patch.multiple(module, collect_paged_rest_items=Mock(side_effect=module.PlanError("inventory denied"))):
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
    with patch.multiple(module.github_identity, github_app_config=lambda: {}), patch.multiple(module, collect_paged_rest_items=lambda *_args, **_kwargs: ("automation-gh", [{"full_name": "someone/archived", "archived": True}, {"full_name": "someone/unread"}])):
        found, coverage = module.discover_direction_work("someone/direction", args, selection_context={})
        assert found == [] and coverage["inventory_truncated"] and not coverage["complete"]
    failure = module.github_api_core.FailureDetail(cause="rest_primary_rate_limited", message="quota exhausted", retryable=False, fallback_eligible=False, disposition="stop")
    with patch.multiple(module.github_identity, github_app_config=lambda: {}), patch.multiple(module, collect_paged_rest_items=Mock(side_effect=module.PlanError("quota exhausted", failure=failure))):
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
        assert not any(wait["number"] == 3 for wait in result["waiting"])
        assert any(wait["number"] == 2 for wait in result["recorded_waits"])


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
        with patch.multiple(module, next_selection_context=lambda _args: context):
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
        with patch.multiple(module, load_direction=lambda *_: "# Direction\n## Order\nOwn projects.\n## Milestones\n"):
            module.cmd_next(next_args())
        assert [item["number"] for item in result["candidates"]] == [2]
        assert result["unowned"][0]["number"] == 1
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
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
        assert result["available_candidates"] == []
        assert result["candidates"][0]["discussion"]["digest"] != first["discussion"]["digest"]
        with patch.multiple(module, read_next_parent=Mock(side_effect=module.PlanError("parent inaccessible"))):
            module.cmd_next(next_args())
        assert result["candidates"] == []
        assert result["discovery_context"]["complete"] is False


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
        args.selection_context = "holds.json"
        try:
            module.cmd_next(args)
        except module.PlanError as exc:
            assert "OWNER/direction" in str(exc)
        else:
            raise AssertionError("Local next must not silently ignore global selection evidence")


def test_portfolio_service_discoveries_cannot_bypass_exclusions_or_parent_context() -> None:
    module = load_module()
    shared = module.github_direction_next
    discoveries = []
    for number, reason in enumerate(("blocked_by_open_dependency", "waiting", "unknown_dependencies", "delegated_to_open_sub_issues", None), 1):
        raw = global_issue("someone/product", number)
        candidate = {**raw, "url": raw["html_url"], "discussion": shared.discussion_snapshot(raw, [], complete=True)}
        if reason:
            candidate["exclusion"] = reason
        candidate = shared.include_parent_context(candidate, [], complete=True, tracking_roots=[])
        discoveries.append(candidate)
    context = {"issues": {f"someone/product#{item['number']}": reviewed(item) for item in discoveries}}
    ranked = shared.rank_portfolio_work({"candidates": []}, discoveries, milestone_titles=[], selection_context=context)
    assert [item["number"] for item in ranked["available_candidates"]] == [5]
    assert len(ranked["excluded"]) == 4
    assert ranked["waiting"] == []
    discoveries[-1]["discussion"].pop("ancestry_complete")
    ranked = shared.rank_portfolio_work({"candidates": []}, discoveries, milestone_titles=[], selection_context=context)
    assert ranked["available_candidates"] == []
    assert ranked["candidates"][0]["review_required"] == "complete_parent_context"


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
    assert len(calls) == 4


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
    with patch.multiple(module, collect_paged_rest_items=lambda *_a, **_kw: ("automation-gh", [issue(1)]),
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
    edges = {(incidents[1]["repo"], 2): relationships(blocked_by=[global_issue("someone/gate", 99)])}
    with global_fixture([], [], edges, discovered=incidents) as (module, result, _reads):
        module.cmd_next(next_args(scan_limit=1))
        reviews = {f"{item['repo']}#{item['number']}": reviewed(item) for item in result["candidates"]}
        reviews["someone/p4#4"]["state"] = "underway"
        context = {"issues": reviews, "repository_holds": {"someone/p1": {"reason": "Owner hold", "evidence": ["owner decision"]}}}
        with patch.multiple(module, next_selection_context=lambda _args: context,
                            load_direction=lambda *_: "# Direction\n## Milestones\n"):
            module.cmd_next(next_args(scan_limit=1))
        assert [item["number"] for item in result["available_candidates"]] == [5]
        assert {item["exclusion"] for item in result["excluded"]} >= {"repository_held", "blocked_by_open_dependency", "waiting"}
        assert result["underway"][0]["number"] == 4
        assert result["candidate_coverage"]["complete"]
        assert result["candidate_coverage"]["warning"] is None


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
        return "automation-gh", [global_issue("someone/quiet", 1), global_issue("someone/quiet", 2)]

    args = next_args()
    args.repository_issue_limit = 1
    with patch.multiple(module.github_identity, github_app_config=lambda: {}), patch.multiple(module, collect_paged_rest_items=collect, load_direction=lambda *_: DIRECTION):
        inventory, coverage = module.discover_direction_work("someone/direction", args, selection_context={})
    assert {item["number"] for item in inventory} == {1, 243}
    assert coverage["repositories"][0]["live_breakage_inventory"]["complete"]
    assert not coverage["complete"]  # Ordinary issue inventory remains bounded.
    assert len(calls) == 3


def test_live_breakage_label_inventory_failure_preserves_ordinary_work_and_reports_bounds() -> None:
    module = load_module()
    marker = module.github_direction_next.LIVE_BREAKAGE_LABEL
    incidents = [global_issue("someone/quiet", n, labels=[marker]) for n in (243, 244)]

    def collect(path: str, **kwargs: Any) -> Any:
        if path == "/installation/repositories":
            return "automation-gh", [{"full_name": "someone/quiet"}]
        if kwargs["query"].get("labels") == marker:
            raise module.PlanError("label inventory unavailable")
        return "automation-gh", [global_issue("someone/quiet", n) for n in (1, 2)]

    args = next_args()
    args.repository_issue_limit = 1
    with patch.multiple(module.github_identity, github_app_config=lambda: {}), patch.multiple(module, collect_paged_rest_items=collect, load_direction=lambda *_: DIRECTION):
        inventory, coverage = module.discover_direction_work("someone/direction", args, selection_context={})
        assert [item["number"] for item in inventory] == [1]
        assert coverage["repositories"][0]["live_breakage_inventory"]["error"]
        assert not coverage["complete"]

        original_collect = collect
        with patch.multiple(module, NEXT_PLAN_INVENTORY_LIMIT=1,
                            collect_paged_rest_items=lambda path, **kw: ("automation-gh", incidents) if kw["query"].get("labels") == marker else original_collect(path, **kw)):
            inventory, coverage = module.discover_direction_work("someone/direction", args, selection_context={})
        assert {item["number"] for item in inventory} == {1, 243}
        assert not coverage["repositories"][0]["live_breakage_inventory"]["complete"]


def test_live_breakage_label_query_to_direction_ranking_end_to_end() -> None:
    with global_fixture([], [], {}) as (module, result, _reads):
        marker = module.github_direction_next.LIVE_BREAKAGE_LABEL
        incident = global_issue("someone/quiet", 243, labels=[marker])

        def collect(path: str, **kwargs: Any) -> Any:
            if path.endswith("/comments") or path == "/repos/someone/direction/issues":
                return "automation-gh", []
            if path == "/installation/repositories":
                return "automation-gh", [{"full_name": "someone/quiet"}]
            if kwargs["query"].get("labels") == marker:
                return "automation-gh", [incident]
            return "automation-gh", [global_issue("someone/quiet", n) for n in (1, 2)]

        args = next_args(scan_limit=1)
        args.repository_issue_limit = 1
        with patch.multiple(module.github_identity, github_app_config=lambda: {}), patch.multiple(module,
                            discover_direction_work=module.real_discover_direction_work,
                            collect_paged_rest_items=collect,
                            load_direction=lambda *_: "# Direction\n## Milestones\n"):
            module.cmd_next(args)
        assert [item["number"] for item in result["candidates"]] == [243, 1]
        assert result["discovery_context"]["live_breakage_evaluated"] == 1
        assert not result["candidate_coverage"]["complete"]


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
    waiting = global_issue("someone/direction", 7, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Alex to decide.")
    blocker = global_issue("someone/product", 12)
    edges = {(roots[0]["repo"], 1): relationships(sub_issues=[waiting]), (waiting["repo"], 7): relationships(blocked_by=[blocker]), (blocker["repo"], 12): relationships(blocking=[waiting])}
    with global_fixture(roots, [waiting, blocker], edges, discovered=[blocker]) as (module, result, _reads):
        module.discover_direction_work = lambda *_a, **_kw: ([blocker], {"complete": True, "repositories": [{"repo": blocker["repo"], "direction": DIRECTION}]})
        module.cmd_next(next_args())
        candidate = result["candidates"][0]
        assert candidate["overall_milestone_context"]["state"] == "matched"
        assert candidate["overall_milestone_context"]["titles"] == ["First"]
        assert candidate["availability"] == "needs_review" and not candidate.get("via")
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
        module.next_selection_context = lambda *_: {"repository_holds": {"someone/held": {"reason": "Director hold", "evidence": ["owner instruction"]}}}
        args = next_args(scan_limit=1)
        args.agent = "codex"
        args.limit = 1
        module.cmd_next(args)
        bounded = result["discovery_context"].copy()
        assert bounded["evaluated"] == 4  # Three allowances plus a marked incident.
        assert bounded["capacity_unevaluated_count"] == len(inventory) - bounded["evaluated"]
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
        assert result["discovery_context"]["capacity_unevaluated_count"] == 0


def test_client_requests_rank_with_product_without_changing_order_or_authority() -> None:
    shared = load_module().github_direction_next
    def candidate(repo: str, number: int, author: str, title: str | None = None) -> dict[str, Any]:
        raw = {**global_issue(repo, number, labels=[], created_at="2026-01-01T00:00:00Z"), "user": {"login": author}}
        raw["labels"] = []
        raw["milestone"] = {"title": title, "state": "open"} if title else None
        item = {**shared.compact_list_issue(repo, raw), "milestone": raw["milestone"],
                "discussion": shared.discussion_snapshot(raw, [], complete=True), "reasons": []}
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
    context = {"issues": {f"{item['repo']}#{item['number']}": reviewed(item)
                          for item in [client, staff, absent, another, first]}}
    result = shared.rank_portfolio_work(graph, [client, staff, absent, another], selection_context=context, **kwargs)
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
    # Full discussion, current review, holds and ownership still fence Client work.
    unreviewed = shared.rank_portfolio_work(graph, [client], **kwargs)
    assert unreviewed["available_candidates"] == []
    assert unreviewed["candidates"][1]["availability"] == "needs_review"
    held = {**context, "repository_holds": {"someone/site": {"reason": "Director hold", "evidence": ["decision"]}}}
    assert [item["number"] for item in shared.rank_portfolio_work(graph, [client], selection_context=held, **kwargs)["available_candidates"]] == [10]
    blocked = {**client, "exclusion": "blocked_by_open_dependency"}
    assert [item["number"] for item in shared.rank_portfolio_work(graph, [blocked], selection_context=context, **kwargs)["available_candidates"]] == [10]
    # Explicitly reviewed breakage ranks first, without changing any label.
    context["issues"]["someone/site#20"] = reviewed(client, category="live_incident")
    incident = shared.rank_portfolio_work(graph, [client], selection_context=context, **kwargs)
    assert [item["number"] for item in incident["available_candidates"]] == [20, 10]
    assert incident["candidates"][0]["labels"] == []


def test_client_requests_inherit_native_product_track_and_still_need_reviews() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    product = global_issue("someone/site", 30)
    request = {**global_issue("someone/site", 31, labels=[]), "user": {"login": "client"}}
    with global_fixture(roots, [product], {(roots[1]["repo"], 2): relationships(sub_issues=[product])}, discovered=[request]) as (module, result, _reads):
        with patch.object(module.github_client, "recorded_client", return_value={"status": "recorded", "source": "repo_people_overlay", "login": "client"}):
            module.cmd_next(next_args())
            item = next(item for item in result["candidates"] if item["number"] == 31)
            assert item["client_request"]["milestone"] == "Second"
            assert result["available_candidates"] == []
            context = {"issues": {"someone/site#31": reviewed(item)}}
            with patch.object(module, "next_selection_context", return_value=context):
                module.cmd_next(next_args())
            assert [item["number"] for item in result["available_candidates"]] == [31]
            assert result["available_candidates"][0]["category"] == "milestone"


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


def test_unscanned_client_request_prevents_spare_capacity_admission() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    wait = {**global_issue("someone/site", 10, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Client testing."), "user": {"login": "client"}}
    tools = [global_issue("someone/tools", number) for number in (20, 21, 22)]
    request = {**global_issue("someone/site", 30), "user": {"login": "client"}}
    with global_fixture(roots, [wait], {("someone/direction", 1): relationships(sub_issues=[wait])}, discovered=[*tools, request]) as (module, result, _reads):
        with patch.object(module.github_client, "recorded_client", return_value={"status": "recorded", "source": "launchplane", "login": "client"}):
            module.cmd_next(next_args(scan_limit=3))
            items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
            context = {"issues": {"someone/site#10": reviewed(items[10], "waiting", waiting_on="person"),
                                  "someone/tools#20": reviewed(items[20], category="repeated_stop_tooling")}}
            with patch.object(module, "next_selection_context", return_value=context):
                module.cmd_next(next_args(scan_limit=3))
            assert result["tooling_capacity_context"]["admitted"] is False
            assert [item["number"] for item in result["discovery_context"]["unevaluated_milestone_issues"]] == [30]
            assert result["available_candidates"] == []


def test_global_waits_belong_to_their_own_open_waiting_issue() -> None:
    root = track("someone/direction", 3, "First")
    root["body"] = "## Current Status\nState: Waiting.\nWaiting for: #91\nLast verified: 2026-10-02"
    closed = global_issue("someone/product", 167, state="closed", labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Chris's approval.")
    active = global_issue("someone/product", 124, body="## Current Status\nWaiting for: nothing; this is agent work")
    pending = global_issue("someone/product", 141, labels=["plan", "plan:waiting"], body="## Current Status\nState: Waiting.\nBlocked by: None\nWaiting for: #167\nLast verified: 2026-10-05")
    edges = {(root["repo"], 3): relationships(sub_issues=[active, pending, closed])}
    with global_fixture([root], [active, pending, closed], edges) as (module, result, _reads):
        module.cmd_next(next_args())
    assert {(row["repo"], row["number"]) for row in result["waiting"]} == {("someone/direction", 3)}
    own = next(row for row in result["stale_waits"] if row["number"] == 141)
    assert own["last_verified"] == "2026-10-05"
    assert own["closed_references"][0]["number"] == 167
    assert own["stale_wait_evidence"]["evidence"][0]["kind"] == "completed_wait_issue"
    assert own["review_required"] is True
    track_wait = next(row for row in result["waiting"] if row["number"] == 3)
    assert track_wait["references"][0]["repo"] == "someone/direction"
    assert track_wait["references"][0]["state"] == "unknown"
    assert result["stale_wait_report"]["complete"] is False
    assert any(item["number"] == 124 for item in result["candidates"])
    assert not any(row["number"] in {91, 124, 167} for row in result["waiting"])


def test_global_agent_only_waits_are_unowned_and_real_holds_stay_parked() -> None:
    reasons = ["Supervisor routing", "engineering selection", "separately authorized activation", "future work", "None", "live acceptance"]
    items = [global_issue("someone/tools", number, labels=["plan", "plan:waiting"], body="## Current Status\nState: Waiting.\nBlocked by: None\nWaiting for: " + reason) for number, reason in enumerate(reasons, 1)]
    genuine = global_issue("someone/tools", 20, labels=["plan", "plan:waiting"], body="## Current Status\nWaiting for: Chris to perform the device test after #21 closes.")
    closed = global_issue("someone/tools", 21, state="closed")
    with global_fixture([], [closed], {}, discovered=[*items, genuine]) as (module, result, _reads):
        module.cmd_next(next_args())
    assert {row["number"] for row in result["unowned"]} == set(range(1, 7))
    assert [row["number"] for row in result["waiting"]] == [20]
    assert result["waiting"][0]["stale_wait_evidence"] is None
    assert {row["number"] for row in result["stale_wait_report"]["items"]} == {1, 2, 4, 5}
    assert result["candidates"] == []  # Report-only evidence never releases a hold.


def test_global_stale_wait_report_reuses_merged_pr_proof_and_refuses_capacity() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    waiting = global_issue("someone/business", 10, labels=["plan", "plan:waiting"], body="## Current Status\nState: Waiting.\nBlocked by: None\nWaiting for: PR #11 to merge\nLast verified: 2026-10-01")
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
            rows = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
            context = {"issues": {"someone/business#10": reviewed(rows[10], "waiting", waiting_on="person"), "someone/tools#20": reviewed(rows[20], category="repeated_stop_tooling")}}
            with patch.multiple(module, next_selection_context=lambda _args: context):
                module.cmd_next(next_args())
    assert result["stale_wait_report"]["items"][0]["evidence"][0]["kind"] == "merged_wait_pr"
    assert result["tooling_capacity_context"]["admitted"] is False
    assert result["available_candidates"] == []
    assert result["waiting"] == []
    assert result["stale_waits"][0]["number"] == 10
    assert result["stale_waits"][0]["last_verified"] == "2026-10-01"


def test_global_wait_reference_budget_and_auth_stop_are_explicit() -> None:
    module = load_module()
    item = global_issue("someone/business", 10, body="## Current Status\nWaiting for: Review " + " ".join(f"#{number}" for number in range(20, 32)))
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
        item = global_issue("someone/tools", 10, labels=labels, body="## Current Status\nState: Waiting.\nBlocked by: None\nWaiting for: separately authorized activation")
        with global_fixture([], [], {}, discovered=[item]) as (module, result, _reads):
            module.cmd_next(next_args())
        assert result["candidates"] == []
        assert next(row for row in result["excluded"] if row["number"] == 10)["exclusion"] == "waiting"
        assert result["stale_wait_report"]["items"] == []
        assert result["unowned"][0]["number"] == 10


def test_global_cross_repo_link_label_does_not_create_a_local_reference() -> None:
    pending = global_issue("someone/business", 10, labels=["plan:waiting"], body="## Current Status\nWaiting for: Alex to review [#12](https://github.com/other/product/issues/12)")
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
    parent = global_issue("someone/business", 10, labels=["plan:waiting"], body="## Current Status\nState: Waiting.\nBlocked by: None\nWaiting for: PR #11 to merge")
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
        raw = global_issue("someone/product", 2, body="## Current Status\nState: Active.\nWaiting for: Nothing.\n" + note)
        with global_fixture([root], [raw], {(root["repo"], 1): relationships(sub_issues=[raw])}) as (module, result, _reads):
            module.cmd_next(next_args())
        assert bool(result["candidates"]) is selectable
    raw = global_issue("someone/product", 2, labels=[], body="## Current Status\nWaiting for: provider capacity")
    with global_fixture([], [], {}, discovered=[raw]) as (module, result, _reads):
        module.cmd_next(next_args())
    assert result["candidates"] == []
    assert result["unowned"][0]["waiting_for"] == "provider capacity"
    assert result["excluded"][0]["exclusion"] == "waiting"
    # The Supervisor acting next is agent work, offered with the step it names.
    raw = global_issue("someone/product", 2, labels=[], body="## Current Status\nWaiting for: Supervisor to route the PR.")
    with global_fixture([], [], {}, discovered=[raw]) as (module, result, _reads):
        module.cmd_next(next_args())
    assert result["candidates"][0]["agent_next_step"] == ["Supervisor to route the PR."]
    assert result["unowned"] == []


def test_global_partial_unowned_holds_and_caller_wait_evidence_remain_visible() -> None:
    root = track("someone/direction", 1, "First")
    parent = global_issue("someone/product", 2, body="## Current Status\nState: Active.\nWaiting for: separately authorized production activation")
    child = global_issue("someone/product", 3)
    edges = {(root["repo"], 1): relationships(sub_issues=[parent]), (parent["repo"], 2): relationships(sub_issues=[child])}
    with global_fixture([root], [parent, child], edges) as (module, result, _reads):
        module.cmd_next(next_args())
    assert [item["number"] for item in result["candidates"]] == [3]
    assert result["recorded_waits"][0]["number"] == 2
    assert result["recorded_waits"][0]["unowned"] is True
    shared = module.github_direction_next
    raw = global_issue("someone/product", 4, body="## Current Status\nWaiting for: the next agent")
    item = {**shared.compact_list_issue(raw["repo"], raw), "plan_status": "active", "discussion": shared.discussion_snapshot(raw, [], complete=True)}
    context = {"issues": {"someone/product#4": reviewed(item, "waiting", reason="Chris must finish the device test.")}}
    ranked = shared.rank_portfolio_work({"candidates": [item]}, [], milestone_titles=[], selection_context=context)
    assert any(row["waiting_for"] == context["issues"]["someone/product#4"]["reason"] and row.get("source") == "caller_review" for row in ranked["recorded_waits"])
    assert ranked["candidates"] == []


def test_global_wait_comments_named_actors_and_wrapped_urls_use_own_evidence() -> None:
    root = track("someone/direction", 1, "First")
    comment = global_issue("someone/product", 2, labels=["plan:waiting"], body="## Current Status\n<!--\nWaiting for: who or what\n-->")
    named = global_issue("someone/product", 3, labels=["plan:waiting"], body="## Current Status\nWaiting for: separately authorized activation by Chris")
    wrapped = global_issue("someone/product", 4, labels=["plan:waiting"], body="## Current Status\nWaiting for: approved prerequisite readback\nhttps://github.com/other/product/issues/12")
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


def test_global_unread_frontier_wait_prevents_capacity_without_unrelated_veto() -> None:
    module = load_module()
    raw = global_issue("someone/business", 3, labels=["plan:waiting"], milestone=milestone_data(1, "First", created_at="2026-01-01"), body="## Current Status\nWaiting for: Customer testing.")
    shared = module.github_direction_next
    item = {**shared.compact_list_issue(raw['repo'], raw), "plan_status": "waiting", "exclusion": "waiting", "milestone": raw['milestone'], "discussion": shared.discussion_snapshot(raw, [], complete=True)}
    context = {"issues": {"someone/business#3": reviewed(item, "waiting", waiting_on="person")}}
    graph = {"candidates": [], "excluded": [item], "dependency_context": {"complete": True}}
    with patch.multiple(module, api_json=Mock(side_effect=AssertionError('unselected wait must not be read')), load_config=lambda *_: module.DEFAULT_CONFIG):
        report = module.next_wait_context([global_issue('someone/other', 1), raw], scan_limit=1, inventory_complete=True)
    checked = {(row['repo'], row['number']) for row in report['checked_issues']}
    item['wait_evidence_complete'] = (raw['repo'], raw['number']) in checked
    ranked = shared.rank_portfolio_work(graph, [], milestone_titles=['First'], selection_context=context, repository_waypoints={raw['repo']: ['First']}, coverage_complete=True)
    assert ranked['tooling_capacity_context']['required'] == 'current_wait_evidence'
    assert ranked['tooling_capacity_context']['admitted'] is False
    item['wait_evidence_complete'] = True
    ranked = shared.rank_portfolio_work(graph, [], milestone_titles=['First'], selection_context=context, repository_waypoints={raw['repo']: ['First']}, coverage_complete=True)
    assert ranked['tooling_capacity_context']['admitted'] is True  # Other report coverage does not replace this own-issue proof.


def test_local_and_global_next_exclude_stale_landing_but_keep_split_work() -> None:
    status = "## Current Status\nState: Source complete.\nNext action: Supervisor lands PR https://github.com/someone/product/pull/71 and reconciles closure.\nWaiting for: None.\n"
    stale = global_issue("someone/product", 10, body=status)
    split = global_issue("someone/product", 11, body=status.replace("Supervisor lands PR", "Test split remainder after PR"))
    person = global_issue("someone/product", 12, body=split["body"].replace("Waiting for: None.", "Waiting for: Chris to test on the phone."))
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
            assert any(row["number"] == 12 and "Chris" in row["waiting_for"] for row in result["recorded_waits"])
            assert not any(row["number"] == 12 for row in result["stale_waits"])
            excluded = next(row for row in result["excluded"] if row["number"] == 10)
            assert excluded["exclusion"] == "landed_status_needs_reconciliation"
            assert not excluded["post_merge_evidence"]["completion_proven"]
            result.clear()
            with patch.multiple(module, collect_paged_rest_items=lambda *_a, **_kw: ("automation-gh", [stale, split])):
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


def test_active_timestamp_uncertainty_and_custom_labels_reach_selection_output() -> None:
    module = load_module()
    row = global_issue("someone/product", 10, labels=["plan", "custom-active"],
                       updated_at="2026-10-06T12:00:00Z",
                       body="## Current Status\nNext action: Supervisor lands PR https://github.com/someone/product/pull/71 and closes this issue.\n")
    def fetch(_method: str, path: str, **_kwargs: Any) -> Any:
        return "automation-gh", ({"default_branch": "main"} if path == "/repos/someone/product" else
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
    with patch.multiple(module, api_json=Mock(side_effect=AssertionError("no PR read needed")), load_config=lambda *_: config):
        report = module.next_wait_context([row, active], scan_limit=1, inventory_complete=True)
    assert report["complete"] and report["checked_issues"] == [{"repo": row["repo"], "number": 11}]


TESTS = [
    test_active_timestamp_uncertainty_and_custom_labels_reach_selection_output,
    test_active_post_merge_read_gaps_remain_visible_on_selection_candidates,
    test_local_and_global_next_exclude_stale_landing_but_keep_split_work,
    test_global_adjacent_notes_preserve_absence_and_pending_clauses,
    test_global_partial_unowned_holds_and_caller_wait_evidence_remain_visible,
    test_global_wait_comments_named_actors_and_wrapped_urls_use_own_evidence,
    test_global_unread_frontier_wait_prevents_capacity_without_unrelated_veto,

    test_global_no_wait_notes_do_not_park_active_work,
    test_global_unowned_authorization_holds_never_become_candidates_or_stale,
    test_global_cross_repo_link_label_does_not_create_a_local_reference,
    test_global_parent_only_stale_wait_moves_out_of_current_waiting,

    test_global_waits_belong_to_their_own_open_waiting_issue,
    test_global_agent_only_waits_are_unowned_and_real_holds_stay_parked,
    test_global_stale_wait_report_reuses_merged_pr_proof_and_refuses_capacity,
    test_global_wait_reference_budget_and_auth_stop_are_explicit,

    test_unscanned_client_request_prevents_spare_capacity_admission,
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
    test_live_breakage_label_inventory_failure_preserves_ordinary_work_and_reports_bounds,
    test_live_breakage_label_query_to_direction_ranking_end_to_end,
    test_milestone_candidate_coverage_is_scoped_to_graph,
    test_live_breakage_beyond_scan_window_ranks_first_without_granting_availability,
    test_live_breakage_marker_preserves_holds_dependencies_waits_and_review,
    test_live_breakage_inventory_finds_incident_beyond_repository_issue_bound,
    test_local_next_inbound_scan_preserves_plan_budget_and_waits,
    test_inbound_scan_bounds_and_partial_reads_are_explicit,
    test_inbound_unclassified_failure_degrades_report_but_quota_stops,
    test_portfolio_nonempty_unparsed_direction_and_local_only_flags_refuse,
    test_portfolio_service_discoveries_cannot_bypass_exclusions_or_parent_context,
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
    test_portfolio_capacity_admission_ranks_stops_then_age_and_keeps_own_projects,
    test_portfolio_capacity_needs_current_person_waits_and_complete_graph,
    test_portfolio_capacity_truncated_discovery_admits_tooling_but_not_incomplete_graph,
    test_portfolio_capacity_reports_reviews_before_unread_excluded_ancestry,
    test_portfolio_capacity_unscanned_known_milestone_prevents_admission,
    test_portfolio_capacity_partial_milestone_source_differs_from_unrelated_source,
    test_portfolio_capacity_waiting_discovery_milestone_blocks_admission,
    test_portfolio_capacity_shared_blocker_service_parity_and_unknown_context,
    test_portfolio_capacity_ignores_unrelated_waits_and_blocking_tooling,
    test_portfolio_capacity_preserves_wait_reports_when_ancestry_is_unavailable,
    test_portfolio_capacity_includes_discovered_milestone_dependencies,
    test_portfolio_held_reads_do_not_consume_ordinary_scan_allowance,
    test_portfolio_available_milestone_skips_capacity_only_reads,
    test_portfolio_inventory_sources_exclusions_round_robin_and_read_bounds,
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
    test_global_inconsistent_blocked_parent_makes_evidence_incomplete,
    test_global_empty_tracks_are_not_reported_as_people_waits,
    test_global_service_consumer_uses_the_same_classification_and_sort_as_cli,
    test_global_placeholder_waits_do_not_park_actionable_work,
    test_global_active_parent_partial_wait_need_not_name_an_issue,
    test_global_shared_classifier_preserves_incomplete_relationship_evidence,
]


def test_milestone_order_wait_is_a_finding_and_remains_available() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    for reason in ("starts after milestones 'First' and 'Second'", "", "nothing", "TBD", "TBD; since 2026-08-20", "unknown; since 2026-08-20"):
        leaf = global_issue("someone/business", 120, labels=["plan", "plan:waiting"],
                            body="## Current Status\nState: Waiting.\nWaiting for: " + reason)
        edges = {(roots[1]["repo"], 2): relationships(sub_issues=[leaf])}
        with global_fixture(roots, [leaf], edges) as (module, result, _reads):
            module.cmd_next(next_args())
            candidate = next(item for item in result["candidates"] if item["number"] == 120)
            context = {"issues": {"someone/business#120": reviewed(candidate, category="milestone")}}
            with patch.multiple(module, next_selection_context=lambda _args: context):
                module.cmd_next(next_args())
            assert [item["number"] for item in result["available_candidates"]] == [120]
            assert any(item["number"] == 120 and item["kind"] == "milestone_wait_invalid" for item in result["findings"])
            assert not any(item["number"] == 120 for item in result["waiting"])
            assert result["tooling_capacity_context"]["admitted"] is False


def test_capacity_names_each_milestone_and_wait_start_for_shared_person_gate() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    wait = global_issue("someone/business", 10, labels=["plan", "plan:waiting"],
                        body="## Current Status\nWaiting for: Alex to test.\nWaiting since: 2026-08-20")
    edges = {(root["repo"], root["number"]): relationships(blocked_by=[wait]) for root in roots}
    with global_fixture(roots, [wait], edges) as (module, result, _reads):
        module.cmd_next(next_args())
        item = next(item for item in result["excluded"] if item["number"] == 10)
        context = {"issues": {"someone/business#10": reviewed(item, "waiting", waiting_on="person")}}
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
        capacity = result["tooling_capacity_context"]
        assert capacity["admitted"] is True
        assert {row["milestone"] for row in capacity["milestone_waits"]} == {"First", "Second"}
        for row in capacity["milestone_waits"]:
            assert row["waits"][0]["waiting_for"] == "Alex to test."
            assert row["waits"][0]["since"] == "2026-08-20"


def test_capacity_wait_dates_belong_to_each_frontier_issue() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    waits = [global_issue("someone/business", number, labels=["plan", "plan:waiting"],
                          body=f"## Current Status\nWaiting for: {person} to test.\nWaiting since: {since}")
             for number, person, since in ((10, "Alex", "2026-08-20"), (11, "Sam", "2026-09-02"))]
    edges = {(root["repo"], root["number"]): relationships(blocked_by=[wait])
             for root, wait in zip(roots, waits)}
    with global_fixture(roots, waits, edges) as (module, result, _reads):
        module.cmd_next(next_args())
        items = {item["number"]: item for item in result["excluded"]}
        context = {"issues": {f"someone/business#{wait['number']}": reviewed(items[wait["number"]], "waiting", waiting_on="person")
                              for wait in waits}}
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
        capacity = result["tooling_capacity_context"]
        assert capacity["admitted"] is True
        records = {row["milestone"]: row["waits"][0] for row in capacity["milestone_waits"]}
        assert (records["First"]["waiting_for"], records["First"]["since"]) == ("Alex to test.", "2026-08-20")
        assert (records["Second"]["waiting_for"], records["Second"]["since"]) == ("Sam to test.", "2026-09-02")


def test_milestone_order_detection_preserves_additional_human_holds() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    for reason in ("First and Chris approves the testing date.", "starts after First and Chris to sign the contract.",
                   "First\nParked until: Alex approves the copy.",
                   "First\nBlocked by: Chris's contract signature.", "First\nState: Waiting on Alex."):
        leaf = global_issue("someone/business", 120, labels=["plan", "plan:waiting"],
                            body="## Current Status\nWaiting for: " + reason)
        with global_fixture(roots, [leaf], {(roots[1]["repo"], 2): relationships(sub_issues=[leaf])}) as (module, result, _reads):
            module.cmd_next(next_args())
            assert not result["candidates"] and not result["findings"]
            assert result["waiting"]
    shared = load_module().github_direction_next
    item = {"repo": "someone/business", "number": 120, "url": "https://github.com/someone/business/issues/120"}
    for reason in ("First to ship", "Second is done", "earlier milestones to finish", "the previous milestone", "the next milestone", "First closes", "First is complete", "First wraps up", "First to be completed", "‘First’", "v2_beta"):
        assert not shared.milestone_wait_evidence(item, "Waiting for: " + reason, ["First", "Second", "v2_beta"])["valid"]
    assert shared.milestone_wait_evidence(item, "Waiting for: Alex to retest the build shipped since 2026-07-01", ["First"])["since"] is None
    assert shared.milestone_wait_evidence(item, "Waiting for: Alex to test; since 2026-08-20", ["First"])["since"] == "2026-08-20"


TESTS.extend([test_milestone_order_detection_preserves_additional_human_holds, test_capacity_wait_dates_belong_to_each_frontier_issue, test_milestone_order_wait_is_a_finding_and_remains_available,
              test_capacity_names_each_milestone_and_wait_start_for_shared_person_gate])


def test_discovered_milestone_wait_remains_available_and_cannot_admit_tooling() -> None:
    leaf = global_issue("someone/business", 120, labels=["plan", "plan:waiting"],
                        milestone=milestone_data(7, "First", created_at="2026-01-01"),
                        body="## Current Status\nWaiting for: milestone Second.")
    with global_fixture([], [], {}, discovered=[leaf]) as (module, result, _reads):
        module.discover_direction_work = lambda *_a, **_kw: ([leaf], {"complete": True,
            "repositories": [{"repo": leaf["repo"], "direction": DIRECTION}]})
        module.cmd_next(next_args())
        item = result["candidates"][0]
        assert item["discussion"]["ancestry_complete"] is True
        context = {"issues": {"someone/business#120": reviewed(item)}}
        with patch.multiple(module, next_selection_context=lambda _args: context):
            module.cmd_next(next_args())
        assert [item["number"] for item in result["available_candidates"]] == [120]
        assert result["findings"][0]["reason"] == "wait_names_another_milestone"
        assert result["tooling_capacity_context"]["admitted"] is False


def test_supported_wait_forms_and_person_work_do_not_become_ordering_findings() -> None:
    roots = [track("someone/direction", 1, "First")]
    for status in ("Parked until: 2026-12-01 holiday freeze.", "**Waiting for:** Alex to test.", "**Waiting for**: Alex to test.",
                   "Blocked by: No native issue blocker; waiting for Alex to test.\nWaiting for:\nLast verified: 2026-10-01",
                   "Blocked by: No native issue blocker; waiting for Alex to test.",
                   "Waiting for: Alex to approve the beta copy."):
        leaf = global_issue("someone/business", 120, labels=["plan:waiting"],
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
    leaf = global_issue("someone/business", 120, labels=["plan:waiting"],
                        milestone=milestone_data(7, "First", created_at="2026-01-01"),
                        body="## Current Status\nWaiting for: TBD.")
    with global_fixture([], [], {}, discovered=[leaf]) as (module, result, _reads):
        module.discover_direction_work = lambda *_a, **_kw: ([leaf], {"complete": True,
            "repositories": [{"repo": leaf["repo"], "direction": "# Direction\n## Milestones\n"}]})
        module.cmd_next(next_args())
        assert not result["candidates"] and not result["findings"]
        assert next(item for item in result["excluded"] if item["number"] == 120)["exclusion"] == "waiting"


def test_capacity_requires_own_wait_instead_of_inheriting_parent() -> None:
    shared = load_module().github_direction_next
    parent_raw = global_issue("someone/business", 10, body="## Current Status\nWaiting for: Alex to test.\nWaiting since: 2026-08-20")
    parent = {**parent_raw, "url": parent_raw["html_url"], "discussion": shared.discussion_snapshot(parent_raw, [], complete=True)}
    child_raw = global_issue("someone/business", 11)
    child = {**child_raw, "url": child_raw["html_url"], "milestone": {"title": "First"}, "via": [parent],
             "exclusion": "parent_waiting", "waiting_on_parent": parent["url"],
             "discussion": {**shared.discussion_snapshot(child_raw, [], complete=True), "parents": [parent], "ancestry_complete": True}}
    graph = {"candidates": [], "excluded": [child], "dependency_context": {"complete": True}}
    context = {"issues": {"someone/business#11": reviewed(child, "waiting", waiting_on="person")}}
    result = shared.tooling_capacity_context(graph, [], milestone_titles=["First"], context=context,
                                             repository_waypoints={}, coverage_complete=True)
    assert result["admitted"] is False
    assert result["required"] == "current_own_issue_wait"


TESTS.extend([test_discovered_milestone_wait_remains_available_and_cannot_admit_tooling,
              test_supported_wait_forms_and_person_work_do_not_become_ordering_findings,
              test_unmatched_local_milestone_keeps_its_recorded_wait,
              test_capacity_requires_own_wait_instead_of_inheriting_parent])


def test_invalid_discovered_parent_wait_delegates_to_its_child() -> None:
    parent = global_issue("someone/business", 120, labels=["plan:waiting"],
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


def test_empty_track_discovery_still_reads_held_milestone_capacity_evidence() -> None:
    roots = [track("someone/direction", 1, "First"), track("someone/direction", 2, "Second")]
    first = global_issue("someone/business", 10, labels=["plan:waiting"], body="## Current Status\nWaiting for: Alex to test.")
    second = global_issue("someone/business", 11, labels=["plan:waiting"], milestone=milestone_data(7, "Second", created_at="2026-01-01"),
                          body="## Current Status\nWaiting for: Alex to test.")
    held = global_issue("someone/held", 12, labels=["plan:waiting"], milestone=milestone_data(8, "Second", created_at="2026-01-01"),
                        body="## Current Status\nWaiting for: Alex to test.")
    tool = global_issue("someone/tools", 20)
    modes = []
    with global_fixture(roots, [first], {(roots[0]["repo"], 1): relationships(sub_issues=[first])},
                        discovered=[second, held, tool]) as (module, result, _reads):
        def discover(_repo: str, _args: Any, **kwargs: Any) -> Any:
            mode = kwargs["capacity_evidence"]
            modes.append(mode)
            return [second, tool, *([held] if mode else [])], {"complete": True, "repositories": [
                {"repo": "someone/business", "direction": DIRECTION},
                {"repo": "someone/tools", "direction": None},
                {"repo": "someone/held", "direction": DIRECTION if mode else None}]}
        module.discover_direction_work = discover
        module.cmd_next(next_args())
        items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
        context = {"repository_holds": {"someone/held": {"reason": "Director hold", "evidence": ["recorded hold"]}},
                   "issues": {**{f"someone/business#{number}": reviewed(items[number], "waiting", waiting_on="person") for number in (10, 11)},
                              "someone/tools#20": reviewed(items[20], category="repeated_stop_tooling")}}
        with patch.object(module, "next_selection_context", return_value=context):
            module.cmd_next(next_args())
        assert modes[-1] is True
        assert result["tooling_capacity_context"]["admitted"] is False
        assert result["tooling_capacity_context"]["issue"] == "someone/held#12"
        assert not result["available_candidates"]


def test_multiline_wait_and_literal_milestone_wait_have_opposite_results() -> None:
    shared = load_module().github_direction_next
    for reason in ("Waiting for:\n- Alex to approve the copy.", "Waiting for: milestone review call with Alex on 2026-10-10.",
                   "Waiting for: once launch partners sign."):
        assert shared.milestone_wait_evidence({"repo": "someone/business", "number": 10, "url": "https://github.com/someone/business/issues/10"}, reason, ["Launch", "Dogfood week", "Thin fork decision"])["valid"]
    for reason in ("Waiting for: Dogfood week to finish.", "Waiting for: the Thin fork decision milestone."):
        assert not shared.milestone_wait_evidence({"repo": "someone/business", "number": 10, "url": "https://github.com/someone/business/issues/10"}, reason, ["Launch", "Dogfood week", "Thin fork decision"])["valid"]
    assert not shared.milestone_wait_evidence({"repo": "someone/business", "number": 10, "url": "https://github.com/someone/business/issues/10"}, "State: Waiting on Alex", ["Launch"])["valid"]


TESTS.extend([test_empty_track_discovery_still_reads_held_milestone_capacity_evidence,
              test_multiline_wait_and_literal_milestone_wait_have_opposite_results])


def test_milestone_scope_still_recognizes_other_listed_milestone_waits() -> None:
    roots = [track("someone/direction", 1, "First")]
    leaf = global_issue("someone/business", 120, labels=["plan:waiting"],
                        body="## Current Status\nWaiting for: Second to finish.")
    with global_fixture(roots, [leaf], {(roots[0]["repo"], 1): relationships(sub_issues=[leaf])}) as (module, result, _reads):
        with patch.object(module.github_milestone_core, "show_milestone", return_value={"milestone": roots[0]["milestone"]}):
            module.cmd_next(next_args(milestone="First"))
        assert [item["number"] for item in result["candidates"]] == [120]
        assert result["findings"][0]["reason"] == "wait_names_another_milestone"


TESTS.append(test_milestone_scope_still_recognizes_other_listed_milestone_waits)


def test_discovery_preserves_valid_waits_above_invalid_child_or_parent_waits() -> None:
    grand = global_issue("someone/business", 40, labels=["plan:waiting"],
                         body="## Current Status\nWaiting for: Alex to sign the contract.")
    parent = global_issue("someone/business", 41, labels=["plan:waiting"],
                          body="## Current Status\nWaiting for: TBD.")
    for chain in ([grand], [parent, grand], [parent]):
        child = global_issue("someone/business", 50, labels=["plan:waiting"],
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
    leaf = global_issue("someone/business", 120, labels=["plan:blocked"], body="## Current Status\nState: Waiting.")
    edges = {(root["repo"], root["number"]): relationships(sub_issues=[leaf]) for root in roots}
    with global_fixture(roots, [leaf], edges) as (module, result, _reads):
        module.cmd_next(next_args())
        assert not result["candidates"]
        assert next(item for item in result["excluded"] if item["number"] == 120)["exclusion"] == "waiting"
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
    leaf = global_issue("someone/business", 10, labels=["plan:waiting"], body="## Current Status\nWaiting for: Alex to test.")
    unrelated = global_issue("someone/other", 50, labels=["plan:waiting"], body="## Current Status\nWaiting for: TBD.")
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
            assert not result["discovery_context"]["capacity_complete"]
            assert next(item for item in result["excluded"] if item["number"] == 50)["exclusion"] == "unknown_ancestry"


def test_unmatched_parent_wait_does_not_emit_internal_exclusion() -> None:
    parent = global_issue("someone/other", 40, labels=["plan:waiting"], body="## Current Status\nWaiting for: Alex to test.")
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
    pull = {**global_issue("someone/business", 120, labels=["plan:waiting"]), "pull_request": {}}
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
    site = global_issue("someone/site", 91, labels=["plan", "plan:waiting"], body="## Current Status\nState: Waiting.\nWaiting for: Justin's release acceptance.")
    release = global_issue("another/platform", 2682, labels=["plan", "plan:blocked"])
    notice = global_issue("another/platform", 3073, body="## Current Status\nState: Active.\nWaiting for: Supervisor delivery closeout; Justin's acceptance belongs to #2682. No new Director decision is open.")
    credentials = global_issue("someone/tenant", 124, body="## Current Status\nState: Active.\nWaiting for: None for bounded preparation; no live apply or grant is authorized by this audit.")
    unrelated = global_issue("someone/tooling", 10)
    edges = {
        ("someone/direction", 1): relationships(sub_issues=[site, release, credentials]),
        ("another/platform", 2682): relationships(blocked_by=[notice]),
        ("another/platform", 3073): relationships(blocking=[release]),
    }
    with global_fixture([first, second], [site, release, notice, credentials], edges, discovered=[unrelated]) as (module, result, _reads):
        module.cmd_next(next_args())
    ranked = [(item["repo"], item["number"]) for item in result["candidates"]]
    assert ranked[:3] == [("another/platform", 3073), ("someone/tenant", 124), ("someone/direction", 2)]
    assert ranked[-1] == ("someone/tooling", 10)
    assert result["candidates"][0]["agent_next_step"][0].startswith("Supervisor delivery closeout")
    assert [step["number"] for step in result["candidates"][0]["via"]] == [1, 2682, 3073]
    summary = {entry["milestone"]: entry for entry in result["milestone_summary"]}
    assert summary["First"]["state"] == "work_listed"
    assert [wait["number"] for wait in summary["First"]["waits"]] == [91]
    assert [ref["number"] for ref in summary["First"]["next_action_outside_graph"]] == [99]
    assert [item["number"] for item in summary["Second"]["work"]] == [2]
    assert result["candidate_coverage"]["milestone_ranking_complete"] is True


def test_milestone_summary_names_each_wait_when_no_milestone_work_is_listed() -> None:
    first = active_track("someone/direction", 1, "First", "State: Active.\nNext action: someone/site#91.")
    site = global_issue("someone/site", 91, labels=["plan", "plan:waiting"], body="## Current Status\nState: Waiting.\nWaiting for: Justin's release acceptance.")
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
        raw = global_issue("someone/product", 2, body="## Current Status\nState: Active.\nWaiting for: " + reason)
        with global_fixture([root], [raw], {(root["repo"], 1): relationships(sub_issues=[raw])}) as (module, result, _reads):
            module.cmd_next(next_args())
        assert result["candidates"] == [], reason
        assert next(item for item in result["excluded"] if item["number"] == 2)["exclusion"] == "waiting"


TESTS.extend([test_milestone_agent_work_behind_bookkeeping_blocker_ranks_first,
              test_global_scan_diagnostic_counts_all_allowances_and_limit_only_caps_results,
              test_milestone_summary_names_each_wait_when_no_milestone_work_is_listed,
              test_named_next_actor_or_absence_keeps_a_stated_precondition,
              test_track_whose_next_action_belongs_to_a_person_is_not_agent_work])


def main() -> None:
    for test in TESTS:
        test()
        print(f"ok {test.__name__}")
    print(f"\nAll {len(TESTS)} tests passed.")


if __name__ == "__main__":
    main()
