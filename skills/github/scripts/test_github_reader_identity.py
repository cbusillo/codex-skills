#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline reader routing, credential-path minting and identity isolation."""
from __future__ import annotations

import contextlib
import io
import importlib.util
from types import SimpleNamespace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ['CODEX_SKILLS_ENV_FILE'] = '/missing/reader-tests.env'
import github_api as api
import github_identity as identity
import github_read
import github_request_usage as usage
from test_github_identity import test_private_key


class ReaderIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.environment = {
            'HOME': str(self.root), 'PATH': os.environ['PATH'],
            'CODEX_SKILLS_ENV_FILE': str(self.root / 'missing.env'),
            'CODEX_AUTOMATION_LOGIN': 'main-app[bot]',
            'GITHUB_RETRY_STATE_DIR': str(self.root / 'state'),
            'GITHUB_HTTP_CACHE_DIR': str(self.root / 'bodies'),
            'GITHUB_APP_ID': '12345', 'GITHUB_APP_INSTALLATION_ID': '67890',
            'GITHUB_APP_PRIVATE_KEY_PATH': str(self.root / 'unused-main-path'),
        }
        env = patch.dict(os.environ, self.environment, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def credentials(self):
        key = self.root / 'synthetic.pem'
        key.write_text(test_private_key())
        key.chmod(0o600)
        os.environ.update({'GITHUB_READER_APP_ID': '54321',
                           'GITHUB_READER_APP_INSTALLATION_ID': '76543',
                           'GITHUB_READER_APP_PRIVATE_KEY_PATH': str(key),
                           'GITHUB_READER_APP_TOKEN_CACHE_DIR': str(self.root / 'tokens')})
        return identity.github_app_config(reader=True)

    @staticmethod
    def response(actor='reader-app[bot]', *, status=200, headers=None, body=None):
        raw = f'HTTP/2.0 {status}\n' + ''.join(f'{k}: {v}\n' for k, v in (headers or {}).items()) + '\n'
        if status != 304:
            raw += json.dumps(body if body is not None else {'ok': True})
        return subprocess.CompletedProcess([], 0, raw.encode(), f'acting as {actor}'.encode())

    def test_bulk_routing_and_essential_reads_writes_keep_main(self):
        self.credentials()
        with patch.object(identity, 'github_app_auth', return_value=('synthetic-token', 'reader-app[bot]')) as auth:
            for operation in ('github.plan.index', 'github.plan.next', 'github.repo_snapshot', 'github.pr.watch'):
                actor, expected, prefix = api.request_identity(
                    operation=operation, is_write=False, gh_cmd=api.DEFAULT_GH,
                    gh_prefix_args=[], actor='main-app[bot]', expected_actor='main-app[bot]')
                self.assertEqual((actor, expected), ('reader-app[bot]', 'reader-app[bot]'))
                self.assertIn('--reader', prefix)
            auth.reset_mock()
            for operation, write in [('github.pr.view', False), ('github.pr.merge', True), ('github.plan.index', True)]:
                result = api.request_identity(operation=operation, is_write=write, gh_cmd=api.DEFAULT_GH,
                    gh_prefix_args=[], actor='main-app[bot]', expected_actor='main-app[bot]')
                self.assertEqual(result, ('main-app[bot]', 'main-app[bot]', []))
            auth.assert_not_called()

    def test_missing_or_partial_reader_falls_back_without_human_auth(self):
        for credentials in ({}, {'GITHUB_READER_APP_ID': '54321'}):
            with patch.dict(os.environ, credentials), patch.object(api, '_reader_fallback_reported', False), contextlib.redirect_stderr(io.StringIO()) as notice:
                result = api.request_identity(operation='github.plan.index', is_write=False,
                    gh_cmd=api.DEFAULT_GH, gh_prefix_args=[], actor='main-app[bot]', expected_actor='main-app[bot]')
            self.assertEqual(result[:2], ('main-app[bot]', 'main-app[bot]'))
            self.assertIn('--main-app-only', result[2])
            self.assertIn('reader App credentials missing', notice.getvalue())

    def test_explicit_reader_write_refused_before_auth_or_transport(self):
        for method, path, body in [('POST', '/repos/example/app/issues', {}),
                ('DELETE', '/repos/example/app/issues/1', None),
                ('POST', '/graphql', {'query': 'mutation { closeIssue(input: {}) { clientMutationId } }'}),
                ('POST', '/graphql', {'query': 'query Read { viewer { login } } mutation Write { closeIssue(input: {}) { clientMutationId } }', 'operationName': 'Write'})]:
            with patch.object(identity, 'github_app_auth') as auth, patch('subprocess.run') as run:
                result = api.call_gh_with_retry(method, path, body, gh_prefix_args=['--reader'], is_write=False)
            self.assertFalse(result.ok)
            self.assertEqual(result.failure.cause, 'identity_refused')
            self.assertEqual(result.failure.write_outcome, 'not_started')
            auth.assert_not_called()
            run.assert_not_called()

    def test_configured_reader_auth_failure_never_falls_back(self):
        self.credentials()
        with patch.object(identity, 'github_app_auth', side_effect=identity.GitHubAppError('token request denied')), patch('subprocess.run') as run:
            result = api.call_gh_with_retry('GET', '/repos/example/app/issues', operation='github.plan.index')
        self.assertFalse(result.ok)
        self.assertEqual(result.failure.cause, 'identity_refused')
        run.assert_not_called()

    def test_reader_auth_failure_is_structured_at_reader_and_planner_boundaries(self):
        self.credentials()
        with patch.object(identity, 'github_app_auth', side_effect=identity.GitHubAppError('token request denied')), patch('subprocess.run') as run:
            reader = github_read.GitHubReader(operation='github.plan.index', cache_enabled=True)
            with self.assertRaises(github_read.GitHubReadError) as failure:
                reader.request('GET', '/repos/example/app/issues', step='read')
        self.assertEqual(failure.exception.result.failure.cause, 'identity_refused')
        run.assert_not_called()
        env = {**os.environ, 'GITHUB_READER_APP_PRIVATE_KEY_PATH': str(self.root / 'missing-key')}
        result = subprocess.run([sys.executable, str(Path(api.__file__).with_name('gh-plan.py')),
                                 '--repo', 'example/app', 'index'], env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['error_code'], 'identity_refused')
        self.assertNotIn('Traceback', result.stderr)

    def test_legacy_tokens_own_user_projects_and_explicit_actor_keep_their_routes(self):
        arguments = dict(operation='github.plan.index', is_write=False, gh_cmd=api.DEFAULT_GH,
                         gh_prefix_args=[], actor='main-app[bot]', expected_actor='main-app[bot]')
        for suffix in ('ID', 'INSTALLATION_ID', 'PRIVATE_KEY_PATH'):
            os.environ.pop(f'GITHUB_APP_{suffix}')
        self.assertEqual(api.request_identity(**arguments), ('main-app[bot]', 'main-app[bot]', []))
        self.credentials()
        with patch.object(identity, 'github_app_auth') as auth:
            with patch.dict(os.environ, {'GH_WITH_ENV_TOKEN_OWN_USER': '1'}):
                self.assertEqual(api.request_identity(**arguments)[2], [])
            self.assertEqual(api.request_identity(**{**arguments, 'operation': 'github.plan.project_list',
                                                     'preserve_identity': True})[2], [])
            os.environ.pop('CODEX_AUTOMATION_LOGIN')
            self.assertEqual(api.request_identity(**{**arguments, 'actor': 'other-user', 'expected_actor': 'other-user'}),
                             ('other-user', 'other-user', []))
        auth.assert_not_called()

    def test_main_app_opt_out_keeps_audit_metadata_and_probes_on_one_identity(self):
        from test_github_capabilities import cli, capabilities
        config = self.credentials()
        observed = []
        def membership(reader, *_args, **_kwargs):
            observed.append(reader.expected_actor)
            return [{'full_name': 'example/app'}]
        installation = {'actor': 'main-app[bot]', 'suspended': False,
                        'permissions': capabilities.permission_profile(capabilities.load_matrix())['permissions']['repository']}
        with patch.object(identity, 'github_app_config', return_value=config), patch.object(
                identity, 'github_app_installation_metadata', return_value=installation), patch.object(
                identity, 'github_app_auth', return_value=('synthetic', 'reader-app[bot]')) as auth, patch.object(
                github_read.GitHubReader, 'paged_json', autospec=True, side_effect=membership), patch.object(
                capabilities, 'audit_repository', return_value={'state': 'audited'}):
            result = cli.run_audit(SimpleNamespace(refresh_token=False, all_installed=False, repo=['example/app']),
                                   capabilities.load_matrix())
        auth.assert_not_called()
        self.assertEqual(observed, [installation['actor']])
        self.assertEqual(result['installation']['actor'], installation['actor'])

    def test_planner_derives_project_identity_preservation_without_excluding_issue_reads(self):
        self.credentials()
        spec = importlib.util.spec_from_file_location('reader_identity_plan', Path(api.__file__).with_name('gh-plan.py'))
        planner = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = planner
        self.addCleanup(sys.modules.pop, spec.name)
        spec.loader.exec_module(planner)
        response = subprocess.CompletedProcess([], 0, '{}', '')
        with patch.object(identity, 'github_app_auth', return_value=('synthetic', 'reader-app[bot]')) as auth, patch(
                'subprocess.run', return_value=response) as run:
            planner.run_raw(['project', 'list', '--owner', 'example', '--format', 'json'],
                            operation='github.plan.next', prefer_active=True, bucket='graphql')
            auth.assert_not_called()
            self.assertNotIn('--reader', run.call_args.args[0])
            planner.run_raw(['api', '/repos/example/app/issues'], operation='github.plan.next', bucket='rest_core')
            self.assertIn('--reader', run.call_args.args[0])
            self.assertEqual(auth.call_count, 1)

    def test_watcher_delegates_bulk_context_to_pr_reads_without_changing_writes(self):
        self.credentials()
        modules = {}
        for name, path in [('reader_identity_pr', Path(api.__file__).with_name('gh-pr.py')),
                ('reader_identity_watch', Path(api.__file__).parents[2] / 'babysit-pr/scripts/gh_pr_watch.py')]:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            self.addCleanup(sys.modules.pop, name)
            # Delegation does not use the watcher's unrelated YAML settings.
            with patch.dict(sys.modules, {'yaml': SimpleNamespace()}):
                spec.loader.exec_module(module)
            modules[name] = module
        helper, watcher = modules.values()
        observed = []
        args = SimpleNamespace(repo='example/app', pr='7')
        def get_pr(reader, *_args, **_kwargs):
            observed.append(reader.expected_actor)
            return {'number': 7}
        def checks(reader, *_args, **_kwargs):
            observed.append(reader.expected_actor)
            return {'pr': {'number': 7}, 'summary': {'countsComplete': True, 'countsAreLowerBounds': False}}
        def delegate(command, **kwargs):
            with patch.dict(os.environ, kwargs['env']), patch.object(
                    identity, 'github_app_auth', return_value=('synthetic', 'reader-app[bot]')), patch.object(
                    github_read.GitHubReader, 'get_json', autospec=True, side_effect=get_pr), patch.object(
                    github_read, 'pull_request_checks', side_effect=checks):
                helper.CURRENT_OPERATION = f'github.pr.{command[-2]}'
                result = helper.cmd_view(args) if command[-2] == 'view' else helper.cmd_checks(args)
                os.environ.pop('GH_PR_READ_CONTEXT', None)
                standalone = helper.cmd_view(args) if command[-2] == 'view' else helper.cmd_checks(args)
                self.assertEqual(standalone['expected_actor'], 'main-app[bot]')
                # An inherited watch hint cannot change a write/preflight context.
                os.environ['GH_PR_READ_CONTEXT'] = 'watch'
                helper.CURRENT_OPERATION = 'github.pr.comment'
                self.assertEqual(helper.read_operation(), helper.CURRENT_OPERATION)
            return subprocess.CompletedProcess(command, 0, json.dumps(result), '')
        with patch.object(watcher, 'PR_HELPER', str(Path(api.__file__).with_name('gh-pr.py'))), patch(
                'subprocess.run', side_effect=delegate):
            for command in ('view', 'checks'):
                result = watcher.pr_helper_json(command, '7', repo='example/app')
                self.assertEqual(result['expected_actor'], 'reader-app[bot]')
        self.assertEqual(observed, ['reader-app[bot]', 'main-app[bot]', 'reader-app[bot]', 'main-app[bot]'])
        with patch.object(identity, 'github_app_auth', return_value=('synthetic', 'reader-app[bot]')) as auth:
            preflight = watcher.watcher_reader(operation=watcher.RERUN_READ_OPERATION)
        auth.assert_not_called()
        self.assertEqual(preflight.expected_actor, 'main-app[bot]')
        clock = [1000.0]
        runtime = api.RetryRuntime(now=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                                   jitter=lambda _: 0, progress=lambda _: None)
        with patch.object(api, 'default_retry_runtime', return_value=runtime), patch('subprocess.run', side_effect=[
                self.response('main-app[bot]', status=502), self.response('main-app[bot]')]) as run:
            result = preflight.request('GET', '/repos/example/app/actions/runs', step='preflight')
        self.assertTrue(result.ok)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result.retry_summary.attempts, 2)

    def test_synthetic_path_mints_through_existing_app_auth_and_cache(self):
        config = self.credentials()
        requests = []
        def respond(request, *, operation):
            requests.append((request.full_url, request.method))
            # A generated test key signed the JWT. Nothing reads a real key.
            jwt = request.get_header('Authorization').split(' ', 1)[1]
            self.assertEqual(len(jwt.split('.')), 3)
            if request.method == 'GET':
                return {'slug': 'reader-app'}
            return {'token': 'synthetic-installation-token', 'expires_at': '2030-01-01T00:00:00Z'}
        with patch.object(identity, '_request_json', side_effect=respond), contextlib.redirect_stdout(io.StringIO()) as output:
            first = identity.github_app_auth(config, now=1000)
            second = identity.github_app_auth(config, now=1000)
        self.assertEqual(first, second)
        self.assertEqual(first[1], 'reader-app[bot]')
        self.assertEqual([method for _, method in requests], ['GET', 'POST'])
        self.assertTrue(requests[-1][0].endswith('/app/installations/76543/access_tokens'))
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(next((self.root / 'tokens').glob('*.json')).stat().st_mode & 0o777, 0o600)

    def test_reserve_and_receipts_use_reader_budget(self):
        self.credentials()
        now = 1000.0
        def record(actor, remaining):
            usage.record_response(method='GET', path='/repos/example/app/issues', status=200,
                headers={'x-ratelimit-limit': '100', 'x-ratelimit-remaining': str(remaining),
                         'x-ratelimit-reset': '2000'}, operation='github.plan.index', actor=actor, now=now)
        record('main-app[bot]', 1)
        record('reader-app[bot]', 90)
        runtime = api.RetryRuntime(now=lambda: now, sleep=lambda _: None, jitter=lambda _: 0, progress=lambda _: None)
        with patch.dict(os.environ, {'GITHUB_QUOTA_RESERVE_NO_WAIT': '1'}), patch.object(identity, 'github_app_auth', return_value=('synthetic', 'reader-app[bot]')), patch('subprocess.run', return_value=self.response()) as run:
            result = api.call_gh_with_retry('GET', '/repos/example/app/issues', operation='github.plan.index',
                                            expected_actor='main-app[bot]', retry_runtime=runtime)
            self.assertTrue(result.ok)
            self.assertEqual(result.retry_summary.last_actor, 'reader-app[bot]')
            record('reader-app[bot]', 1)
            stopped = api.call_gh_with_retry('GET', '/repos/example/app/issues', operation='github.plan.index',
                                             expected_actor='main-app[bot]', retry_runtime=runtime)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(stopped.failure.cause, 'quota_reserve')
        self.assertEqual(usage.quota_snapshot(actor='main-app[bot]', repository='example/app')['remaining'], 1)
        report = usage.report(since=now, now=now)
        self.assertEqual({row['actor'] for row in report['consumers']}, {'main-app[bot]', 'reader-app[bot]'})

    def test_cache_and_strict_reader_checks_use_selected_identity(self):
        self.credentials()
        with patch.object(identity, 'github_app_auth', return_value=('synthetic', 'reader-app[bot]')), patch('subprocess.run', return_value=self.response(headers={'etag': '"reader"'})):
            reader = github_read.GitHubReader(operation='github.plan.index', expected_actor='main-app[bot]',
                                               cache_enabled=True, cache_revalidate=True, strict_actor=True)
            self.assertTrue(reader.request('GET', '/repos/example/app/issues', step='first').ok)
        self.assertFalse(reader.degraded_reasons)
        self.assertEqual(reader.expected_actor, 'reader-app[bot]')
        with patch('subprocess.run', return_value=self.response('main-app[bot]', headers={'etag': '"main"'})) as run:
            api.call_gh('GET', '/repos/example/app/issues', actor='main-app[bot]', expected_actor='main-app[bot]')
        self.assertFalse(any(str(arg).startswith('If-None-Match:') for arg in run.call_args.args[0]))

    def test_essential_request_override_uses_main_then_bulk_reader_continues(self):
        self.credentials()
        with patch.object(identity, 'github_app_auth', return_value=('synthetic', 'reader-app[bot]')), patch(
                'subprocess.run', side_effect=[self.response('main-app[bot]', body={'data': {}}), self.response()]) as run:
            reader = github_read.GitHubReader(operation='github.pr.watch', strict_actor=True)
            essential = reader.graphql_json('query { viewer { login } }', {},
                step='readiness', operation='github.pr.review_readiness')
            bulk = reader.request('GET', '/repos/example/app/issues/7/comments', step='poll')
        self.assertTrue(essential.ok)
        self.assertEqual(essential.expected_actor, 'main-app[bot]')
        self.assertNotIn('--reader', run.call_args_list[0].args[0])
        self.assertTrue(bulk.ok)
        self.assertIn('--reader', run.call_args_list[1].args[0])
        self.assertEqual(reader.expected_actor, 'reader-app[bot]')
        self.assertFalse(reader.degraded_reasons)

    def test_wrapper_refuses_reader_writes_and_missing_auth_before_delegating(self):
        wrapper = Path(api.__file__).with_name('gh-with-env-token')
        fake_gh = self.root / 'gh'
        marker = self.root / 'called'
        fake_gh.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        fake_gh.chmod(0o700)
        env = {**os.environ, 'GH_WITH_ENV_TOKEN_PYTHON': sys.executable, 'GH_WITH_ENV_TOKEN_GH': str(fake_gh),
               'GH_WITH_ENV_TOKEN_ALLOW_ACTIVE_AUTH_FALLBACK': '1', 'GH_TOKEN': 'synthetic-personal-token'}
        for suffix in ('ID', 'INSTALLATION_ID', 'PRIVATE_KEY_PATH'):
            env.pop(f'GITHUB_APP_{suffix}')
        for args, body in [(['--reader', 'api', '/repos/example/app/issues', '-X', 'POST'], ''),
                (['--reader', 'api', 'graphql', '--input', '-'], '{"query":"mutation { x }"}'),
                (['--reader', 'api', 'graphql', '--input', '-'], '{"query":"query Read { x } mutation Write { y }", "operationName":"Write"}'),
                (['--reader', 'pr', 'merge', '1'], ''),
                (['--reader', 'api', '/repos/example/app/issues'], ''),
                (['--main-app-only', 'api', '/repos/example/app/issues'], '')]:
            result = subprocess.run([str(wrapper), *args], input=body, text=True, capture_output=True, env=env)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertFalse(marker.exists())

    def test_wrapper_delegates_rest_and_graphql_reads_with_reader_token(self):
        wrapper = Path(api.__file__).with_name('gh-with-env-token')
        fake_identity = self.root / 'identity.py'
        fake_identity.write_text("import sys\nassert sys.argv[1] == '--reader'\nprint('reader-app[bot]')\nif sys.argv[2] == 'app-auth': print('synthetic-reader-token')\n")
        fake_gh = self.root / 'gh'
        fake_gh.write_text('#!/bin/sh\n[ "$GH_TOKEN" = synthetic-reader-token ] || exit 9\nprintf \'{"ok":true}\\n\'\n')
        fake_gh.chmod(0o700)
        env = {**os.environ, 'GITHUB_READER_APP_ID': '54321', 'GITHUB_READER_APP_INSTALLATION_ID': '76543',
               'GITHUB_READER_APP_PRIVATE_KEY_PATH': str(self.root / 'unused-synthetic-path'),
               'GH_WITH_ENV_TOKEN_PYTHON': sys.executable, 'GH_WITH_ENV_TOKEN_GH': str(fake_gh),
               'GH_WITH_ENV_TOKEN_IDENTITY_HELPER': str(fake_identity)}
        for args, body in [(['api', '/repos/example/app/issues'], ''),
                (['api', 'graphql', '--input', '-'], '{"query":"query { viewer { login } }"}'),
                (['api', 'graphql', '--field=query=query { viewer { login } }'], ''),
                (['repo', 'view', 'example/app', '--json', 'name'], '')]:
            result = subprocess.run([str(wrapper), '--reader', *args], input=body, text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, f'{args}: {result.stderr}')
            self.assertEqual(json.loads(result.stdout), {'ok': True})
        checked = subprocess.run([str(wrapper), '--reader', '--check'], text=True, capture_output=True, env=env)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn('reader-app[bot]', checked.stdout)

    def test_graphql_strings_comments_and_fragments_are_reads(self):
        for document in ['query { node(id: "mutation") { id } }',
                '# mutation\nquery Q($mutation: ID!) { node(id:$mutation) { id } }',
                'query { viewer { ...mutation } } fragment mutation on User { login }']:
            self.assertFalse(api.reader_request_is_write('POST', '/graphql', {'query': document}))


if __name__ == '__main__':
    unittest.main()
