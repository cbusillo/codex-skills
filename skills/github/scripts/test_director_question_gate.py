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
BOOK = {"justin": frozenset({"justin-login"}), "justin-login": frozenset({"justin-login"}),
        "mike": frozenset({"mike-login"}), "mike-login": frozenset({"mike-login"})}


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


def wait(waiting_for: str, ask_url: str | None = None, **entry: Any) -> dict[str, Any]:
    record = github_plan_wait.reviewed(evidence="Recorded in the session", waiting_for=waiting_for, ends_when="It happens")
    record["waits"][0].update(entry, **({"ask_url": ask_url} if ask_url else {}))
    return record


def link(number: int) -> str:
    return COMMENTS[number]["html_url"]


class DirectorQuestionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue: str | None = QUEUE
        self.queue_reads = 0
        for patcher in (patch.object(gate, "director_terms", side_effect=lambda owner: [owner, "Chris", "Director"]),
                        patch.object(gate, "address_book", side_effect=lambda owner: BOOK),
                        patch("github_client.recorded_client", return_value={"status": "none"}),
                        patch.object(gate, "configured_queue", side_effect=lambda: self.queue),
                        patch.object(github_identity, "configured_bot_logins", return_value=[BOT]),
                        patch.object(github_identity, "automation_login", return_value=BOT)):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def read_comment(path: str) -> Any:
        return COMMENTS.get(int(path.rsplit("/", 1)[1]), "<html>gateway error</html>" if path.endswith("/500") else None)

    def read_pages(self, path: str) -> list[dict[str, Any]]:
        self.assertEqual(path, "/repos/director/catalog/issues/9/comments")
        self.queue_reads += 1
        return QUEUE_COMMENTS

    def gap(self, record: dict[str, Any]) -> str | None:
        return gate.ask_gap(REPO, record, read_comment=self.read_comment, read_pages=self.read_pages)

    def refusal(self, record: dict[str, Any]) -> str:
        message = self.gap(record)
        self.assertIsNotNone(message)
        return message or ""

    def test_unasked_director_decision_refuses(self) -> None:
        message = self.refusal(wait("Chris approves the live install"))
        self.assertIn('"Chris approves the live install"', message)

    def test_registered_numbered_question_allows_the_wait(self) -> None:
        self.assertIsNone(self.gap(wait("Chris approves the live install", link(11))))
        self.assertEqual(self.queue_reads, 1)

    def test_a_question_link_only_in_evidence_no_longer_counts(self) -> None:
        record = github_plan_wait.reviewed(evidence=f"Asked: {link(11)}", waiting_for="Chris approves the live install", ends_when="It happens")
        self.assertIsNotNone(self.gap(record))

    def test_unregistered_unnumbered_or_untrusted_links_still_refuse(self) -> None:
        self.enterContext(patch.dict(COMMENTS, {17: comment(17, BOT, "Director question: Q814. Approve the install?")}))
        for ask_url in (link(12), link(15), link(17)):
            with self.subTest(ask_url=ask_url):
                self.assertIsNotNone(self.gap(wait("Chris approves the live install", ask_url)))

    def test_asker_withdrawal_does_not_authorize_a_director_hold(self) -> None:
        with patch.dict(COMMENTS, {17: comment(17, BOT, "Q812 withdrawn by the Supervisor. Premise corrected.")}):
            self.assertIsNotNone(self.gap(wait("Chris to resume after the weekend", link(17))))

    def test_hold_the_director_set_or_agreed_is_exempt(self) -> None:
        self.assertIsNone(self.gap(wait("Chris to dogfood 1.0.76 from TestFlight", link(13))))
        self.assertIsNone(self.gap(wait("Chris to dogfood 1.0.76 from TestFlight", link(14))))
        for body, recorded in (("Director decision Q700: try it through the weekend", True),
                               ("Director decision Q700 pending; continuing independent work.", False),
                               ("Owner decision needed: Q700", False)):
            with self.subTest(body=body), patch.dict(COMMENTS, {14: comment(14, BOT, body)}):
                self.assertEqual(self.gap(wait("Chris to dogfood 1.0.76 from TestFlight", link(14))) is None, recorded)
        for waiting_for in ("Chris to dogfood 1.0.76 from TestFlight", "Chris to resume after the weekend",
                            "Chris selects the deployment architecture"):
            with self.subTest(waiting_for=waiting_for):
                self.assertIsNotNone(self.gap(wait(waiting_for)))

    def test_unreadable_ask_is_named_and_refused(self) -> None:
        for missing in (f"{ISSUE}#issuecomment-404", f"{ISSUE}#issuecomment-500"):
            with self.subTest(missing=missing):
                self.assertIn(missing, self.refusal(wait("Chris approves the live install", missing)))

    def test_events_and_later_steps_need_no_ask(self) -> None:
        for waiting_for in ("Two weeks with no use of the token, then Chris retires it",
                            "A free Anthropic fleet worker slot", "No Director decision needed; CI finishes",
                            "The client cache rebuild finishes"):
            with self.subTest(waiting_for=waiting_for):
                self.assertIsNone(self.gap(wait(waiting_for)))
        self.assertEqual(self.queue_reads, 0)

    def test_unasked_people_from_the_november_reports_refuse(self) -> None:
        unasked = comment(30, BOT, "Justin, five questions for the demo; no rush on these.")
        for waiting_for in ("Justin creates the docker-tk-prod guest and backup SSH access",
                            "Justin confirms switch-back triggers",
                            "Client gives QBO country and blank bill template",
                            "Mike acceptance of the qualified cbusillo/odoo-tenant-opw#323 candidate",
                            "Mike accepts the motor-price correction release"):
            with self.subTest(waiting_for=waiting_for):
                self.assertIn(waiting_for, self.refusal(wait(waiting_for)))
        with patch.dict(COMMENTS, {30: unasked}):
            for waiting_for in ("Justin confirms switch-back triggers", "Client gives QBO country and blank bill template"):
                with self.subTest(waiting_for=waiting_for):
                    self.assertIn(link(30), self.refusal(wait(waiting_for, link(30))))

    def test_real_asks_and_own_words_allow_the_person_wait(self) -> None:
        with patch.dict(COMMENTS, {31: comment(31, BOT, "Please confirm the guest, @justin-login."),
                                   33: comment(33, BOT, "Review is ready for @client-login: accept it here.")}):
            for waiting_for, ask in (("Justin creates the docker-tk-prod guest", link(31)),
                                     ("Justin creates the docker-tk-prod guest", link(14)),
                                     ("Client accepts the deletion candidate", link(33))):
                with self.subTest(ask=ask):
                    self.assertIsNone(self.gap(wait(waiting_for, ask)))

    def test_the_named_persons_own_comment_is_not_an_ask(self) -> None:
        with patch.dict(COMMENTS, {32: comment(32, "justin-login", "I will build the guest on Friday.")}):
            self.assertIsNotNone(self.gap(wait("Justin creates the guest", link(32))))

    def test_every_named_person_must_be_mentioned(self) -> None:
        with patch.dict(COMMENTS, {36: comment(36, BOT, "Please confirm, @justin-login.")}):
            self.assertIsNotNone(self.gap(wait("Justin and Mike confirm the guest", link(36))))

    def test_a_recorded_client_must_be_the_one_mentioned(self) -> None:
        with patch.dict(COMMENTS, {37: comment(37, BOT, "Please confirm, @someone-else.")}), \
                patch("github_client.recorded_client", return_value={"status": "recorded", "login": "real-client"}):
            self.assertIsNotNone(self.gap(wait("Client accepts the deletion candidate", link(37))))

    def test_an_ask_mentioning_someone_else_does_not_count(self) -> None:
        with patch.dict(COMMENTS, {34: comment(34, BOT, "Asking @someone-else about the guest.")}):
            self.assertIsNotNone(self.gap(wait("Justin creates the guest", link(34))))

    def test_quoted_mention_is_not_an_ask(self) -> None:
        with patch.dict(COMMENTS, {35: comment(35, BOT, "Noted: `@justin-login` was not asked yet.")}):
            self.assertIsNotNone(self.gap(wait("Justin creates the guest", link(35))))

    def test_a_date_does_not_turn_a_named_persons_acceptance_into_an_event(self) -> None:
        event = {"version": 1, "kind": "date", "at": "2026-11-20", "evidence": "Agreed window"}
        self.assertIsNotNone(self.gap(wait("Justin accepts the release", event=event)))
        self.assertIsNotNone(self.gap(wait("Chris approves the live install", event=event)))

    def test_ask_url_must_match_the_comment_github_returns(self) -> None:
        self.assertIsNotNone(self.gap(wait("Justin creates the guest", f"https://github.com/{REPO}/issues/99#issuecomment-31")))

    def test_attach_asks_gives_each_person_wait_its_own_link_and_refuses_extras(self) -> None:
        record = github_plan_wait.reviewed(evidence="Reviewed", ends_when=["It happens", "It ends", "It lands"],
                                           waiting_for=["Justin creates the guest", "A free worker slot", "Mike tests the build"])
        gate.attach_asks("director", record, [link(11), link(12)])
        self.assertEqual([entry.get("ask_url") for entry in record["waits"]], [link(11), None, link(12)])
        with self.assertRaises(ValueError):
            gate.attach_asks("director", record, [link(11), link(12), link(13)])

    def test_status_reports_author_and_only_the_askeds_reply(self) -> None:
        thread = [
            {"url": link(11), "author": BOT, "body": "Asking @justin-login"},
            {"url": link(12), "author": "someone", "body": "unrelated"},
        ]
        status = gate.ask_status("director", wait("Justin creates the guest", link(11)), wait("Justin creates the guest", link(11))["waits"][0],
                                 trusted={BOT.casefold(), "director"}, read_thread=lambda repo, number: thread)
        self.assertEqual((status["author"], status["replied_since"]), (BOT, False))
        thread.append({"url": link(13), "author": "justin-login", "body": "Done, thanks"})
        status = gate.ask_status("director", wait("Justin creates the guest", link(11)), wait("Justin creates the guest", link(11))["waits"][0],
                                 trusted={BOT.casefold(), "director"}, read_thread=lambda repo, number: thread)
        self.assertEqual((status["author"], status["replied_since"]), (BOT, True))

    def test_status_is_unknown_when_the_thread_cannot_be_read_and_empty_for_events(self) -> None:
        record = wait("Justin creates the guest", link(11))
        status = gate.ask_status("director", record, record["waits"][0], trusted=set(), read_thread=lambda repo, number: None)
        self.assertEqual((status["ask_url"], status["author"], status["replied_since"], status["found"]), (link(11), None, None, None))
        deleted = gate.ask_status("director", record, record["waits"][0], trusted=set(), read_thread=lambda repo, number: [])
        self.assertIs(deleted["found"], False)
        record = wait("A free Anthropic fleet worker slot")
        self.assertIsNone(gate.ask_status("director", record, record["waits"][0], trusted=set(), read_thread=lambda repo, number: None))

    def test_without_a_configured_queue_a_numbered_question_suffices(self) -> None:
        self.queue = None
        self.assertIsNone(self.gap(wait("Chris approves the live install", link(16))))
        self.assertIsNotNone(self.gap(wait("Chris approves the live install", link(12))))
        self.assertNotIn("--register", self.refusal(wait("Chris approves the live install")))


if __name__ == "__main__":
    unittest.main()
