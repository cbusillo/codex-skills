#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import direction_audit as audit
import direction_stranded

sys.path.insert(0, str(audit.REPO_ROOT / "skills/supervisor/scripts"))
import account_choice
import iterm_tab

# https://docs.renovatebot.com/configuration-options/
RENOVATE_ROOT_FILES = frozenset({"renovate.json", "renovate.json5", ".renovaterc", ".renovaterc.json", ".renovaterc.json5"})
RENOVATE_GITHUB_FILES = frozenset({"renovate.json", "renovate.json5"})
AGENT = "agent"
DIRECTOR = "Director"


class Setup(NamedTuple):
    label: str
    fixer: str
    fix: str


SETUPS = {
    "app": Setup("automation App", DIRECTOR, "install the automation GitHub App on all repositories of the account"),
    "merge_train": Setup("merge train", AGENT, "propose enrollment with launchplane-write-action.py "
                                               "privileged-policy-propose; the Director approves it on the Access policy card"),
    "renovate": Setup("Renovate", AGENT, "open a PR adding renovate.json that extends the shared preset"),
    "rulesets": Setup("ruleset pair", AGENT, "gh-rulesets.py apply --repo OWNER/REPO --confirm-owner-admin-write"),
    "rulesets_unavailable": Setup("rulesets (plan)", DIRECTOR,
                                  "a private repository on GitHub Free has no rulesets; make it public or change the plan"),
    "checkout": Setup("no checkout", AGENT, "clone it into the Developer folder"),
    "codex_trust": Setup("Codex trust", DIRECTOR,
                         "run codex -C DIRECTORY interactively in each named Codex home and accept the folder-trust prompt"),
}
PolicyReader = Callable[[str], dict | None]


class SetupUnknown(Exception):
    pass


def gap(repo: str | None, setup: str, detail: str = "") -> dict[str, Any]:
    return {"repo": repo, "setup": setup, "detail": detail, "fixer": SETUPS[setup].fixer}


def merge_train_missing(repo: str, policy_reader: PolicyReader) -> bool:
    policy = policy_reader(repo)
    if policy is None:
        raise SetupUnknown("merge_train")
    return policy["status"] != "enrolled"


def file_names(repo: str, path: str, fetch: Callable[[list[str]], Any]) -> set[str]:
    try:
        listing = fetch(["api", f"repos/{repo}/contents/{path}", "--method", "GET"])
    except audit.AuditError as exc:
        raise SetupUnknown("renovate") from exc
    if not isinstance(listing, list):
        raise SetupUnknown("renovate")
    return {str(item.get("name")) for item in listing if isinstance(item, dict)}


def renovate_missing(repo: str, fetch: Callable[[list[str]], Any]) -> bool:
    root = file_names(repo, "", fetch)
    if root & RENOVATE_ROOT_FILES:
        return False
    return ".github" not in root or not file_names(repo, ".github", fetch) & RENOVATE_GITHUB_FILES


def ruleset_gap(repo: str, fetch: Callable[[list[str]], Any]) -> dict[str, Any] | None:
    try:
        rulesets, cut = audit.fetch_rulesets(repo, fetch=fetch)
    except audit.AuditError as exc:
        raise SetupUnknown("rulesets") from exc
    if rulesets is None:
        return gap(repo, "rulesets_unavailable")
    if cut:
        raise SetupUnknown("rulesets")
    missing = audit.github_rulesets.missing_standard_rulesets(rulesets)
    if not missing:
        return None
    partial = len(missing) < len(audit.github_rulesets.required_ruleset_names())
    return gap(repo, "rulesets", "; ".join(missing) if partial else "")


def trust_gaps(repo: str, developer: Path, codex_homes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    directory = developer / repo.split("/")[1]
    if not directory.is_dir():
        return [gap(repo, "checkout")]
    untrusted = [home["name"] for home in codex_homes if not codex_trusted(directory, home)]
    return [gap(repo, "codex_trust", ", ".join(untrusted))] if untrusted else []


def codex_trusted(directory: Path, home: dict[str, Any]) -> bool:
    config = Path(home["env"]["CODEX_HOME"]).expanduser() / "config.toml"
    try:
        return iterm_tab.codex_folder_trusted(iterm_tab.codex_project_entry(directory, config).project)
    except ValueError as exc:
        raise SetupUnknown("codex_trust") from exc


def repository_gaps(repo: str, *, fetch: Callable[[list[str]], Any], policy_reader: PolicyReader,
                    developer: Path, codex_homes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    gaps: list[dict[str, Any]] = []
    unknown: list[dict[str, str]] = []
    checks: list[Callable[[], list[dict[str, Any]]]] = [
        lambda: [gap(repo, "merge_train")] if merge_train_missing(repo, policy_reader) else [],
        lambda: [gap(repo, "renovate")] if renovate_missing(repo, fetch) else [],
        lambda: [found] if (found := ruleset_gap(repo, fetch)) else [],
        lambda: trust_gaps(repo, developer, codex_homes),
    ]
    for check in checks:
        try:
            gaps.extend(check())
        except SetupUnknown as exc:
            unknown.append({"repo": repo, "source": str(exc), "reason": "unavailable"})
    return gaps, unknown


def installation_readable(fetch: Callable[[list[str]], Any]) -> bool:
    try:
        fetch(["api", "installation/repositories?per_page=1", "--method", "GET"])
    except audit.AuditError:
        return False
    return True


def collect(owner: str, *, fetch: Callable[[list[str]], Any], policy_reader: PolicyReader,
            developer: Path, codex_homes: list[dict[str, Any]]) -> dict[str, Any]:
    repos, cut = audit.owner_repositories(owner, fetch=fetch)
    repos = {key: repo for key, repo in repos.items() if key.casefold() not in audit.github_rulesets.UPSTREAM_CONTRIBUTION_FORKS}
    coverage = audit.repository_listing_gaps(cut, fetch=fetch)
    gaps = [gap(None, "app") for listing in coverage if listing["reason"] == "installation_selected_repositories"]
    if not installation_readable(fetch):
        coverage.append({"source": "app", "reason": "unavailable"})
    for key in sorted(repos):
        found, unknown = repository_gaps(str(repos[key]["full_name"]), fetch=fetch, policy_reader=policy_reader,
                                         developer=developer, codex_homes=codex_homes)
        gaps.extend({**row, "fork": bool(repos[key].get("fork"))} for row in found)
        coverage.extend(unknown)
    return {"owner": owner, "repositories": len(repos), "complete": not coverage, "gaps": gaps, "coverage_gaps": coverage}


def markdown(result: dict[str, Any]) -> str:
    coverage = "complete" if result["complete"] else "INCOMPLETE"
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for row in result["gaps"]:
        by_repo.setdefault(row["repo"] or "account", []).append(row)
    if not by_repo:
        lines = [f"Repository setup: nothing missing across {result['repositories']} repositories ({coverage})."]
    else:
        lines = [f"Repository setup: {len(by_repo)} of {result['repositories']} repositories miss something ({coverage})."]
        lines += [f"- {repo}{' (fork)' if rows[0].get('fork') else ''}: {', '.join(map(gap_text, rows))}"
                  for repo, rows in by_repo.items()]
        lines.append("Fixes:")
        lines += [f"- {SETUPS[setup].label}, {SETUPS[setup].fixer}: {SETUPS[setup].fix}"
                  for setup in SETUPS if any(row["setup"] == setup for row in result["gaps"])]
    lines += [f"- Unread: {row.get('repo', 'account')} {row['source']} ({row['reason']})" for row in result["coverage_gaps"]]
    return "\n".join(lines) + "\n"


def gap_text(row: dict[str, Any]) -> str:
    label = SETUPS[row["setup"]].label
    return f"{label} ({row['detail']})" if row["detail"] else label


def codex_accounts() -> list[dict[str, Any]]:
    return [account for account in account_choice.load_config()["accounts"] if account["provider"] == "openai"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report the standard setup each repository is missing, read-only.")
    parser.add_argument("--owner", required=True, help="the Director's GitHub account")
    parser.add_argument("--gh", default=str(audit.WRAPPER), help="gh-compatible command used for reads")
    parser.add_argument("--developer", type=Path, default=Path.home() / "Developer", help="folder holding the checkouts")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--deadline-seconds", type=float, default=600)
    args = parser.parse_args(argv)
    deadline_at = time.time() + args.deadline_seconds
    try:
        homes = codex_accounts()
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": f"cannot read the Codex accounts for folder trust: {exc}"}))
        return 1
    try:
        result = collect(
            args.owner, fetch=lambda a: audit.gh_json(a, gh=args.gh), developer=args.developer, codex_homes=homes,
            policy_reader=lambda repo: direction_stranded.train_policy(repo, deadline_at=deadline_at),
        )
    except audit.AuditError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(markdown(result) if args.format == "markdown" else json.dumps(result, indent=2))
    return 0 if result["complete"] else 3


if __name__ == "__main__":
    sys.exit(main())
