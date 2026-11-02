# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "websockets==17.3"]
# ///
import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timezone
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
            ):
                commands, choices = iterm_tab.prepare_launch_batch(args)
            select.assert_called_once()
            receipts.assert_called_once_with(choice)
            self.assertIn(choices[0]["file_native"]["log"], commands[0])
            with (
                patch.object(account_choice, "select", side_effect=ValueError("Context Panel has no google choice")),
                patch.object(account_choice, "prepare_launch") as receipts,
            ):
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_launch_batch(args)
                receipts.assert_not_called()

    def test_cli_preserves_a_submitted_launch_failure_receipt(self) -> None:
        script = Path(iterm_tab.__file__).resolve()
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
            self.assertEqual(entry["tmux_session_id"], launch["session_id"])
            self.assertEqual(len(json.loads(ledger.read_text())), 1)
            status = session_closeout.ready(entry, "supervisor")
            self.assertEqual(
                status["session_outcome"],
                {"model": model, "effort": "medium", "landed_without_rescue": True, "review_findings_acted_on": 0},
            )
            original_log = log.read_text()
            resolved = "Gemini resolved (High)"
            log.write_text(f'model alias "fixture-alias" resolved to "{resolved}"\nCreated conversation {session}\n')
            self.assertEqual(session_closeout.ready(entry, "supervisor")["session_outcome"]["model"], resolved)
            log.write_text(original_log)
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
                writer.execute("PRAGMA journal_mode=WAL")
                read = session_record.read_jsonl

                def append_during_read(path: Path) -> list[dict]:
                    found = read(path)
                    writer.execute(
                        "INSERT INTO steps VALUES (1,14,3,0,?)",
                        (protobuf({1: 14, 4: 3, 5: metadata, 19: protobuf({2: b"continue"})}),),
                    )
                    writer.commit()
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


if __name__ == "__main__":
    unittest.main()
