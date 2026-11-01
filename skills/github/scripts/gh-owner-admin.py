#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, NoReturn

import github_api as api
import github_rulesets as rules

SETTINGS = {
    "deleteBranchOnMerge": "delete_branch_on_merge",
}
MERGE_METHODS = {"merge": "allow_merge_commit", "squash": "allow_squash_merge", "rebase": "allow_rebase_merge"}
RUNNER_LABELS_PATH = re.compile(r"/actions/runners/([1-9][0-9]*)/labels")
FORK_APPROVAL_PATH = "/actions/permissions/fork-pr-contributor-approval"
FORK_APPROVAL_POLICIES = {
    "first_time_contributors_new_to_github",
    "first_time_contributors",
    "all_external_contributors",
}
MANAGED_NAMES = {
    rules.LANDING_RULESET_NAME,
    rules.DIRECTION_RULESET_NAME,
    rules.CLIENT_BRANCH_LOCK_NAME,
    rules.CLIENT_TAG_LOCK_NAME,
    *rules.LANDING_RULESET_ALIASES,
}


def fail(message: str, cause: str = "validation_error", **payload: Any) -> NoReturn:
    raise rules.RulesetError(message, cause=cause, payload=payload)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        fail(f"cannot read JSON input: {exc}")


def settings_request(config: Any) -> dict[str, Any]:
    try:
        expected = config.get("githubSettings", {}).get("expected", {})
        method = config.get("prWorkflow", {}).get("mergePolicy", {}).get("method")
        if not isinstance(expected, dict) or set(expected) - SETTINGS.keys():
            fail("unsupported expected repository setting; supported names are " + ", ".join(SETTINGS))
        payload = {}
        for name, value in expected.items():
            if not isinstance(value, bool):
                fail(f"{name} must be boolean")
            payload[SETTINGS[name]] = value
        if method is not None:
            if not isinstance(method, str) or method not in MERGE_METHODS:
                fail("merge policy method must be merge, squash or rebase")
            for key in MERGE_METHODS.values():
                payload[key] = key == MERGE_METHODS[method]
        return validate_request({"method": "PATCH", "path": "", "body": payload})
    except AttributeError as exc:
        raise rules.RulesetError("repository metadata must contain objects", cause="validation_error") from exc


def check_tag_shape(value: Any, *, cause: str) -> None:
    if (
        not isinstance(value, dict)
        or any(key not in value for key in rules.RULESET_FIELDS)
        or any(not isinstance(value[key], str) or not value[key] for key in ("name", "target", "enforcement"))
        or not isinstance(value["conditions"], dict)
        or any(not isinstance(value[key], list) for key in ("rules", "bypass_actors"))
    ):
        fail("invalid custom tag ruleset shape", cause)
    rule_types = set()
    for rule in value["rules"]:
        if (
            not isinstance(rule, dict)
            or not isinstance(rule.get("type"), str)
            or not rule["type"]
            or rule["type"] in rule_types
            or (rule.get("parameters") is not None and not isinstance(rule["parameters"], dict))
        ):
            fail("invalid or duplicate custom tag rule", cause)
        rule_types.add(rule["type"])
        parameters = rule.get("parameters") or {}
        if "allowed_merge_methods" in parameters and (
            not isinstance(parameters["allowed_merge_methods"], list)
            or any(not isinstance(item, str) for item in parameters["allowed_merge_methods"])
        ):
            fail("invalid merge-method parameter shape", cause)
    actors = set()
    for actor in value["bypass_actors"]:
        if (
            not isinstance(actor, dict)
            or not isinstance(actor.get("actor_type"), str)
            or not actor["actor_type"]
            or (actor.get("actor_id") is not None and (type(actor["actor_id"]) is not int or actor["actor_id"] <= 0))
            or ("bypass_mode" in actor and (not isinstance(actor["bypass_mode"], str) or not actor["bypass_mode"]))
        ):
            fail("invalid custom tag bypass actor", cause)
        identity = actor["actor_type"], actor.get("actor_id")
        if identity in actors:
            fail("duplicate custom tag bypass actor", cause)
        actors.add(identity)


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"method", "path", "body"}:
        fail("request must contain method, repository-relative path and optional body")
    method, path = value.get("method"), value.get("path")
    body = value.get("body")
    if not isinstance(path, str) or not isinstance(method, str):
        fail("request method and repository-relative path must be strings")
    if method == "PATCH" and path == "":
        if not is_archive(body) and (
            not isinstance(body, dict)
            or not body
            or set(body) - (set(SETTINGS.values()) | set(MERGE_METHODS.values()))
            or any(not isinstance(item, bool) for item in body.values())
        ):
            fail("settings PATCH accepts only declared boolean merge and branch-cleanup flags, or archived: true alone")
    elif method == "PUT" and path == FORK_APPROVAL_PATH:
        if body != {"approval_policy": "all_external_contributors"}:
            fail("fork approval PUT accepts only approval_policy: all_external_contributors")
    elif method == "PUT" and RUNNER_LABELS_PATH.fullmatch(value["path"]):
        if not isinstance(body, dict) or set(body) != {"labels"}:
            fail("runner labels PUT accepts only a labels array")
        check_label_names(value["body"]["labels"], cause="validation_error")
    elif method == "DELETE" and (
        re.fullmatch(r"/collaborators/[A-Za-z0-9][A-Za-z0-9-]{0,38}", path) or re.fullmatch(r"/invitations/[1-9][0-9]*", path)
    ):
        if body is not None:
            fail("collaborator removal accepts no body")
    elif (method == "POST" and path == "/rulesets") or (method == "PUT" and re.fullmatch(r"/rulesets/[1-9][0-9]*", path)):
        check_tag_shape(body, cause="validation_error")
        if (
            set(body) != set(rules.RULESET_FIELDS)
            or body.get("target") != "tag"
            or not body["name"].strip()
            or body["name"] in MANAGED_NAMES
        ):
            fail("custom ruleset writes need a complete tag ruleset, separate from managed rulesets")
        refs = body["conditions"].get("ref_name")
        if (
            set(body["conditions"]) != {"ref_name"}
            or not isinstance(refs, dict)
            or set(refs) != {"include", "exclude"}
            or not refs["include"]
            or any(
                not isinstance(refs[key], list) or any(not isinstance(item, str) or not item for item in refs[key])
                for key in ("include", "exclude")
            )
        ):
            fail("invalid custom tag-rule conditions, rules or bypass actors")
    else:
        fail(
            "unsupported owner-admin request; use repository settings, archive, fork approval, collaborator DELETE "
            "runner labels PUT or custom tag rulesets"
        )
    return {"method": method, "path": path, **({"body": copy.deepcopy(body)} if body is not None else {})}


def check_label_names(value: Any, *, cause: str) -> None:
    if not isinstance(value, list) or any(not isinstance(name, str) or not name.strip() for name in value):
        fail("label names must be nonempty strings", cause)
    if len({name.casefold() for name in value}) != len(value):
        fail("duplicate label names", cause)


def check_runner_snapshot(value: Any, path: str, *, cause: str) -> None:
    match = RUNNER_LABELS_PATH.fullmatch(path)
    if (
        not isinstance(value, dict)
        or set(value) != {"id", "name", "labels"}
        or not match
        or type(value["id"]) is not int
        or value["id"] != int(match[1])
        or not isinstance(value["name"], str)
        or not value["name"]
        or not isinstance(value["labels"], list)
        or any(
            not isinstance(item, dict) or set(item) != {"name", "type"} or item["type"] not in ("custom", "read-only")
            for item in value["labels"]
        )
    ):
        fail("invalid runner identity or labels", cause)
    check_label_names([item["name"] for item in value["labels"]], cause=cause)


def sorted_labels(labels: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(labels, key=lambda item: (item["type"], item["name"]))


def is_archive(body: Any) -> bool:
    return isinstance(body, dict) and set(body) == {"archived"} and body["archived"] is True


def owned_repository(client: Any, repo: str, *, allow_archived: bool = False) -> dict[str, Any]:
    owner, _ = rules.resolve_repositories(client=client, repos=[repo], all_owned=False, owner=None)
    repository = client.request("GET", f"/repos/{rules.repo_path(repo)}", operation="github.owner.admin.read")
    if (
        not isinstance(repository, dict)
        or not isinstance(repository.get("full_name"), str)
        or repository["full_name"].casefold() != repo.casefold()
        or not isinstance(repository.get("owner"), dict)
        or repository["owner"].get("type") != "User"
        or not isinstance(repository["owner"].get("login"), str)
        or repository["owner"]["login"].casefold() != owner.casefold()
        or type(repository.get("id")) is not int
        or repository["id"] <= 0
        or not isinstance(repository.get("archived"), bool)
        or (repository["archived"] and not allow_archived)
    ):
        fail("owner-admin requires an active repository owned by the verified human", "owner_actor_mismatch")
    return repository


def pages(client: Any, path: str, *, field: str | None = None) -> list[dict[str, Any]]:
    items = []
    expected_total = None
    for page in range(1, rules.MAX_PAGES + 1):
        separator = "&" if "?" in path else "?"
        body = client.request("GET", f"{path}{separator}per_page={rules.PER_PAGE}&page={page}", operation="github.owner.admin.read")
        if field and not isinstance(body, dict):
            fail("invalid paged owner-admin response", "invalid_response")
        values = body.get(field) if field else body
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            fail("invalid paged owner-admin response", "invalid_response")
        if field:
            total = body.get("total_count")
            if type(total) is not int or total < 0 or (expected_total is not None and total != expected_total):
                fail("incomplete or changing owner-admin listing", "coverage_incomplete")
            expected_total = total
        items.extend(values)
        if len(values) < rules.PER_PAGE:
            if expected_total is not None and len(items) != expected_total:
                fail("owner-admin listing does not match its total", "coverage_incomplete")
            return items
    fail("owner-admin listing reached page cap", "coverage_incomplete")


def check_fork_approval(value: Any, *, cause: str) -> None:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("approval_policy"), str)
        or value["approval_policy"] not in FORK_APPROVAL_POLICIES
    ):
        fail("invalid fork contributor approval policy response", cause)


def snapshot(client: Any, repo: str, request: dict[str, Any]) -> dict[str, Any]:
    repository = owned_repository(client, repo, allow_archived=request["method"] == "PATCH" and is_archive(request.get("body")))
    path, method = request["path"], request["method"]
    identity = {"id": repository["id"], "full_name": repository["full_name"].casefold()}
    if RUNNER_LABELS_PATH.fullmatch(path):
        runner = client.request(
            "GET", f"/repos/{rules.repo_path(repo)}{path.removesuffix('/labels')}", operation="github.owner.admin.read"
        )
        if not isinstance(runner, dict):
            fail("invalid runner response", "invalid_response")
        labels = runner.get("labels")
        if not isinstance(labels, list) or any(not isinstance(item, dict) for item in labels):
            raise rules.RulesetError("invalid runner labels response", cause="invalid_response")
        value: dict[str, Any] = {
            "id": runner.get("id"),
            "name": runner.get("name"),
            "labels": [{"name": item.get("name"), "type": item.get("type")} for item in labels],
        }
        check_runner_snapshot(value, path, cause="invalid_response")
        value["labels"] = sorted_labels(value["labels"])
        return {"repository": identity, "value": value}
    if path == FORK_APPROVAL_PATH:
        value = client.request(
            "GET",
            f"/repos/{rules.repo_path(repo)}{path}",
            operation="github.owner.admin.read",
        )
        check_fork_approval(value, cause="invalid_response")
        return {
            "repository": identity,
            "value": {"approval_policy": value["approval_policy"]},
        }
    if method == "PATCH":
        values = {key: repository.get(key) for key in request["body"]}
        if any(not isinstance(value, bool) for value in values.values()):
            fail("repository did not report all requested settings", "invalid_response")
        merged = {key: request["body"].get(key, repository.get(key)) for key in MERGE_METHODS.values()}
        if any(not isinstance(value, bool) for value in merged.values()) or not any(merged.values()):
            fail("repository must retain at least one merge method")
        return {"repository": identity, "value": values}
    if method == "DELETE":
        invitations = pages(client, f"/repos/{rules.repo_path(repo)}/invitations")
        if any(
            type(item.get("id")) is not int
            or "invitee" not in item
            or (
                item["invitee"] is not None
                and (not isinstance(item["invitee"], dict) or not isinstance(item["invitee"].get("login"), str))
            )
            for item in invitations
        ):
            fail("incomplete repository invitation response", "invalid_response")
        invitations = [
            {"id": item["id"], "login": item["invitee"]["login"].casefold() if item["invitee"] else None} for item in invitations
        ]
        if path.startswith("/invitations/"):
            invitation_id = int(path.rsplit("/", 1)[1])
            matches = [item for item in invitations if item["id"] == invitation_id]
            return {"repository": identity, "value": matches}
        login = path.rsplit("/", 1)[1]
        if login.casefold() == repo.split("/")[0].casefold():
            fail("cannot remove the repository owner")
        pending = [item["id"] for item in invitations if item["login"] in {None, login.casefold()}]
        if pending:
            fail(
                "pending invitation remains; plan and review DELETE /invitations/ID before collaborator removal",
                "pending_invitation",
                invitation_ids=pending,
            )
        collaborators = pages(client, f"/repos/{rules.repo_path(repo)}/collaborators?affiliation=direct")
        if any(not isinstance(item.get("login"), str) or type(item.get("id")) is not int for item in collaborators):
            fail("incomplete collaborator response", "invalid_response")
        matches = [item for item in collaborators if item["login"].casefold() == login.casefold()]
        if len(matches) > 1:
            fail("ambiguous collaborator response", "invalid_response")
        return {"repository": identity, "value": [{"login": item["login"].casefold(), "id": item["id"]} for item in matches]}
    app = client.request("GET", "/apps/github-actions", operation="github.owner.admin.read")
    if not isinstance(app, dict) or type(app.get("id")) is not int or app.get("slug") != "github-actions":
        fail("cannot identify GitHub Actions for personal-account ruleset validation", "invalid_response")
    if any(
        item.get("actor_type") == "Integration" and item.get("actor_id") == app["id"] for item in request["body"]["bypass_actors"]
    ):
        fail(
            "GitHub Actions integration must be part of the ruleset source or owner organization; "
            "personal-account repositories cannot exempt GitHub Actions. Use a separately authorized supported release identity "
            "or omit the blocking rule; do not create a rule that blocks the release workflow.",
            "unsupported_personal_actions_bypass",
        )
    existing = client.list_rulesets(repo)
    target_id = int(path.rsplit("/", 1)[1]) if method == "PUT" else None
    matches = (
        [item for item in existing if item.get("id") == target_id]
        if target_id
        else [item for item in existing if item.get("name") == request["body"]["name"]]
    )
    if target_id and (len(matches) != 1 or matches[0].get("target") != "tag" or matches[0].get("name") in MANAGED_NAMES):
        fail("custom tag target must exist and cannot be a managed or branch ruleset")
    if len(matches) > 1:
        fail("custom tag target name is ambiguous", "invalid_response")
    value = None
    if matches:
        if any(key not in matches[0] for key in rules.RULESET_FIELDS) or type(matches[0].get("id")) is not int:
            fail("incomplete custom tag ruleset response", "invalid_response")
        value = {key: copy.deepcopy(matches[0][key]) for key in rules.RULESET_FIELDS}
        value["id"] = matches[0]["id"]
        check_tag_shape(value, cause="invalid_response")
    return {"repository": identity, "value": value}


def build_plan(repo: str, request: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    rules.repo_path(repo)
    request = validate_request(request)
    if not isinstance(before, dict) or set(before) != {"repository", "value"}:
        fail("invalid plan snapshot")
    after = copy.deepcopy(before)
    if RUNNER_LABELS_PATH.fullmatch(request["path"]):
        check_runner_snapshot(before["value"], request["path"], cause="validation_error")
        automatic = [item for item in before["value"]["labels"] if item["type"] == "read-only"]
        after["value"]["labels"] = sorted_labels(
            automatic + [{"name": name, "type": "custom"} for name in request["body"]["labels"]]
        )
        check_runner_snapshot(after["value"], request["path"], cause="validation_error")
    elif request["path"] == FORK_APPROVAL_PATH:
        check_fork_approval(before["value"], cause="validation_error")
        if set(before["value"]) != {"approval_policy"}:
            fail("invalid fork approval snapshot")
        after["value"] = copy.deepcopy(request["body"])
    elif request["method"] == "PATCH":
        if (
            not isinstance(before["value"], dict)
            or set(before["value"]) != set(request["body"])
            or any(not isinstance(value, bool) for value in before["value"].values())
        ):
            fail("invalid settings snapshot")
        after["value"] = copy.deepcopy(request["body"])
    elif request["method"] == "DELETE":
        if not isinstance(before["value"], list):
            fail("invalid collaborator snapshot")
        after["value"] = []
    else:
        if before["value"] is not None:
            check_tag_shape(before["value"], cause="validation_error")
        if request["method"] == "POST" and before["value"] is not None:
            fail("custom ruleset name already exists; use its exact id to update")
        after["value"] = copy.deepcopy(request["body"])
        after["value"]["id"] = before["value"]["id"] if request["method"] == "PUT" else None
    return {"version": 1, "repo": repo, "request": request, "before": before, "after": after}


def plan_request(client: Any, repo: str, request: Any) -> dict[str, Any]:
    request = validate_request(request)
    return build_plan(repo, request, snapshot(client, repo, request))


def matches_snapshot(expected: dict[str, Any], live: dict[str, Any], *, generated_id: bool = False) -> bool:
    if expected["repository"] != live["repository"]:
        return False
    desired, actual = expected["value"], live["value"]
    if isinstance(desired, dict) and desired.get("target") == "tag" and isinstance(actual, dict):
        if not generated_id and desired["id"] != actual["id"]:
            return False
        spec = rules.RulesetSpec("custom_tag", desired["name"], frozenset(), desired)
        return rules.ruleset_matches(actual, spec, exact=True)
    return desired == actual


def run_plan(client: Any, plan: Any, digest: str, *, apply: bool, confirm: bool) -> dict[str, Any]:
    if apply and not confirm:
        fail("apply requires --confirm-owner-admin-write", "authorization_required")
    try:
        if not isinstance(plan, dict) or rules.check_plan_digest(plan) != digest:
            fail("reviewed plan digest mismatch", "plan_digest_mismatch")
        if build_plan(plan["repo"], plan["request"], plan["before"]) != plan:
            fail("plan does not match the change computed from its snapshot")
    except (KeyError, TypeError, ValueError) as exc:
        raise rules.RulesetError("invalid reviewed plan", cause="validation_error") from exc
    repo, request = plan["repo"], plan["request"]
    live = snapshot(client, repo, request)
    if matches_snapshot(plan["after"], live, generated_id=request["method"] == "POST"):
        return {"repo": repo, "changed": False, "verified": True, "plan_digest": digest}
    if not matches_snapshot(plan["before"], live):
        fail("repository state differs from reviewed plan; plan and review again", "plan_state_drift")
    if not apply:
        fail("reviewed change is not present", "post_write_verification_failed")
    if not matches_snapshot(live, snapshot(client, repo, request)):
        fail("repository state changed during preflight", "plan_state_drift")
    write_completed = False
    try:
        client.request(
            request["method"],
            f"/repos/{rules.repo_path(repo)}{request['path']}",
            operation="github.owner.admin.write",
            body=request.get("body"),
        )
        write_completed = True
        if not matches_snapshot(plan["after"], snapshot(client, repo, request), generated_id=request["method"] == "POST"):
            fail("owner-admin readback differs from the reviewed result", "post_write_verification_failed")
    except Exception as exc:
        cause = exc.cause if isinstance(exc, rules.RulesetError) else "invalid_response"
        details = exc.payload if isinstance(exc, rules.RulesetError) else {}
        raise rules.RulesetError(
            str(exc),
            cause=cause,
            payload={
                **details,
                "write_attempted": True,
                "failed_step": "readback" if write_completed else "write",
                "plan_digest": digest,
                "next_action": "Read back this plan before any retry; never switch identities.",
            },
        ) from exc
    return {"repo": repo, "changed": True, "verified": True, "plan_digest": digest}


def read_request(client: Any, repo: str, path: str) -> dict[str, Any]:
    if path == FORK_APPROVAL_PATH:
        current = snapshot(client, repo, {"method": "GET", "path": path})
        return {"repo": repo, **current, "write_authorized": False}
    match = re.fullmatch(r"/environments/([^/]+)/secrets", path)
    if not match:
        fail("owner read supports only fork approval and environment secret-name listings")
    name = urllib.parse.unquote(match[1])
    if not name or any(ord(char) < 32 for char in name) or urllib.parse.quote(name, safe="") != match[1]:
        fail("environment name must be canonically URL encoded")
    owned_repository(client, repo)
    values = pages(client, f"/repos/{rules.repo_path(repo)}{path}", field="secrets")
    if any(not isinstance(item.get("name"), str) or not item["name"] for item in values):
        fail("invalid environment secret-name response", "invalid_response")
    names = sorted({item["name"] for item in values})
    if len(names) != len(values):
        fail("duplicate secret names make coverage uncertain", "coverage_incomplete")
    return {"repo": repo, "environment": name, "names": names, "write_authorized": False}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = api.TerminalArgumentParser(description="Explicit verified-owner administration; no automatic identity fallback.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=api.TerminalArgumentParser)
    for command in ("plan", "settings-plan"):
        sub = commands.add_parser(command)
        sub.add_argument("--repo", required=True)
        sub.add_argument("--out", required=True, type=Path)
        sub.add_argument("--request-file" if command == "plan" else "--config", required=True, type=Path)
    for command in ("apply", "readback"):
        sub = commands.add_parser(command)
        sub.add_argument("--plan-file", required=True, type=Path)
        sub.add_argument("--plan-digest", required=True)
        if command == "apply":
            sub.add_argument("--confirm-owner-admin-write", action="store_true")
    read = commands.add_parser("read")
    read.add_argument("--repo", required=True)
    read.add_argument("--path", required=True)
    return parser.parse_args(argv)


def run(args: argparse.Namespace, *, client: Any = None) -> dict[str, Any]:
    client = client or rules.RulesetClient()
    if args.command in {"plan", "settings-plan"}:
        request = settings_request(read_json(args.config)) if args.command == "settings-plan" else read_json(args.request_file)
        plan = plan_request(client, args.repo, request)
        try:
            with args.out.open("x") as stream:
                json.dump(plan, stream, indent=2, sort_keys=True, allow_nan=False)
                stream.write("\n")
        except (OSError, ValueError) as exc:
            raise rules.RulesetError(f"cannot save plan: {exc}", cause="validation_error") from exc
        result = {
            "repo": args.repo,
            "plan_file": str(args.out),
            "plan_digest": rules.check_plan_digest(plan),
            "plan": plan,
            "write_authorized": False,
        }
    elif args.command == "read":
        result = read_request(client, args.repo, args.path)
    else:
        result = run_plan(
            client,
            read_json(args.plan_file),
            args.plan_digest,
            apply=args.command == "apply",
            confirm=getattr(args, "confirm_owner_admin_write", False),
        )
    return {"actor": client.actor, **result}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = api.requested_subcommand(argv, {"plan", "settings-plan", "read", "apply", "readback"})
    operation = f"github.owner.admin.{(command or 'unknown').replace('-', '.')}"
    try:
        payload = run(parse_args(argv))
    except (api.ArgumentParsingError, rules.RulesetError) as exc:
        cause = exc.cause if isinstance(exc, rules.RulesetError) else "validation_error"
        extra = exc.payload if isinstance(exc, rules.RulesetError) else {}
        api_result = extra.get("api_result")
        api_failure = api_result.get("failure") if isinstance(api_result, dict) else None
        reported = api_failure.get("write_outcome") if isinstance(api_failure, dict) else None
        outcome = None
        if command == "apply":
            outcome = "unknown" if extra.get("write_attempted") else "not_started"
            if reported in {"not_started", "rejected", "unknown"}:
                outcome = reported
        failure = api.FailureDetail(
            cause=cause,
            message=str(exc),
            retryable=False,
            fallback_eligible=False,
            disposition="stop",
            write_outcome=outcome,
            failed_step=extra.get("failed_step", "preflight"),
        )
        return api.emit_terminal(
            api.terminal_failure(failure, operation=operation, payload=extra, transport="rest_api", bucket="rest_core", exit_code=1)
        )
    return api.emit_terminal(
        api.terminal_success(
            payload,
            operation=operation,
            actor=payload.get("actor"),
            expected_actor=payload.get("actor"),
            transport="rest_api",
            bucket="rest_core",
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
