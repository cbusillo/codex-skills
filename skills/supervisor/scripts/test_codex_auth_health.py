# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp==3.14.5"]
# ///

import asyncio
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import account_choice
import codex_auth_health as health
import codex_idle_watch
import codex_session_proxy
import github_plan_closure
import interactive_accounts


class AuthHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.root / "failed"
        self.home.mkdir()
        self.database = self.home / "logs_2.sqlite"
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, "
                "level TEXT, target TEXT, process_uuid TEXT, feedback_log_body TEXT)"
            )
        self.enterContext(patch.object(health, "process_inventory", return_value=[{"pid": 42, "executable": "codex"}]))
        self.auth = self.home / "auth.json"
        self.auth.write_text("opaque synthetic credential")
        os.utime(self.auth, ns=(1, 1))
        self.account = {
            "name": "failed",
            "provider": "openai",
            "env": {"CODEX_HOME": str(self.home)},
            "match": "context_panel_label",
            "match_value": "Failed",
        }
        self.good = {**self.account, "name": "healthy", "env": {"CODEX_HOME": str(self.root / "healthy")}, "match_value": "Healthy"}
        self.config = {"accounts": [self.account, self.good]}
        self.snapshot = {
            "schemaVersion": 1,
            "accounts": [
                {"provider": "openai", "id": "openai-a", "label": "Failed"},
                {"provider": "openai", "id": "openai-b", "label": "Healthy"},
            ],
            "ranking": [{"provider": "openai", "launchOrder": ["openai-a", "openai-b"]}],
            "answers": {"useNext": [{"provider": "openai", "accountID": "openai-a"}]},
        }

    def append(self, ident: int = 1, body: str | None = None, target: str = "codex_login::auth::manager") -> None:
        body = body or (
            "Failed to refresh token: Your access token could not be refreshed because "
            "your refresh token has expired. Please log out and sign in again."
        )
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO logs VALUES (?, ?, 0, 'ERROR', ?, ?, ?)",
                (ident, ident, target, "pid:42:01234567-89ab-cdef-0123-456789abcdef", body),
            )

    def test_permanent_failure_excludes_only_its_account_before_launch(self) -> None:
        self.append()
        choice = account_choice.choose("openai", self.config, self.snapshot, None)
        self.assertEqual(choice["name"], "healthy")
        with self.assertRaises(ValueError):
            account_choice.choose("openai", self.config, self.snapshot, None, name="failed")
        self.assertEqual(account_choice.choose("openai", self.config, self.snapshot, None, name="healthy")["name"], "healthy")
        with self.assertRaises(ValueError):
            account_choice.choose_batch("openai", self.config, self.snapshot, None, count=2)
        self.snapshot["ranking"][0]["launchOrder"].append("openai-b")
        batch = account_choice.choose_batch("openai", self.config, self.snapshot, None, count=2)
        self.assertEqual([choice["name"] for choice in batch], ["healthy", "healthy"])

    def test_preserves_only_allowlisted_evidence_across_rotation_and_recovery(self) -> None:
        self.append(body="Refreshing token secret=DO-NOT-COPY")
        self.append(2)
        self.append(3, body="random secret=DO-NOT-COPY")
        self.append(4, target="unrelated::auth")
        state = health.observe(self.home)
        evidence = Path(state["evidence"])
        saved = evidence.read_text()
        self.assertNotIn("DO-NOT-COPY", saved)
        self.assertNotIn("opaque synthetic credential", saved)
        self.assertEqual([e["event"] for e in json.loads(saved)["events"]], ["refresh_attempt", "expired"])
        self.assertTrue(state["blocked"])
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
        self.database.unlink()
        self.assertTrue(health.observe(self.home)["blocked"])
        os.utime(self.auth, ns=(10_000_000_000, 10_000_000_000))
        self.assertFalse(health.observe(self.home)["blocked"])
        self.assertEqual(evidence.read_text(), saved)

    def test_old_errors_after_sign_in_do_not_block_and_network_errors_never_quarantine(self) -> None:
        self.append()
        os.utime(self.auth, ns=(10_000_000_000, 10_000_000_000))
        self.assertFalse(health.observe(self.home)["blocked"])
        self.append(20, body="Failed to refresh token: connection timeout")
        self.assertFalse(health.observe(self.home)["blocked"])
        self.assertEqual(account_choice.choose("openai", self.config, self.snapshot, None)["name"], "failed")

    def test_bad_database_is_diagnostic_and_does_not_block_healthy_launches(self) -> None:
        self.database.write_bytes(b"not sqlite")
        self.assertFalse(health.observe(self.home)["blocked"])
        self.assertIn("diagnostic", health.observe(self.home))
        self.database.unlink()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, "
                "level TEXT, target TEXT, process_uuid TEXT, feedback_log_body TEXT)"
            )
        self.append()
        self.assertTrue(health.observe(self.home)["blocked"])
        self.database.write_bytes(b"not sqlite")
        self.assertTrue(health.observe(self.home)["blocked"])

    def test_watcher_observes_without_a_worker_and_wakes_once_across_restarts(self) -> None:
        self.append()
        ledger = self.root / "ledger.json"
        ledger.write_text("[]")
        seen = {}
        notices = codex_idle_watch.poll(ledger, seen, 90, 1000, account_homes={"failed": self.home})
        self.assertEqual(len(notices), 1)
        self.assertIn("auth_failure", notices[0])
        self.append(2)
        self.assertEqual(codex_idle_watch.poll(ledger, seen, 90, 1001, account_homes={"failed": self.home}), [])
        self.enterContext(patch.object(account_choice, "load_config", return_value=self.config))
        args = SimpleNamespace(
            ledger=ledger,
            idle_seconds=90,
            harness="codex",
            deadline_seconds=1,
            delivered_state=self.root / "delivered.json",
            capacity_state=None,
            resume_capacity=False,
            once=True,
            interval=1,
        )
        first, restarted = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(first):
            codex_idle_watch.watch(args, None)
        with contextlib.redirect_stdout(restarted):
            codex_idle_watch.watch(args, None)
        self.assertIn("auth_failure", first.getvalue())
        self.assertEqual(restarted.getvalue(), "")

    def test_interactive_fallback_refuses_failed_account_but_login_is_available(self) -> None:
        self.append()
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

    def test_direct_managed_client_refuses_before_creating_ingress_or_process(self) -> None:
        self.append()
        with (
            patch.object(github_plan_closure, "register_proxy") as register,
            patch.object(codex_session_proxy.subprocess, "Popen") as spawn,
        ):
            with self.assertRaises(ValueError):
                asyncio.run(codex_session_proxy.run_client(self.home, ["codex"]))
        register.assert_not_called()
        spawn.assert_not_called()

    def test_missing_credential_generation_is_unknown_instead_of_a_permanent_trap(self) -> None:
        self.append()
        self.auth.unlink()
        result = health.observe(self.home)
        self.assertFalse(result["blocked"])
        self.assertIn("diagnostic", result)


if __name__ == "__main__":
    unittest.main()
