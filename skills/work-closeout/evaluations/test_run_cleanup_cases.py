#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused no-model tests for the cleanup native-host runner."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("run_cleanup_cases.py")
FIXTURE_AUTH_CANARY = "public-fixture-auth-canary-not-a-credential"
HIDDEN_ANALYSIS_CANARY = "hidden-analysis-must-not-be-retained"
HIDDEN_PROMPT_CANARY = "hidden-prompt-must-not-be-retained"
OTHER_SESSION_CANARY = "other-session-tool-must-not-be-retained"


class CleanupRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="cleanup-runner-test-")
        self.base = Path(self.temp.name)
        self.workspace = self.base / "workspace"
        self.catalog = self.base / "catalog"
        self.outcomes = self.base / "outcomes"
        self.private = self.base / "private"
        self.auth_home = self.base / "source-auth"
        self.uv_python = self.base / "uv-python"
        for path in (
            self.workspace, self.catalog / "work-closeout", self.catalog / "references",
            self.outcomes, self.private, self.auth_home, self.uv_python,
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.catalog / "work-closeout" / "SKILL.md").write_text(
            "---\nname: work-closeout\ndescription: Close work.\n---\nFixture skill.\n",
            encoding="utf-8",
        )
        (self.catalog / "references" / "repo-cleanup.md").write_text(
            "Fixture reference.\n", encoding="utf-8"
        )
        (self.auth_home / "auth.json").write_text(
            json.dumps({"fixture_marker": FIXTURE_AUTH_CANARY}), encoding="utf-8"
        )
        self.receipt = self.base / "source.json"
        self.receipt.write_text(json.dumps({"revision": "a" * 40}), encoding="utf-8")
        self.fake = self.base / "fake-codex"
        self.fake.write_text(self.fake_cli_source(), encoding="utf-8")
        self.fake.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def fake_cli_source() -> str:
        return textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, pathlib, sys, time
            args = sys.argv[1:]
            if args == ["--version"]:
                print("codex-cli 9.9.9-test")
                raise SystemExit(0)
            if args == ["exec", "--help"]:
                print("--ignore-user-config --ignore-rules --json --output-last-message --sandbox --skip-git-repo-check --strict-config")
                raise SystemExit(0)
            assert not any(key in os.environ for key in ("GH_TOKEN", "GITHUB_TOKEN", "CODEX_GITHUB_TOKEN", "SSH_AUTH_SOCK"))
            mode = os.environ.get("CODEX_CLEANUP_FIXTURE_MODE", "success")
            marker = json.loads((pathlib.Path(os.environ["CODEX_HOME"]) / "auth.json").read_text())["fixture_marker"]
            print(marker, file=sys.stderr, flush=True)
            if mode in ("oversized", "oversized-native", "malformed"):
                pathlib.Path("rejection-mode").write_text(mode, encoding="utf-8")
            if mode == "oversized":
                sys.stdout.write(marker + "x" * (17 * 1024 * 1024))
                sys.stdout.flush()
                time.sleep(10)
            if mode == "timeout":
                time.sleep(10)
            output = pathlib.Path(args[args.index("-o") + 1])
            output.write_text("completed", encoding="utf-8")
            if mode == "malformed":
                print("not-json " + marker, flush=True)
                raise SystemExit(0)
            thread = "00000000-0000-0000-0000-000000000001"
            session = pathlib.Path(os.environ["CODEX_HOME"]) / "sessions/2026/09/13/rollout-test.jsonl"
            session.parent.mkdir(parents=True, exist_ok=True)
            cwd = args[args.index("-C") + 1] if "-C" in args else str(pathlib.Path.cwd())
            network_access = mode == "network-enabled"
            lines = [
                {"type":"session_meta","payload":{"id":thread,"cli_version":"9.9.9-test","model_provider":"openai","source":"exec"}},
                {"type":"turn_context","payload":{"model":"gpt-6-astra","effort":"high","approval_policy":"never","sandbox_policy":{"type":"workspace-write","network_access":network_access,"exclude_slash_tmp":True,"exclude_tmpdir_env_var":True},"workspace_roots":[cwd]}},
                {"type":"response_item","payload":{"type":"reasoning","summary":[{"text":"hidden-analysis-must-not-be-retained"}]}},
                {"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hidden-prompt-must-not-be-retained"}]}},
                {"type":"response_item","payload":{"type":"function_call","name":"shell","arguments":json.dumps({"cmd":"printf %s " + marker}),"call_id":"call-1"}},
                {"type":"response_item","payload":{"type":"function_call_output","call_id":"call-1","output":"tool output " + marker}},
                {"type":"response_item","payload":{"type":"custom_tool_call","call_id":"call-2","name":"apply_patch","input":"fixture patch"}},
                {"type":"response_item","payload":{"type":"custom_tool_call_output","call_id":"call-2","output":"Done"}},
                {"type":"response_item","payload":{"type":"unknown_future_record","value":"exclude me"}},
            ]
            if mode == "oversized-native":
                lines.append({"type":"response_item","payload":{"type":"function_call_output","call_id":"too-large","output":"x" * (17 * 1024 * 1024)}})
            with session.open("a", encoding="utf-8") as handle:
                handle.write("".join(json.dumps(item) + "\\n" for item in lines))
            other_session = session.with_name("rollout-other.jsonl")
            other_session.write_text("".join(json.dumps(item) + "\\n" for item in [
                {"type":"session_meta","payload":{"id":"00000000-0000-0000-0000-000000000099"}},
                {"type":"response_item","payload":{"type":"function_call","name":"shell","arguments":"other-session-tool-must-not-be-retained","call_id":"other-call"}},
            ]), encoding="utf-8")
            print(json.dumps({"type":"thread.started","thread_id":thread}))
            print(json.dumps({"type":"turn.started"}))
            print(json.dumps({"type":"item.completed","item":{"type":"command_execution","command":"git status","aggregated_output":marker,"exit_code":0,"status":"completed"}}))
            print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}))
            """
        )

    def write_case(self, *, name: str = "cleanup-two-turn", prompts: list[str] | None = None,
                   environment: dict[str, str] | None = None, marker: bool = True,
                   between_turn_files: dict[str, str] | None = None) -> tuple[Path, Path]:
        if marker:
            (self.workspace / ".cleanup-fixture").write_text(
                json.dumps({
                    "schema_version": 1,
                    "purpose": "cleanup-behavior-fixture",
                    "case": name,
                    "fixture_revision": 1,
                }),
                encoding="utf-8",
            )
        outcome = self.outcomes / f"{name}.json"
        case = self.base / f"{name}.case.json"
        value = {
            "name": name,
            "workspace": str(self.workspace),
            "catalog": str(self.catalog),
            "outcome": str(outcome),
            "source_receipt": str(self.receipt),
            "prompts": prompts or ["Inspect only.", "Proceed."],
            "environment": environment or {},
        }
        if between_turn_files is not None:
            value["between_turn_files"] = between_turn_files
        case.write_text(json.dumps(value), encoding="utf-8")
        return case, outcome

    def run_case(self, case: Path, *, timeout: float = 3) -> subprocess.CompletedProcess[str]:
        env = dict(
            os.environ,
            CODEX_HOME=str(self.auth_home),
            UV_PYTHON_INSTALL_DIR=str(self.uv_python),
            GH_TOKEN="must-be-stripped",
            SSH_AUTH_SOCK="must-be-stripped",
        )
        return subprocess.run(
            [
                sys.executable, str(SCRIPT), str(case), "--codex-bin", str(self.fake),
                "--artifact-root", str(self.base), "--private-root", str(self.private),
                "--timeout", str(timeout),
            ],
            text=True, capture_output=True, env=env, timeout=20,
        )

    def test_two_turn_run_records_sanitized_native_evidence_and_jit_change(self) -> None:
        state = self.workspace / "provider-state.txt"
        state.write_text("before\n", encoding="utf-8")
        code_home = self.workspace / "runtime-home"
        code_home.mkdir()
        case, outcome = self.write_case(
            environment={
                "CODEX_AUTOMATION_LOGIN": "fixture-automation",
                "CODE_HOME": str(code_home),
                "CLEANUP_FIXTURE_PROVIDER_STATE": str(state),
            },
            between_turn_files={"provider-state.txt": "after\n"},
        )
        result = self.run_case(case)
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(outcome.read_text(encoding="utf-8"))
        self.assertTrue(report["attribution_matches_request"])
        self.assertIsNone(report["attribution"]["served_model"])
        self.assertEqual(2, len(report["attribution"]["turn_contexts"]))
        self.assertEqual(report["catalog"]["sha256_before"], report["catalog"]["sha256_after"])
        self.assertEqual(2, len(report["turns"]))
        self.assertEqual("after\n", state.read_text(encoding="utf-8"))
        self.assertEqual("provider-state.txt", report["between_turn_files"][0]["path"])
        artifacts = Path(report["artifacts"])
        self.assertEqual("Inspect only.", (artifacts / "turn-01/prompt.txt").read_text())
        evidence_path = artifacts / "native-tool-evidence.jsonl"
        evidence_text = evidence_path.read_text(encoding="utf-8")
        evidence = [json.loads(line) for line in evidence_text.splitlines()]
        self.assertEqual(report["native_tool_evidence"]["event_count"], len(evidence))
        self.assertEqual(
            {item["payload"]["type"] for item in evidence},
            {"function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"},
        )
        self.assertIn("[redacted-auth]", evidence_text)
        self.assertNotIn(HIDDEN_ANALYSIS_CANARY, evidence_text)
        self.assertNotIn(HIDDEN_PROMPT_CANARY, evidence_text)
        self.assertNotIn(OTHER_SESSION_CANARY, evidence_text)
        self.assertNotIn("unknown_future_record", evidence_text)
        captured = outcome.read_text() + "".join(
            path.read_text(errors="replace") for path in artifacts.rglob("*") if path.is_file()
        )
        self.assertNotIn(FIXTURE_AUTH_CANARY, captured)
        self.assertIn("[redacted-auth]", (artifacts / "turn-01/stderr.log").read_text())
        self.assertFalse(any(self.private.iterdir()))

    def test_timeout_records_124_and_removes_private_home(self) -> None:
        case, outcome = self.write_case(
            prompts=["Timeout."], environment={"CODEX_CLEANUP_FIXTURE_MODE": "timeout"}
        )
        result = self.run_case(case, timeout=0.2)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual(124, json.loads(outcome.read_text())["turns"][0]["returncode"])
        self.assertFalse(any(self.private.iterdir()))

    @unittest.skipUnless(os.name == "posix", "process-group cleanup requires POSIX")
    def test_invoke_timeout_terminates_owned_descendants_after_readiness_witness(self) -> None:
        spec = importlib.util.spec_from_file_location("cleanup_runner_under_test", SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(runner)
        descendant_ready = self.workspace / "descendant-ready"
        descendant_command = (
            "from pathlib import Path; import time; "
            f"Path({str(descendant_ready)!r}).write_text('ready', encoding='utf-8'); "
            "time.sleep(10)"
        )
        command = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import subprocess, sys, time; "
                f"child = subprocess.Popen([sys.executable, '-c', {descendant_command!r}]); "
                "time.sleep(10)"
            ),
        ]
        real_popen = subprocess.Popen
        processes = []

        def witnessed_popen(*args, **kwargs):
            spawned_process = real_popen(*args, **kwargs)
            invoked = args[0] if args else kwargs.get("args")
            if invoked != command:
                return spawned_process
            processes.append(spawned_process)
            deadline = time.monotonic() + 2
            while not descendant_ready.exists() and spawned_process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.005)
            if not descendant_ready.exists():
                try:
                    os.killpg(spawned_process.pid, runner.signal.SIGKILL)
                except ProcessLookupError:
                    pass
                spawned_process.wait(timeout=2)
                self.fail("descendant did not reach readiness witness")
            return spawned_process

        group_absent = False
        try:
            with mock.patch.object(subprocess, "Popen", side_effect=witnessed_popen):
                returncode, _, _, _ = runner.invoke(
                    command,
                    "timeout proof",
                    os.environ.copy(),
                    self.workspace,
                    0.05,
                    self.base / "invoke-capture",
                )

            self.assertTrue(descendant_ready.exists(), "descendant did not reach readiness witness")
            self.assertEqual(returncode, 124)
            self.assertEqual(len(processes), 1)
            group_deadline = time.monotonic() + 2
            while time.monotonic() < group_deadline:
                try:
                    os.killpg(processes[0].pid, 0)
                except ProcessLookupError:
                    group_absent = True
                    break
                time.sleep(0.005)
            self.assertTrue(group_absent, "owned invoke process group remained after timeout cleanup")
        finally:
            for owned_process in processes:
                if not group_absent:
                    try:
                        os.killpg(owned_process.pid, runner.signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    owned_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    if not group_absent:
                        try:
                            os.killpg(owned_process.pid, runner.signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    owned_process.wait(timeout=2)

    def test_network_enabled_native_context_fails_attribution(self) -> None:
        case, outcome = self.write_case(
            name="cleanup-network-enabled",
            prompts=["Inspect."],
            environment={"CODEX_CLEANUP_FIXTURE_MODE": "network-enabled"},
        )
        result = self.run_case(case)
        self.assertEqual(1, result.returncode, result.stderr)
        report = json.loads(outcome.read_text(encoding="utf-8"))
        self.assertFalse(report["attribution_matches_request"])
        self.assertIs(report["attribution"]["turn_context"]["sandbox_policy"]["network_access"], True)
        self.assertFalse(any(self.private.iterdir()))

    @unittest.skipUnless(os.name == "posix", "process-group cleanup requires POSIX")
    def test_stop_group_observes_exit_when_signal_delivery_exhausts_deadline(self) -> None:
        spec = importlib.util.spec_from_file_location("cleanup_runner_delayed_signal", SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(runner)
        process = subprocess.Popen(
            [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE, text=True, start_new_session=True,
        )
        real_killpg = os.killpg
        clock = 0.0

        def delayed_killpg(group: int, sig: int) -> None:
            nonlocal clock
            real_killpg(group, sig)
            if sig in (runner.signal.SIGTERM, runner.signal.SIGKILL):
                if sig == runner.signal.SIGKILL:
                    process.wait(timeout=2)
                # Model a scheduler pause consuming the observation window.
                clock += 1.0

        try:
            assert process.stdout is not None
            self.assertEqual("ready\n", process.stdout.readline())
            with (
                mock.patch.object(runner.os, "killpg", side_effect=delayed_killpg),
                mock.patch.object(runner.time, "monotonic", side_effect=lambda: clock),
            ):
                runner.stop_group(process)
            with self.assertRaises(ProcessLookupError):
                real_killpg(process.pid, 0)
        finally:
            try:
                real_killpg(process.pid, runner.signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
            if process.stdout is not None:
                process.stdout.close()

    def test_unconfirmed_process_group_stop_retains_private_auth_home(self) -> None:
        case, outcome = self.write_case(name="cleanup-unconfirmed-stop", prompts=["noop"])
        spec = importlib.util.spec_from_file_location("cleanup_runner_unconfirmed_stop", SCRIPT)
        runner = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(runner)
        argv = [
            str(SCRIPT), str(case), "--codex-bin", str(self.fake),
            "--artifact-root", str(self.base), "--private-root", str(self.private),
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, {
                "CODEX_HOME": str(self.auth_home),
                "UV_PYTHON_INSTALL_DIR": str(self.uv_python),
            }),
            mock.patch.object(runner, "stop_group", side_effect=runner.ProcessCleanupError("owned Codex process group did not stop")),
            self.assertRaisesRegex(runner.ProcessCleanupError, "private auth home retained"),
        ):
            runner.main()
        self.assertTrue(any(self.private.rglob("auth.json")))
        self.assertFalse(outcome.exists())

    @unittest.skipUnless(os.name == "posix", "process-group cleanup requires POSIX")
    def test_invoke_capture_boundary_and_overflow_process_cleanup(self) -> None:
        spec = importlib.util.spec_from_file_location("cleanup_runner_capture", SCRIPT)
        assert spec is not None and spec.loader is not None
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        real_popen = subprocess.Popen
        for stream in ("stdout", "stderr", "combined"):
            for excess in (0, 1):
                with self.subTest(stream=stream, excess=excess):
                    total = runner.CAPTURE_LIMIT + excess
                    stdout_size = {"stdout": total, "stderr": 0, "combined": total // 2}[stream]
                    stderr_size = total - stdout_size
                    ready = self.workspace / f"capture-{stream}-{excess}.ready"
                    child_ready = ready.with_suffix(".child")
                    child_command = textwrap.dedent(f"""\
                        import json, os, pathlib, time
                        ready = pathlib.Path({str(child_ready)!r})
                        pending = ready.with_suffix('.pending-child')
                        pending.write_text(json.dumps({{"pid": os.getpid(), "group": os.getpgrp()}}), encoding='utf-8')
                        pending.replace(ready)
                        time.sleep(20)
                        """)
                    command = [sys.executable, "-c", textwrap.dedent(f"""\
                        import os, pathlib, subprocess, sys, time
                        if {excess} and os.getpgrp() == os.getpid():
                            subprocess.Popen([sys.executable, '-c', {child_command!r}])
                            while not pathlib.Path({str(child_ready)!r}).exists():
                                time.sleep(0.005)
                        prefix, suffix = b'{{"padding":"', b'"}}\\n'
                        if {stdout_size}:
                            sys.stdout.buffer.write(prefix + b'x' * ({stdout_size} - len(prefix) - len(suffix)) + suffix)
                            sys.stdout.buffer.flush()
                        sys.stderr.buffer.write(b'x' * {stderr_size})
                        sys.stderr.buffer.flush()
                        ready = pathlib.Path({str(ready)!r})
                        pending = ready.with_suffix('.pending')
                        pending.write_text(str(os.getpid()), encoding='utf-8')
                        pending.replace(ready)
                        time.sleep(20 if {excess} else 0.2)
                        """)]
                    processes: list[subprocess.Popen[bytes]] = []
                    descendants: dict[int, int] = {}
                    output_ready_at = 0.0

                    def witnessed_popen(*args, **kwargs):
                        nonlocal output_ready_at
                        owned_process = real_popen(*args, **kwargs)
                        processes.append(owned_process)
                        readiness_deadline = time.monotonic() + 5
                        while not ready.exists() and owned_process.poll() is None and time.monotonic() < readiness_deadline:
                            time.sleep(0.005)
                        self.assertTrue(ready.exists(), "output producer never reached readiness")
                        self.assertEqual(str(owned_process.pid), ready.read_text(encoding="utf-8"))
                        if excess:
                            self.assertEqual(owned_process.pid, os.getpgid(owned_process.pid))
                            child = json.loads(child_ready.read_text(encoding="utf-8"))
                            descendants[child["pid"]] = child["group"]
                            self.assertEqual(owned_process.pid, child["group"])
                            self.assertEqual(owned_process.pid, os.getpgid(child["pid"]))
                        output_ready_at = time.monotonic()
                        return owned_process

                    try:
                        with mock.patch.object(subprocess, "Popen", side_effect=witnessed_popen):
                            if excess:
                                with self.assertRaises(runner.RunnerError) as rejection:
                                    runner.invoke(command, "capture proof", os.environ.copy(), self.workspace, 30,
                                                  self.base / f"capture-{stream}-{excess}")
                                self.assertIs(type(rejection.exception), runner.RunnerError)
                                self.assertLess(time.monotonic() - output_ready_at, 10,
                                                "overflow waited for producer exit or invocation timeout")
                            else:
                                returncode, stdout, stderr, _ = runner.invoke(
                                    command, "capture proof", os.environ.copy(), self.workspace, 30,
                                    self.base / f"capture-{stream}-{excess}",
                                )
                                self.assertEqual(0, returncode)
                                self.assertEqual(total, len(stdout.encode()) + len(stderr.encode()))
                                if stdout:
                                    self.assertEqual(1, len(runner.json_events(stdout)))
                        self.assertEqual(1, len(processes))
                        group_absent = False
                        group_deadline = time.monotonic() + 2
                        while time.monotonic() < group_deadline:
                            try:
                                os.killpg(processes[0].pid, 0)
                            except ProcessLookupError:
                                group_absent = True
                                break
                            time.sleep(0.005)
                        self.assertTrue(group_absent, "owned invoke process group remained after capture")
                    finally:
                        for child_pid, witnessed_group in descendants.items():
                            try:
                                if os.getpgid(child_pid) == witnessed_group:
                                    os.kill(child_pid, runner.signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        for owned_process in processes:
                            try:
                                if owned_process.poll() is None:
                                    if os.getpgid(owned_process.pid) == owned_process.pid:
                                        os.killpg(owned_process.pid, runner.signal.SIGKILL)
                                    else:
                                        owned_process.kill()
                            except ProcessLookupError:
                                pass
                            owned_process.wait(timeout=2)

    def test_oversized_and_malformed_output_leave_no_raw_capture(self) -> None:
        for mode in ("oversized", "oversized-native", "malformed"):
            with self.subTest(mode=mode):
                case, outcome = self.write_case(
                    name=f"cleanup-{mode}", prompts=[mode],
                    environment={"CODEX_CLEANUP_FIXTURE_MODE": mode},
                )
                result = self.run_case(case)
                self.assertEqual(2, result.returncode, result.stderr)
                observed = self.workspace / "rejection-mode"
                self.assertTrue(observed.is_file(), result.stderr)
                self.assertEqual(mode, observed.read_text(encoding="utf-8"))
                self.assertFalse(outcome.exists())
                self.assertFalse(
                    FIXTURE_AUTH_CANARY in result.stdout + result.stderr,
                    "fixture auth echoed by runner",
                )
                for path in self.base.rglob("*"):
                    if path.is_file() and path != self.auth_home / "auth.json":
                        self.assertFalse(
                            FIXTURE_AUTH_CANARY.encode() in path.read_bytes(),
                            f"fixture auth retained in {path.relative_to(self.base)}",
                        )
                self.assertFalse(any(self.private.iterdir()))

    def test_rejects_unmarked_workspace_secret_environment_and_external_output(self) -> None:
        case, _ = self.write_case(name="cleanup-unmarked", prompts=["noop"], marker=False)
        result = self.run_case(case)
        self.assertEqual(2, result.returncode)
        self.assertIn("workspace requires", result.stderr)
        (self.workspace / ".cleanup-fixture").write_text(json.dumps({
            "schema_version": 1, "purpose": "cleanup-behavior-fixture",
            "case": "cleanup-bad-environment",
        }))
        case, _ = self.write_case(
            name="cleanup-bad-environment", prompts=["noop"],
            environment={"GH_TOKEN": "synthetic"}, marker=False,
        )
        result = self.run_case(case)
        self.assertEqual(2, result.returncode)
        self.assertIn("nonsecret allowlist", result.stderr)
        with tempfile.TemporaryDirectory(prefix="cleanup-runner-outside-") as external:
            outside = Path(external)
            sentinel = outside / "sentinel"
            sentinel.write_text("unchanged\n", encoding="utf-8")
            case, _ = self.write_case(name="cleanup-external-output", prompts=["noop"])
            case_value = json.loads(case.read_text(encoding="utf-8"))
            case_value["outcome"] = str(outside / "forbidden.json")
            case.write_text(json.dumps(case_value), encoding="utf-8")
            result = self.run_case(case)
            self.assertEqual(2, result.returncode)
            self.assertIn("under the artifact root", result.stderr)
            self.assertEqual("unchanged\n", sentinel.read_text(encoding="utf-8"))
            self.assertFalse((outside / "forbidden.json").exists())
        (self.catalog / "linked-skill").symlink_to(self.catalog / "work-closeout")
        case, _ = self.write_case(name="cleanup-catalog-symlink", prompts=["noop"])
        result = self.run_case(case)
        self.assertEqual(2, result.returncode)
        self.assertIn("catalog contains a symlink", result.stderr)
        self.assertFalse(any(self.private.iterdir()))

    def test_private_auth_cleanup_failure_is_reported(self) -> None:
        case, _ = self.write_case(name="cleanup-cleanup-failure", prompts=["noop"])
        spec = importlib.util.spec_from_file_location("cleanup_runner_test_module", SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        argv = [
            str(SCRIPT), str(case), "--codex-bin", str(self.fake),
            "--artifact-root", str(self.base), "--private-root", str(self.private),
        ]
        environment = {
            "CODEX_HOME": str(self.auth_home),
            "UV_PYTHON_INSTALL_DIR": str(self.uv_python),
        }
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, environment),
            mock.patch("shutil.rmtree", side_effect=OSError("fixture denial")),
            self.assertRaisesRegex(module.RunnerError, "private auth home cleanup failed"),
        ):
            module.main()
        self.assertTrue(any(self.private.iterdir()), "failed cleanup must remain visible")


if __name__ == "__main__":
    unittest.main()
