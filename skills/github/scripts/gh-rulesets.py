#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Plan or explicitly apply the standard ruleset pair to owned repositories."""

from __future__ import annotations

import argparse
import sys
from typing import Any

import github_api as github_api_core
import github_rulesets


COMMAND_CONTEXT = {
    "plan": ("read", False),
    "apply": ("mixed", True),
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
    return parser.parse_args(argv)


def operation_name(command: str) -> str:
    return f"github.rulesets.{command}"


def run(
    args: argparse.Namespace,
    *,
    client: github_rulesets.RulesetClientProtocol | None = None,
    app_id: int | None = None,
) -> dict[str, Any]:
    client = client or github_rulesets.RulesetClient()
    app_id = github_rulesets.configured_app_id() if app_id is None else app_id
    owner, repositories = github_rulesets.resolve_repositories(
        client=client,
        repos=args.repo,
        all_owned=args.all_owned,
        owner=args.owner,
    )
    specs = github_rulesets.standard_specs(app_id)
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
