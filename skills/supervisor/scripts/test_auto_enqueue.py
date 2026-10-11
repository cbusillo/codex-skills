#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Hermetic evidence for Supervisor enqueue guards and mutation ordering."""

import argparse
import copy
import json
import subprocess
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import auto_enqueue as watch
from ready_handoff import READY, WAITING

REPO = "example/tool"
CLIENT = "example/client-product"
SHA = "a" * 40
TARGET = {
    "repo": REPO,
    "base": "trunk",
    "label": "enqueue-from-policy",
    "policy": {"record_id": "revision"},
}


def pr(number=1):
    return {
        "number": number,
        "head": {"sha": SHA},
        "base": {"ref": "trunk"},
        "draft": False,
        "state": "open",
        "user": {"login": "engineer"},
        "labels": [],
        "body": "",
        "created_at": f"2026-01-01T00:00:{number:02d}Z",
        "changed_files": 1,
    }


def comment(body=None, number=1, login="engineer"):
    return {
        "id": number,
        "user": {"login": login},
        "created_at": f"2026-01-01T00:01:{number:02d}Z",
        "body": body
        if body is not None
        else f"{READY}\n\nHead: {SHA}. Verified.",
    }


def response(pulls, own=(), **result) -> dict[str, Any]:
    return {
        "status": "accepted",
        "result": {
            "mode": "dry-run",
            "controller_action": "idle",
            "dry_run_result": {
                "queue_order": list(own),
                "queue": [
                    {
                        "number": p["number"],
                        "head_sha": p["head"]["sha"],
                        "actor_role": "trusted_automation",
                        "mergeable": "mergeable",
                        "required_checks_status": "pass",
                        "eligible": p["number"] in own
                        or TARGET["label"] in {label["name"] for label in p["labels"]},
                        "branch_update_required": False,
                        "ineligible_reasons": []
                        if p["number"] in own
                        or TARGET["label"] in {label["name"] for label in p["labels"]}
                        else [f"missing {TARGET['label']} label"],
                    }
                    for p in pulls
                ],
            },
            **result,
        },
    }


def candidate_response(
    own, *, status="ready_for_checks", action="observe_candidate", records=None, queue=None
):
    result: dict[str, Any] = {
        "mode": "dry-run",
        "controller_action": action,
        "candidate": {
            "status": status,
            "base_sha": SHA,
            "candidate_ref": "launchplane/train/example/batch",
            "candidate_sha": "c" * 40,
            "entries": [
                {"pull_request_number": number, "head_sha": SHA} for number in own
            ],
        },
    }
    if queue is not None:
        result["dry_run_result"] = queue
    return watch.operator.summarize_success(
        operation="merge-train-controller-run-once",
        request={"repository": REPO},
        provider_payload={
            "status": "accepted",
            "records": records
            if records is not None
            else {"merge_train_batch_candidate_record_id": "active"},
            "result": result,
        },
    )


class Fake:
    def __init__(self, count=6):
        self.pulls = {number: pr(number) for number in range(1, count + 1)}
        self.writes = []
        self.qualify = True
        self.active = False
        self.calls = 0
        self.before = lambda _self: None
        self.targets = [copy.deepcopy(TARGET)]
        self.after_label_action = "idle"
        self.ineligible = {}
        self.scheduler_owns_batch = False
        self.candidate_status = "ready_for_checks"

    def policies(self, _owner):
        return copy.deepcopy(self.targets)

    def train(self, _target):
        self.calls += 1
        self.before(self)
        value = response(
            self.pulls.values(),
            self.writes,
            **(
                {
                    "controller_action": "observe_candidate",
                    "merge_train_batch_candidate_record_id": "active",
                }
                if self.active
                else {
                    "controller_action": self.after_label_action
                    if self.writes
                    else "idle"
                }
            ),
        )

        dry = value["result"]["dry_run_result"]
        for row in dry["queue"]:
            if row["number"] in self.ineligible:
                row["ineligible_reasons"] = self.ineligible[row["number"]]
        dry["queue_order"] = [
            row["number"] for row in dry["queue"] if not row["ineligible_reasons"]
        ]
        if self.scheduler_owns_batch and self.writes:
            # A normal active-candidate observation omits dry_run_result.
            value = candidate_response(self.writes, status=self.candidate_status)
        return value

    def get(self, path):
        return copy.deepcopy(self.pulls[int(path.rsplit("/", 1)[-1])])

    def pages(self, path, **_params):
        if path.endswith("/labels"):
            return self.pulls[int(path.split("/")[-2])]["labels"]
        if path.endswith("/files"):
            number = int(path.split("/")[-2])
            return [{"filename": "src/code.py" if number != 3 else "docs/guide.md"}]
        return copy.deepcopy(list(self.pulls.values()))

    def qualified(self, *_args):
        return self.qualify

    def label(self, _target, number):
        self.writes.append(number)
        self.pulls[number]["labels"].append({"name": TARGET["label"]})


class EnqueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "holds.json"
        self.holds = {
            "paused_repositories": [],
            "coordinated_repositories": [],
            "client_releases": [],
        }
        self.save()
        self.args = argparse.Namespace(
            owner="example",
            repo=[],
            hold_file=self.path,
            handoff_login=[],
            max_batch_size=4,
            apply=True,
        )
        self.events = []

    def save(self):
        self.path.write_text(json.dumps(self.holds))

    def run_pass(self, fake):
        watch.run_pass(
            fake, self.args, lambda event, payload: self.events.append((event, payload))
        )

    def test_capped_area_group_and_repeat_does_not_extend_queued_batch(self):
        fake = Fake()
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        handoff = next(
            payload for event, payload in self.events if event == "driver_handoff"
        )
        self.assertEqual(handoff["prs"], fake.writes)
        self.assertIn("launchplane-train-drive.py", handoff["argv"][2])

    def test_custom_limit_and_dry_run(self):
        fake = Fake()
        self.args.max_batch_size = 2
        self.args.apply = False
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertEqual(
            next(data["prs"] for event, data in self.events if event == "batch"), [1, 2]
        )

    def test_active_candidate_keeps_all_arrivals_unlabeled(self):
        fake = Fake()
        fake.active = True
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_unavailable_controller_with_null_result_stays_busy(self):
        fake = Fake()
        fake.train = lambda _target: {"status": "unavailable", "result": None}
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "busy" for event, _ in self.events))

    def test_candidate_starts_between_labels_stops_remaining_group(self):
        fake = Fake()
        fake.before = lambda io: setattr(io, "active", bool(io.writes))
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])

    def test_scheduler_candidate_from_own_labels_finishes_selected_batch(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        self.assertFalse(any(event == "batch_stopped" for event, _ in self.events))
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])

    def test_own_candidate_planning_building_and_passed_states_continue(self):
        for candidate_status in ("planned", "building", "passed"):
            with self.subTest(candidate_status=candidate_status):
                fake = Fake()
                fake.scheduler_owns_batch = True
                fake.candidate_status = candidate_status
                self.run_pass(fake)
                self.assertEqual(fake.writes, [1, 2, 4, 5])

    def test_own_scheduler_candidate_preserves_unchanged_held_labels(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        fake.pulls[6]["labels"] = [
            {"name": TARGET["label"]}, {"name": "merge-blocked"}
        ]
        fake.ineligible[6] = ["held by merge-blocked label"]
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        self.assertEqual(
            fake.pulls[6]["labels"],
            [{"name": TARGET["label"]}, {"name": "merge-blocked"}],
        )

    def test_queue_free_observation_cannot_preserve_a_nonlabel_hold(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        fake.pulls[6]["labels"] = [{"name": TARGET["label"]}]
        fake.ineligible[6] = ["actor role is not allowed to enqueue"]
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])

    def test_own_scheduler_candidate_stops_when_held_labels_change(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        fake.pulls[6]["labels"] = [
            {"name": TARGET["label"]}, {"name": "merge-blocked"}
        ]
        fake.ineligible[6] = ["held by merge-blocked label"]

        def before(io):
            if io.writes:
                io.pulls[6]["labels"] = [{"name": TARGET["label"]}]

        fake.before = before
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])
        self.assertTrue(any(event == "batch_stopped" for event, _ in self.events))

    def test_own_candidate_does_not_hide_new_foreign_queue_labels(self):
        fake = Fake()
        fake.scheduler_owns_batch = True

        def before(io):
            if io.writes:
                io.pulls[6]["labels"].append({"name": TARGET["label"]})

        fake.before = before
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])
        self.assertTrue(any(event == "batch_stopped" for event, _ in self.events))

    def test_own_candidate_reflow_after_next_label_finishes_batch(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        original_train = fake.train

        def train(target):
            value = original_train(target)
            if len(fake.writes) > 1:
                return candidate_response(
                    fake.writes,
                    action="plan_candidate",
                    queue=response(fake.pulls.values(), fake.writes)["result"][
                        "dry_run_result"
                    ],
                    records={
                        "superseded_merge_train_batch_candidate_record_id": "active"
                    },
                )
            return value

        fake.train = train
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])

    def test_reflow_of_unobserved_candidate_stops_batch(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        original_train = fake.train

        def train(target):
            value = original_train(target)
            if fake.writes:
                value["result"]["controller_action"] = "plan_candidate"
                value["records"] = {
                    "superseded_merge_train_batch_candidate_record_id": "unobserved"
                }
            return value

        fake.train = train
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])

    def test_own_candidate_retains_each_members_head_labels_and_readiness_guard(self):
        for changed in ("head", "hold", "readiness"):
            with self.subTest(changed=changed):
                fake = Fake()
                fake.scheduler_owns_batch = True

                def before(io):
                    if io.writes:
                        if changed == "head":
                            io.pulls[2]["head"]["sha"] = "b" * 40
                        elif changed == "hold":
                            io.pulls[2]["labels"].append({"name": "merge-blocked"})
                        else:
                            io.qualify = False

                fake.before = before
                self.run_pass(fake)
                self.assertEqual(fake.writes, [1])

    def test_unrelated_or_incomplete_candidate_evidence_stops_batch(self):
        for mutation in (
            lambda value: value["candidate"].update(pull_request_numbers=[6]),
            lambda value: value["candidate"].update(pull_request_numbers=[]),
            lambda value: value["candidate"].pop("pull_request_numbers"),
            lambda value: value["candidate"].update(entries_count=2),
            lambda value: value["candidate"].update(status="failed"),
            lambda value: value.update(controller_action="plan_landing"),
            lambda value: value.update(controller_reconciliation_status="required"),
            lambda value: value.update(landing_plan={"record_id": "landing"}),
            lambda value: value.update(stack_collapse_plan={"record_id": "stack"}),
            lambda value: value.update(active_record_id="lease"),
            lambda value: value.update(error={"code": "failure"}),
            lambda value: value.update(
                dry_run_result={"queue": [], "queue_order": []}
            ),
        ):
            fake = Fake()
            fake.scheduler_owns_batch = True
            original_train = fake.train

            def train(target):
                value = original_train(target)
                if fake.writes:
                    mutation(value["result"])
                return value

            fake.train = train
            self.run_pass(fake)
            self.assertEqual(fake.writes, [1])

    def test_own_candidate_with_fresh_queue_still_checks_member_disposition(self):
        fake = Fake()
        fake.scheduler_owns_batch = True
        original_train = fake.train

        def train(target):
            value = original_train(target)
            if fake.writes:
                queue = response(fake.pulls.values(), fake.writes)["result"][
                    "dry_run_result"
                ]
                queue["queue"][1].update(
                    eligible=False,
                    ineligible_reasons=["actor role is not allowed to enqueue"],
                )
                return candidate_response(fake.writes, queue=queue)
            return value

        fake.train = train
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])

    def test_unready_member_never_labeled(self):
        fake = Fake()
        fake.qualify = False
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_paused_coordinated_and_ready_or_open_release(self):
        for key, value in (
            ("paused_repositories", [REPO]),
            ("coordinated_repositories", [REPO]),
            ("client_releases", [{"repository": REPO, "status": "ready"}]),
            ("client_releases", [{"repository": REPO, "status": "open"}]),
        ):
            with self.subTest(key=key, value=value):
                self.holds[key] = value
                self.save()
                fake = Fake()
                self.run_pass(fake)
                self.assertEqual(fake.writes, [])
                self.holds[key] = []

    def test_client_release_pauses_only_its_product(self):
        self.holds["client_releases"] = [{"repository": CLIENT, "status": "open"}]
        self.save()
        self.assertTrue(watch.held(self.holds, CLIENT))
        fake = Fake()
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])

    def test_missing_or_invalid_holds_refuse_before_effect(self):
        for value in ("{}", "{", '{"paused_repositories": []}'):
            self.path.write_text(value)
            fake = Fake()
            with self.assertRaises(ValueError):
                self.run_pass(fake)
            self.assertEqual(fake.writes, [])
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_pass(Fake())

    def test_hold_change_during_scan_refuses(self):
        fake = Fake()
        original = fake.qualified

        def qualifies(*args):
            self.holds["paused_repositories"] = [REPO]
            self.save()
            return original(*args)

        fake.qualified = qualifies
        with self.assertRaises(ValueError):
            self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_policy_change_during_scan_refuses(self):
        fake = Fake()

        def qualifies(*_args):
            fake.targets[0]["label"] = "changed-label"
            return True

        fake.qualified = qualifies
        with self.assertRaises(ValueError):
            self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_head_changes_before_write_refuse(self):
        fake = Fake()

        def before(io):
            if io.calls == 2:
                io.pulls[1]["head"]["sha"] = "b" * 40

        fake.before = before
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])

    def test_changed_file_inventory_skips_that_pr_and_continues(self):
        fake = Fake()
        fake.pulls[1]["changed_files"] = 2
        self.run_pass(fake)
        self.assertEqual(fake.writes, [2, 4, 5, 6])
        self.assertTrue(
            any(event == "skipped" and data["pr"] == 1 for event, data in self.events)
        )

    def test_repository_filter_cannot_create_enrollment(self):
        fake = Fake()
        self.args.repo = ["example/not-enrolled"]
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.calls, 0)

    def test_lock_refuses_second_watcher_and_pause_updates_use_same_inode(self):
        with watch.hold_lock(self.path):
            with self.assertRaises(BlockingIOError):
                with watch.hold_lock(self.path):
                    pass
        inode = self.path.with_name(self.path.name + ".lock").stat().st_ino
        watch.edit_holds(["--hold-file", str(self.path), "--release-ready", REPO])
        self.assertTrue(watch.held(watch.read_holds(self.path), REPO))
        watch.edit_holds(
            ["--hold-file", str(self.path), "--release-finished", REPO, "--pause", REPO]
        )
        self.assertEqual(watch.read_holds(self.path)["client_releases"], [])
        self.assertTrue(watch.held(watch.read_holds(self.path), REPO))
        self.assertEqual(
            self.path.with_name(self.path.name + ".lock").stat().st_ino, inode
        )
        watch.edit_holds(["--hold-file", str(self.path), "--resume", REPO.upper()])
        self.assertFalse(watch.held(watch.read_holds(self.path), REPO))

    def test_latest_wait_stale_head_missing_head_and_untrusted_ready_refuse(self):
        self.assertTrue(watch.latest_ready(pr(), [comment()], {"engineer"}))
        for entries in (
            [comment(), comment(f"{WAITING} the Client's acceptance", 2)],
            [comment(READY)],
            [comment(f"{READY}\nHead: {'b' * 40}")],
            [comment(login="stranger")],
            [comment(f"> {READY}\nHead: {SHA}")],
        ):
            with self.subTest(entries=entries):
                self.assertFalse(watch.latest_ready(pr(), entries, {"engineer"}))

    def test_unknown_reconciliation_or_candidate_without_queue_never_proves_idle(self):
        for payload in (
            {},
            {"status": "unavailable"},
            response([], controller_action="resume_reconciliation"),
            response([], candidate_record_id="stored"),
            response([], controller_reconciliation_status="required"),
        ):
            self.assertFalse(watch.idle(payload))
        self.assertTrue(watch.idle(response([])))

    def test_renames_group_both_top_level_areas(self):
        self.assertEqual(
            watch.areas([{"filename": "src/x", "previous_filename": "legacy/x"}]),
            {"src", "legacy"},
        )

    def test_behind_base_root_can_accumulate_its_group_without_branch_mutation(self):
        fake = Fake()
        fake.after_label_action = "update_branch"
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        self.assertFalse(watch.idle(response([], controller_action="update_branch")))

    def test_live_label_adapter_finishes_the_batch_and_driver_handoff(self):
        fake = Fake()
        adapter = object.__new__(watch.Live)
        adapter.deadline = 10_000
        adapter.pages = fake.pages
        fake.label = adapter.label

        def write(_method, path, _body, **_kwargs):
            number = int(path.split("/")[-2])
            fake.writes.append(number)
            fake.pulls[number]["labels"].append({"name": TARGET["label"]})
            return api_result("confirmed")

        with patch.object(watch.github_api, "call_gh_with_retry", side_effect=write):
            self.run_pass(fake)
        self.assertEqual(fake.writes, [1, 2, 4, 5])
        self.assertTrue(any(event == "driver_handoff" for event, _data in self.events))

    def test_only_held_labeled_prs_allow_the_next_ready_batch(self):
        fake = Fake()
        for number, reason in (
            (1, "held by merge-blocked label"),
            (2, "actor role is not allowed to enqueue"),
        ):
            fake.pulls[number]["labels"] = [{"name": TARGET["label"]}]
            fake.ineligible[number] = [reason]
        self.run_pass(fake)
        self.assertEqual(fake.writes, [3])
        self.assertEqual(fake.pulls[1]["labels"], [{"name": TARGET["label"]}])
        self.assertEqual(fake.pulls[2]["labels"], [{"name": TARGET["label"]}])
        self.run_pass(fake)
        self.assertEqual(fake.writes, [3])

    def test_running_candidate_preserves_held_labeled_members(self):
        fake = Fake()
        fake.pulls[1]["labels"] = [{"name": TARGET["label"]}]
        fake.ineligible[1] = ["held by merge-blocked label"]
        fake.active = True
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "busy" for event, _ in self.events))

    def test_eligible_labeled_member_preserves_batch_with_held_members(self):
        fake = Fake()
        for number in (1, 2):
            fake.pulls[number]["labels"] = [{"name": TARGET["label"]}]
        fake.ineligible[1] = ["held by merge-blocked label"]
        fake.ineligible[2] = []
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "busy" for event, _ in self.events))

    def test_labeled_member_missing_from_controller_inventory_stays_busy(self):
        fake = Fake()
        fake.pulls[1]["labels"] = [{"name": TARGET["label"]}]
        original_train = fake.train

        def missing(target):
            value = original_train(target)
            value["result"]["dry_run_result"]["queue"] = []
            return value

        fake.train = missing
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "busy" for event, _ in self.events))

    def test_label_added_after_controller_read_preserves_batch(self):
        fake = Fake()
        self.args.apply = False
        original_pages = fake.pages

        def label_during_read(path, **params):
            fake.pulls[1]["labels"] = [{"name": TARGET["label"]}]
            return original_pages(path, **params)

        fake.pages = label_during_read
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "busy" for event, _ in self.events))
        self.assertFalse(any(event == "batch" for event, _ in self.events))

    def test_unknown_label_author_preserves_batch(self):
        fake = Fake()
        fake.pulls[1]["labels"] = [{"name": TARGET["label"]}]
        fake.ineligible[1] = [
            f"{TARGET['label']} label ignored: could not read who applied it"
        ]
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        self.assertTrue(any(event == "busy" for event, _ in self.events))

    def test_unknown_label_author_between_writes_stops_batch(self):
        fake = Fake()

        def before(io):
            if io.calls == 3:
                io.pulls[6]["labels"] = [{"name": TARGET["label"]}]
                io.ineligible[6] = [
                    f"{TARGET['label']} label ignored: could not read who applied it"
                ]

        fake.before = before
        self.run_pass(fake)
        self.assertEqual(fake.writes, [1])
        self.assertTrue(any(event == "batch_stopped" for event, _ in self.events))

    def test_eligible_labeled_member_outside_queue_order_preserves_batch(self):
        fake = Fake()
        fake.pulls[1]["labels"] = [{"name": TARGET["label"]}]
        original_train = fake.train

        def truncated_order(target):
            value = original_train(target)
            value["result"]["dry_run_result"]["queue_order"] = []
            return value

        fake.train = truncated_order
        self.run_pass(fake)
        self.assertEqual(fake.writes, [])
        busy = next(data for event, data in self.events if event == "busy")
        self.assertEqual(busy["labeled"], [1])

    def test_held_labels_on_other_enrolled_base_allow_next_batch(self):
        fake = Fake()
        fake.targets.append({**TARGET, "base": "release"})
        fake.pulls[2]["base"]["ref"] = "release"
        for number in (1, 2):
            fake.pulls[number]["labels"] = [{"name": TARGET["label"]}]
            fake.ineligible[number] = ["held by merge-blocked label"]
        original_train = fake.train

        def scoped(target):
            value = original_train(target)
            dry = value["result"]["dry_run_result"]
            dry["queue"] = [
                row
                for row in dry["queue"]
                if fake.pulls[row["number"]]["base"]["ref"] == target["base"]
            ]
            dry["queue_order"] = [
                row["number"] for row in dry["queue"] if not row["ineligible_reasons"]
            ]
            return value

        fake.train = scoped
        self.run_pass(fake)
        self.assertEqual(fake.writes, [3])

    def test_an_edited_wait_invalidates_a_later_created_ready(self):
        waiting = comment(f"{WAITING} the Client's acceptance")
        waiting["updated_at"] = "2026-01-01T00:05:00Z"
        self.assertFalse(
            watch.latest_ready(pr(), [waiting, comment(number=2)], {"engineer"})
        )


def api_result(certainty=None):
    result = watch.github_api.ApiResult(ok=True, status=200, body=[])
    if certainty is not None:
        result.retry_summary = watch.github_api.RetrySummary(
            attempts=1,
            elapsed_wait=0,
            retry_eligible=False,
            last_actor="automation",
            last_bucket="rest_core",
            outcome_certainty=certainty,
            reconciliation=None,
            recommended_next_action="none",
            effective_deadline=10_000,
        )
    return result


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.io = object.__new__(watch.Live)
        self.io.reader = object()
        self.pull = pr()
        self.comments = [comment()]
        self.reviews = []
        self.requirements = {"checks": [{"context": "CI", "app_id": 7}]}
        self.checks = {
            "summary": {
                "countsComplete": True,
                "unavailableComponents": [],
                "failingCount": 0,
                "pendingCount": 0,
                "unfinishedWorkflowRunCount": 0,
            },
            "checkRuns": [
                {
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "appId": 7,
                }
            ],
            "statuses": [],
        }
        self.io.get = lambda _path: self.requirements
        self.io.pages = lambda path: (
            self.reviews if path.endswith("/reviews") else self.comments
        )
        self.io.acceptance = lambda *_args: False
        mock = patch.object(
            watch,
            "pull_request_checks",
            side_effect=lambda *_args, **_kwargs: self.checks,
        )
        mock.start()
        self.addCleanup(mock.stop)

    def qualified(self):
        return self.io.qualified(TARGET, self.pull, set())

    def test_green_required_check_is_bound_to_expected_app(self):
        self.assertTrue(self.qualified())
        self.checks["checkRuns"][0]["appId"] = 99
        self.assertFalse(self.qualified())

    def test_missing_pending_failed_unavailable_required_checks_refuse(self):
        for field, value in (
            ("countsComplete", False),
            ("unavailableComponents", ["checkRuns"]),
            ("pendingCount", 1),
            ("failingCount", 1),
            ("unfinishedWorkflowRunCount", 1),
        ):
            original = copy.deepcopy(self.checks)
            self.checks["summary"][field] = value
            self.assertFalse(self.qualified())
            self.checks = original
        self.checks["checkRuns"] = []
        self.assertFalse(self.qualified())

    def test_client_request_notes_and_label_require_recorded_acceptance(self):
        for configure in (
            lambda: self.pull["labels"].append({"name": "owner-review"}),
            lambda: self.checks["checkRuns"].append(
                {"name": "launchplane/owner-review"}
            ),
            lambda: self.pull.update(body="## Client test notes\nTry the preview."),
        ):
            configure()
            self.assertFalse(self.qualified())
        self.io.acceptance = lambda *_args: True
        self.assertTrue(self.qualified())

    def test_latest_current_head_changes_requested_refuses(self):
        self.reviews = [
            {
                "id": 1,
                "user": {"login": "reviewer"},
                "state": "CHANGES_REQUESTED",
                "commit_id": SHA,
            }
        ]
        self.assertFalse(self.qualified())
        self.reviews.append(
            {
                "id": 2,
                "user": {"login": "reviewer"},
                "state": "APPROVED",
                "commit_id": SHA,
            }
        )
        self.assertTrue(self.qualified())

    def test_acceptance_uses_service_decision_and_exact_head(self):
        self.io.args = argparse.Namespace(
            config=None, env_config=None, url=None, timeout=10
        )
        for decision, head, expected in (
            ("accepted", SHA, True),
            ("accepted", "b" * 40, False),
            ("rejected", SHA, False),
        ):
            with patch.object(
                watch.review,
                "read_review",
                return_value={"decision": decision, "head_sha": head},
            ):
                self.assertEqual(
                    watch.Live.acceptance(self.io, REPO, self.pull), expected
                )

    def test_review_scope_denial_holds_only_that_pr(self):
        self.io.args = watch.operator.parse_args(["merge-train-policy-read"])
        denial = urllib.error.HTTPError(
            "https://example.invalid/review", 403, "scope denied", Message(), None
        )
        with patch.object(watch.review, "read_review", side_effect=denial):
            self.assertFalse(watch.Live.acceptance(self.io, REPO, self.pull))

    def test_live_label_requires_confirmed_or_reconciled_write_and_readback(self):
        self.io.deadline = 10_000
        self.io.pages = lambda _path: [{"name": TARGET["label"]}]
        for certainty in ("confirmed", "reconciled_applied"):
            with patch.object(
                watch.github_api,
                "call_gh_with_retry",
                return_value=api_result(certainty),
            ) as call:
                watch.Live.label(self.io, TARGET, 1)
                self.assertEqual(call.call_count, 1)
        for certainty in (None, "unknown", "not_applicable"):
            with patch.object(
                watch.github_api,
                "call_gh_with_retry",
                return_value=api_result(certainty),
            ) as call:
                with self.assertRaises(ValueError):
                    watch.Live.label(self.io, TARGET, 1)
                self.assertEqual(call.call_count, 1)
        self.io.pages = lambda _path: []
        with patch.object(
            watch.github_api, "call_gh_with_retry", return_value=api_result("confirmed")
        ):
            with self.assertRaises(ValueError):
                watch.Live.label(self.io, TARGET, 1)


class WatchLoopTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "holds.json"
        self.path.write_text(json.dumps({
            "paused_repositories": [],
            "coordinated_repositories": [],
            "client_releases": [],
        }))

    def run_watch(self, factory, *, once=False, duration=8):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        argv = [
            "--owner", "example", "--hold-file", str(self.path), "--apply",
            "--interval", "4", "--deadline-seconds", str(duration),
        ]
        if once:
            argv.append("--once")
        with (
            patch.object(watch, "Live", side_effect=factory) as live,
            patch.object(watch.time, "time", side_effect=lambda: now[0]),
            patch.object(watch.time, "sleep", side_effect=sleep) as sleeps,
            patch("builtins.print") as output,
        ):
            result = watch.main(argv)
        events = [json.loads(call.args[0]) for call in output.call_args_list]
        return result, live.call_count, sleeps.call_args_list, events

    def test_transient_reads_end_only_pass_then_fresh_pass_enqueues(self):
        github_error = watch.GitHubReadError(
            "private provider detail",
            result=watch.github_api.ApiResult(
                ok=False, status=503, body=None,
                failure=watch.github_api.FailureDetail(
                    cause="network_provider_failure", message="private",
                    retryable=True, fallback_eligible=False, disposition="retry",
                ),
            ),
            diagnostics={},
        )
        for error in (
            urllib.error.HTTPError("https://private.invalid", 503, "private", Message(), None),
            urllib.error.URLError("private"),
            TimeoutError("private"),
            ConnectionResetError("private"),
            github_error,
        ):
            for stage in ("initialize", "scan", "before_write"):
                with self.subTest(error=type(error).__name__, stage=stage):
                    failed = Fake(count=1)
                    healthy = Fake(count=1)
                    if stage == "scan":
                        failed.pages = Mock(side_effect=error)
                    elif stage == "before_write":
                        def before(io):
                            if io.calls == 2:
                                raise error

                        failed.before = before
                    factory = Mock(side_effect=[
                        error if stage == "initialize" else failed, healthy,
                    ])
                    result, calls, sleeps, events = self.run_watch(factory)
                    self.assertEqual(result, 0)
                    self.assertEqual(calls, 2)
                    self.assertEqual([call.args[0] for call in sleeps], [4, 4])
                    self.assertEqual(failed.writes, [])
                    self.assertEqual(healthy.writes, [1])
                    self.assertEqual(
                        sum(event["event"] == "pass_failed" for event in events), 1,
                    )
                    self.assertEqual(events[-1]["payload"]["outcome"], "deadline")
                    self.assertNotIn("private", json.dumps(events))

    def test_retry_sleep_is_bounded_by_deadline(self):
        result, calls, sleeps, events = self.run_watch(
            Mock(side_effect=TimeoutError()), duration=2,
        )
        self.assertEqual(result, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(sleeps[0].args, (2,))
        self.assertEqual(events[-1]["payload"]["outcome"], "deadline")
        self.assertEqual(events[-1]["payload"]["completed_passes"], 0)
        self.assertEqual(events[-1]["payload"]["failed_passes"], 1)

    def test_once_reports_failed_read_without_retry_or_success(self):
        result, calls, sleeps, events = self.run_watch(
            Mock(side_effect=urllib.error.URLError("private")), once=True,
        )
        self.assertEqual((result, calls, sleeps), (1, 1, []))
        self.assertEqual([event["event"] for event in events], ["pass_failed"])

    def test_nontransient_read_failures_still_stop(self):
        for error in (
            urllib.error.HTTPError("https://private.invalid", 403, "private", Message(), None),
            FileNotFoundError("private"),
            ValueError("private"),
            watch.GitHubReadShapeError("private"),
        ):
            with self.subTest(error=type(error).__name__):
                result, calls, sleeps, events = self.run_watch(
                    Mock(side_effect=error),
                )
                self.assertEqual((result, calls, sleeps), (1, 1, []))
                self.assertEqual(events[-1]["payload"]["outcome"], "error")
                self.assertNotIn("private", json.dumps(events))

    def test_shared_cli_timeout_recovers_but_setup_and_auth_failures_stop(self):
        for message, expected in (
            ("i/o timeout", True),
            ("connection reset by peer", True),
            ("Failed to launch GitHub CLI (FileNotFoundError)", False),
            ("local wrapper configuration is missing", False),
            ("permission denied", False),
            ("bad credentials", False),
        ):
            with self.subTest(message=message):
                failure = watch.github_api.classify_legacy_failure(message)
                error = watch.GitHubReadError(
                    "read failed", diagnostics={},
                    result=watch.github_api.ApiResult(
                        ok=False, status=0, body=None, failure=failure,
                    ),
                )
                healthy = Fake(count=1)
                result, calls, sleeps, events = self.run_watch(Mock(side_effect=[error, healthy]))
                self.assertEqual(result, 0 if expected else 1)
                self.assertEqual(calls, 2 if expected else 1)
                self.assertEqual(healthy.writes, [1] if expected else [])
                self.assertEqual(len(sleeps), 2 if expected else 0)
                self.assertEqual(events[0]["event"], "pass_failed" if expected else "stop")

    def test_controller_read_transport_failure_recovers_without_retrying_rejections(self):
        adapter = object.__new__(watch.Live)
        adapter.deadline = 10_000
        request = {"repository": REPO, "base_branch": TARGET["base"], "mutate": False}
        for code, http_status, expected in (
            ("provider_unavailable", None, True),
            ("client_timeout", None, True),
            ("service_error", 503, True),
            ("permission_denied", 403, False),
            ("unsafe_redirect", None, False),
        ):
            with self.subTest(code=code):
                payload = watch.operator.unavailable_payload(
                    operation="merge-train-controller-run-once", request=request,
                    status="unavailable", code=code, message="private",
                )
                if http_status is not None:
                    payload["summary"]["http_status"] = http_status
                failed = Fake(count=1)
                failed.train = adapter.train
                healthy = Fake(count=1)
                with patch.object(watch.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 1, stdout=json.dumps(payload), stderr="private",
                )):
                    result, calls, sleeps, events = self.run_watch(Mock(side_effect=[failed, healthy]))
                self.assertEqual(result, 0 if expected else 1)
                self.assertEqual(calls, 2 if expected else 1)
                self.assertEqual(len(sleeps), 2 if expected else 0)
                self.assertEqual(healthy.writes, [1] if expected else [])
                self.assertNotIn("private", json.dumps(events))

    def test_transport_error_during_label_dispatch_never_replays(self):
        fake = Fake(count=1)
        fake.label = Mock(side_effect=ConnectionResetError("private"))
        result, calls, sleeps, events = self.run_watch(lambda _deadline: fake)
        self.assertEqual((result, calls, sleeps), (1, 1, []))
        self.assertEqual(fake.label.call_count, 1)
        self.assertEqual(events[-1]["payload"]["outcome"], "error")

    def test_uncertain_label_write_or_failed_readback_stops_without_replay(self):
        for certainty in ("unknown", "confirmed"):
            with self.subTest(certainty=certainty):
                fake = Fake(count=1)
                adapter = object.__new__(watch.Live)
                adapter.deadline = 10_000
                adapter.pages = Mock(side_effect=urllib.error.URLError("private"))
                fake.label = adapter.label
                with patch.object(
                    watch.github_api, "call_gh_with_retry", return_value=api_result(certainty),
                ) as write:
                    result, calls, sleeps, events = self.run_watch(lambda _deadline: fake)
                self.assertEqual((result, calls, sleeps), (1, 1, []))
                self.assertEqual(write.call_count, 1)
                self.assertEqual(adapter.pages.call_count, int(certainty == "confirmed"))
                self.assertEqual(events[-1]["payload"]["outcome"], "error")

    def test_transient_read_after_confirmed_label_stops_remaining_batch(self):
        fake = Fake()

        def before(io):
            if io.writes:
                raise ConnectionResetError("private")

        fake.before = before
        result, calls, sleeps, events = self.run_watch(lambda _deadline: fake)
        self.assertEqual((result, calls, sleeps), (1, 1, []))
        self.assertEqual(fake.writes, [1])
        self.assertEqual(events[-1]["payload"]["outcome"], "error")


if __name__ == "__main__":
    unittest.main()
