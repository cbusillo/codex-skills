#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Plan or explicitly apply the standard ruleset pair, and optionally the client lock, to owned repositories."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import github_api as github_api_core
import github_rulesets


COMMAND_CONTEXT = {
    "plan": ("read", False),
    "apply": ("mixed", True),
    "checks-plan": ("read", False),
    "checks-apply": ("mixed", True),
    "checks-readback": ("read", False),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = github_api_core.TerminalArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=github_api_core.TerminalArgumentParser,
    )
    for command in ("plan", "apply"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--repo", action="append", default=[], help="OWNER/REPO; repeat for an ordered set.")
        sub.add_argument(
            "--all-owned",
            action="store_true",
            help="Include every non-archived repository owned by --owner.",
        )
        sub.add_argument("--owner", help="Expected active GitHub owner; required with --all-owned.")
        sub.add_argument(
            "--client-lock",
            action="store_true",
            help="Also manage a ruleset that stops anyone but the admin role and bypass Apps "
                 "from creating, updating or deleting any branch.",
        )
        sub.add_argument(
            "--client-lock-tags",
            action="store_true",
            help="Also manage the same lock for every tag.",
        )
        sub.add_argument(
            "--bypass-integration-id",
            action="append",
            type=int,
            default=[],
            help="Extra GitHub App id that bypasses the client lock; Dependabot and Renovate always bypass "
                 "the branch lock. Repeatable.",
        )
        if command == "apply":
            sub.add_argument(
                "--confirm-owner-admin-write",
                action="store_true",
                help="Required acknowledgement that the active owner account may create or update rulesets.",
            )
            sub.add_argument(
                "--confirm-repository-count",
                type=int,
                help="Required for more than one repository and must equal the resolved repository count.",
            )
    plan = subparsers.add_parser("checks-plan")
    plan.add_argument("--repo", required=True)
    plan.add_argument("--target", required=True, help="classic, new, or an existing default-branch ruleset id")
    plan.add_argument("--checks-file", required=True, type=Path, help="JSON list of context/app_id objects; null means any App")
    plan.add_argument("--out", required=True, type=Path, help="New file for the reviewable plan")
    for command in ("checks-apply", "checks-readback"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--plan-file", required=True, type=Path)
        sub.add_argument("--plan-digest", required=True)
        if command == "checks-apply":
            sub.add_argument("--confirm-owner-admin-write", action="store_true")
    return parser.parse_args(argv)


def read_check_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise github_rulesets.RulesetError(f"cannot read required-check input: {exc}", cause="validation_error") from exc


def run_checks(args: argparse.Namespace, client: github_rulesets.RulesetClientProtocol) -> dict[str, Any]:
    if args.command == "checks-plan":
        checks = read_check_json(args.checks_file)
        # Validate additions before using the owner client.
        github_rulesets.normalize_checks(checks, binding="app_id")
        owner, _ = github_rulesets.resolve_repositories(client=client, repos=[args.repo], all_owned=False, owner=None)
        plan = github_rulesets.build_check_plan(args.repo, args.target, checks,
                                               github_rulesets.check_snapshot(client, args.repo, args.target))
        try:
            with args.out.open("x") as stream:
                json.dump(plan, stream, indent=2, sort_keys=True, allow_nan=False)
                stream.write("\n")
        except (OSError, ValueError, TypeError) as exc:
            raise github_rulesets.RulesetError(f"cannot save required-check plan: {exc}", cause="validation_error") from exc
        return {"owner": owner, "actor": client.actor, "plan_file": str(args.out),
                "plan_digest": github_rulesets.check_plan_digest(plan), "plan": plan,
                "changed": plan["changed"], "write_authorized": False}
    plan = github_rulesets.validate_check_plan(read_check_json(args.plan_file), args.plan_digest)
    applying = args.command == "checks-apply"
    if applying and not args.confirm_owner_admin_write:
        raise github_rulesets.RulesetError("checks-apply requires --confirm-owner-admin-write",
                                           cause="authorization_required")
    owner, _ = github_rulesets.resolve_repositories(client=client, repos=[plan["repo"]], all_owned=False, owner=None)
    result = github_rulesets.run_check_plan(client, plan, apply=applying)
    return {"owner": owner, "actor": client.actor, "plan_digest": args.plan_digest,
            "write_authorized": applying, **result,
            "limits": ["GitHub has no conditional-write token for this endpoint; preflight and readback cannot lock concurrent settings writers."]}


def operation_name(command: str) -> str:
    return f"github.rulesets.{command.replace('-', '.')}"


def run(
    args: argparse.Namespace,
    *,
    client: github_rulesets.RulesetClientProtocol | None = None,
    app_id: int | None = None,
) -> dict[str, Any]:
    client = client or github_rulesets.RulesetClient()
    if args.command.startswith("checks-"):
        return run_checks(args, client)
    app_id = github_rulesets.configured_app_id() if app_id is None else app_id
    lock_branches = bool(getattr(args, "client_lock", False))
    lock_tags = bool(getattr(args, "client_lock_tags", False))
    extra_integration_ids = list(getattr(args, "bypass_integration_id", None) or [])
    lock_specs = github_rulesets.client_lock_specs(
        app_id,
        branches=lock_branches,
        tags=lock_tags,
        extra_integration_ids=extra_integration_ids,
    )
    owner, repositories = github_rulesets.resolve_repositories(
        client=client,
        repos=args.repo,
        all_owned=args.all_owned,
        owner=args.owner,
    )
    exempt = [repo for repo in repositories if repo.casefold() in github_rulesets.UPSTREAM_CONTRIBUTION_FORKS]
    repositories = [repo for repo in repositories if repo not in exempt]
    specs = (*github_rulesets.standard_specs(app_id), *lock_specs)
    if args.command == "apply":
        if not args.confirm_owner_admin_write:
            raise github_rulesets.RulesetError(
                "apply requires --confirm-owner-admin-write",
                cause="authorization_required",
            )
        repository_count = len(repositories)
        if repository_count > 1:
            if args.confirm_repository_count != repository_count:
                raise github_rulesets.RulesetError(
                    f"apply resolved {repository_count} repositories; "
                    f"pass --confirm-repository-count {repository_count}",
                    cause="confirmation_mismatch",
                    payload={"repository_count": repository_count},
                )
        results = []
        for repo in repositories:
            try:
                results.append(github_rulesets.apply_repository(client, repo, specs))
            except github_rulesets.RulesetError as exc:
                raise github_rulesets.RulesetError(
                    str(exc),
                    cause=exc.cause,
                    payload={
                        **exc.payload,
                        "failed_repository": repo,
                        "completed_repositories": [item["repo"] for item in results],
                    },
                ) from exc
    else:
        results = [github_rulesets.plan_repository(client, repo, specs) for repo in repositories]
    return {
        "owner": owner,
        "actor": client.actor,
        "app_id": app_id,
        "limits": [] if app_id is not None else [{
            "kind": "no_app_bypass",
            "detail": "No GitHub App is configured; only repository administrators can update the default branch. "
                      "The direction rule still has no bypass; an owner-authored PR needs another eligible code owner.",
        }],
        "client_lock": {
            "branches": lock_branches,
            "tags": lock_tags,
            "bypass_integration_ids": sorted(set(extra_integration_ids)) if lock_specs else [],
        },
        "exempt_repositories": exempt,
        "repository_count": len(repositories),
        "changed": any(result["changed"] for result in results),
        "repositories": results,
        "write_authorized": args.command == "apply",
    }


def failure_context(*, is_write: bool, payload: dict[str, Any]) -> tuple[str | None, str]:
    failed_step = str(payload.get("failed_step") or "preflight")
    if not is_write:
        return None, failed_step
    api_result = payload.get("api_result")
    api_failure = api_result.get("failure") if isinstance(api_result, dict) else None
    reported = api_failure.get("write_outcome") if isinstance(api_failure, dict) else None
    if payload.get("applied") or payload.get("completed_repositories"):
        return "unknown", failed_step
    if reported in {"not_started", "rejected", "unknown"}:
        return str(reported), failed_step
    if payload.get("write_attempted"):
        return "unknown", failed_step
    return "not_started", failed_step


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = github_api_core.requested_subcommand(argv, set(COMMAND_CONTEXT))
    operation = operation_name(command or "unknown")
    mutation_class, is_write = COMMAND_CONTEXT.get(command, ("unknown", False))
    try:
        args = parse_args(argv)
        payload = run(args)
    except (github_api_core.ArgumentParsingError, github_rulesets.RulesetError) as exc:
        cause = exc.cause if isinstance(exc, github_rulesets.RulesetError) else "validation_error"
        extra = exc.payload if isinstance(exc, github_rulesets.RulesetError) else {}
        write_outcome, failed_step = failure_context(is_write=is_write, payload=extra)
        failure = github_api_core.FailureDetail(
            cause=cause,
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="requires_authorization" if cause == "authorization_required" else "stop",
            write_outcome=write_outcome,
            failed_step=failed_step,
        )
        return github_api_core.emit_terminal(
            github_api_core.terminal_failure(
                failure,
                operation=operation,
                payload=extra,
                transport="rest_api",
                bucket="rest_core",
                failed_step=failed_step,
                exit_code=2 if cause in {"validation_error", "authorization_required", "confirmation_mismatch"} else 1,
            ),
            stderr_message=f"error: {exc}",
        )
    return github_api_core.emit_terminal(
        github_api_core.terminal_success(
            payload,
            operation=operation,
            actor=payload.get("actor"),
            expected_actor=payload.get("owner"),
            transport="rest_api",
            bucket="rest_core",
            completed_steps=["resolve_owner", "resolve_repositories", mutation_class],
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
