#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise pilot parsers and terminal behavior with disposable transcripts/adapters."""

import argparse
import asyncio
import json
import subprocess
import shlex
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import account_choice
import close_ttys
import codex_idle_watch
import finished_map
import iterm_tab
import oq
import session_record
import status


def claude(text, **extra):
    return {
        "type": "assistant",
        "timestamp": "2026-10-03T00:00:00Z",
        "message": {"content": [{"type": "text", "text": text}], **extra},
    }


def event(kind, **extra):
    return {
        "type": "event_msg",
        "timestamp": "2026-10-03T00:00:00Z",
        "payload": {"type": kind, **extra},
    }


class TranscriptTests(unittest.TestCase):
    def test_claude_cached_context_and_latest_response(self):
        records = [
            claude("old"),
            claude(
                "done\nSafe to exit: yes",
                usage={
                    "input_tokens": 4,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 10,
                },
            ),
        ]
        result = session_record.summarize(records, "claude")
        self.assertEqual(result["context_tokens"], 34)
        self.assertTrue(result["safe_verdict"])
        self.assertFalse(
            session_record.summarize(
                records + [{"type": "user", "message": "continue"}], "claude"
            )["safe_verdict"]
        )

    def test_negative_quoted_and_conditional_verdicts(self):
        for text in (
            "> Safe to exit: yes",
            'I quoted "Safe to exit: yes"',
            "Safe to exit: yes, if CI passes",
            "Safe to exit: conditional",
            "Safe to exit: no",
            "Safe to exit: yes — if CI passes",
            "```text\nSafe to exit: yes",
            "Safe to exit: yes\nStill doing work",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    session_record.summarize([claude(text)], "claude")["safe_verdict"]
                )

    def test_standard_closeout_bullets_and_bold_labels(self):
        for text in (
            "- Safe to exit: yes",
            "- **Safe to exit:** yes",
            "* **Safe to exit: yes**",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    session_record.summarize([claude(text)], "claude")["safe_verdict"]
                )
        for text in (
            "> - **Safe to exit:** yes",
            "- **Safe to exit:** yes, if CI passes",
        ):
            with self.subTest(text=text):
                self.assertFalse(
                    session_record.summarize([claude(text)], "claude")["safe_verdict"]
                )

    def test_tool_use_after_verdict_invalidates_it(self):
        record = claude("Safe to exit: yes")
        record["message"]["content"].append({"type": "tool_use", "name": "Bash"})
        self.assertFalse(session_record.summarize([record], "claude")["safe_verdict"])

    def test_codex_turn_end_abort_and_resume(self):
        records = [
            event("task_started"),
            event("agent_message", message="Safe to exit: yes"),
            event("task_complete"),
        ]
        self.assertTrue(session_record.summarize(records, "codex")["safe_verdict"])
        for tail in (
            event("turn_aborted"),
            event("task_started"),
            event("user_message", message="continue"),
        ):
            self.assertFalse(
                session_record.summarize(records + [tail], "codex")["safe_verdict"]
            )

    def test_memory_citation_tail_preserves_verdict_constraints(self):
        records = session_record.read_jsonl(
            Path(__file__).parent / "fixtures/codex-closeout-memory-citation.jsonl"
        )
        response = records[1]["payload"]["content"][0]["text"]
        citation = response[response.index("<oai-mem-citation>"):]
        self.assertTrue(session_record.summarize(records, "codex")["safe_verdict"])
        for text in (
            "> Safe to exit: yes",
            '"Safe to exit: yes"',
            "Not Safe to exit: yes",
            "Safe to exit: no",
            "Safe to exit: yes, if CI passes",
            "If CI passes:\n  Safe to exit: yes, conditional",
            "```text\nSafe to exit: yes",
            "Safe to exit: yes\nStill doing work",
        ):
            with self.subTest(text=text):
                self.assertFalse(session_record.summarize(
                    [event("agent_message", message=text + "\n\n" + citation),
                     event("task_complete")], "codex"
                )["safe_verdict"])
        for suffix in (
            citation + "Still doing work",
            citation.replace("</oai-mem-citation>", ""),
            citation.replace("<oai-mem-citation>", "> <oai-mem-citation>"),
            citation + citation,
        ):
            with self.subTest(suffix=suffix):
                self.assertFalse(session_record.summarize(
                    [event("agent_message", message="Safe to exit: yes\n\n" + suffix),
                     event("task_complete")], "codex"
                )["safe_verdict"])
        self.assertFalse(session_record.summarize(
            [event("agent_message", message=citation), event("task_complete")],
            "codex"
        )["safe_verdict"])
        for tail in (event("turn_aborted"), event("task_started"),
                     event("user_message", message="continue"), event("function_call")):
            with self.subTest(tail=tail):
                self.assertFalse(session_record.summarize(records + [tail], "codex")["safe_verdict"])

    def test_codex_response_item_text_and_usage(self):
        records = [
            event(
                "token_count",
                info={
                    "last_token_usage": {"input_tokens": 12},
                    "total_token_usage": {"total_tokens": 100},
                },
            ),
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Safe to exit: yes"}],
                },
            },
            event("task_complete"),
        ]
        result = session_record.summarize(records, "codex")
        self.assertTrue(result["safe_verdict"])
        self.assertEqual(result["context_tokens"], 12)
        self.assertEqual(result["total_tokens"], 100)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transcript = self.root / "session.jsonl"
        self.ledger = self.root / "ledger.json"
        self.entry = {
            "session_id": "02b123-new-prefix",
            "harness": "codex",
            "transcript": "session.jsonl",
            "supervisor_owned": True,
            "pid": 123,
            "tty": "ttys001",
            "iterm_session_id": "term1",
            "handoff_url": "https://github.com/example/repo/issues/1#issuecomment-2",
        }
        self.write(
            [
                event("agent_message", message="Safe to exit: yes"),
                event("task_complete"),
            ]
        )

    def write(self, records):
        records = [
            {"type": "session_meta", "payload": {"id": self.entry["session_id"]}}
        ] + records
        self.transcript.write_text(
            "".join(json.dumps(record) + "\n" for record in records)
        )
        self.ledger.write_text(json.dumps([self.entry]))
        os.utime(self.transcript, (1000, 1000))

    def test_claude_local_exit_tail_allows_exited_tab_close(self):
        self.entry["harness"] = "claude"
        records = [
            claude("Safe to exit: yes"),
            {
                "type": "user",
                "isMeta": True,
                "message": {
                    "content": "<local-command-caveat>local command</local-command-caveat>"
                },
            },
            {
                "type": "user",
                "message": {
                    "content": "<command-name>/exit</command-name>\n<command-message>exit</command-message>\n<command-args></command-args>"
                },
            },
            {
                "type": "user",
                "message": {
                    "content": "<local-command-stdout>(no content)</local-command-stdout>"
                },
            },
        ]
        for record in records:
            record["sessionId"] = self.entry["session_id"]
        self.write(records)
        entry = session_record.load_ledger(self.ledger)[0]
        terminal = SimpleNamespace(
            session_id="term1",
            async_get_variable=AsyncMock(return_value="ttys001"),
            async_close=AsyncMock(),
        )
        app = SimpleNamespace(
            terminal_windows=[
                SimpleNamespace(tabs=[SimpleNamespace(sessions=[terminal])])
            ]
        )
        with patch.object(
            close_ttys,
            "inventory",
            return_value=[(20, "ttys001", "-zsh"), (21, "ttys001", "/usr/bin/login")],
        ):
            asyncio.run(close_ttys.close(app, entry, True, True, True))
        terminal.async_close.assert_awaited_once_with(force=False)
        for kind in ("/compact", "/clear"):
            changed = records.copy()
            changed[-2] = {
                "type": "user",
                "message": {"content": f"<command-name>{kind}</command-name>"},
            }
            self.assertFalse(
                session_record.summarize(changed, "claude")["safe_verdict"]
            )
        self.assertFalse(
            session_record.summarize(
                records + [{"type": "user", "message": {"content": "continue"}}],
                "claude",
            )["safe_verdict"]
        )

    def test_bad_watched_transcript_does_not_hide_healthy_session(self):
        bad = {**self.entry, "session_id": "bad", "transcript": "missing.jsonl"}
        self.ledger.write_text(json.dumps([bad, self.entry]))
        seen = {}
        notices = codex_idle_watch.poll(self.ledger, seen, 90, 1200)
        self.assertIn("error", notices[0])
        self.assertEqual(notices[1]["turn_end"], "task_complete")
        self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1200), [])

    def test_relative_path_and_duplicate_identity(self):
        self.assertEqual(
            session_record.load_ledger(self.ledger)[0]["transcript"],
            str(self.transcript.resolve()),
        )
        self.ledger.write_text(json.dumps([self.entry, self.entry]))
        with self.assertRaises(ValueError):
            session_record.load_ledger(self.ledger)

    def test_wrong_transcript_identity_is_not_a_candidate(self):
        self.entry["session_id"] = "another-thread"
        self.ledger.write_text(json.dumps([self.entry]))
        self.assertFalse(finished_map.candidates(self.ledger)[0]["candidate"])

    def test_iterm_tty_path_is_normalized_for_process_matching(self):
        self.entry["tty"] = "/dev/ttys001"
        self.ledger.write_text(json.dumps([self.entry]))
        self.assertEqual(session_record.load_ledger(self.ledger)[0]["tty"], "ttys001")

    def test_partial_write_is_error_not_finished(self):
        self.transcript.write_text("{unfinished")
        self.assertIn("error", status.snapshot(self.ledger)[0])
        self.assertFalse(finished_map.candidates(self.ledger)[0]["candidate"])
        self.assertIn("error", codex_idle_watch.poll(self.ledger, {}, 90, 1200)[0])

    def test_owned_candidate_and_director_session(self):
        self.assertTrue(finished_map.candidates(self.ledger)[0]["candidate"])
        self.entry["supervisor_owned"] = False
        self.ledger.write_text(json.dumps([self.entry]))
        self.assertFalse(finished_map.candidates(self.ledger)[0]["candidate"])

    def test_watch_dedup_resume_and_abort_notice(self):
        seen = {}
        self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1050), [])
        notice = codex_idle_watch.poll(self.ledger, seen, 90, 1200)
        self.assertEqual(notice[0]["session_id"], self.entry["session_id"])
        self.assertIsNone(notice[0]["clean"])
        self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1200), [])
        self.write([event("task_complete"), event("task_started")])
        self.assertEqual(codex_idle_watch.poll(self.ledger, seen, 90, 1200), [])
        self.write([event("task_started"), event("turn_aborted")])
        self.assertEqual(
            codex_idle_watch.poll(self.ledger, {}, 90, 1200)[0]["turn_end"],
            "turn_aborted",
        )

    def test_close_only_exited_exact_session_and_never_force(self):
        entry = session_record.load_ledger(self.ledger)[0]
        terminal = SimpleNamespace(
            session_id="term1",
            async_get_variable=AsyncMock(return_value="/dev/ttys001"),
            async_close=AsyncMock(),
        )
        app = SimpleNamespace(
            terminal_windows=[
                SimpleNamespace(tabs=[SimpleNamespace(sessions=[terminal])])
            ]
        )
        with patch.object(
            close_ttys,
            "inventory",
            return_value=[(20, "ttys001", "-zsh"), (21, "ttys001", "/usr/bin/login")],
        ):
            result = asyncio.run(close_ttys.close(app, entry, False, True, True))
            self.assertTrue(result["dry_run"])
            terminal.async_close.assert_not_awaited()
            asyncio.run(close_ttys.close(app, entry, True, True, True))
            terminal.async_close.assert_awaited_once_with(force=False)
        for rows in (
            [(123, "ttys001", "claude")],
            [(456, "ttys001", "/usr/bin/codex")],
            [(123, "other", "unrelated")],
            [(456, "ttys001", "/usr/bin/node")],
        ):
            with (
                patch.object(close_ttys, "inventory", return_value=rows),
                self.assertRaises(ValueError),
            ):
                asyncio.run(close_ttys.close(app, entry, True, True, True))
        with self.assertRaises(ValueError):
            asyncio.run(close_ttys.close(app, entry, True, False, True))
        terminal.async_get_variable.return_value = "/dev/ttys999"
        with self.assertRaises(ValueError):
            asyncio.run(close_ttys.close(app, entry, True, True, True))

    def test_memory_citation_fixture_through_status_candidates_and_close(self):
        fixture = Path(__file__).parent / "fixtures/codex-closeout-memory-citation.jsonl"
        self.transcript.write_text(fixture.read_text())
        result = status.snapshot(self.ledger)[0]
        self.assertTrue(result["safe_verdict"])
        self.assertIn("<oai-mem-citation>", result["last_text"])
        self.assertTrue(finished_map.candidates(self.ledger)[0]["candidate"])
        entry = session_record.load_ledger(self.ledger)[0]
        terminal = SimpleNamespace(
            session_id="term1",
            async_get_variable=AsyncMock(return_value="ttys001"),
            async_close=AsyncMock(),
        )
        app = SimpleNamespace(terminal_windows=[
            SimpleNamespace(tabs=[SimpleNamespace(sessions=[terminal])])
        ])
        with patch.object(close_ttys, "inventory", return_value=[(20, "ttys001", "-zsh")]):
            self.assertTrue(asyncio.run(close_ttys.close(app, entry, False, True, True))["dry_run"])
            terminal.async_close.assert_not_awaited()
            asyncio.run(close_ttys.close(app, entry, True, True, True))
        terminal.async_close.assert_awaited_once_with(force=False)

    def test_activity_race_keeps_tab(self):
        entry = session_record.load_ledger(self.ledger)[0]
        terminal = SimpleNamespace(
            session_id="term1",
            async_get_variable=AsyncMock(return_value="ttys001"),
            async_close=AsyncMock(),
        )
        app = SimpleNamespace(
            terminal_windows=[
                SimpleNamespace(tabs=[SimpleNamespace(sessions=[terminal])])
            ]
        )
        with (
            patch.object(
                session_record,
                "session_status",
                side_effect=[{"safe_verdict": True}, {"safe_verdict": False}],
            ) as reader,
            patch.object(
                close_ttys, "inventory", return_value=[(20, "ttys001", "/bin/zsh")]
            ),
            self.assertRaises(ValueError),
        ):
            asyncio.run(close_ttys.close(app, entry, True, True, True))
        self.assertEqual(reader.call_count, 2)
        terminal.async_close.assert_not_awaited()


class QuestionTests(unittest.TestCase):
    def test_both_spellings_preserve_answer_authority_and_question_link(self):
        for question_role in ("Director", "Owner"):
            for decision_role in ("Director", "Owner"):
                with self.subTest(question=question_role, decision=decision_role):
                    question = {
                        "id": 123,
                        "url": "https://example.test/questions/123",
                        "body": f"**{question_role} question:** proceed?",
                        "author": "bot",
                    }
                    answers = [
                        {"author": "outsider", "body": f"{decision_role} decision: yes 123", "url": "foreign"},
                        {"author": "bot", "body": f"{decision_role} decision: yes 1234", "url": "unlinked"},
                        {"author": "bot", "body": "yes 123", "url": "unrecorded"},
                        {"author": "bot", "body": "Director decision needed: see 123", "url": "needed"},
                        {"author": "bot", "body": "Director decisions still open: 123", "url": "open"},
                    ]
                    # Even the Director's linked follow-up question is not an answer.
                    answers.extend(
                        {
                            "id": comment_id,
                            "author": "director",
                            "body": f"{role} question: about 123?",
                            "url": role,
                        }
                        for comment_id, role in enumerate(("Director", "Owner"), start=124)
                    )
                    self.assertEqual(
                        oq.questions([question, *answers], "director", {"bot"})[0]["status"],
                        "needs_review",
                    )
                    for body in (
                        f"**{decision_role} decision:** yes 123",
                        f"{decision_role} decision, recorded: {question['url']}",
                        f"**{decision_role} decision** on {question['url']}: yes",
                        f"{decision_role} decision (recorded in chat): yes 123",
                    ):
                        answer = {"author": "bot", "body": body, "url": "recorded"}
                        result = oq.questions([question, *answers, answer], "director", {"bot"})[0]
                        self.assertEqual(result["answers"], ["recorded"])
                        self.assertEqual(result["status"], "answered")
                    direct = {"author": "director", "body": "yes 123", "url": "direct"}
                    self.assertEqual(
                        oq.questions([question, direct], "director", {"bot"})[0]["answers"],
                        ["direct"],
                    )

    def test_all_questions_and_only_linked_authorized_answers(self):
        q1 = {
            "id": 1,
            "url": "https://example.test/1",
            "author": "bot",
            "body": "Owner question: first?",
        }
        q2 = {
            "id": 2,
            "url": "https://example.test/2",
            "author": "bot",
            "body": "Owner question: second?",
        }
        comments = [
            q1,
            q2,
            {
                "author": "outsider",
                "body": "Owner decision https://example.test/1",
                "url": "a",
            },
            {"author": "owner", "body": "Unrelated reply", "url": "b"},
            {
                "author": "bot",
                "body": "**Owner decision** on https://example.test/2: yes",
                "url": "c",
            },
        ]
        result = oq.questions(comments, "owner", {"bot"})
        self.assertEqual([q["status"] for q in result], ["needs_review", "answered"])
        self.assertEqual(result[1]["answers"], ["c"])

    def test_bold_question_heading_is_included(self):
        comment = {
            "id": 1,
            "url": "https://example.test/1",
            "body": "**Owner question:** proceed?",
            "author": "bot",
        }
        self.assertEqual(len(oq.questions([comment], "owner", {"bot"})), 1)

    def test_paged_helper_failure_never_means_no_questions(self):
        with (
            patch.object(
                oq.subprocess,
                "run",
                return_value=SimpleNamespace(stdout=json.dumps({"ok": False})),
            ),
            self.assertRaises(ValueError),
        ):
            oq.fetch("example/repo#1")
        with self.assertRaises(ValueError):
            oq.fetch("repo#1")


NOW = datetime(2026, 10, 3, 16, tzinfo=timezone.utc)
ACCOUNTS_TOML = """
[accounts]
snapshot_command = ["context-panel-snapshot"]
reserve = 0.05

[[accounts.account]]
name = "main"
provider = "anthropic"
context_panel_label = "Main"
env = { CLAUDE_CONFIG_DIR = "~/claude-main" }
reserve = 0.2

[[accounts.account]]
name = "spare"
provider = "anthropic"
context_panel_configuration_id = "cfg-spare"
env = { CLAUDE_CONFIG_DIR = "/accounts/spare dir" }
"""


def account_row(label, remaining, resets, state="available", cfg="cfg"):
    """resets: (five-hour, weekly) reset times, or one unlabeled window's."""
    labels = ["Claude 5-hour", "Claude weekly"] if len(resets) == 2 else ["Claude"]
    return {
        "provider": "anthropic",
        "label": label,
        "configurationID": cfg,
        "id": "anthropic-def" if cfg == "cfg-spare" else "anthropic-abc",
        "state": state,
        "remainingFraction": remaining,
        "windows": [
            {"label": name, "naturalResetAt": reset} for name, reset in zip(labels, resets)
        ],
    }


def accounts_config(text=ACCOUNTS_TOML):
    return account_choice.validate_config(
        __import__("tomllib").loads(text)["accounts"]
    )


def choose_from(rows, recommendations=None, **extra):
    snapshot = {"schemaVersion": 1, "accounts": rows}
    if recommendations is not None:
        snapshot["answers"] = {"useNext": recommendations}
    return account_choice.choose(
        "anthropic", accounts_config(), snapshot, None, now=NOW, **extra
    )


class AccountChoiceTests(unittest.TestCase):
    def snapshot(self, provider="anthropic", order=None, stale=False, age=0):
        rows = [account_row("main", .01, []), account_row("spare", .04, [], cfg="cfg-spare")]
        for row in rows:
            row["provider"] = provider
            row["id"] = row["id"].replace("anthropic", provider)
        order = order if order is not None else [rows[1]["id"], rows[0]["id"], rows[1]["id"]]
        return {"schemaVersion": 1, "accounts": rows,
                "answers": {"useNext": [] if stale else [{"provider": provider, "accountID": rows[1]["id"]}]},
                "ranking": [{"provider": provider, "launchOrder": order,
                             "basedOnStaleReadings": stale,
                             "readingsObservedAt": (NOW - timedelta(minutes=age)).isoformat()}]}

    def test_use_next_is_authoritative_despite_config_reserves_order_and_row_state(self):
        for provider in account_choice.PROVIDERS:
            config = accounts_config()
            for account in config["accounts"]:
                account["provider"] = provider
            snapshot = self.snapshot(provider)
            snapshot["accounts"][1].update(state="limited", useLast=True)
            # The tightest (model-only) window may be empty while CP ranks the main window.
            snapshot["accounts"][1]["remainingFraction"] = 0
            choice = account_choice.choose(provider, config, snapshot, None, now=NOW)
            self.assertEqual((choice["name"], choice["source"]), ("spare", "context-panel"))
            self.assertEqual(choice["account_id"], provider + "-def")
            config["accounts"].reverse()
            self.assertEqual(account_choice.choose(provider, config, snapshot, None, now=NOW)["name"], "spare")

    def test_batch_follows_published_order_including_repeated_accounts(self):
        snapshot = self.snapshot()
        choices = account_choice.choose_batch("anthropic", accounts_config(), snapshot, None, count=3, now=NOW)
        self.assertEqual([c["name"] for c in choices], ["spare", "main", "spare"])
        with self.assertRaisesRegex(ValueError, "smaller batch"):
            account_choice.choose_batch("anthropic", accounts_config(), snapshot, None, count=4, now=NOW)

    def test_stale_list_uses_observation_age_and_refuses_at_thirty_minutes(self):
        for age in (0, 29.99):
            snapshot = self.snapshot(stale=True, age=age)
            choice = account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)
            self.assertEqual((choice["name"], choice["source"]), ("spare", "context-panel-stale"))
        for age in (30, 31, -1):
            with self.subTest(age=age), self.assertRaisesRegex(ValueError, "not under 30 minutes"):
                account_choice.choose("anthropic", accounts_config(), self.snapshot(stale=True, age=age), None, now=NOW)
        snapshot = self.snapshot(stale=True)
        snapshot["ranking"][0].pop("readingsObservedAt")
        with self.assertRaisesRegex(ValueError, "not under 30 minutes"):
            account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)

    def test_no_choice_refuses_and_reports_next_capacity_without_private_fallback(self):
        snapshot = self.snapshot(order=[])
        snapshot["answers"]["useNext"] = []
        snapshot["ranking"][0]["nextCapacityAt"] = "2026-10-04T03:00:00Z"
        with self.assertRaisesRegex(ValueError, "next capacity at 2026-10-04T03:00:00Z"):
            account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)
        with self.assertRaisesRegex(ValueError, "snapshot unavailable.*exited 1"):
            account_choice.choose("anthropic", accounts_config(), None, "snapshot command exited 1")

    def test_ambiguous_or_unconfigured_choices_refuse_without_substitution(self):
        for change, error in (("missing", "single account row"), ("unconfigured", "single configured account"),
                              ("duplicates", "ambiguous"), ("rowduplicates", "single account row")):
            snapshot = self.snapshot()
            if change == "missing":
                snapshot["answers"]["useNext"][0]["accountID"] = "anthropic-123"
            elif change == "unconfigured":
                snapshot["accounts"][1]["configurationID"] = "unknown"
            elif change == "duplicates":
                snapshot["answers"]["useNext"] *= 2
            else:
                snapshot["accounts"].append(dict(snapshot["accounts"][1]))
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, error):
                account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)

    def test_explicit_account_overrides_ranking_but_needs_snapshot_receipt_id(self):
        choice = account_choice.choose("anthropic", accounts_config(), self.snapshot(), None, now=NOW, name="main")
        self.assertEqual((choice["name"], choice["source"], choice["account_id"]), ("main", "named", "anthropic-abc"))
        with self.assertRaisesRegex(ValueError, "snapshot unavailable"):
            account_choice.choose("anthropic", accounts_config(), None, "reader failed", name="main")
        with self.assertRaisesRegex(ValueError, "named missing"):
            account_choice.choose("anthropic", accounts_config(), self.snapshot(), None, name="missing")

    def test_configuration_errors_and_plain_reset_lines_reported(self):
        snapshot = self.snapshot()
        snapshot["configurationErrors"] = ["multipleUseLast:anthropic"]
        snapshot["resetPrompts"] = [{"provider": "anthropic", "accountID": "anthropic-abc", "line": "Apply main's reset now"}]
        choice = choose_from(snapshot["accounts"], snapshot["answers"]["useNext"])
        self.assertEqual(choice["name"], "spare")
        choice = account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)
        self.assertEqual(choice["configuration_errors"], snapshot["configurationErrors"])
        self.assertEqual(choice["reset_prompts"], ["Apply main's reset now"])
        snapshot["answers"]["useNext"] = []
        with self.assertRaisesRegex(ValueError, "(?s)no anthropic choice.*multipleUseLast.*Apply main"):
            account_choice.choose("anthropic", accounts_config(), snapshot, None, now=NOW)

    def test_snapshot_read_failures_mean_unavailable(self):
        for stdout, code, reason in (("{", 0, "not JSON"), ('{"schemaVersion":2}', 0, "version 1"), ("", 1, "exited 1")):
            with self.subTest(reason=reason):
                snapshot, problem = account_choice.read_snapshot(["reader"], runner=Mock(return_value=SimpleNamespace(stdout=stdout, returncode=code)))
                self.assertIsNone(snapshot)
                self.assertIn(reason, problem)
        snapshot, problem = account_choice.read_snapshot(["reader"], runner=Mock(side_effect=FileNotFoundError()))
        self.assertIsNone(snapshot)
        self.assertIn("could not run", problem)

    def test_private_config_location_legacy_reserves_ignored_and_env_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "skill-data/supervisor.toml"
            path.parent.mkdir()
            path.write_text(ACCOUNTS_TOML)
            config = account_choice.load_config(env={"CODE_HOME": folder}, home=Path("/nowhere"))
            self.assertEqual([a["name"] for a in config["accounts"]], ["main", "spare"])
            self.assertTrue(all("reserve" not in a for a in config["accounts"]))
            for text, error in ((ACCOUNTS_TOML.replace('context_panel_label = "Main"', ''), "exactly one"),
                                (ACCOUNTS_TOML.replace('provider = "anthropic"', 'provider = "openai"'), "must set CODEX_HOME"),
                                (ACCOUNTS_TOML.replace('CLAUDE_CONFIG_DIR = "~/claude-main"', 'CLAUDE_CONFIG_DIR = false'), "env must map")):
                path.write_text(text)
                with self.assertRaisesRegex(ValueError, error):
                    account_choice.load_config(env={"CODE_HOME": folder}, home=Path("/nowhere"))

    def test_default_claude_profile_has_no_env_values(self):
        config = accounts_config(ACCOUNTS_TOML.replace('env = { CLAUDE_CONFIG_DIR = "~/claude-main" }', 'env = {}'))
        for name in (None, "main"):
            snapshot = self.snapshot()
            snapshot["answers"]["useNext"][0]["accountID"] = "anthropic-abc"
            choice = account_choice.choose("anthropic", config, snapshot, None, name=name)
            self.assertEqual(account_choice.public(choice)["env_keys"], [])

    def test_select_preview_and_receipts_use_readers_storage_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = accounts_config()
            config["snapshot_command"] = ["reader", "--storage-root", str(root)]
            with patch.object(account_choice, "load_config", return_value=config), patch.object(account_choice, "read_snapshot", return_value=(self.snapshot(), None)):
                choice = account_choice.select("anthropic")
            self.assertEqual(list(root.iterdir()), [])  # preview makes no receipt
            path = account_choice.record_launch(choice, NOW)
            receipt = json.loads(path.read_text())
            self.assertEqual(receipt, {"schemaVersion": 1, "provider": "anthropic", "accountID": "anthropic-def", "launchedAt": "2026-10-03T16:00:00Z"})
            self.assertEqual(path.parent, root / "Launch Receipts")
            self.assertLess(path.stat().st_size, 1024)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            self.assertNotIn(str(root), json.dumps(account_choice.public(choice)))

    def test_atomic_receipt_and_pruning_preserve_other_writers_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            choice = account_choice.choose("anthropic", accounts_config(), self.snapshot(), None)
            choice["storage_root"] = Path(folder)
            old = account_choice.record_launch(choice, NOW - timedelta(days=1))
            directory = old.parent
            other = directory / "20261001T160000Z-other.json"
            other.write_bytes(old.read_bytes())
            symlink = directory / "20261001T160000Z-supervisor-11111111111111111111111111111111.json"
            symlink.symlink_to(other)
            recent = account_choice.record_launch(choice, NOW - timedelta(hours=23))
            self.assertTrue(old.exists())
            original_replace = account_choice.os.replace
            observed = []
            def replace(source, target):
                self.assertEqual(source.parent, target.parent)
                self.assertFalse(target.exists())
                self.assertEqual(json.loads(source.read_text())["accountID"], "anthropic-def")
                observed.append(target)
                original_replace(source, target)
            with patch.object(account_choice.os, "replace", side_effect=replace):
                account_choice.record_launch(choice, NOW)
            self.assertEqual(len(observed), 1)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(other.exists())
            self.assertTrue(symlink.is_symlink())

    def test_receipt_rename_failure_leaves_no_partial_json_or_temp_file(self):
        with tempfile.TemporaryDirectory() as folder:
            choice = account_choice.choose("anthropic", accounts_config(), self.snapshot(), None)
            choice["storage_root"] = Path(folder)
            with patch.object(account_choice.os, "replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
                account_choice.record_launch(choice, NOW)
            self.assertEqual(list((Path(folder) / "Launch Receipts").iterdir()), [])


class TerminalTests(unittest.TestCase):
    def test_default_claude_launch_clears_inherited_profile_after_cd(self):
        config = accounts_config(ACCOUNTS_TOML.replace(
            'env = { CLAUDE_CONFIG_DIR = "~/claude-main" }', 'env = {}'
        ))
        choice = account_choice.choose("anthropic", config, AccountChoiceTests().snapshot(), None, name="main")
        with tempfile.TemporaryDirectory() as folder:
            fake_claude = Path(folder) / "claude"
            fake_claude.write_text('#!/bin/sh\nprintf "%s\\n" "${CLAUDE_CONFIG_DIR+set}" "$1"\n')
            fake_claude.chmod(0o700)
            command = iterm_tab.with_account(f'cd / && {shlex.quote(str(fake_claude))} brief', choice)
            launched = subprocess.run(
                ["/bin/sh", "-c", command], capture_output=True, text=True, check=True,
                env={**os.environ, "CLAUDE_CONFIG_DIR": "/inherited/alternate"},
            )
            self.assertEqual(launched.stdout.splitlines(), ["", "brief"])
        with self.assertRaisesRegex(ValueError, "already sets CLAUDE_CONFIG_DIR"):
            iterm_tab.with_account("env CLAUDE_CONFIG_DIR=/other claude", choice)

    def test_exact_session_and_ambiguity(self):
        one = SimpleNamespace(session_id="id1")
        two = SimpleNamespace(session_id="id2")
        app = SimpleNamespace(
            terminal_windows=[
                SimpleNamespace(tabs=[SimpleNamespace(sessions=[one, two])])
            ]
        )
        self.assertIs(iterm_tab.select_session(app, "id1"), one)
        with self.assertRaises(ValueError):
            iterm_tab.select_session(app, "id")
        two.session_id = "id1"
        with self.assertRaises(ValueError):
            iterm_tab.select_session(app, "id1")

    def test_send_separate_return_and_broadcast_suppression(self):
        terminal = SimpleNamespace(async_send_text=AsyncMock())
        asyncio.run(iterm_tab.send(terminal, "brief"))
        self.assertEqual(terminal.async_send_text.await_args_list[0].args, ("brief",))
        self.assertEqual(terminal.async_send_text.await_args_list[1].args, ("\r",))
        self.assertTrue(
            all(
                call.kwargs["suppress_broadcast"]
                for call in terminal.async_send_text.await_args_list
            )
        )
        with self.assertRaises(ValueError):
            asyncio.run(iterm_tab.send(terminal, "\x1bunsafe"))

    def test_multiline_message_uses_bracketed_paste(self):
        terminal = SimpleNamespace(async_send_text=AsyncMock())
        asyncio.run(iterm_tab.send(terminal, "two\nlines"))
        self.assertEqual(
            terminal.async_send_text.await_args_list[0].args,
            ("\x1b[200~two\nlines\x1b[201~",),
        )

    def test_launch_explicit_window_without_moving_focus(self):
        terminal = SimpleNamespace(session_id="new", async_send_text=AsyncMock())
        tab = SimpleNamespace(tab_id="newtab", current_session=terminal)
        previous = SimpleNamespace(async_select=AsyncMock())
        window = SimpleNamespace(
            window_id="chosen", async_create_tab=AsyncMock(return_value=tab)
        )
        app = SimpleNamespace(
            terminal_windows=[window],
            current_terminal_window=SimpleNamespace(current_tab=previous),
        )
        with tempfile.TemporaryDirectory() as folder:
            command = Path(folder) / "launch.txt"
            command.write_text("run-authorized-brief\n")
            args = argparse.Namespace(
                command="new", window_id="chosen", command_file=command
            )
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}):
                result = asyncio.run(iterm_tab.operate(app, args))
        self.assertEqual(result["session_id"], "new")
        window.async_create_tab.assert_awaited_once_with(select=False)
        previous.async_select.assert_not_awaited()
        self.assertEqual(
            terminal.async_send_text.await_args_list[0].args, ("run-authorized-brief",)
        )

    def test_late_session_launches_only_in_refreshed_created_tab(self):
        terminal = SimpleNamespace(session_id="late", async_send_text=AsyncMock())
        created = SimpleNamespace(tab_id="newtab", current_session=None)
        # iTerm can replace hierarchy objects during a refresh.
        refreshed = SimpleNamespace(tab_id="newtab", current_session=terminal)
        previous = SimpleNamespace(async_select=AsyncMock(), async_send_text=AsyncMock())
        window = SimpleNamespace(
            window_id="chosen", async_create_tab=AsyncMock(return_value=created)
        )
        app = SimpleNamespace(
            terminal_windows=[window],
            current_terminal_window=SimpleNamespace(current_tab=previous),
            async_refresh=AsyncMock(),
            get_tab_by_id=Mock(side_effect=[created, refreshed]),
        )
        with tempfile.TemporaryDirectory() as folder:
            command = Path(folder) / "launch.txt"
            command.write_text("run-authorized-brief\n")
            args = argparse.Namespace(command="new", window_id="chosen", command_file=command)
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}):
                result = asyncio.run(iterm_tab.operate(app, args))
        self.assertEqual(result["session_id"], "late")
        self.assertEqual(app.async_refresh.await_count, 2)
        self.assertTrue(all(call.args == ("newtab",) for call in app.get_tab_by_id.call_args_list))
        window.async_create_tab.assert_awaited_once_with(select=False)
        previous.async_select.assert_not_awaited()
        previous.async_send_text.assert_not_awaited()
        self.assertEqual(
            [call.args for call in terminal.async_send_text.await_args_list],
            [("run-authorized-brief",), ("\r",)],
        )

    def test_session_wait_timeout_covers_stalled_refresh(self):
        async def stalled_refresh():
            await asyncio.Future()

        tab = SimpleNamespace(tab_id="newtab", current_session=None)
        for refresh in (AsyncMock(), AsyncMock(side_effect=stalled_refresh)):
            with self.subTest(stalled=bool(refresh.side_effect)):
                app = SimpleNamespace(
                    async_refresh=refresh,
                    get_tab_by_id=Mock(return_value=tab),
                )
                with self.assertRaisesRegex(ValueError, "newtab.*no session after"):
                    asyncio.run(iterm_tab.wait_for_session(app, tab, timeout=0.01))

    def test_launch_timeout_never_sends_or_moves_focus(self):
        previous = SimpleNamespace(async_select=AsyncMock(), async_send_text=AsyncMock())
        tab = SimpleNamespace(tab_id="newtab", current_session=None)
        window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock(return_value=tab))
        app = SimpleNamespace(
            terminal_windows=[window],
            current_terminal_window=SimpleNamespace(current_tab=previous),
            async_refresh=AsyncMock(),
            get_tab_by_id=Mock(return_value=tab),
        )
        wait = iterm_tab.wait_for_session

        async def short_wait(target_app, target_tab):
            return await wait(target_app, target_tab, timeout=0.01)

        with tempfile.TemporaryDirectory() as folder:
            command = Path(folder) / "launch.txt"
            command.write_text("run-authorized-brief\n")
            args = argparse.Namespace(command="new", window_id="chosen", command_file=command)
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.object(iterm_tab, "wait_for_session", side_effect=short_wait),
                patch.object(iterm_tab, "send", new_callable=AsyncMock) as send,
                self.assertRaisesRegex(ValueError, "newtab.*no session after"),
            ):
                asyncio.run(iterm_tab.operate(app, args))
            send.assert_not_awaited()
        window.async_create_tab.assert_awaited_once_with(select=False)
        previous.async_select.assert_not_awaited()
        previous.async_send_text.assert_not_awaited()

    def test_closed_new_tab_does_not_fall_back_to_another_session(self):
        terminal = SimpleNamespace(async_send_text=AsyncMock())
        app = SimpleNamespace(
            async_refresh=AsyncMock(),
            get_tab_by_id=Mock(return_value=None),
            current_terminal_window=SimpleNamespace(current_tab=SimpleNamespace(current_session=terminal)),
        )
        for tab in (None, SimpleNamespace(tab_id="closed", current_session=None)):
            with self.subTest(tab=tab), self.assertRaises(ValueError):
                asyncio.run(iterm_tab.wait_for_session(app, tab))
        terminal.async_send_text.assert_not_awaited()

    def test_codex_remote_launch_inherits_selected_home_after_cd(self):
        rows = [account_row("main", 0.6, []), account_row("spare", 0.5, [], cfg="cfg-spare")]
        for row in rows:
            row["provider"] = "openai"
            row["id"] = row["id"].replace("anthropic", "openai")
        config = accounts_config(ACCOUNTS_TOML.replace("anthropic", "openai").replace("CLAUDE_CONFIG_DIR", "CODEX_HOME"))
        choice = account_choice.choose("openai", config, {
            "accounts": rows,
            "answers": {"useNext": [{"provider": "openai", "accountID": "openai-def"}]},
        }, None, now=NOW)
        # Stand in for the CLI without reading auth or starting a real session.
        with tempfile.TemporaryDirectory() as folder:
            fake_codex = Path(folder) / "codex"
            fake_codex.write_text('#!/bin/sh\nprintf "%s\\n" "$CODEX_HOME" "$1" "$2"\n')
            fake_codex.chmod(0o700)
            command = iterm_tab.with_account(f"cd / && {shlex.quote(str(fake_codex))} --remote unix://", choice)
            launched = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, check=True)
        self.assertEqual(launched.stdout.splitlines(), [choice["env"]["CODEX_HOME"], "--remote", "unix://"])

    def test_launch_on_chosen_account_prefixes_env_only(self):
        terminal = SimpleNamespace(session_id="new", async_send_text=AsyncMock())
        tab = SimpleNamespace(tab_id="newtab", current_session=terminal)
        window = SimpleNamespace(
            window_id="chosen", async_create_tab=AsyncMock(return_value=tab)
        )
        app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
        choice = account_choice.decision(
            accounts_config()["accounts"][1], "context-panel", "resets soonest"
        )
        with tempfile.TemporaryDirectory() as folder:
            command = Path(folder) / "launch.txt"
            command.write_text("cd /repo && claude 'brief'\n")
            args = argparse.Namespace(
                command="new", window_id="chosen", command_file=command,
                account_provider="anthropic", account=None, account_config=None,
            )
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.object(account_choice, "select", return_value=choice),
                patch.object(account_choice, "record_launch") as receipt,
                patch.object(account_choice, "prepare_launch"),
            ):
                result = asyncio.run(iterm_tab.operate(app, args))
                command.write_text("env CLAUDE_CONFIG_DIR=/other claude\n")
                with self.assertRaisesRegex(ValueError, "already sets"):
                    asyncio.run(iterm_tab.operate(app, args))
        self.assertEqual(
            terminal.async_send_text.await_args_list[0].args,
            ("export CLAUDE_CONFIG_DIR='/accounts/spare dir' && cd /repo && claude 'brief'",),
        )
        window.async_create_tab.assert_awaited_once_with(select=False)
        receipt.assert_called_once_with(choice)
        self.assertEqual(result["account"]["env_keys"], ["CLAUDE_CONFIG_DIR"])
        self.assertNotIn("env", result["account"])

    def test_batch_creates_one_count_only_receipt_before_each_command(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = [root / "first.txt", root / "second.txt"]
            for index, path in enumerate(files):
                path.write_text(f"claude brief-{index}")
            snapshot = AccountChoiceTests().snapshot()
            config = accounts_config()
            config["snapshot_command"] = ["reader", "--storage-root", str(root)]
            counts_at_send = []
            async def submit(_text, **_kwargs):
                counts_at_send.append(len(list((root / "Launch Receipts").glob("*.json"))))
            terminals = [SimpleNamespace(session_id=f"session-{i}", async_send_text=AsyncMock(side_effect=submit)) for i in range(2)]
            tabs = [SimpleNamespace(tab_id=f"tab-{i}", current_session=t) for i, t in enumerate(terminals)]
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock(side_effect=tabs))
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = argparse.Namespace(command="new", window_id="chosen", command_file=files,
                                      account_provider="anthropic", account=None, account_config=None)
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), patch.object(account_choice, "load_config", return_value=config), patch.object(account_choice, "read_snapshot", return_value=(snapshot, None)):
                results = asyncio.run(iterm_tab.operate(app, args))
            self.assertEqual([r["account"]["name"] for r in results], ["spare", "main"])
            self.assertEqual(counts_at_send, [1, 1, 2, 2])
            receipts = [json.loads(p.read_text()) for p in (root / "Launch Receipts").glob("*.json")]
            self.assertCountEqual([r["accountID"] for r in receipts], ["anthropic-def", "anthropic-abc"])
            for receipt in receipts:
                self.assertEqual(set(receipt), {"schemaVersion", "provider", "accountID", "launchedAt"})

    def test_new_cli_requires_provider_before_connecting_to_iterm(self):
        fake = SimpleNamespace(run_until_complete=Mock())
        with patch.dict("sys.modules", {"iterm2": fake}), patch.object(__import__("sys"), "argv", ["iterm_tab.py", "new", "--window-id", "window", "--command-file", "launch.txt"]):
            with self.assertRaises(SystemExit) as failed:
                iterm_tab.main()
        self.assertEqual(failed.exception.code, 2)
        fake.run_until_complete.assert_not_called()

    def test_account_setters_reject_override_but_briefs_and_other_variables_work(self):
        choice = account_choice.decision(accounts_config()["accounts"][1], "context-panel", "test")
        for command in ("CLAUDE_CONFIG_DIR=/other claude brief", "cd / && env CLAUDE_CONFIG_DIR=/other claude brief",
                        "export CLAUDE_CONFIG_DIR=/other && claude brief", "declare -x CLAUDE_CONFIG_DIR=/other; claude brief",
                        "unset CLAUDE_CONFIG_DIR && claude brief", "env -u CLAUDE_CONFIG_DIR claude brief",
                        "env --unset=CLAUDE_CONFIG_DIR claude brief", "env -uCLAUDE_CONFIG_DIR claude brief",
                        "exec env CLAUDE_CONFIG_DIR=/other claude brief", "command env CLAUDE_CONFIG_DIR=/other claude brief",
                        "time CLAUDE_CONFIG_DIR=/other claude brief", "{ CLAUDE_CONFIG_DIR=/other claude brief; }",
                        "zsh -lc 'CLAUDE_CONFIG_DIR=/other claude brief'", "eval 'CLAUDE_CONFIG_DIR=/other claude brief'",
                        "env -P /usr/bin CLAUDE_CONFIG_DIR=/other claude brief", "env -S 'CLAUDE_CONFIG_DIR=/other claude brief'",
                        "env -i claude brief", "env - claude brief", "readonly CLAUDE_CONFIG_DIR=/other; claude brief",
                        "CLAUDE_CONFIG_DIR+=/other claude brief"):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "already sets CLAUDE_CONFIG_DIR"):
                iterm_tab.with_account(command, choice)
        with self.assertRaisesRegex(ValueError, "already sets CODEX_HOME"):
            iterm_tab.with_account("CODEX_HOME=/manual codex brief", choice)
        command = "MY_CLAUDE_CONFIG_DIR=example claude 'Discuss CLAUDE_CONFIG_DIR=/example and CODEX_HOME=/example'"
        prepared = iterm_tab.with_account(command, choice)
        with tempfile.TemporaryDirectory() as folder:
            cli = Path(folder) / "claude"
            cli.write_text('#!/bin/sh\nprintf "%s\\n" "$CLAUDE_CONFIG_DIR" "$1"\n')
            cli.chmod(0o700)
            result = subprocess.run(["/bin/sh", "-c", prepared], env={**os.environ, "PATH": folder + ":" + os.environ["PATH"]}, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.splitlines(), [choice["env"]["CLAUDE_CONFIG_DIR"], "Discuss CLAUDE_CONFIG_DIR=/example and CODEX_HOME=/example"])
        with tempfile.TemporaryDirectory() as folder:
            cli = Path(folder) / "claude"
            cli.write_text('#!/bin/sh\nprintf "%s\\n" "$CLAUDE_CONFIG_DIR" "$1"\n')
            cli.chmod(0o700)
            brief = Path(folder) / "brief.txt"
            content = "Don't change CLAUDE_CONFIG_DIR=/example; discuss the setting."
            brief.write_text(content)
            prepared = iterm_tab.with_account(f'claude "$(cat {shlex.quote(str(brief))})"', choice)
            result = subprocess.run(["/bin/sh", "-c", prepared], env={**os.environ, "PATH": folder + ":" + os.environ["PATH"]}, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(), [choice["env"]["CLAUDE_CONFIG_DIR"], content])

    def test_manual_account_environment_without_provider_refuses_before_tabs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launch.txt"
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = argparse.Namespace(command="new", window_id="chosen", command_file=path,
                                      account_provider=None, account=None, account_config=None)
            for command in ("env CODEX_HOME=/manual codex brief", "export CLAUDE_CONFIG_DIR=/manual && claude brief"):
                path.write_text(command)
                with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), self.assertRaisesRegex(ValueError, "account settings need --account-provider"):
                    asyncio.run(iterm_tab.operate(app, args))
            window.async_create_tab.assert_not_awaited()

    def test_unwritable_receipt_root_refuses_before_any_tab_and_creates_no_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "launch.txt"
            path.write_text("claude brief")
            choice = account_choice.choose("anthropic", accounts_config(), AccountChoiceTests().snapshot(), None)
            choice["storage_root"] = root / "missing-store"
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = argparse.Namespace(command="new", window_id="chosen", command_file=path,
                                      account_provider="anthropic", account=None, account_config=None)
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), patch.object(account_choice, "select", return_value=choice):
                with self.assertRaisesRegex(ValueError, "check write access") as failed:
                    asyncio.run(iterm_tab.operate(app, args))
            window.async_create_tab.assert_not_awaited()
            self.assertNotIn(str(root), str(failed.exception))
            self.assertFalse(choice["storage_root"].exists())
            choice["storage_root"] = root
            account_choice.prepare_launch(choice)
            self.assertEqual(list((root / "Launch Receipts").iterdir()), [])
            with patch.object(account_choice.os, "fsync", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(ValueError, "cannot prepare"):
                    account_choice.prepare_launch(choice)
            self.assertEqual(list((root / "Launch Receipts").iterdir()), [])

    def test_partial_batch_failure_preserves_started_and_failed_launch_identities(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = [root / "first.txt", root / "second.txt"]
            for path in files:
                path.write_text("claude brief")
            choices = account_choice.choose_batch("anthropic", accounts_config(), AccountChoiceTests().snapshot(), None, count=2)
            for choice in choices:
                choice["storage_root"] = root
            terminals = [SimpleNamespace(session_id=f"session-{i}", async_send_text=AsyncMock()) for i in range(2)]
            tabs = [SimpleNamespace(tab_id=f"tab-{i}", current_session=t) for i, t in enumerate(terminals)]
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock(side_effect=tabs))
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = argparse.Namespace(command="new", window_id="chosen", command_file=files,
                                      account_provider="anthropic", account=None, account_config=None)
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), patch.object(account_choice, "select_batch", return_value=choices), patch.object(account_choice, "record_launch", side_effect=[root / "receipt.json", PermissionError("private-root")]):
                try:
                    asyncio.run(iterm_tab.operate(app, args))
                    self.fail("second launch must fail")
                except (ValueError, OSError) as error:
                    detail = getattr(error, "detail", {})
            self.assertEqual(detail["launched"][0]["session_id"], "session-0")
            self.assertEqual(detail["failed_launch"]["tab_id"], "tab-1")
            self.assertFalse(detail["failed_launch"]["receipt_written"])
            self.assertEqual(detail["failed_launch"]["submission"], "not_attempted")
            self.assertNotIn("private-root", json.dumps(detail))
            terminals[1].async_send_text.assert_not_awaited()

    def test_receipt_failure_prevents_command_submission(self):
        terminal = SimpleNamespace(session_id="new", async_send_text=AsyncMock())
        window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock(return_value=SimpleNamespace(tab_id="tab", current_session=terminal)))
        app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
        choice = account_choice.choose("anthropic", accounts_config(), AccountChoiceTests().snapshot(), None)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launch.txt"
            path.write_text("claude brief")
            args = argparse.Namespace(command="new", window_id="chosen", command_file=path,
                                      account_provider="anthropic", account=None, account_config=None)
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), patch.object(account_choice, "select", return_value=choice), patch.object(account_choice, "record_launch", side_effect=OSError("disk full")), patch.object(account_choice, "prepare_launch"), self.assertRaises(iterm_tab.LaunchFailure):
                asyncio.run(iterm_tab.operate(app, args))
            terminal.async_send_text.assert_not_awaited()

    def test_uncertain_submit_retains_receipt_and_reports_exact_tab(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "launch.txt"
            path.write_text("claude brief")
            choice = account_choice.choose("anthropic", accounts_config(), AccountChoiceTests().snapshot(), None)
            choice["storage_root"] = root
            terminal = SimpleNamespace(session_id="session", async_send_text=AsyncMock(side_effect=Exception("private RPC payload")))
            tab = SimpleNamespace(tab_id="tab", current_session=terminal)
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock(return_value=tab))
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            args = argparse.Namespace(command="new", window_id="chosen", command_file=path,
                                      account_provider="anthropic", account=None, account_config=None)
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), patch.object(account_choice, "select", return_value=choice):
                with self.assertRaises(iterm_tab.LaunchFailure) as failed:
                    asyncio.run(iterm_tab.operate(app, args))
            self.assertEqual(len(list((root / "Launch Receipts").glob("*.json"))), 1)
            self.assertEqual(failed.exception.detail["failed_launch"]["submission"], "unknown")
            self.assertTrue(failed.exception.detail["failed_launch"]["receipt_written"])
            self.assertEqual(failed.exception.detail["failed_launch"]["tab_id"], "tab")
            self.assertNotIn("private RPC payload", json.dumps(failed.exception.detail))

    def test_batch_refusal_and_bad_second_command_create_no_tabs_or_receipts(self):
        window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
        app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
        with tempfile.TemporaryDirectory() as folder:
            files = [Path(folder) / "first.txt", Path(folder) / "second.txt"]
            files[0].write_text("claude brief")
            files[1].write_text("env CLAUDE_CONFIG_DIR=/other claude")
            args = argparse.Namespace(command="new", window_id="chosen", command_file=files,
                                      account_provider="anthropic", account=None, account_config=None)
            choices = account_choice.choose_batch("anthropic", accounts_config(), AccountChoiceTests().snapshot(), None, count=2)
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                patch.object(account_choice, "select_batch", return_value=choices),
                patch.object(account_choice, "record_launch") as receipt,
                patch.object(account_choice, "prepare_launch"),
                self.assertRaisesRegex(ValueError, "already sets"),
            ):
                asyncio.run(iterm_tab.operate(app, args))
            window.async_create_tab.assert_not_awaited()
            receipt.assert_not_called()
            with patch.dict("sys.modules", {"iterm2": SimpleNamespace()}), patch.object(account_choice, "select_batch", side_effect=ValueError("no choice")), self.assertRaises(ValueError):
                asyncio.run(iterm_tab.operate(app, args))
            window.async_create_tab.assert_not_awaited()

    def test_invalid_launch_file_creates_no_tab(self):
        window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
        app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "launch.txt"
            path.write_text("bad\tcommand")
            args = argparse.Namespace(
                command="new", window_id="chosen", command_file=path
            )
            with (
                patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
                self.assertRaises(ValueError),
            ):
                asyncio.run(iterm_tab.operate(app, args))
        window.async_create_tab.assert_not_awaited()

    def test_unverified_send_refuses_without_keys(self):
        terminal = SimpleNamespace(session_id="term", async_send_text=AsyncMock())
        app = SimpleNamespace(
            terminal_windows=[
                SimpleNamespace(tabs=[SimpleNamespace(sessions=[terminal])])
            ]
        )
        args = argparse.Namespace(
            command="send", session_id="term", verified_target=False
        )
        with self.assertRaises(ValueError):
            asyncio.run(iterm_tab.operate(app, args))
        terminal.async_send_text.assert_not_awaited()

    def test_missing_window_refuses_without_launch(self):
        app = SimpleNamespace(terminal_windows=[], current_terminal_window=None)
        args = argparse.Namespace(command="new", window_id="missing")
        # Import is only needed by actual window creation, not target validation.
        with (
            patch.dict("sys.modules", {"iterm2": SimpleNamespace()}),
            self.assertRaises(ValueError),
        ):
            asyncio.run(iterm_tab.operate(app, args))

    def test_running_shell_script_is_not_an_idle_shell(self):
        entry = {"pid": 123, "tty": "ttys001"}
        for command in (
            "bash build.sh",
            "zsh -c 'while :; do :; done'",
            "/bin/sh /tmp/task.sh",
        ):
            with self.subTest(command=command):
                self.assertFalse(
                    close_ttys.process_exited(entry, [(20, "ttys001", command)])
                )
        self.assertTrue(
            close_ttys.process_exited(
                entry,
                [(20, "ttys001", "-zsh"), (21, "ttys001", "/usr/bin/login -fp user")],
            )
        )

    def test_missing_tty_rows_are_not_exit_proof(self):
        with self.assertRaises(ValueError):
            close_ttys.process_exited(
                {"pid": 123, "tty": "ttys001"}, [(20, "ttys999", "/bin/zsh")]
            )

    def test_process_inventory_refuses_unparseable_or_live_identity(self):
        self.assertEqual(
            close_ttys.process_rows("123 ttys001 /bin/claude\n"),
            [(123, "ttys001", "/bin/claude")],
        )
        with self.assertRaises(ValueError):
            close_ttys.process_rows("unexpected")
        with self.assertRaises(ValueError):
            close_ttys.process_exited({}, [])


if __name__ == "__main__":
    unittest.main()
