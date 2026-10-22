#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise missed-question delivery, retries and acknowledgements offline."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import question_delivery as delivery
from github_fixture_support import api_success, with_call_stub


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent / "fixtures/question-delivery-week-2026-10-15.json").read_text())
        # Keep the historical evidence unchanged; queue exercises use repaired
        # copies, while the refusal regression below uses the original source.
        self.questions = [{**q, "body": q["body"] if delivery.question_id(q["body"])
                           else q["body"].replace("question:", "question: Q1.", 1)}
                          for q in self.fixture["questions"]]
        self.queue = self.fixture["queue"]
        self.owner = self.fixture["owner"]
        self.authors = self.fixture["decision_authors"]
        self.comments = []
        self.sources = {delivery.source_item(q["url"]): [q] for q in self.questions}
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = patch.dict(os.environ, {"CODE_HOME": self.temporary.name})
        self.home.start()
        self.addCleanup(self.home.stop)

    def fetch(self, item):
        return list(self.comments if item == self.queue else self.sources[item])

    def post(self, queue, body):
        self.assertEqual(queue, self.queue)
        self.comments.append({"id": len(self.comments) + 1, "body": body,
                              "author": self.authors[0],
                              "url": f"https://github.com/{self.queue.replace('#', '/issues/')}#issuecomment-{len(self.comments) + 1}",
                              "created_at": "2026-10-15T17:35:24Z"})
        return self.comments[-1]

    def run_queue(self, **overrides):
        args = SimpleNamespace(queue=self.queue, owner=self.owner, decision_author=self.authors,
                               items=[q["url"] for q in self.questions], register=False, ack=None, delivery_url=None)
        vars(args).update(overrides)
        now = datetime.fromisoformat(self.fixture["observed_at"].replace("Z", "+00:00"))
        with patch.object(delivery, "post", side_effect=self.post), patch.object(delivery, "datetime") as clock:
            clock.now.return_value = now
            clock.fromisoformat.side_effect = datetime.fromisoformat
            return delivery.run_queue(args, self.fetch)

    def test_week_missed_questions_register_once_and_reappear_until_acknowledged(self):
        first = self.run_queue(register=True)
        replay = self.run_queue(register=True)
        self.assertEqual(len(self.comments), len(self.questions))
        self.assertEqual(first, replay)
        self.assertEqual({q["url"] for q in replay["questions"]}, {q["url"] for q in self.questions})
        self.assertTrue(all(q["resurface"] and q["registration_url"] for q in first["questions"]))
        # An empty worker list recovers all registrations from GitHub on takeover.
        takeover = self.run_queue(items=[])
        self.assertEqual(takeover["questions"], replay["questions"])
        self.assertEqual({q["question_id"] for q in first["questions"] if q["question_id"]},
                         {delivery.question_id(q["body"]) for q in self.questions})

    def test_unnumbered_source_preserves_readable_queue_but_refuses_its_registration_and_ack(self):
        original = next(q for q in self.fixture["questions"] if not delivery.question_id(q["body"]))
        # A reference to another question in the context is not this one's ID.
        self.sources[delivery.source_item(original["url"])] = [{**original, "body": original["body"] + " See Q327."}]
        report = self.run_queue()
        self.assertFalse(report["complete"])
        self.assertEqual(report["unnumbered_sources"], [original["url"]])
        bad = next(q for q in report["questions"] if q["url"] == original["url"])
        self.assertTrue(bad["needs_repair"])
        self.assertFalse(bad["deliverable"])
        self.assertIsNone(bad["question"])
        self.assertIsNone(bad["question_id"])
        good = next(q for q in report["questions"] if q["url"] != original["url"])
        self.assertTrue(good["deliverable"])
        self.assertIsNotNone(good["question"])
        for options in ({"register": True}, {"ack": original["url"],
                         "delivery_url": "https://discord.com/channels/1/2/3"}):
            self.comments.clear()
            if options.get("ack"):
                self.post(self.queue, delivery.marker_body("registered", original["url"]))
            before = list(self.comments)
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_queue(**options)
            self.assertEqual(self.comments, before)

        # A legacy bad registration must not prevent presenting a numbered peer.
        self.post(self.queue, delivery.marker_body("registered", good["url"]))
        accepted = self.run_queue(ack=good["url"], delivery_url="https://discord.com/channels/1/2/4")
        peer = next(q for q in accepted["questions"] if q["url"] == good["url"])
        self.assertEqual(peer["delivery_url"], "https://discord.com/channels/1/2/4")
        self.assertFalse(accepted["complete"])

    def test_original_timestamps_expose_four_hour_delay_and_utc_conversion(self):
        report = delivery.inventory(self.questions, [], self.owner, self.authors,
                                    datetime.fromisoformat(self.fixture["observed_at"].replace("Z", "+00:00")))
        rows = {q["url"]: q for q in report["questions"]}
        for question in self.questions:
            expected = datetime.fromisoformat(self.fixture["observed_at"].replace("Z", "+00:00")) - datetime.fromisoformat(question["created_at"].replace("Z", "+00:00"))
            self.assertEqual(rows[question["url"]]["age_seconds"], expected.total_seconds())
        self.assertGreater(rows[self.questions[0]["url"]]["age_seconds"], 4 * 3600)
        self.assertEqual(report["questions"][0]["url"], self.questions[1]["url"])

    def test_presentation_ack_is_separate_from_decision_and_later_stays_queued(self):
        self.run_queue(register=True)
        target = self.questions[0]["url"]
        receipt = "https://discord.com/channels/1/2/3"
        report = self.run_queue(ack=target, delivery_url=receipt)
        row = next(q for q in report["questions"] if q["url"] == target)
        self.assertEqual(row["delivery_url"], receipt)
        self.assertFalse(row["resurface"])
        self.assertEqual(report["queue_length"], len(self.questions))
        self.run_queue(ack=target, delivery_url=receipt)
        self.assertEqual(len(self.comments), len(self.questions) + 1)
        # "Later" or a receipt from the owner must not become a decision.
        self.comments[-1]["author"] = self.owner
        self.assertEqual(self.run_queue(items=[])["queue_length"], len(self.questions))
        for body, author in ((f"Q327 later: {target}", self.owner),
                             (f"Director decision: Q327 = later; {target}", self.authors[0])):
            self.sources[delivery.source_item(target)].append({"author": author, "body": body, "url": "later",
                                                               "created_at": self.fixture["observed_at"]})
            self.assertEqual(self.run_queue(items=[])["queue_length"], len(self.questions))
        self.sources[delivery.source_item(target)].append(self.fixture["decision"])
        self.assertEqual([q["url"] for q in self.run_queue(items=[])["questions"]], [self.questions[1]["url"]])

    def test_cross_thread_linked_decision_reconciles_but_foreign_or_unlinked_does_not(self):
        self.run_queue(register=True)
        decision = self.fixture["decision"]
        for answer in ({**decision, "author": "outsider"},
                       {**decision, "body": "Director decision: yes to something else"}):
            self.comments.append(answer)
            self.assertEqual(self.run_queue()["queue_length"], len(self.questions))
            self.comments.pop()
        self.comments.append(decision)
        self.assertEqual(self.run_queue()["queue_length"], len(self.questions) - 1)

    def test_no_ack_from_quoted_forged_or_incomplete_records(self):
        target = self.questions[0]["url"]
        self.run_queue(register=True)
        body = delivery.marker_body("delivered", target, "https://discord.com/channels/1/2/3")
        for forged in ("> " + body, "```\n" + body + "\n```", body):
            comment = {"author": "outsider" if forged == body else self.authors[0],
                       "body": forged, "url": "forged"}
            self.comments.append(comment)
        self.assertTrue(all(q["resurface"] for q in self.run_queue()["questions"]))
        self.comments.append({"author": self.authors[0], "url": "malformed",
                              "body": '<!-- director-question:delivery {"version": 1} -->'})
        with self.assertRaises(ValueError):
            self.run_queue()

    def test_failed_write_readback_and_retry_preserve_registration_identity(self):
        args = SimpleNamespace(queue=self.queue, owner=self.owner, decision_author=self.authors,
                               items=[q["url"] for q in self.questions], register=True, ack=None, delivery_url=None)
        def lost_reply(queue, body):
            self.post(queue, body)
            raise ValueError("unknown write outcome")
        with patch.object(delivery, "post", side_effect=lost_reply), self.assertRaises(ValueError):
            delivery.run_queue(args, self.fetch)
        report = self.run_queue(register=True)
        self.assertEqual(len(self.comments), len(self.questions))
        self.assertTrue(all(q["resurface"] for q in report["questions"]))
        with patch.object(delivery, "post", return_value={}), self.assertRaises(ValueError):
            delivery.run_queue(SimpleNamespace(**{**vars(args), "register": False,
                               "ack": self.questions[0]["url"], "delivery_url": "https://discord.com/channels/1/2/3"}), self.fetch)
        self.assertTrue(all(q["resurface"] for q in self.run_queue()["questions"]))

    def test_missing_source_or_failed_paged_read_cannot_report_empty_queue(self):
        self.run_queue(register=True)
        self.sources[delivery.source_item(self.questions[0]["url"])] = []
        partial = self.run_queue(items=[])
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["missing_sources"], [self.questions[0]["url"]])
        self.assertEqual({q["url"] for q in partial["questions"]}, {q["url"] for q in self.questions})
        with patch.object(delivery, "records", side_effect=ValueError("page unavailable")), self.assertRaises(ValueError):
            self.run_queue()

    def test_two_workers_serialize_registration_using_shared_counter_home(self):
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(delivery, "post", side_effect=self.post):
            args = SimpleNamespace(queue=self.queue, owner=self.owner, decision_author=self.authors,
                                   items=[q["url"] for q in self.questions], register=True, ack=None, delivery_url=None)
            results = list(pool.map(lambda _: delivery.run_queue(args, self.fetch), range(2)))
        self.assertEqual(len(self.comments), len(self.questions))
        self.assertEqual([q["url"] for q in results[0]["questions"]],
                         [q["url"] for q in results[1]["questions"]])

    def test_receipt_requires_registered_question_and_never_accepts_source_as_delivery(self):
        with self.assertRaises(ValueError):
            self.run_queue(ack="https://github.com/cbusillo/codex-skills/issues/1#issuecomment-999999",
                           delivery_url="https://discord.com/channels/1/2/3")
        with self.assertRaises(ValueError):
            self.run_queue(ack=self.questions[0]["url"], delivery_url="https://discord.com/channels/1/2/3")
        report = self.run_queue(register=True)
        row = report["questions"][0]
        for receipt in (row["url"], row["registration_url"], row["url"].split("#")[0],
                        "https://github.com/cbusillo/codex-skills/issues/1043", "https://private.test/secret", "not a URL"):
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                self.run_queue(ack=row["url"], delivery_url=receipt)

    def test_writes_use_automation_helper_dedupe_and_preserve_unknown_outcome(self):
        repo, number = self.queue.split("#")
        saved = []
        def api(method, path, body, **_kwargs):
            if method == "POST":
                saved.append({"id": 1, "html_url": f"https://github.com/{repo}/issues/{number}#issuecomment-1",
                              "user": {"login": self.authors[0]}, "body": body["body"]})
                return api_success(saved[-1], operation="github.comment.issue")
            return api_success(saved, operation="github.comment.issue")
        results = []
        def exercise(calls):
            with patch.object(delivery.github_comment, "authenticated_actor", return_value=self.authors[0]):
                results.append(delivery.post(self.queue, "reference"))
                results.append(delivery.post(self.queue.upper(), "reference"))
            self.assertEqual(sum(c["method"] == "POST" for c in calls), 1)
        with_call_stub(api, exercise)
        result = results[0]
        with patch.object(delivery.github_comment, "comment", return_value={**result, "outcome_certainty": "reconciled_applied"}):
            self.assertEqual(delivery.post(self.queue, "reference")["url"], result["url"])
        with patch.object(delivery.github_comment, "comment", return_value={**result, "outcome_certainty": "unknown"}), self.assertRaises(ValueError):
            delivery.post(self.queue, "reference")

    def test_register_selects_one_exact_question_not_the_whole_discussion(self):
        target = self.questions[0]
        old = {**target, "id": 2, "url": target["url"].split("#")[0] + "#issuecomment-2",
               "body": "Owner question: older request?"}
        self.sources[delivery.source_item(target["url"])].insert(0, old)
        report = self.run_queue(items=[target["url"]], register=True)
        self.assertEqual([q["url"] for q in report["questions"]], [target["url"]])
        self.assertEqual([q["url"] for q in self.run_queue(items=[])["questions"]], [target["url"]])
        self.assertEqual([r["question_url"] for r in delivery.records(self.comments, {self.authors[0]})], [target["url"]])
        with self.assertRaises(ValueError):
            self.run_queue(items=[delivery.source_item(target["url"])], register=True)

    def test_unknown_question_author_stays_visible_and_case_does_not_change_authority(self):
        self.questions[0]["author"] = "new-bot"
        report = self.run_queue()
        self.assertFalse(report["complete"])
        self.assertEqual(report["untrusted_sources"], [self.questions[0]["url"]])
        self.assertEqual(report["queue_length"], len(self.questions))
        self.questions[0]["author"] = self.authors[0].upper()
        self.sources[delivery.source_item(self.questions[0]["url"])].append(self.fixture["decision"])
        self.assertEqual(self.run_queue(owner=self.owner.upper(), decision_author=[a.upper() for a in self.authors])["queue_length"], len(self.questions) - 1)

    def test_untrusted_queue_records_warn_without_poisoning_trusted_delivery(self):
        self.run_queue(register=True)
        self.comments.append({**self.comments[0], "author": None, "url": "unknown"})
        report = self.run_queue(items=[])
        self.assertTrue(report["complete"])
        self.assertEqual(report["untrusted_records"], [{"url": "unknown", "author": None}])
        self.assertEqual(report["queue_length"], len(self.questions))

    def test_unavailable_source_retains_other_rows_and_recorded_disposition_clears_it(self):
        self.run_queue(register=True)
        target = self.questions[0]["url"]
        del self.sources[delivery.source_item(target)]
        report = self.run_queue(items=[])
        self.assertFalse(report["complete"])
        self.assertEqual(report["missing_sources"], [target])
        self.assertEqual(len(report["source_errors"]), 1)
        self.assertEqual({q["url"] for q in report["questions"]}, {q["url"] for q in self.questions})
        self.comments.append({"author": self.owner, "body": f"Director decision: withdraw this obsolete question: {target}", "url": "disposition"})
        report = self.run_queue(items=[])
        self.assertTrue(report["complete"])
        self.assertEqual([q["url"] for q in report["questions"]], [self.questions[1]["url"]])

    def test_explicit_question_read_ignores_an_unhealthy_registered_peer(self):
        self.run_queue(register=True)
        target, peer = self.questions[0], self.questions[1]
        unnumbered = [{**peer, "body": next(q for q in self.fixture["questions"] if q["url"] == peer["url"])["body"]}]
        for case, peer_source in (("unavailable", None), ("unnumbered", unnumbered)):
            with self.subTest(peer=case):
                self.sources.pop(delivery.source_item(peer["url"]), None)
                if peer_source:
                    self.sources[delivery.source_item(peer["url"])] = peer_source
                scoped = self.run_queue(items=[target["url"]])
                self.assertEqual([q["url"] for q in scoped["questions"]], [target["url"]])
                self.assertTrue(scoped["complete"])
                self.assertEqual(scoped["source_errors"], [])
                whole = self.run_queue(items=[])
                self.assertFalse(whole["complete"])
                self.assertIn(peer["url"], {q["url"] for q in whole["questions"]})

    def test_real_decision_words_are_not_deferrals_and_plain_replies_are_candidates(self):
        self.run_queue(register=True)
        target = self.questions[0]["url"]
        source = self.sources[delivery.source_item(target)]
        source.append({"author": self.owner, "body": f"Yes to {target}", "url": "candidate",
                       "created_at": self.fixture["observed_at"]})
        row = next(q for q in self.run_queue(items=[])["questions"] if q["url"] == target)
        self.assertEqual(row["candidate_answers"], ["candidate"])
        for answer in ("defer to your recommendation", "hold the rebuild"):
            source.append({"author": self.authors[0], "body": f"Director decision: {answer}; {target}",
                           "url": "decision", "created_at": self.fixture["observed_at"]})
            self.assertEqual(self.run_queue(items=[])["queue_length"], len(self.questions) - 1)
            source.pop()


if __name__ == "__main__":
    unittest.main()
