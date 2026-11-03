# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import supervisor


class SupervisorStartTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
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
            with self.assertRaisesRegex(ValueError, "reconcil"):
                supervisor.start(self.args)
        self.assertEqual(launch.call_count, 1)

    def test_held_service_refuses_before_launch_or_binding(self) -> None:
        (self.root / "services.json.foreign.reconcile").touch()
        with (
            patch.object(supervisor, "launch_supervisor") as launch,
            patch.object(supervisor.shutil, "which", return_value="/fixture/uv"),
        ):
            with self.assertRaisesRegex(ValueError, "reconcil"):
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

    def test_recovery_reuses_the_registered_session_without_launching_another(self) -> None:
        with (
            patch.object(supervisor, "launch_supervisor", return_value={"session_id": "native"}) as launch,
            patch.object(supervisor, "recover_supervisor", return_value={"session_id": "native"}) as recover,
            patch.object(supervisor, "ensure_foreign_service", side_effect=[ValueError("service unknown"), {"running": True}]),
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
        import codex_session_proxy
        import iterm_tab

        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                self.args.harness = harness
                supervisor.save_state(self.root / "start.json", {"status": "launching"})
                ledger = self.root / "workers.json"
                supervisor.save_state(ledger, [])
                commands = []

                def launch(
                    argv: list[str],
                    *,
                    selected_harness: str = harness,
                    observed: list = commands,
                    workers: Path = ledger,
                    **_: object,
                ) -> dict:
                    if argv[-1] == "window":
                        return {"window_id": "$1"}
                    command = Path(argv[argv.index("--command-file") + 1]).read_text().rstrip("\n")
                    words = iterm_tab.launch_words(command)
                    if selected_harness == "codex":
                        codex_session_proxy.managed_argv(self.root, words)
                    else:
                        iterm_tab.fresh_claude_session(command)
                    observed.append(words)
                    registration = Path(argv[argv.index("--ledger") + 1])
                    self.assertNotEqual(registration, workers)
                    supervisor.save_state(registration, [{"session_id": "actual-native"}])
                    return {"native_session_id": "actual-native"}

                with (
                    patch.object(supervisor, "run_json", side_effect=launch),
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


if __name__ == "__main__":
    unittest.main()
