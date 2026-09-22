#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Tests for the DIRECTION.md gate on milestone creation, rename, and reopen.

The gate reads the target repository's merged DIRECTION.md through the GitHub
API. A local checkout is never consulted, so these tests never touch the
filesystem; they fake the API read and the milestone backend.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import pathlib
import sys
from typing import Any

SCRIPT = pathlib.Path(__file__).with_name("gh-plan.py")

DIRECTION = """# Direction

## Milestones
- `Dogfood week` proves daily use; ends if the owner stops using it.
-   `Thin fork decision`   proves the engine choice.
- Not a milestone because it has no backticks

## Notes
- `Looks like one` but lives under the wrong heading
"""


def load_module() -> Any:
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("gh_plan_direction_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_contents(module: Any, *, text: str | None = None, stderr: str = "", stdout: str | None = None) -> list[list[str]]:
    """Make run_raw answer the contents read with a merged file, a 404, or a failure."""
    calls: list[list[str]] = []

    def run_raw(args: list[str], **_: Any) -> tuple[str, str, str]:
        calls.append(args)
        if stdout is not None:
            return "automation-gh", stdout, stderr
        if text is None:
            return "automation-gh", json.dumps({"message": "Not Found", "status": "404"}), "gh: Not Found (HTTP 404)"
        payload = {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}
        return "automation-gh", json.dumps(payload), ""

    module.run_raw = run_raw
    return calls


def update_args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = dict(
        repo="owner/repo", milestone="7", title=None, description=None, description_file=None,
        due_on=None, clear_due_on=False, state=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def wire_backend(module: Any, *, current_title: str = "Retired waypoint") -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"update": [], "create": [], "show": []}
    module.milestone_route = lambda: ("gh", "bot")
    module.default_repo = lambda explicit=None: explicit or "owner/repo"
    module.emit = lambda payload: None
    core = module.github_milestone_core
    core.update_milestone = lambda repo, ref, **kw: calls["update"].append(kw) or {"ok": True}
    core.create_milestone = lambda repo, title, **kw: calls["create"].append(title) or {"ok": True}
    core.show_milestone = lambda repo, ref, **kw: calls["show"].append(ref) or {"ok": True, "milestone": {"title": current_title, "number": 7}}
    return calls


def expect_refusal(module: Any, action: Any, *, mention: str) -> None:
    try:
        action()
    except module.PlanError as exc:
        assert mention in str(exc), exc
        assert exc.failure is not None and exc.failure.write_outcome == "not_started"
    else:
        raise AssertionError(f"expected refusal mentioning {mention!r}")


def test_titles_come_only_from_backticked_milestone_lines() -> None:
    module = load_module()
    assert module.direction_milestone_titles(DIRECTION) == ["Dogfood week", "Thin fork decision"]


def test_gate_reads_the_target_repo_not_the_checkout() -> None:
    module = load_module()
    calls = fake_contents(module, text=DIRECTION)
    assert module.load_direction("owner/repo") == DIRECTION
    assert calls == [["api", "repos/owner/repo/contents/DIRECTION.md", "--method", "GET"]]
    assert module.check_direction_lists_milestone("Dogfood week", DIRECTION, repo="owner/repo") == {
        "direction": "listed",
        "direction_source": "owner/repo:DIRECTION.md",
    }
    for title in ("Agent invented this", "dogfood week", "Looks like one"):
        expect_refusal(module, lambda t=title: module.check_direction_lists_milestone(t, DIRECTION, repo="owner/repo"), mention=title)


def test_repo_without_direction_file_is_not_adopted() -> None:
    module = load_module()
    fake_contents(module, text=None)
    assert module.load_direction("owner/repo") is None
    assert module.check_direction_lists_milestone("Anything", None, repo="owner/repo") == {"direction": "not_adopted"}


def test_unreadable_direction_file_refuses_instead_of_assuming_not_adopted() -> None:
    module = load_module()
    fake_contents(module, stdout="", stderr="gh: server error (HTTP 502)")
    expect_refusal(module, lambda: module.load_direction("owner/repo"), mention="refusing")


def test_create_rename_and_reopen_are_gated_before_any_write() -> None:
    module = load_module()
    fake_contents(module, text=DIRECTION)
    calls = wire_backend(module, current_title="Retired waypoint")

    create = argparse.Namespace(repo="owner/repo", title="Unlisted", description=None, description_file=None, due_on=None, state="open")
    expect_refusal(module, lambda: module.cmd_milestone_create(create), mention="Unlisted")
    create.title = "Dogfood week"
    module.cmd_milestone_create(create)
    assert calls["create"] == ["Dogfood week"]

    expect_refusal(module, lambda: module.cmd_milestone_update(update_args(title="Unlisted title")), mention="Unlisted title")
    assert calls["update"] == [], "no update request may be sent before the gate"
    module.cmd_milestone_update(update_args(title="Thin fork decision"))
    assert calls["update"][-1]["title"] == "Thin fork decision"

    # Reopening resolves the milestone's current title and gates on it.
    expect_refusal(module, lambda: module.cmd_milestone_update(update_args(state="open")), mention="Retired waypoint")
    assert calls["show"] == ["7"] and len(calls["update"]) == 1

    # Updates that neither rename nor reopen are not gated and do not read direction.
    before = len(calls["show"])
    module.cmd_milestone_update(update_args(due_on="2026-10-01"))
    assert len(calls["update"]) == 2 and len(calls["show"]) == before


def test_reopen_of_a_listed_milestone_passes() -> None:
    module = load_module()
    fake_contents(module, text=DIRECTION)
    calls = wire_backend(module, current_title="Dogfood week")
    module.cmd_milestone_update(update_args(state="open"))
    assert calls["update"][-1]["state"] == "open"


def main() -> int:
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"ok {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
