#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Prove shared allocation, lock serialization, and fresh GitHub seeding."""

import fcntl
import base64
import importlib.util
import json
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
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
    with patch.object(counter, "configured_queue", return_value=None):
        results.put([counter.update(Path(path)) for _ in range(12)])


def allocate_one(path, ready, results):
    ready.set()
    with patch.object(counter, "configured_queue", return_value=None):
        results.put(counter.update(Path(path)))


class QuestionCounterTests(unittest.TestCase):
    def test_outstanding_reservations_bind_only_to_their_confirmed_source(self) -> None:
        counter.update(self.path, seed=self.seed, allocate=False)
        first, second = counter.update(self.path), counter.update(self.path)
        self.assertNotEqual(first, second)
        for number in (first, second):
            counter.verify_number(self.path, number)
        with self.assertRaises(ValueError):
            counter.verify_number(self.path, second + 1)
        source = "https://github.com/owner/repo/issues/1#issuecomment-2"
        self.assertEqual(counter.update(self.path, question_url=source, question_number=first), first)
        counter.verify_number(self.path, first, source)
        self.assertEqual(counter.update(self.path, question_url=source, question_number=first), first)
        with self.assertRaises(ValueError):
            counter.verify_number(self.path, first)
        with self.assertRaises(ValueError):
            counter.update(self.path, question_url=source + "3", question_number=first)
        counter.verify_number(self.path, second)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "counter.json"
        self.seed = (317, ["https://github.com/owner/direction/issues/7"])
        self.local_reservation = patch.object(counter, "configured_queue", return_value=None)
        self.local_reservation.start()
        self.addCleanup(self.local_reservation.stop)

    def test_two_machines_reserve_unique_numbers_and_reuse_question_identity(self) -> None:
        paths = [self.path, self.path.with_name("other-machine.json")]
        for path in paths:
            counter.update(path, seed=self.seed, allocate=False)
        barrier = threading.Barrier(2)
        state = {"version": 1, "director": "owner", "last_id": self.seed[0], "questions": {}}
        revision = 0
        reads = 0
        conflicts = 0
        synchronize = False
        lock = threading.Lock()

        def read(repo: str, minimum: int, **kwargs: object) -> counter.CounterSnapshot:
            nonlocal reads
            with lock:
                snapshot = counter.CounterSnapshot(str(revision), json.loads(json.dumps(state)), f"commit-{revision}")
                reads += 1
                wait = synchronize and reads <= 2
            if wait:
                barrier.wait(timeout=10)
            return snapshot

        def write(repo: str, sha: str, updated: dict) -> str | None:
            nonlocal revision, conflicts
            with lock:
                if sha != str(revision):
                    conflicts += 1
                    return None
                state.update(updated)
                revision += 1
                return f"commit-{revision}"

        with patch.object(counter, "configured_queue", return_value="owner/direction#7"), \
                patch.object(counter, "read_counter", side_effect=read), \
                patch.object(counter, "write_counter", side_effect=write), \
                patch.object(counter, "read_discussion", side_effect=AssertionError("legacy seeds are not reread")), \
                ThreadPoolExecutor(max_workers=2) as pool:
            for path in paths:
                counter.update(path, seed=self.seed, allocate=False)
            reads, synchronize = 0, True
            numbers = list(pool.map(lambda path: counter.update(path, question_url=f"https://example.test/{path.name}"), paths))
            self.assertEqual(sorted(numbers), [self.seed[0] + 1, self.seed[0] + 2])
            self.assertGreater(conflicts, 0)
            self.assertEqual(counter.update(paths[1], question_url="https://example.test/counter.json"), numbers[0])
            self.assertEqual(len(state["questions"]), 2)
            self.assertTrue(all("example.test" not in key for key in state["questions"]))

            def write_then_lose_response(repo: str, sha: str, updated: dict) -> str | None:
                self.assertTrue(write(repo, sha, updated))
                raise OSError("response lost after commit")

            with patch.object(counter, "write_counter", side_effect=write_then_lose_response):
                with self.assertRaises(ValueError):
                    counter.update(paths[0])
            self.assertEqual(counter.update(paths[0]), max(numbers) + 1)
            self.assertEqual(len(state["questions"]), 3)
            high = state["last_id"] + 10
            counter.update(paths[0], seed=(high, ["legacy source no longer readable"]), allocate=False)
            self.assertEqual(counter.update(paths[1]), high + 1)
            reserved = [counter.update(path) for path in paths]
            for number in reserved:
                counter.verify_number(paths[0], number)
            source = "https://github.com/owner/repo/issues/3#issuecomment-4"
            self.assertEqual(counter.update(paths[0], question_url=source, question_number=reserved[0]), reserved[0])
            counter.verify_number(paths[1], reserved[0], source)
            with self.assertRaises(ValueError):
                counter.update(paths[1], question_url=source + "5", question_number=reserved[0])
            with self.assertRaises(ValueError):
                counter.verify_number(paths[0], reserved[0])
            counter.verify_number(paths[1], reserved[1])

    def test_seed_ignores_foreign_authors_and_incidental_question_references(self) -> None:
        record = {"expected_actor": "bot", "issue": {"body": "Director question: Q95. See another Director's Q455.",
                  "provenance": {"author": "owner"}, "url": self.seed[1][0],
                  "comments": [{"author": "bot", "body": "Owner question: Q101. Continue? Context: Q900."},
                               {"author": "foreign", "body": "Owner question: Q999. My question."},
                               {"author": "owner", "body": "We discussed Q1000.\n```text\nOwner question: Q2000. Example only.\n```\n> Owner question: Q3000. Quote."}]}}
        with patch.object(counter, "read_discussion", return_value=record):
            self.assertEqual(counter.github_seed(["owner/direction#7"]), (101, [self.seed[1][0]]))
        with patch.object(counter, "configured_queue", return_value="owner/direction#7"), \
                patch.object(counter, "read_discussion", return_value=record):
            self.assertEqual(counter.github_seed(["organization/product#1"])[0], 101)

    def test_shared_allocation_requires_seed_and_never_recreates_missing_state(self) -> None:
        counter.update(self.path, seed=self.seed, allocate=False)
        with patch.object(counter, "configured_queue", return_value="owner/direction#7"), \
                patch.object(counter, "counter_request", return_value=SimpleNamespace(ok=False, status=404)) as request:
            self.assertEqual(counter.update(self.path), self.seed[0] + 1)
            request.assert_not_called()
            state = json.loads(self.path.read_text()) | {"counter_repository": "owner/direction", "counter_commit": "verified"}
            self.path.write_text(json.dumps(state))
            for allocate in (True, False):
                with self.subTest(allocate=allocate), self.assertRaises(ValueError):
                    counter.update(self.path, seed=self.seed, allocate=allocate, confirm_prior_counters_complete=True)
            self.assertTrue(all(call.args[0] == "GET" for call in request.call_args_list))

    def test_unconfirmed_seed_keeps_shared_intent_and_imported_assignments(self) -> None:
        counter.update(self.path, seed=self.seed, allocate=False)
        other = {"last_id": self.seed[0] + 20, "questions": {"https://example.test/prior": self.seed[0] + 20}}
        with patch.object(counter, "configured_queue", return_value="owner/direction#7"), \
                patch.object(counter, "reserve_question", side_effect=ValueError("outcome unknown")) as reserve:
            with self.assertRaises(ValueError):
                counter.update(self.path, seed=self.seed, allocate=False, include_counters=[other],
                               confirm_prior_counters_complete=True)
            state = json.loads(self.path.read_text())
            self.assertEqual(state["questions"], other["questions"])
            self.assertEqual(state["counter_repository"], "owner/direction")
            with self.assertRaises(ValueError):
                counter.update(self.path)
            self.assertEqual(reserve.call_count, 1)
            state["counter_commit"] = "previously-verified"
            state.pop("counter_seed_pending", None)
            self.path.write_text(json.dumps(state))
            with self.assertRaises(ValueError):
                counter.update(self.path, seed=self.seed, allocate=False)
            with self.assertRaises(ValueError):
                counter.update(self.path)
            self.assertEqual(reserve.call_count, 2)

    def test_bootstrap_imports_every_prior_counter_without_changing_old_assignments(self) -> None:
        current = {"last_id": self.seed[0], "questions": {"https://example.test/a": self.seed[0]}}
        other = {"last_id": self.seed[0] + 20, "questions": {"https://example.test/b": self.seed[0] + 20}}
        assignments = counter.prior_assignments([current, other])
        self.assertEqual(sorted(assignments.values()), [current["last_id"], other["last_id"]])
        with self.assertRaises(ValueError):
            counter.prior_assignments([current, {**other, "questions": {"https://example.test/a": other["last_id"]}}])
        shared = {"version": 1, "director": "owner", "last_id": 0, "questions": {}}
        snapshot = counter.CounterSnapshot("file", shared, "commit")
        with patch.object(counter, "configured_queue", return_value="owner/direction#7"), \
                patch.object(counter, "read_counter", return_value=snapshot) as read, \
                patch.object(counter, "write_counter", return_value="confirmed"):
            counter.update(self.path, seed=self.seed, allocate=False, include_counters=[other],
                           confirm_prior_counters_complete=True)
        self.assertTrue(read.call_args.kwargs["create"])
        self.assertEqual(shared["last_id"], other["last_id"])
        self.assertEqual(shared["questions"], counter.prior_assignments([other]))

    def test_restore_uses_verified_history_and_never_replaces_an_existing_branch(self) -> None:
        state = {"version": 1, "director": "owner", "last_id": self.seed[0], "questions": {}}
        payload = {"sha": "file", "content": base64.b64encode(json.dumps(state).encode()).decode()}
        responses = [SimpleNamespace(ok=False), SimpleNamespace(ok=True, body=payload), SimpleNamespace(ok=True),
                     SimpleNamespace(ok=True, body={"object": {"sha": "history"}}), SimpleNamespace(ok=True, body=payload)]
        with patch.object(counter, "counter_request", side_effect=responses) as request:
            snapshot = counter.read_counter("owner/catalog", self.seed[0], restore_from="history")
        self.assertEqual(snapshot.state, state)
        writes = [call for call in request.call_args_list if call.args[0] != "GET"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].args[2]["sha"], snapshot.commit_sha)
        with patch.object(counter, "counter_request", return_value=SimpleNamespace(ok=True)) as request:
            with self.assertRaises(ValueError):
                counter.read_counter("owner/catalog", self.seed[0], restore_from="history")
        self.assertEqual(request.call_count, 1)
        with patch.object(counter, "counter_request", return_value=SimpleNamespace(ok=True, body=payload)):
            self.assertEqual(counter.decoded_counter("owner/catalog", {"encoding": "none", "sha": "file"}), state)
        with patch.object(counter, "counter_request", return_value=SimpleNamespace(ok=False,
                body={"message": "Expected status checks did not pass"})):
            with self.assertRaises(ValueError):
                counter.write_counter("owner/catalog", "file", state)

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

        def reserve(minimum: int, key: str, _repo: str | None, **_kwargs: object) -> counter.Allocation:
            keys.append(key)
            if len(keys) == 1:
                raise ValueError("unconfirmed write")
            return counter.Allocation(minimum + 1)

        with patch.object(counter, "reserve_question", side_effect=reserve):
            with self.assertRaises(ValueError):
                counter.update(self.path)
            self.assertEqual(json.loads(self.path.read_text())["last_id"], self.seed[0])
            self.assertEqual(counter.update(self.path), self.seed[0] + 1)
        self.assertEqual(keys[0], keys[1])
        self.assertNotIn("pending_request", json.loads(self.path.read_text()))
        with patch.object(counter, "configured_queue", return_value="owner/direction#7"), \
                patch.object(counter, "read_counter", side_effect=subprocess.CalledProcessError(1, "read")):
            with self.assertRaises(ValueError):
                counter.reserve_question(self.seed[0], "request", "owner/direction")

    def test_shared_counter_writes_use_file_sha_and_initialize_without_source(self) -> None:
        calls = []

        def request(method: str, path: str, body: dict | None = None, allowed: tuple[int, ...] = ()) -> SimpleNamespace:
            calls.append((method, path, body))
            if method == "GET":
                return SimpleNamespace(ok=False, status=404)
            return SimpleNamespace(ok=True, body={"sha": "server-object", "commit": {"sha": "confirmed-commit"}})

        with patch.object(counter, "counter_request", side_effect=request):
            counter.initialize_counter("owner/catalog", self.seed[0])
            tree = next(body["tree"] for method, path, body in calls if path.endswith("/git/trees"))
            self.assertEqual([entry["path"] for entry in tree], [counter.COUNTER_FILE])
            state = json.loads(tree[0]["content"])
            self.assertEqual(state["last_id"], self.seed[0])
            counter.write_counter("owner/catalog", "observed-file-sha", state)
        update = calls[-1][2]
        self.assertEqual(update["sha"], "observed-file-sha")
        self.assertEqual(update["branch"], counter.COUNTER_BRANCH)
        with patch.object(counter, "counter_request", return_value=SimpleNamespace(ok=False, status=409, body={"message": "sha does not match"})):
            self.assertFalse(counter.write_counter("owner/catalog", "stale", state))

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
                    "comments": [{"author": "owner", "body": "Reserved Q240–Q259. Context from someone else: Q999."}], "url": self.seed[1][0]}
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
