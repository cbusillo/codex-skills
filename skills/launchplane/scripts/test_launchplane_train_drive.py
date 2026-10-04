#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Behavior tests for launchplane-train-drive.py's outcome rules."""

from __future__ import annotations

import importlib.util
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
        }

    def update_branch(self, _repository: str, _number: int) -> bool:
        self.updates += 1
        return True

    def failing_checks(self, _repository: str, _sha: str) -> list[dict[str, str]]:
        return self.failing

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def io(self) -> Any:
        return train_drive.DriveIO(
            controller=self.controller,
            pull_request=self.pull_request,
            update_branch=self.update_branch,
            failing_checks=self.failing_checks,
            merged_since=lambda _repository, _since: self.companions,
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

    def test_a_landing_by_another_driver_counts_without_a_controller_call(self) -> None:
        train = FakeTrain([_response("idle")], merge_after={7: 0})
        outcome, _ = _drive(train)
        self.assertEqual((outcome, train.calls), ("landed", 0))

    def test_a_block_fails_with_its_reason(self) -> None:
        reason = {"code": "landing_lineage_changed", "message": "changed"}
        outcome, events = _drive(FakeTrain([_response("block", blocking_reason=reason)]))
        self.assertEqual(outcome, "failed")
        self.assertEqual(events[-1][1]["blocking_reason"], reason)

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
        outcome, _ = _drive(train)
        self.assertEqual(outcome, "needs_owner")

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
