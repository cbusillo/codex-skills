#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Show the maintained permission profile or audit the configured App with safe reads."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone

import github_capabilities as capabilities
import github_identity
import github_read


def repository_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise argparse.ArgumentTypeError("repository must use owner/name")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    profile = commands.add_parser("profile", help="Derive the default or read-only profile from the operation matrix")
    profile.add_argument("--read-only", action="store_true")
    audit = commands.add_parser("audit", help="Audit installation membership, grants and safe repository probes")
    targets = audit.add_mutually_exclusive_group(required=True)
    targets.add_argument("--repo", action="append", type=repository_name, help="owner/name; repeat for multiple repositories")
    targets.add_argument("--all-installed", action="store_true")
    audit.add_argument("--refresh-token", action="store_true", help="Renew the same App's cached token after accepted permission changes")
    commands.add_parser("fingerprints", help="Print current API surface hashes for review; never modifies the matrix")
    return parser


def run_audit(args: argparse.Namespace, matrix: dict) -> dict:
    config = github_identity.github_app_config()
    if config is None:
        return {"state": "unavailable", "reason": "github_app_not_configured"}
    installation = github_identity.github_app_installation_metadata(config)
    if installation["suspended"]:
        return {"state": "unavailable", "reason": "installation_suspended", "installation": installation}
    if args.refresh_token:
        _, actor = github_identity.github_app_auth(config, refresh=True)
        if actor != installation["actor"]:
            return {"state": "unavailable", "reason": "app_actor_mismatch"}
    reader = github_read.GitHubReader(
        expected_actor=str(installation["actor"]), strict_actor=True,
        operation="github.capabilities.audit", gh_prefix_args=["--require-automation-auth"])
    repositories = reader.paged_json("/installation/repositories", step_prefix="installation_membership", collection_key="repositories")
    names = [entry.get("full_name") for entry in repositories]
    if any(not isinstance(name, str) or not name for name in names):
        return {"state": "unavailable", "reason": "installation_membership_invalid"}
    membership = {name.casefold() for name in names}
    targets = sorted(names if args.all_installed else set(args.repo))
    required = capabilities.permission_profile(matrix)["permissions"]["repository"]
    findings = []
    for index, repository in enumerate(targets, start=1):
        print(f"Auditing {repository} ({index}/{len(targets)})", file=sys.stderr, flush=True)
        findings.append(capabilities.audit_repository(
            reader, repository, installation=installation, membership=membership, capabilities=matrix["capabilities"]))
    return {"state": "audited", "observed_at": datetime.now(timezone.utc).isoformat(),
            "installation": installation, "installed_repository_count": len(names),
            "missing_repository_grants": capabilities.permission_gaps(required, installation["permissions"]),
            "repositories": findings,
            "write_proof": "No repository write was attempted. Accepted grants do not prove write execution or authorization."}


def main() -> int:
    args = build_parser().parse_args()
    matrix = capabilities.load_matrix()
    root = capabilities.MATRIX.parents[2]
    errors = capabilities.validate_permissions(matrix, root, check_sources=False)
    if errors:
        print(json.dumps({"state": "unavailable", "reason": "invalid_permission_catalog", "errors": errors}))
        return 1
    if args.command == "profile":
        result = capabilities.permission_profile(matrix, read_only=args.read_only)
    elif args.command == "fingerprints":
        result = capabilities.source_fingerprints(root)
    else:
        try:
            result = run_audit(args, matrix)
        except (github_identity.GitHubAppError, github_read.GitHubReadError,
                github_read.GitHubReadShapeError, OSError, ValueError):
            # Provider bodies and exception strings can contain sensitive payloads.
            result = {"state": "unavailable", "reason": "app_audit_failed", "retry_with_another_actor": False}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result.get("state") == "unavailable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
