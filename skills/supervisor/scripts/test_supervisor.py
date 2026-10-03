#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise pilot parsers and terminal behavior with disposable transcripts/adapters."""

import argparse
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    return {
        "provider": "anthropic",
        "label": label,
        "configurationID": cfg,
        "state": state,
        "remainingFraction": remaining,
        "windows": [{"naturalResetAt": reset} for reset in resets],
    }


def accounts_config(text=ACCOUNTS_TOML):
    return account_choice.validate_config(
        __import__("tomllib").loads(text)["accounts"]
    )


def choose_from(rows, **extra):
    snapshot = {"schemaVersion": 1, "accounts": rows}
    return account_choice.choose(
        "anthropic", accounts_config(), snapshot, None, now=NOW, **extra
    )


class AccountChoiceTests(unittest.TestCase):

    def test_longest_window_reset_ranks_and_short_windows_do_not(self):
        rows = [
            # Main's five-hour window resets first, but its week ends later.
            account_row("main", 0.6, ["2026-10-03T17:00:00Z", "2026-10-07T08:00:00Z"]),
            account_row("Spare", 0.5, ["2026-10-03T19:00:00Z", "2026-10-04T03:00:00Z"], cfg="cfg-spare"),
        ]
        choice = choose_from(rows)
        self.assertEqual((choice["name"], choice["source"]), ("spare", "context-panel"))
        self.assertEqual(choice["resets_at"], "2026-10-04T03:00:00+00:00")
        self.assertEqual([o["name"] for o in choice["others"]], ["main"])

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

    def test_fallback_only_without_a_current_reading(self):
        unavailable = account_choice.choose(
            "anthropic", accounts_config(), None, "snapshot command exited 1", now=NOW
        )
        self.assertEqual((unavailable["name"], unavailable["source"]), ("main", "fallback"))
        self.assertIn("snapshot command exited 1", unavailable["reason"])
        unread = choose_from([account_row("main", None, [], state="unknown")])
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


class TerminalTests(unittest.TestCase):
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

    def test_launch_explicit_window_and_restore_focus(self):
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
        window.async_create_tab.assert_awaited_once()
        previous.async_select.assert_awaited_once()
        self.assertEqual(
            terminal.async_send_text.await_args_list[0].args, ("run-authorized-brief",)
        )

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
            command.write_text("claude 'brief'\n")
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
            ("env CLAUDE_CONFIG_DIR='/accounts/spare dir' claude 'brief'",),
        )
        window.async_create_tab.assert_awaited_once()
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
