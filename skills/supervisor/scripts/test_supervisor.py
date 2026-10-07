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
from datetime import datetime, timezone
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
        "id": "row-" + cfg,
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

    def test_context_panel_choice_overrides_weekly_ranking_for_each_provider(self):
        for provider in account_choice.PROVIDERS:
            with self.subTest(provider=provider):
                config = accounts_config()
                for account in config["accounts"]:
                    account["provider"] = provider
                rows = [
                    account_row("main", 0.6, ["2026-10-04T03:00:00Z"]),
                    account_row("spare", 0.5, ["2026-10-07T08:00:00Z"], cfg="cfg-spare"),
                ]
                for row, count in zip(rows, [1, 2]):
                    row["provider"] = provider
                    row["bankedResets"] = {"summary": {
                        "availableCount": count,
                        "knownExpiries": ["2026-10-05T03:00:00Z", "2026-10-06T03:00:00Z"][:count],
                    }}
                snapshot = {"accounts": rows, "answers": {"useNext": [
                    {"provider": other, "accountID": "irrelevant"}
                    for other in account_choice.PROVIDERS if other != provider
                ] + [{"provider": provider, "accountID": "row-cfg-spare"}]}}
                choice = account_choice.choose(provider, config, snapshot, None, now=NOW)
                self.assertEqual((choice["name"], choice["source"]), ("spare", "context-panel"))
                self.assertIn("Context Panel use next", choice["reason"])
                # Label-based configuration must resolve the same snapshot ID too.
                snapshot["answers"]["useNext"][-1]["accountID"] = "row-cfg"
                rows[0]["useLast"] = True  # The consumer must not re-rank the app's answer.
                self.assertEqual(account_choice.choose(provider, config, snapshot, None, now=NOW)["name"], "main")

    def test_missing_provider_choice_reports_previous_ranking_as_fallback(self):
        rows = [
            account_row("main", 0.6, ["2026-10-07T08:00:00Z"]),
            account_row("spare", 0.5, ["2026-10-04T03:00:00Z"], cfg="cfg-spare"),
        ]
        for recommendations in (None, [], [{"provider": "openai", "accountID": "other"}]):
            with self.subTest(recommendations=recommendations):
                choice = choose_from(rows, recommendations=recommendations)
                self.assertEqual((choice["name"], choice["source"]), ("spare", "fallback"))
                self.assertIn("no anthropic use-next choice", choice["reason"])
                self.assertIn("soonest reset with room", choice["reason"])

    def test_unlaunchable_context_panel_choice_refuses_without_substitution(self):
        rows = [account_row("main", 0.6, []), account_row("spare", 0.5, [], cfg="cfg-spare")]
        recommendation = [{"provider": "anthropic", "accountID": "row-cfg-spare"}]
        rows[1]["remainingFraction"] = 0.01
        with self.assertRaisesRegex(ValueError, "use-next account cannot be launched"):
            choose_from(rows, recommendations=recommendation)
        rows[1].update(remainingFraction=None, state="stale")
        with self.assertRaisesRegex(ValueError, "use-next account cannot be launched"):
            choose_from(rows, recommendations=recommendation)
        with self.assertRaisesRegex(ValueError, "no single account row"):
            choose_from(rows, recommendations=[{"provider": "anthropic", "accountID": "missing"}])
        rows[1].update(configurationID="unconfigured", id="row-unconfigured")
        with self.assertRaisesRegex(ValueError, "no single configured account"):
            choose_from(rows, recommendations=[{"provider": "anthropic", "accountID": "row-unconfigured"}])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            choose_from(rows, recommendations=recommendation * 2)

    def test_weekly_reset_ranks_and_five_hour_windows_do_not(self):
        rows = [
            # Main's five-hour window resets first, but its week ends later.
            account_row("main", 0.6, ["2026-10-03T17:00:00Z", "2026-10-07T08:00:00Z"]),
            account_row("Spare", 0.5, ["2026-10-03T19:00:00Z", "2026-10-04T03:00:00Z"], cfg="cfg-spare"),
        ]
        choice = choose_from(rows)
        self.assertEqual((choice["name"], choice["source"]), ("spare", "fallback"))
        self.assertEqual(choice["resets_at"], "2026-10-04T03:00:00+00:00")
        self.assertEqual([o["name"] for o in choice["others"]], ["main"])
        # Near the end of a week the five-hour window outlasts it; the week still ranks.
        rows = [
            account_row("main", 0.6, ["2026-10-03T20:00:00Z", "2026-10-03T17:00:00Z"]),
            account_row("spare", 0.5, ["2026-10-03T18:30:00Z", "2026-10-03T18:00:00Z"], cfg="cfg-spare"),
        ]
        self.assertEqual(choose_from(rows)["name"], "main")

    def test_reserve_and_stale_readings_are_skipped(self):
        rows = [
            account_row("main", 0.6, ["2026-10-07T08:00:00Z"]),
            account_row("spare", 0.04, ["2026-10-04T03:00:00Z"], cfg="cfg-spare"),
        ]
        self.assertEqual(choose_from(rows)["name"], "main")
        rows[0]["remainingFraction"] = 0.15  # below main's own 20% reserve
        with self.assertRaisesRegex(ValueError, "main: 15% left.*spare: 4% left"):
            choose_from(rows)
        rows[0].update(remainingFraction=0.9, state="stale")
        with self.assertRaisesRegex(ValueError, "no current reading"):
            choose_from(rows)

    def test_known_readings_without_room_refuse_rather_than_fall_back(self):
        exhausted = [
            account_row("main", None, [], state="limited"),
            account_row("spare", None, [], state="limited", cfg="cfg-spare"),
        ]
        with self.assertRaisesRegex(ValueError, "main: no room.*spare: no room"):
            choose_from(exhausted)
        ambiguous = [account_row("main", 0.9, []), account_row("Main", 0.9, [])]
        with self.assertRaisesRegex(ValueError, "2 Context Panel rows match"):
            choose_from(ambiguous)

    def test_fallback_only_without_a_current_reading(self):
        unavailable = account_choice.choose(
            "anthropic", accounts_config(), None, "snapshot command exited 1", now=NOW
        )
        self.assertEqual((unavailable["name"], unavailable["source"]), ("main", "fallback"))
        self.assertIn("snapshot command exited 1", unavailable["reason"])
        unread = choose_from([
            account_row("main", None, [], state="unknown"),
            account_row("spare", None, [], state="stale", cfg="cfg-spare"),
        ])
        self.assertEqual((unread["name"], unread["source"]), ("main", "fallback"))
        named = choose_from([], name="spare")
        self.assertEqual((named["name"], named["source"]), ("spare", "named"))
        with self.assertRaises(ValueError):
            choose_from([], name="missing")

    def test_snapshot_read_failures_mean_unavailable(self):
        def runner(code=0, stdout="", error=None):
            def fake_run(*_args, **_kwargs):
                if error:
                    raise error
                return SimpleNamespace(returncode=code, stdout=stdout)
            return fake_run

        cases = {
            "exited 1": runner(code=1),
            "not JSON": runner(stdout="{"),
            "version 1": runner(stdout='{"schemaVersion": 2, "accounts": []}'),
            "could not run": runner(error=FileNotFoundError()),
        }
        for reason, fake in cases.items():
            with self.subTest(reason=reason):
                snapshot, problem = account_choice.read_snapshot(["reader"], runner=fake)
                self.assertIsNone(snapshot)
                self.assertIn(reason, problem)
        snapshot, problem = account_choice.read_snapshot(
            ["reader"], runner=runner(stdout='{"schemaVersion": 1, "accounts": []}')
        )
        self.assertEqual((snapshot["accounts"], problem), ([], None))

    def test_private_config_location_and_invalid_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "skill-data/supervisor.toml"
            path.parent.mkdir()
            path.write_text(ACCOUNTS_TOML)
            config = account_choice.load_config(env={"CODE_HOME": folder}, home=Path("/nowhere"))
            self.assertEqual([a["reserve"] for a in config["accounts"]], [0.2, 0.05])
            path.write_text(ACCOUNTS_TOML.replace('context_panel_label = "Main"', ""))
            with self.assertRaisesRegex(ValueError, "exactly one"):
                account_choice.load_config(env={"CODE_HOME": folder}, home=Path("/nowhere"))
            # Codex still requires an explicit account home.
            path.write_text(ACCOUNTS_TOML.replace('provider = "anthropic"', 'provider = "openai"'))
            with self.assertRaisesRegex(ValueError, "must set CODEX_HOME"):
                account_choice.load_config(env={"CODE_HOME": folder}, home=Path("/nowhere"))

    def test_default_claude_profile_can_be_selected_by_use_next_or_name(self):
        config = accounts_config(ACCOUNTS_TOML.replace(
            'env = { CLAUDE_CONFIG_DIR = "~/claude-main" }', 'env = {}'
        ))
        rows = [account_row("Main", 0.6, []), account_row("spare", 0.5, [], cfg="cfg-spare")]
        snapshot = {"accounts": rows, "answers": {"useNext": [
            {"provider": "anthropic", "accountID": "row-cfg"}
        ]}}
        for name in (None, "main"):
            with self.subTest(name=name):
                choice = account_choice.choose("anthropic", config, snapshot, None, now=NOW, name=name)
                self.assertEqual(choice["name"], "main")
                self.assertEqual(choice["env"], {})
                self.assertEqual(account_choice.public(choice)["env_keys"], [])

    def test_default_profile_does_not_allow_invalid_env_values(self):
        for env in ('{ CLAUDE_CONFIG_DIR = "" }', '{ CLAUDE_CONFIG_DIR = false }', '[]'):
            with self.subTest(env=env), self.assertRaisesRegex(ValueError, "env must map"):
                accounts_config(ACCOUNTS_TOML.replace(
                    'env = { CLAUDE_CONFIG_DIR = "~/claude-main" }', f'env = {env}'
                ))


class TerminalTests(unittest.TestCase):
    def test_default_claude_launch_clears_inherited_profile_after_cd(self):
        config = accounts_config(ACCOUNTS_TOML.replace(
            'env = { CLAUDE_CONFIG_DIR = "~/claude-main" }', 'env = {}'
        ))
        choice = account_choice.choose("anthropic", config, None, None, name="main")
        with tempfile.TemporaryDirectory() as folder:
            fake_claude = Path(folder) / "claude"
            fake_claude.write_text('#!/bin/sh\nprintf "%s\\n" "${CLAUDE_CONFIG_DIR+set}" "$1"\n')
            fake_claude.chmod(0o700)
            for launch_env in ({}, {"EXTRA_FLAG": "some value"}):
                with self.subTest(env=launch_env):
                    command = iterm_tab.with_account(
                        f'cd / && {shlex.quote(str(fake_claude))} brief', {**choice, "env": launch_env}
                    )
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
        config = accounts_config(ACCOUNTS_TOML.replace("anthropic", "openai").replace("CLAUDE_CONFIG_DIR", "CODEX_HOME"))
        choice = account_choice.choose("openai", config, {
            "accounts": rows,
            "answers": {"useNext": [{"provider": "openai", "accountID": "row-cfg-spare"}]},
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
        self.assertEqual(result["account"]["env_keys"], ["CLAUDE_CONFIG_DIR"])
        self.assertNotIn("env", result["account"])

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
