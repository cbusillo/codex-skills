#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Plan and apply the catalog's standard repository ruleset pair and the opt-in client lock."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, NoReturn, Protocol, Sequence

import github_api as github_api_core
import github_identity


RULESET_API_VERSION = "2026-03-10"
LANDING_RULESET_NAME = "Only the owner and automation update the default branch"
LANDING_RULESET_ALIASES = frozenset({"Only the administrator updates the default branch"})
DIRECTION_RULESET_NAME = "Code-owner review for DIRECTION.md"
CLIENT_BRANCH_LOCK_NAME = "Only the owner and automation change branches"
CLIENT_TAG_LOCK_NAME = "Only the owner and automation change tags"
ADMIN_REPOSITORY_ROLE_ID = 5
# https://api.github.com/apps/dependabot
DEPENDABOT_APP_ID = 29110
# https://api.github.com/apps/renovate
RENOVATE_APP_ID = 2740
DEFAULT_REF = "~DEFAULT_BRANCH"
ALL_REFS = "~ALL"
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
    bypass_added: tuple[dict[str, Any], ...] = ()
    bypass_removed: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "action": self.action,
            "name": self.name,
            "ruleset_id": self.ruleset_id,
        }
        if self.current_name and self.current_name != self.name:
            payload["current_name"] = self.current_name
        if self.bypass_added:
            payload["bypass_added"] = list(self.bypass_added)
        if self.bypass_removed:
            payload["bypass_removed"] = list(self.bypass_removed)
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


def configured_app_id(environ: Mapping[str, str] | None = None) -> int | None:
    try:
        config = github_identity.github_app_config(environ)
    except github_identity.GitHubAppError as exc:
        raise RulesetError(str(exc), cause="unconfigured_identity") from exc
    if config is None:
        return None
    return positive_int(config.app_id, "GitHub App id")


def _pull_request_parameters(*, code_owner_review: bool, dismiss_stale: bool) -> dict[str, Any]:
    return {
        "allowed_merge_methods": ["merge", "squash", "rebase"],
        "dismiss_stale_reviews_on_push": dismiss_stale,
        "require_code_owner_review": code_owner_review,
        # GitHub defaults this preview setting to true when omitted, which
        # makes every app-opened pull request wait for a human approval.
        "require_extra_approval_for_unattributed_changes": False,
        "require_last_push_approval": False,
        "required_approving_review_count": 0,
        "required_review_thread_resolution": False,
        "required_reviewers": [],
    }


def _owner_and_app_bypass(app_id: int | None, extra_integration_ids: Sequence[int] = ()) -> list[dict[str, Any]]:
    integration_ids: list[int] = []
    for value in ([app_id] if app_id is not None else []) + list(extra_integration_ids):
        parsed = positive_int(value, "bypass Integration id")
        if parsed not in integration_ids:
            integration_ids.append(parsed)
    return [
        {"actor_id": ADMIN_REPOSITORY_ROLE_ID, "actor_type": "RepositoryRole", "bypass_mode": "always"},
        *({"actor_id": integration_id, "actor_type": "Integration", "bypass_mode": "always"}
          for integration_id in integration_ids),
    ]


def standard_specs(app_id: int | None) -> tuple[RulesetSpec, RulesetSpec]:
    if app_id is not None:
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
            "bypass_actors": _owner_and_app_bypass(app_id),
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


def client_lock_specs(
    app_id: int | None,
    *,
    branches: bool,
    tags: bool,
    extra_integration_ids: Sequence[int] = (),
) -> tuple[RulesetSpec, ...]:
    if not branches and not tags:
        if extra_integration_ids:
            raise RulesetError(
                "--bypass-integration-id only applies with --client-lock or --client-lock-tags",
                cause="validation_error",
            )
        return ()
    if app_id is None:
        raise RulesetError(
            "the client lock needs the configured GitHub App as a bypass actor, or it would also stop "
            "the automation that pushes branches; configure the App and rerun plan",
            cause="unconfigured_identity",
            payload={"failed_step": "client_lock_app_bypass", "write_attempted": False},
        )
    app_id = positive_int(app_id, "GitHub App id")
    branch_bypass = _owner_and_app_bypass(app_id, [*extra_integration_ids, DEPENDABOT_APP_ID, RENOVATE_APP_ID])
    tag_bypass = _owner_and_app_bypass(app_id, extra_integration_ids)
    conditions = {"ref_name": {"include": [ALL_REFS], "exclude": []}}
    specs: list[RulesetSpec] = []
    selected = (
        (branches, "client_branches", CLIENT_BRANCH_LOCK_NAME, "branch", branch_bypass,
         {"type": "update", "parameters": {"update_allows_fetch_and_merge": False}}),
        (tags, "client_tags", CLIENT_TAG_LOCK_NAME, "tag", tag_bypass, {"type": "update"}),
    )
    for enabled, key, name, target, bypass, update_rule in selected:
        if not enabled:
            continue
        specs.append(RulesetSpec(
            key=key,
            name=name,
            aliases=frozenset(),
            payload={
                "name": name,
                "target": target,
                "enforcement": "active",
                "bypass_actors": copy.deepcopy(bypass),
                "conditions": copy.deepcopy(conditions),
                "rules": [{"type": "creation"}, update_rule, {"type": "deletion"}],
            },
        ))
    return tuple(specs)


def required_ruleset_names() -> tuple[str, str]:
    return LANDING_RULESET_NAME, DIRECTION_RULESET_NAME


def _normalized_actor(actor: Mapping[str, Any]) -> tuple[str, int | None, str]:
    actor_type = str(actor.get("actor_type") or "")
    actor_id = actor.get("actor_id")
    return actor_type, int(actor_id) if actor_id is not None else None, str(actor.get("bypass_mode") or "always")


def _actor_order(actor: tuple[str, int | None, str]) -> tuple[str, int, str]:
    return actor[0], actor[1] or 0, actor[2]


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
        "require_extra_approval_for_unattributed_changes": True,
        "require_last_push_approval": False,
        "required_approving_review_count": 0,
        "required_review_thread_resolution": False,
        "required_reviewers": [],
        "update_allows_fetch_and_merge": False,
        "negate": False,
    }
    for key, value in desired.items():
        observed = actual.get(key, omitted_defaults.get(key))
        if key == "allowed_merge_methods":
            if sorted(observed or []) != sorted(value):
                return False
        elif observed != value:
            return False
    return True


def ruleset_matches(ruleset: Mapping[str, Any], spec: RulesetSpec, *, exact: bool = False) -> bool:
    if ruleset.get("name") != spec.name:
        return False
    for key in ("target", "enforcement"):
        if ruleset.get(key) != spec.payload[key]:
            return False
    if ruleset.get("conditions") != spec.payload["conditions"]:
        return False
    actual_actors = sorted((_normalized_actor(actor) for actor in ruleset.get("bypass_actors") or []), key=_actor_order)
    desired_actors = sorted((_normalized_actor(actor) for actor in spec.payload.get("bypass_actors") or []), key=_actor_order)
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
        if exact and not _parameters_match(desired_parameters, actual_parameters):
            return False
    return True


def _actor_dicts(actors: Iterable[tuple[str, int | None, str]]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {"actor_type": actor_type, "actor_id": actor_id, "bypass_mode": bypass_mode}
        for actor_type, actor_id, bypass_mode in sorted(actors, key=_actor_order)
    )


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
        current_apps = [actor.get("actor_id") for actor in current.get("bypass_actors") or []
                        if actor.get("actor_type") == "Integration"]
        desired_apps = [actor for actor in spec.payload.get("bypass_actors") or []
                        if actor.get("actor_type") == "Integration"]
        if spec.key == "landing" and current_apps and not desired_apps:
            raise RulesetError(
                "the landing ruleset already has an App bypass; restore the GitHub App configuration "
                "and rerun plan before applying, so the existing bypass is not removed",
                cause="unconfigured_identity",
                payload={"key": spec.key, "ruleset_id": ruleset_id, "existing_app_ids": current_apps,
                         "failed_step": "existing_app_bypass", "write_attempted": False},
            )
        action = "none" if ruleset_matches(current, spec) else "update"
        current_actors = {_normalized_actor(actor) for actor in current.get("bypass_actors") or []}
        desired_actors = {_normalized_actor(actor) for actor in spec.payload.get("bypass_actors") or []}
        changes.append(
            RulesetChange(
                spec.key,
                action,
                spec.name,
                ruleset_id,
                current_name=str(current.get("name") or ""),
                bypass_added=_actor_dicts(desired_actors - current_actors),
                bypass_removed=_actor_dicts(current_actors - desired_actors),
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

    def request(self, method: str, path: str, *, operation: str, body: Any = None,
                allow_unprotected: bool = False) -> Any:
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
        if (allow_unprotected and result.status == 404 and isinstance(result.body, dict)
                and result.body.get("message") == "Branch not protected"):
            return None
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
        summaries = []
        for page in range(1, MAX_PAGES + 1):
            path = (f"/repos/{repo_path(repo)}/rulesets?includes_parents=false&targets=branch,tag"
                    f"&per_page={PER_PAGE}&page={page}")
            body = self.request("GET", path, operation="github.rulesets.list")
            if not isinstance(body, list) or any(not isinstance(item, dict) for item in body):
                raise RulesetError("ruleset listing returned an invalid response", cause="invalid_response")
            summaries.extend(body)
            if len(body) < PER_PAGE:
                break
        else:
            raise RulesetError("ruleset listing hit the page cap", cause="coverage_incomplete")
        details: list[dict[str, Any]] = []
        for summary in summaries:
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

    def request(self, method: str, path: str, *, operation: str, body: Any = None,
                allow_unprotected: bool = False) -> Any: ...

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
    try:
        rulesets = client.list_rulesets(repo)
        changes = plan_changes(rulesets, specs)
    except RulesetError as exc:
        raise RulesetError(str(exc), cause=exc.cause,
                           payload={**exc.payload, "failed_repository": repo}) from exc
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


# The additive status-check route deliberately shares RulesetClient and the
# verified-owner resolver above. It never applies the standard-pair specs.
CHECK_RULESET_NAME = "Required status checks on the default branch"
RULESET_FIELDS = ("name", "target", "enforcement", "bypass_actors", "conditions", "rules")


def _check_error(message: str, cause: str = "validation_error") -> NoReturn:
    raise RulesetError(message, cause=cause)


def normalize_checks(value: Any, *, binding: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _check_error("checks must be a list")
    result = []
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"context", binding}
                or not isinstance(item["context"], str) or not item["context"].strip()
                or isinstance(item[binding], bool)
                or not (item[binding] is None or isinstance(item[binding], int))
                or (item[binding] is not None and item[binding] <= 0)):
            _check_error(f"each check needs a nonempty context and explicit {binding} (positive id or null)")
        if item not in result:
            if any(existing["context"] == item["context"] for existing in result):
                _check_error("a check context cannot have conflicting App bindings")
            result.append(copy.deepcopy(item))
    return sorted(result, key=lambda item: (item["context"], item[binding] or 0))


def _classic_checks(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("strict"), bool):
        _check_error("classic checks lack strictness", "invalid_response")
    # Do not infer App bindings from the legacy contexts list.
    checks = normalize_checks(value.get("checks"), binding="app_id")
    contexts = value.get("contexts")
    if (not isinstance(contexts, list) or not all(isinstance(item, str) for item in contexts)
            or set(contexts) != {item["context"] for item in checks}):
        _check_error("classic contexts and App-bound checks disagree", "invalid_response")
    return {**value, "checks": checks, "contexts": sorted(set(value["contexts"]))}


def check_snapshot(client: RulesetClientProtocol, repo: str, target: str) -> dict[str, Any]:
    path = f"/repos/{repo_path(repo)}"
    repository = client.request("GET", path, operation="github.rulesets.checks.read")
    # Validate metadata before consuming the default branch to fetch protection.
    _check_repository(repository, repo)
    branch = repository["default_branch"]
    protection = client.request(
        "GET", f"{path}/branches/{urllib.parse.quote(branch, safe='')}/protection",
        operation="github.rulesets.checks.read", allow_unprotected=True,
    )
    return normalize_check_snapshot({"repository": repository, "protection": protection,
                                     "rulesets": client.list_rulesets(repo)}, repo, target,
                                    cause="invalid_response")


def _check_repository(repository: Any, repo: str) -> None:
    if (not isinstance(repository, dict) or not isinstance(repository.get("id"), int)
            or isinstance(repository["id"], bool) or repository["id"] <= 0
            or not isinstance(repository.get("owner"), dict)
            or not all(isinstance(repository["owner"].get(key), str) and repository["owner"][key]
                       for key in ("login", "type"))
            or not all(isinstance(repository.get(key), str) and repository[key]
                       for key in ("default_branch", "full_name"))
            or not isinstance(repository.get("archived"), bool)):
        _check_error("invalid required-check repository metadata", "invalid_response")
    owner = repository["owner"]
    if (owner["type"] != "User" or owner["login"].casefold() != repo.split("/")[0].casefold()
            or repository["archived"] or repository["full_name"].casefold() != repo.casefold()):
        _check_error("required-check plans need an active repository owned by the verified human", "owner_actor_mismatch")


def normalize_check_snapshot(value: Any, repo: str, target: str, *, cause: str) -> dict[str, Any]:
    """One structural contract for live responses and the reviewed before image."""
    try:
        return _normalize_check_snapshot(value, repo, target)
    except RulesetError as exc:
        if exc.cause == "owner_actor_mismatch":
            raise
        raise RulesetError(str(exc), cause=cause) from exc
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RulesetError("invalid required-check snapshot structure", cause=cause) from exc


def _normalize_check_snapshot(value: Any, repo: str, target: str) -> dict[str, Any]:
    target_id = None if target in {"classic", "new"} else positive_int(target, "target ruleset id")
    if not isinstance(value, dict) or set(value) != {"repository", "protection", "rulesets"}:
        _check_error("invalid required-check snapshot fields")
    repository = value["repository"]
    _check_repository(repository, repo)
    protection = copy.deepcopy(value["protection"])
    if protection is not None:
        if not isinstance(protection, dict):
            _check_error("invalid branch protection response", "invalid_response")
        protection = copy.deepcopy(protection)
        if protection.get("required_status_checks") is not None:
            protection["required_status_checks"] = _classic_checks(protection["required_status_checks"])
    rulesets = []
    if not isinstance(value["rulesets"], list):
        _check_error("rulesets must be a list")
    for item in value["rulesets"]:
        if not isinstance(item, dict) or not all(key in item for key in RULESET_FIELDS):
            _check_error("incomplete ruleset detail, including bypass actors", "invalid_response")
        if (not all(isinstance(item[key], str) and item[key] for key in ("name", "target", "enforcement"))
                or not isinstance(item["bypass_actors"], list) or not isinstance(item["rules"], list)):
            _check_error("invalid ruleset fields")
        normalized = {"id": positive_int(item.get("id"), "ruleset id"),
                      **{key: copy.deepcopy(item[key]) for key in RULESET_FIELDS}}
        if normalized["id"] == target_id:
            conditions = normalized["conditions"]
            refs = conditions.get("ref_name") if isinstance(conditions, dict) else None
            if (not isinstance(refs, dict) or not all(isinstance(refs.get(key), list)
                    and all(isinstance(ref, str) for ref in refs[key]) for key in ("include", "exclude"))):
                _check_error("selected ruleset needs ref_name include and exclude string lists")
        for rule in normalized["rules"]:
            if not isinstance(rule, dict) or not isinstance(rule.get("type"), str) or not rule["type"]:
                _check_error("each ruleset rule needs a nonempty string type")
            if rule["type"] == "required_status_checks":
                parameters = rule.get("parameters")
                if (not isinstance(parameters, dict)
                        or not isinstance(parameters.get("required_status_checks"), list)
                        or not isinstance(parameters.get("strict_required_status_checks_policy"), bool)):
                    _check_error("invalid required-status-check parameters")
                for check in parameters["required_status_checks"]:
                    if not isinstance(check, dict):
                        _check_error("each required status check must be an object")
                parameters["required_status_checks"] = normalize_checks([
                    {**check, "integration_id": check.get("integration_id")}
                    for check in parameters["required_status_checks"]], binding="integration_id")
        rulesets.append(normalized)
    if len({item["id"] for item in rulesets}) != len(rulesets):
        _check_error("duplicate ruleset ids")
    return {
        "repository": {key: repository[key] for key in ("id", "full_name", "default_branch", "archived", "owner")},
        "protection": protection,
        "rulesets": sorted(rulesets, key=lambda item: item["id"]),
    }


def check_plan_digest(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(plan), sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def build_check_plan(repo: str, target: str, checks: Any, before: dict[str, Any]) -> dict[str, Any]:
    repo_path(repo)
    if not isinstance(target, str):
        _check_error("target must be classic, new or a ruleset id")
    before = normalize_check_snapshot(before, repo, target, cause="validation_error")
    additions = normalize_checks(checks, binding="app_id")
    if not additions:
        _check_error("at least one required check is needed")
    after = copy.deepcopy(before)
    added = []
    if target == "classic":
        protection = after.get("protection")
        if not isinstance(protection, dict) or not isinstance(protection.get("required_status_checks"), dict):
            _check_error("classic target needs existing status-check protection; use target new for an isolated ruleset")
        current = _classic_checks(protection["required_status_checks"])
        for item in additions:
            if item not in current["checks"]:
                current["checks"].append(copy.deepcopy(item))
                added.append(item)
        current["checks"] = normalize_checks(current["checks"], binding="app_id")
        current["contexts"] = sorted({item["context"] for item in current["checks"]})
        protection["required_status_checks"] = current
        payload = {"strict": current["strict"], "checks": [
            {**item, "app_id": item["app_id"] if item["app_id"] is not None else -1}
            for item in current["checks"]]}
    else:
        if target == "new":
            if any(item["name"] == CHECK_RULESET_NAME for item in before["rulesets"]):
                _check_error("required-check ruleset already exists; select its id")
            current = {
                "id": None, "name": CHECK_RULESET_NAME, "target": "branch", "enforcement": "active",
                "bypass_actors": [], "conditions": {"ref_name": {"include": [DEFAULT_REF], "exclude": []}},
                "rules": [{"type": "required_status_checks", "parameters": {
                    "strict_required_status_checks_policy": False, "do_not_enforce_on_create": False,
                    "required_status_checks": []}}],
            }
            after["rulesets"].append(current)
        else:
            ruleset_id = positive_int(target, "target ruleset id")
            matches = [item for item in after["rulesets"] if item["id"] == ruleset_id]
            if len(matches) != 1:
                _check_error("target ruleset not found")
            current = matches[0]
            managed_names = {LANDING_RULESET_NAME, DIRECTION_RULESET_NAME, CLIENT_BRANCH_LOCK_NAME,
                             CLIENT_TAG_LOCK_NAME, *LANDING_RULESET_ALIASES}
            if current["name"] in managed_names:
                _check_error("standard and client-lock rulesets are replaced by standard apply; use --target new",
                             "managed_ruleset_target")
            refs = current["conditions"].get("ref_name", {})
            if (current["target"] != "branch" or current["enforcement"] != "active"
                    or refs.get("exclude") != []
                    or refs.get("include") not in ([DEFAULT_REF], [f"refs/heads/{before['repository']['default_branch']}"])):
                _check_error("target must be an active ruleset covering exactly the default branch")
        rules = [rule for rule in current["rules"] if rule["type"] == "required_status_checks"]
        if len(rules) != 1:
            _check_error("target needs exactly one existing required-status-check rule; use target new")
        parameters = rules[0]["parameters"]
        if not isinstance(parameters.get("strict_required_status_checks_policy"), bool):
            _check_error("ruleset lacks status-check strictness")
        existing = normalize_checks(parameters.get("required_status_checks"), binding="integration_id")
        for item in additions:
            translated = {"context": item["context"], "integration_id": item["app_id"]}
            if translated not in existing:
                existing.append(translated)
                added.append(item)
        parameters["required_status_checks"] = normalize_checks(existing, binding="integration_id")
        payload = {key: copy.deepcopy(current[key]) for key in RULESET_FIELDS}
        # The ruleset write schema accepts an optional integer integration_id;
        # omission means any App. Keep null only in our normalized snapshot.
        for rule in payload["rules"]:
            if rule["type"] == "required_status_checks":
                for check in rule["parameters"]["required_status_checks"]:
                    if check["integration_id"] is None:
                        del check["integration_id"]
    return {"version": 1, "repo": repo, "target": target, "checks": additions,
            "before": before, "after": after, "payload": payload, "added": added,
            "changed": bool(added)}


def validate_check_plan(plan: Any, digest: str) -> dict[str, Any]:
    try:
        if not isinstance(plan, dict) or check_plan_digest(plan) != digest:
            _check_error("reviewed plan digest mismatch", "plan_digest_mismatch")
        rebuilt = build_check_plan(plan["repo"], plan["target"], plan["checks"], plan["before"])
        if rebuilt != plan:
            _check_error("reviewed plan is not the additive change computed from its snapshot")
        return rebuilt
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RulesetError("invalid reviewed check plan", cause="validation_error") from exc


def _matches_check_after(plan: dict[str, Any], live: dict[str, Any]) -> bool:
    observed = copy.deepcopy(live)
    if plan["target"] == "new":
        matches = [item for item in observed["rulesets"] if item["name"] == CHECK_RULESET_NAME]
        if len(matches) != 1:
            return False
        created = matches[0]
        observed["rulesets"].remove(created)
        created["id"] = None
        observed["rulesets"].append(created)
    return observed == plan["after"]


def run_check_plan(client: RulesetClientProtocol, plan: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    repo = plan["repo"]
    live = check_snapshot(client, repo, plan["target"])
    if _matches_check_after(plan, live):
        return {"repo": repo, "changed": False, "verified": True, "applied": []}
    if live != plan["before"]:
        _check_error("current protection or rulesets differ from the reviewed plan; plan and review again", "plan_state_drift")
    if not apply:
        _check_error("reviewed change is not present", "post_write_verification_failed")
    attempted = False
    try:
        # Fresh comparison immediately before the single mutation. GitHub exposes
        # no conditional-write token for these endpoints; the final API race is
        # reported as a limit, never described as a server-side transaction.
        if check_snapshot(client, repo, plan["target"]) != live:
            _check_error("protection changed during preflight", "plan_state_drift")
        attempted = True
        if plan["target"] == "classic":
            branch = urllib.parse.quote(live["repository"]["default_branch"], safe="")
            client.request("PATCH", f"/repos/{repo_path(repo)}/branches/{branch}/protection/required_status_checks",
                           operation="github.rulesets.checks.update", body=plan["payload"])
        else:
            spec = RulesetSpec("required_checks", plan["payload"]["name"], frozenset(), plan["payload"])
            if plan["target"] == "new":
                client.create_ruleset(repo, spec)
            else:
                client.update_ruleset(repo, positive_int(plan["target"], "ruleset id"), spec)
        if not _matches_check_after(plan, check_snapshot(client, repo, plan["target"])):
            _check_error("required-check readback differs from the reviewed result", "post_write_verification_failed")
    except Exception as exc:
        cause = exc.cause if isinstance(exc, RulesetError) else "invalid_response"
        details = exc.payload if isinstance(exc, RulesetError) else {}
        raise RulesetError(str(exc), cause=cause, payload={**details, "repo": repo,
                           "failed_step": "check_write_or_readback" if attempted else "preflight",
                           "write_attempted": attempted}) from exc
    return {"repo": repo, "changed": True, "verified": True, "applied": plan["added"]}
