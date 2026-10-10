#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Behavior tests for launchplane-train-drive.py's outcome rules."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("train_drive", SCRIPT_DIR / "launchplane-train-drive.py")
assert _SPEC is not None and _SPEC.loader is not None
train_drive = importlib.util.module_from_spec(_SPEC)
sys.modules["train_drive"] = train_drive
_SPEC.loader.exec_module(train_drive)

REPO = "example/app"


def _response(action: str, **result: Any) -> dict[str, Any]:
    return {"status": "accepted", "result": {"controller_action": action, **result}, "summary": {"trace_id": "t"}}


def _refusal(error_code: str, *, status: str = "stale", http_status: int = 409) -> dict[str, Any]:
    """The write-action helper's envelope for a controller call Launchplane rejected."""
    return {"status": status, "result": {}, "summary": {"http_status": http_status, "trace_id": "refused", "error_code": error_code}}


def _queue(*entries: tuple[int, list[str]]) -> dict[str, Any]:
    return {
        "dry_run_result": {
            "queue_order": [number for number, reasons in entries if not reasons],
            "queue": [{"number": number, "ineligible_reasons": reasons} for number, reasons in entries],
        }
    }


class FakeTrain:
    """A scripted controller plus GitHub pull request states."""

    def __init__(self, responses: list[dict[str, Any] | None], *, merge_after: dict[int, int] | None = None) -> None:
        self.responses = responses
        self.calls = 0
        self.merge_after = merge_after or {}
        self.closed: set[int] = set()
        self.updates = 0
        self.failing: list[dict[str, str]] = []
        self.companions: list[int] = []
        self.clock = 0.0
        self.reconciled: list[str] = []
        self.reconcile_receipt: dict[str, Any] | None = None

    def controller(self, _repository: str, _base: str, _key: str) -> dict[str, Any] | None:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response

    def pull_request(self, _repository: str, number: int) -> dict[str, Any]:
        merged = number in self.merge_after and self.calls >= self.merge_after[number]
        return {
            "state": "closed" if merged or number in self.closed else "open",
            "merged": merged,
            "merge_commit_sha": f"sha-{number}" if merged else None,
            "head_repository": REPO,
            "head": {"sha": f"head-{number}"},
        }

    def update_branch(self, _repository: str, _number: int) -> bool:
        self.updates += 1
        return True

    def failing_checks(self, _repository: str, _sha: str) -> list[dict[str, str]]:
        return self.failing

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def reconcile(self, _repository: str, landing_sha: str) -> dict[str, Any] | None:
        self.reconciled.append(landing_sha)
        return self.reconcile_receipt

    def io(self) -> Any:
        return train_drive.DriveIO(
            controller=self.controller,
            pull_request=self.pull_request,
            update_branch=self.update_branch,
            failing_checks=self.failing_checks,
            merged_since=lambda _repository, _since: self.companions,
            reconcile=self.reconcile,
            now=lambda: self.clock,
            sleep=self.sleep,
        )


def _drive(train: FakeTrain, **settings: Any) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    outcome = train_drive.drive(
        train_drive.DriveSettings(**{"repository": REPO, "number": 7, "deadline": 10_000, "poll_seconds": 60, **settings}),
        train.io(),
        lambda event, payload: events.append((event, payload)),
    )
    return outcome, events


class TrainDriveTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {"GITHUB_RETRY_STATE_DIR": directory.name,
                                               "GITHUB_READ_CACHE_DIR": directory.name + "/cache",
                                               "CODEX_SKILLS_ENV_FILE": "/missing/train-fixture"})
        environment.start()
        self.addCleanup(environment.stop)
        home = patch.object(train_drive.Path, "home", return_value=Path(directory.name))
        home.start()
        self.addCleanup(home.stop)

    def test_live_driver_refuses_second_cli_and_releases_on_process_death(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        script = '''
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("driver", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.Path.home = lambda: pathlib.Path(sys.argv[2])
with module.local_driver(module.DriveSettings(repository="EXAMPLE/App", number=8)) as holder:
    assert holder is None
    print("ready", flush=True)
    sys.stdin.read()
'''
        child = subprocess.Popen([sys.executable, "-c", script, str(SCRIPT_DIR / "launchplane-train-drive.py"),
                                  str(Path.home())], cwd=Path.home(), stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup() -> None:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)
        self.addCleanup(cleanup)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        output = StringIO()
        with patch.object(train_drive, "live_io") as live, redirect_stdout(output):
            code = train_drive.main(["--repo", REPO, "--pr", "7"])
        live.assert_not_called()
        stop = json.loads(output.getvalue())["payload"]
        self.assertEqual((code, stop["outcome"], stop["running_driver"]["pr"]),
                         (train_drive.EXIT_CODES["needs_owner"], "needs_owner", 8))
        self.assertEqual(stop["prs"], [])  # Refusal does not read or assert GitHub state.
        self.assertTrue(stop["running_driver"]["started_at"])
        self.assertIn("ready-to-merge", stop["recommendation"])
        for settings in (train_drive.DriveSettings(repository="other/app", number=9),
                         train_drive.DriveSettings(repository=REPO, number=9, base_branch="release")):
            with train_drive.local_driver(settings) as holder:
                self.assertIsNone(holder)
        child.kill()
        child.communicate(timeout=10)
        train = FakeTrain([_response("land_batch")], merge_after={7: 0})
        with patch.object(train_drive, "live_io", return_value=train.io()), redirect_stdout(StringIO()):
            self.assertEqual(train_drive.main(["--repo", REPO, "--pr", "7"]), 0)

    def test_cli_exception_releases_local_driver_lock(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        with patch.object(train_drive, "live_io", side_effect=OSError("fixture")), self.assertRaises(OSError):
            train_drive.main(["--repo", REPO, "--pr", "7"])
        train = FakeTrain([_response("idle")], merge_after={7: 0})
        with patch.object(train_drive, "live_io", return_value=train.io()), redirect_stdout(StringIO()):
            self.assertEqual(train_drive.main(["--repo", REPO, "--pr", "7"]), 0)

    def test_cli_lock_setup_failure_never_calls_controller(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with patch.object(train_drive.os, "open", side_effect=OSError("fixture")), patch.object(
            train_drive, "live_io"
        ) as live, redirect_stdout(output):
            code = train_drive.main(["--repo", REPO, "--pr", "7"])
        live.assert_not_called()
        self.assertEqual((code, json.loads(output.getvalue())["payload"]["outcome"]),
                         (train_drive.EXIT_CODES["error"], "error"))

    def test_own_merge_refusal_stops_early_with_diagnostics(self) -> None:
        refusal = _refusal("github_merge_rejected")
        refusal["summary"]["pull_request_number"] = 7
        train = FakeTrain([refusal])
        limit = train_drive.DriveSettings(repository=REPO, number=7).max_own_refusals
        outcome, events = _drive(train, max_helper_failures=10)
        stop = events[-1][1]
        self.assertEqual((outcome, train.calls), ("needs_owner", limit))
        self.assertEqual((stop["error_code"], stop["http_status"], stop["trace_id"]),
                         ("github_merge_rejected", 409, "refused"))

    def test_own_refusal_streak_resets_on_progress_or_other_refusal(self) -> None:
        refusal = _refusal("github_merge_rejected")
        refusal["summary"]["pull_request_number"] = 7
        other_code = _refusal("different")
        other_code["summary"]["pull_request_number"] = 7
        other_pr = _refusal("github_merge_rejected")
        other_pr["summary"]["pull_request_number"] = 8
        for interruption in (_response("plan_candidate"), other_code, other_pr,
                             _refusal(train_drive.LEASE_HELD_CODE), None):
            with self.subTest(interruption=interruption):
                train = FakeTrain([refusal, refusal, interruption, refusal, refusal, _response("land_batch")],
                                  merge_after={7: 6})
                self.assertEqual(_drive(train, max_helper_failures=10)[0], "landed")

    def test_other_pr_refusals_keep_existing_failure_budget(self) -> None:
        refusal = _refusal("github_merge_rejected")
        refusal["summary"]["pull_request_number"] = 8
        train = FakeTrain([refusal])
        self.assertEqual(_drive(train)[0], "error")
        self.assertEqual(train.calls, train_drive.DriveSettings(repository=REPO, number=7).max_helper_failures)

    def test_landing_racing_own_refusal_preserves_success(self) -> None:
        refusal = _refusal("github_merge_rejected")
        refusal["summary"]["pull_request_number"] = 7
        limit = train_drive.DriveSettings(repository=REPO, number=7).max_own_refusals
        train = FakeTrain([refusal], merge_after={7: limit})
        self.assertEqual(_drive(train)[0], "landed")

    def test_close_racing_own_refusal_preserves_closed_reason(self) -> None:
        refusal = _refusal("github_merge_rejected")
        refusal["summary"]["pull_request_number"] = 7
        train = FakeTrain([refusal])
        limit = train_drive.DriveSettings(repository=REPO, number=7).max_own_refusals
        original = train.controller
        def controller(*args):
            result = original(*args)
            if train.calls >= limit:
                train.closed.add(7)
            return result
        train.controller = controller
        outcome, events = _drive(train)
        self.assertEqual((outcome, events[-1][1]["reason"]), ("needs_owner", "pull request is closed"))

    def test_live_quota_wait_requires_zero_budget_and_refreshes_evidence(self) -> None:
        import json
        reset = 2_000_000_000
        responses = [subprocess.CompletedProcess([], 0, stdout=(
            'HTTP/2 200\ncontent-type: application/json\n\n' + json.dumps({"resources": {"core": {
                "remaining": remaining, "reset": reset
            }}})
        ).encode(), stderr=b"GitHub automation actor: fixture-bot (source: github_app)") for remaining in (10, 0)]
        with patch.dict(os.environ, {"CODEX_AUTOMATION_LOGIN": "fixture-bot"}), patch("subprocess.run", side_effect=responses) as run:
            io = train_drive.live_io(180, deadline_at=reset + 100, repository_context=REPO)
            self.assertEqual(io.quota_wait(), 0)
            self.assertGreater(io.quota_wait(), 0)
            self.assertEqual(run.call_args.args[0][:2], ["env", f"GH_REPO={REPO}"])

    def test_other_candidate_membership_keeps_target_landing_reads(self) -> None:
        train = FakeTrain([_response("observe_candidate", candidate={"candidate_sha": "other", "pull_request_numbers": [8]})],
                          merge_after={7: 2})
        outcome, _ = _drive(train)
        self.assertEqual((outcome, train.calls), ("landed", 2))

    def test_lease_refusal_clears_candidate_ownership(self) -> None:
        train = FakeTrain([
            _response("observe_candidate", candidate={"candidate_sha": "ours", "pull_request_numbers": [7]}),
            _refusal(train_drive.LEASE_HELD_CODE),
        ], merge_after={7: 2})
        outcome, _ = _drive(train)
        self.assertEqual((outcome, train.calls), ("landed", 2))

    def test_unavailable_controller_resumes_landing_reads(self) -> None:
        train = FakeTrain([
            _response("observe_candidate", candidate={"candidate_sha": "ours", "pull_request_numbers": [7]}), None,
        ], merge_after={7: 2})
        outcome, _ = _drive(train)
        self.assertEqual((outcome, train.calls), ("landed", 2))

    def test_cli_auth_failure_emits_error_stop_instead_of_traceback(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        import json
        api = train_drive.github_read.github_api_core
        result = api.ApiResult(ok=False, status=403, body=None, failure=api.FailureDetail(
            cause="permission_denied", message="fixture refusal", retryable=False, fallback_eligible=False, disposition="stop"
        ))
        io = FakeTrain([_response("idle")]).io()
        io.pull_request = lambda *_args: (_ for _ in ()).throw(train_drive.github_read.GitHubReadError(
            "fixture refusal", result=result, diagnostics={}
        ))
        output = StringIO()
        with patch.object(train_drive, "live_io", return_value=io), redirect_stdout(output):
            code = train_drive.main(["--repo", REPO, "--pr", "7"])
        stop = json.loads(output.getvalue())
        self.assertEqual((code, stop["event"], stop["payload"]["outcome"]), (train_drive.EXIT_CODES["error"], "stop", "error"))
        self.assertEqual(stop["payload"]["read_failure_cause"], "permission_denied")

    def test_auth_refusal_after_target_lands_preserves_the_landing(self) -> None:
        api = train_drive.github_read.github_api_core
        result = api.ApiResult(ok=False, status=403, body=None, failure=api.FailureDetail(
            cause="permission_denied", message="fixture refusal", retryable=False, fallback_eligible=False, disposition="stop"
        ))
        train = FakeTrain([_response("idle")], merge_after={7: 0})
        io = train.io()
        io.merged_since = lambda *_args: (_ for _ in ()).throw(train_drive.github_read.GitHubReadError(
            "fixture refusal", result=result, diagnostics={}
        ))
        events = []
        outcome = train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=10000), io,
                                    lambda event, payload: events.append((event, payload)))
        self.assertEqual((outcome, events[-1][1]["landing_sha"]), ("landed", "sha-7"))
        self.assertEqual(events[-1][1]["companion_evidence"], "unavailable")

    def test_malformed_candidate_check_response_emits_error(self) -> None:
        train = FakeTrain([_response("candidate_failed", candidate={"candidate_sha": "abc"})])
        io = train.io()
        io.failing_checks = lambda *_args: (_ for _ in ()).throw(train_drive.github_read.GitHubReadShapeError("missing check_runs"))
        events = []
        outcome = train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=10000), io,
                                    lambda event, payload: events.append((event, payload)))
        self.assertEqual(outcome, "error")
        self.assertEqual(events[-1][1]["read_failure_cause"], "invalid_response")

    def test_final_landing_reads_have_one_fixed_grace_deadline(self) -> None:
        with patch.object(train_drive.github_read, "GitHubReader") as reader_class:
            io = train_drive.live_io(180, deadline_at=1000)
            reader = reader_class.return_value
            io.finish_reads()
            first = reader.deadline_at
            io.finish_reads()
            self.assertEqual(first, reader.deadline_at)
            self.assertGreater(first, 1000)
            self.assertLess(first, 1060)

    def test_final_read_can_confirm_a_landing_after_normal_deadline_expires(self) -> None:
        import json
        api = train_drive.github_read.github_api_core
        response = subprocess.CompletedProcess([], 0, stdout=(
            'HTTP/2 200\ncontent-type: application/json\n\n' + json.dumps({"merged": True, "state": "closed", "merge_commit_sha": "landed"})
        ).encode(), stderr=b"GitHub automation actor: fixture-bot (source: github_app)")
        with patch.dict(os.environ, {"CODEX_AUTOMATION_LOGIN": "fixture-bot"}), patch("time.time", return_value=1000), patch.object(
            api, "default_retry_runtime", return_value=api.RetryRuntime(now=lambda: 1000)
        ), patch("subprocess.run", return_value=response) as run:
            io = train_drive.live_io(180, deadline_at=999)
            self.assertIsNone(io.pull_request(REPO, 7))
            self.assertEqual(run.call_count, 0)
            io.finish_reads()
            self.assertEqual(io.pull_request(REPO, 7)["merge_commit_sha"], "landed")
            self.assertEqual(run.call_count, 1)

    def test_controller_subprocess_is_bounded_by_the_parent_deadline(self) -> None:
        with patch("time.time", return_value=1000), patch("subprocess.run", return_value=subprocess.CompletedProcess(
            [], 0, stdout='{"status":"accepted","result":{}}', stderr=""
        )) as run:
            io = train_drive.live_io(180, deadline_at=1010)
            self.assertEqual(io.controller(REPO, "main", "key")["status"], "accepted")
            self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_primary_exhaustion_waits_past_the_refusal_budget(self) -> None:
        outcomes = []
        for wait_for_quota in (False, True):
            train = FakeTrain([_refusal("github_request_failed", http_status=502)], merge_after={7: 2})
            def controller(*_args):
                train.calls += 1
                return _refusal("github_request_failed", http_status=502) if train.clock < 3600 else _response("land_batch")
            train.pull_request = lambda *_args: {"state": "open", "merged": train.clock >= 3600 and train.calls >= 2,
                                                 "merge_commit_sha": "landed"}
            io = train.io()
            io.controller = controller
            io.quota_wait = (lambda: max(0, 3600 - train.clock)) if wait_for_quota else lambda: 0
            outcome = train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=10000), io, lambda *_args: None)
            outcomes.append(outcome)
        self.assertEqual(outcomes, ["error", "landed"])

    def test_local_quota_does_not_mask_repeated_unrelated_controller_failures(self) -> None:
        train = FakeTrain([_refusal("github_request_failed", http_status=502)])
        io = train.io()
        io.quota_wait = lambda: 3600
        events = []
        outcome = train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=10000), io,
                                    lambda event, payload: events.append((event, payload)))
        self.assertEqual(outcome, "error")
        self.assertEqual(events[-1][1]["reason"], "merge-train controller kept refusing")
        self.assertLess(train.clock, 10000)

    def test_confirmed_primary_wait_obeys_the_driver_deadline(self) -> None:
        train = FakeTrain([_refusal("github_request_failed", http_status=502)])
        io = train.io()
        io.quota_wait = lambda: 3600
        outcome = train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=120),
                                    io, lambda *_args: None)
        self.assertEqual((outcome, train.clock, train.calls), ("error", 120, 1))

    def test_observed_candidate_does_not_poll_batch_prs(self) -> None:
        waiting = _response("observe_candidate", candidate={"candidate_sha": "abc", "pull_request_numbers": [7, 8, 9]}, **_queue((7, []), (8, []), (9, [])))
        train = FakeTrain([waiting] * 8 + [_response("land_batch")], merge_after={7: 9, 8: 9, 9: 9})
        reads = []
        original = train.pull_request
        train.pull_request = lambda repository, number: reads.append(number) or original(repository, number)
        outcome, events = _drive(train)
        self.assertEqual(outcome, "landed")
        self.assertEqual(reads, [7, 7, 8, 9])
        self.assertEqual({item["number"] for item in events[-1][1]["prs"]}, {7, 8, 9})

    def test_live_pr_reads_use_shared_conditional_cache_and_deadline(self) -> None:
        import json
        body = {"state": "open", "merged": False, "head": {"repo": {"full_name": REPO}}}
        first = subprocess.CompletedProcess([], 0, stdout=(
            'HTTP/2 200\ncontent-type: application/json\netag: "v1"\n\n' + json.dumps(body)
        ).encode(), stderr=b"GitHub automation actor: fixture-bot (source: github_app)")
        second = subprocess.CompletedProcess([], 1, stdout=b'HTTP/2 304\netag: "v1"\n\n', stderr=first.stderr)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "CODEX_SKILLS_ENV_FILE": "/missing/train-fixture", "CODEX_AUTOMATION_LOGIN": "fixture-bot",
            "GITHUB_READ_CACHE_DIR": directory + "/cache", "GITHUB_RETRY_STATE_DIR": directory + "/retry",
        }), patch("subprocess.run", side_effect=[first, second]) as run:
            io = train_drive.live_io(180, deadline_at=2_000_000_000)
            self.assertEqual(io.pull_request(REPO, 7)["head_repository"], REPO)
            with patch("time.time", return_value=train_drive.time.time() + 10):
                self.assertFalse(io.pull_request(REPO, 7)["merged"])
            self.assertIn('If-None-Match: "v1"', run.call_args.args[0])
            self.assertLessEqual(run.call_args.kwargs["timeout"], train_drive.github_read.github_api_core.DEFAULT_RETRY_MAX_WAIT_SECONDS)

    def test_reports_every_landed_pull_request_in_the_batch(self) -> None:
        train = FakeTrain(
            [_response("wait_for_checks", **_queue((7, []), (8, []))), _response("plan_candidate"), _response("land_batch")],
            merge_after={7: 3, 8: 3},
        )
        outcome, events = _drive(train)
        stop = events[-1][1]
        self.assertEqual(outcome, "landed")
        self.assertEqual(stop["landing_sha"], "sha-7")
        self.assertEqual({pr["number"]: pr["outcome"] for pr in stop["prs"]}, {7: "landed", 8: "landed"})
        self.assertEqual([event for event, _ in events].count("pr_landed"), 2)

    def test_a_landing_reconciles_the_installed_catalog_with_its_landing_sha(self) -> None:
        train = FakeTrain([_response("land_batch")], merge_after={7: 1})
        train.reconcile_receipt = {"status": "synchronized", "reason_code": "runtime_fast_forwarded"}
        outcome, events = _drive(train)
        self.assertEqual((outcome, train.reconciled), ("landed", ["sha-7"]))
        self.assertEqual(events[-1][1]["runtime_reconciliation"], train.reconcile_receipt)

    def test_another_repository_or_a_failed_drive_has_no_reconciliation(self) -> None:
        landed = FakeTrain([_response("land_batch")], merge_after={7: 1})
        _, events = _drive(landed)
        self.assertNotIn("runtime_reconciliation", events[-1][1])
        failed = FakeTrain([_response("block", blocking_reason={"code": "x"})])
        failed.reconcile_receipt = {"status": "synchronized"}
        self.assertEqual((_drive(failed)[0], failed.reconciled), ("failed", []))

    def test_live_reconcile_only_runs_for_the_catalog_repository(self) -> None:
        io = train_drive.live_io(1.0)
        catalog = subprocess.run(
            ["git", "-C", str(SCRIPT_DIR), "config", "--get", "remote.origin.url"], capture_output=True, text=True
        ).stdout.strip().removesuffix(".git").replace(":", "/").split("/")
        with patch.object(train_drive, "_run_json", return_value={"status": "already_current"}) as run:
            self.assertIsNone(io.reconcile("someone-else/app", "a" * 40))
            run.assert_not_called()
            receipt = io.reconcile(f"{catalog[-2]}/{catalog[-1]}", "a" * 40)
        self.assertEqual(receipt, {"status": "already_current"})
        self.assertIn("--landing-sha", run.call_args.args[0])

    def test_a_landing_by_another_driver_counts_without_a_controller_call(self) -> None:
        train = FakeTrain([_response("idle")], merge_after={7: 0})
        outcome, _ = _drive(train)
        self.assertEqual((outcome, train.calls), ("landed", 0))

    def test_a_block_fails_with_its_reason(self) -> None:
        reason = {"code": "landing_lineage_changed", "message": "changed"}
        outcome, events = _drive(FakeTrain([_response("block", blocking_reason=reason)]))
        self.assertEqual(outcome, "failed")
        self.assertEqual(events[-1][1]["blocking_reason"], reason)

    def test_another_pull_request_block_is_reported_without_failing_target(self) -> None:
        for block_result in ({}, {"status": "blocked", "pull_request_number": 2, "train_should_continue": False}):
            with self.subTest(block_result=block_result):
                train = FakeTrain([_response(
                    "block", dry_run_result={"selected_pr": {"number": 2}, "next_action_detail": "Required checks failed."},
                    block_result=block_result,
                )])
                outcome, events = _drive(train)
                stop = events[-1][1]
                self.assertEqual(outcome, "needs_owner")
                self.assertEqual(stop["reason"], "another pull request is blocking the train")
                self.assertEqual(stop["blocking_pull_request_number"], 2)
                self.assertEqual(stop["tracked_pull_request_number"], 7)
                self.assertEqual(stop["block_detail"], "Required checks failed.")
                self.assertEqual(train.calls, 1)

    def test_applied_other_pr_block_continues_only_when_policy_allows(self) -> None:
        train = FakeTrain([
            _response("block", dry_run_result={"selected_pr": {"number": 2}},
                      block_result={"status": "blocked", "pull_request_number": 2, "train_should_continue": True}),
            _response("plan_candidate"), _response("land_batch"),
        ], merge_after={7: 3})
        outcome, events = _drive(train)
        self.assertEqual((outcome, train.calls), ("landed", 3))
        self.assertTrue(any(event == "pr_blocked" and data["pull_request_number"] == 2 for event, data in events))

    def test_own_block_and_mismatched_block_receipt_never_allow_continuation(self) -> None:
        for selected in (7, 2):
            with self.subTest(selected=selected):
                train = FakeTrain([_response(
                    "block", dry_run_result={"selected_pr": {"number": selected}},
                    block_result={"status": "blocked", "pull_request_number": 7, "train_should_continue": True},
                )])
                outcome, _ = _drive(train)
                self.assertEqual(outcome, "failed" if selected == 7 else "needs_owner")
                self.assertEqual(train.calls, 1)

    def test_unsupported_stack_root_is_not_assumed_unrelated_to_child(self) -> None:
        outcome, _ = _drive(FakeTrain([_response(
            "stack_unsupported", dry_run_result={"selected_pr": {"number": 2}},
        )]))
        self.assertEqual(outcome, "failed")

    def test_a_block_on_batch_checks_still_running_waits_for_them(self) -> None:
        waiting = {"code": "batch_pull_request_checks_not_ready", "message": "checks running"}
        train = FakeTrain(
            [_response("block", blocking_reason=waiting), _response("block", blocking_reason=waiting), _response("land_batch")],
            merge_after={7: 3},
        )
        outcome, _ = _drive(train)
        self.assertEqual((outcome, train.calls), ("landed", 3))

        outcome, events = _drive(FakeTrain([_response("block", blocking_reason=waiting)]), deadline=300)
        self.assertEqual((outcome, events[-1][1]["reason"]), ("error", "deadline reached"))

    def test_a_failed_candidate_names_its_failing_check(self) -> None:
        train = FakeTrain([_response("candidate_failed", candidate={"candidate_sha": "abc"})])
        train.failing = [{"name": "ci-gate", "conclusion": "failure", "url": "https://example.test/run/1"}]
        outcome, events = _drive(train)
        self.assertEqual(outcome, "failed")
        self.assertEqual(events[-1][1]["failing_checks"], train.failing)

    def test_a_stale_candidate_without_failures_is_rebuilt(self) -> None:
        train = FakeTrain(
            [_response("candidate_failed", candidate={"candidate_sha": "abc"}), _response("plan_candidate"), _response("land_batch")],
            merge_after={7: 3},
        )
        outcome, _ = _drive(train)
        self.assertEqual(outcome, "landed")

    def test_a_behind_branch_needs_the_owner_unless_updates_are_allowed(self) -> None:
        outcome, _ = _drive(FakeTrain([_response("update_branch")]))
        self.assertEqual(outcome, "needs_owner")

        train = FakeTrain([_response("update_branch"), _response("land_batch")], merge_after={7: 2})
        outcome, _ = _drive(train, allow_branch_update=True)
        self.assertEqual((outcome, train.updates), ("landed", 1))

    def test_a_different_pull_request_behind_its_base_is_not_updated(self) -> None:
        train = FakeTrain([_response("update_branch", dry_run_result={"selected_pr": {"number": 8}})])
        outcome, events = _drive(train, allow_branch_update=True)
        self.assertEqual((outcome, train.updates), ("needs_owner", 0))
        self.assertIn("#8", events[-1][1]["reason"])

    def test_a_root_check_wait_that_needs_a_branch_update_stops_early(self) -> None:
        train = FakeTrain([_response("wait_for_root_checks", dry_run_result={
            "intended_next_action": "update_branch", "selected_pr": {"number": 7},
            "branch_update_required": True, "required_checks_status": "pass",
        })])
        outcome, events = _drive(train)
        self.assertEqual((outcome, train.calls, train.updates, train.clock), ("needs_owner", 1, 0, 0))
        self.assertIn("branch is behind its base", events[-1][1]["reason"])

    def test_an_intended_branch_update_uses_the_existing_update_permissions(self) -> None:
        for selected in (7, 8):
            with self.subTest(selected=selected):
                train = FakeTrain([
                    _response("wait_for_root_checks", dry_run_result={
                        "intended_next_action": "update_branch", "selected_pr": {"number": selected},
                    }),
                    _response("land_batch"),
                ], merge_after={7: 2})
                outcome, events = _drive(train, allow_branch_update=True)
                if selected == 7:
                    self.assertEqual((outcome, train.updates), ("landed", 1))
                else:
                    self.assertEqual((outcome, train.calls, train.updates), ("needs_owner", 1, 0))
                    self.assertIn("#8", events[-1][1]["reason"])

    def test_an_intended_branch_update_already_done_by_the_controller_is_progress(self) -> None:
        train = FakeTrain([
            _response("update_branch", dry_run_result={"intended_next_action": "update_branch"},
                      branch_update_result={"status": "updated"}),
            _response("land_batch"),
        ], merge_after={7: 2})
        outcome, _ = _drive(train)
        self.assertEqual((outcome, train.updates), ("landed", 0))

    def test_a_root_check_wait_without_a_branch_update_keeps_waiting(self) -> None:
        train = FakeTrain([
            _response("wait_for_root_checks", dry_run_result={"intended_next_action": "wait_for_checks"}),
            _response("land_batch"),
        ], merge_after={7: 2})
        outcome, _ = _drive(train)
        self.assertEqual((outcome, train.calls, train.updates), ("landed", 2, 0))

    def test_batch_companions_missing_from_the_queue_are_still_reported(self) -> None:
        train = FakeTrain([_response("land_batch")], merge_after={7: 1, 9: 1})
        train.companions = [9]
        _, events = _drive(train)
        self.assertEqual({pr["number"]: pr["outcome"] for pr in events[-1][1]["prs"]}, {7: "landed", 9: "landed"})

    def test_a_stack_landing_waits_for_the_controller_to_finish_the_batch(self) -> None:
        train = FakeTrain(
            [_response("execute_stack_collapse"), _response("land_batch"), _response("land_batch"), _response("batch_landed")],
            merge_after={7: 2},
        )
        outcome, _ = _drive(train)
        self.assertEqual((outcome, train.calls), ("landed", 4))

    def test_a_pull_request_that_stays_ineligible_needs_the_owner(self) -> None:
        outcome, events = _drive(FakeTrain([_response("idle", **_queue((7, ["missing ready-to-merge label"])))]))
        self.assertEqual(outcome, "needs_owner")
        self.assertEqual(events[-1][1]["ineligible_reasons"], ["missing ready-to-merge label"])

    def test_a_closed_pull_request_needs_the_owner(self) -> None:
        train = FakeTrain([_response("idle")])
        train.closed.add(7)
        outcome, events = _drive(train)
        self.assertEqual(outcome, "needs_owner")
        self.assertEqual(events[-1][1]["prs"], [{"number": 7, "outcome": "closed", "merge_commit_sha": ""}])

    def test_cli_reports_carried_children_from_the_projected_controller_response(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        spec = importlib.util.spec_from_file_location("disposition_write_action", SCRIPT_DIR / "launchplane-write-action.py")
        assert spec is not None and spec.loader is not None
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        raw = {
            "status": "accepted", "records": {}, "trace_id": "disposition-trace",
            "result": {
                "controller_action": "batch_landed",
                "stack_collapse_plan": {
                    "collapse_id": "collapse-example", "root_pull_request_number": 7,
                    "child_dispositions": [{"pull_request_number": 8, "expected_head_sha": "head-8",
                                            "status": "closed", "detail": "child PR closed after root landed"}],
                },
            },
        }
        projected = helper.summarize_success(operation="merge-train-controller-run-once",
                                             request={"repository": REPO}, provider_payload=raw)
        train = FakeTrain([projected], merge_after={7: 1})
        train.closed.add(8)
        output = StringIO()
        with patch.object(train_drive, "live_io", return_value=train.io()), redirect_stdout(output):
            code = train_drive.main(["--repo", REPO, "--pr", "7"])
        receipt = json.loads(output.getvalue().splitlines()[-1])["payload"]
        self.assertEqual((code, receipt["outcome"]), (0, "landed"))
        self.assertEqual(receipt["prs"], [
            {"number": 7, "outcome": "landed", "merge_commit_sha": "sha-7"},
            {"number": 8, "outcome": "superseded", "merge_commit_sha": "",
             "carried_by": {"collapse_id": "collapse-example", "root_pull_request_number": 7,
                            "expected_head_sha": "head-8"}},
        ])

    def test_closed_child_without_matching_carried_evidence_is_only_closed(self) -> None:
        for expected_head in (None, "older-head"):
            with self.subTest(expected_head=expected_head):
                response = _response("land_batch", **_queue((7, []), (8, [])), stack_collapse_plan={
                    "collapse_id": "collapse-example", "root_pull_request_number": 7,
                    "child_dispositions": [{"pull_request_number": 8, "expected_head_sha": expected_head, "status": "closed"}],
                })
                train = FakeTrain([response], merge_after={7: 1})
                train.closed.add(8)
                outcome, events = _drive(train)
                self.assertEqual(outcome, "landed")
                self.assertEqual(events[-1][1]["prs"][1], {"number": 8, "outcome": "closed", "merge_commit_sha": ""})

    def test_unavailable_companion_read_is_unknown_after_an_open_observation(self) -> None:
        train = FakeTrain([_response("wait_for_checks", **_queue((7, []), (8, []))),
                           _response("land_batch")], merge_after={7: 2})
        read = train.pull_request
        train.pull_request = lambda repo, number: None if number == 8 and train.calls >= 2 else read(repo, number)
        outcome, events = _drive(train)
        self.assertEqual(outcome, "landed")
        self.assertEqual(events[-1][1]["prs"][1], {"number": 8, "outcome": "unknown", "merge_commit_sha": ""})

    def test_final_read_failure_preserves_a_verified_closed_companion(self) -> None:
        train = FakeTrain([_response("wait_for_checks", **_queue((7, []), (8, []))),
                           _response("land_batch")], merge_after={7: 2})
        train.closed.add(8)
        read = train.pull_request
        train.pull_request = lambda repo, number: None if number == 8 and train.calls >= 2 else read(repo, number)
        self.assertEqual(_drive(train)[1][-1][1]["prs"][1], {"number": 8, "outcome": "closed", "merge_commit_sha": ""})

    def test_stack_finish_discovers_closed_children_and_preserves_open_members(self) -> None:
        train = FakeTrain([
            _response("execute_stack_collapse", **_queue((7, []), (9, []))), _response("land_batch"),
            _response("batch_landed", stack_collapse_plan={
                "collapse_id": "collapse-example", "root_pull_request_number": 7,
                "child_dispositions": [{"pull_request_number": 8, "expected_head_sha": "head-8", "status": "closed"}],
            }),
        ], merge_after={7: 2})
        train.closed.add(8)
        self.assertEqual({row["number"]: row["outcome"] for row in _drive(train)[1][-1][1]["prs"]},
                         {7: "landed", 8: "superseded", 9: "open"})

    def test_a_helper_that_keeps_failing_ends_in_an_error(self) -> None:
        outcome, events = _drive(FakeTrain([None]))
        self.assertEqual(outcome, "error")
        self.assertEqual(events[-1][1]["reason"], "merge-train helper kept failing")

    def test_a_held_controller_lease_waits_out_the_lease_and_continues(self) -> None:
        train = FakeTrain([_refusal(train_drive.LEASE_HELD_CODE), _response("land_batch")], merge_after={7: 2})
        outcome, events = _drive(train, max_helper_failures=1)
        self.assertEqual(outcome, "landed")
        held = events[0][1]
        self.assertEqual((held["controller_action"], held["error_code"], held["http_status"]), ("controller_lease_held", train_drive.LEASE_HELD_CODE, 409))

    def test_a_lease_still_held_at_the_deadline_needs_the_owner(self) -> None:
        outcome, events = _drive(FakeTrain([_refusal(train_drive.LEASE_HELD_CODE)]), deadline=1_000)
        self.assertEqual(outcome, "needs_owner")
        self.assertEqual(events[-1][1]["trace_id"], "refused")

    def test_a_refusal_reports_its_code_instead_of_an_unavailable_helper(self) -> None:
        outcome, events = _drive(FakeTrain([_refusal("merge_train_new_refusal")]))
        snapshot = events[0][1]
        self.assertEqual((snapshot["controller_action"], snapshot["status"], snapshot["error_code"]),
                         ("controller_refused", "stale", "merge_train_new_refusal"))
        self.assertEqual(outcome, "error")
        self.assertEqual((events[-1][1]["reason"], events[-1][1]["error_code"]), ("merge-train controller kept refusing", "merge_train_new_refusal"))

    def test_branches_the_controller_already_updated_are_progress(self) -> None:
        # A busy train refreshes several queued PRs ahead of this one before it lands.
        updated = _response("update_branch", branch_update_result={"status": "updated"})
        train = FakeTrain([updated] * 5 + [_response("land_batch")], merge_after={7: 6})
        outcome, _ = _drive(train)
        self.assertEqual((outcome, train.updates), ("landed", 0))

    def test_the_deadline_ends_in_an_error_instead_of_waiting_forever(self) -> None:
        outcome, events = _drive(FakeTrain([_response("wait_for_checks")]), deadline=300)
        self.assertEqual(outcome, "error")
        self.assertEqual(events[-1][1]["reason"], "deadline reached")


if __name__ == "__main__":
    unittest.main()
