#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import argparse
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

AUTHOR = "fixture-automation"
os.environ["CODEX_AUTOMATION_LOGIN"] = AUTHOR
os.environ["CODEX_SKILLS_ENV_FILE"] = "/missing/fixture.env"

import question_record as record

from skills.github.scripts.github_fixture_support import api_success, with_call_stub


def question() -> dict:
    return {
        "version": record.VERSION,
        "issue": "https://github.com/example/product/issues/3",
        "decision": "Should we reuse the existing runner?",
        "basis": "The current runner has spare capacity. No prior decision applies.",
        "options": [
            {"answer": "Reuse", "changes": "Run this job there", "cost": 0, "next": "Configure the job"},
            {"answer": "Wait", "changes": "Leave the job pending", "cost": "Existing queue upkeep", "next": "Keep it queued"},
        ],
        "recommendation": "Reuse",
        "comparison": "An experienced developer reuses available capacity because it needs less upkeep.",
        "manual": "Answer only",
    }


class QuestionRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = patch.dict(os.environ, {"CODE_HOME": self.temporary.name})
        self.home.start()
        self.addCleanup(self.home.stop)
        self.config = patch.object(record.question_id, "configured_queue", return_value=None)
        self.config.start()
        self.addCleanup(self.config.stop)
        self.args = argparse.Namespace(queue=None, owner="example", decision_author=[AUTHOR], dry_run=False)
        self.source = {
            "body": record.question_id.number_heading(record.render(question()), "Q5"),
            "url": question()["issue"] + "#issuecomment-17",
            "author": AUTHOR,
            "issue": question()["issue"],
        }
        self.storage = unittest.mock.create_autospec(record.QuestionStorage, instance=True)
        self.storage.create.return_value = self.source["url"]
        self.storage.read.return_value = self.source
        self.fetch = unittest.mock.Mock()
        self.actor = patch.object(record.github_comment, "authenticated_actor", return_value=AUTHOR)
        self.actor.start()
        self.addCleanup(self.actor.stop)

    def create(self, value: object) -> dict:
        return record.create(value, self.args, self.storage, self.fetch)

    def test_validation_reports_all_fields_and_writes_nothing(self) -> None:
        invalid = question() | {
            "decision": "",
            "basis": [],
            "manual": None,
            "issue": "https://example.invalid",
            "recommendation": "Unknown",
            "number": "Q999",
        }
        invalid["options"] = [{"answer": "Same"}, {"answer": "same", "cost": False}]
        errors = record.validation_errors(invalid)
        self.assertGreaterEqual(len(errors), 10)
        with self.assertRaises(ValueError) as failure:
            self.create(invalid)
        self.assertTrue(all(error in str(failure.exception) for error in errors))
        self.storage.create.assert_not_called()
        self.fetch.assert_not_called()
        self.actor.target.authenticated_actor.assert_not_called()

    def test_round_trip_preserves_every_choice_and_rejects_changed_source_text(self) -> None:
        value = question()
        value["options"] += [
            {"answer": f"Option {i}", "changes": "Other action", "cost": "Small upkeep", "next": "Act"} for i in range(6)
        ]
        self.source["body"] = record.question_id.number_heading(record.render(value), "Q5")
        view = record.read_record(self.source)
        self.assertEqual(view["record"], value)
        self.assertTrue(all(option["answer"] in view["text"] for option in value["options"]))
        self.source["body"] = self.source["body"].replace(value["decision"], "A different decision?", 1)
        with self.assertRaises(ValueError):
            record.read_record(self.source)

    def test_create_numbers_posts_registers_and_returns_original_text(self) -> None:
        self.args.queue = "example/direction#9"
        report: dict[str, Any] = {"complete": True, "questions": [{"url": self.source["url"], "registration_url": "receipt"}]}
        with patch.object(record.delivery, "run_queue", return_value=report) as register:
            result = self.create(question())
        self.storage.create.assert_called_once_with(question()["issue"], record.render(question()))
        self.assertIn("Q5.", result["text"])
        self.assertNotIn("director-question:record", result["text"])
        self.assertEqual(result["registration"], report["questions"][0]["registration_url"])
        self.assertEqual(register.call_args.args[0].items, [self.source["url"]])
        with record.delivery.queue_lock():
            self.assertEqual(result["url"], self.source["url"])

    def test_dry_run_and_no_queue_preserve_truthful_registration(self) -> None:
        self.args.dry_run = True
        self.assertEqual(self.create(question())["registration"], "dry_run")
        self.storage.create.assert_not_called()
        self.args.dry_run = False
        result = self.create(question())
        self.assertEqual(result["registration"], "not_configured")
        self.assertEqual(result["recipient"], self.args.owner)
        self.fetch.assert_not_called()

    def test_queue_configuration_and_writer_disagreement_refuse_before_post(self) -> None:
        self.args.queue = "example/direction#9"
        with patch.object(record.question_id, "configured_queue", return_value="example/direction#10"):
            with self.assertRaises(ValueError):
                self.create(question())
        self.args.queue = None
        with patch.object(record.github_comment, "authenticated_actor", return_value="untrusted"):
            with self.assertRaises(ValueError):
                self.create(question())
        self.storage.create.assert_not_called()

    def test_oversize_and_unsafe_mentions_refuse_before_post(self) -> None:
        for value in (question() | {"basis": "x" * record.COMMENT_LIMIT}, question() | {"manual": "Ask @outsider"}):
            with self.assertRaises(ValueError):
                self.create(value)
        self.storage.create.assert_not_called()

    def test_real_storage_numbers_and_registers_once_with_readback(self) -> None:
        self.args.queue = "example/direction#9"
        counter = record.question_id.counter_path()
        record.question_id.update(counter, seed=(10, ["fixture"]), allocate=False)
        threads = {record.delivery.source_item(self.source["url"]): [], self.args.queue: []}

        def fetch(item: str) -> list[dict]:
            return [dict(source) for source in threads[item]]

        def api(method: str, endpoint: str, body: Any, **_kwargs: Any) -> Any:
            if endpoint == "/user":
                return api_success({"login": AUTHOR}, operation="test.actor")
            if method == "GET":
                if "/comments/" in endpoint:
                    return api_success(
                        next(c for rows in threads.values() for c in rows if str(c["id"]) == endpoint.rsplit("/", 1)[1]),
                        operation="test.read",
                    )
                repo, number = endpoint.split("/repos/")[1].split("/issues/")
                return api_success(threads[f"{repo}#{number.split('/')[0]}"], operation="test.list")
            if method == "POST":
                repo, number = endpoint.split("/repos/")[1].split("/issues/")
                number = number.split("/")[0]
                rows = threads[f"{repo}#{number}"]
                saved = {
                    "id": sum(len(rows) for rows in threads.values()) + 1,
                    "body": body["body"],
                    "author": AUTHOR,
                    "user": {"login": AUTHOR},
                    "created_at": "2026-10-29T16:00:00Z",
                    "updated_at": "2026-10-29T16:00:00Z",
                    "issue_url": f"https://api.github.com/repos/{repo}/issues/{number}",
                }
                saved["url"] = saved["html_url"] = f"https://github.com/{repo}/issues/{number}#issuecomment-{saved['id']}"
                rows.append(saved)
                return api_success(dict(saved), operation="test.create")
            saved = next(c for rows in threads.values() for c in rows if str(c["id"]) == endpoint.rsplit("/", 1)[1])
            saved["body"] = body["body"]
            return api_success(dict(saved), operation="test.edit")

        def run(calls: list[dict]) -> None:
            storage = record.GitHubComments(fetch)
            first = record.create(question(), self.args, storage, fetch)
            second = record.create(question(), self.args, storage, fetch)
            self.assertEqual(first, second)
            self.assertEqual(sum(c["method"] == "POST" for c in calls), 2)
            record.question_id.verify_number(counter, int(first["question_id"][1:]), first["url"])
            self.assertEqual(record.read_record(storage.read(first["url"]))["text"], first["text"])
            self.assertNotIn("director-question:record", first["text"])

        with patch.dict(os.environ, {"CODEX_SKILLS_ENV_FILE": "/missing/fixture.env"}):
            with_call_stub(api, run)

    def test_partial_readback_or_registration_failure_keeps_saved_source(self) -> None:
        self.args.queue = "example/direction#9"
        for failure in (ValueError("readback unavailable"), {"complete": False}):
            with patch.object(
                record.delivery, "run_queue", side_effect=failure if isinstance(failure, Exception) else None, return_value=failure
            ):
                with self.assertRaises(ValueError) as caught:
                    self.create(question())
                self.assertIn(self.source["url"], str(caught.exception))


if __name__ == "__main__":
    unittest.main()
