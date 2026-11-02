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
from datetime import UTC, datetime
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


def protobuf(values: dict[int, int | bytes]) -> bytes:
    return b"".join(
        varint(key * 8) + varint(value) if isinstance(value, int) else varint(key * 8 + 2) + varint(len(value)) + value
        for key, value in values.items()
    )


class GoogleWorkerTests(unittest.TestCase):
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
                for tool in ("read_file", "unrecognized_background_tool"):
                    call = protobuf({1: b"native-call", 2: tool.encode(), 3: b"{}"})
                    tool_metadata = protobuf({1: protobuf({1: int(timestamp.timestamp())}), 4: call})
                    tool_step = protobuf({1: 132, 4: 3, 5: tool_metadata, 140: protobuf({2: protobuf({1: b"native result"})})})
                    writer.execute("INSERT INTO steps VALUES (1,132,3,0,?)", (tool_step,))
                    writer.execute("INSERT INTO steps VALUES (2,15,3,0,?)", (response,))
                    writer.commit()
                    if tool == "read_file":
                        self.assertTrue(session_closeout.ready(entry, "supervisor")["safe_verdict"])
                    else:
                        with self.assertRaises(ValueError):
                            session_closeout.ready(entry, "supervisor")
                    writer.execute("DELETE FROM steps WHERE idx>0")
                    writer.commit()
                background = json.loads((Path(__file__).parent / "fixtures/agy-command-background.json").read_text())
                for change in (
                    "captured",
                    "pending",
                    "missing_notification",
                    "old_final",
                    "foreign_task",
                    "cancel",
                    "foreign_origin",
                    "unknown_notification",
                ):
                    steps = [fields(bytes.fromhex(row["step_payload_hex"])) for row in background["steps"]]
                    if change == "pending":
                        steps[0].pop(140)
                    elif change in {"missing_notification", "old_final"}:
                        steps[2] = fields(response)
                    elif change in {"foreign_task", "cancel"}:
                        metadata_fields = fields(steps[1][5])
                        call_fields = fields(metadata_fields[4])
                        arguments = json.loads(call_fields[3])
                        arguments["TaskId" if change == "foreign_task" else "Action"] = "unverified"
                        call_fields[3] = json.dumps(arguments).encode()
                        metadata_fields[4] = protobuf(call_fields)
                        steps[1][5] = protobuf(metadata_fields)
                    elif change in {"foreign_origin", "unknown_notification"}:
                        note = fields(steps[2][114])
                        if change == "unknown_notification":
                            note[3] = b"unverified"
                        else:
                            envelope = fields(note[4])
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
                    with self.subTest(background=change):
                        self.assertEqual(session_record.session_status(entry)["safe_verdict"], change == "captured")
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
                streaming_ledger = root / "streaming-ledger.json"
                with (
                    patch.object(session_record, "read_jsonl", side_effect=append_during_read),
                    patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", "agy")]),
                    patch.object(session_liveness, "process_started_at", return_value=timestamp),
                ):
                    asyncio.run(iterm_tab.register_fresh_launch(argparse.Namespace(ledger=streaming_ledger), dict(launch), None))
                self.assertEqual(session_record.load_ledger(streaming_ledger)[0]["session_id"], session)
                writer.execute("DELETE FROM trajectory_meta")
                writer.commit()

                def complete_startup(path: Path) -> list[dict]:
                    try:
                        return read(path)
                    finally:
                        with sqlite3.connect(transcript) as initializer:
                            if not initializer.execute("SELECT cascade_id FROM trajectory_meta").fetchall():
                                initializer.execute("INSERT INTO trajectory_meta VALUES (?)", (session,))

                startup_ledger = root / "startup-ledger.json"
                with (
                    patch.object(session_record, "read_jsonl", side_effect=complete_startup),
                    patch.object(session_liveness, "inventory", return_value=[(8, "ttys001", "agy")]),
                    patch.object(session_liveness, "process_started_at", return_value=timestamp),
                ):
                    asyncio.run(iterm_tab.register_fresh_launch(argparse.Namespace(ledger=startup_ledger), dict(launch), None))
                self.assertEqual(session_record.load_ledger(startup_ledger)[0]["session_id"], session)

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
