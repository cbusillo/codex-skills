#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Behavior tests for permission derivation, drift detection and redacted audits."""
from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import github_capabilities as capabilities
import github_identity
import github_read
from test_github_identity import app_environment

SPEC = importlib.util.spec_from_file_location("capabilities_cli", Path(__file__).with_name("github-capabilities.py"))
assert SPEC and SPEC.loader
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class Reader(github_read.GitHubReader):
    def __init__(self):
        super().__init__(expected_actor="fixture-app[bot]")
        self.calls = []
        self.metadata = {"full_name": "example/repo", "default_branch": "main", "visibility": "private", "has_issues": True, "has_discussions": False}
        self.status = 200
        self.ok = True

    def get_json(self, path, *, step):
        self.calls.append(("GET", path))
        return self.metadata

    def request(self, method, path, *, step):
        self.calls.append((method, path))
        return github_read.github_api_core.ApiResult(ok=self.ok, status=self.status, body={}, headers={})

    def graphql_json(self, query, variables, *, step, **_kwargs):
        self.calls.append(("GraphQL", query))
        return github_read.github_api_core.ApiResult(ok=self.ok, status=self.status, body={"data": {"repository": {"discussions": {"totalCount": 0}}}})


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.matrix = capabilities.load_matrix()
        self.installation = {"permissions": capabilities.permission_profile(self.matrix)["permissions"]["repository"], "suspended": False}
        self.reader = Reader()

    def audit(self, *names, membership=None):
        return capabilities.audit_repository(self.reader, "example/repo", installation=self.installation,
            membership={"example/repo"} if membership is None else membership,
            capabilities={name: self.matrix["capabilities"][name] for name in names})

    def test_profile_is_derived_and_excludes_other_roles(self):
        full = capabilities.permission_profile(self.matrix)
        grants = full["permissions"]["repository"]
        self.assertEqual(grants["actions"], "write")
        self.assertEqual(grants["administration"], "read")
        self.assertEqual(grants["deployments"], "read")
        self.assertNotIn("packages", grants)
        self.assertFalse(full["permissions"]["organization"])
        read = capabilities.permission_profile(self.matrix, read_only=True)["permissions"]["repository"]
        self.assertEqual(set(read.values()), {"read"})
        self.assertNotIn("workflows", read)
        reduced = copy.deepcopy(self.matrix)
        del reduced["capabilities"]["actions_write"]
        self.assertEqual(capabilities.permission_profile(reduced)["permissions"]["repository"]["actions"], "read")

    def test_missing_write_is_not_satisfied_by_read(self):
        self.installation["permissions"]["actions"] = "read"
        result = self.audit("actions_read", "actions_write")["capabilities"]
        self.assertEqual(result["actions_read"]["state"], "available")
        self.assertEqual(result["actions_write"]["state"], "permission_missing")
        self.assertEqual(len(self.reader.calls), 2)

    def test_audit_and_passthrough_cannot_keep_an_unused_grant(self):
        consumers = {"github.api.call", "github.gh_with_env_token", "github.read", "github.capabilities.audit"}
        for row in self.matrix['operations']:
            if row['id'] not in consumers:
                row['capabilities'] = [name for name in row['capabilities'] if name != 'actions_write']
        self.matrix['operations'] = [row for row in self.matrix['operations'] if row['capabilities'] or row['permission_mode'] != 'fixed']
        self.assertEqual(capabilities.permission_profile(self.matrix)['permissions']['repository']['actions'], 'read')
        self.assertTrue(any('actions_write has no supported operation' in error for error in capabilities.validate_permissions(self.matrix, Path('.'), check_sources=False)))

    def test_membership_and_suspension_precede_public_reads(self):
        self.assertEqual(self.audit("metadata_read", membership=set())["state"], "not_installed")
        self.installation["suspended"] = True
        self.assertEqual(self.audit("metadata_read")["reason"], "installation_suspended")
        self.assertFalse(self.reader.calls)

    def test_archived_and_wrong_identity_are_not_available(self):
        self.reader.metadata["archived"] = True
        self.assertEqual(self.audit("contents_write")["state"], "excluded_archived_or_disabled")
        self.reader.metadata["full_name"] = "example/other"
        self.assertEqual(self.audit("contents_write")["reason"], "repository_identity_mismatch")

    def test_disabled_and_separate_actor_do_not_become_missing_grants(self):
        result = self.audit("discussions_write", "protection_write", "packages_write")["capabilities"]
        self.assertEqual(result["discussions_write"]["state"], "not_enabled")
        for name in ("protection_write", "packages_write"):
            self.assertEqual(result[name]["state"], "requires_separate_actor")
        self.assertEqual(len(self.reader.calls), 1)

    def test_successful_safe_reads_do_not_claim_write_execution(self):
        result = self.audit("contents_read", "contents_write", "workflows_write")["capabilities"]
        self.assertEqual(result["contents_read"]["state"], "available")
        self.assertEqual(result["contents_write"]["state"], "permission_granted")
        self.assertEqual(result["contents_write"]["write_probe"], "not_exercised")
        self.assertEqual(result["workflows_write"]["write_probe"], "not_exercised")
        self.assertEqual(len(self.reader.calls), 2)
        self.assertTrue(all(method == "GET" for method, _ in self.reader.calls))

    def test_denied_ambiguous_and_no_data_remain_distinct(self):
        self.assertEqual(capabilities.classify_probe(403, granted=False)["state"], "permission_missing")
        self.assertEqual(capabilities.classify_probe(403, granted=True)["reason"], "granted_but_denied")
        self.assertEqual(capabilities.classify_probe(404, granted=True)["reason"], "ambiguous_404")
        self.assertEqual(capabilities.classify_probe(404, granted=True, message="No analysis found")["state"], "no_data")
        self.reader.ok = False
        self.assertEqual(self.audit("actions_read")["capabilities"]["actions_read"]["state"], "unavailable")

    def test_secret_scanning_only_uses_redacted_reader(self):
        signal = {"status": "findings", "reason": None, "unexpected_provider_payload": "DO-NOT-PRINT"}
        with patch.object(github_read, "redacted_secret_scanning_status", return_value=signal) as read:
            result = self.audit("secret_scanning_alerts_read")
        read.assert_called_once_with(self.reader, "example/repo", limit=1)
        self.assertNotIn("DO-NOT-PRINT", json.dumps(result))
        self.assertEqual(result["capabilities"]["secret_scanning_alerts_read"]["state"], "available")
        self.assertFalse(any("secret-scanning" in path for _, path in self.reader.calls))

    def test_secret_reader_failure_preserves_other_capabilities(self):
        response = github_read.github_api_core.ApiResult(ok=False, status=503, body={'message': 'DO-NOT-PRINT'})
        errors = [github_read.GitHubReadError('DO-NOT-PRINT', result=response, diagnostics={}), github_read.GitHubReadShapeError('DO-NOT-PRINT')]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(github_read, 'redacted_secret_scanning_status', side_effect=error):
                result = self.audit('contents_read', 'secret_scanning_alerts_read', 'actions_read')
            self.assertEqual(result['state'], 'audited')
            self.assertEqual(result['capabilities']['actions_read']['state'], 'available')
            self.assertEqual(result['capabilities']['secret_scanning_alerts_read']['state'], 'unavailable')
            self.assertNotIn('DO-NOT-PRINT', json.dumps(result))

    def test_explicit_disabled_features_and_empty_repository_are_distinct(self):
        with patch.object(github_read, 'redacted_secret_scanning_status', return_value={'status': 'not_enabled', 'reason': 'scanning_disabled'}):
            self.assertEqual(self.audit('secret_scanning_alerts_read')['capabilities']['secret_scanning_alerts_read']['state'], 'not_enabled')
        self.assertEqual(capabilities.classify_probe(403, granted=True, message='Dependabot alerts are disabled for this repository.')['state'], 'not_enabled')
        self.assertEqual(capabilities.classify_probe(409, granted=True, message='Git Repository is empty.')['state'], 'no_data')

    def test_discussions_probe_is_read_only(self):
        self.reader.metadata["has_discussions"] = True
        result = self.audit("discussions_write")["capabilities"]["discussions_write"]
        self.assertEqual(result["read_probe"], "available")
        self.assertTrue(self.reader.calls[-1][1].startswith("query"))
        self.reader.ok = False
        self.assertEqual(self.audit("discussions_write")["capabilities"]["discussions_write"]["state"], "unavailable")

    def test_schema_rejects_unreviewed_permissions_and_probes(self):
        mutations = [
            lambda m: m["capabilities"]["actions_write"].update(probe="invented"),
            lambda m: m["capabilities"]["actions_write"].update(access=[]),
            lambda m: m["capabilities"]["actions_write"].update(scope="account"),
            lambda m: m["operations"][0].update(capabilities=[{}]),
            lambda m: m["operations"][0].update(permission_note=""),
            lambda m: m["capabilities"].update(unused=dict(m["capabilities"]["metadata_read"])),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                matrix = copy.deepcopy(self.matrix)
                mutate(matrix)
                self.assertTrue(capabilities.validate_permissions(matrix, Path('.'), check_sources=False))

    def test_api_surface_detects_endpoint_method_query_and_new_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / 'github/scripts'
            scripts.mkdir(parents=True)
            path = scripts / 'fixture.py'
            path.write_text('def run(reader):\n    reader.request("GET", "/repos/example/repo/issues", step="read")\n')
            original = capabilities.source_fingerprints(root)
            # Unrelated payload/logging and dictionary access are not API drift.
            path.write_text(path.read_text() + '    payload = {"key": 1}\n    value = payload.get("key")\n')
            self.assertEqual(original, capabilities.source_fingerprints(root))
            path.write_text(path.read_text().replace('"GET"', '"POST"'))
            self.assertNotEqual(original, capabilities.source_fingerprints(root))
            path.write_text('query = "query { viewer { login } }"\n')
            self.assertTrue(capabilities.api_surface(path))
            matrix = copy.deepcopy(self.matrix)
            matrix['api_surface_fingerprints'] = original
            self.assertTrue(any('API surface changed' in error for error in capabilities.validate_permissions(matrix, root)))
            (scripts / 'new_helper.py').write_text('reader.get_json("/repos/example/new", step="new")\n')
            self.assertTrue(any('new_helper.py' in error for error in capabilities.validate_permissions(matrix, root)))

    def test_actor_mismatch_cannot_turn_http_success_into_available(self):
        reader = github_read.GitHubReader(expected_actor='fixture-app[bot]', strict_actor=True)
        response = github_read.github_api_core.ApiResult(ok=True, status=200, body={'private_payload': 'DO-NOT-PRINT'}, actor='unexpected-human')
        with patch.object(reader, '_transport_request', return_value=response):
            result = capabilities._get_probe(reader, '/repos/example/repo/actions/workflows', 'fixture')
        self.assertEqual(result['state'], 'unavailable')
        self.assertNotIn('DO-NOT-PRINT', json.dumps(result))

    def test_drift_covers_cli_wrappers_methods_and_dynamic_probe_paths(self):
        pairs = [
            ('run_raw(["project", "item-add", target])', 'run_raw(["project", "item-delete", target])'),
            ('args = ["pr", "create", target]', 'args = ["pr", "edit", target]'),
            ('rest_json("GET", endpoint)', 'rest_json("DELETE", endpoint)'),
            ('_call_api("GET", endpoint)', '_call_api("PATCH", endpoint)'),
            ('_call("GET", endpoint)', '_call("DELETE", endpoint)'),
            ('new_adapter(endpoint, method="GET")', 'new_adapter(endpoint, method="POST")'),
            ('path = f"{base}/actions/runners"', 'path = f"{base}/actions/secrets"'),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.py'
            for before, after in pairs:
                with self.subTest(before=before):
                    path.write_text(before)
                    original = capabilities.api_surface(path)
                    path.write_text(after)
                    self.assertNotEqual(original, capabilities.api_surface(path))

    def test_installation_metadata_rejects_wrong_identity_and_hides_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            config = github_identity.github_app_config(app_environment(Path(directory), 'https://api.github.invalid'))
            payload: dict[str, Any] = {"id": 67890, "app_id": 12345, "app_slug": "fixture-app", "permissions": {"contents": "write"},
                       "account": {"login": "example", "type": "User"}, "repository_selection": "all", "secret": "DO-NOT-PRINT"}
            with patch.object(github_identity, '_app_headers', return_value={}), patch.object(github_identity, '_request_json', return_value=payload):
                self.assertNotIn('DO-NOT-PRINT', json.dumps(github_identity.github_app_installation_metadata(config)))
                payload['permissions']['contents'] = []
                with self.assertRaises(github_identity.GitHubAppError):
                    github_identity.github_app_installation_metadata(config)
                payload['id'] = 999
                with self.assertRaisesRegex(github_identity.GitHubAppError, 'wrong installation'):
                    github_identity.github_app_installation_metadata(config)

    def test_explicit_refresh_bypasses_cache_without_actor_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            config = github_identity.github_app_config(app_environment(Path(directory), 'https://api.github.invalid'))
            with patch.object(github_identity, '_request_app_login', return_value='fixture-app[bot]') as login, \
                 patch.object(github_identity, '_request_installation_token', side_effect=[('token-one', 9999), ('token-two', 9999)]) as mint:
                self.assertEqual(github_identity.github_app_auth(config, now=1000)[0], 'token-one')
                self.assertEqual(github_identity.github_app_auth(config, now=1001)[0], 'token-one')
                self.assertEqual(github_identity.github_app_auth(config, now=1002, refresh=True), ('token-two', 'fixture-app[bot]'))
                self.assertEqual(mint.call_count, 2)
                self.assertEqual(login.call_count, 2)

    def test_cli_requires_scope_and_does_not_accept_write_options(self):
        parser = cli.build_parser()
        args = parser.parse_args(['audit', '--repo', 'example/repo', '--refresh-token'])
        self.assertEqual(args.repo, ['example/repo'])
        self.assertTrue(args.refresh_token)
        with patch.object(github_identity, 'github_app_config', return_value=None):
            self.assertEqual(cli.run_audit(args, self.matrix)['reason'], 'github_app_not_configured')


if __name__ == '__main__':
    unittest.main()
