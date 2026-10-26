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
train_drive: Any = importlib.util.module_from_spec(_SPEC)
sys.modules["train_drive"] = train_drive
_SPEC.loader.exec_module(train_drive)

REPO = "example/app"


def _response(action: str, **result: Any) -> dict[str, Any]:
    return {"status": "accepted", "result": {"controller_action": action, **result}, "summary": {"trace_id": "t"}}


def _project_controller(raw: dict[str, Any]) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("evidence_write_action", SCRIPT_DIR / "launchplane-write-action.py")
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper.summarize_success(operation="merge-train-controller-run-once",
                                    request={"repository": REPO}, provider_payload=raw)


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


def _stack(*, status: str = "waiting_for_root_checks", child_status: str = "planned",
           collapse_id: str = "collapse-example", root: int = 7) -> dict[str, Any]:
    return {"collapse_id": collapse_id, "root_pull_request_number": root, "status": status,
            "child_dispositions": [{"pull_request_number": 8, "expected_head_sha": "head-8", "status": child_status}]}


class FakeTrain:
    """A scripted controller plus GitHub pull request states."""

    def __init__(self, responses: list[dict[str, Any] | None], *, merge_after: dict[int, int] | None = None) -> None:
        self.responses = responses
        self.calls = 0
        self.merge_after = merge_after or {}
        self.closed: set[int] = set()
        self.updates = 0
        self.checks: list[dict[str, Any]] = []
        self.companions: list[int] = []
        self.clock = 0.0
        self.sleeps: list[float] = []
        self.reconciled: list[str] = []
        self.reconcile_receipt: dict[str, Any] | None = None
        self.observations: list[dict[str, Any] | None] = []
        self.observed = 0
        # Simulated request latency on the fake clock.
        self.controller_delay = 0.0
        self.read_delay = 0.0

    def controller(self, _repository: str, _base: str, _key: str) -> dict[str, Any] | None:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        self.clock += self.controller_delay
        return response

    def pull_request(self, _repository: str, number: int) -> dict[str, Any]:
        self.clock += self.read_delay
        merged = number in self.merge_after and self.calls >= self.merge_after[number]
        return {
            "state": "closed" if merged or number in self.closed else "open",
            "merged": merged,
            "merge_commit_sha": f"sha-{number}" if merged else None,
            "merged_at": train_drive.time.strftime("%Y-%m-%dT%H:%M:%SZ", train_drive.time.gmtime(self.clock)) if merged else None,
            "head_repository": REPO,
            "head": {"sha": f"head-{number}"},
        }

    def observe_controller(self, _repository: str, _base: str) -> dict[str, Any] | None:
        response = self.observations[min(self.observed, len(self.observations) - 1)] if self.observations else None
        self.observed += 1
        return response

    def update_branch(self, _repository: str, _number: int) -> bool:
        self.updates += 1
        return True

    def candidate_checks(self, _repository: str, _sha: str) -> list[dict[str, Any]]:
        return self.checks

    def sleep(self, seconds: float) -> None:
        self.clock += seconds
        self.sleeps.append(seconds)

    def reconcile(self, _repository: str, landing_sha: str) -> dict[str, Any] | None:
        self.reconciled.append(landing_sha)
        return self.reconcile_receipt

    def io(self) -> Any:
        return train_drive.DriveIO(
            controller=self.controller,
            observe_controller=self.observe_controller,
            pull_request=self.pull_request,
            update_branch=self.update_branch,
            candidate_checks=self.candidate_checks,
            merged_since=lambda _repository, _since: {"numbers": self.companions, "complete": True},
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


def _cli(train: FakeTrain) -> tuple[int, dict[str, Any]]:
    from contextlib import redirect_stdout
    from io import StringIO
    output = StringIO()
    with patch.object(train_drive, "live_io", return_value=train.io()), redirect_stdout(output):
        code = train_drive.main(["--repo", REPO, "--pr", "7"])
    return code, json.loads(output.getvalue().splitlines()[-1])["payload"]


def _write_action_helper() -> Any:
    spec = importlib.util.spec_from_file_location("drive_write_action", SCRIPT_DIR / "launchplane-write-action.py")
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


class TrainDriveTests(unittest.TestCase):
    def test_external_landing_stops_before_unrelated_pass_or_sleep(self) -> None:
        for landing_during in ("controller", "sleep"):
            with self.subTest(landing_during=landing_during):
                candidate = {"candidate_sha": "abc", "pull_request_numbers": [7, 8]}
                train = FakeTrain([_response("observe_candidate", candidate=candidate),
                                   _response("build_candidate", **_queue((99, [])))])
                if landing_during == "controller":
                    train.merge_after[7] = 1
                else:
                    sleep = train.sleep

                    def land_while_sleeping(seconds: float) -> None:
                        sleep(seconds)
                        train.merge_after[7] = 1

                    train.sleep = land_while_sleeping
                code, receipt = _cli(train)
                self.assertEqual((code, receipt["outcome"], receipt["landing_sha"]), (0, "landed", "sha-7"))
                self.assertEqual(train.calls, 1)
                self.assertEqual(len(train.sleeps), int(landing_during == "sleep"))

    def test_landing_reconciles_every_landed_batch_member(self) -> None:
        train = FakeTrain([], merge_after={7: 0, 8: 0})
        events = []
        reconciled = []
        io = train.io()
        io.reconcile_plans = lambda repository, numbers: reconciled.append((repository, numbers)) or {"ok": False}
        settings = train_drive.DriveSettings(repository=REPO, number=7)
        wrapped = train_drive._with_runtime_reconciliation(settings, io, lambda event, payload: events.append((event, payload)))
        state = train_drive.DriveState(batch={7, 8, 10}, landed={7: "landing", 8: "landing"})
        self.assertEqual(train_drive._stop(settings, state, wrapped, "landed"), "landed")
        self.assertEqual(reconciled, [(REPO, [7, 8])])
        self.assertFalse(events[-1][1]["planning_reconciliation"]["ok"])
        self.assertEqual(events[-1][1]["outcome"], "landed")

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
                             _refusal(train_drive.LEASE_HELD_CODE)):
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
        self.assertFalse(events[-1][1]["companion_inventory"]["complete"])

    def test_malformed_candidate_check_response_emits_error(self) -> None:
        train = FakeTrain([_response("candidate_failed", candidate={"candidate_sha": "abc"})])
        io = train.io()
        io.candidate_checks = lambda *_args: (_ for _ in ()).throw(train_drive.github_read.GitHubReadShapeError("missing check_runs"))
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
        self.assertEqual([number for number in reads if number != 7], [8, 9])
        self.assertLessEqual(reads.count(7), 2 * train.calls)
        self.assertEqual({item["number"] for item in events[-1][1]["prs"]}, {7, 8, 9})
        train = FakeTrain([waiting])
        sleep = train.sleep

        def close_companion_while_sleeping(seconds: float) -> None:
            sleep(seconds)
            train.closed.add(8)

        train.sleep = close_companion_while_sleeping
        outcome, events = _drive(train, deadline=60)
        self.assertEqual(outcome, "error")
        dispositions = {item["number"]: item["outcome"] for item in events[-1][1]["prs"]}
        self.assertEqual(dispositions[8], "closed")

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
        origin = subprocess.CompletedProcess([], 0, stdout="git@github.com:fixture/catalog.git\n", stderr="")
        with (
            patch.object(train_drive.subprocess, "run", return_value=origin),
            patch.object(train_drive, "_run_json", return_value={"status": "already_current"}) as run,
        ):
            self.assertIsNone(io.reconcile("someone-else/app", "a" * 40))
            run.assert_not_called()
            receipt = io.reconcile("fixture/catalog", "a" * 40)
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
        train.checks = [{"name": "ci-gate", "status": "completed", "conclusion": "failure", "url": "https://example.test/run/1"},
                        {"name": "lint", "status": "completed", "conclusion": "success", "url": ""}]
        outcome, events = _drive(train)
        self.assertEqual(outcome, "failed")
        self.assertEqual(events[-1][1]["failing_checks"], train.checks[:1])

    def test_failed_snapshot_keeps_service_recovery_evidence(self) -> None:
        diagnosis = {"action": "candidate_failed", "reason_code": "candidate_check_retry_used", "source_failed_record_id": "failed-record", "next_actor": "repository_agent"}
        train = FakeTrain([_response("candidate_failed", reason_code="failed_candidate_recovery_unavailable", last_controller_result=diagnosis, candidate={"candidate_sha": "abc"})])
        outcome, events = _drive(train)
        snapshot = next(payload for event, payload in events if event == "snapshot")
        self.assertEqual(outcome, "failed")
        self.assertEqual(snapshot["reason_code"], diagnosis["reason_code"])
        self.assertEqual(snapshot["last_controller_result"], diagnosis)

    def test_old_response_does_not_invent_diagnostic_evidence(self) -> None:
        train = FakeTrain([_response("candidate_failed", reason_code="candidate_check_retry_used")])
        _, events = _drive(train)
        snapshot = next(payload for event, payload in events if event == "snapshot")
        self.assertEqual(snapshot["reason_code"], train.responses[0]["result"]["reason_code"])
        self.assertIsNone(snapshot["last_controller_result"])

    def test_a_stale_candidate_without_failures_is_rebuilt(self) -> None:
        train = FakeTrain(
            [_response("candidate_failed", candidate={"candidate_sha": "abc"}), _response("plan_candidate"), _response("land_batch")],
            merge_after={7: 3},
        )
        outcome, _ = _drive(train)
        self.assertEqual(outcome, "landed")

    def test_a_failed_candidate_waits_while_a_check_rerun_is_running(self) -> None:
        failed = _response("candidate_failed", candidate={"candidate_sha": "abc"})
        train = FakeTrain([failed] * 7 + [_response("plan_candidate"), _response("land_batch")], merge_after={7: 9})
        train.checks = [{"name": "simulator", "status": "in_progress", "conclusion": None, "url": ""}]
        outcome, _ = _drive(train)
        self.assertEqual((outcome, train.calls), ("landed", 9))

    def test_a_failed_candidate_waits_for_running_checks_before_naming_a_failure(self) -> None:
        train = FakeTrain([_response("candidate_failed", candidate={"candidate_sha": "abc"})])
        failure = {"name": "ui-tests", "status": "completed", "conclusion": "failure", "url": ""}
        rerun = {"name": "simulator", "status": "queued", "conclusion": None, "url": ""}

        def checks_with_rerun_until_third_pass(_repository: str, _sha: str) -> list[dict[str, Any]]:
            return [failure] if train.calls >= 3 else [failure, rerun]

        train.candidate_checks = checks_with_rerun_until_third_pass
        outcome, events = _drive(train)
        self.assertEqual((outcome, train.calls, events[-1][1]["failing_checks"]), ("failed", 3, [failure]))

    def test_a_failed_candidate_with_only_finished_passing_checks_stops_at_the_limit(self) -> None:
        train = FakeTrain([_response("candidate_failed", candidate={"candidate_sha": "abc"})])
        train.checks = [{"name": "simulator", "status": "completed", "conclusion": "success", "url": ""}]
        outcome, events = _drive(train)
        self.assertEqual((outcome, train.calls), ("failed", 5))
        self.assertEqual(events[-1][1]["reason"], "candidate_failed without a failing check")

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
        read = train.pull_request
        train.pull_request = lambda repo, number: {**read(repo, number), "merge_commit_sha": "shared-landing"}
        _, events = _drive(train)
        self.assertEqual({pr["number"]: pr["outcome"] for pr in events[-1][1]["prs"]}, {7: "landed", 9: "landed"})

    def test_unrelated_concurrent_landing_is_not_a_batch_companion(self) -> None:
        train = FakeTrain([_response("land_batch")], merge_after={7: 1, 9: 1})
        train.companions = [9]
        _, events = _drive(train)
        self.assertEqual([pr["number"] for pr in events[-1][1]["prs"]], [7])

    def test_service_membership_survives_missing_queue_and_different_merge_commits(self) -> None:
        for component in ("candidate", "landing_plan"):
            with self.subTest(component=component):
                projected = _project_controller({"status": "accepted", "trace_id": "t", "records": {}, "result": {
                    "controller_action": "land_batch",
                    component: {"entries": [{"pull_request_number": 7}, {"pull_request_number": 8}]}}})
                train = FakeTrain([projected],
                                  merge_after={7: 1, 8: 1})
                _, events = _drive(train)
                self.assertEqual({pr["number"]: pr["outcome"] for pr in events[-1][1]["prs"]},
                                 {7: "landed", 8: "landed"})

    def test_live_companion_inventory_ignores_enqueue_labels_and_reports_bounds(self) -> None:
        rows = [{"number": number, "merged_at": "2026-10-16T12:00:00Z", "labels": labels}
                for number, labels in ((8, [{"name": "ship-it"}]), (9, []))]
        with patch.object(train_drive.github_read, "GitHubReader") as reader_class:
            reader = reader_class.return_value
            reader.get_json.return_value = rows
            io = train_drive.live_io(180)
            self.assertEqual(io.merged_since(REPO, 0)["numbers"], [8, 9])
            self.assertTrue(io.merged_since(REPO, 0)["complete"])
            from urllib.parse import parse_qs, urlparse
            per_page = int(parse_qs(urlparse(reader.get_json.call_args.args[0]).query)["per_page"][0])
            reader.get_json.return_value = [rows[0]] * per_page
            inventory = io.merged_since(REPO, 0)
            self.assertFalse(inventory["complete"])

    def test_live_companion_inventory_finishes_at_the_time_window_across_pages(self) -> None:
        from urllib.parse import parse_qs, urlparse
        since = train_drive.calendar.timegm(train_drive.time.strptime("2026-10-16T11:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
        fresh = {"number": 8, "merged_at": "2026-10-16T12:00:00Z", "updated_at": "2026-10-16T12:00:00Z"}
        old = {"number": 6, "merged_at": "2026-10-15T12:00:00Z", "updated_at": "2026-10-15T12:00:00Z"}
        def page(path: str, **_kwargs: Any) -> list[dict[str, Any]]:
            query = parse_qs(urlparse(path).query)
            return ([fresh] * int(query["per_page"][0]) if query["page"] == ["1"]
                    else [{**fresh, "number": 9}, old])
        with patch.object(train_drive.github_read, "GitHubReader") as reader_class:
            reader = reader_class.return_value
            reader.get_json.side_effect = page
            inventory = train_drive.live_io(180).merged_since(REPO, since)
            self.assertTrue(inventory["complete"])
            self.assertIn(9, inventory["numbers"])
            self.assertNotIn(6, inventory["numbers"])
            self.assertEqual(reader.get_json.call_count, 2)

    def test_generated_batch_pr_is_not_an_omitted_companion(self) -> None:
        train = FakeTrain([_response("land_batch")], merge_after={7: 1, 9: 1})
        train.companions = [9]
        read = train.pull_request
        train.pull_request = lambda repo, number: {
            **read(repo, number), "merge_commit_sha": "shared-landing",
            "head": {"ref": "launchplane/train/example/app/main/batch" if number == 9 else "work/change"}}
        _, events = _drive(train)
        self.assertEqual([pr["number"] for pr in events[-1][1]["prs"]], [7])

    def test_incomplete_companion_inventory_is_in_terminal_evidence(self) -> None:
        train = FakeTrain([], merge_after={7: 0})
        io = train.io()
        io.merged_since = lambda *_args: {"numbers": [], "complete": False, "reason": "read_unavailable"}
        events = []
        outcome = train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=10000), io,
                                    lambda event, payload: events.append((event, payload)))
        self.assertEqual(outcome, "landed")
        self.assertFalse(events[-1][1]["companion_inventory"]["complete"])

    def test_unreadable_companion_and_missing_landing_sha_leave_coverage_incomplete(self) -> None:
        for missing_sha in (False, True):
            with self.subTest(missing_sha=missing_sha):
                train = FakeTrain([], merge_after={7: 0})
                train.companions = [9]
                read = train.pull_request
                train.pull_request = lambda repo, number: (
                    {**read(repo, number), "merge_commit_sha": "" if missing_sha else "landing"}
                    if number == 7 else None)
                _, events = _drive(train)
                self.assertFalse(events[-1][1]["companion_inventory"]["complete"])

    def test_preexisting_landing_uses_its_merge_time_for_companion_discovery(self) -> None:
        train = FakeTrain([], merge_after={7: 0})
        train.clock = 100
        io = train.io()
        read = io.pull_request
        io.pull_request = lambda repo, number: {**read(repo, number), "merged_at": "1970-01-01T00:00:10Z"}
        observed = []
        io.merged_since = lambda repo, since: observed.append(since) or {"numbers": [], "complete": True}
        train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=10000), io, lambda *_args: None)
        self.assertEqual(observed, [10])

    def test_uncertain_recovery_marks_companion_discovery_not_attempted(self) -> None:
        train = FakeTrain([], merge_after={7: 0})
        io = train.io()
        io.recover = lambda: {"status": "outcome_unknown", "recovery": {"reason": "unavailable"}}
        events = []
        train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=10000), io,
                          lambda event, payload: events.append((event, payload)))
        self.assertFalse(events[-1][1]["companion_inventory"]["complete"])

    def test_a_stack_landing_observes_the_service_finishing_children(self) -> None:
        train = FakeTrain(
            [_response("execute_stack_collapse", stack_collapse_plan=_stack()), _response("land_batch")],
            merge_after={7: 2},
        )
        train.observations = [_response("land_batch", stack_collapse_plan=_stack()),
                              _response("batch_landed", stack_collapse_plan=_stack(status="ready_for_train", child_status="closed"))]
        read = train.pull_request
        def pull_request(repo: str, number: int) -> dict[str, Any]:
            if train.observed >= 2:
                train.closed.add(8)
            return read(repo, number)
        train.pull_request = pull_request
        outcome, events = _drive(train)
        self.assertEqual((outcome, train.calls, train.observed), ("landed", 2, 2))
        self.assertEqual(events[-1][1]["stack_finish"], "complete")
        self.assertEqual(events[-1][1]["prs"][1]["outcome"], "superseded")

    def test_completed_stack_makes_no_post_landing_controller_calls(self) -> None:
        train = FakeTrain([_response("execute_stack_collapse", stack_collapse_plan=_stack()),
                           _response("land_batch"), _response("build_candidate", **_queue((99, [])))], merge_after={7: 2})
        train.closed.add(8)
        outcome, events = _drive(train)
        self.assertEqual((outcome, train.calls, train.observed), ("landed", 2, 0))
        self.assertEqual(events[-1][1]["stack_finish"], "complete")

    def test_unrelated_dry_run_never_advances_or_adopts_the_other_queue(self) -> None:
        for response in (_response("build_candidate", **_queue((99, []))),
                         _response("land_batch", stack_collapse_plan=_stack(collapse_id="other")),
                         _response("land_batch", stack_collapse_plan=_stack(root=99))):
            with self.subTest(response=response):
                train = FakeTrain([_response("execute_stack_collapse", stack_collapse_plan=_stack()),
                                   _response("land_batch")], merge_after={7: 2})
                train.observations = [response]
                outcome, events = _drive(train)
                self.assertEqual((outcome, train.calls, train.observed), ("landed", 2, 1))
                self.assertEqual(events[-1][1]["stack_finish"], "selection_changed")
                self.assertEqual({row["number"] for row in events[-1][1]["prs"]}, {7, 8})

    def test_stack_observation_stays_bounded_and_keeps_root_landing(self) -> None:
        for options, observations, reason in (({"max_stack_finish_passes": 2}, [_response("land_batch", stack_collapse_plan=_stack())], "pass_limit"),
                                               ({"deadline": 120}, [_response("land_batch", stack_collapse_plan=_stack())], "deadline"),
                                               ({}, [None], "observation_unavailable"),
                                               ({}, [_refusal("permission_denied")], "observation_unavailable")):
            with self.subTest(reason=reason):
                train = FakeTrain([_response("execute_stack_collapse", stack_collapse_plan=_stack()),
                                   _response("land_batch")], merge_after={7: 2})
                train.observations = observations
                outcome, events = _drive(train, **options)
                self.assertEqual((outcome, train.calls), ("landed", 2))
                self.assertEqual(events[-1][1]["landing_sha"], "sha-7")
                self.assertEqual(events[-1][1]["stack_finish"], reason)
                self.assertLessEqual(train.observed, options.get("max_stack_finish_passes", 5))

    def test_preserved_open_child_does_not_keep_finishing(self) -> None:
        train = FakeTrain([_response("execute_stack_collapse", stack_collapse_plan=_stack(child_status="preserved")),
                           _response("land_batch")], merge_after={7: 2})
        outcome, events = _drive(train)
        self.assertEqual((outcome, train.calls, train.observed), ("landed", 2, 0))
        self.assertEqual(events[-1][1]["prs"][1]["outcome"], "open")

    def test_live_stack_observation_is_a_dry_run_and_respects_deadline(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        helper = _write_action_helper()
        requests: list[dict[str, Any]] = []
        def execute_post(**kwargs: Any) -> int:
            requests.append({"body": kwargs["body"], "key": kwargs["args"].idempotency_key})
            helper.emit(_response("land_batch"))
            return 0
        def run_helper(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            output = StringIO()
            with patch.object(helper, "execute_post", side_effect=execute_post), redirect_stdout(output):
                code = helper.main(command[3:])
            return subprocess.CompletedProcess(command, code, stdout=output.getvalue(), stderr="")
        with patch("time.time", return_value=1000), patch("subprocess.run", side_effect=run_helper) as run:
            io = train_drive.live_io(180, deadline_at=1010)
            io.observe_controller(REPO, "main")
            self.assertEqual(requests[-1]["body"]["mutate"], False)
            self.assertEqual(requests[-1]["key"], "")
            self.assertLessEqual(run.call_args.kwargs["timeout"], 10)
            io.controller(REPO, "main", "mutation-key")
            self.assertEqual(requests[-1]["body"]["mutate"], True)
            self.assertEqual(requests[-1]["key"], "mutation-key")
        with patch("time.time", return_value=1010), patch("subprocess.run") as run:
            io.observe_controller(REPO, "main")
            run.assert_not_called()

    def test_cli_stops_after_completed_or_unrelated_stack_without_mutating(self) -> None:
        for complete in (True, False):
            with self.subTest(complete=complete):
                train = FakeTrain([_response("execute_stack_collapse", stack_collapse_plan=_stack()),
                                   _response("land_batch"), _response("build_candidate", **_queue((99, [])))], merge_after={7: 2})
                if complete:
                    train.closed.add(8)
                train.observations = [_response("build_candidate", **_queue((99, [])))]
                code, stop = _cli(train)
                self.assertEqual((code, stop["outcome"], train.calls), (0, "landed", 2))
                self.assertEqual(train.observed, 0 if complete else 1)
                self.assertNotIn(99, [row["number"] for row in stop["prs"]])

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
        helper = _write_action_helper()
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
        code, receipt = _cli(train)
        self.assertEqual((code, receipt["outcome"]), (0, "landed"))
        self.assertEqual(receipt["prs"], [
            {"number": 7, "outcome": "landed", "merge_commit_sha": "sha-7"},
            {"number": 8, "outcome": "superseded", "merge_commit_sha": "",
             "carried_by": {"collapse_id": "collapse-example", "root_pull_request_number": 7,
                            "expected_head_sha": "head-8"}},
        ])

    def test_cli_preserves_helper_projection_through_subprocess_and_terminal_receipt(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        raw = {
            "status": "accepted", "trace_id": "plan-trace",
            "records": {"merge_train_batch_candidate_record_id": "candidate-example",
                        "merge_train_batch_landing_plan_record_id": "plan-example"},
            "result": {
                "controller_action": "plan_landing", "candidate_sha": "candidate-sha",
                "workflow_run_url": "https://github.com/example/app/actions/runs/123",
                "candidate": {"record_id": "candidate-example", "candidate_sha": "candidate-sha",
                              "entries": [{"pull_request_number": 7}], "status": "pass",
                              "provider_note": "discard-me"},
                "landing_plan": {"record_id": "plan-example", "status": "ready"},
                "dry_run_result": {"queue_order": [7], "next_action_detail": "Candidate checks passed."},
            },
        }
        projected = _project_controller(raw)
        final = _project_controller({"status": "accepted", "trace_id": "landing-trace",
                                     "records": {"merge_train_feedback_record_id": "feedback-example"},
                                     "result": {"controller_action": "batch_landed", "landing_sha": "sha-7"}})
        train = FakeTrain([projected, final], merge_after={7: 2})
        live_controller = train_drive.live_io(10).controller
        def controller(repo: str, base: str, key: str) -> dict[str, Any]:
            reply = train.controller(repo, base, key)
            with patch.object(train_drive.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(reply), "discard-me"
            )):
                return live_controller(repo, base, key)
        io = train.io()
        io.controller = controller
        output = StringIO()
        with patch.object(train_drive, "live_io", return_value=io), redirect_stdout(output):
            code = train_drive.main(["--repo", REPO, "--pr", "7"])
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        snapshots = [event["payload"] for event in events if event["event"] == "snapshot"]
        receipt = events[-1]["payload"]
        self.assertEqual((code, receipt["outcome"]), (0, "landed"))
        for snapshot, response in zip(snapshots, (projected, final), strict=True):
            self.assertEqual(snapshot["records"], response["records"])
            self.assertEqual(snapshot["result"], response["result"])
            self.assertEqual(snapshot["trace_id"], response["summary"]["trace_id"])
        self.assertEqual(receipt["controller_evidence"], snapshots)
        self.assertEqual(receipt["landing_sha"], train.pull_request(REPO, 7)["merge_commit_sha"])
        self.assertNotIn("discard-me", output.getvalue())

    def test_evidence_changes_in_same_phase_emit_without_resetting_backoff(self) -> None:
        first = _response("observe_candidate", candidate={"candidate_sha": "candidate-sha", "status": "pending"})
        trace_only = {**first, "summary": {"trace_id": "new-trace"}}
        changed = _response("observe_candidate", candidate={"candidate_sha": "candidate-sha", "status": "pass"})
        changed["records"] = {"merge_train_batch_candidate_record_id": "candidate-example"}
        train = FakeTrain([first, trace_only, changed, _response("land_batch")], merge_after={7: 4})
        with patch.object(train_drive.github_read, "poll_delay", side_effect=lambda delay, *_args, **_kwargs: delay):
            _, events = _drive(train)
        snapshots = [payload for event, payload in events if event == "snapshot"]
        self.assertEqual([payload["result"] for payload in snapshots],
                         [response["result"] for response in (first, changed, train.responses[-1])])
        self.assertEqual(snapshots[1]["records"], changed["records"])
        self.assertEqual(events[-1][1]["controller_evidence"], snapshots)
        baseline = FakeTrain([first, first, first, _response("land_batch")], merge_after={7: 4})
        with patch.object(train_drive.github_read, "poll_delay", side_effect=lambda delay, *_args, **_kwargs: delay):
            _drive(baseline)
        self.assertEqual(train.sleeps, baseline.sleeps)

    def test_refusal_and_deadline_receipts_keep_preceding_plan_evidence(self) -> None:
        planned = _response("plan_landing", landing_plan={"record_id": "plan-example"})
        planned["records"] = {"merge_train_batch_candidate_record_id": "candidate-example"}
        refused = _refusal("merge_readiness_not_ready")
        refused["result"] = {"structural_provenance": {"status": "recorded_rolling"}}
        for responses, settings in (([planned, refused], {"max_helper_failures": 1}),
                                    ([planned], {"deadline": 60})):
            with self.subTest(settings=settings):
                outcome, events = _drive(FakeTrain(responses), **settings)
                receipt = events[-1][1]
                self.assertEqual(outcome, "error")
                self.assertEqual(receipt["controller_evidence"],
                                 [payload for event, payload in events if event == "snapshot"])
                self.assertEqual(receipt["controller_evidence"][0]["records"], planned["records"])
                self.assertEqual(receipt["controller_evidence"][-1]["result"], responses[-1]["result"])

    def test_stack_finish_evidence_survives_later_idle_response(self) -> None:
        finish = _response("complete_landing", stack_collapse_plan={
            "collapse_id": "collapse-example", "root_pull_request_number": 7,
            "child_dispositions": [{"pull_request_number": 8, "expected_head_sha": "head-8", "status": "closed"}],
        })
        train = FakeTrain([
            _response("execute_stack_collapse", stack_collapse_plan=_stack()), _response("land_batch"),
        ], merge_after={7: 2})
        # The service's completion evidence can arrive before GitHub reads
        # show the child's closure. A later idle observation must not erase it.
        train.observations = [finish, _response("idle")]
        outcome, events = _drive(train)
        self.assertEqual(outcome, "landed")
        receipt = events[-1][1]
        self.assertEqual((train.calls, train.observed), (2, 2))
        self.assertEqual(receipt["stack_finish"], "selection_changed")
        self.assertEqual(receipt["controller_evidence"][-1]["result"], finish["result"])
        self.assertEqual(receipt["controller_evidence"],
                         [payload for event, payload in events if event == "snapshot"])

    def test_closed_child_without_matching_carried_evidence_is_only_closed(self) -> None:
        for expected_head in ("", "older-head"):
            with self.subTest(expected_head=expected_head):
                response = _response("land_batch", **_queue((7, []), (8, [])), stack_collapse_plan={
                    "collapse_id": "collapse-example", "root_pull_request_number": 7,
                    "child_dispositions": ([{"pull_request_number": 8, "expected_head_sha": expected_head, "status": "closed"}]
                                           if expected_head else []),
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
            _response("execute_stack_collapse", **_queue((7, []), (9, [])), stack_collapse_plan=_stack()), _response("land_batch"),
        ], merge_after={7: 2})
        train.observations = [
            _response("batch_landed", stack_collapse_plan={
                "collapse_id": "collapse-example", "root_pull_request_number": 7,
                "status": "ready_for_train",
                "child_dispositions": [{"pull_request_number": 8, "expected_head_sha": "head-8", "status": "closed"}],
            }),
        ]
        read = train.pull_request
        def pull_request(repo: str, number: int) -> dict[str, Any]:
            if train.observed:
                train.closed.add(8)
            return read(repo, number)
        train.pull_request = pull_request
        self.assertEqual({row["number"]: row["outcome"] for row in _drive(train)[1][-1][1]["prs"]},
                         {7: "landed", 8: "superseded", 9: "open"})

    def test_an_uncertain_exchange_stops_before_another_mutation(self) -> None:
        train = FakeTrain([None])
        outcome, events = _drive(train)
        self.assertEqual(outcome, "error")
        self.assertEqual(train.calls, 1)
        self.assertIn("controller_recovery", events[-1][1])
        self.assertEqual(events[-1][1]["controller_evidence"],
                         [payload for event, payload in events if event == "snapshot"])
        self.assertTrue(events[-1][1]["controller_evidence"])

    def test_uncertain_shapes_never_start_a_fresh_pass(self) -> None:
        for response in (None, [], {"status": "no_response"},
                         {"status": "outcome_unknown"}, {"status": "accepted_unverified"},
                         {"status": "accepted", "result": None},
                         {"status": "accepted", "result": []},
                         {"status": "accepted", "result": {}},
                         {"status": "stale", "summary": {"http_status": 409, "error_code": ["bad"]}},
                         {"status": "invalid", "summary": {"projection_error_code": "invalid_response"}},
                         _refusal("client_timeout", status="unavailable", http_status=504),
                         _refusal("unknown_write", status="unavailable", http_status=502)):
            with self.subTest(response=response):
                train = FakeTrain([response, _response("idle")])
                outcome, events = _drive(train)
                self.assertEqual((outcome, train.calls, train.sleeps), ("error", 1, []))
                receipt = events[-1][1]["controller_recovery"]
                self.assertTrue(receipt["idempotency_key_sha256"])
                self.assertNotIn("idempotency_key", receipt)

    def test_unavailable_stack_observation_cannot_send_another_mutation(self) -> None:
        train = FakeTrain([_response("execute_stack_collapse", stack_collapse_plan=_stack())], merge_after={7: 1})
        train.observations = [None]
        outcome, events = _drive(train)
        self.assertEqual((outcome, train.calls, train.observed), ("landed", 1, 1))
        self.assertEqual(events[-1][1]["stack_finish"], "observation_unavailable")
        self.assertNotIn("controller_recovery", events[-1][1])

    def test_recovered_stack_landing_retains_read_only_child_observation(self) -> None:
        train = FakeTrain([], merge_after={7: 0})
        train.observations = [_response("land_batch", stack_collapse_plan=_stack())]
        io = train.io()
        io.recover = lambda: _response("execute_stack_collapse", stack_collapse_plan=_stack())
        events = []
        outcome = train_drive.drive(
            train_drive.DriveSettings(repository=REPO, number=7, deadline=100, max_stack_finish_passes=1),
            io, lambda event, payload: events.append((event, payload)))
        self.assertEqual((outcome, train.calls, train.observed), ("landed", 0, 1))
        self.assertEqual(events[-1][1]["stack_finish"], "pass_limit")

    def test_live_observation_neither_creates_nor_consumes_mutation_journal(self) -> None:
        io = train_drive.live_io(1, repository_context=REPO, tracked_pr=7)
        root = Path.home() / ".cache" / "codex-skills" / "train-drivers"
        root.mkdir(parents=True)
        journal = root / "retained.request.json"
        response = _response("wait_for_checks")
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(response), "")) as run:
            self.assertEqual(io.observe_controller(REPO, "main"), response)
            self.assertEqual(list(root.iterdir()), [])
            journal.write_text("retained uncertain mutation")
            self.assertEqual(io.observe_controller(REPO, "main"), response)
            self.assertEqual(journal.read_text(), "retained uncertain mutation")
            self.assertEqual(list(root.iterdir()), [journal])
            for call in run.call_args_list:
                self.assertNotIn("--mutate", call.args[0])
                self.assertNotIn("--idempotency-key", call.args[0])

    def test_cli_timeout_persists_request_and_blocks_plain_restart(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        train = FakeTrain([])
        io = train_drive.live_io(1, repository_context=REPO, tracked_pr=7)
        io.pull_request = train.pull_request
        with patch.object(train_drive, "live_io", return_value=io), patch("subprocess.run", side_effect=subprocess.TimeoutExpired("helper", 1)), redirect_stdout(output):
            self.assertEqual(train_drive.main(["--repo", REPO, "--pr", "7"]), train_drive.EXIT_CODES["error"])
        stop = json.loads(output.getvalue().splitlines()[-1])["payload"]
        journal = Path(stop["controller_recovery"]["request_file"])
        saved = json.loads(journal.read_text())
        self.assertEqual((saved["repository"], saved["base_branch"], saved["mutate"]), (REPO, "main", True))
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(saved["idempotency_key"], output.getvalue())
        restarted = train_drive.live_io(1, repository_context=REPO, tracked_pr=7)
        restarted.pull_request = train.pull_request
        with patch("subprocess.run") as run:
            events = []
            self.assertEqual(train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=10), restarted,
                                               lambda event, payload: events.append((event, payload))), "error")
            run.assert_not_called()
        restarted_receipt = events[-1][1]["controller_recovery"]
        self.assertTrue(restarted_receipt["reason"])
        self.assertTrue(restarted_receipt["recommendation"])
        self.assertEqual(json.loads(journal.read_text()), saved)

    def test_recovery_reads_pr_and_controller_then_replays_original_key(self) -> None:
        original = "original-private-key"
        unknown = subprocess.CompletedProcess([], 1, stdout="malformed", stderr="private diagnostic")
        settled = subprocess.CompletedProcess([], 0, stdout=json.dumps(_response("idle")), stderr="")
        with patch("subprocess.run", return_value=unknown):
            receipt = train_drive.live_io(1, tracked_pr=7).controller(REPO, "main", original)
        journal = Path(receipt["recovery"]["request_file"])
        recovering = train_drive.live_io(1, recover_controller=True, repository_context=REPO)
        # The controller's closure uses its own reader, so replace its public
        # API reader rather than drive's injected PR read.
        with patch.object(train_drive.github_read.GitHubReader, "get_json", return_value={"state": "open", "merged": False}) as read, patch("subprocess.run", return_value=settled) as run:
            result = recovering.recover()
        self.assertFalse(train_drive._uncertain(result))
        read.assert_called_once()
        self.assertEqual(run.call_count, 2)
        observed, replayed = [call.args[0] for call in run.call_args_list]
        self.assertNotIn("--mutate", observed)
        self.assertNotIn("--idempotency-key", observed)
        self.assertEqual(replayed[-3:], ["--mutate", "--idempotency-key", original])
        self.assertFalse(journal.exists())

    def test_recovery_denial_or_unavailable_read_keeps_pending_request(self) -> None:
        unknown = subprocess.CompletedProcess([], 1, stdout="null", stderr="")
        with patch("subprocess.run", return_value=unknown):
            receipt = train_drive.live_io(1, tracked_pr=7).controller(REPO, "main", "saved-key")
        journal = Path(receipt["recovery"]["request_file"])
        for read_response, replay_response in ((None, None), (_response("idle"), _refusal(train_drive.LEASE_HELD_CODE)),
                                               (_response("idle"), _refusal("authorization_denied", status="denied", http_status=403))):
            with self.subTest(read_response=read_response, replay_response=replay_response):
                results = [subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
                           for payload in (read_response, replay_response)]
                with patch.object(train_drive.github_read.GitHubReader, "get_json", return_value={"state": "open"}), patch("subprocess.run", side_effect=results) as run:
                    response = train_drive.live_io(1, recover_controller=True, repository_context=REPO).recover()
                self.assertTrue(train_drive._uncertain(response))
                self.assertTrue(journal.exists())
                self.assertEqual(run.call_count, 1 if read_response is None else 2)

    def test_recovery_of_a_landed_pr_replays_before_reporting_landing(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("helper", 1)):
            receipt = train_drive.live_io(1, tracked_pr=7).controller(REPO, "main", "lost-key")
        journal = Path(receipt["recovery"]["request_file"])
        live_factory = train_drive.live_io
        train = FakeTrain([], merge_after={7: 0})
        def factory(*args, **kwargs):
            io = live_factory(*args, **kwargs)
            io.pull_request = train.pull_request
            io.merged_since = lambda *_args: {"numbers": [], "complete": True}
            io.reconcile = lambda *_args: None
            io.reconcile_plans = lambda *_args: None
            return io
        output = StringIO()
        settled = subprocess.CompletedProcess([], 0, stdout=json.dumps(_response("idle")), stderr="")
        with patch.object(train_drive, "live_io", side_effect=factory), patch.object(train_drive.github_read.GitHubReader, "get_json", return_value={"state": "closed", "merged": True}), patch("subprocess.run", return_value=settled) as run, redirect_stdout(output):
            self.assertEqual(train_drive.main(["--repo", REPO, "--pr", "7", "--recover-controller"]), 0)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][-1], "lost-key")
        self.assertFalse(journal.exists())
        stop = json.loads(output.getvalue().splitlines()[-1])["payload"]
        self.assertEqual(stop["landing_sha"], "sha-7")
        snapshots = [row["payload"] for row in map(json.loads, output.getvalue().splitlines())
                     if row["event"] == "snapshot"]
        self.assertEqual(stop["controller_evidence"], snapshots)
        self.assertEqual(stop["controller_evidence"][0]["result"], _response("idle")["result"])

    def test_recovery_requires_available_pr_evidence(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("helper", 1)):
            receipt = train_drive.live_io(1, tracked_pr=7).controller(REPO, "main", "lost-key")
        io = train_drive.live_io(1, recover_controller=True, repository_context=REPO)
        with patch.object(train_drive.github_read.GitHubReader, "get_json", return_value=None), patch("subprocess.run") as run:
            self.assertTrue(train_drive._uncertain(io.recover()))
            run.assert_not_called()
        self.assertTrue(Path(receipt["recovery"]["request_file"]).exists())

    def test_accepted_but_unreadable_reply_releases_the_saved_request_and_stops(self) -> None:
        unreadable = {"status": "invalid", "result": {}, "records": {}, "summary": {
            "launchplane_status": "accepted", "projection_error_code": "invalid_response",
            "trace_id": "launchplane_req_accepted",
            "error_code": "merge_train_candidate_failure_isolated"}}
        unverified = {**unreadable, "summary": {k: v for k, v in unreadable["summary"].items()
                                                if k != "launchplane_status"}}
        reply = lambda payload: subprocess.CompletedProcess([], 1, stdout=json.dumps(payload), stderr="")
        # Without Launchplane's acceptance the exchange stays uncertain and its key is kept.
        with patch("subprocess.run", return_value=reply(unverified)):
            kept = train_drive.live_io(1, tracked_pr=7).controller(REPO, "main", "unverified-key")
        journal = Path(kept["recovery"]["request_file"])
        self.assertTrue(journal.exists())
        # A replay whose stored reply was accepted releases it, even though it stays unreadable.
        with patch.object(train_drive.github_read.GitHubReader, "get_json", return_value={"state": "open"}), \
                patch("subprocess.run", side_effect=[reply(_response("idle")), reply(unreadable)]) as run:
            recovered = train_drive.live_io(1, recover_controller=True, repository_context=REPO).recover()
        self.assertEqual(run.call_args.args[0][-1], "unverified-key")
        self.assertFalse(journal.exists())
        self.assertTrue(train_drive._uncertain(recovered))
        self.assertEqual(recovered["recovery"]["saved_request"], "released")
        self.assertEqual(recovered["recovery"]["projection_error_code"], "invalid_response")
        # A fresh pass with an accepted, unreadable reply never leaves a request behind.
        with patch("subprocess.run", return_value=reply(unreadable)):
            response = train_drive.live_io(1, tracked_pr=7).controller(REPO, "main", "fresh-key")
        self.assertEqual(list(journal.parent.iterdir()), [])
        train = FakeTrain([response, _response("idle")])
        outcome, events = _drive(train)
        self.assertEqual((outcome, train.calls), ("error", 1))
        receipt = events[-1][1]["controller_recovery"]
        self.assertEqual(receipt["saved_request"], "released")
        self.assertNotIn("--recover-controller", receipt["recommendation"])
        self.assertEqual(events[-1][1]["controller_evidence"][-1]["controller_action"], "controller_reply_unreadable")

    def test_known_denial_releases_its_key_but_gateway_failure_keeps_it(self) -> None:
        denial = subprocess.CompletedProcess([], 1, stdout=json.dumps(_refusal("authorization_denied", status="denied", http_status=403)), stderr="")
        invalid = subprocess.CompletedProcess([], 1, stdout=json.dumps(_refusal("merge_train_controller_invalid_state", status="invalid_request", http_status=400)), stderr="")
        gateway = subprocess.CompletedProcess([], 1, stdout=json.dumps(_refusal("gateway_failure", status="unavailable", http_status=502)), stderr="")
        with patch("subprocess.run", side_effect=[denial, invalid, gateway]) as run:
            io = train_drive.live_io(1, tracked_pr=7)
            self.assertFalse(train_drive._uncertain(io.controller(REPO, "main", "denied-key")))
            self.assertFalse(train_drive._uncertain(io.controller(REPO, "main", "invalid-key")))
            response = io.controller(REPO, "main", "gateway-key")
        self.assertTrue(train_drive._uncertain(response))
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["denied-key", "invalid-key", "gateway-key"])
        saved = json.loads(Path(response["recovery"]["request_file"]).read_text())
        self.assertEqual(saved["idempotency_key"], "gateway-key")

    def test_a_held_controller_lease_waits_out_the_lease_and_continues(self) -> None:
        train = FakeTrain([_refusal(train_drive.LEASE_HELD_CODE), _response("land_batch")], merge_after={7: 2})
        outcome, events = _drive(train, max_helper_failures=1)
        self.assertEqual(outcome, "landed")
        held = events[0][1]
        self.assertEqual((held["controller_action"], held["error_code"], held["http_status"]), ("controller_lease_held", train_drive.LEASE_HELD_CODE, 409))

    def test_an_unchanged_wait_backs_off_to_the_longest_pause(self) -> None:
        # Each controller call re-reads the whole train from GitHub, so a long CI wait must not call it every few minutes.
        train = FakeTrain([_response("wait_for_checks")])
        _drive(train, deadline=3_600, max_wait_seconds=900)
        self.assertLessEqual(train.calls, 8)
        # Pauses carry up to three seconds of jitter; the last sleep is cut short by the deadline.
        self.assertEqual([int(pause) // 10 * 10 for pause in train.sleeps[:-1]][:6], [60, 120, 240, 480, 900, 900])
        self.assertLess(max(train.sleeps), 904)

    def test_a_held_lease_backs_off_instead_of_polling_every_minute(self) -> None:
        train = FakeTrain([_refusal(train_drive.LEASE_HELD_CODE)])
        _drive(train, deadline=3_600, max_wait_seconds=900)
        self.assertLessEqual(train.calls, 8)
        self.assertGreater(train.sleeps[3], train.sleeps[0] * 4)

    def test_progress_resets_the_pause_to_the_poll_interval(self) -> None:
        train = FakeTrain([_response("wait_for_checks")] * 4 + [_response("plan_landing"), _response("land_batch")],
                          merge_after={7: 6})
        outcome, _ = _drive(train)
        self.assertEqual(outcome, "landed")
        self.assertGreater(train.sleeps[3], 400)
        self.assertLess(train.sleeps[4], 64)

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

    def _landing_path(self) -> FakeTrain:
        candidate = {"candidate_sha": "abc", "pull_request_numbers": [7]}
        return FakeTrain([_response("plan_candidate"), _response("build_candidate"),
                          _response("observe_candidate", candidate=candidate),
                          _response("plan_landing"), _response("land_batch")], merge_after={7: 5})

    def test_known_progress_calls_again_sooner_than_a_full_poll(self) -> None:
        elapsed = {}
        for progress_seconds in (60, 15):
            train = self._landing_path()
            outcome, events = _drive(train, progress_seconds=progress_seconds)
            self.assertEqual((outcome, train.calls), ("landed", 5))
            elapsed[progress_seconds] = events[-1][1]["cadence"]["elapsed_seconds"]
        # Plan, build and landing-plan each save most of a poll; candidate observation still waits a full poll.
        self.assertGreater(elapsed[60] - elapsed[15], 3 * 40)
        reasons = [(item["controller_action"], item.get("pause", {}).get("reason")) for item in events[-1][1]["cadence"]["passes"]]
        self.assertEqual(reasons, [("plan_candidate", "progress"), ("build_candidate", "progress"),
                                   ("observe_candidate", "backoff"), ("plan_landing", "progress"), ("land_batch", None)])
        self.assertGreaterEqual(train.sleeps[2], 60)

    def test_progress_pause_keeps_low_budget_floor_deadline_and_repeat_backoff(self) -> None:
        with patch.object(train_drive.github_read.github_request_usage, "polling_floor", return_value=300.0):
            train = self._landing_path()
            _drive(train)
        self.assertGreaterEqual(min(train.sleeps), 300)
        train = FakeTrain([_response("plan_candidate")])
        outcome, events = _drive(train, deadline=1_000)
        self.assertEqual((outcome, events[-1][1]["reason"]), ("error", "deadline reached"))
        # Only the first report is known progress; a repeated call-again action backs off like any other phase.
        self.assertLess(train.sleeps[0], 17)
        self.assertEqual([int(pause) // 10 * 10 for pause in train.sleeps[1:4]], [120, 240, 480])
        train = FakeTrain([_response("plan_candidate")])
        _drive(train, deadline=10)
        self.assertEqual((train.clock, train.calls), (10, 1))

    def test_operator_poll_interval_caps_the_progress_pause(self) -> None:
        train = self._landing_path()
        _drive(train, poll_seconds=5, progress_seconds=15)
        self.assertLess(max(train.sleeps[:2]), 6)

    def test_cadence_receipt_separates_requests_sleep_reads_and_read_back(self) -> None:
        train = self._landing_path()
        train.controller_delay, train.read_delay = 4.0, 1.0
        train.companions = [9]
        outcome, events = _drive(train)
        self.assertEqual(outcome, "landed")
        cadence = events[-1][1]["cadence"]
        self.assertEqual((cadence["controller_requests"], cadence["controller_seconds"]), (5, 20.0))
        self.assertEqual(cadence["sleep_seconds"], round(sum(train.sleeps), 3))
        # Read-back reads count as requests but their time is read-back, not ordinary reads.
        self.assertGreater(cadence["github_reads"], cadence["github_read_seconds"])
        self.assertGreater(cadence["read_back_seconds"], 0)
        parts = sum(cadence[key] for key in ("controller_seconds", "github_read_seconds", "sleep_seconds",
                                             "read_back_seconds", "unattributed_seconds"))
        self.assertAlmostEqual(parts, cadence["elapsed_seconds"], places=2)
        self.assertEqual([item["progress"] for item in cadence["passes"]], [True] * 5)
        self.assertEqual({item["controller_seconds"] for item in cadence["passes"]}, {4.0})

    def test_every_github_read_is_counted_and_unmeasured_time_is_unattributed(self) -> None:
        train = FakeTrain([_response("candidate_failed", candidate={"candidate_sha": "abc"})])
        def slow_checks(_repository: str, _sha: str) -> list[dict[str, str]]:
            train.clock += 30
            return [{"name": "ci", "status": "completed", "conclusion": "failure", "url": ""}]
        train.candidate_checks = slow_checks
        events = []
        io = train.io()
        # Work outside any measured call, such as local processing, is not sleep or a read.
        controller = io.controller
        io.controller = lambda *args: (setattr(train, "clock", train.clock + 7), controller(*args))[1]
        outcome = train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=10_000), io,
                                    lambda event, payload: events.append((event, payload)))
        cadence = events[-1][1]["cadence"]
        self.assertEqual(outcome, "failed")
        self.assertEqual((cadence["github_read_seconds"], cadence["controller_seconds"], cadence["sleep_seconds"]),
                         (30.0, 7.0, 0.0))
        # One PR read before the pass, the check read, and the re-read before failing.
        self.assertEqual(cadence["github_reads"], 3)
        self.assertEqual(cadence["unattributed_seconds"], 0.0)
        train = FakeTrain([_response("land_batch")], merge_after={7: 1})
        train.companions = [9]
        reads = []
        io = train.io()
        merged_since = io.merged_since
        io.merged_since = lambda *args: reads.append("merged_since") or merged_since(*args)
        events = []
        train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=10_000), io,
                          lambda event, payload: events.append((event, payload)))
        self.assertEqual(reads, ["merged_since"])
        # Initial read, landing read, read-back reads of 7's companion: merged_since is one of them.
        self.assertEqual(events[-1][1]["cadence"]["github_reads"], 4)

    def test_unmeasured_work_stays_unattributed_instead_of_sleep(self) -> None:
        train = FakeTrain([_response("update_branch"), _response("land_batch")], merge_after={7: 2})
        def slow_update(_repository: str, _number: int) -> bool:
            train.clock += 40
            return True
        train.update_branch = slow_update
        outcome, events = _drive(train, allow_branch_update=True)
        cadence = events[-1][1]["cadence"]
        self.assertEqual(outcome, "landed")
        self.assertEqual(cadence["unattributed_seconds"], 40.0)
        self.assertEqual(cadence["sleep_seconds"], round(sum(train.sleeps), 3))

    def test_waits_record_why_they_paused(self) -> None:
        train = FakeTrain([_refusal(train_drive.LEASE_HELD_CODE), _refusal("merge_train_new_refusal"),
                           _response("wait_for_checks"), _response("land_batch")], merge_after={7: 4})
        outcome, events = _drive(train)
        self.assertEqual(outcome, "landed")
        self.assertEqual([(item["controller_action"], item.get("pause", {}).get("reason"))
                          for item in events[-1][1]["cadence"]["passes"]],
                         [("controller_lease_held", "lease_held"), ("controller_refused", "refusal"),
                          ("wait_for_checks", "backoff"), ("land_batch", None)])
        train = FakeTrain([_refusal("github_request_failed", http_status=502), _response("land_batch")], merge_after={7: 2})
        io = train.io()
        io.quota_wait = lambda: 500.0
        events = []
        train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=10_000), io,
                          lambda event, payload: events.append((event, payload)))
        first = events[-1][1]["cadence"]["passes"][0]["pause"]
        self.assertEqual(first["reason"], "quota_reset")
        self.assertGreaterEqual(first["seconds"], 500)

    def test_recovery_is_one_measured_span_not_an_assumed_controller_request(self) -> None:
        train = FakeTrain([_response("land_batch")], merge_after={7: 0})
        io = train.io()
        # A local refusal and a replay look alike from outside; neither is counted as a request.
        io.recover = lambda: (setattr(train, "clock", train.clock + 12), _response("land_batch"))[1]
        events = []
        train_drive.drive(train_drive.DriveSettings(repository=REPO, number=7, deadline=10_000), io,
                          lambda event, payload: events.append((event, payload)))
        cadence = events[-1][1]["cadence"]
        self.assertEqual((events[-1][1]["outcome"], cadence["controller_requests"], cadence["recovery_seconds"]),
                         ("landed", 0, 12.0))
        self.assertEqual(cadence["passes"][0]["controller_action"], "recovery")
        self.assertEqual(cadence["unattributed_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
