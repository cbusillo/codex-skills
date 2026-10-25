#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/codex-skills-test.env"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import director_question_gate as gate
import github_identity
import github_plan_wait
import question_delivery

REPO = "director/project"
QUEUE = "director/catalog#9"
BOT = "helper-bot"
ISSUE = f"https://github.com/{REPO}/issues/7"


def comment(number: int, author: str, body: str) -> dict[str, Any]:
    return {"id": number, "user": {"login": author}, "body": body, "html_url": f"{ISSUE}#issuecomment-{number}"}


COMMENTS = {
    11: comment(11, BOT, "Director question: Q812. Approve the install on the hub? I recommend yes."),
    12: comment(12, BOT, "Director question: Approve the install on the hub?"),
    13: comment(13, "director", "Try 1.0.76 for a few days before it goes to the App Store."),
    14: comment(14, BOT, 'Director decision: Q700, October 24. Director: "try it through the weekend"'),
    15: comment(15, "someone", "Director decision: ship it"),
    16: comment(16, BOT, "Director question: Q813. Retire the token?"),
}
QUEUE_COMMENTS = [{"user": {"login": BOT}, "body": question_delivery.marker_body("registered", COMMENTS[number]["html_url"]),
                   "html_url": f"https://github.com/{QUEUE.replace('#', '/issues/')}#issuecomment-{900 + number}"}
                  for number in (11, 16)]


def wait(waiting_for: str, evidence: str = "Recorded in the session") -> dict[str, Any]:
    return github_plan_wait.reviewed(evidence=evidence, waiting_for=waiting_for, ends_when="It happens")


def link(number: int) -> str:
    return COMMENTS[number]["html_url"]


class DirectorQuestionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue: str | None = QUEUE
        self.queue_reads = 0
        for patcher in (patch.object(gate, "director_terms", side_effect=lambda owner: [owner, "Chris", "Director"]),
                        patch.object(gate, "configured_queue", side_effect=lambda: self.queue),
                        patch.object(github_identity, "configured_bot_logins", return_value=[BOT]),
                        patch.object(github_identity, "automation_login", return_value=BOT)):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def read_json(path: str) -> dict[str, Any]:
        return COMMENTS[int(path.rsplit("/", 1)[1])]

    def read_pages(self, path: str) -> list[dict[str, Any]]:
        self.assertEqual(path, "/repos/director/catalog/issues/9/comments")
        self.queue_reads += 1
        return QUEUE_COMMENTS

    def gap(self, record: dict[str, Any]) -> str | None:
        return gate.director_question_gap(REPO, record, read_json=self.read_json, read_pages=self.read_pages)

    def refusal(self, record: dict[str, Any]) -> str:
        message = self.gap(record)
        self.assertIsNotNone(message)
        return message or ""

    def test_unasked_director_decision_refuses_naming_the_registration_command(self) -> None:
        message = self.refusal(wait("Chris approves the live install"))
        self.assertIn('"Chris approves the live install"', message)
        self.assertIn(f"oq.py QUESTION_URL --owner director --decision-author {BOT} --queue {QUEUE} --register", message)

    def test_registered_numbered_question_allows_the_wait(self) -> None:
        self.assertIsNone(self.gap(wait("Chris approves the live install", f"Asked: {link(11)}")))
        self.assertIsNone(self.gap(wait("Chris approves the live install", f"{link(12)} and {link(11)}")))
        self.assertEqual(self.queue_reads, 2)

    def test_unregistered_unnumbered_or_untrusted_links_still_refuse(self) -> None:
        COMMENTS[17] = comment(17, BOT, "Director question: Q814. Approve the install?")
        self.addCleanup(COMMENTS.pop, 17)
        for evidence in (link(12), link(15), link(17)):
            with self.subTest(evidence=evidence):
                self.assertIsNotNone(self.gap(wait("Chris approves the live install", evidence)))

    def test_hold_the_director_set_or_agreed_is_exempt(self) -> None:
        self.assertIsNone(self.gap(wait("Chris to dogfood 1.0.76 from TestFlight", link(13))))
        self.assertIsNone(self.gap(wait("Chris to dogfood 1.0.76 from TestFlight", link(14))))
        self.assertIsNone(self.gap(wait("Chris to resume after the weekend")))
        self.assertIsNotNone(self.gap(wait("Chris to dogfood 1.0.76 from TestFlight")))

    def test_timers_and_other_peoples_steps_are_not_on_the_director(self) -> None:
        for waiting_for in ("Two weeks with no use of the token, then Chris retires it",
                            "Mike tests the build", "No Director decision needed; CI finishes"):
            with self.subTest(waiting_for=waiting_for):
                self.assertIsNone(self.gap(wait(waiting_for)))
        self.assertEqual(self.queue_reads, 0)

    def test_without_a_configured_queue_a_numbered_question_suffices(self) -> None:
        self.queue = None
        self.assertIsNone(self.gap(wait("Chris approves the live install", link(16))))
        self.assertIsNotNone(self.gap(wait("Chris approves the live install", link(12))))
        self.assertNotIn("--register", self.refusal(wait("Chris approves the live install")))


if __name__ == "__main__":
    unittest.main()
