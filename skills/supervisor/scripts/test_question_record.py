#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

AUTHOR = "fixture-automation"
os.environ["CODEX_AUTOMATION_LOGIN"] = AUTHOR
os.environ["CODEX_SKILLS_ENV_FILE"] = "/missing/fixture.env"

import q_number_guard as guard
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
        self.home = patch.dict(os.environ, {"CODE_HOME": self.temporary.name, "GH_WITH_ENV_TOKEN_EXPECTED_LOGIN": AUTHOR,
                                            "CODEX_AUTOMATION_BOT_LOGINS": ""})
        self.home.start()
        self.addCleanup(self.home.stop)
        self.config = patch.object(record.question_id, "configured_queue", return_value=None)
        self.config.start()
        self.addCleanup(self.config.stop)
        self.args = argparse.Namespace(queue=None, owner="example", decision_author=[AUTHOR], dry_run=False)
        self.source = {
            "body": record.question_id.number_heading(record.render(question(), legacy=True), "Q5"),
            "url": question()["issue"] + "#issuecomment-17",
            "author": AUTHOR,
            "issue": question()["issue"],
            "updated_at": "2026-11-03T13:00:00Z",
        }
        self.storage = unittest.mock.create_autospec(record.QuestionStorage, instance=True)
        self.storage.create.return_value = self.source
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
        for field in ("decision", "basis", "manual", "issue", "recommendation", "options", "number"):
            self.assertTrue(any(field in error for error in errors), field)
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
        self.source["body"] = record.question_id.number_heading(record.render(value, legacy=True), "Q5")
        view = record.read_record(self.source)
        self.assertEqual(view["record"], value)
        self.assertTrue(all(option["answer"] in view["text"] for option in value["options"]))
        self.source["body"] = self.source["body"].replace(value["decision"], "A different decision?", 1)
        with self.assertRaises(ValueError):
            record.read_record(self.source)

    def test_existing_real_question_renders_as_numbered_bullets_without_rewriting_source(self) -> None:
        source = json.loads(Path(__file__).with_name("fixtures").joinpath("q1160-source.json").read_text())
        original = source["body"]
        view = record.read_record(source)
        self.assertTrue(view["text"].startswith(f"**{view['question_id']}. "))
        lines = view["text"].splitlines()
        self.assertTrue(lines[0].endswith("**"))
        choices = [line for line in lines if line.startswith("  - **")]
        self.assertEqual(len(choices), len(view["record"]["options"]))
        for option, line in zip(view["record"]["options"], choices, strict=True):
            for field in record.OPTION_FIELDS:
                self.assertIn(str(option[field]), line)
            self.assertNotIn("..", line)
        for field in ("decision", "basis", "comparison", "manual", "recommendation"):
            self.assertIn(view["record"][field], view["text"])
        self.assertEqual(view["text"].count(view["record"]["comparison"]), 1)
        self.assertEqual(source["body"], original)
        self.assertEqual(view["url"], source["url"])
        self.storage.read.return_value = source
        self.args.decision_author.append(source["author"])
        self.storage.create.return_value = source
        self.assertEqual(self.create(view["record"])["text"], view["text"])
        shown = StringIO()
        with redirect_stdout(shown):
            self.assertEqual(record.main(["show-question", source["url"]], lambda _item: [source]), 0)
        self.assertEqual(json.loads(shown.getvalue())["text"], view["text"])
        source["body"] = original.replace(view["record"]["manual"], "Do something else", 1)
        with self.assertRaises(ValueError):
            record.read_record(source)

    def test_existing_question_chat_print_matches_create_and_update(self) -> None:
        path = Path(self.temporary.name) / "question.json"
        path.write_text(json.dumps(question()))
        outputs = []
        for command in ("create", "update", "show-question"):
            output = StringIO()
            arguments = (
                [command, self.source["url"], "--chat"]
                if command == "show-question"
                else [command, "--record-file", str(path), "--owner", self.args.owner]
            )
            if command == "update":
                arguments += [self.source["url"], "--expected-updated-at", self.source["updated_at"]]
            with (
                patch.object(record, "create", return_value=record.read_record(self.source) | {"registration": "not_configured"}),
                patch.object(record, "update", return_value=record.read_record(self.source) | {"registration": "unchanged"}),
                redirect_stdout(output),
            ):
                self.assertEqual(record.main(arguments, lambda _item: [self.source]), 0)
            outputs.append(output.getvalue().split("Registration:")[0].strip())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])
        self.assertIn(record.read_record(self.source)["text"], outputs[2])
        self.assertIn(self.source["url"], outputs[2])

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
        self.source["author"] = "untrusted"
        with self.assertRaises(ValueError):
            self.create(question())
        self.source["author"] = AUTHOR

    def test_oversize_and_unsafe_mentions_refuse_before_post(self) -> None:
        for value in (question() | {"basis": "x" * record.COMMENT_LIMIT}, question() | {"manual": "Ask @outsider"}):
            with self.assertRaises(ValueError):
                self.create(value)
        self.storage.create.assert_not_called()

    def test_untrusted_writer_cannot_create_or_number_a_recovered_question(self) -> None:
        self.args.decision_author = []
        counter = record.question_id.counter_path()
        seed = record.question_id.update(counter, seed=(300, ["fixture"]), allocate=False)
        saved = {"id": 17, "html_url": self.source["url"], "user": {"login": AUTHOR},
                 "body": record.render(question(), legacy=True), "issue_url": "https://api.github.com/repos/example/product/issues/3"}

        def api(method: str, endpoint: str, body: Any, **_kwargs: Any) -> Any:
            if method == "GET":
                return api_success(saved if "/comments/" in endpoint else [saved], operation="test.read")
            saved["body"] = body["body"]
            return api_success(dict(saved), operation="test.edit")

        def run(calls: list[dict]) -> None:
            with self.assertRaises(record.github_comment.CommentError):
                record.create(question(), self.args, record.GitHubComments(self.fetch), self.fetch)
            self.assertFalse(calls)

        with_call_stub(api, run)
        self.fetch.assert_not_called()
        self.assertEqual(json.loads(counter.read_text())["last_id"], seed)

    def test_create_and_default_routine_reader_agree_on_open_questions(self) -> None:
        self.actor.stop()
        self.args.queue = "example/direction#9"
        counter = record.question_id.counter_path()
        record.question_id.update(counter, seed=(300, ["fixture"]), allocate=False)
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
            storage = record.GitHubComments(record.delivery.oq.fetch)
            first = record.create(question(), self.args, storage, record.delivery.oq.fetch)
            self.assertLessEqual(len(calls), 10)
            self.assertLessEqual(sum(c["path"] == "/user" for c in calls), 2)
            published = record.github_comment.comment(
                "issue", 3, record.render(question(), legacy=True), repo="example/product", dedupe_body=True
            )
            self.assertEqual(first["url"], published["url"])
            path = Path(self.temporary.name) / "question.json"
            path.write_text(json.dumps(question()))
            created_output = StringIO()
            with redirect_stdout(created_output):
                self.assertEqual(record.main(["create", "--record-file", str(path), "--owner", self.args.owner,
                                               "--queue", self.args.queue], fetch), 0)
            self.assertIn(first["url"], created_output.getvalue())
            self.assertTrue(created_output.getvalue().startswith(f"**{first['question_id']}. "))
            self.assertEqual(sum(c["method"] == "POST" for c in calls), 2)
            record.question_id.verify_number(counter, int(first["question_id"][1:]), first["url"])
            self.assertEqual(record.read_record(storage.read(first["url"]))["text"], first["text"])
            self.assertIsNone(record.RECORD.search(first["text"]))
            self.assertIsNotNone(guard.missing_context(first["text"]))
            state = Path(self.temporary.name) / "questions-state.json"

            def routine_check(*options: str) -> dict:
                output = StringIO()
                with (
                    patch.object(record.delivery.oq, "fetch", side_effect=fetch),
                    patch.object(sys, "argv", ["oq.py", "--owner", self.args.owner, "--queue", self.args.queue,
                                               "--changes-state", str(state), *options]),
                    redirect_stdout(output),
                ):
                    self.assertEqual(record.delivery.oq.main(), 0)
                return json.loads(output.getvalue())

            with patch.dict(os.environ, {"GH_WITH_ENV_TOKEN_EXPECTED_LOGIN": "new-writer", "CODEX_AUTOMATION_BOT_LOGINS": AUTHOR}):
                self.assertEqual(routine_check()["queue_length"], 1)
            queued = routine_check()
            self.assertEqual(queued["queue_length"], 1)
            self.assertEqual(queued["untrusted_records"], [])
            self.assertIn(first["url"], json.dumps(queued["questions"]))
            prior_updated = storage.read(first["url"])["updated_at"]
            revised = question() | {"basis": "The runner's capacity has been checked again."}
            path.write_text(json.dumps(revised))
            update_output = StringIO()
            before = len([c for c in calls if c["method"] == "POST"])
            last_number = json.loads(counter.read_text())["last_id"]
            with redirect_stdout(update_output):
                self.assertEqual(record.main(["update", first["url"], "--record-file", str(path),
                                               "--expected-updated-at", prior_updated, "--owner", self.args.owner], fetch), 0)
            self.assertIn(revised["basis"], update_output.getvalue())
            self.assertEqual(record.read_record(storage.read(first["url"]))["question_id"], first["question_id"])
            self.assertEqual(len([c for c in calls if c["method"] == "POST"]), before)
            self.assertEqual(json.loads(counter.read_text())["last_id"], last_number)
            threads[record.delivery.source_item(first["url"])].append({
                "id": 100, "author": AUTHOR, "url": question()["issue"] + "#issuecomment-100",
                "body": f"Director decision {first['question_id']}: reuse the runner, combined with Q299's testing decision.",
                "created_at": "2026-10-29T17:00:00Z",
            })
            decided = routine_check("--decision-author", AUTHOR.upper())
            self.assertEqual(decided["queue_length"], 0)
            self.assertEqual([row["url"] for row in decided["questions"] if row.get("removed")], [first["url"]])

        with patch.dict(os.environ, {"CODEX_SKILLS_ENV_FILE": "/missing/fixture.env"}):
            with_call_stub(api, run)

    def test_source_reads_need_no_local_counter_or_lock(self) -> None:
        storage = record.GitHubComments(lambda _item: [self.source])
        view = record.read_record(storage.read(self.source["url"]))
        self.assertEqual(view["record"], question())
        self.assertEqual(view["author"], self.source["author"])
        output = StringIO()
        with (
            patch.object(record.delivery.oq, "fetch", return_value=[self.source]),
            patch.object(sys, "argv", ["oq.py", "show-question", self.source["url"]]),
            redirect_stdout(output),
        ):
            self.assertEqual(record.delivery.oq.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), view)
        counter = record.question_id.counter_path()
        self.assertFalse(counter.exists())
        self.assertFalse(counter.with_name(counter.name + ".lock").exists())

    def test_public_create_command_validates_and_renders_without_allocation(self) -> None:
        path = Path(self.temporary.name) / "question.json"
        value = question()
        path.write_text(json.dumps(value))
        command = [
            sys.executable,
            str(Path(__file__).with_name("oq.py")),
            "create",
            "--record-file",
            str(path),
            "--owner",
            self.args.owner,
            "--dry-run",
        ]
        environment = {**os.environ, "HOME": self.temporary.name, "CODEX_HOME": self.temporary.name}
        valid = subprocess.run(command, env=environment, text=True, capture_output=True, timeout=15)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        for field in ("decision", "basis", "comparison", "manual"):
            self.assertIn(value[field], valid.stdout)
        self.assertIsNone(record.RECORD.search(valid.stdout))
        self.assertFalse(valid.stdout.startswith("Director question:"))
        value.update(basis=[], manual="")
        path.write_text(json.dumps(value))
        invalid = subprocess.run(command, env=environment, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(invalid.returncode, 0)
        for field in ("basis", "manual"):
            self.assertIn(field, invalid.stderr)
        self.assertFalse(record.question_id.counter_path().exists())

    def test_registration_failure_keeps_saved_source(self) -> None:
        self.args.queue = "example/direction#9"
        for failure in (ValueError("readback unavailable"), {"complete": False}):
            with patch.object(
                record.delivery, "run_queue", side_effect=failure if isinstance(failure, Exception) else None, return_value=failure
            ):
                with self.assertRaises(ValueError) as caught:
                    self.create(question())
                self.assertIn(self.source["url"], str(caught.exception))

    def test_update_preserves_identity_and_refuses_answers_or_stale_body(self) -> None:
        self.args.url = self.source["url"]
        self.args.expected_updated_at = self.source["updated_at"]
        revised = question() | {"basis": "The runner now has more spare capacity."}
        self.source["id"] = 17
        self.fetch.return_value = [self.source]
        updated = {**self.source, "body": record.question_id.number_heading(record.render(revised, legacy=True), "Q5")}
        self.storage.update.return_value = updated
        with patch.object(record.delivery, "run_queue") as register:
            result = record.update(revised, self.args, self.storage, self.fetch)
        self.assertEqual((result["question_id"], result["url"]), ("Q5", self.source["url"]))
        register.assert_not_called()
        self.storage.create.assert_not_called()
        self.storage.update.assert_called_once_with(self.source["url"], updated["body"], self.source["body"], self.source["updated_at"])
        self.storage.update.reset_mock()
        for author, body in (("example", "Agree"), (AUTHOR, "Director decision Q5: approved")):
            self.fetch.return_value = [self.source, {"id": 18, "url": self.source["url"] + "1", "author": author, "body": body}]
            with self.assertRaises(ValueError):
                record.update(revised, self.args, self.storage, self.fetch)
        self.fetch.return_value = [self.source]
        self.args.expected_updated_at = "stale timestamp"
        with self.assertRaises(ValueError):
            record.update(revised, self.args, self.storage, self.fetch)
        self.storage.update.assert_not_called()


BOOTSTRAP = """
import runpy, subprocess, sys
from unittest.mock import patch
script = sys.argv.pop(1)
response = subprocess.CompletedProcess([], 0, stdout=sys.stdin.buffer.read(), stderr=b'')
with patch.object(subprocess, 'run', return_value=response):
    runpy.run_path(script, run_name='__main__')
"""


class PublicReadTests(unittest.TestCase):
    def read(self, *flags: str) -> subprocess.CompletedProcess[str]:
        source = "https://github.com/example/repo/issues/1#issuecomment-1"
        self.accepted_body = f"Director decision Q1218: only touch skills/**/*.md\n\nOnly after https://example.test/backup passes.\n\nQuestion: {source}"
        comments = [
            {"id": 1, "html_url": source, "user": {"login": "fixture-bot"}, "body": "Director question: **Q1218.** Keep both?"},
            {
                "id": 2,
                "html_url": source.replace("-1", "-2"),
                "user": {"login": "fixture-bot"},
                "body": self.accepted_body,
            },
            {
                "id": 3,
                "html_url": source.replace("-1", "-3"),
                "user": {"login": "outsider"},
                "body": f"Director decision Q1218: Drop both.\n\nQuestion: {source}",
            },
            {"id": 4, "html_url": source.replace("-1", "-4"), "user": {"login": "director"},
             "body": f"Not sure yet, what is the cost? {source}"},
        ]
        response = "HTTP/2.0 200\ncontent-type: application/json\n\n" + json.dumps(comments)
        with tempfile.TemporaryDirectory() as home:
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    BOOTSTRAP,
                    str(Path(__file__).with_name("oq.py")),
                    "example/repo#1",
                    "--owner",
                    "director",
                    *flags,
                ],
                cwd=home,
                input=response,
                text=True,
                capture_output=True,
                timeout=20,
                env={
                    "PATH": os.environ["PATH"],
                    "HOME": home,
                    "CODE_HOME": home,
                    "CODEX_HOME": home,
                    "CODEX_AUTOMATION_LOGIN": "fixture-bot",
                    "CODEX_GITHUB_TOKEN": "fixture-token",
                },
            )

    def test_public_read_works_outside_the_catalog(self) -> None:
        result = self.read()
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = json.loads(result.stdout)["questions"][0]
        self.assertEqual(entry["status"], "answered")
        self.assertNotIn("decision_comments", entry)

    def test_public_read_exposes_only_accepted_answer_text_and_its_source(self) -> None:
        result = self.read("--include-answers")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = json.loads(result.stdout)["questions"][0]
        self.assertEqual(entry["number"], "Q1218")
        accepted = entry["decision_comments"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["body"], self.accepted_body)
        self.assertEqual(accepted[0]["url"], entry["answers"][0])


if __name__ == "__main__":
    unittest.main()
