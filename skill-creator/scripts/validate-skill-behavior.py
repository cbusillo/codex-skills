#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Small behavior-oriented checks for skill instructions.

These checks complement structural validation by guarding high-impact wording
that shapes when skills fire and what agents say when optional context is
unavailable.
"""

from __future__ import annotations

import json
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
        "prefer signed-in, scoped operator sessions" in normalized,
        "Launchplane operator guidance must prefer signed-in scoped operator sessions",
    )
    require(
        "source terminal/local operator credentials only through the operator contract" in normalized,
        "Launchplane operator guidance must keep terminal credentials contract-bound",
    )
    require(
        "missing private config means the write-capable path is unavailable and must fail closed" in normalized,
        "Launchplane operator guidance must fail closed when private config is missing",
    )
    require(
        "do not use `.github/github.override.json` for launchplane credentials" in normalized,
        "Launchplane operator guidance must not store credentials in GitHub overrides",
    )
    require(
        "post /v1/work-graph/merge-train/controller/run-once" in normalized,
        "Launchplane guidance must name the merge-train controller route",
    )
    require(
        "phase-specific merge-train endpoints as detail or recovery surfaces" in normalized,
        "Launchplane merge-train guidance must not make phase endpoints the default path",
    )
    require(
        "do not hardcode repositories, labels, tokens" in normalized,
        "Launchplane guidance must forbid hardcoded merge-train/operator config",
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


def test_launchplane_operator_config_stays_private_and_optional() -> None:
    contract = (ROOT / "launchplane" / "references" / "operator-contract.md").read_text()
    normalized = " ".join(contract.lower().split())

    require(
        "terminal/operator execution is optional private configuration" in normalized,
        "Launchplane operator config must remain optional private configuration",
    )
    require(
        "references/launchplane-operator.local.example.json" in normalized,
        "Launchplane operator contract must point to the fake local config example",
    )
    require(
        "real token values stay in the operator's private environment or secret manager" in normalized,
        "Launchplane operator contract must keep real tokens out of committed config",
    )
    require(
        "missing private config is a normal unavailable state" in normalized,
        "Launchplane operator contract must treat missing private config as unavailable",
    )
    require(
        "explicit write actions must fail closed" in normalized,
        "Launchplane operator contract must fail closed for explicit writes",
    )
    require(
        "do not use `.github/github.override.json` for secrets" in normalized,
        "Launchplane operator contract must forbid storing secrets in GitHub overrides",
    )

    example = json.loads(
        (ROOT / "launchplane" / "references" / "launchplane-operator.local.example.json").read_text()
    )
    require(
        example["service_url"] == "https://launchplane.example.invalid",
        "Launchplane operator example must use a fake public-safe service URL",
    )
    require(
        example["operator_token_env"] == "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
        "Launchplane operator example must name an env var instead of storing a token",
    )
    require(
        not any(str(value).startswith(("ghp_", "github_pat_", "sk-")) for value in example.values()),
        "Launchplane operator example must not contain token-like placeholder values",
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


def test_github_cross_repo_pr_create_is_explicit() -> None:
    github_text = (ROOT / "github" / "SKILL.md").read_text().lower()
    normalized = " ".join(github_text.split())

    require(
        "when creating a pr for a repository other than the current working directory" in normalized,
        "GitHub skill must warn about cross-repo PR creation context",
    )
    require(
        "pass both `--repo owner/repo` and an explicit `--head` branch" in normalized,
        "GitHub skill must require --repo plus explicit --head for cross-repo PR create",
    )


def main() -> None:
    tests = [
        test_chronicle_stays_quiet_when_unavailable,
        test_launchplane_product_config_uses_operator_api_first,
        test_launchplane_operator_config_stays_private_and_optional,
        test_github_plan_sweeps_stale_related_issues,
        test_github_cross_repo_pr_create_is_explicit,
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
