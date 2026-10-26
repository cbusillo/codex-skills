#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Prove shared allocation, lock serialization, and fresh GitHub seeding."""

import fcntl
import importlib.util
import json
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import question_id as counter

PLAN_SCRIPTS = Path(__file__).resolve().parents[2] / "github/scripts"
sys.path.insert(0, str(PLAN_SCRIPTS))
SPEC = importlib.util.spec_from_file_location("gh_plan_for_seed", PLAN_SCRIPTS / "gh-plan.py")
assert SPEC and SPEC.loader
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)


def allocate_many(path, barrier, results):
    barrier.wait(timeout=15)
    with patch.object(counter, "reserve_question", side_effect=lambda minimum, *_: minimum + 1):
        results.put([counter.update(Path(path)) for _ in range(12)])


def allocate_one(path, ready, results):
    ready.set()
    with patch.object(counter, "reserve_question", side_effect=lambda minimum, *_: minimum + 1):
        results.put(counter.update(Path(path)))


class QuestionCounterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "counter.json"
        self.seed = (317, ["https://github.com/owner/direction/issues/7"])
        self.local_reservation = patch.object(counter, "reserve_question", side_effect=lambda minimum, *_: minimum + 1)
        self.local_reservation.start()
        self.addCleanup(self.local_reservation.stop)

    def test_two_machines_reserve_unique_numbers_and_reuse_question_identity(self) -> None:
        self.local_reservation.stop()
        paths = [self.path, self.path.with_name("other-machine.json")]
        for path in paths:
            counter.update(path, seed=self.seed, allocate=False)
        barrier = threading.Barrier(2)
        records = []
        lock = threading.Lock()

        def read(item: str) -> dict:
            with lock:
                return {"expected_actor": "bot", "issue": {"body": "", "provenance": {"author": "owner"},
                        "comments": list(records), "url": self.seed[1][0]}}

        def post(item: str, body: str) -> None:
            with lock:
                records.append({"id": len(records) + 1, "body": body, "author": "bot"})
            barrier.wait(timeout=10)

        with patch.object(counter, "configured_queue", return_value="owner/direction#7"), \
                patch.object(counter, "read_discussion", side_effect=read), \
                patch.object(counter, "post_reservation", side_effect=post), \
                ThreadPoolExecutor(max_workers=2) as pool:
            numbers = list(pool.map(lambda path: counter.update(path, question_url=f"https://example.test/{path.name}"), paths))
            self.assertEqual(sorted(numbers), [self.seed[0] + 1, self.seed[0] + 2])
            self.assertEqual(counter.update(paths[1], question_url="https://example.test/counter.json"), numbers[0])
            self.assertEqual(len(records), 2)

    def test_seed_ignores_foreign_authors_and_incidental_question_references(self) -> None:
        record = {"expected_actor": "bot", "issue": {"body": "Director question: Q95. See another Director's Q455.",
                  "provenance": {"author": "owner"}, "url": self.seed[1][0],
                  "comments": [{"author": "bot", "body": "Owner question: Q101. Continue? Context: Q900."},
                               {"author": "foreign", "body": "Owner question: Q999. My question."},
                               {"author": "owner", "body": "We discussed Q1000.\n```text\nOwner question: Q2000. Example only.\n```\n> Owner question: Q3000. Quote."}]}}
        with patch.object(counter, "read_discussion", return_value=record):
            self.assertEqual(counter.github_seed(["owner/direction#7"]), (101, [self.seed[1][0]]))
        with self.assertRaises(ValueError):
            counter.github_seed(["owner/direction#7", "foreign/direction#1"])

    def test_two_harnesses_and_many_sessions_share_unique_sequence(self):
        with patch.dict(os.environ, {"CODE_HOME": self.temporary.name, "CODEX_HOME": "/codex-a"}):
            first_path = counter.counter_path()
        with patch.dict(os.environ, {"CODE_HOME": self.temporary.name, "CLAUDE_CONFIG_DIR": "/claude-b"}):
            self.assertEqual(counter.counter_path(), first_path)
        counter.update(self.path, seed=self.seed, allocate=False)
        context = multiprocessing.get_context("spawn")
        workers = 8
        barrier, results = context.Barrier(workers), context.Queue()
        processes = [context.Process(target=allocate_many, args=(str(self.path), barrier, results))
                     for _ in range(workers)]
        for process in processes:
            process.start()
        try:
            numbers = [number for _ in processes for number in results.get(timeout=30)]
            self.assertEqual(sorted(numbers), list(range(self.seed[0] + 1, self.seed[0] + len(numbers) + 1)))
            self.assertEqual(json.loads(self.path.read_text())["last_id"], max(numbers))
        finally:
            for process in processes:
                process.join(timeout=10)
                if process.is_alive():
                    process.kill()
                    process.join()
        self.assertTrue(all(process.exitcode == 0 for process in processes))

    def test_another_process_cannot_allocate_while_lock_is_held(self):
        counter.update(self.path, seed=self.seed, allocate=False)
        context = multiprocessing.get_context("spawn")
        ready, results = context.Event(), context.Queue()
        process = context.Process(target=allocate_one, args=(str(self.path), ready, results))
        with self.path.with_name(self.path.name + ".lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process.start()
            try:
                self.assertTrue(ready.wait(timeout=15))
                with self.assertRaises(queue.Empty):
                    results.get(timeout=0.5)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                process.join(timeout=15)
                if process.is_alive():
                    process.kill()
                    process.join()
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(results.get(timeout=5), self.seed[0] + 1)

    def test_reseed_never_reuses_allocated_number(self):
        allocated = counter.update(self.path, seed=self.seed)
        counter.update(self.path, seed=(self.seed[0] - 1, []), allocate=False)
        self.assertEqual(counter.update(self.path), allocated + 1)

    def test_confirmed_comment_identity_survives_retry_and_shares_chat_sequence(self):
        counter.update(self.path, seed=self.seed, allocate=False)
        url = "https://github.com/owner/repo/issues/1#issuecomment-2"
        first = counter.update(self.path, question_url=url)
        chat = counter.update(self.path)
        self.assertEqual(counter.update(self.path, question_url=url), first)
        self.assertEqual(chat, first + 1)
        self.assertEqual(json.loads(self.path.read_text())["last_id"], chat)

    def test_unknown_remote_allocation_reuses_pending_chat_request(self) -> None:
        counter.update(self.path, seed=self.seed, allocate=False)
        keys = []

        def reserve(minimum: int, key: str, sources: list[str]) -> int:
            keys.append(key)
            if len(keys) == 1:
                raise ValueError("unconfirmed write")
            return minimum + 1

        with patch.object(counter, "reserve_question", side_effect=reserve):
            with self.assertRaises(ValueError):
                counter.update(self.path)
            self.assertEqual(json.loads(self.path.read_text())["last_id"], self.seed[0])
            self.assertEqual(counter.update(self.path), self.seed[0] + 1)
        self.assertEqual(keys[0], keys[1])
        self.assertNotIn("pending_request", json.loads(self.path.read_text()))

    def test_question_heading_distinguishes_its_id_from_context(self):
        for heading in ("Owner question:", "**Director question:**", "\n **Owner question: ", "***Director question:***"):
            body = heading + " Continue? See Q72."
            self.assertIsNone(counter.heading_id(body))
            numbered = counter.number_heading(body, "Q73")
            self.assertEqual(counter.heading_id(numbered), "Q73")
            self.assertEqual(counter.number_heading(numbered, "Q74"), numbered)
        self.assertIsNone(counter.heading_id("A report mentioning Owner question: Q72"))
        for quarter in range(1, 5):
            for context in (" budget: approve the spend?", ": approve the budget?", "-2026 hiring plan?"):
                body = f"Owner question: Q{quarter}" + context
                self.assertIsNone(counter.heading_id(body))
                self.assertEqual(counter.heading_id(counter.number_heading(body, "Q73")), "Q73")
        for separator in (".", " —"):
            self.assertEqual(counter.heading_id("Owner question: Q3" + separator), "Q3")
        self.assertEqual(counter.heading_id("Owner question: Q73 Continue?"), "Q73")
        self.assertEqual(counter.number_heading("Owner question: Q73 Continue?", "Q74"), "Owner question: Q73 Continue?")

    def test_missing_and_corrupt_state_never_reset_the_counter(self):
        with self.assertRaises(ValueError):
            counter.update(self.path)
        for data in ("broken", '{"last_id": true}', '{"last_id": -1, "seed_sources": []}'):
            self.path.write_text(data)
            with self.assertRaises(ValueError):
                counter.update(self.path, seed=self.seed)
            self.assertEqual(self.path.read_text(), data)

    def test_seed_reads_all_current_question_headings(self):
        reserved = {"body": "Director question: Q239. Earlier?", "provenance": {"author": "owner"},
                    "comments": [{"author": "owner", "body": "Owner question: Q259. Later?"}], "url": self.seed[1][0]}
        catalog = "https://github.com/owner/catalog/issues/9"
        for later_comment, expected in (("Owner question: Q263. Continue?", 263), ("Nothing numbered yet", 259)):
            with self.subTest(expected=expected):
                records = [reserved, {"body": "No questions here", "comments": [{"author": "owner", "body": later_comment}], "url": catalog}]
                envelopes = [SimpleNamespace(stdout=json.dumps({"ok": True, "disposition": "complete",
                              "outcome_certainty": "confirmed", "issue": record})) for record in records]
                with patch.object(counter.subprocess, "run", side_effect=envelopes):
                    highest, sources = counter.github_seed(["owner/direction#7", "owner/catalog#9"])
                self.assertEqual(highest, expected)
                self.assertEqual(sources, [record["url"] for record in records])

    def test_seed_read_resolves_its_repository_outside_any_checkout(self):
        record = {"body": "Owner question: Q601. Continue?", "provenance": {"author": "owner"}, "comments": [], "url": "https://github.com/owner/launchplane/issues/3601"}
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()

        def show(command, **_kwargs):
            # Resolve the read the way gh-plan show does, where `gh repo view`
            # cannot name a repository either.
            args = PLAN.build_parser().parse_args(command[3:])
            with patch.object(PLAN, "run_raw", return_value=(None, "", "")):
                resolved = PLAN.issue_ref(args.issue, PLAN.default_repo(args.repo))
            self.assertEqual(resolved, ("owner/launchplane", 3601))
            return SimpleNamespace(stdout=json.dumps({"ok": True, "disposition": "complete",
                                                      "outcome_certainty": "confirmed", "issue": record}))

        previous = Path.cwd()
        os.chdir(outside)
        self.addCleanup(os.chdir, previous)
        with patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": self.temporary.name}), \
                patch.object(counter.subprocess, "run", side_effect=show):
            self.assertEqual(counter.github_seed(["owner/launchplane#3601"]), (601, [record["url"]]))

    def test_incomplete_or_empty_github_read_cannot_seed(self):
        for result in ({"ok": False}, {"ok": True, "disposition": "complete", "outcome_certainty": "confirmed",
            "issue": {"body": "No IDs", "comments": [], "url": self.seed[1][0]}}):
            with patch.object(counter.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(result))):
                with self.assertRaises(ValueError):
                    counter.github_seed(["owner/direction#7"])


if __name__ == "__main__":
    unittest.main()
