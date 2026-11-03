# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import importlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import supervisor


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


class SupervisorStartTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.enterContext(
            patch.object(supervisor, "prepare_foreign_service", return_value={"log": str(self.root / "foreign.jsonl")})
        )
        github_identity = importlib.import_module("github_identity")

        self.enterContext(patch_attr(github_identity, "automation_login", return_value="automation[bot]"))
        self.enterContext(patch_attr(github_identity, "configured_bot_logins", return_value=()))
        self.enterContext(patch.object(supervisor.github_access, "select_login", return_value=None))
        self.args = supervisor.parser().parse_args(
            [
                "start",
                "--owner",
                "example",
                "--harness",
                "claude",
                "--miss-issue",
                "example/plans#4",
                "--state-dir",
                str(self.root),
            ]
        )

    def test_plain_install_launches_both_harnesses_without_registering_supervisor_as_worker(self) -> None:
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as directory:
                self.args.harness = harness
                self.args.state_dir = Path(directory)
                recipient = {"session_id": "native-identity", "harness": harness}
                with (
                    patch.object(supervisor, "launch_supervisor", return_value=recipient) as launch,
                    patch.object(supervisor, "ensure_foreign_service", return_value={"running": True}),
                    patch.object(supervisor.shutil, "which", return_value="/fixture/uv"),
                ):
                    result = supervisor.start(self.args)
                self.assertEqual(result["supervisor"], recipient)
                self.assertEqual(json.loads((Path(directory) / "workers.json").read_text()), [])
                self.assertEqual(launch.call_args.args[0].harness, harness)
                config = json.loads((Path(directory) / "services.json").read_text())
                self.assertIn(str(Path(directory).resolve() / "workers.json"), config["services"]["foreign"]["argv"])

    def test_existing_watcher_filters_state_and_other_services_are_preserved(self) -> None:
        config = self.root / "services.json"
        state = self.root / "existing-state.json"
        ledger = self.root / "existing-workers.json"
        original = [
            "/fixture/uv",
            "run",
            str(supervisor.SCRIPTS / "foreign_watch.py"),
            "--owner",
            "example",
            "--repo",
            "example/product",
            "--own-login",
            "automation[bot]",
            "--state",
            str(state),
            "--persistent",
        ]
        data = {
            "uv": "/fixture/uv",
            "services": {
                "foreign": {"argv": original, "cwd": str(self.root), "restart_seconds": 12},
                "catalog": {"argv": ["/fixture/catalog"], "cwd": str(self.root), "restart_seconds": 60},
            },
        }
        config.write_text(json.dumps(data))
        self.args.ledger = ledger
        with (
            patch.object(supervisor, "launch_supervisor", return_value={"session_id": "native"}),
            patch.object(supervisor, "ensure_foreign_service", return_value={}),
            patch.object(supervisor.shutil, "which", return_value="/fixture/uv"),
        ):
            supervisor.start(self.args)
        updated = json.loads(config.read_text())
        foreign = updated["services"]["foreign"]["argv"]
        self.assertEqual(foreign[: len(original)], original)
        self.assertEqual(updated["services"]["catalog"], data["services"]["catalog"])
        self.assertIn(str(ledger), foreign)
        self.assertEqual(updated["services"]["foreign"]["restart_seconds"], 12)

    def test_uncertain_launch_is_not_repeated_by_another_start(self) -> None:
        with (
            patch.object(supervisor, "launch_supervisor", side_effect=ValueError("uncertain launch")) as launch,
            patch.object(supervisor.shutil, "which", return_value="/fixture/uv"),
        ):
            with self.assertRaisesRegex(ValueError, "uncertain launch"):
                supervisor.start(self.args)
            with self.assertRaises(ValueError):
                supervisor.start(self.args)
        self.assertEqual(launch.call_count, 1)

    def test_held_service_refuses_before_launch_or_binding(self) -> None:
        supervisor.services.reconciliation_marker(self.root / "services.json", "foreign").touch()
        with (
            patch.object(supervisor, "launch_supervisor") as launch,
            patch.object(supervisor.shutil, "which", return_value="/fixture/uv"),
        ):
            with self.assertRaises(ValueError):
                supervisor.start(self.args)
        launch.assert_not_called()

    def test_wrong_owner_repair_issue_refuses_before_files_or_launch(self) -> None:
        self.args.miss_issue = "someone/plans#4"
        with self.assertRaises(ValueError):
            supervisor.start(self.args)
        self.assertFalse((self.root / "workers.json").exists())

    def test_completed_start_returns_existing_recipient_without_launching_again(self) -> None:
        recipient = {"session_id": "native-identity", "harness": "claude"}
        with (
            patch.object(supervisor, "launch_supervisor", return_value=recipient) as launch,
            patch.object(supervisor, "ensure_foreign_service", return_value={"running": True}),
            patch.object(supervisor, "verify_existing_supervisor", return_value=recipient),
            patch.object(supervisor.shutil, "which", return_value="/fixture/uv"),
        ):
            supervisor.start(self.args)
            again = supervisor.start(self.args)
        self.assertEqual(again["status"], "already_running")
        self.assertEqual(launch.call_count, 1)

    def test_recovery_dispatch_never_calls_the_launcher(self) -> None:
        with (
            patch.object(supervisor, "launch_supervisor", return_value={"session_id": "native"}) as launch,
            patch.object(supervisor, "recover_supervisor", return_value={"session_id": "native"}) as recover,
            patch.object(
                supervisor, "ensure_foreign_service", side_effect=[ValueError("service unknown"), {"running": True}]
            ),
            patch.object(supervisor.shutil, "which", return_value="/fixture/uv"),
        ):
            with self.assertRaisesRegex(ValueError, "service unknown"):
                supervisor.start(self.args)
            self.args.recover = True
            result = supervisor.start(self.args)
        self.assertEqual(result["supervisor"]["session_id"], "native")
        self.assertEqual(launch.call_count, 1)
        recover.assert_called_once()

    def test_launcher_receipt_is_bound_outside_the_worker_ledger_on_both_harnesses(self) -> None:
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                self.assert_launcher_receipt(harness)

    def assert_launcher_receipt(self, harness: str) -> None:
        import codex_session_proxy
        import iterm_tab

        self.args.harness = harness
        supervisor.save_state(self.root / "start.json", {"status": "launching"})
        ledger = self.root / "workers.json"
        supervisor.save_state(ledger, [])
        commands = []

        def launch(argv: list[str], **_: object) -> subprocess.CompletedProcess:
            if argv[-1] == "window":
                return subprocess.CompletedProcess(argv, 0, json.dumps({"window_id": "$1"}), "")
            command = Path(argv[argv.index("--command-file") + 1]).read_text().rstrip("\n")
            words = iterm_tab.launch_words(command)
            if self.args.harness == "codex":
                codex_session_proxy.managed_argv(self.root, words)
            else:
                iterm_tab.fresh_claude_session(command)
            commands.append(words)
            registration = Path(argv[argv.index("--ledger") + 1])
            self.assertNotEqual(registration, ledger)
            supervisor.save_state(registration, [{"session_id": "actual-native"}])
            return subprocess.CompletedProcess(argv, 0, json.dumps({"native_session_id": "actual-native"}), "")

        with (
            patch.object(supervisor.subprocess, "run", side_effect=launch),
            patch.object(
                supervisor.services, "bind_launched_supervisor", return_value={"session_id": "actual-native"}
            ) as bind,
        ):
            result = supervisor.launch_supervisor(
                self.args, self.root, self.root / "services.json", ledger, self.root / "state.json"
            )
        self.assertEqual(len(commands), 1)
        self.assertEqual(result["session_id"], bind.return_value["session_id"])
        self.assertEqual(json.loads(ledger.read_text()), [])

    def test_recovery_registers_the_original_failed_launcher_receipt(self) -> None:
        attempt = self.root / "attempt"
        attempt.mkdir()
        payload = {"failed_launch": {"launch": {"session_id": "retained-terminal"}, "phase": "register_ledger"}}
        supervisor.save_state(attempt / "receipt.json", payload)
        original = (attempt / "receipt.json").read_bytes()
        supervisor.save_state(attempt / "launch.json", [])
        supervisor.save_state(self.root / "start.json", {"status": "launching", "attempt": str(attempt)})
        workers = self.root / "workers.json"
        supervisor.save_state(workers, [])

        def register(argv: list[str], **_: object) -> subprocess.CompletedProcess:
            self.assertIn("register", argv)
            self.assertEqual(Path(argv[argv.index("--receipt-file") + 1]).read_bytes(), original)
            supervisor.save_state(attempt / "launch.json", [{"session_id": "original-native"}])
            return subprocess.CompletedProcess(argv, 0, json.dumps({"native_session_id": "original-native"}), "")

        with (
            patch.object(supervisor.subprocess, "run", side_effect=register) as commands,
            patch.object(
                supervisor.services, "bind_launched_supervisor", return_value={"session_id": "original-native"}
            ),
        ):
            result = supervisor.recover_supervisor(self.root, self.root / "services.json", workers)
        self.assertEqual(result["session_id"], "original-native")
        self.assertEqual(commands.call_count, 1)
        self.assertEqual((attempt / "receipt.json").read_bytes(), original)
        self.assertFalse((attempt / "launch.json").exists())

    def test_certainly_unstarted_preflight_can_be_retried_without_recovery(self) -> None:
        failed = {"mutation_started": False, "launched": [], "failed_launch": {"phase": "prepare_launches"}}
        with (
            patch.object(
                supervisor.subprocess,
                "run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, json.dumps({"window_id": "$1"}), ""),
                    subprocess.CompletedProcess([], 1, json.dumps(failed), "missing CLI"),
                ],
            ),
            patch.object(supervisor.shutil, "which", return_value="/fixture/uv"),
        ):
            with self.assertRaises(supervisor.LaunchCommandError):
                supervisor.start(self.args)
        self.assertEqual(json.loads((self.root / "start.json").read_text())["status"], "not_started")
        with (
            patch.object(supervisor, "launch_supervisor", return_value={"session_id": "new-native"}) as launch,
            patch.object(supervisor, "ensure_foreign_service", return_value={}),
            patch.object(supervisor.shutil, "which", return_value="/fixture/uv"),
        ):
            result = supervisor.start(self.args)
        self.assertEqual(result["supervisor"]["session_id"], "new-native")
        launch.assert_called_once()

    def test_equals_form_paths_and_automation_filters_are_preserved(self) -> None:
        flags = self.root / "flags.txt"
        state = self.root / "state with spaces.json"
        flags.write_text(f"--owner example --state='{state}'")
        self.args.watch_args_file = flags
        with (
            patch.object(supervisor, "launch_supervisor", return_value={"session_id": "native"}),
            patch.object(supervisor, "ensure_foreign_service", return_value={}),
            patch.object(supervisor.shutil, "which", return_value="/fixture/uv"),
        ):
            result = supervisor.start(self.args)
        argv = json.loads((self.root / "services.json").read_text())["services"]["foreign"]["argv"]
        self.assertEqual(result["state"], str(state))
        self.assertNotIn("--state", argv)
        self.assertIn("automation[bot]", argv)

    def test_recovery_after_registration_does_not_register_again(self) -> None:
        attempt = self.root / "attempt"
        attempt.mkdir()
        supervisor.save_state(attempt / "receipt.json", {"failed_launch": {"launch": {"session_id": "terminal"}}})
        supervisor.save_state(attempt / "registered.json", {"native_session_id": "retained-native"})
        supervisor.save_state(attempt / "launch.json", [{"session_id": "retained-native"}])
        supervisor.save_state(self.root / "start.json", {"attempt": str(attempt), "status": "launching"})
        ledger = self.root / "workers.json"
        supervisor.save_state(ledger, [])
        with (
            patch.object(supervisor.subprocess, "run") as launch,
            patch.object(
                supervisor.services, "bind_launched_supervisor", return_value={"session_id": "retained-native"}
            ),
        ):
            result = supervisor.recover_supervisor(self.root, self.root / "services.json", ledger)
        self.assertEqual(result["session_id"], "retained-native")
        launch.assert_not_called()

    def test_failed_launch_recovery_requires_exact_dead_or_absent_terminal(self) -> None:
        import tmux_terminal

        attempt = self.root / "attempt"
        attempt.mkdir()
        workers = self.root / "workers.json"
        supervisor.save_state(workers, [])
        launch = {
            "session_id": "tmux:fixture:identity",
            "pid": 44,
            "pane_id": "%2",
            "tab_id": "@2",
            "tmux_socket": "fixture",
        }
        supervisor.save_state(attempt / "receipt.json", {"failed_launch": {**launch, "launch": launch}})
        supervisor.save_state(attempt / "launch.json", [])
        for rows in (
            [{**launch, "dead": True}],
            [],
            [{**launch, "pid": 55, "dead": True}],
            [{**launch, "dead": False}],
        ):
            supervisor.save_state(self.root / "start.json", {"attempt": str(attempt), "status": "launching"})
            with (
                self.subTest(rows=rows),
                patch.object(tmux_terminal.Terminal, "list", return_value=rows),
                patch.object(supervisor, "run_json", side_effect=ValueError("still blocked")) as register,
            ):
                if not rows or rows[0]["dead"] and rows[0]["pid"] == launch["pid"]:
                    self.assertEqual(
                        supervisor.recover_supervisor(self.root, self.root / "services.json", workers)["status"],
                        "not_started",
                    )
                    register.assert_not_called()
                else:
                    with self.assertRaises(ValueError):
                        supervisor.recover_supervisor(self.root, self.root / "services.json", workers)
                    self.assertEqual(json.loads((self.root / "start.json").read_text())["status"], "launching")
        with patch.object(tmux_terminal.Terminal, "list", side_effect=ValueError("inventory unavailable")):
            with self.assertRaises(ValueError):
                supervisor.recover_supervisor(self.root, self.root / "services.json", workers)

    def test_retry_after_unstarted_takeover_still_requires_takeover_for_existing_binding(self) -> None:
        supervisor.save_state(self.root / "start.json", {"status": "not_started"})
        with (
            patch.object(supervisor.services, "load_config", return_value={"supervisor": {"session_id": "bound"}}),
            patch.object(supervisor, "launch_supervisor") as launch,
        ):
            (self.root / "services.json").touch()
            with self.assertRaises(ValueError):
                supervisor.start(self.args)
        launch.assert_not_called()

    def test_ended_supervisor_generation_can_restart_but_unknown_liveness_cannot(self) -> None:
        with (
            patch.object(supervisor, "launch_supervisor", return_value={"session_id": "first"}),
            patch.object(supervisor, "ensure_foreign_service", return_value={}),
            patch.object(supervisor.shutil, "which", return_value="/fixture/uv"),
        ):
            supervisor.start(self.args)
        with (
            patch.object(supervisor, "verify_existing_supervisor", side_effect=ValueError("old process ended")),
            patch.object(supervisor.services, "supervisor_recipient", return_value={"pid": 50}),
            patch.object(supervisor, "generation_ended", return_value=False),
            patch.object(supervisor, "launch_supervisor") as launch,
        ):
            with self.assertRaises(ValueError):
                supervisor.start(self.args)
            launch.assert_not_called()
        with (
            patch.object(supervisor, "verify_existing_supervisor", side_effect=ValueError("old process ended")),
            patch.object(supervisor.services, "supervisor_recipient", return_value={"pid": 50}),
            patch.object(supervisor, "generation_ended", return_value=True),
            patch.object(supervisor, "launch_supervisor", return_value={"session_id": "successor"}) as launch,
            patch.object(supervisor, "ensure_foreign_service", return_value={}),
            patch.object(supervisor.shutil, "which", return_value="/fixture/uv"),
        ):
            self.assertEqual(supervisor.start(self.args)["supervisor"]["session_id"], "successor")
            launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
