#!/usr/bin/env python3
"""Small behavior-oriented checks for skill instructions.

These checks complement structural validation by guarding high-impact wording
that shapes when skills fire and what agents say when optional context is
unavailable.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_chronicle_stays_quiet_when_unavailable() -> None:
    text = (ROOT / "chronicle" / "SKILL.md").read_text()
    lower = text.lower()
    normalized = " ".join(lower.split())

    require(
        "do not use it for ordinary repo, github, filesystem, or memory-context questions" in normalized,
        "Chronicle must not trigger for ordinary repo/GitHub/filesystem/memory questions",
    )
    require(
        "do not mention chronicle status unless the user explicitly asked" in normalized,
        "Chronicle unavailable status should stay quiet unless the user asked for it",
    )
    require(
        "this skill must be used whenever you need to resolve ambiguity" not in normalized,
        "Chronicle must not regain broad mandatory ambiguity-trigger wording",
    )


def test_launchplane_product_config_uses_operator_api_first() -> None:
    text = (ROOT / "launchplane" / "SKILL.md").read_text()
    lower = text.lower()
    normalized = " ".join(lower.split())

    require(
        "use the service api path from the operator contract first" in normalized,
        "Launchplane product-config work should start with the service API path",
    )
    require(
        "post /v1/product-config/apply" in normalized,
        "Launchplane operator guidance must name the product-config service route",
    )
    require(
        "~/.config/launchplane/local-operator.env" in normalized,
        "Launchplane operator guidance must point at local operator credentials",
    )
    require(
        "do not assume a global `launchplane` binary exists" in normalized,
        "Launchplane guidance must not make the global CLI the first-shot path",
    )
    require(
        "launchplane host-only cli helpers" in normalized,
        "Launchplane CLI guidance should be quarantined as host-only",
    )
    require(
        "explicitly on the launchplane host via ssh" in normalized,
        "Launchplane CLI guidance should require the host or a concrete command",
    )


def test_github_plan_sweeps_stale_related_issues() -> None:
    plan_text = (ROOT / "github-plan" / "SKILL.md").read_text().lower()
    github_text = (ROOT / "github" / "SKILL.md").read_text().lower()
    normalized_plan = " ".join(plan_text.split())
    normalized_github = " ".join(github_text.split())

    require(
        "stale github planning state is a regression source" in normalized_plan,
        "GitHub planning guidance must name stale issues as a regression source",
    )
    require(
        "update every related issue" in normalized_plan,
        "GitHub planning closeout must update all related issues that changed",
    )
    require(
        "stale, duplicate, related, and pr-linked issues were swept" in normalized_plan,
        "GitHub plan closeout checklist must include stale/duplicate/related/PR-linked sweep",
    )
    require(
        "use `github-plan` to sweep stale/duplicate/related planning issues" in normalized_github,
        "GitHub execution closeout must delegate related issue sweep to github-plan",
    )


def main() -> None:
    tests = [
        test_chronicle_stays_quiet_when_unavailable,
        test_launchplane_product_config_uses_operator_api_first,
        test_github_plan_sweeps_stale_related_issues,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"not ok {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
