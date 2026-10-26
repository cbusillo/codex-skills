# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5", "websockets==17.2"]
# ///

import asyncio
import contextlib
import importlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import account_choice
import codex_auth_health as health
import codex_idle_watch
import codex_session_proxy
import interactive_accounts

closure = importlib.import_module("github_plan_closure")


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


MESSAGES = {
    "attempt": "Refreshing token",
    "expired": "Failed to refresh token: Your access token could not be refreshed because your refresh token has expired. Please log out and sign in again.",
    "reused": "Failed to refresh token: Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again.",
    "reset": "Reloaded auth, changed: true",
    "reload": "Skipping token refresh because auth changed after guarded reload.",
    "issuer": "Failed to refresh token status=401 Unauthorized secret=DO-NOT-COPY",
}
PROCESS = "pid:42:01234567-89ab-cdef-0123-456789abcdef"


class AuthHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(patch.object(Path, "home", return_value=self.root))
        self.home = self.root / "failed"
        self.home.mkdir()
        self.database = self.home / "logs_2.sqlite"
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, "
                "level TEXT, target TEXT, process_uuid TEXT, feedback_log_body TEXT)"
            )
        self.identity = health.DaemonIdentity(42, 0, "fixture-start")
        self.enterContext(patch.object(health, "daemon_identity", return_value=self.identity))
        self.enterContext(patch.object(health, "process_inventory", return_value=[{"pid": 42, "executable": "codex"}]))
        self.probe = self.enterContext(patch.object(health, "native_status", side_effect=self.failed_probe))
        self.clock = 0
        self.row = 0
        self.account = {
            "name": "failed",
            "provider": "openai",
            "env": {"CODEX_HOME": str(self.home)},
            "match": "context_panel_label",
            "match_value": "Failed",
        }
        self.good = {**self.account, "name": "healthy", "env": {"CODEX_HOME": str(self.root / "healthy")}, "match_value": "Healthy"}
        self.config = {"accounts": [self.account, self.good]}
        self.snapshot: dict[str, Any] = {
            "schemaVersion": 1,
            "accounts": [
                {"provider": "openai", "id": "openai-a", "label": "Failed"},
                {"provider": "openai", "id": "openai-b", "label": "Healthy"},
            ],
            "ranking": [{"provider": "openai", "launchOrder": ["openai-a", "openai-b"]}],
            "answers": {"useNext": [{"provider": "openai", "accountID": "openai-a"}]},
        }

    def append(self, kind: str, *, body: str | None = None) -> int:
        self.clock += 1_000_000_000
        self.row += 1
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self.row,
                    self.clock // 1_000_000_000,
                    self.clock % 1_000_000_000,
                    "ERROR" if kind in {"expired", "reused", "issuer"} else "INFO",
                    "codex_login::auth::manager",
                    PROCESS,
                    body or MESSAGES[kind],
                ),
            )
        return self.clock

    def failure(self, kind: str = "expired") -> None:
        self.append("attempt")
        self.append(kind)

    def failed_probe(self, home: Path) -> health.AuthStatus:
        requested = self.clock + 1
        if home != self.home:
            return health.AuthStatus(self.identity, requested, "chatgpt", True, True)
        self.failure()
        return health.AuthStatus(self.identity, requested, "chatgpt", True, False)

    def reset_probe(self, _home: Path) -> health.AuthStatus:
        requested = self.clock + 1
        self.append("reset")
        return health.AuthStatus(self.identity, requested, "chatgpt", True, True)

    def args(self, *, once: bool = True) -> SimpleNamespace:
        ledger = self.root / "ledger.json"
        ledger.write_text("[]")
        return SimpleNamespace(
            ledger=ledger,
            idle_seconds=90,
            harness="codex",
            deadline_seconds=60,
            delivered_state=self.root / "delivered.json",
            capacity_state=None,
            resume_capacity=False,
            once=once,
            interval=1,
        )

    def test_proactive_read_runs_before_any_failure_exists(self) -> None:
        self.probe.side_effect = None
        self.probe.return_value = SimpleNamespace(
            identity=self.identity, requested_ns=1, auth_method="chatgpt", requires_auth=True, account_present=True
        )
        self.assertFalse(health.observe(self.home)["blocked"])
        self.probe.assert_called_once_with(self.home)

    def test_single_cached_permanent_failure_holds_until_verified_recovery(self) -> None:
        self.failure()
        self.probe.side_effect = None
        self.probe.return_value = SimpleNamespace(
            identity=self.identity, requested_ns=self.clock + 1, auth_method="chatgpt", requires_auth=True, account_present=False
        )
        with patch.object(health, "LOG_DELIVERY_SECONDS", 0):
            self.assertTrue(health.observe(self.home)["blocked"])
        self.probe.side_effect = TimeoutError
        self.assertTrue(health.observe(self.home)["blocked"])
        self.probe.side_effect = None
        self.probe.return_value.account_present = True
        self.assertFalse(health.observe(self.home)["blocked"])

    def test_recovery_separates_later_failures_from_the_delivered_episode(self) -> None:
        first = health.observe(self.home)["episode_ns"]
        self.probe.side_effect = self.reset_probe
        self.assertFalse(health.observe(self.home)["blocked"])
        self.probe.side_effect = self.failed_probe
        self.assertGreater(health.observe(self.home)["episode_ns"], first)

    def test_old_responses_cannot_overwrite_a_newer_saved_hold(self) -> None:
        health.record_status(self.home, {"blocked": True, "episode_ns": 4, "observed_ns": 5})
        self.assertTrue(health.record_status(self.home, {"blocked": False, "episode_ns": None, "observed_ns": 3})["blocked"])
        self.assertTrue(health.journal_status(self.home)["blocked"])

    def test_older_healthy_reply_cannot_release_a_newer_observed_failure(self) -> None:
        def stale_reply(_home: Path) -> health.AuthStatus:
            self.failure()
            health.record_status(self.home, {"blocked": True, "episode_ns": self.clock, "observed_ns": self.clock + 2})
            return health.AuthStatus(self.identity, 1, "chatgpt", True, True)

        self.probe.side_effect = stale_reply
        self.assertTrue(health.observe(self.home)["blocked"])

    def test_timeout_uses_a_hold_saved_by_another_observer(self) -> None:
        def timed_out(_home: Path) -> health.AuthStatus:
            health.record_status(self.home, {"blocked": True, "episode_ns": 4, "observed_ns": 5})
            raise TimeoutError

        self.probe.side_effect = timed_out
        self.assertTrue(health.observe(self.home)["blocked"])

    def test_older_failed_reply_cannot_reopen_a_recovered_account(self) -> None:
        health.observe(self.home)

        def stale_failure(_home: Path) -> health.AuthStatus:
            health.record_status(self.home, {"blocked": False, "episode_ns": None, "observed_ns": self.clock + 2})
            return health.AuthStatus(self.identity, 1, "chatgpt", True, False)

        self.probe.side_effect = stale_failure
        self.assertFalse(health.observe(self.home)["blocked"])

    def test_a_new_daemon_or_missing_proof_does_not_release_a_confirmed_hold(self) -> None:
        health.observe(self.home)
        self.probe.side_effect = None
        self.probe.return_value = health.AuthStatus(health.DaemonIdentity(43, 0, "replacement"), 0, "chatgpt", True, False)
        self.assertTrue(health.observe(self.home)["blocked"])

    def test_unknown_without_a_confirmed_failure_does_not_block_a_healthy_account(self) -> None:
        self.probe.side_effect = TimeoutError
        self.assertFalse(health.observe(self.home)["blocked"])

    def test_confirmed_failure_excludes_only_its_account_and_preserves_published_order(self) -> None:
        self.failure()
        self.assertEqual(account_choice.choose("openai", self.config, self.snapshot, None, name="healthy")["name"], "healthy")
        self.probe.assert_called_once()
        self.probe.reset_mock()
        self.assertEqual(account_choice.choose("openai", self.config, self.snapshot, None)["name"], "healthy")
        self.snapshot["accounts"].append({"provider": "openai", "id": "openai-c", "label": "Unconfigured"})
        self.snapshot["ranking"][0]["launchOrder"].insert(1, "openai-c")
        self.assertEqual(account_choice.choose("openai", self.config, self.snapshot, None)["name"], "healthy")
        with self.assertRaises(ValueError):
            account_choice.choose("openai", self.config, self.snapshot, None, name="failed")
        self.assertEqual(account_choice.choose("openai", self.config, self.snapshot, None, name="healthy")["name"], "healthy")
        with self.assertRaises(ValueError):
            account_choice.choose_batch("openai", self.config, self.snapshot, None, count=2)
        self.snapshot["ranking"][0]["launchOrder"].append("openai-b")
        choices = account_choice.choose_batch("openai", self.config, self.snapshot, None, count=2)
        self.assertEqual([choice["name"] for choice in choices], ["healthy", "healthy"])
        self.snapshot["ranking"][0].update(basedOnStaleReadings=True, readingsObservedAt="2026-10-26T17:00:00Z")
        stale = account_choice.choose("openai", self.config, self.snapshot, None, now=datetime(2026, 10, 26, 17, tzinfo=UTC))
        self.assertEqual(stale["name"], "healthy")

    def test_first_error_is_preserved_before_native_read_and_no_credentials_are_opened(self) -> None:
        self.failure()
        self.append("attempt", body="Refreshing token secret=DO-NOT-COPY")
        other_process = "pid:55:01234567-89ab-cdef-0123-456789abcdef"
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO logs VALUES (-1, 0, 0, 'INFO', 'codex_tui::client', ?, ?)", (other_process, "DO-NOT-COPY")
            )
        auth = self.home / "auth.json"
        auth.write_text("opaque synthetic credential")
        original_open = io.open

        def guarded_open(path: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(path, str | os.PathLike):
                self.assertNotEqual(Path(path), auth)
            return original_open(path, *args, **kwargs)

        def check_capture(home: Path) -> health.AuthStatus:
            captured_journal = home / "skill-data/supervisor-auth/evidence.jsonl"
            self.assertIn("expired", captured_journal.read_text())
            return self.failed_probe(home)

        self.probe.side_effect = check_capture
        with patch_attr(io, "open", guarded_open):
            result = health.observe(self.home)
        self.assertTrue(result["blocked"])
        journal = self.home / "skill-data/supervisor-auth/evidence.jsonl"
        self.assertNotIn("DO-NOT-COPY", journal.read_text())
        self.assertNotIn("opaque synthetic credential", journal.read_text())
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        batches = [json.loads(line) for line in journal.read_text().splitlines() if line]
        self.assertTrue(any(other_process in batch["home_log_processes"] for batch in batches))

    def test_evidence_symlink_cannot_read_or_modify_a_credential_file(self) -> None:
        directory = self.home / "skill-data/supervisor-auth"
        directory.mkdir(parents=True)
        credential = self.home / "auth.json"
        credential.write_text("synthetic credential")
        (directory / "evidence.jsonl").symlink_to(credential)
        self.failure()
        self.assertIn("diagnostic", health.observe(self.home))
        self.assertEqual(credential.read_text(), "synthetic credential")
        self.probe.assert_not_called()

    def test_reused_then_blocking_failure_remains_in_journal_after_rotation(self) -> None:
        self.failure("reused")
        self.assertTrue(health.observe(self.home)["blocked"])
        self.failure()
        self.assertTrue(health.observe(self.home)["blocked"])
        with sqlite3.connect(self.database) as connection:
            connection.execute("DELETE FROM logs")
        preserved = health.read_journal(self.home / "skill-data/supervisor-auth/evidence.jsonl")
        self.assertTrue({"reused", "expired"} <= {event["event"] for event in preserved})
        self.assertTrue(health.observe(self.home)["blocked"])

    def test_issuer_rejection_is_kept_without_claiming_a_permanent_failure(self) -> None:
        self.append("issuer")
        self.probe.side_effect = None
        self.probe.return_value = health.AuthStatus(self.identity, self.clock + 1, "chatgpt", True, False)
        with patch.object(health, "LOG_DELIVERY_SECONDS", 0):
            self.assertFalse(health.observe(self.home)["blocked"])
        journal = self.home / "skill-data/supervisor-auth/evidence.jsonl"
        rejection = health.read_journal(journal)[0]
        self.assertEqual(rejection["status"], 401)
        self.assertNotIn("DO-NOT-COPY", journal.read_text())

    def test_one_permanent_error_has_a_stable_episode_without_another_attempt(self) -> None:
        self.failure()
        episode = health.failure_episode(health.read_log(self.home))
        self.append("expired")
        self.assertEqual(health.failure_episode(health.read_log(self.home)), episode)

    def test_sign_in_in_load_to_log_race_clears_through_native_reload(self) -> None:
        self.failure()
        self.assertTrue(health.observe(self.home)["blocked"])
        self.probe.side_effect = self.reset_probe
        self.assertFalse(health.observe(self.home)["blocked"])
        calls = self.probe.call_count
        self.assertFalse(health.observe(self.home)["blocked"])
        self.assertEqual(self.probe.call_count, calls + 1)

    def test_delayed_logs_are_awaited_but_absent_current_evidence_is_unknown(self) -> None:
        requested = self.clock + 1
        self.probe.side_effect = None
        self.probe.return_value = health.AuthStatus(self.identity, requested, "chatgpt", True, False)
        with patch.object(health, "LOG_DELIVERY_SECONDS", 0):
            result = health.observe(self.home)
        self.assertFalse(result["blocked"])
        self.assertIn("diagnostic", result)
        original_read = health.read_log
        elapsed = 0.0

        def delayed(home: Path) -> list[dict]:
            if elapsed < 10:
                return [event for event in original_read(home) if event["ts_ns"] < requested]
            return original_read(home)

        def advance(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        self.probe.side_effect = self.failed_probe
        with (
            patch.object(health, "read_log", side_effect=delayed),
            patch.object(health.time, "monotonic", side_effect=lambda: elapsed),
            patch.object(health.time, "sleep", side_effect=advance),
        ):
            self.assertTrue(health.observe(self.home)["blocked"])

    def test_partial_journal_reconstructs_from_retained_logs(self) -> None:
        directory = self.home / "skill-data/supervisor-auth"
        directory.mkdir(parents=True)
        journal = directory / "evidence.jsonl"
        journal.write_text('{"partial":')
        self.failure()
        self.assertTrue(health.observe(self.home)["blocked"])
        self.assertTrue(health.read_journal(journal))

    def test_capture_overlap_is_not_a_diagnostic_or_a_launch_bypass(self) -> None:
        self.failure()
        with patch.object(health.fcntl, "flock", side_effect=BlockingIOError):
            result = health.observe(self.home)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["diagnostic"], "auth_observation_not_saved")

    def test_unavailable_or_changed_daemon_cannot_inherit_a_hold(self) -> None:
        self.failure()
        self.probe.side_effect = ValueError("unavailable")
        self.assertFalse(health.observe(self.home)["blocked"])
        self.probe.side_effect = None
        self.probe.return_value = health.AuthStatus(health.DaemonIdentity(43, 0, "new"), 0, "chatgpt", True, False)
        self.assertFalse(health.observe(self.home)["blocked"])

    def test_corrupt_database_is_unknown_and_does_not_create_a_hold(self) -> None:
        self.database.write_bytes(b"not sqlite")
        result = health.observe(self.home)
        self.assertFalse(result["blocked"])
        self.assertIn("diagnostic", result)

    def test_watcher_wakes_once_per_hold_across_rearms(self) -> None:
        self.failure()
        self.enterContext(patch.object(account_choice, "load_config", return_value=self.config))
        args = self.args()
        first, rearmed = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(first):
            codex_idle_watch.watch(args, None)
        with contextlib.redirect_stdout(rearmed):
            codex_idle_watch.watch(args, None)
        self.assertIn("auth_failure", first.getvalue())
        self.assertEqual(rearmed.getvalue(), "")

    def test_diagnostics_do_not_request_an_immediate_monitor_rearm(self) -> None:
        args = self.args(once=False)
        clock = iter((0, 0, 61))
        diagnostic = {"auth_diagnostic": {"home_id": "fixture", "reason": "unavailable"}}
        self.enterContext(patch.object(account_choice, "load_config", return_value=self.config))
        with (
            patch.object(health, "notices", return_value=[diagnostic]),
            patch.object(codex_idle_watch.time, "monotonic", side_effect=lambda: next(clock, 61)),
            patch.object(codex_idle_watch.time, "sleep") as wait,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            codex_idle_watch.watch(args, None)
        wait.assert_called_once()

    def test_interactive_and_managed_clients_refuse_before_execution_but_login_remains_available(self) -> None:
        self.failure()
        self.enterContext(patch.dict(os.environ, {"CODEX_HOME": str(self.home)}))
        self.enterContext(patch.object(account_choice, "load_config", return_value=self.config))
        self.enterContext(patch.object(interactive_accounts.shutil, "which", return_value="/fixture/codex"))
        self.enterContext(patch.object(interactive_accounts, "state_path", return_value=self.root / "last.json"))
        execute = self.enterContext(patch.object(os, "execvpe"))
        with self.assertRaises(ValueError):
            interactive_accounts.launch("codex", ["--resume"])
        execute.assert_not_called()
        interactive_accounts.launch("codex", ["login"])
        execute.assert_called_once()
        with patch_attr(closure, "register_proxy") as register:
            with self.assertRaises(ValueError):
                asyncio.run(codex_session_proxy.run_client(self.home, ["codex"]))
        register.assert_not_called()


class NativeStatusTests(unittest.TestCase):
    def test_only_token_free_status_is_requested_and_changed_peer_is_rejected(self) -> None:
        from websockets.sync import client

        identity = health.DaemonIdentity(42, 0, "fixture")
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        responses: list[dict[str, Any]] = [
            {"id": 1, "result": {}},
            {"id": 2, "result": {"authMethod": "chatgpt", "authToken": None, "requiresOpenaiAuth": True}},
            {"id": 3, "result": {"account": {"type": "chatgpt"}, "requiresOpenaiAuth": True}},
        ]
        connection.recv.side_effect = [json.dumps(response) for response in responses]
        with (
            patch.object(client, "unix_connect", return_value=connection),
            patch.object(health, "peer_pid", return_value=42),
            patch.object(health, "daemon_identity", return_value=identity),
        ):
            result = health.native_status(Path("/fixture"))
        self.assertEqual(result.identity, identity)
        self.assertTrue(result.account_present)
        requests = [json.loads(call.args[0]) for call in connection.send.call_args_list]
        self.assertEqual({request["method"] for request in requests}, {"initialize", "initialized", "getAuthStatus", "account/read"})
        self.assertTrue(all(not request.get("params", {}).get("includeToken") for request in requests))
        self.assertTrue(all(not request.get("params", {}).get("refreshToken") for request in requests))
        responses[-1]["result"]["account"] = None
        connection.recv.side_effect = [json.dumps(response) for response in responses]
        with (
            patch.object(client, "unix_connect", return_value=connection),
            patch.object(health, "peer_pid", return_value=42),
            patch.object(health, "daemon_identity", return_value=identity),
        ):
            self.assertFalse(health.native_status(Path("/fixture")).account_present)
        connection.recv.side_effect = [json.dumps(response) for response in responses]
        with (
            patch.object(client, "unix_connect", return_value=connection),
            patch.object(health, "peer_pid", return_value=42),
            patch.object(health, "daemon_identity", side_effect=[identity, health.DaemonIdentity(43, 0, "changed")]),
        ):
            with self.assertRaises(ValueError):
                health.native_status(Path("/fixture"))
        connection.recv.side_effect = ["[]"]
        with (
            patch.object(client, "unix_connect", return_value=connection),
            patch.object(health, "peer_pid", return_value=42),
            patch.object(health, "daemon_identity", return_value=identity),
        ):
            with self.assertRaises(ValueError):
                health.native_status(Path("/fixture"))

    def test_pid_metadata_must_match_the_live_process_start(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            directory = home / "app-server-daemon"
            directory.mkdir()
            timestamp = datetime(2026, 10, 26, 12, tzinfo=UTC)
            (directory / "daemon.pid").write_text(
                json.dumps({"pid": 42, "processIdentity": {"startSeconds": int(timestamp.timestamp()), "startMicroseconds": 123}})
            )
            completed = SimpleNamespace(stdout=timestamp.strftime("%a %b %d %H:%M:%S %Y"))
            with patch.object(health.subprocess, "run", return_value=completed):
                self.assertEqual(health.daemon_identity(home, 42).pid, 42)
                completed.stdout = timestamp.replace(second=1).strftime("%a %b %d %H:%M:%S %Y")
                with self.assertRaises(ValueError):
                    health.daemon_identity(home, 42)
            with self.assertRaises(ValueError):
                health.daemon_identity(home, 43)
            (directory / "daemon.pid").write_text("[]")
            with self.assertRaises(ValueError):
                health.daemon_identity(home, 42)


if __name__ == "__main__":
    unittest.main()
