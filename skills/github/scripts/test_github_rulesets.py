#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Deterministic tests for standard GitHub ruleset planning and apply guards."""

from __future__ import annotations

import argparse
import copy
import importlib.util
from pathlib import Path
from typing import Any

import github_rulesets

CLI_PATH = Path(__file__).with_name("gh-rulesets.py")


def load_cli() -> Any:
    spec = importlib.util.spec_from_file_location("gh_rulesets_under_test", CLI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response_ruleset(spec: github_rulesets.RulesetSpec, ruleset_id: int) -> dict[str, Any]:
    result = copy.deepcopy(spec.payload)
    result["id"] = ruleset_id
    return result


class FakeClient:
    def __init__(self, repos: dict[str, list[dict[str, Any]]], *, owner: str = "owner") -> None:
        self.repos = copy.deepcopy(repos)
        self.owner = owner
        self.actor: str | None = None
        self.writes: list[tuple[str, str, int | None]] = []

    def resolve_owner(self, owner: str) -> str:
        if owner.casefold() != self.owner.casefold():
            raise github_rulesets.RulesetError("owner mismatch", cause="owner_actor_mismatch")
        self.actor = self.owner
        return self.owner

    def list_owned_repositories(self, owner: str) -> list[str]:
        self.resolve_owner(owner)
        return sorted(self.repos)

    def list_rulesets(self, repo: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self.repos[repo])

    def create_ruleset(self, repo: str, spec: github_rulesets.RulesetSpec) -> dict[str, Any]:
        ruleset_id = max([int(item["id"]) for item in self.repos[repo]] + [0]) + 1
        created = response_ruleset(spec, ruleset_id)
        self.repos[repo].append(created)
        self.writes.append(("create", spec.key, None))
        return copy.deepcopy(created)

    def update_ruleset(self, repo: str, ruleset_id: int, spec: github_rulesets.RulesetSpec) -> dict[str, Any]:
        updated = response_ruleset(spec, ruleset_id)
        self.repos[repo] = [updated if int(item["id"]) == ruleset_id else item for item in self.repos[repo]]
        self.writes.append(("update", spec.key, ruleset_id))
        return copy.deepcopy(updated)


def args(command: str, **overrides: Any) -> argparse.Namespace:
    values = {
        "command": command,
        "repo": ["owner/repo"],
        "all_owned": False,
        "owner": None,
        "confirm_owner_admin_write": False,
        "confirm_repository_count": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_standard_specs_have_separate_bypass_boundaries() -> None:
    landing, direction = github_rulesets.standard_specs(77)
    assert {actor["actor_type"] for actor in landing.payload["bypass_actors"]} == {"Integration", "RepositoryRole"}
    assert any(actor["actor_id"] == 77 for actor in landing.payload["bypass_actors"])
    assert direction.payload["bypass_actors"] == []
    landing_pr = next(rule for rule in landing.payload["rules"] if rule["type"] == "pull_request")
    direction_pr = next(rule for rule in direction.payload["rules"] if rule["type"] == "pull_request")
    assert landing_pr["parameters"]["require_code_owner_review"] is False
    assert direction_pr["parameters"]["require_code_owner_review"] is True
    assert direction_pr["parameters"]["dismiss_stale_reviews_on_push"] is True


def test_unattributed_change_approval_is_off_and_omission_counts_as_drift() -> None:
    # GitHub turns this on when a payload omits it, which blocks app-opened
    # pull requests even with zero required approvals.
    for spec in github_rulesets.standard_specs(77):
        pr = next(rule for rule in spec.payload["rules"] if rule["type"] == "pull_request")
        assert pr["parameters"]["require_extra_approval_for_unattributed_changes"] is False
        live = {key: value for key, value in pr["parameters"].items() if key != "require_extra_approval_for_unattributed_changes"}
        assert not github_rulesets._parameters_match(live, pr["parameters"])
        assert not github_rulesets._parameters_match({**live, "require_extra_approval_for_unattributed_changes": True}, pr["parameters"])
        assert github_rulesets._parameters_match(pr["parameters"], pr["parameters"])


def test_client_uses_active_human_auth_and_clears_token_overrides() -> None:
    calls: list[dict[str, Any]] = []

    def fake_call(method: str, path: str, body: Any = None, **kwargs: Any) -> Any:
        calls.append({"method": method, "path": path, "body": body, **kwargs})
        return github_rulesets.github_api_core.ApiResult(ok=True, status=200, body={"login": "owner"})

    client = github_rulesets.RulesetClient(
        active_gh="gh-human",
        env_command="env",
        call=fake_call,
    )
    assert client.resolve_owner("owner") == "owner"
    prefix = calls[0]["gh_prefix_args"]
    assert prefix[-1] == "gh-human"
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "CODEX_GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
    ):
        assert ["-u", name] == prefix[prefix.index(name) - 1 : prefix.index(name) + 1]
    assert calls[0]["api_version"] == github_rulesets.RULESET_API_VERSION


def test_plan_is_idempotent_and_accepts_server_omission_for_false_update_parameter() -> None:
    specs = github_rulesets.standard_specs(77)
    current = [response_ruleset(spec, index) for index, spec in enumerate(specs, start=1)]
    current[0]["rules"][0].pop("parameters")
    pull_request = next(rule for rule in current[1]["rules"] if rule["type"] == "pull_request")
    pull_request["parameters"].pop("allowed_merge_methods")
    pull_request["parameters"].pop("required_reviewers")
    changes = github_rulesets.plan_changes(current, specs)
    assert [change.action for change in changes] == ["none", "none"]


def test_plan_creates_missing_and_updates_known_manual_alias() -> None:
    landing, direction = github_rulesets.standard_specs(77)
    alias = response_ruleset(landing, 10)
    alias["name"] = next(iter(github_rulesets.LANDING_RULESET_ALIASES))
    alias["bypass_actors"] = [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
    changes = github_rulesets.plan_changes([alias], (landing, direction))
    assert [change.action for change in changes] == ["update", "create"]
    assert changes[0].current_name == alias["name"]


def test_duplicate_managed_candidates_fail_closed() -> None:
    landing, _direction = github_rulesets.standard_specs(77)
    one = response_ruleset(landing, 1)
    two = response_ruleset(landing, 2)
    two["name"] = next(iter(github_rulesets.LANDING_RULESET_ALIASES))
    try:
        github_rulesets.plan_changes([one, two], (landing,))
    except github_rulesets.RulesetError as exc:
        assert exc.cause == "ambiguous_ruleset"
    else:
        raise AssertionError("duplicate managed rulesets must fail closed")


def test_apply_converges_and_second_apply_writes_nothing() -> None:
    specs = github_rulesets.standard_specs(77)
    client = FakeClient({"owner/repo": []})
    first = github_rulesets.apply_repository(client, "owner/repo", specs)
    assert first["verified"] is True
    assert client.writes == [("create", "landing", None), ("create", "direction", None)]
    second = github_rulesets.apply_repository(client, "owner/repo", specs)
    assert second == {"repo": "owner/repo", "changed": False, "applied": [], "verified": True}
    assert len(client.writes) == 2


def test_cli_requires_explicit_write_confirmation() -> None:
    cli = load_cli()
    client = FakeClient({"owner/repo": []})
    try:
        cli.run(args("apply"), client=client, app_id=77)
    except github_rulesets.RulesetError as exc:
        assert exc.cause == "authorization_required"
    else:
        raise AssertionError("apply without confirmation must fail")
    assert client.writes == []


def test_cli_requires_exact_count_for_multi_repository_apply() -> None:
    cli = load_cli()
    client = FakeClient({"owner/a": [], "owner/b": []})
    multi = args(
        "apply",
        repo=["owner/a", "owner/b"],
        confirm_owner_admin_write=True,
        confirm_repository_count=1,
    )
    try:
        cli.run(multi, client=client, app_id=77)
    except github_rulesets.RulesetError as exc:
        assert exc.cause == "confirmation_mismatch"
        assert exc.payload["repository_count"] == 2
    else:
        raise AssertionError("a stale repository count must fail")
    assert client.writes == []


def test_cli_preserves_completed_repository_receipts_on_later_failure() -> None:
    cli = load_cli()

    class FailingClient(FakeClient):
        def create_ruleset(self, repo: str, spec: github_rulesets.RulesetSpec) -> dict[str, Any]:
            if repo == "owner/b":
                raise github_rulesets.RulesetError("fixture failure", cause="permission_denied")
            return super().create_ruleset(repo, spec)

    client = FailingClient({"owner/a": [], "owner/b": []})
    multi = args(
        "apply",
        repo=["owner/a", "owner/b"],
        confirm_owner_admin_write=True,
        confirm_repository_count=2,
    )
    try:
        cli.run(multi, client=client, app_id=77)
    except github_rulesets.RulesetError as exc:
        assert exc.cause == "permission_denied"
        assert exc.payload["completed_repositories"] == ["owner/a"]
        assert exc.payload["failed_repository"] == "owner/b"
    else:
        raise AssertionError("the second repository must fail")


def test_cli_all_owned_uses_owner_identity_and_sorted_inventory() -> None:
    cli = load_cli()
    client = FakeClient({"owner/z": [], "owner/a": []})
    payload = cli.run(args("plan", repo=[], all_owned=True, owner="owner"), client=client, app_id=77)
    assert payload["actor"] == "owner"
    assert [item["repo"] for item in payload["repositories"]] == ["owner/a", "owner/z"]
    assert payload["write_authorized"] is False


def test_intra_repository_failure_preserves_receipts_and_write_step() -> None:
    class FailingClient(FakeClient):
        def create_ruleset(self, repo: str, spec: github_rulesets.RulesetSpec) -> dict[str, Any]:
            if spec.key == "direction":
                raise github_rulesets.RulesetError(
                    "fixture rejection",
                    cause="validation_error",
                    payload={"api_result": {"failure": {"write_outcome": "rejected"}}},
                )
            return super().create_ruleset(repo, spec)

    cli = load_cli()
    client = FailingClient({"owner/repo": []})
    try:
        cli.run(
            args("apply", confirm_owner_admin_write=True),
            client=client,
            app_id=77,
        )
    except github_rulesets.RulesetError as exc:
        assert exc.payload["applied"][0]["key"] == "landing"
        assert exc.payload["failed_step"] == "create_direction"
        assert exc.payload["write_attempted"] is True
        outcome, step = cli.failure_context(is_write=True, payload=exc.payload)
        assert (outcome, step) == ("unknown", "create_direction")
    else:
        raise AssertionError("the second ruleset create must fail")


def test_post_write_read_failure_preserves_receipts_and_write_step() -> None:
    class FailingReadClient(FakeClient):
        reads = 0

        def list_rulesets(self, repo: str) -> list[dict[str, Any]]:
            self.reads += 1
            if self.reads == 2:
                raise github_rulesets.RulesetError("fixture read failure", cause="provider_failure")
            return super().list_rulesets(repo)

    cli = load_cli()
    client = FailingReadClient({"owner/repo": []})
    try:
        cli.run(
            args("apply", confirm_owner_admin_write=True),
            client=client,
            app_id=77,
        )
    except github_rulesets.RulesetError as exc:
        assert [item["key"] for item in exc.payload["applied"]] == ["landing", "direction"]
        outcome, step = cli.failure_context(is_write=True, payload=exc.payload)
        assert (outcome, step) == ("unknown", "post_write_read")
    else:
        raise AssertionError("the verification read must fail")


def test_post_write_drift_reports_verification_step() -> None:
    class DriftingClient(FakeClient):
        def create_ruleset(self, repo: str, spec: github_rulesets.RulesetSpec) -> dict[str, Any]:
            created = super().create_ruleset(repo, spec)
            self.repos[repo][-1]["enforcement"] = "disabled"
            return created

    cli = load_cli()
    client = DriftingClient({"owner/repo": []})
    try:
        cli.run(
            args("apply", confirm_owner_admin_write=True),
            client=client,
            app_id=77,
        )
    except github_rulesets.RulesetError as exc:
        outcome, step = cli.failure_context(is_write=True, payload=exc.payload)
        assert (outcome, step) == ("unknown", "post_write_verification")
        assert exc.payload["remaining"]
    else:
        raise AssertionError("server-normalized drift must fail verification")


def test_missing_standard_rulesets_requires_active_branch_rulesets() -> None:
    names = github_rulesets.required_ruleset_names()
    current = [
        {"name": names[0], "target": "branch", "enforcement": "active"},
        {"name": names[1], "target": "branch", "enforcement": "disabled"},
    ]
    assert github_rulesets.missing_standard_rulesets(current) == [names[1]]


def main() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
