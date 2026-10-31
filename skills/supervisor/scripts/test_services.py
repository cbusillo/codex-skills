#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise generated service entry points with bounded offline child fixtures."""

import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any
from types import SimpleNamespace
from unittest.mock import patch

import services


class ServicesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "config.json"
        self.fixture = self.root / "child.py"
        self.data: dict[str, Any] = {
            "uv": shutil.which("uv"),
            "services": {name: {"argv": [sys.executable, str(self.fixture)],
                                "cwd": str(self.root), "restart_seconds": 0.01}
                         for name in services.SERVICES},
        }

    def save(self):
        self.config.write_text(json.dumps(self.data))

    def test_rendered_job_restarts_clean_deadlines_and_stops_on_failure(self):
        self.fixture.write_text(
            "from pathlib import Path\n"
            "p = Path('passes.txt')\n"
            "previous = p.read_text() if p.exists() else ''\n"
            "p.write_text(previous + 'pass\\n')\n"
            "raise SystemExit(4 if previous.count('pass') >= 2 else 0)\n"
        )
        self.save()
        catalog = Path(__file__).resolve().parents[3]
        output = services.render(self.config, catalog, self.root / "jobs")
        jobs = list(output.glob("*.plist"))
        self.assertEqual(len(jobs), len(self.data["services"]))
        with (output / f"{services.LABEL_PREFIX}.enqueue.plist").open("rb") as stream:
            job = plistlib.load(stream)
        result = subprocess.run(job["ProgramArguments"], cwd=job["WorkingDirectory"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((self.root / "passes.txt").read_text().splitlines(), ["pass"] * 3)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row["exit_code"] for row in events if row["event"] == "completed"],
                         [0, 0, 4])
        self.assertEqual(events[-1]["event"], "reconciliation_required")
        again = subprocess.run(job["ProgramArguments"], cwd=job["WorkingDirectory"],
                               capture_output=True, text=True, timeout=15)
        self.assertEqual(again.returncode, 1)
        self.assertEqual((self.root / "passes.txt").read_text().splitlines(), ["pass"] * 3)
        cleared = subprocess.run(
            [sys.executable, str(Path(services.__file__)), "clear", "--config",
             str(self.config), "--service", "enqueue"], capture_output=True, timeout=5,
        )
        self.assertEqual(cleared.returncode, 0)
        resumed = subprocess.run(job["ProgramArguments"], cwd=job["WorkingDirectory"],
                                 capture_output=True, timeout=15)
        self.assertEqual(resumed.returncode, 1)
        self.assertEqual(len((self.root / "passes.txt").read_text().splitlines()), 4)

    def test_running_service_and_uv_child_leave_shared_cache_prunable(self):
        self.assert_service_cache_prunable([])

    def test_optional_catalog_job_uses_existing_runner_and_preserves_failure_stop(self) -> None:
        name = services.OPTIONAL_SERVICES[-1]
        self.data["services"] = {name: dict(self.data["services"]["foreign"])}
        sender = self.root / "sender.py"
        sender.write_text("from pathlib import Path\nimport sys\np=Path('notices.txt')\np.write_text((p.read_text() if p.exists() else '') + sys.stdin.read())\n")
        self.data["services"][name]["failure_argv"] = [sys.executable, str(sender)]
        self.fixture.write_text("raise SystemExit(4)\n")
        self.save()
        output = services.render(self.config, Path(__file__).resolve().parents[3], self.root / "jobs")
        with (output / f"{services.LABEL_PREFIX}.{name}.plist").open("rb") as stream:
            job = plistlib.load(stream)
        result = subprocess.run(job["ProgramArguments"], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len((self.root / "notices.txt").read_text().splitlines()), 1)
        self.assertTrue(services.reconciliation_marker(self.config, name).exists())
        again = subprocess.run(job["ProgramArguments"], capture_output=True, text=True, timeout=15)
        self.assertEqual(again.returncode, 1)
        events = [json.loads(line) for line in again.stdout.splitlines()]
        self.assertFalse(any(event["event"] == "started" for event in events))
        self.assertEqual(len((self.root / "notices.txt").read_text().splitlines()), 1)
        cleared = subprocess.run([sys.executable, str(Path(__file__).with_name("services.py")), "clear", "--config", str(self.config),
                                  "--service", name], capture_output=True, text=True, timeout=5)
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        subprocess.run(job["ProgramArguments"], cwd=job["WorkingDirectory"], capture_output=True, timeout=15)
        self.assertEqual(len((self.root / "notices.txt").read_text().splitlines()), 2)

    def test_stale_marker_alerts_once_and_uncertain_notification_never_replays(self) -> None:
        settings = {"failure_argv": ["/existing-sender"], "cwd": str(self.root)}
        marker = self.root / "catalog.reconcile"
        marker.touch()
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("sender", 120)) as sender:
            with self.assertRaises(subprocess.TimeoutExpired):
                services.notify_failure(settings, marker, "catalog")
            services.notify_failure(settings, marker, "catalog")
            self.assertEqual(sender.call_count, 1)
        self.data["services"]["foreign"]["failure_argv"] = settings["failure_argv"]
        self.save()
        configured_marker = services.reconciliation_marker(self.config, "foreign")
        configured_marker.touch()
        marker.with_suffix(".notice.json").rename(configured_marker.with_suffix(".notice.json"))
        with patch.object(subprocess, "run") as sender:
            self.assertEqual(services.main(["clear", "--config", str(self.config), "--service", "foreign"]), 1)
            self.assertTrue(configured_marker.exists())
            self.assertEqual(services.main(["clear", "--config", str(self.config), "--service", "foreign",
                                            "--failure-delivered-url", "https://discord.com/channels/1/2/3"]), 0)
            sender.assert_not_called()

    def test_runner_alerts_on_stale_marker_without_restarting_child(self) -> None:
        self.fixture.write_text("raise SystemExit(0)\n")
        self.save()
        marker = services.reconciliation_marker(self.config, "foreign")
        marker.touch()
        with patch.object(services, "notify_failure") as notify, patch.object(services, "supervise") as supervise:
            self.assertEqual(services.main(["run", "--config", str(self.config), "--service", "foreign"]), 1)
            notify.assert_called_once()
            supervise.assert_not_called()

    def test_uv_child_with_existing_cache_flag_still_runs_and_allows_pruning(self):
        self.assert_service_cache_prunable(["--no-cache"])

    def assert_service_cache_prunable(self, cache_flags):
        self.fixture.write_text(
            "from pathlib import Path\nimport subprocess\nimport time\n"
            "Path('child-cache').write_text(subprocess.check_output(['uv', '--no-config', 'cache', 'dir'], text=True).strip())\n"
            "Path('started').touch()\ntime.sleep(60)\n")
        uv = self.data["uv"]
        self.data["services"]["foreign"]["argv"] = [
            uv, *cache_flags, "run", "--no-project", "--python", "3.12", "python", str(self.fixture)]
        self.save()
        cache = self.root / "shared-cache"
        cache.mkdir()
        output = services.render(self.config, Path(__file__).resolve().parents[3], self.root / "jobs")
        with (output / f"{services.LABEL_PREFIX}.foreign.plist").open("rb") as stream:
            job = plistlib.load(stream)
        env = {**os.environ, "UV_CACHE_DIR": str(cache), "UV_LOCK_TIMEOUT": "1"}
        env.pop("UV_NO_CACHE", None)
        runner = subprocess.Popen(job["ProgramArguments"], cwd=job["WorkingDirectory"], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            until = time.monotonic() + 10
            while not (self.root / "started").exists() and time.monotonic() < until:
                if runner.poll() is not None:
                    break
                time.sleep(0.01)
            self.assertTrue((self.root / "started").exists())
            self.assertEqual((self.root / "child-cache").read_text(), str(cache))
            subprocess.run([uv, "--no-config", "cache", "prune", "--cache-dir", str(cache)],
                           env=env, capture_output=True, check=True, timeout=10)
            self.assertIsNone(runner.poll())
        finally:
            runner.terminate()
            runner.communicate(timeout=10)

    def test_sigterm_stops_active_child_and_leaves_no_restart(self):
        self.fixture.write_text(
            "from pathlib import Path\nimport signal\nimport time\n"
            "def stop(*_):\n    Path('terminated').touch()\n    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\nPath('started').touch()\n"
            "while True: time.sleep(0.1)\n"
        )
        self.save()
        output = services.render(self.config, Path(__file__).resolve().parents[3], self.root / "jobs")
        with (output / f"{services.LABEL_PREFIX}.foreign.plist").open("rb") as stream:
            job = plistlib.load(stream)
        runner = subprocess.Popen(
            job["ProgramArguments"], cwd=job["WorkingDirectory"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(lambda: runner.kill() if runner.poll() is None else None)
        until = time.monotonic() + 5
        while not (self.root / "started").exists() and time.monotonic() < until:
            time.sleep(0.01)
        self.assertTrue((self.root / "started").exists())
        runner.send_signal(signal.SIGTERM)
        stdout, stderr = runner.communicate(timeout=5)
        self.assertEqual(runner.returncode, 0, stderr)
        self.assertTrue((self.root / "terminated").exists())
        self.assertEqual(sum(json.loads(line)["event"] == "started"
                             for line in stdout.splitlines()), 1)
        self.assertFalse(services.reconciliation_marker(self.config, "foreign").exists())

    def test_killed_child_requires_explicit_reconciliation_before_restart(self):
        self.fixture.write_text("import os\nimport signal\nos.kill(os.getpid(), signal.SIGTERM)\n")
        self.save()
        argv = [sys.executable, str(Path(services.__file__)), "run", "--config",
                str(self.config), "--service", "drive"]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(services.reconciliation_marker(self.config, "drive").exists())
        restarted = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        self.assertEqual(restarted.returncode, 1)
        self.assertNotIn("started", [json.loads(line)["event"] for line in restarted.stdout.splitlines()])

    def test_stopping_unhandled_child_retains_marker(self):
        self.fixture.write_text(
            "from pathlib import Path\nimport time\nPath('started').touch()\ntime.sleep(30)\n"
        )
        self.save()
        argv = [sys.executable, str(Path(services.__file__)), "run", "--config",
                str(self.config), "--service", "enqueue"]
        runner = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: runner.kill() if runner.poll() is None else None)
        until = time.monotonic() + 5
        while not (self.root / "started").exists() and time.monotonic() < until:
            time.sleep(0.01)
        self.assertTrue((self.root / "started").exists())
        runner.terminate()
        stdout, _stderr = runner.communicate(timeout=5)
        self.assertEqual(runner.returncode, 1)
        self.assertTrue(services.reconciliation_marker(self.config, "enqueue").exists())
        events = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(events[-1]["event"], "reconciliation_required")
        self.assertLess(events[-1]["exit_code"], 0)

    def test_duplicate_service_refuses_before_launch(self):
        self.save()
        with services.service_lock(self.config, "drive"):
            with self.assertRaises(ValueError):
                with services.service_lock(self.config, "drive"):
                    self.fail("duplicate service acquired the lock")

    def codex_source(self, identity: str) -> tuple[Path, dict[str, str]]:
        home = self.root / "account"
        transcript = home / "sessions" / f"{identity}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": identity}}) + "\n")
        return transcript, {"CODEX_HOME": str(home), "CODEX_THREAD_ID": identity, "CODEX_SESSION_ID": identity}

    def test_bind_records_actual_source_and_takeover_preserves_services(self) -> None:
        import codex_session_proxy
        self.save()
        identities = ("01970242-7941-7f0c-b461-16aa5723fb51", "01970242-7941-7f0c-b461-16aa5723fb52")
        with patch.object(codex_session_proxy, "source_endpoint"):
            for identity in identities:
                transcript, env = self.codex_source(identity)
                bound = services.bind_supervisor(self.config, environ=env)
                self.assertEqual(bound["session_id"], identity)
                self.assertNotIn("supervisor_owned", bound)
                self.assertEqual(services.supervisor_recipient(self.config), bound)
                self.assertEqual(services.load_config(self.config)["services"], self.data["services"])
        self.assertEqual(services.supervisor_recipient(self.config)["session_id"], identities[-1])
        self.assertEqual(self.config.stat().st_mode & 0o077, 0)

    def test_bind_refuses_codex_child_without_replacing_recipient(self) -> None:
        self.save()
        transcript, env = self.codex_source("01970242-7941-7f0c-b461-16aa5723fb51")
        env["CODEX_SESSION_ID"] = "01970242-7941-7f0c-b461-16aa5723fb52"
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            services.bind_supervisor(self.config, transcript, environ=env)
        self.assertEqual(self.config.read_bytes(), before)

    def test_bind_refuses_another_native_transcript(self) -> None:
        import codex_session_proxy
        self.save()
        transcript, env = self.codex_source("01970242-7941-7f0c-b461-16aa5723fb51")
        env["CODEX_THREAD_ID"] = env["CODEX_SESSION_ID"] = "01970242-7941-7f0c-b461-16aa5723fb52"
        with patch.object(codex_session_proxy, "source_endpoint"):
            with self.assertRaises(ValueError):
                services.bind_supervisor(self.config, transcript, environ=env)
        self.assertNotIn("supervisor", services.load_config(self.config))

    def test_failed_takeover_source_validation_preserves_previous_binding(self) -> None:
        import codex_session_proxy
        self.save()
        transcript, env = self.codex_source("01970242-7941-7f0c-b461-16aa5723fb51")
        with patch.object(codex_session_proxy, "source_endpoint"):
            bound = services.bind_supervisor(self.config, transcript, environ=env)
        transcript, env = self.codex_source("01970242-7941-7f0c-b461-16aa5723fb52")
        with patch.object(codex_session_proxy, "source_endpoint", side_effect=ValueError("source unavailable")):
            with self.assertRaises(ValueError):
                services.bind_supervisor(self.config, transcript, environ=env)
        self.assertEqual(services.supervisor_recipient(self.config), bound)

    def test_takeover_cannot_replace_recipient_during_notification(self) -> None:
        import codex_session_proxy
        self.save()
        transcript, env = self.codex_source("01970242-7941-7f0c-b461-16aa5723fb51")
        with patch.object(codex_session_proxy, "source_endpoint"):
            before = services.bind_supervisor(self.config, transcript, environ=env)
            with services.supervisor_lock(self.config):
                with self.assertRaises(ValueError):
                    services.bind_supervisor(self.config, transcript, environ=env)
        self.assertEqual(services.supervisor_recipient(self.config), before)

    def claude_source(self) -> SimpleNamespace:
        identity = "01970242-7941-7f0c-b461-16aa5723fb51"
        home = self.root / "claude"
        transcript = home / "projects" / "repository" / f"{identity}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"sessionId": identity, "type": "user", "message": {"content": "work"}}) + "\n")
        pane = {"session_id": "tmux:supervisor:" + "a" * 32, "window_id": "$1", "tab_id": "@2",
                "tty": "/dev/ttys001", "pid": 20, "dead": False, "backend": "tmux", "tmux_socket": "supervisor"}
        env = {"CLAUDE_CODE_SESSION_ID": identity, "CLAUDE_CONFIG_DIR": str(home)}
        return SimpleNamespace(transcript=transcript, env=env, pane=pane)

    def test_bind_claude_matches_callers_harness_ancestor_and_managed_pane(self) -> None:
        import session_liveness
        import tmux_terminal
        self.save()
        source = self.claude_source()
        transcript, env, pane = source.transcript, source.env, source.pane
        parents = {100: 50, 50: 30, 30: 20, 20: 1}
        processes = [(50, "ttys001", "/usr/local/bin/claude"), (30, "ttys001", "/usr/local/bin/claude")]
        with patch.object(services.os, "getppid", return_value=100), \
                patch.object(session_liveness, "inventory", return_value=processes), \
                patch.object(tmux_terminal, "process_parents", return_value=parents), \
                patch.object(tmux_terminal.Terminal, "list", return_value=[pane]):
            bound = services.bind_supervisor(self.config, transcript, environ=env)
        self.assertEqual(bound["pid"], processes[0][0])
        self.assertEqual(bound["tmux_session_id"], pane["session_id"])
        self.assertEqual(bound["session_id"], env["CLAUDE_CODE_SESSION_ID"])
        self.assertNotIn("supervisor_owned", bound)

    def test_bind_claude_refuses_unmanaged_or_ambiguous_terminal(self) -> None:
        import session_liveness
        import tmux_terminal
        self.save()
        source = self.claude_source()
        transcript, env, pane = source.transcript, source.env, source.pane
        with patch.object(services.os, "getppid", return_value=100), \
                patch.object(session_liveness, "inventory", return_value=[(50, "ttys001", "/usr/local/bin/claude")]), \
                patch.object(tmux_terminal, "process_parents", return_value={100: 50, 50: 20, 20: 1}):
            for rows in ([{**pane, "session_id": "tmux:supervisor:restored:%1"}],
                         [{**pane, "dead": True}], [pane, dict(pane)]):
                with self.subTest(rows=rows), patch.object(tmux_terminal.Terminal, "list", return_value=rows):
                    with self.assertRaises(ValueError):
                        services.bind_supervisor(self.config, transcript, environ=env)
        self.assertNotIn("supervisor", services.load_config(self.config))

    def test_invalid_config_cannot_render_or_run(self):
        for change in (
            lambda: self.data["services"]["enqueue"].update(argv="a shell command"),
            lambda: self.data["services"]["drive"].update(restart_seconds=0),
            lambda: self.data["services"]["foreign"].update(cwd="relative"),
        ):
            original = json.loads(json.dumps(self.data))
            change()
            self.save()
            with self.assertRaises(ValueError):
                services.load_config(self.config)
            self.data = original


if __name__ == "__main__":
    unittest.main()
