#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Plan and apply the catalog's standard repository ruleset pair."""

from __future__ import annotations

import os
import re
import shutil
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

import github_api as github_api_core
import github_identity


RULESET_API_VERSION = "2026-03-10"
LANDING_RULESET_NAME = "Only the owner and automation update the default branch"
LANDING_RULESET_ALIASES = frozenset({"Only the administrator updates the default branch"})
DIRECTION_RULESET_NAME = "Code-owner review for DIRECTION.md"
ADMIN_REPOSITORY_ROLE_ID = 5
DEFAULT_REF = "~DEFAULT_BRANCH"
PER_PAGE = 100
MAX_PAGES = 100
ACTIVE_GH = os.environ.get("GH_RULESETS_ACTIVE_GH") or shutil.which("gh") or "gh"
ENV_COMMAND = shutil.which("env") or "env"


class RulesetError(Exception):
    def __init__(
        self,
        message: str,
        *,
        cause: str = "ruleset_error",
        payload: Mapping[str, Any] | None = None,
    ):
        super().__init__(github_api_core.redact_string(message))
        self.cause = cause
        self.payload = github_api_core.redact_body(dict(payload or {}))


@dataclass(frozen=True)
class RulesetSpec:
    key: str
    name: str
    aliases: frozenset[str]
    payload: dict[str, Any]


@dataclass(frozen=True)
class RulesetChange:
    key: str
    action: str
    name: str
    ruleset_id: int | None
    current_name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "action": self.action,
            "name": self.name,
            "ruleset_id": self.ruleset_id,
        }
        if self.current_name and self.current_name != self.name:
            payload["current_name"] = self.current_name
        return payload


def repo_path(repo: str) -> str:
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo or ""):
        raise RulesetError(f"invalid repository: {repo!r}", cause="validation_error")
    return "/".join(urllib.parse.quote(part, safe="") for part in repo.split("/", 1))


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise RulesetError(f"{label} must be a positive integer", cause="validation_error")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RulesetError(f"{label} must be a positive integer", cause="validation_error") from exc
    if parsed <= 0:
        raise RulesetError(f"{label} must be a positive integer", cause="validation_error")
    return parsed


def configured_app_id(environ: Mapping[str, str] | None = None) -> int:
    try:
        config = github_identity.github_app_config(environ)
    except github_identity.GitHubAppError as exc:
        raise RulesetError(str(exc), cause="unconfigured_identity") from exc
    if config is None:
        raise RulesetError(
            "standard rulesets require configured GitHub App authentication",
            cause="unconfigured_identity",
        )
    return positive_int(config.app_id, "GitHub App id")


def _pull_request_parameters(*, code_owner_review: bool, dismiss_stale: bool) -> dict[str, Any]:
    return {
        "allowed_merge_methods": ["merge", "squash", "rebase"],
        "dismiss_stale_reviews_on_push": dismiss_stale,
        "require_code_owner_review": code_owner_review,
        "require_last_push_approval": False,
        "required_approving_review_count": 0,
        "required_review_thread_resolution": False,
        "required_reviewers": [],
    }


def standard_specs(app_id: int) -> tuple[RulesetSpec, RulesetSpec]:
    app_id = positive_int(app_id, "GitHub App id")
    conditions = {"ref_name": {"include": [DEFAULT_REF], "exclude": []}}
    landing = RulesetSpec(
        key="landing",
        name=LANDING_RULESET_NAME,
        aliases=LANDING_RULESET_ALIASES,
        payload={
            "name": LANDING_RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [
                {
                    "actor_id": ADMIN_REPOSITORY_ROLE_ID,
                    "actor_type": "RepositoryRole",
                    "bypass_mode": "always",
                },
                {"actor_id": app_id, "actor_type": "Integration", "bypass_mode": "always"},
            ],
            "conditions": conditions,
            "rules": [
                {"type": "update", "parameters": {"update_allows_fetch_and_merge": False}},
                {
                    "type": "pull_request",
                    "parameters": _pull_request_parameters(
                        code_owner_review=False,
                        dismiss_stale=False,
                    ),
                },
            ],
        },
    )
    direction = RulesetSpec(
        key="direction",
        name=DIRECTION_RULESET_NAME,
        aliases=frozenset(),
        payload={
            "name": DIRECTION_RULESET_NAME,
            "target": "branch",
            "enforcement": "active",
            "bypass_actors": [],
            "conditions": conditions,
            "rules": [
                {
                    "type": "pull_request",
                    "parameters": _pull_request_parameters(
                        code_owner_review=True,
                        dismiss_stale=True,
                    ),
                },
            ],
        },
    )
    return landing, direction


def required_ruleset_names() -> tuple[str, str]:
    return LANDING_RULESET_NAME, DIRECTION_RULESET_NAME


def _normalized_actor(actor: Mapping[str, Any]) -> tuple[str, int | None, str]:
    actor_type = str(actor.get("actor_type") or "")
    actor_id = actor.get("actor_id")
    return actor_type, int(actor_id) if actor_id is not None else None, str(actor.get("bypass_mode") or "always")


def _rules_by_type(ruleset: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for rule in ruleset.get("rules") or []:
        if isinstance(rule, Mapping) and isinstance(rule.get("type"), str):
            result[str(rule["type"])] = rule
    return result


def _parameters_match(actual: Mapping[str, Any], desired: Mapping[str, Any]) -> bool:
    omitted_defaults: dict[str, Any] = {
        "allowed_merge_methods": ["merge", "squash", "rebase"],
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "require_last_push_approval": False,
        "required_approving_review_count": 0,
        "required_review_thread_resolution": False,
        "required_reviewers": [],
        "update_allows_fetch_and_merge": False,
    }
    for key, value in desired.items():
        observed = actual.get(key, omitted_defaults.get(key))
        if key == "allowed_merge_methods":
            if sorted(observed or []) != sorted(value):
                return False
        elif observed != value:
            return False
    return True


def ruleset_matches(ruleset: Mapping[str, Any], spec: RulesetSpec) -> bool:
    if ruleset.get("name") != spec.name:
        return False
    for key in ("target", "enforcement"):
        if ruleset.get(key) != spec.payload[key]:
            return False
    if ruleset.get("conditions") != spec.payload["conditions"]:
        return False
    actual_actors = sorted(_normalized_actor(actor) for actor in ruleset.get("bypass_actors") or [])
    desired_actors = sorted(_normalized_actor(actor) for actor in spec.payload.get("bypass_actors") or [])
    if actual_actors != desired_actors:
        return False
    actual_rules = _rules_by_type(ruleset)
    desired_rules = _rules_by_type(spec.payload)
    if set(actual_rules) != set(desired_rules):
        return False
    for rule_type, desired_rule in desired_rules.items():
        actual_parameters = dict(actual_rules[rule_type].get("parameters") or {})
        desired_parameters = dict(desired_rule.get("parameters") or {})
        if not _parameters_match(actual_parameters, desired_parameters):
            return False
    return True


def plan_changes(
    rulesets: Sequence[Mapping[str, Any]],
    specs: Sequence[RulesetSpec],
) -> list[RulesetChange]:
    changes: list[RulesetChange] = []
    for spec in specs:
        names = {spec.name, *spec.aliases}
        matches = [item for item in rulesets if item.get("name") in names]
        if len(matches) > 1:
            raise RulesetError(
                f"multiple managed candidates found for {spec.key} ruleset",
                cause="ambiguous_ruleset",
                payload={"key": spec.key, "candidate_ids": [item.get("id") for item in matches]},
            )
        if not matches:
            changes.append(RulesetChange(spec.key, "create", spec.name, None))
            continue
        current = matches[0]
        ruleset_id = positive_int(current.get("id"), f"{spec.key} ruleset id")
        action = "none" if ruleset_matches(current, spec) else "update"
        changes.append(
            RulesetChange(
                spec.key,
                action,
                spec.name,
                ruleset_id,
                current_name=str(current.get("name") or ""),
            )
        )
    return changes


def missing_standard_rulesets(rulesets: Sequence[Mapping[str, Any]]) -> list[str]:
    active_branch_names = {
        str(item.get("name") or "")
        for item in rulesets
        if item.get("target") == "branch" and item.get("enforcement") == "active"
    }
    return [name for name in required_ruleset_names() if name not in active_branch_names]


class RulesetClient:
    """REST client that deliberately uses the active human GitHub identity."""

    def __init__(
        self,
        *,
        active_gh: str = ACTIVE_GH,
        env_command: str = ENV_COMMAND,
        call: Callable[..., github_api_core.ApiResult] = github_api_core.call_gh_with_retry,
    ) -> None:
        self.active_gh = active_gh
        self.env_command = env_command
        self.call = call
        self.actor: str | None = None

    @property
    def gh_prefix_args(self) -> list[str]:
        return [
            "-u",
            "GH_TOKEN",
            "-u",
            "GITHUB_TOKEN",
            "-u",
            "CODEX_GITHUB_TOKEN",
            "-u",
            "GH_ENTERPRISE_TOKEN",
            "-u",
            "GITHUB_ENTERPRISE_TOKEN",
            self.active_gh,
        ]

    def request(self, method: str, path: str, *, operation: str, body: Any = None) -> Any:
        result = self.call(
            method,
            path,
            body,
            gh_cmd=self.env_command,
            gh_prefix_args=self.gh_prefix_args,
            api_version=RULESET_API_VERSION,
            operation=operation,
            actor=self.actor,
            expected_actor=self.actor,
            bucket="rest_core",
        )
        if not result.ok:
            failure = result.failure
            raise RulesetError(
                failure.message if failure else f"GitHub request failed with status {result.status}",
                cause=failure.cause if failure else "github_request_failed",
                payload={"api_result": _public_api_result(result)},
            )
        return result.body

    def resolve_owner(self, owner: str) -> str:
        body = self.request("GET", "/user", operation="github.rulesets.actor.read")
        login = str(body.get("login") if isinstance(body, Mapping) else "")
        if not login:
            raise RulesetError("active GitHub identity response is missing login", cause="invalid_actor_response")
        if login.casefold() != owner.casefold():
            raise RulesetError(
                f"active GitHub identity '{login}' is not repository owner '{owner}'",
                cause="owner_actor_mismatch",
            )
        self.actor = login
        return login

    def list_owned_repositories(self, owner: str) -> list[str]:
        repos: list[str] = []
        for page in range(1, MAX_PAGES + 1):
            path = f"/user/repos?affiliation=owner&per_page={PER_PAGE}&page={page}&sort=full_name"
            body = self.request("GET", path, operation="github.rulesets.repository.list")
            if not isinstance(body, list):
                raise RulesetError("owned repository listing returned an invalid response", cause="invalid_response")
            for item in body:
                if not isinstance(item, Mapping):
                    continue
                full_name = str(item.get("full_name") or "")
                owner_payload = item.get("owner")
                item_owner = str(owner_payload.get("login") or "") if isinstance(owner_payload, Mapping) else ""
                if item_owner.casefold() == owner.casefold() and full_name and not item.get("archived"):
                    repos.append(full_name)
            if len(body) < PER_PAGE:
                return sorted(set(repos), key=str.casefold)
        raise RulesetError("owned repository listing hit the page cap", cause="coverage_incomplete")

    def list_rulesets(self, repo: str) -> list[dict[str, Any]]:
        path = f"/repos/{repo_path(repo)}/rulesets?includes_parents=false&targets=branch&per_page={PER_PAGE}"
        body = self.request("GET", path, operation="github.rulesets.list")
        if not isinstance(body, list):
            raise RulesetError("ruleset listing returned an invalid response", cause="invalid_response")
        details: list[dict[str, Any]] = []
        for summary in body:
            if not isinstance(summary, Mapping):
                continue
            ruleset_id = positive_int(summary.get("id"), "ruleset id")
            detail = self.request(
                "GET",
                f"/repos/{repo_path(repo)}/rulesets/{ruleset_id}?includes_parents=false",
                operation="github.rulesets.detail",
            )
            if not isinstance(detail, dict):
                raise RulesetError("ruleset detail returned an invalid response", cause="invalid_response")
            details.append(detail)
        return details

    def create_ruleset(self, repo: str, spec: RulesetSpec) -> dict[str, Any]:
        body = self.request(
            "POST",
            f"/repos/{repo_path(repo)}/rulesets",
            operation="github.rulesets.create",
            body=spec.payload,
        )
        if not isinstance(body, dict):
            raise RulesetError("ruleset create returned an invalid response", cause="invalid_response")
        return body

    def update_ruleset(self, repo: str, ruleset_id: int, spec: RulesetSpec) -> dict[str, Any]:
        body = self.request(
            "PUT",
            f"/repos/{repo_path(repo)}/rulesets/{positive_int(ruleset_id, 'ruleset id')}",
            operation="github.rulesets.update",
            body=spec.payload,
        )
        if not isinstance(body, dict):
            raise RulesetError("ruleset update returned an invalid response", cause="invalid_response")
        return body


class RulesetClientProtocol(Protocol):
    actor: str | None

    def resolve_owner(self, owner: str) -> str: ...

    def list_owned_repositories(self, owner: str) -> list[str]: ...

    def list_rulesets(self, repo: str) -> list[dict[str, Any]]: ...

    def create_ruleset(self, repo: str, spec: RulesetSpec) -> dict[str, Any]: ...

    def update_ruleset(self, repo: str, ruleset_id: int, spec: RulesetSpec) -> dict[str, Any]: ...


def _public_api_result(result: github_api_core.ApiResult) -> dict[str, Any]:
    payload = result.as_dict()
    payload.pop("body", None)
    return payload


def resolve_repositories(
    *,
    client: RulesetClientProtocol,
    repos: Iterable[str],
    all_owned: bool,
    owner: str | None,
) -> tuple[str, list[str]]:
    requested = [repo_path(repo).replace("%2F", "/") for repo in repos]
    owners = {repo.split("/", 1)[0].casefold(): repo.split("/", 1)[0] for repo in requested}
    if all_owned:
        if not owner:
            raise RulesetError("--all-owned requires --owner", cause="validation_error")
        owners[owner.casefold()] = owner
    if len(owners) != 1:
        raise RulesetError("all repositories must have the same owner", cause="validation_error")
    resolved_owner = next(iter(owners.values()))
    client.resolve_owner(resolved_owner)
    if all_owned:
        requested.extend(client.list_owned_repositories(resolved_owner))
    repositories = sorted(set(requested), key=str.casefold)
    if not repositories:
        raise RulesetError("at least one --repo or --all-owned is required", cause="validation_error")
    return resolved_owner, repositories


def plan_repository(
    client: RulesetClientProtocol,
    repo: str,
    specs: Sequence[RulesetSpec],
) -> dict[str, Any]:
    rulesets = client.list_rulesets(repo)
    changes = plan_changes(rulesets, specs)
    return {
        "repo": repo,
        "changed": any(change.action != "none" for change in changes),
        "changes": [change.as_dict() for change in changes],
    }


def apply_repository(
    client: RulesetClientProtocol,
    repo: str,
    specs: Sequence[RulesetSpec],
) -> dict[str, Any]:
    before = client.list_rulesets(repo)
    changes = plan_changes(before, specs)
    applied: list[dict[str, Any]] = []
    by_key = {spec.key: spec for spec in specs}
    for change in changes:
        spec = by_key[change.key]
        try:
            if change.action == "create":
                created = client.create_ruleset(repo, spec)
                applied.append({**change.as_dict(), "ruleset_id": created.get("id")})
            elif change.action == "update":
                ruleset_id = positive_int(change.ruleset_id, "ruleset id")
                updated = client.update_ruleset(repo, ruleset_id, spec)
                applied.append({**change.as_dict(), "ruleset_id": updated.get("id", ruleset_id)})
        except RulesetError as exc:
            raise RulesetError(
                str(exc),
                cause=exc.cause,
                payload={
                    **exc.payload,
                    "repo": repo,
                    "applied": applied,
                    "failed_change": change.as_dict(),
                    "failed_step": f"{change.action}_{change.key}",
                    "write_attempted": change.action in {"create", "update"},
                },
            ) from exc
    try:
        after = client.list_rulesets(repo)
    except RulesetError as exc:
        raise RulesetError(
            str(exc),
            cause=exc.cause,
            payload={
                **exc.payload,
                "repo": repo,
                "applied": applied,
                "failed_step": "post_write_read",
                "write_attempted": bool(applied),
            },
        ) from exc
    remaining = plan_changes(after, specs)
    drift = [change.as_dict() for change in remaining if change.action != "none"]
    if drift:
        raise RulesetError(
            f"ruleset verification failed after applying {repo}",
            cause="post_write_verification_failed",
            payload={
                "repo": repo,
                "applied": applied,
                "remaining": drift,
                "failed_step": "post_write_verification",
                "write_attempted": bool(applied),
            },
        )
    return {"repo": repo, "changed": bool(applied), "applied": applied, "verified": True}
