#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Hermetic evidence for Supervisor enqueue guards and mutation ordering."""

import argparse
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import auto_enqueue as watch

REPO = "example/tool"
CONTROL = "example/controller"
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
        else f"{watch.READY}\n\nHead: {SHA}. Verified.",
    }


def response(pulls, own=(), **result):
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
                        "ineligible_reasons": []
                        if p["number"] in own
                        else [f"missing {TARGET['label']} label"],
                    }
                    for p in pulls
                ],
            },
            **result,
        },
    }


class Fake:
    def __init__(self, count=6):
        self.pulls = {number: pr(number) for number in range(1, count + 1)}
        self.writes = []
        self.qualify = True
        self.active = False
        self.calls = 0
        self.before = lambda _self: None
        self.targets = [copy.deepcopy(TARGET)]

    def policies(self, _owner):
        return copy.deepcopy(self.targets)

    def train(self, _target):
        self.calls += 1
        self.before(self)
        return response(
            self.pulls.values(),
            self.writes,
            **(
                {
                    "controller_action": "observe_candidate",
                    "merge_train_batch_candidate_record_id": "active",
                }
                if self.active
                else {}
            ),
        )

    def get(self, path):
        return copy.deepcopy(self.pulls[int(path.rsplit("/", 1)[-1])])

    def pages(self, path, **_params):
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
            control_plane_repo=CONTROL,
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

    def test_candidate_starts_between_labels_stops_remaining_group(self):
        fake = Fake()
        fake.before = lambda io: setattr(io, "active", bool(io.writes))
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
        self.holds["client_releases"] = [{"repository": REPO, "status": "ready"}]
        self.assertTrue(watch.held(self.holds, CONTROL, CONTROL))
        self.assertFalse(watch.held(self.holds, "example/independent", CONTROL))

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

    def test_truncated_file_inventory_refuses(self):
        fake = Fake()
        fake.pulls[1]["changed_files"] = 2
        with self.assertRaises(ValueError):
            self.run_pass(fake)
        self.assertEqual(fake.writes, [])

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
        self.assertTrue(watch.held(watch.read_holds(self.path), CONTROL, CONTROL))
        watch.edit_holds(
            ["--hold-file", str(self.path), "--release-finished", REPO, "--pause", REPO]
        )
        self.assertFalse(watch.held(watch.read_holds(self.path), CONTROL, CONTROL))
        self.assertTrue(watch.held(watch.read_holds(self.path), REPO, CONTROL))
        self.assertEqual(
            self.path.with_name(self.path.name + ".lock").stat().st_ino, inode
        )

    def test_latest_wait_stale_head_missing_head_and_untrusted_ready_refuse(self):
        self.assertTrue(watch.latest_ready(pr(), [comment()], {"engineer"}))
        for entries in (
            [comment(), comment(f"{watch.WAITING} the Client's acceptance", 2)],
            [comment(watch.READY)],
            [comment(f"{watch.READY}\nHead: {'b' * 40}")],
            [comment(login="stranger")],
            [comment(f"> {watch.READY}\nHead: {SHA}")],
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


if __name__ == "__main__":
    unittest.main()
