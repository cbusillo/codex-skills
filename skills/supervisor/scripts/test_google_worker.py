# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "websockets==17.3"]
# ///
import argparse
import asyncio
import json
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from unittest.mock import patch

import account_choice
import iterm_tab
import session_closeout
import session_liveness
import session_record


def varint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes([*result, value])


def protobuf(values: Mapping[int, int | bytes]) -> bytes:
    return b"".join(
        varint(key * 8) + varint(value) if isinstance(value, int) else varint(key * 8 + 2) + varint(len(value)) + value
        for key, value in values.items()
    )


class GoogleWorkerTests(unittest.TestCase):
    def test_idle_wal_conversation_passes_its_first_read(self) -> None:
        for reader in (session_record.session_status, session_record.posted_comments):
            with self.subTest(reader=reader.__name__), tempfile.TemporaryDirectory() as folder:
                session = "12345678-1234-1234-1234-123456789abc"
                transcript = Path(folder) / f"{session}.db"
                with sqlite3.connect(transcript) as database:
                    database.execute("PRAGMA journal_mode=WAL")
                    database.execute("CREATE TABLE trajectory_meta (cascade_id TEXT)")
                    database.execute("INSERT INTO trajectory_meta VALUES (?)", (session,))
                    database.execute(
                        "CREATE TABLE steps (idx INTEGER, step_type INTEGER, status INTEGER, has_subtrajectory INTEGER, step_payload BLOB)"
                    )
                    metadata = protobuf({1: protobuf({1: 1}), 8: protobuf({1: 1})})
                    response = protobuf({1: 15, 4: 3, 5: metadata, 20: protobuf({1: b"Safe to exit: yes"})})
                    database.execute("INSERT INTO steps VALUES (0,15,3,0,?)", (response,))
                database.close()
                entry = {"session_id": session, "harness": "agy", "transcript": str(transcript)}
                first = reader(entry)
                if reader is session_record.session_status:
                    self.assertTrue(first["safe_verdict"])
                    self.assertEqual(first["background_tasks"], [])
                else:
                    self.assertEqual(first, [])
                self.assertEqual(reader(entry), first)

    def test_native_error_requires_a_new_final_while_compaction_preserves_the_verdict(self) -> None:
        fields = iterm_tab.claim_history.protobuf_fields
        metadata = protobuf({1: protobuf({1: 1})})
        final = {"type": "assistant", "message": {"content": "Safe to exit: yes", "stop_reason": "end_turn"}}
        for kind, (status, children) in product((17, 23), ((3, False), (2, False), (3, True))):
            with self.subTest(kind=kind, status=status, children=children):
                payload = (
                    {24: protobuf({3: protobuf({2: b"API unavailable"})})}
                    if kind == 17
                    else {30: protobuf({5: b"<summary>context</summary>"})}
                )
                step = fields(protobuf({1: kind, 4: status, 5: metadata, **payload}))
                records = iterm_tab.claim_history.agy_step_records("session", 0, kind, status, children, step, {})
                summary = session_record.summarize([*records, final], "agy")
                self.assertEqual(summary["safe_verdict"], status == 3 and not children)
                self.assertEqual(bool(summary["background_tasks"]), status != 3 or children)
                self.assertEqual(
                    session_record.summarize([final, *records], "agy")["safe_verdict"], kind == 23 and status == 3 and not children
                )

    def test_finished_task_and_subagent_lists_do_not_count_as_background_work(self) -> None:
        fields = iterm_tab.claim_history.protobuf_fields
        final = {"type": "assistant", "message": {"content": "Safe to exit: yes", "stop_reason": "end_turn"}}
        for (tool, action), status in product(
            (("manage_task", "list"), ("manage_subagents", "list"), ("manage_subagents", "unverified")), (3, 2)
        ):
            with self.subTest(tool=tool, action=action, status=status):
                call = protobuf({1: b"call", 2: tool.encode(), 3: json.dumps({"Action": action}).encode()})
                metadata = protobuf({1: protobuf({1: 1}), 4: call})
                step = fields(protobuf({1: 132, 4: status, 5: metadata, 140: protobuf({2: protobuf({1: b"[]"})})}))
                records = iterm_tab.claim_history.agy_step_records("session", 0, 132, status, False, step, {})
                summary = session_record.summarize([*records, final], "agy")
                self.assertEqual(summary["safe_verdict"], status == 3 and action == "list")
                self.assertEqual(bool(summary["background_tasks"]), status != 3 or action != "list")

    def test_google_ledger_preparation_uses_the_existing_capacity_choice(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            command, brief = root / "command", root / "brief"
            command.write_text(f"cd {root} && exec agy --effort medium")
            brief.write_text("A delegated deletion. Rules: " + iterm_tab.BRIEF_RULES_URL)
            args = argparse.Namespace(
                command_file=[command],
                brief_file=[brief],
                ledger=root / "ledger.json",
                account_provider="google",
                account_config=None,
                account=None,
            )
            choice = {"provider": "google", "env": {}}
            with (
                patch.object(account_choice, "select", return_value=choice) as select,
                patch.object(account_choice, "prepare_launch") as receipts,
                patch.object(iterm_tab.claude_trust, "plan", return_value=[]),
            ):
                commands, choices = iterm_tab.prepare_launch_batch(args)
            select.assert_called_once()
            receipts.assert_called_once_with(choice)
            selected = choices[0]
            assert selected is not None
            self.assertIn(selected["file_native"]["log"], commands[0])
            self.assertIn("--dangerously-skip-permissions", shlex.split(commands[0]))
            command.write_text(f"cd {root} && exec agy --continue")
            with patch.object(account_choice, "select", side_effect=AssertionError("invalid fresh mode reached account selection")):
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_launch_batch(args)
            command.write_text(f"cd {root} && exec agy --effort medium")
            with (
                patch.object(account_choice, "select", side_effect=ValueError("Context Panel has no google choice")),
                patch.object(account_choice, "prepare_launch") as receipts,
            ):
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_launch_batch(args)
                receipts.assert_not_called()

    def test_cli_preserves_a_submitted_launch_failure_receipt(self) -> None:
        source_file = iterm_tab.__file__
        assert source_file is not None
        script = Path(source_file).resolve()
        probe = """
import asyncio, runpy, sys, types
sys.path.insert(0, sys.argv[1])
async def operate(args):
    import iterm_tab
    raise iterm_tab.LaunchFailure([], {"mutation_started": True, "session_id": "exact-terminal"}, ValueError("native registration unavailable"))
sys.modules["tmux_terminal"] = types.SimpleNamespace(operate=operate)
sys.argv = [sys.argv[2], "--backend", "tmux", "new", "--window-id", "fixture", "--account-provider", "google"]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
        result = subprocess.run([sys.executable, "-c", probe, str(script.parent), str(script)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["failed_launch"]["session_id"], "exact-terminal")
        self.assertTrue(receipt["failed_launch"]["mutation_started"])

    def test_google_registers_and_closes_through_shared_native_paths(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = "12345678-1234-1234-1234-123456789abc"
            transcript = root / "conversations" / f"{session}.db"
            transcript.parent.mkdir()
            timestamp = datetime(2026, 1, 1, tzinfo=UTC)
            metadata = protobuf({1: protobuf({1: int(timestamp.timestamp())}), 8: protobuf({1: int(timestamp.timestamp())})})
            final = 'Session outcome: {"landed_without_rescue": true, "review_findings_acted_on": 0}\nSafe to exit: yes'
            response = protobuf({1: 15, 4: 3, 5: metadata, 20: protobuf({1: final.encode()})})
            with sqlite3.connect(transcript) as database:
                database.execute("CREATE TABLE trajectory_meta (cascade_id TEXT)")
                database.execute("INSERT INTO trajectory_meta VALUES (?)", (session,))
                database.execute(
                    "CREATE TABLE steps (idx INTEGER, step_type INTEGER, status INTEGER, has_subtrajectory INTEGER, step_payload BLOB)"
                )
                database.execute("INSERT INTO steps VALUES (0,15,3,0,?)", (response,))
            log = root / "launch.log"
            model = "Gemini fixture (Medium)"
            log.write_text(f'Propagating selected model override to backend: label="{model}"\nCreated conversation {session}\n')
            native = {"harness": "agy", "home": str(root), "log": str(log)}
            launch = {"session_id": "owned-terminal", "pid": 8, "tty": "/dev/ttys001", "backend": "tmux"}
            ledger = root / "ledger.json"
            with (
                patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", "agy")]),
                patch.object(session_liveness, "process_started_at", return_value=timestamp),
            ):
                asyncio.run(iterm_tab.register_fresh_launch(argparse.Namespace(ledger=ledger), launch, {"file_native": native}))
                recovered = dict(launch)
                asyncio.run(iterm_tab.register_fresh_launch(argparse.Namespace(ledger=ledger), recovered, None))
            entry = session_record.load_ledger(ledger)[0]
            self.assertEqual(entry["session_id"], session)
            closed_log = root / "closed.json"
            closed_log.write_text(json.dumps([entry]))
            closed_log.chmod(0o600)
            with patch.object(session_closeout.closure, "closed_log", return_value=closed_log):
                self.assertEqual(session_closeout.closure.source_records({session})[session]["session_id"], session)
            self.assertEqual(entry["tmux_session_id"], launch["session_id"])
            self.assertEqual(len(json.loads(ledger.read_text())), 1)
            status = session_closeout.ready(entry, "supervisor")
            self.assertEqual(
                status["session_outcome"],
                {"model": model, "effort": "medium", "landed_without_rescue": True, "review_findings_acted_on": 0},
            )
            original_log = log.read_text()
            unknown_model = session_record.session_outcomes.record({"harness": "agy"}, [], "")["model"]
            resolved = "Gemini resolved (High)"
            log.write_text(f'model alias "fixture-alias" resolved to "{resolved}"\nCreated conversation {session}\n')
            self.assertEqual(session_closeout.ready(entry, "supervisor")["session_outcome"]["model"], resolved)
            log.write_text(original_log)
            log.unlink()
            self.assertEqual(session_closeout.ready(entry, "supervisor")["session_outcome"]["model"], unknown_model)
            with patch.object(session_liveness, "process_absent", return_value=False):
                with self.assertRaises(session_closeout.GateFailed):
                    asyncio.run(session_closeout.native_idle(entry))
            with patch.object(session_liveness, "process_absent", return_value=True):
                asyncio.run(session_closeout.native_idle(entry))
            log.write_text(original_log + "Created conversation abcdef00-0000-4000-8000-000000000001\n")
            self.assertEqual(session_closeout.ready(entry, "supervisor")["session_outcome"]["model"], unknown_model)
            with patch.object(session_liveness, "process_absent", return_value=False):
                with self.assertRaises(session_closeout.GateFailed):
                    asyncio.run(session_closeout.native_idle(entry))
            for resumed in ("abcdef00-0000-4000-8000-000000000001", "unavailable"):
                log.write_text(original_log + f"Resuming conversation {resumed}\n")
                with patch.object(session_liveness, "process_absent", return_value=False):
                    with self.assertRaises(session_closeout.GateFailed):
                        asyncio.run(session_closeout.native_idle(entry))
            log.write_text(original_log)
            with patch.object(session_liveness, "process_absent", return_value=False):
                asyncio.run(session_closeout.native_idle(entry))
            self.assertEqual(session_liveness.invocation(["agy", "--conversation", session])[0], [session])
            with (
                patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", "agy")]),
                patch.object(session_liveness, "process_absent", return_value=True),
            ):
                ended = iterm_tab.exit_observer("/exit", "ttys001")
                assert ended is not None
                self.assertTrue(ended())
            for change in ("pending", "child", "later_user", "identity"):
                with self.subTest(change=change), sqlite3.connect(transcript) as database:
                    if change == "pending":
                        database.execute("UPDATE steps SET status=2,step_payload=?", (protobuf({1: 15, 4: 2, 5: metadata}),))
                    elif change == "child":
                        database.execute("UPDATE steps SET has_subtrajectory=1")
                    elif change == "later_user":
                        database.execute(
                            "INSERT INTO steps VALUES (1,14,3,0,?)",
                            (protobuf({1: 14, 4: 3, 5: metadata, 19: protobuf({2: b"continue"})}),),
                        )
                    else:
                        database.execute("UPDATE trajectory_meta SET cascade_id='another-session'")
                    database.commit()
                    with self.assertRaises((session_closeout.GateFailed, ValueError)):
                        session_closeout.ready(entry, "supervisor")
                    database.execute("DELETE FROM steps WHERE idx=1")
                    database.execute("UPDATE steps SET status=3,has_subtrajectory=0,step_payload=?", (response,))
                    database.execute("UPDATE trajectory_meta SET cascade_id=?", (session,))
            with sqlite3.connect(transcript) as writer:
                fixture = json.loads((Path(__file__).parent / "fixtures/agy-command-completed.json").read_text())
                fields = iterm_tab.claim_history.protobuf_fields
                captured = fields(bytes.fromhex(fixture["step_payload_hex"]))
                for change in (
                    "captured",
                    "silent",
                    "pending_exit",
                    "pending_status",
                    "unknown_kind",
                    "failed_exit",
                    "cancelled",
                    "launch_error",
                ):
                    step = dict(captured)
                    result = fields(fields(step[140])[2])
                    terminal = fields(fields(fields(result[3])[10])[3])
                    if change == "silent":
                        terminal.pop(4, None)
                        result.pop(1, None)
                    elif change == "pending_exit":
                        terminal.pop(3)
                    elif change == "pending_status":
                        step[4] = 9
                    elif change == "unknown_kind":
                        step[1] = 999
                    elif change == "failed_exit":
                        terminal[3] = 1
                    elif change in {"cancelled", "launch_error"}:
                        step[4] = 6 if change == "cancelled" else 7
                    result[3] = protobuf({10: protobuf({3: protobuf(terminal)})})
                    if change in {"cancelled", "launch_error"}:
                        result.pop(3)
                        result.pop(1, None)
                    step[140] = protobuf({2: protobuf(result)})
                    writer.execute("INSERT INTO steps VALUES (1,?,?,0,?)", (step[1], step[4], protobuf(step)))
                    writer.execute("INSERT INTO steps VALUES (2,15,3,0,?)", (response,))
                    writer.commit()
                    with self.subTest(change=change):
                        summary = session_record.session_status(entry)
                        self.assertEqual(
                            summary["safe_verdict"], change in {"captured", "silent", "failed_exit", "cancelled", "launch_error"}
                        )
                        if change == "pending_status":
                            pending_ledger = root / "pending-ledger.json"
                            with (
                                patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", "agy")]),
                                patch.object(session_liveness, "process_started_at", return_value=timestamp),
                            ):
                                asyncio.run(
                                    iterm_tab.register_fresh_launch(argparse.Namespace(ledger=pending_ledger), dict(launch), None)
                                )
                            self.assertTrue(summary["background_tasks"])
                            self.assertEqual(len(session_record.load_ledger(pending_ledger)), 1)
                        if change in {"failed_exit", "cancelled", "launch_error"}:
                            results = [
                                block
                                for row in session_record.read_jsonl(transcript)
                                for block in row.get("message", {}).get("content", [])
                                if isinstance(block, dict) and block.get("type") == "tool_result"
                            ]
                            self.assertTrue(results[-1]["is_error"])
                    writer.execute("DELETE FROM steps WHERE idx>0")
                    writer.commit()
                for tool, change in product(
                    ("read_file", "search_web", "unrecognized_background_tool"),
                    ("completed", "pending", "async", "child", "old_final"),
                ):
                    arguments = {"query": "fixture query", "domain": "example.invalid"} if tool == "search_web" else {}
                    call = protobuf({1: b"native-call", 2: tool.encode(), 3: json.dumps(arguments).encode()})
                    tool_metadata = protobuf({1: protobuf({1: int(timestamp.timestamp())}), 4: call})
                    status = 2 if change == "pending" else 3
                    step = {1: 132, 4: status, 5: tool_metadata, 140: protobuf({2: protobuf({1: b"native result"})})}
                    if change == "async":
                        background = json.loads((Path(__file__).parent / "fixtures/agy-command-background.json").read_text())
                        declaration = fields(fields(bytes.fromhex(background["steps"][0]["step_payload_hex"]))[148])
                        declaration[1] = f"{session}/task-1".encode()
                        step[148] = protobuf(declaration)
                    writer.execute("INSERT INTO steps VALUES (1,132,?,?,?)", (status, int(change == "child"), protobuf(step)))
                    if change != "old_final":
                        writer.execute("INSERT INTO steps VALUES (2,15,3,0,?)", (response,))
                    writer.commit()
                    with self.subTest(tool=tool, change=change):
                        if tool in {"read_file", "search_web"} and change == "completed":
                            self.assertTrue(session_closeout.ready(entry, "supervisor")["safe_verdict"])
                        else:
                            with self.assertRaises(ValueError):
                                session_closeout.ready(entry, "supervisor")
                    writer.execute("DELETE FROM steps WHERE idx>0")
                    writer.commit()
                identity = f"{session}/task-0".encode()
                prompt = b"Check the fixture result"
                payload = b"> " + prompt + b"\n\n**Status**: Fired"
                schedule_cases: tuple[str, ...] = (
                    "complete",
                    "condition",
                    "missing_notification",
                    "recurring",
                    "daemon",
                    "unknown_argument",
                    "bad_duration",
                    "foreign_declaration",
                    "foreign_session",
                    "foreign_task",
                    "foreign_prompt",
                    "not_done",
                    "different_result",
                    "reworded_result",
                    "unknown_tool",
                    "early_notification",
                    "missing_terminal",
                    "empty_terminal",
                    "missing_declaration",
                    "bad_prompt",
                    "zero_duration",
                )
                for timer_case in schedule_cases:
                    with self.subTest(timer_case=timer_case):
                        arguments = {"DurationSeconds": 1, "Prompt": prompt.decode()}
                        if timer_case in {"recurring", "daemon", "unknown_argument", "condition"}:
                            key, value = {
                                "recurring": ("CronExpression", "* * * * *"),
                                "daemon": ("IsDaemon", True),
                                "unknown_argument": ("Unproven", True),
                                "condition": ("TimerCondition", "after"),
                            }[timer_case]
                            arguments[key] = value
                        if timer_case == "bad_duration":
                            arguments["DurationSeconds"] = True
                        if timer_case == "zero_duration":
                            arguments["DurationSeconds"] = 0
                        if timer_case == "bad_prompt":
                            arguments["Prompt"] = 1
                        result_payload = b"Timer finished" if timer_case == "reworded_result" else payload
                        if timer_case == "empty_terminal":
                            result_payload = b""
                        result = protobuf({10: protobuf({1: result_payload})})
                        timer_metadata = protobuf(
                            {
                                1: protobuf({1: 1}),
                                4: protobuf(
                                    {
                                        1: b"timer-call",
                                        2: b"unknown_tool" if timer_case == "unknown_tool" else b"schedule",
                                        3: json.dumps(arguments).encode(),
                                    }
                                ),
                            }
                        )
                        scheduled = {
                            1: 132,
                            4: 3,
                            5: timer_metadata,
                            148: protobuf({1: b"foreign/task-0" if timer_case == "foreign_declaration" else identity}),
                            140: protobuf({2: protobuf({3: result})}),
                        }
                        if timer_case == "missing_terminal":
                            scheduled.pop(140)
                        if timer_case == "missing_declaration":
                            scheduled.pop(148)
                        if timer_case == "early_notification":
                            scheduled[148] = protobuf({1: f"{session}/task-1".encode()})
                        envelope = {
                            2: b"foreign" if timer_case == "foreign_session" else session.encode(),
                            3: b"foreign/task-0" if timer_case == "foreign_task" else identity,
                            4: b"foreign prompt" if timer_case == "foreign_prompt" else prompt,
                            5: 2 if timer_case == "not_done" else 3,
                        }
                        if timer_case == "early_notification":
                            envelope[3] = f"{session}/task-1".encode()
                        note = {
                            2: protobuf({10: protobuf({1: b"other"})}) if timer_case == "different_result" else result,
                            3: b"task_notification",
                            4: protobuf(envelope),
                        }
                        notification = {
                            1: 101,
                            4: 3,
                            5: protobuf({1: protobuf({1: 2})}),
                            114: protobuf(note),
                        }
                        steps = [scheduled] if timer_case == "missing_notification" else [scheduled, notification]
                        if timer_case == "early_notification":
                            steps.reverse()
                        writer.execute("DELETE FROM steps")
                        for index, step in enumerate(steps):
                            writer.execute("INSERT INTO steps VALUES (?,?,?,0,?)", (index, step[1], step[4], protobuf(step)))
                        writer.execute("INSERT INTO steps VALUES (?,15,3,0,?)", (len(steps), response))
                        writer.commit()
                        summary = session_record.session_status(entry)
                        self.assertEqual(summary["safe_verdict"], timer_case in {"complete", "reworded_result"})
                        self.assertEqual(bool(summary["background_tasks"]), timer_case not in {"complete", "reworded_result"})

                for fixture_name, change in product(
                    ("agy-command-background.json", "agy-command-cancelled.json"),
                    (
                        "captured",
                        "kill_done",
                        "pending",
                        "missing_notification",
                        "old_final",
                        "foreign_task",
                        "cancel",
                        "foreign_origin",
                        "unknown_notification",
                        "opaque_notification",
                        "running",
                        "foreign_call",
                        "non_error",
                    ),
                ):
                    if change == "kill_done" and fixture_name == "agy-command-cancelled.json":
                        continue
                    background = json.loads((Path(__file__).parent / "fixtures" / fixture_name).read_text())
                    steps = [fields(bytes.fromhex(row["step_payload_hex"])) for row in background["steps"]]
                    if change == "pending":
                        steps[0].pop(140)
                    elif change == "running":
                        steps[0][4] = 2
                    elif change in {"missing_notification", "old_final"}:
                        steps[2] = fields(response)
                    elif change in {"foreign_task", "cancel", "kill_done"}:
                        metadata_fields = fields(steps[1][5])
                        call_fields = fields(metadata_fields[4])
                        arguments = json.loads(call_fields[3])
                        arguments["TaskId" if change == "foreign_task" else "Action"] = (
                            "kill" if change == "kill_done" else "unverified"
                        )
                        call_fields[3] = json.dumps(arguments).encode()
                        metadata_fields[4] = protobuf(call_fields)
                        steps[1][5] = protobuf(metadata_fields)
                        if change == "kill_done":
                            steps[1][4] = 7
                            steps[1][31] = protobuf({2: b"native rejection"})
                    elif change in {"foreign_origin", "foreign_call", "non_error", "unknown_notification", "opaque_notification"}:
                        note = fields(steps[2][114])
                        if change in {"unknown_notification", "opaque_notification"}:
                            note[3] = b"unverified"
                            if change == "opaque_notification":
                                note[4] = 1
                        elif change == "non_error":
                            result = fields(note[2])
                            result.pop(4, None)
                            note[2] = protobuf(result)
                        else:
                            envelope = fields(note[4])
                            if change == "foreign_call":
                                origin = fields(fields(envelope[12])[1])
                                call_fields = fields(origin[3])
                                call_fields[1] = b"unrelated-call"
                                origin[3] = protobuf(call_fields)
                                envelope[12] = protobuf({1: protobuf(origin)})
                            else:
                                envelope[2] = b"another-session"
                            note[4] = protobuf(envelope)
                        steps[2][114] = protobuf(note)
                    writer.execute("DELETE FROM steps")
                    for index, step in enumerate(steps):
                        writer.execute("INSERT INTO steps VALUES (?,?,?,0,?)", (index, step[1], step[4], protobuf(step)))
                    last = (
                        fields(bytes.fromhex(background["steps"][2]["step_payload_hex"]))
                        if change == "old_final"
                        else fields(response)
                    )
                    writer.execute("INSERT INTO steps VALUES (3,?,?,0,?)", (last[1], last[4], protobuf(last)))
                    writer.commit()
                    with self.subTest(fixture=fixture_name, background=change):
                        self.assertEqual(
                            session_record.session_status(entry)["safe_verdict"],
                            change in {"captured", "kill_done"}
                            or change == "non_error"
                            and fixture_name == "agy-command-background.json",
                        )
                writer.execute("DELETE FROM steps")
                writer.execute("INSERT INTO steps VALUES (0,15,3,0,?)", (response,))
                writer.commit()
                writer.execute("PRAGMA journal_mode=WAL")
                read = session_record.read_jsonl

                def append_during_read(path: Path) -> list[dict]:
                    found = read(path)
                    with sqlite3.connect(transcript) as concurrent_writer:
                        concurrent_writer.execute(
                            "INSERT INTO steps VALUES (1,14,3,0,?)",
                            (protobuf({1: 14, 4: 3, 5: metadata, 19: protobuf({2: b"continue"})}),),
                        )
                    return found

                for reader in (session_record.session_status, session_record.posted_comments):
                    with (
                        self.subTest(reader=reader.__name__),
                        patch.object(session_record, "read_jsonl", side_effect=append_during_read),
                    ):
                        with self.assertRaises(session_record.TranscriptChanged):
                            reader(entry)
                    writer.execute("DELETE FROM steps WHERE idx=1")
                    writer.commit()

                def complete_startup(path: Path) -> list[dict]:
                    try:
                        return read(path)
                    finally:
                        with sqlite3.connect(transcript) as initializer:
                            if not initializer.execute("SELECT cascade_id FROM trajectory_meta").fetchall():
                                initializer.execute("INSERT INTO trajectory_meta VALUES (?)", (session,))

                for label, during_read in (("streaming", append_during_read), ("startup", complete_startup)):
                    if label == "startup":
                        writer.execute("DELETE FROM trajectory_meta")
                        writer.commit()
                    registration_ledger = root / f"{label}-ledger.json"
                    with (
                        patch.object(session_record, "read_jsonl", side_effect=during_read),
                        patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", "agy")]),
                        patch.object(session_liveness, "process_started_at", return_value=timestamp),
                    ):
                        asyncio.run(
                            iterm_tab.register_fresh_launch(argparse.Namespace(ledger=registration_ledger), dict(launch), None)
                        )
                    self.assertEqual(session_record.load_ledger(registration_ledger)[0]["session_id"], session)

    def test_input_line_excludes_agy_slash_menu_suggestion_row(self) -> None:
        screen = [
            "────────────────────────────────────────",
            "> /exit",
            "────────────────────────────────────────",
            "> /exit  Exit the CLI",
            "",
            "  ↑/↓ Navigate · enter Select · tab Complete",
            "esc to cancel    Gemini 3.8 Flash · medium",
        ]
        lines, styles = iterm_tab.ansi_screen("\n".join(screen))
        result = iterm_tab.input_line(lines, styles)
        self.assertEqual(result["line_index"], 1)
        self.assertEqual(result["text"], "/exit")
        direct = iterm_tab.input_line(screen, [[None] * len(line) for line in screen])
        self.assertEqual(direct["line_index"], 1)
        self.assertEqual(direct["text"], "/exit")


if __name__ == "__main__":
    unittest.main()
