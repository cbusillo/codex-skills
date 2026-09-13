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

    def fake_cli_source(self) -> str:
        return textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json, os, pathlib, subprocess, sys, time
            args = sys.argv[1:]
            if args == ["--version"]:
                print("codex-cli 9.9.9-test")
                raise SystemExit(0)
            if args == ["exec", "--help"]:
                print("--ignore-user-config --ignore-rules --json --output-last-message --sandbox --skip-git-repo-check --strict-config")
                raise SystemExit(0)
            assert not any(key in os.environ for key in ("GH_TOKEN", "GITHUB_TOKEN", "CODEX_GITHUB_TOKEN", "SSH_AUTH_SOCK"))
            mode = os.environ.get("CODEX_CLEANUP_FIXTURE_MODE", "success")
            if mode == "oversized":
                sys.stdout.write("x" * (17 * 1024 * 1024))
                sys.stdout.flush()
                time.sleep(10)
            if mode == "timeout":
                child = subprocess.Popen(["sleep", "60"])
                pathlib.Path("child.pid").write_text(str(child.pid), encoding="utf-8")
                time.sleep(60)
            output = pathlib.Path(args[args.index("-o") + 1])
            output.write_text("completed", encoding="utf-8")
            if mode == "malformed":
                print("not-json", flush=True)
                raise SystemExit(0)
            thread = "00000000-0000-0000-0000-000000000001"
            session = pathlib.Path(os.environ["CODEX_HOME"]) / "sessions/2026/09/13/rollout-test.jsonl"
            session.parent.mkdir(parents=True, exist_ok=True)
            cwd = args[args.index("-C") + 1] if "-C" in args else str(pathlib.Path.cwd())
            marker = json.loads((pathlib.Path(os.environ["CODEX_HOME"]) / "auth.json").read_text())["fixture_marker"]
            lines = [
                {"type":"session_meta","payload":{"id":thread,"cli_version":"9.9.9-test","model_provider":"openai","source":"exec"}},
                {"type":"turn_context","payload":{"model":"gpt-6-astra","effort":"high","approval_policy":"never","sandbox_policy":{"type":"workspace-write","network_access":False,"exclude_slash_tmp":True,"exclude_tmpdir_env_var":True},"workspace_roots":[cwd]}},
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
            print(json.dumps({"type":"item.completed","item":{"type":"command_execution","command":"git status","aggregated_output":"","exit_code":0,"status":"completed"}}))
            print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"cached_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}))
            print(marker, file=sys.stderr)
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
            text=True, capture_output=True, env=env, timeout=20, check=False,
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

    def test_timeout_stops_owned_child_group(self) -> None:
        case, outcome = self.write_case(
            prompts=["Timeout."], environment={"CODEX_CLEANUP_FIXTURE_MODE": "timeout"}
        )
        result = self.run_case(case, timeout=0.2)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual(124, json.loads(outcome.read_text())["turns"][0]["returncode"])
        child = int((self.workspace / "child.pid").read_text())
        for _ in range(40):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("owned child process survived timeout cleanup")
        self.assertFalse(any(self.private.iterdir()))

    def test_oversized_and_malformed_output_leave_no_raw_capture(self) -> None:
        for mode in ("oversized", "oversized-native", "malformed"):
            with self.subTest(mode=mode):
                case, outcome = self.write_case(
                    name=f"cleanup-{mode}", prompts=[mode],
                    environment={"CODEX_CLEANUP_FIXTURE_MODE": mode},
                )
                result = self.run_case(case)
                self.assertEqual(2, result.returncode)
                self.assertFalse(outcome.exists())
                artifact = self.outcomes / f"cleanup-{mode}.artifacts"
                self.assertFalse(any(path.name.startswith("raw.") for path in artifact.rglob("*")))
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
            mock.patch.dict(os.environ, environment, clear=False),
            mock.patch.object(module.shutil, "rmtree", side_effect=OSError("fixture denial")),
            self.assertRaisesRegex(module.RunnerError, "private auth home cleanup failed"),
        ):
            module.main()
        self.assertTrue(any(self.private.iterdir()), "failed cleanup must remain visible")


if __name__ == "__main__":
    unittest.main()
