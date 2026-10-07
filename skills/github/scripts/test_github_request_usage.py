#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise per-attempt accounting, private storage and cross-process pressure."""

import os
import pathlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/usage-fixture.env"
os.environ["CODEX_AUTOMATION_LOGIN"] = "fixture-bot"

import github_api
import github_read
import github_request_usage as usage


def reply(status=200, *, remaining=4000, reset=7200):
    return subprocess.CompletedProcess([], int(status >= 300), stdout=(
        f'HTTP/2 {status}\nx-ratelimit-limit: 5000\nx-ratelimit-remaining: {remaining}\n'
        f'x-ratelimit-reset: {reset}\nx-ratelimit-resource: core\n'
        f'x-github-request-id: r{status}\n\n{{"secret": "must-not-be-recorded"}}'
    ).encode(), stderr=b"GitHub automation actor: fixture-bot (source: github_app)")


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.environment = patch.dict(os.environ, {"GITHUB_RETRY_STATE_DIR": self.directory.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_real_transport_attempts_distinguish_304_and_retry_from_local_refusal(self):
        with patch("subprocess.run", side_effect=[reply(), reply(304), reply(403, remaining=0)]), patch(
            "github_request_usage.time.time", return_value=1000
        ), patch.dict(os.environ, {"GITHUB_REQUEST_CALLER": "/path/gh_pr_watch.py"}):
            for _ in range(3):
                github_api.call_gh("GET", "/repos/example/app/pulls/7?token=private",
                                   operation="github.pr.watch", expected_actor="fixture-bot")
            github_api.call_gh("POST", "/repos/example/app/issues", actor="wrong", expected_actor="fixture-bot")
        consumers = usage.report(since=0, now=1100)["consumers"]
        self.assertEqual(len(consumers), 1)
        self.assertEqual(consumers[0]["http_requests"], 3)
        self.assertEqual(consumers[0]["primary_requests"], 1)
        self.assertEqual(consumers[0]["not_modified"], 1)
        self.assertEqual((consumers[0]["helper"], consumers[0]["repository"]), ("gh_pr_watch.py", "example/app"))
        ledger = next(pathlib.Path(self.directory.name).rglob("*.jsonl"))
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("private", ledger.read_text())
        self.assertNotIn("must-not-be-recorded", ledger.read_text())
        self.assertNotIn("token", ledger.read_text())

    def test_shared_budget_survives_stale_replies_and_expires_at_reset(self):
        for remaining in (800, 4000):
            usage.record_response(method="GET", path="/repos/example/app", status=200,
                                  headers={"x-ratelimit-limit": "5000", "x-ratelimit-remaining": str(remaining),
                                           "x-ratelimit-reset": "7200"},
                                  operation="github.read", actor="fixture-bot", now=1000)
        usage.record_response(method="GET", path="/repos/different-owner/app", status=200,
                              headers={"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "5000", "x-ratelimit-reset": "7300"},
                              operation="github.read", actor="fixture-bot", now=1000)
        self.assertEqual(usage.polling_floor(actor="fixture-bot", repository="example/app", now=1000), 300)
        self.assertEqual(usage.polling_floor(actor="fixture-bot", repository="example/other", now=1000), 300)
        self.assertEqual(usage.polling_floor(actor="fixture-bot", repository="different-owner/app", now=1000), 0)
        self.assertEqual(usage.polling_floor(actor="fixture-bot", repository="example/app", now=7200), 0)
        self.assertEqual(usage.polling_floor(actor="other", repository="example/app", now=1000), 0)
        command = [sys.executable, "-c", "import github_request_usage as u; print(u.polling_floor(actor='fixture-bot', repository='example/app', now=1000))"]
        child = subprocess.run(command, cwd=pathlib.Path(__file__).parent, capture_output=True, text=True, check=True)
        self.assertEqual(float(child.stdout), 300)

    def test_low_budget_slows_pollers_without_delaying_a_write(self):
        with patch("subprocess.run", return_value=reply(remaining=10, reset=2_000_000_000)) as transport:
            github_api.call_gh("GET", "/repos/example/app", expected_actor="fixture-bot")
            with patch("github_read.random.uniform", return_value=0):
                self.assertEqual(github_read.poll_delay(60, repository="example/app"), 300)
            result = github_api.call_gh("POST", "/repos/example/app/issues", {"title": "new"}, expected_actor="fixture-bot")
        self.assertTrue(result.ok)
        self.assertEqual(transport.call_count, 2)

    def test_enterprise_host_and_non_core_bucket_are_separate(self):
        for resource, remaining, limit in (("core", 4000, 5000), ("code_search", 0, 10)):
            usage.record_response(method="GET", path="/repos/example/app", status=200, host="enterprise.example.invalid",
                                  headers={"x-ratelimit-resource": resource, "x-ratelimit-limit": str(limit),
                                           "x-ratelimit-remaining": str(remaining), "x-ratelimit-reset": "7200"},
                                  operation="github.read", actor="fixture-bot", now=1000)
        with patch.dict(os.environ, {"GH_HOST": "enterprise.example.invalid"}):
            self.assertEqual(usage.polling_floor(actor="fixture-bot", repository="example/app", now=1000), 0)
            usage.record_response(method="GET", path="/repos/example/app", status=200, host="enterprise.example.invalid",
                                  headers={"x-ratelimit-resource": "core", "x-ratelimit-limit": "5000",
                                           "x-ratelimit-remaining": "800", "x-ratelimit-reset": "7200"},
                                  operation="github.read", actor="fixture-bot", now=1000)
            self.assertEqual(usage.polling_floor(actor="fixture-bot", repository="example/app", now=1000), 300)
            self.assertEqual(usage.polling_floor(host="github.com", actor="fixture-bot", repository="example/app", now=1000), 0)

    def test_accounting_failure_leaves_transport_success_intact(self):
        with patch("github_request_usage._private_open", side_effect=OSError("disk full")), patch(
            "subprocess.run", return_value=reply()
        ):
            self.assertTrue(github_api.call_gh("GET", "/repos/example/app", expected_actor="fixture-bot").ok)

    def test_synthetic_actor_response_succeeds_without_spending_http_or_quota(self):
        response = subprocess.CompletedProcess([], 0, stdout=(
            b'HTTP/2 200\ncontent-type: application/json\n'
            b'x-codex-synthetic-response: app-actor\n\n{"login":"fixture-bot"}'
        ), stderr=b"GitHub automation actor: fixture-bot (source: github_app)")
        with patch("subprocess.run", return_value=response):
            result = github_api.call_gh("GET", "/user", expected_actor="fixture-bot")
        self.assertTrue(result.ok)
        self.assertEqual(result.body, {"login": "fixture-bot"})
        self.assertEqual(usage.report(since=0)["consumers"], [])
        self.assertEqual(usage.quota_snapshot(actor="fixture-bot"), {})

    def test_simultaneous_helpers_append_complete_records(self):
        source = (
            "import github_request_usage as u; "
            "[u.record_response(method='GET',path='/repos/example/app',status=200,headers={},"
            "operation='github.read',actor='fixture-bot',now=1000) for _ in range(20)]"
        )
        children = [subprocess.Popen([sys.executable, "-c", source], cwd=pathlib.Path(__file__).parent) for _ in range(4)]
        for child in children:
            self.assertEqual(child.wait(timeout=15), 0)
        result = usage.report(since=0, now=1100)
        self.assertEqual(result["unreadable_records"], 0)
        self.assertEqual(sum(item["http_requests"] for item in result["consumers"]), 80)

    def test_graphql_cost_and_missing_receipts_remain_explicit(self):
        usage.record_response(method="POST", path="/graphql", status=200,
                              headers={"x-ratelimit-resource": "graphql"}, operation="github.pr.review_threads",
                              actor="fixture-bot", now=1000)
        consumer = usage.report(since=0, now=1100)["consumers"][0]
        self.assertEqual((consumer["primary_requests"], consumer["unknown_cost"]), (0, 1))

    def test_rate_limit_probe_is_free_and_actor_filter_keeps_app_usage_separate(self):
        usage.record_response(method="GET", path="/rate_limit", status=200, headers={},
                              operation="github.api.rate_limit", actor="fixture-bot", now=1000)
        usage.record_response(method="GET", path="/repos/example/app", status=200, headers={},
                              operation="github.pr.watch", actor="other", now=1000)
        consumers = usage.report(since=0, now=1100, actor="fixture-bot")["consumers"]
        self.assertEqual(len(consumers), 1)
        self.assertEqual((consumers[0]["http_requests"], consumers[0]["primary_requests"]), (1, 0))

    def test_fleet_report_combines_ledgers_without_counting_copied_receipts_twice(self):
        first = pathlib.Path(self.directory.name)
        with tempfile.TemporaryDirectory() as other:
            second = pathlib.Path(other)
            with patch.dict(os.environ, {"GITHUB_REQUEST_SESSION": "session-a"}):
                usage.record_response(method="GET", path="/repos/example/app", status=200,
                                      headers={"x-github-request-id": "request-a"},
                                      operation="github.pr.watch", actor="fixture-bot", now=1000)
            shutil.copytree(first / "request-usage", second / "request-usage")
            with patch.dict(os.environ, {"GITHUB_RETRY_STATE_DIR": other, "GITHUB_REQUEST_CALLER": "launchplane-train-drive.py",
                                       "GITHUB_REQUEST_SESSION": "driver-a"}):
                usage.record_response(method="GET", path="/repos/example/app/pulls/7", status=304,
                                      headers={"x-github-request-id": "request-b"},
                                      operation="github.train.drive", actor="fixture-bot", now=1001)
            result = usage.report(since=900, now=1100, actor="fixture-bot", state_dirs=[first, second, first])
        self.assertEqual(result["duplicate_records"], 1)
        self.assertEqual(sum(row["http_requests"] for row in result["consumers"]), 2)
        self.assertEqual(sum(row["primary_requests"] for row in result["consumers"]), 1)
        self.assertEqual({row["session"] for row in result["sessions"]}, {"session-a", "driver-a"})
        self.assertIn("launchplane-train-drive.py", {row["helper"] for row in result["consumers"]})
        self.assertIsNone(result["session_ceiling"])

    def test_quota_windows_keep_peak_headers_separate_from_local_cost_and_scope(self):
        for request, (path, host, actor, resource, reset, used, status) in enumerate([
            ("/repos/example/app", "github.com", "fixture-bot", "core", 3600, 4100, 200),
            ("/repos/example/other", "github.com", "fixture-bot", "core", 3600, 4000, 304),
            ("/repos/example/app", "github.com", "fixture-bot", "core", 7200, 4, 200),
            ("/repos/elsewhere/app", "github.com", "fixture-bot", "core", 3600, 50, 200),
            ("/repos/example/app", "elsewhere.invalid", "fixture-bot", "core", 3600, 20, 200),
            ("/repos/example/app", "github.com", "other-bot", "core", 3600, 30, 200),
            ("/graphql", "github.com", "fixture-bot", "graphql", 3600, 80, 200),
        ]):
            usage.record_response(method="GET", path=path, status=status, host=host, actor=actor, now=1000 + request,
                                  headers={"x-ratelimit-limit": "5000", "x-ratelimit-remaining": str(5000-used),
                                           "x-ratelimit-reset": str(reset), "x-ratelimit-resource": resource,
                                           "x-github-request-id": f"request-{request}"}, operation="github.read")
        windows = usage.report(since=0, now=8000)["quota_windows"]
        self.assertEqual(len(windows), 6)
        peak = next(row for row in windows if (row["host"], row["actor"], row["owner"], row["bucket"], row["reset"]) ==
                    ("github.com", "fixture-bot", "example", "rest_core", 3600))
        self.assertEqual((peak["max_used"], peak["http_requests"], peak["primary_requests"]), (4100, 2, 1))
        self.assertEqual((peak["first_observed_at"], peak["last_observed_at"]), (1000, 1001))

    def test_legacy_and_unreadable_receipts_do_not_invent_session_coverage(self):
        usage.record_response(method="GET", path="/repos/example/app", status=200, headers={},
                              operation="github.read", actor="fixture-bot", now=1000)
        ledger = next(pathlib.Path(self.directory.name).rglob("*.jsonl"))
        record = json.loads(ledger.read_text())
        record.pop("session")
        ledger.write_text(json.dumps(record) + '\n{"timestamp":\n')
        result = usage.report(since=0, now=1100)
        self.assertEqual((result["unreadable_records"], result["unattributed_requests"]), (1, 1))
        self.assertEqual(result["sessions"], [])
        self.assertIsNone(result["session_ceiling"])

    def test_fleet_cli_reads_only_explicit_state_directories(self):
        usage.record_response(method="GET", path="/repos/example/app", status=200, headers={},
                              operation="github.read", actor="fixture-bot")
        script = pathlib.Path(__file__).with_name("github_request_usage.py")
        output = subprocess.run([sys.executable, str(script), "--state-dir", self.directory.name,
                                 "--state-dir", self.directory.name], capture_output=True, text=True, check=True)
        result = json.loads(output.stdout)
        self.assertEqual((result["state_directory_count"], result["files_read"]), (1, 1))
        self.assertEqual(sum(row["http_requests"] for row in result["consumers"]), 1)


if __name__ == "__main__":
    unittest.main()
