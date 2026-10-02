#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Behavior tests for launchplane-train-drive.py's outcome rules."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("train_drive", SCRIPT_DIR / "launchplane-train-drive.py")
assert _SPEC is not None and _SPEC.loader is not None
train_drive = importlib.util.module_from_spec(_SPEC)
sys.modules["train_drive"] = train_drive
_SPEC.loader.exec_module(train_drive)

REPO = "example/app"


def _response(action: str, **result: Any) -> dict[str, Any]:
    return {"status": "accepted", "result": {"controller_action": action, **result}, "summary": {"trace_id": "t"}}


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

    def test_the_deadline_ends_in_an_error_instead_of_waiting_forever(self) -> None:
        outcome, events = _drive(FakeTrain([_response("wait_for_checks")]), deadline=300)
        self.assertEqual(outcome, "error")
        self.assertEqual(events[-1][1]["reason"], "deadline reached")


if __name__ == "__main__":
    unittest.main()
