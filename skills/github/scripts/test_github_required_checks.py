#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline safety checks for additive owner-writer plans and readback."""
from __future__ import annotations

import copy
import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import github_rulesets as rules
from test_github_rulesets import FakeClient, load_cli, response_ruleset


class CheckClient(FakeClient):
    def __init__(self, *, classic: bool = True, rulesets=None):
        super().__init__({"owner/repo": rulesets or []})
        self.repository = {"id": 9, "full_name": "owner/repo", "owner": {"login": "owner", "type": "User"},
                           "default_branch": "main", "archived": False}
        self.protection = {"enforce_admins": {"enabled": True}, "required_pull_request_reviews": {
            "require_code_owner_reviews": True, "bypass_pull_request_allowances": {"apps": [{"id": 88}]}},
            "required_status_checks": {"strict": True, "contexts": ["old"],
                                       "checks": [{"context": "old", "app_id": 44}]}} if classic else None
        self.drop_write = False
        self.fail_readback = False
        self.calls = []

    def request(self, method, path, *, operation, body=None, allow_unprotected=False):
        self.calls.append((method, path, operation, copy.deepcopy(body)))
        if method == "PATCH":
            self.writes.append(("patch", "checks", None))
            if not self.drop_write:
                self.protection["required_status_checks"] = {
                    "strict": body["strict"], "contexts": sorted({item["context"] for item in body["checks"]}),
                    "checks": [{**item, "app_id": None if item["app_id"] == -1 else item["app_id"]}
                               for item in body["checks"]]}
            return {}
        if self.fail_readback and self.writes:
            raise rules.RulesetError("readback unavailable", cause="provider_failure")
        return copy.deepcopy(self.protection if path.endswith("/protection") else self.repository)


def plan(client, target="classic", checks=None):
    return rules.build_check_plan("owner/repo", target, checks or [{"context": "new", "app_id": 55}],
                                  rules.check_snapshot(client, "owner/repo", target))


def refused(call, cause):
    try:
        call()
    except rules.RulesetError as exc:
        assert exc.cause == cause, (exc.cause, str(exc))
        return exc
    raise AssertionError(f"expected {cause}")


def test_classic_preserves_requirements_bindings_strictness_and_other_protection():
    client = CheckClient()
    before = copy.deepcopy(client.protection)
    reviewed = plan(client)
    assert client.writes == []
    assert reviewed["added"] == [{"context": "new", "app_id": 55}]
    assert rules.run_check_plan(client, reviewed, apply=True)["verified"]
    after = copy.deepcopy(client.protection)
    old = before.pop("required_status_checks")
    checks = after.pop("required_status_checks")
    assert before == after
    assert checks["strict"] == old["strict"]
    assert all(item in checks["checks"] for item in old["checks"])
    assert not rules.run_check_plan(client, reviewed, apply=True)["changed"]
    assert rules.run_check_plan(client, reviewed, apply=False)["verified"]
    assert len(client.writes) == 1


def test_ruleset_preserves_every_rule_parameter_and_bypass():
    landing = response_ruleset(rules.standard_specs(77)[0], 12)
    landing["name"] = "Custom quality checks"
    landing["rules"].extend([{"type": "deletion"}, {"type": "future_rule", "parameters": {"opaque": [None, {}]}}])
    landing["rules"].append({"type": "required_status_checks", "parameters": {
        "strict_required_status_checks_policy": True, "do_not_enforce_on_create": True,
        "required_status_checks": [{"context": "old", "integration_id": 44}]}})
    client = CheckClient(rulesets=[landing])
    reviewed = plan(client, "12")
    rules.run_check_plan(client, reviewed, apply=True)
    after = client.repos["owner/repo"][0]
    original = copy.deepcopy(landing)
    added_rule = after["rules"].pop()
    original_rule = original["rules"].pop()
    assert after == original
    assert original_rule["parameters"]["required_status_checks"][0] in added_rule["parameters"]["required_status_checks"]
    added_rule["parameters"].pop("required_status_checks")
    original_rule["parameters"].pop("required_status_checks")
    assert added_rule == original_rule


def test_managed_targets_refuse_and_separate_checks_survive_standard_apply():
    specs = rules.standard_specs(77)
    names = [spec.name for spec in specs] + list(rules.LANDING_RULESET_ALIASES)
    names += [spec.name for spec in rules.client_lock_specs(77, branches=True, tags=True)]
    for name in names:
        current = response_ruleset(specs[0], 12)
        current["name"] = name
        client = CheckClient(rulesets=[current])
        refused(lambda: plan(client, "12"), "managed_ruleset_target")
        assert client.writes == []
    client = CheckClient(rulesets=[response_ruleset(spec, index) for index, spec in enumerate(specs, 1)])
    reviewed = plan(client, "new")
    rules.run_check_plan(client, reviewed, apply=True)
    created = copy.deepcopy(client.repos["owner/repo"][-1])
    client.repos["owner/repo"][0]["enforcement"] = "disabled"
    before_writes = len(client.writes)
    rules.apply_repository(client, "owner/repo", specs)
    assert len(client.writes) == before_writes + 1
    assert client.writes[-1] == ("update", specs[0].key, 1)
    assert created in client.repos["owner/repo"]
    assert rules.run_check_plan(client, reviewed, apply=False)["verified"]


def test_new_ruleset_does_not_replace_existing_protection_and_retry_writes_nothing():
    client = CheckClient(classic=False, rulesets=[response_ruleset(rules.standard_specs(77)[1], 2)])
    before = copy.deepcopy(client.repos)
    reviewed = plan(client, "new", [{"context": "new", "app_id": None}])
    wire_checks = reviewed["payload"]["rules"][0]["parameters"]["required_status_checks"]
    assert wire_checks == [{"context": "new"}]
    assert rules.run_check_plan(client, reviewed, apply=True)["verified"]
    assert client.repos["owner/repo"][:-1] == before["owner/repo"]
    assert client.repos["owner/repo"][-1]["bypass_actors"] == []
    assert client.protection is None
    assert not rules.run_check_plan(client, reviewed, apply=True)["changed"]
    assert len(client.writes) == 1


def test_drift_in_any_protection_ruleset_or_repository_refuses_before_write():
    for change in ("strict", "bypass", "rule", "branch", "archived", "check"):
        client = CheckClient(rulesets=[response_ruleset(rules.standard_specs(77)[0], 1)])
        reviewed = plan(client)
        if change == "strict":
            client.protection["required_status_checks"]["strict"] = False
        elif change == "check":
            client.protection["required_status_checks"]["checks"][0]["app_id"] = 99
        elif change == "bypass":
            client.repos["owner/repo"][0]["bypass_actors"] = []
        elif change == "rule":
            client.repos["owner/repo"][0]["rules"].append({"type": "deletion"})
        elif change == "branch":
            client.repository["default_branch"] = "trunk"
        else:
            client.repository["archived"] = True
        refused(lambda: rules.run_check_plan(client, reviewed, apply=True),
                "owner_actor_mismatch" if change == "archived" else "plan_state_drift")
        assert client.writes == []


def test_digest_and_recomputed_payload_prevent_tampering():
    reviewed = plan(CheckClient())
    digest = rules.check_plan_digest(reviewed)
    assert rules.validate_check_plan(reviewed, digest) == reviewed
    edited = copy.deepcopy(reviewed)
    edited["payload"]["checks"] = []
    refused(lambda: rules.validate_check_plan(edited, digest), "plan_digest_mismatch")
    refused(lambda: rules.validate_check_plan(edited, rules.check_plan_digest(edited)), "validation_error")


def test_unbound_classic_checks_stay_explicit_and_conflicting_bindings_refuse():
    client = CheckClient()
    client.protection["required_status_checks"]["checks"][0]["app_id"] = None
    reviewed = plan(client, checks=[{"context": "new", "app_id": None}])
    assert all(item["app_id"] == -1 for item in reviewed["payload"]["checks"])
    assert rules.run_check_plan(client, reviewed, apply=True)["verified"]
    refused(lambda: plan(client, checks=[{"context": "old", "app_id": 33}]), "validation_error")
    del client.protection["required_status_checks"]["checks"]
    refused(lambda: plan(client), "invalid_response")


def test_incomplete_or_ambiguous_ruleset_refuses():
    landing = response_ruleset(rules.standard_specs(77)[0], 12)
    landing["name"] = "Custom quality checks"
    client = CheckClient(rulesets=[landing])
    refused(lambda: plan(client, "12"), "validation_error")
    del client.repos["owner/repo"][0]["bypass_actors"]
    refused(lambda: plan(client), "invalid_response")


def test_failed_write_or_readback_keeps_uncertain_outcome_without_retry():
    cli = load_cli()
    for mode in ("drop_write", "fail_readback"):
        client = CheckClient()
        reviewed = plan(client)
        setattr(client, mode, True)
        exc = refused(lambda: rules.run_check_plan(client, reviewed, apply=True),
                      "provider_failure" if mode == "fail_readback" else "post_write_verification_failed")
        assert cli.failure_context(is_write=True, payload=exc.payload)[0] == "unknown"
        assert len(client.writes) == 1


def test_second_preflight_catches_a_concurrent_change_before_the_mutation():
    class ChangingClient(CheckClient):
        repository_reads = 0

        def request(self, method, path, **kwargs):
            if path == "/repos/owner/repo":
                self.repository_reads += 1
                if self.repository_reads == 3:
                    self.protection["enforce_admins"]["enabled"] = False
            return super().request(method, path, **kwargs)

    client = ChangingClient()
    reviewed = plan(client)
    refused(lambda: rules.run_check_plan(client, reviewed, apply=True), "plan_state_drift")
    assert client.writes == []


MISSING = object()
MALFORMED_SNAPSHOTS = [
    (("repository", "owner"), None),
    (("repository", "owner", "login"), None),
    (("repository", "default_branch"), []),
    (("repository", "id"), True),
    (("protection",), []),
    (("protection", "required_status_checks"), []),
    (("protection", "required_status_checks", "strict"), None),
    (("protection", "required_status_checks", "contexts"), None),
    (("protection", "required_status_checks", "contexts"), [{}]),
    (("protection", "required_status_checks", "checks"), MISSING),
    (("protection", "required_status_checks", "checks"), [None]),
    (("rulesets",), None),
    (("rulesets",), [None]),
    (("rulesets", 0, "id"), MISSING),
    (("rulesets", 0, "name"), []),
    (("rulesets", 0, "bypass_actors"), MISSING),
    (("rulesets", 0, "bypass_actors"), None),
    (("rulesets", 0, "conditions"), MISSING),
    (("rulesets", 0, "conditions"), None),
    (("rulesets", 0, "conditions", "ref_name"), None),
    (("rulesets", 0, "conditions", "ref_name", "include"), None),
    (("rulesets", 0, "conditions", "ref_name", "exclude"), [None]),
    (("rulesets", 0, "rules"), None),
    (("rulesets", 0, "rules"), [None]),
    (("rulesets", 0, "rules", 0, "type"), MISSING),
    (("rulesets", 0, "rules", 0, "type"), ""),
    (("rulesets", 0, "rules", 0, "parameters"), None),
    (("rulesets", 0, "rules", 0, "parameters", "strict_required_status_checks_policy"), []),
    (("rulesets", 0, "rules", 0, "parameters", "required_status_checks"), None),
    (("rulesets", 0, "rules", 0, "parameters", "required_status_checks"), [None]),
]


def corrupt(snapshot, mutation):
    result = copy.deepcopy(snapshot)
    path, value = mutation
    parent = result
    for key in path[:-1]:
        parent = parent[key]
    if value is MISSING:
        del parent[path[-1]]
    else:
        parent[path[-1]] = copy.deepcopy(value)
    return result


class SnapshotClient(CheckClient):
    def __init__(self):
        current = response_ruleset(rules.standard_specs(77)[0], 12)
        current["name"] = "Custom quality checks"
        current["rules"] = [{"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": True, "required_status_checks": [
                {"context": "old", "integration_id": 44}]}}]
        super().__init__(rulesets=[current])
        self.reads = 0
        self.corrupt_at = 0
        self.mutation = None
        self.snapshot = None

    def request(self, method, path, **kwargs):
        if method == "GET" and path == "/repos/owner/repo":
            self.reads += 1
            self.snapshot = {"repository": self.repository, "protection": self.protection,
                             "rulesets": self.repos["owner/repo"]}
            self.snapshot = (corrupt(self.snapshot, self.mutation) if self.reads == self.corrupt_at
                             else copy.deepcopy(self.snapshot))
        if method == "GET":
            return copy.deepcopy(self.snapshot["protection" if path.endswith("/protection") else "repository"])
        return super().request(method, path, **kwargs)

    def list_rulesets(self, repo):
        return copy.deepcopy(self.snapshot["rulesets"])


def terminal(cli, client, arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with (patch.object(rules, "RulesetClient", return_value=client),
          redirect_stdout(stdout), redirect_stderr(stderr)):
        code = cli.main(arguments)
    result = json.loads(stdout.getvalue())
    assert code != 0 and not result["ok"], result
    assert "Traceback" not in stderr.getvalue()
    return result


def test_malformed_snapshots_share_one_terminal_contract_at_every_phase():
    cli = load_cli()
    with tempfile.TemporaryDirectory() as directory:
        checks, output = Path(directory) / "checks.json", Path(directory) / "plan.json"
        checks.write_text(json.dumps([{"context": "new", "app_id": 55}]))
        for mutation in MALFORMED_SNAPSHOTS:
            # Malformed live planning responses never leave a reviewable plan.
            output.unlink(missing_ok=True)
            client = SnapshotClient()
            client.mutation, client.corrupt_at = mutation, 1
            result = terminal(cli, client, ["checks-plan", "--repo", "owner/repo", "--target", "012",
                                           "--checks-file", str(checks), "--out", str(output)])
            assert result["failure"]["cause"] == "invalid_response", (mutation, result)
            assert not output.exists() and client.writes == []

            # Even a recomputed digest cannot authorize structurally invalid saved input.
            reviewed = plan(SnapshotClient(), "012")
            malformed = copy.deepcopy(reviewed)
            malformed["before"] = corrupt(reviewed["before"], mutation)
            output.write_text(json.dumps(malformed))
            common = ["--plan-file", str(output), "--plan-digest", rules.check_plan_digest(malformed)]
            client = SnapshotClient()
            result = terminal(cli, client, ["checks-apply", *common, "--confirm-owner-admin-write"])
            assert result["failure"]["cause"] == "validation_error", (mutation, result)
            assert client.actor is None and client.reads == 0 and client.writes == []

            output.write_text(json.dumps(reviewed))
            common = ["--plan-file", str(output), "--plan-digest", rules.check_plan_digest(reviewed)]
            for phase in (1, 2, 3):
                client = SnapshotClient()
                client.mutation, client.corrupt_at = mutation, phase
                result = terminal(cli, client, ["checks-apply", *common, "--confirm-owner-admin-write"])
                assert result["failure"]["cause"] == "invalid_response", (mutation, phase, result)
                assert result["failure"]["write_outcome"] == ("unknown" if phase == 3 else "not_started")
                assert len(client.writes) == (1 if phase == 3 else 0)

            client = SnapshotClient()
            rules.run_check_plan(client, reviewed, apply=True)
            client.corrupt_at, client.mutation = client.reads + 1, mutation
            result = terminal(cli, client, ["checks-readback", *common])
            assert result["failure"]["cause"] == "invalid_response", (mutation, result)
            assert len(client.writes) == 1


def test_uninterpreted_ruleset_conditions_stay_opaque():
    client = CheckClient(rulesets=[response_ruleset(rules.standard_specs(77)[0], 12)])
    client.repos["owner/repo"][0]["conditions"] = None
    reviewed = plan(client, "new")
    assert rules.run_check_plan(client, reviewed, apply=True)["verified"]
    assert client.repos["owner/repo"][0]["conditions"] is None


def test_cli_plan_apply_readback_use_the_existing_owner_and_confirmation():
    cli = load_cli()
    with tempfile.TemporaryDirectory() as directory:
        checks = Path(directory) / "checks.json"
        output = Path(directory) / "plan.json"
        checks.write_text(json.dumps([{"context": "new", "app_id": 55}]))
        client = CheckClient()
        with patch.object(rules, "RulesetClient", side_effect=AssertionError("live client forbidden")):
            result = cli.run(cli.parse_args(["checks-plan", "--repo", "owner/repo", "--target", "classic",
                                            "--checks-file", str(checks), "--out", str(output)]), client=client)
            common = ["--plan-file", str(output), "--plan-digest", result["plan_digest"]]
            refused(lambda: cli.run(cli.parse_args(["checks-apply", *common]), client=client), "authorization_required")
            assert client.writes == []
            assert cli.run(cli.parse_args(["checks-apply", *common, "--confirm-owner-admin-write"]), client=client)["verified"]
            assert cli.run(cli.parse_args(["checks-readback", *common]), client=client)["verified"]
            stranger = CheckClient()
            stranger.owner = "stranger"
            refused(lambda: cli.run(cli.parse_args(["checks-readback", *common]), client=stranger), "owner_actor_mismatch")


def test_transport_only_accepts_explicit_branch_not_protected_and_pages_all_rulesets():
    calls = []
    def fake_call(method, path, body=None, **kwargs):
        calls.append((method, path, kwargs))
        if path.endswith("/protection"):
            return rules.github_api_core.ApiResult(ok=False, status=404, body={"message": "Branch not protected"})
        if "/rulesets?" in path:
            return rules.github_api_core.ApiResult(ok=True, status=200, body=[{"id": 1}] if path.endswith("&page=1") else [])
        return rules.github_api_core.ApiResult(ok=True, status=200, body={"id": 1})
    client = rules.RulesetClient(call=fake_call)
    assert client.request("GET", "/protection", operation="github.rulesets.checks.read", allow_unprotected=True) is None
    with patch.object(rules, "PER_PAGE", 1):
        assert client.list_rulesets("owner/repo") == [{"id": 1}]
    assert len([call for call in calls if "/rulesets?" in call[1]]) == 2
    def not_found(*_args, **_kwargs):
        return rules.github_api_core.ApiResult(ok=False, status=404, body={"message": "Not Found"})
    client.call = not_found
    refused(lambda: client.request("GET", "/protection", operation="github.rulesets.checks.read", allow_unprotected=True),
            "github_request_failed")


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
            print(f"ok {name}")
