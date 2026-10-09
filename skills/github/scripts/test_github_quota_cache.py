#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Transport regressions for quota admission and shared conditional bodies."""
from __future__ import annotations

import json
import itertools
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ['CODEX_SKILLS_ENV_FILE'] = '/definitely/missing/test.env'
os.environ['CODEX_AUTOMATION_LOGIN'] = 'fixture-bot'
import github_api as api
import github_http_cache as cache
import github_request_usage as usage


REQUEST_IDS = itertools.count()


class QuotaCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {'GITHUB_RETRY_STATE_DIR': str(self.root),
                                              'GITHUB_HTTP_CACHE_DIR': str(self.root / 'cache')})
        environment.start()
        self.addCleanup(environment.stop)
        self.clock = 1000.0
        self.waits = []

    def sleep(self, seconds):
        self.clock += seconds
        self.waits.append(seconds)

    def runtime(self):
        return api.RetryRuntime(now=lambda: self.clock, sleep=self.sleep, jitter=lambda _: 0,
                                progress=lambda _: None)

    def budget(self, remaining, *, owner='example', reset=1010):
        usage.record_response(method='GET', path=f'/repos/{owner}/app/issues', status=200,
                              headers={'x-ratelimit-limit': '100', 'x-ratelimit-remaining': str(remaining),
                                       'x-ratelimit-reset': str(reset), 'x-ratelimit-resource': 'core'},
                              operation='github.plan.index', actor='fixture-bot', now=self.clock)

    def execute(self, operation, *, write=False, repository='example/app', deadline=None, cancelled=None):
        calls = []
        runtime = self.runtime()
        if cancelled:
            runtime.cancelled = cancelled
        result = api.run_with_retry(
            lambda: calls.append(self.clock) or api.ApiResult(ok=True, status=200, body=None, actor='fixture-bot',
                                                             expected_actor='fixture-bot', bucket='rest_core'),
            operation=operation, is_write=write, actor='fixture-bot', expected_actor='fixture-bot',
            bucket='rest_core', repository=repository,
            retry_policy=api.RetryPolicy(state_dir=self.root, max_wait_seconds=30, jitter_seconds=0,
                                         drain_seconds=0), retry_runtime=runtime, deadline_at=deadline,
        )
        return result, calls

    def test_reserve_boundary_and_essential_routes(self):
        self.budget(25)
        self.assertEqual(self.execute('github.plan.index')[1], [1000])
        self.budget(24)
        self.assertEqual(self.execute('github.plan.show')[1], [1000])
        self.assertEqual(self.execute('github.train.drive')[1], [1000])
        self.assertEqual(self.execute('github.plan.index', write=True)[1], [1000])
        self.assertEqual(self.execute('github.plan.index', repository='other/app')[1], [1000])
        result, calls = self.execute('github.plan.index')
        self.assertTrue(result.ok)
        self.assertEqual(calls, [1010])
        self.assertEqual(sum(self.waits), 10)
        self.assertEqual(result.retry_summary.elapsed_wait, 10)

    def test_short_deadline_and_no_wait_name_reserve_and_reset(self):
        self.budget(24)
        result, calls = self.execute('github.pr.watch', deadline=1005)
        self.assertFalse(result.ok)
        self.assertEqual(calls, [])
        self.assertIn('reserve 25/100', result.failure.message)
        self.assertIn('reset=', result.failure.message)
        with patch.dict(os.environ, {'GITHUB_QUOTA_RESERVE_NO_WAIT': '1'}):
            result, calls = self.execute('github.plan.index')
        self.assertEqual(result.failure.cause, 'quota_reserve')
        self.assertEqual(calls, [])
        self.assertEqual(self.waits, [])

    def test_cancellation_and_expired_budget(self):
        self.budget(24)
        result, calls = self.execute('github.plan.index', cancelled=lambda: self.clock >= 1002)
        self.assertFalse(result.ok)
        self.assertEqual(calls, [])
        self.assertEqual(sum(self.waits), 2)
        self.clock = 1011
        self.assertTrue(self.execute('github.plan.index')[0].ok)

    @staticmethod
    def response(body, *, status=200, headers=None, actor='fixture-bot'):
        values = {'content-type': 'application/json', 'x-github-request-id': f'fixture-{next(REQUEST_IDS)}', **(headers or {})}
        raw = f'HTTP/2.0 {status}\n' + ''.join(f'{key}: {value}\n' for key, value in values.items()) + '\n'
        if body is not None:
            raw += json.dumps(body)
        return subprocess.CompletedProcess([], int(status >= 300), raw.encode(),
                                           f'acting as {actor}'.encode())

    def get(self, path='/repos/example/app/issues/7', **kwargs):
        return api.call_gh('GET', path, actor='fixture-bot', expected_actor='fixture-bot', **kwargs)

    def test_304_reuses_body_and_links_only_after_network_response(self):
        body = {'id': 7}
        responses = [self.response(body, headers={'etag': '"first"', 'link': '<next>; rel="next"'}),
                     self.response(None, status=304)]
        with patch('subprocess.run', side_effect=responses) as run:
            first, second = self.get(), self.get()
        self.assertTrue(second.ok)
        self.assertEqual(first.body, second.body)
        self.assertEqual(second.status, 304)
        self.assertEqual(second.headers['link'], '<next>; rel="next"')
        self.assertIsNone(second.failure)
        self.assertIn('If-None-Match: "first"', run.call_args_list[1].args[0])
        self.assertEqual(run.call_count, 2)
        self.assertEqual((self.root / 'cache').stat().st_mode & 0o777, 0o700)
        self.assertEqual(next((self.root / 'cache').glob('*.json')).stat().st_mode & 0o777, 0o600)

    def test_modified_since_identity_url_and_representation_isolation(self):
        response = self.response({'id': 7}, headers={'last-modified': 'Wed, 07 Oct 2026 12:00:00 GMT'})
        with patch('subprocess.run', return_value=response) as run:
            self.get()
            self.get()
            self.get('/repos/example/app/issues/8')
            self.get(extra_headers={'Accept': 'other'})
            api.call_gh('GET', '/repos/example/app/issues/7', actor='other', expected_actor='other')
            self.get(host='other.invalid')
        self.assertTrue(any(str(arg).startswith('If-Modified-Since:') for arg in run.call_args_list[1].args[0]))
        for call in run.call_args_list[2:]:
            self.assertFalse(any(str(arg).startswith(('If-Modified-Since:', 'If-None-Match:')) for arg in call.args[0]))

    def test_auth_errors_missing_body_and_tokens_do_not_become_cached_success(self):
        with patch('subprocess.run', side_effect=[self.response({'id': 7}, headers={'etag': '"e"'}),
                                                self.response({'message': 'denied'}, status=403)]):
            self.get()
            self.assertFalse(self.get().ok)
        for entry in (self.root / 'cache').glob('*.json'):
            entry.write_text('{broken')
        with patch('subprocess.run', return_value=self.response(None, status=304)):
            self.assertFalse(self.get().ok)
        with patch('subprocess.run', return_value=self.response({'token': 'private'}, headers={'etag': '"e"'})):
            self.assertTrue(self.get().ok)
        self.assertEqual(list((self.root / 'cache').glob('*.json')), [])

    def test_cache_storage_failure_does_not_repeat_completed_get(self):
        response = self.response({'id': 7}, headers={'etag': '"e"'})
        with patch('subprocess.run', return_value=response) as run, patch.object(cache, '_publish', side_effect=OSError):
            self.assertTrue(self.get().ok)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(list((self.root / 'cache').glob('*.json')), [])

    def test_bounded_cache_and_oversized_responses(self):
        with patch.object(cache, 'MAX_ENTRIES', 2), patch('subprocess.run', return_value=self.response({'id': 7}, headers={'etag': '"e"'})):
            for number in range(5):
                self.get(f'/repos/example/app/issues/{number}')
        self.assertLessEqual(len(list((self.root / 'cache').glob('*.json'))), 2)
        with patch.object(cache, 'MAX_ENTRY_BYTES', 30), patch('subprocess.run', return_value=self.response({'big': 'x' * 100}, headers={'etag': '"e"'})):
            self.get('/repos/example/app/issues/large')
        self.assertLessEqual(len(list((self.root / 'cache').glob('*.json'))), 2)

    def test_reader_cache_keeps_one_request_per_revalidation(self):
        import github_read
        headers = {"Accept": "application/vnd.github+json"}
        def respond(command, **_kwargs):
            conditional = any(str(arg).startswith("If-None-Match:") for arg in command)
            return self.response(None if conditional else {"id": 7},
                                 status=304 if conditional else 200,
                                 headers={"etag": '\"e\"'})
        with patch.dict(os.environ, {"GITHUB_READ_CACHE_DIR": str(self.root / "reader-cache")}), patch(
                "subprocess.run", side_effect=respond) as run:
            self.get(extra_headers=headers)
            reader = github_read.GitHubReader(actor="fixture-bot", expected_actor="fixture-bot",
                                              cache_enabled=True, cache_revalidate=True)
            initial = reader.request("GET", "/repos/example/app/issues/7", step="initial")
            repeat = reader.request("GET", "/repos/example/app/issues/7", step="repeat")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(initial.status, 200)
        self.assertEqual(repeat.status, 304)
        self.assertEqual(initial.body, repeat.body)

    def test_retry_then_reserve_preserves_waits_and_attempt_diagnostics(self):
        self.budget(25)
        calls = []
        def attempt():
            calls.append(self.clock)
            if len(calls) > 1:
                return api.ApiResult(ok=True, status=200, body={}, actor="fixture-bot", bucket="rest_core")
            return api.ApiResult(ok=False, status=503, body={}, actor="fixture-bot",
                                 expected_actor="fixture-bot", bucket="rest_core", request_id="first-attempt",
                                 failure=api.FailureDetail(cause="network_provider_failure", message="unavailable",
                                                          retryable=True, fallback_eligible=False, disposition="retry"))
        runtime = self.runtime()
        def sleep_and_observe(seconds):
            self.sleep(seconds)
            self.budget(24)
        runtime.sleep = sleep_and_observe
        def run(deadline=None):
            return api.run_with_retry(attempt, operation="github.plan.index", is_write=False,
                                      actor="fixture-bot", expected_actor="fixture-bot", bucket="rest_core",
                                      repository="example/app", retry_runtime=runtime,
                                      retry_policy=api.RetryPolicy(state_dir=self.root, max_wait_seconds=30,
                                                                   jitter_seconds=0, drain_seconds=0),
                                      deadline_at=deadline)
        result = run()
        self.assertTrue(result.ok)
        self.assertEqual(result.retry_summary.elapsed_wait, sum(self.waits))
        self.assertEqual(result.retry_summary.attempts, 2)
        self.clock, self.waits, calls = 1000.0, [], []
        # The second scenario starts with fresh fixture cooldown state.
        for state_file in self.root.glob("*.json"):
            state_file.unlink()
        usage._budget_path("github.com", "fixture-bot", "rest_core", "example/app").unlink()
        self.budget(25)
        result = run(deadline=1005)
        self.assertFalse(result.ok)
        self.assertEqual(result.retry_summary.attempts, 1)
        self.assertEqual(result.request_id, "first-attempt")
        self.assertEqual(result.retry_summary.elapsed_wait, sum(self.waits))

    def test_cli_repo_flag_selects_the_installation_reserve(self):
        self.budget(24)
        with patch.dict(os.environ, {"GITHUB_QUOTA_RESERVE_NO_WAIT": "1"}):
            for args in (["issue", "list", "-R", "example/app"],
                         ["issue", "list", "--repo=example/app"],
                         ["api", "/repos/example/app/issues"]):
                result, calls = self.execute("github.plan.index", repository=cache.repository_from_command(args))
                self.assertFalse(result.ok)
                self.assertEqual(calls, [])

    def test_representative_three_page_sweep_cost(self):
        # Two unchanged passes still contact all three pages. GitHub returns
        # free 304s on pass two, reducing charged requests from six to three.
        responses = [self.response([{'id': page}], headers={'etag': f'"page-{page}"'}) for page in range(3)]
        responses += [self.response(None, status=304) for _ in range(3)]
        with patch('subprocess.run', side_effect=responses) as run:
            first = [self.get(f'/repos/example/app/issues?page={page}').body for page in range(3)]
            second = [self.get(f'/repos/example/app/issues?page={page}').body for page in range(3)]
        self.assertEqual(first, second)
        self.assertEqual(run.call_count, 6)
        report = usage.report(since=time.time() - 60)
        self.assertEqual(sum(row['http_requests'] for row in report['consumers']), 6)
        self.assertEqual(sum(row['primary_requests'] for row in report['consumers']), 3)


if __name__ == '__main__':
    unittest.main()
