#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import github_rulesets as rules

CLI_PATH = Path(__file__).with_name("gh-owner-admin.py")


def load_cli() -> Any:
    spec = importlib.util.spec_from_file_location("owner_admin_under_test", CLI_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OwnerClient:
    def __init__(self) -> None:
        self.actor: str | None = None
        self.repository: dict[str, Any] = {
            "id": 9,
            "full_name": "owner/repo",
            "owner": {"login": "owner", "type": "User"},
            "archived": False,
            "default_branch": "main",
            "delete_branch_on_merge": False,
            "allow_merge_commit": True,
            "allow_squash_merge": True,
            "allow_rebase_merge": True,
        }
        self.collaborators = [{"login": "contributor", "id": 12}, {"login": "other", "id": 13}]
        self.rulesets: list[dict[str, Any]] = []
        self.actions_app: dict[str, Any] = {"id": 731, "slug": "github-actions"}
        self.calls: list[tuple[str, str, Any]] = []
        self.drop_write = False
        self.fail_write = False

    def resolve_owner(self, owner: str) -> str:
        if owner != "owner":
            raise rules.RulesetError("owner mismatch", cause="owner_actor_mismatch")
        self.actor = owner
        return owner

    def list_rulesets(self, repo: str) -> list[dict[str, Any]]:
        assert repo == "owner/repo"
        return copy.deepcopy(self.rulesets)

    def request(self, method: str, path: str, *, operation: str, body: Any = None) -> Any:
        self.calls.append((method, path, copy.deepcopy(body)))
        if method != "GET" and self.fail_write:
            raise rules.RulesetError(
                "permission denied", cause="permission_denied", payload={"api_result": {"failure": {"write_outcome": "rejected"}}}
            )
        if method == "PATCH":
            if not self.drop_write:
                self.repository.update(body)
            return copy.deepcopy(self.repository)
        if method == "DELETE":
            if not self.drop_write:
                self.collaborators = [item for item in self.collaborators if item["login"] != path.rsplit("/", 1)[1]]
            return None
        if method in {"POST", "PUT"}:
            result = {**copy.deepcopy(body), "id": 51}
            if not self.drop_write:
                if method == "POST":
                    self.rulesets.append(result)
                else:
                    self.rulesets = [result if item["id"] == 51 else item for item in self.rulesets]
            return result
        if path == "/apps/github-actions":
            return copy.deepcopy(self.actions_app)
        if "/collaborators?" in path:
            page = int(path.rsplit("page=", 1)[1])
            return copy.deepcopy(self.collaborators[(page - 1) * rules.PER_PAGE : page * rules.PER_PAGE])
        if "/secrets?" in path:
            page = int(path.rsplit("page=", 1)[1])
            return {
                "total_count": 101,
                "secrets": [
                    {"name": f"NAME_{i}", "encrypted_value": "never-print-this"} for i in (range(100) if page == 1 else [100])
                ],
            }
        if path == "/repos/owner/repo":
            return copy.deepcopy(self.repository)
        raise AssertionError((method, path, operation))

    @property
    def writes(self) -> list[tuple[str, str, Any]]:
        return [call for call in self.calls if call[0] != "GET"]


def tag_rule() -> dict[str, Any]:
    return {
        "name": "Release tags",
        "target": "tag",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
        "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}],
    }


class OwnerAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cli = load_cli()
        self.client = OwnerClient()

    def plan(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.cli.plan_request(self.client, "owner/repo", request)

    def apply(self, plan: dict[str, Any], *, confirm: bool = True, readback: bool = False) -> dict[str, Any]:
        return self.cli.run_plan(self.client, plan, rules.check_plan_digest(plan), apply=not readback, confirm=confirm)

    def test_metadata_settings_plan_applies_only_declared_flags_and_reads_back(self) -> None:
        request = self.cli.settings_request(
            {"githubSettings": {"expected": {"deleteBranchOnMerge": True}}, "prWorkflow": {"mergePolicy": {"method": "merge"}}}
        )
        plan = self.plan(request)
        self.assertFalse(self.client.writes)
        self.assertTrue(self.apply(plan)["verified"])
        self.assertEqual(
            self.client.writes,
            [
                (
                    "PATCH",
                    "/repos/owner/repo",
                    {
                        "delete_branch_on_merge": True,
                        "allow_merge_commit": True,
                        "allow_squash_merge": False,
                        "allow_rebase_merge": False,
                    },
                )
            ],
        )
        self.assertEqual(self.client.repository["default_branch"], "main")
        self.assertFalse(self.apply(plan)["changed"])
        self.assertTrue(self.apply(plan, readback=True)["verified"])
        self.assertEqual(len(self.client.writes), 1)

    def test_settings_input_never_silently_coerces_bad_metadata(self) -> None:
        for config in (
            {"githubSettings": {"expected": {"deleteBranchOnMerge": "yes"}}},
            {"githubSettings": {"expected": {"inventedSetting": True}}},
            {"prWorkflow": {"mergePolicy": {"method": "invented"}}},
        ):
            with self.subTest(config=config), self.assertRaises(rules.RulesetError):
                self.cli.settings_request(config)
        request = self.cli.settings_request({"githubSettings": {"expected": {"deleteBranchOnMerge": False}}})
        self.assertEqual(request["body"], {"delete_branch_on_merge": False})

    def test_confirmation_digest_owner_and_state_drift_refuse_before_write(self) -> None:
        plan = self.plan({"method": "PATCH", "path": "", "body": {"delete_branch_on_merge": True}})
        with self.assertRaises(rules.RulesetError):
            self.apply(plan, confirm=False)
        changed = copy.deepcopy(plan)
        changed["request"]["body"]["delete_branch_on_merge"] = False
        with self.assertRaises(rules.RulesetError):
            self.cli.run_plan(self.client, changed, rules.check_plan_digest(plan), apply=True, confirm=True)
        self.client.repository["id"] += 1
        with self.assertRaises(rules.RulesetError):
            self.apply(plan)
        self.assertFalse(self.client.writes)
        self.client.repository["owner"]["login"] = "another"
        with self.assertRaises(rules.RulesetError):
            self.plan({"method": "PATCH", "path": "", "body": {"delete_branch_on_merge": True}})

    def test_collaborator_removal_is_exact_and_preserves_other_access(self) -> None:
        self.client.collaborators = [{"login": f"user{i}", "id": i + 100} for i in range(rules.PER_PAGE)] + self.client.collaborators
        others = copy.deepcopy([item for item in self.client.collaborators if item["login"] != "contributor"])
        plan = self.plan({"method": "DELETE", "path": "/collaborators/contributor"})
        self.assertTrue(self.apply(plan)["verified"])
        self.assertEqual(self.client.collaborators, others)
        self.assertTrue(self.apply(plan, readback=True)["verified"])
        self.assertEqual(len(self.client.writes), 1)
        with self.assertRaises(rules.RulesetError):
            self.plan({"method": "DELETE", "path": "/collaborators/owner"})

    def test_tag_rule_plan_apply_and_duplicate_or_managed_target_refusal(self) -> None:
        plan = self.plan({"method": "POST", "path": "/rulesets", "body": tag_rule()})
        self.assertTrue(self.apply(plan)["verified"])
        self.assertTrue(self.apply(plan, readback=True)["verified"])
        self.assertEqual(self.client.rulesets[0]["conditions"], tag_rule()["conditions"])
        update = tag_rule()
        update["rules"] = [{"type": "deletion"}]
        self.assertTrue(self.apply(self.plan({"method": "PUT", "path": "/rulesets/51", "body": update}))["verified"])
        with self.assertRaises(rules.RulesetError):
            self.plan({"method": "POST", "path": "/rulesets", "body": update})
        self.client.rulesets[0]["name"] = rules.CLIENT_TAG_LOCK_NAME
        with self.assertRaises(rules.RulesetError):
            self.plan({"method": "PUT", "path": "/rulesets/51", "body": update})

    def test_personal_github_actions_bypass_refuses_without_blocking_other_apps(self) -> None:
        body = tag_rule()
        body["bypass_actors"] = [{"actor_id": self.client.actions_app["id"], "actor_type": "Integration", "bypass_mode": "always"}]
        with self.assertRaisesRegex(rules.RulesetError, "GitHub Actions"):
            self.plan({"method": "POST", "path": "/rulesets", "body": body})
        body["bypass_actors"][0]["actor_id"] = self.client.actions_app["id"] + 1
        self.assertTrue(self.apply(self.plan({"method": "POST", "path": "/rulesets", "body": body}))["verified"])

    def test_unbounded_or_cross_repository_requests_never_reach_transport(self) -> None:
        requests = [
            {"method": "DELETE", "path": "/repos/other/repo"},
            {"method": "PATCH", "path": "", "body": {"private": False}},
            {"method": "PATCH", "path": "?name=other", "body": {"delete_branch_on_merge": True}},
            {"method": "PUT", "path": "/collaborators/user", "body": {"permission": "admin"}},
            {"method": "POST", "path": "/rulesets", "body": {**tag_rule(), "target": "branch"}},
            {"method": "DELETE", "path": "/collaborators/%2e%2e"},
            {"method": ["PATCH"], "path": "", "body": {"delete_branch_on_merge": True}},
            {"method": "POST", "path": "/rulesets", "body": {**tag_rule(), "enforcement": []}},
        ]
        for request in requests:
            with self.subTest(request=request), self.assertRaises(rules.RulesetError):
                self.plan(request)
        self.assertFalse(self.client.writes)

    def test_denied_write_and_missing_readback_do_not_retry_or_report_success(self) -> None:
        plan = self.plan({"method": "PATCH", "path": "", "body": {"delete_branch_on_merge": True}})
        self.client.fail_write = True
        with self.assertRaises(rules.RulesetError) as caught:
            self.apply(plan)
        self.assertEqual(caught.exception.cause, "permission_denied")
        self.assertEqual(len(self.client.writes), 1)
        self.client.fail_write = False
        self.client.drop_write = True
        with self.assertRaises(rules.RulesetError):
            self.apply(plan)
        self.assertEqual(len(self.client.writes), 2)

    def test_environment_secret_name_read_pages_and_discards_all_other_fields(self) -> None:
        result = self.cli.read_request(self.client, "owner/repo", "/environments/release%2Fcandidate/secrets")
        self.assertEqual(len(result["names"]), 101)
        self.assertNotIn("never-print-this", json.dumps(result))
        self.assertFalse(self.client.writes)
        self.assertEqual(sum("/secrets?" in path for _, path, _ in self.client.calls), 2)

    def test_malformed_or_incomplete_secret_listing_stays_unavailable(self) -> None:
        original = self.client.request
        for response in ([{"name": "PARTIAL"}], {"secrets": [{"name": "PARTIAL"}], "total_count": 2}):

            def request(method: str, path: str, *, operation: str, body: Any = None, response: Any = response) -> Any:
                if "/secrets?" in path:
                    return response
                return original(method, path, operation=operation, body=body)

            with self.subTest(response=response), patch.object(self.client, "request", side_effect=request):
                with self.assertRaises(rules.RulesetError):
                    self.cli.read_request(self.client, "owner/repo", "/environments/release/secrets")
        self.assertFalse(self.client.writes)

    def test_cli_uses_saved_plan_and_confirmation_with_fixture_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "github.json"
            config.write_text(json.dumps({"githubSettings": {"expected": {"deleteBranchOnMerge": True}}}))
            output = root / "plan.json"
            args = self.cli.parse_args(["settings-plan", "--repo", "owner/repo", "--config", str(config), "--out", str(output)])
            result = self.cli.run(args, client=self.client)
            args = self.cli.parse_args(
                ["apply", "--plan-file", str(output), "--plan-digest", result["plan_digest"], "--confirm-owner-admin-write"]
            )
            self.assertTrue(self.cli.run(args, client=self.client)["verified"])

    def test_cli_emits_terminal_failure_without_retry_or_active_auth_fallback(self) -> None:
        plan = self.plan({"method": "PATCH", "path": "", "body": {"delete_branch_on_merge": True}})
        self.client.fail_write = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan))
            output = StringIO()
            with patch.object(self.cli.rules, "RulesetClient", return_value=self.client), redirect_stdout(output):
                status = self.cli.main(
                    [
                        "apply",
                        "--plan-file",
                        str(path),
                        "--plan-digest",
                        rules.check_plan_digest(plan),
                        "--confirm-owner-admin-write",
                    ]
                )
        result = json.loads(output.getvalue())
        self.assertNotEqual(status, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure"]["cause"], "permission_denied")
        self.assertEqual(result["failure"]["write_outcome"], "rejected")
        self.assertEqual(len(self.client.writes), 1)


if __name__ == "__main__":
    unittest.main()
