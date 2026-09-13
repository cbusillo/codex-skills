#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Offline integration tests using executable synthetic Codex hosts; no inference."""

from __future__ import annotations

import errno
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
import unittest
from itertools import count
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch
from urllib.parse import urlunsplit


SCRIPT = Path(__file__).with_name("local_codex_agent.py")
SPEC = importlib.util.spec_from_file_location("local_codex_agent", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)

FAKE_HOST = r'''
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

options = OPTIONS
record = Path(options["record"])
stage = "version" if sys.argv[1:] == ["--version"] else "help" if sys.argv[1:] == ["exec", "--help"] else "exec"
snapshot = {"argv": sys.argv[1:], "env": dict(os.environ), "pid": os.getpid()}
snapshot["blocked_signals"] = list(signal.pthread_sigmask(signal.SIG_BLOCK, []))
if stage == "version":
    record.with_suffix(".version.json").write_text(json.dumps(snapshot))
    print(options.get("version", "codex-cli synthetic-test"))
    raise SystemExit(0)
if stage == "help":
    record.with_suffix(".help.json").write_text(json.dumps(snapshot))
    print(options.get("help", "--ephemeral --ignore-user-config --sandbox --cd --config --json --output-last-message --skip-git-repo-check --color"))
    raise SystemExit(0)

parser = argparse.ArgumentParser()
parser.add_argument("exec")
parser.add_argument("prompt")
parser.add_argument("--ephemeral", action="store_true")
parser.add_argument("--ignore-user-config", action="store_true")
parser.add_argument("--skip-git-repo-check", action="store_true")
parser.add_argument("--json", action="store_true")
parser.add_argument("--color")
parser.add_argument("-C", required=True)
parser.add_argument("-s", required=True)
parser.add_argument("--output-last-message", type=Path, required=True)
parser.add_argument("-c", action="append", default=[])
args = parser.parse_args()
assert args.exec == "exec" and args.prompt == "-"
assert args.ephemeral and args.ignore_user_config and args.json
snapshot["settings"] = tomllib.loads("\n".join(args.c))
host_home = Path(os.environ["CODEX_HOME"])
snapshot["config_text"] = (host_home / "config.toml").read_text()
snapshot["config_mode"] = (host_home / "config.toml").stat().st_mode & 0o777
snapshot["auth_exists"] = (host_home / "auth.json").exists()
snapshot["prompt"] = sys.stdin.read()
snapshot["sandbox"] = args.s
snapshot["workdir"] = args.C
record.with_suffix(".exec.json").write_text(json.dumps(snapshot))
mode = options.get("mode", "success")
if mode in {"timeout", "surviving_child", "interrupt"}:
    child_source = "import os,signal,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path(" + repr(str(record.with_suffix(".child"))) + ").write_text(str(os.getpid()));time.sleep(60)"
    subprocess.Popen([sys.executable, "-c", child_source])
    while not record.with_suffix(".child").exists():
        time.sleep(0.01)
    if mode != "surviving_child":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)

if mode == "output_limit":
    sys.stdout.write("x" * (17 * 1024 * 1024))
    raise SystemExit(0)
message = "LOCAL_AGENT_OK"
if mode == "blank":
    message = " \n"
if mode == "token_echo":
    message += " " + os.environ["LOCAL_CODEX_AGENT_ENDPOINT_TOKEN"]
    print(message, file=sys.stderr)
if mode != "missing_final":
    args.output_last_message.write_text(message)
if mode == "invalid_json":
    print("invalid json")
else:
    print(json.dumps({"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": message}}))
    if mode == "failed_event":
        print(json.dumps({"type": "turn.failed", "error": {"message": "synthetic failure"}}))
    elif mode != "missing_completed":
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
raise SystemExit(7 if mode == "nonzero" else 0)
'''


SIGNAL_BOUNDARY_DRIVER = r'''
import os
import signal
import sys
import time
from pathlib import Path

options = OPTIONS
sys.path.insert(0, options["script_dir"])
import local_codex_agent as agent

real_popen = agent.subprocess.Popen
real_killpg = agent.os.killpg
active_pid = None
injected = False

def inject():
    global injected
    injected = True
    Path(options["boundary_marker"]).write_text(options["boundary"])
    os.kill(os.getpid(), options["signum"])
    # A second, different signal must not interrupt cleanup either.
    other = signal.SIGINT if options["signum"] == signal.SIGTERM else signal.SIGTERM
    os.kill(os.getpid(), other)

def spawn(*args, **kwargs):
    global active_pid
    process = real_popen(*args, **kwargs)
    if "-C" in args[0]:
        active_pid = process.pid
        if options["boundary"] == "spawn":
            deadline = time.monotonic() + 5
            while not Path(options["child_marker"]).exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError("synthetic child did not start")
                time.sleep(0.01)
            # Deliver after a real spawn but before Popen returns to run_process.
            inject()
    return process

def killpg(pgid, signum):
    result = real_killpg(pgid, signum)
    if options["boundary"] == "cleanup" and pgid == active_pid and signum == signal.SIGTERM and not injected:
        # Deliver while real cleanup still has a TERM-resistant child to stop.
        inject()
    return result

agent.subprocess.Popen = spawn
agent.os.killpg = killpg
raise SystemExit(agent.main(sys.argv[1:]))
'''


class LocalCodexAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="test-local-codex-agent-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        self.config = self.root / "local-llm.yaml"
        self.config.write_text("{}", encoding="utf-8")
        self.record = self.root / "record"
        self.host = self.root / "synthetic-host"
        self.write_host()

    def write_host(self, **options: object) -> None:
        options["record"] = str(self.record)
        self.host.write_text(
            f"#!{sys.executable}\n" + textwrap.dedent(FAKE_HOST).replace("OPTIONS", repr(options), 1),
            encoding="utf-8",
        )
        self.host.chmod(0o700)

    def command(self, *arguments: str, host: str = "codex", model: bool = True) -> list[str]:
        return [
            sys.executable, str(SCRIPT), "--host", host, "--host-bin", str(self.host),
            "--config", str(self.config), "-C", str(self.workdir),
            *(["--model", "synthetic-local-model"] if model else []), *arguments,
        ]

    def invoke(self, *arguments: str, env: dict[str, str] | None = None, host: str = "codex", model: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*arguments, host=host, model=model), input="synthetic private prompt\n",
            capture_output=True, text=True, timeout=15, env=env,
        )

    def observed(self, stage: str = "exec") -> dict[str, Any]:
        return json.loads(self.record.with_suffix(f".{stage}.json").read_text())

    def endpoint_config(self, **changes: object) -> None:
        endpoint: dict[str, Any] = {
            "provider": "lm_studio", "base_url": "http://127.0.0.1:1234/v1",
            "locality": "localhost", "trust": "private_local", "enabled": True,
        }
        endpoint.update(changes)
        self.config.write_text(json.dumps({"default_endpoint": "chosen", "endpoints": {"chosen": endpoint}}))

    def assert_stopped(self, pid: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], text=True, capture_output=True)
            if status.returncode or not status.stdout.strip() or status.stdout.strip().startswith("Z"):
                return
            time.sleep(0.025)
        self.fail(f"owned synthetic process {pid} is still running")

    def assert_cleaned_run(self) -> None:
        observed = self.observed()
        self.assert_stopped(observed["pid"])
        self.assert_stopped(int(self.record.with_suffix(".child").read_text()))
        self.assertFalse(Path(observed["env"]["CODEX_HOME"]).exists())

    def test_explicit_model_exec_uses_stdin_and_a_unique_responses_provider(self) -> None:
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "LOCAL_AGENT_OK\n")
        observed = self.observed()
        self.assertEqual(observed["prompt"], "synthetic private prompt\n")
        self.assertNotIn("synthetic private prompt", " ".join(observed["argv"]))
        self.assertNotIn("synthetic private prompt", result.stderr)
        self.assertEqual(observed["sandbox"], "read-only")
        self.assertEqual(observed["workdir"], str(self.workdir))
        settings = observed["settings"]
        provider = settings["model_provider"]
        self.assertTrue(provider.startswith("local_codex_agent_"))
        self.assertNotIn(provider, {"lmstudio", "openai", "ollama"})
        self.assertEqual(settings["model"], "synthetic-local-model")
        self.assertEqual(settings["model_providers"][provider]["wire_api"], "responses")
        self.assertFalse(settings["model_providers"][provider]["requires_openai_auth"])
        self.assertEqual(settings, tomllib.loads(observed["config_text"]))
        self.assertEqual(observed["config_mode"], 0o600)
        self.assertFalse(observed["auth_exists"])
        self.assertNotIn(signal.SIGINT, observed["blocked_signals"])
        self.assertNotIn(signal.SIGTERM, observed["blocked_signals"])
        summary = json.loads(result.stderr.splitlines()[0])["local_codex_agent"]
        self.assertEqual(summary["host_version"], "codex-cli synthetic-test")
        self.assertIsNone(summary["served_model"])
        self.assertIsNone(summary["configured_context_window"])
        self.assertFalse(Path(observed["env"]["CODEX_HOME"]).exists())

    def test_lab_host_is_explicit_and_uses_an_isolated_lab_home(self) -> None:
        self.write_host(version="codex-lab synthetic-test")
        result = self.invoke(host="codex-lab")
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = self.observed()
        self.assertEqual(observed["env"]["CODEX_LAB_HOME"], observed["env"]["CODEX_HOME"])
        self.assertIn('"host": "codex-lab"', result.stderr)

    def test_environment_filters_auth_proxy_and_profiles_but_keeps_runtime_and_certs(self) -> None:
        environment = os.environ.copy()
        environment.update({
            "CODEX_HOME": str(self.root / "real-codex"),
            "CODEX_LAB_HOME": str(self.root / "real-lab"),
            "CODE_HOME": str(self.root / "real-code"),
            "OPENAI_API_KEY": "synthetic-cloud-key", "CODEX_API_KEY": "synthetic-codex-key",
            "CODEX_ACCESS_TOKEN": "synthetic-access-token", "CODEX_MODEL": "cloud-model",
            "OPENAI_BASE_URL": "https://cloud.example.invalid", "HTTP_PROXY": "http://proxy.example.invalid",
            "AWS_PROFILE": "cloud-account", "ZDOTDIR": str(self.root / "shell-config"),
            "SSH_AUTH_SOCK": str(self.root / "ssh-agent"),
            "SSL_CERT_FILE": str(self.root / "approved-ca.pem"), "LC_TIME": "C",
        })
        result = self.invoke(env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        child = self.observed()["env"]
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_MODEL", "OPENAI_BASE_URL", "HTTP_PROXY", "AWS_PROFILE", "ZDOTDIR", "SSH_AUTH_SOCK"):
            self.assertNotIn(key, child)
        for key in ("PATH", "SSL_CERT_FILE", "LC_TIME"):
            self.assertEqual(child[key], environment[key])
        for key in ("HOME", "CODEX_HOME", "CODEX_LAB_HOME", "CODE_HOME", "XDG_CONFIG_HOME"):
            self.assertNotEqual(child[key], environment.get(key))
        self.assertEqual(environment["CODE_HOME"], str(self.root / "real-code"))

    def test_token_is_remapped_not_given_to_probes_or_persisted_and_is_redacted(self) -> None:
        self.endpoint_config(token_env="OPENAI_API_KEY")
        self.write_host(mode="token_echo")
        token = "synthetic-endpoint-secret-123"
        result = self.invoke("--json", env={**os.environ, "OPENAI_API_KEY": token})
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = self.observed()
        self.assertEqual(observed["env"][agent.TOKEN_KEY], token)
        self.assertNotIn("OPENAI_API_KEY", observed["env"])
        self.assertNotIn(token, observed["config_text"])
        self.assertNotIn(token, " ".join(observed["argv"]))
        for stage in ("version", "help"):
            self.assertNotIn(agent.TOKEN_KEY, self.observed(stage)["env"])
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertIn("[redacted-endpoint-token]", result.stdout)
        for line in result.stdout.splitlines():
            json.loads(line)

    def test_private_role_uses_its_configured_endpoint_and_context(self) -> None:
        self.endpoint_config(base_url="https://local.example.invalid/v1", locality="trusted_lan")
        config = json.loads(self.config.read_text())
        config["model_roles"] = {"chosen_role": {"primary": "explicit-role-model", "endpoint": "chosen", "load": {"context_length": 32768}}}
        self.config.write_text(json.dumps(config))
        result = self.invoke("--role", "chosen_role", model=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = self.observed()["settings"]
        self.assertEqual(settings["model"], "explicit-role-model")
        self.assertEqual(settings["model_context_window"], 32768)
        self.assertEqual(settings["model_providers"][settings["model_provider"]]["base_url"], "https://local.example.invalid/v1")
        self.assertNotIn("local.example.invalid", result.stderr)
        self.assertIn("[redacted:trusted_lan]", result.stderr)

    def test_unicode_model_and_explicit_context_override_are_usable_toml(self) -> None:
        result = self.invoke("--model", "synthetic-model-\U0001f9ea", "--context-window", "4096")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.observed()["settings"]["model"], "synthetic-model-\U0001f9ea")
        self.assertEqual(self.observed()["settings"]["model_context_window"], 4096)

    def test_token_redaction_preserves_json_for_escaped_and_keyword_values(self) -> None:
        self.endpoint_config(token_env="SYNTHETIC_ENDPOINT_TOKEN")
        self.write_host(mode="token_echo")
        for token in ('synthetic-"escaped\\token', "true"):
            with self.subTest(token=token):
                result = self.invoke("--json", env={**os.environ, "SYNTHETIC_ENDPOINT_TOKEN": token})
                self.assertEqual(result.returncode, 0, result.stderr)
                summary = json.loads(result.stderr.splitlines()[0])["local_codex_agent"]
                self.assertTrue(summary["uses_token_env"])
                events = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual(events[0]["item"]["text"], "LOCAL_AGENT_OK [redacted-endpoint-token]")

    def test_public_role_does_not_supply_a_model(self) -> None:
        result = self.invoke("--role", "rollout_scout", model=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown model role", result.stderr)
        self.assertFalse(self.record.with_suffix(".version.json").exists())

    def test_missing_explicit_config_fails_before_any_host_probe(self) -> None:
        self.config.unlink()
        output = self.root / "last.txt"
        output.write_text("previous result")
        result = self.invoke("--output-last-message", str(output))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("explicit --config", result.stderr)
        self.assertFalse(self.record.with_suffix(".version.json").exists())
        self.assertEqual(output.read_text(), "previous result")

    def test_omitted_default_config_can_be_absent(self) -> None:
        arguments = agent.parse_args(["--host", "codex", "--model", "synthetic-local-model"])
        with patch.dict(vars(agent), {"DEFAULT_CONFIG": self.root / "absent-default.yaml"}):
            settings, _, _ = agent.build_settings(arguments)
        self.assertEqual(settings["model"], "synthetic-local-model")

    def test_invalid_output_directory_fails_before_any_host_probe(self) -> None:
        parent_file = self.root / "parent-file"
        parent_file.write_text("preserved parent file")
        output_directory = self.root / "existing-directory"
        output_directory.mkdir()
        sentinel = output_directory / "preserved.txt"
        sentinel.write_text("preserved directory content")
        for output in (self.root / "absent-directory" / "last.txt", parent_file / "last.txt", output_directory):
            with self.subTest(output=output):
                result = self.invoke("--output-last-message", str(output))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--output-last-message", result.stderr)
                self.assertFalse(self.record.with_suffix(".version.json").exists())
                self.assertEqual(parent_file.read_text(), "preserved parent file")
                self.assertEqual(sentinel.read_text(), "preserved directory content")

    def test_rejects_unsafe_endpoint_before_starting_host(self) -> None:
        # Build the intentionally invalid synthetic URL, as the public-safety
        # validator's own fixtures do; no real credential is used here.
        credentialed_url = urlunsplit(("http", "name:synthetic-secret@127.0.0.1:1234", "/v1", "", ""))
        cases = (
            {"locality": "cloud", "base_url": "https://cloud.example.invalid/v1"},
            {"locality": "mistyped"}, {"locality": []}, {"enabled": False}, {"enabled": "yes"},
            {"base_url": "https://cloud.example.invalid/v1"},
            {"base_url": credentialed_url},
            {"base_url": "http://127.0.0.1:1234/v1?token=synthetic-secret"},
            {"base_url": "file:///tmp/model"}, {"base_url": "http://127.0.0.1:70000/v1"},
            {"base_url": None}, {"base_url": ""}, {"base_url": "http://127.0.0.1:1234/invalid path"},
            {"locality": "trusted_lan", "trust": "external"},
            {"token_env": "MISSING_SYNTHETIC_ENDPOINT_TOKEN"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.endpoint_config(**changes)
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("synthetic-secret", result.stderr)
                self.assertFalse(self.record.with_suffix(".version.json").exists())

    def test_cli_base_url_refuses_cloud_without_a_host_probe(self) -> None:
        result = self.invoke("--base-url", "https://cloud.example.invalid/v1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no cloud fallback", result.stderr)
        self.assertFalse(self.record.with_suffix(".version.json").exists())

    def test_output_is_published_only_after_success_and_json_is_valid(self) -> None:
        output = self.root / "last.txt"
        output.write_text("previous good result")
        result = self.invoke("--json", "--output-last-message", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(), "LOCAL_AGENT_OK")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(events[-1]["type"], "turn.completed")
        for mode, diagnostic in (
            ("blank", "nonblank final"), ("missing_final", "final message file"),
            ("missing_completed", "completed turn"), ("failed_event", "failed turn"),
            ("nonzero", "status 7"), ("invalid_json", "invalid exec JSONL"),
        ):
            with self.subTest(mode=mode):
                self.write_host(mode=mode)
                result = self.invoke("--output-last-message", str(output))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)
                self.assertEqual(output.read_text(), "LOCAL_AGENT_OK")

    def test_each_run_uses_a_fresh_home_and_provider(self) -> None:
        first = self.invoke()
        self.assertEqual(first.returncode, 0, first.stderr)
        previous = self.observed()
        second = self.invoke()
        self.assertEqual(second.returncode, 0, second.stderr)
        current = self.observed()
        self.assertNotEqual(previous["env"]["CODEX_HOME"], current["env"]["CODEX_HOME"])
        self.assertNotEqual(previous["settings"]["model_provider"], current["settings"]["model_provider"])
        self.assertFalse(Path(previous["env"]["CODEX_HOME"]).exists())
        self.assertFalse(Path(current["env"]["CODEX_HOME"]).exists())

    def test_timeout_kills_term_resistant_descendants_and_preserves_previous_output(self) -> None:
        self.write_host(mode="timeout")
        output = self.root / "last.txt"
        output.write_text("previous result")
        started = time.monotonic()
        result = self.invoke("--max-seconds", "0.4", "--output-last-message", str(output))
        self.assertLess(time.monotonic() - started, 6)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stderr)
        self.assertEqual(output.read_text(), "previous result")
        self.assert_cleaned_run()

    def test_success_cleans_up_a_descendant_that_outlived_the_host(self) -> None:
        self.write_host(mode="surviving_child")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_stopped(int(self.record.with_suffix(".child").read_text()))

    def test_sigterm_stops_owned_group_and_returns_interrupted_status(self) -> None:
        self.write_host(mode="interrupt")
        with subprocess.Popen(self.command("synthetic prompt"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            try:
                deadline = time.monotonic() + 6
                while not self.record.with_suffix(".child").exists() and time.monotonic() < deadline:
                    time.sleep(0.025)
                self.assertTrue(self.record.with_suffix(".child").exists())
                process.send_signal(signal.SIGTERM)
                _, stderr = process.communicate(timeout=6)
                self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr)
                self.assertIn("interrupted", stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.assert_stopped(self.observed()["pid"])
        self.assert_stopped(int(self.record.with_suffix(".child").read_text()))

    def test_interrupts_at_spawn_and_cleanup_boundaries_do_not_orphan_processes(self) -> None:
        for boundary in ("spawn", "cleanup"):
            for signum in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(boundary=boundary, signum=signum):
                    self.record = self.root / f"record-{boundary}-{signum}"
                    self.write_host(mode="interrupt" if boundary == "spawn" else "surviving_child")
                    output = self.root / "last.txt"
                    output.write_text("previous result")
                    driver = self.root / "signal-boundary-driver.py"
                    options = {
                        "script_dir": str(SCRIPT.parent.resolve()), "boundary": boundary,
                        "signum": int(signum), "child_marker": str(self.record.with_suffix(".child")),
                        "boundary_marker": str(self.record.with_suffix(".boundary")),
                    }
                    driver.write_text(textwrap.dedent(SIGNAL_BOUNDARY_DRIVER).replace("OPTIONS", repr(options), 1))
                    try:
                        result = subprocess.run(
                            [sys.executable, str(driver), *self.command("--output-last-message", str(output), "synthetic prompt")[2:]],
                            text=True, capture_output=True, timeout=12,
                        )
                        self.assertEqual(result.returncode, 128 + signum, result.stderr)
                        self.assertIn("interrupted", result.stderr)
                        self.assertEqual(self.record.with_suffix(".boundary").read_text(), boundary)
                        self.assertEqual(output.read_text(), "previous result")
                        self.assert_cleaned_run()
                    finally:
                        # Keep a failed regression test from leaving its synthetic child.
                        if self.record.with_suffix(".exec.json").exists() and self.record.with_suffix(".child").exists():
                            pgid = self.observed()["pid"]
                            child_pid = int(self.record.with_suffix(".child").read_text())
                            try:
                                if os.getpgid(child_pid) == pgid:
                                    os.killpg(pgid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass

    def test_output_limit_fails_without_publishing_output(self) -> None:
        self.write_host(mode="output_limit")
        output = self.root / "last.txt"
        output.write_text("previous result")
        for iteration in range(20):
            with self.subTest(iteration=iteration):
                result = self.invoke("--output-last-message", str(output))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("output limit", result.stderr)
                self.assertFalse(result.stdout)
                self.assertEqual(output.read_text(), "previous result")
                observed = self.observed()
                self.assert_stopped(observed["pid"])
                self.assertFalse(Path(observed["env"]["CODEX_HOME"]).exists())

    def test_unconfirmed_process_exit_is_a_cleanup_failure(self) -> None:
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired("synthetic-host", 0.5)
        kill_group = Mock()
        with patch.dict(vars(agent.os), {"killpg": kill_group}):
            with self.assertRaises(agent.LocalCodexAgentError):
                agent.stop_process_group(process)
        self.assertEqual(kill_group.call_args.args[1], signal.SIGKILL)
        self.assertGreater(process.wait.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(process.wait.call_args.kwargs["timeout"], 1)

    def test_cleanup_retries_permission_races_at_each_signal_stage(self) -> None:
        for fault_signal in (signal.SIGTERM, 0, signal.SIGKILL):
            with self.subTest(fault_signal=fault_signal):
                process = Mock(pid=12345)
                attempts = 0

                def poll() -> int | None:
                    return 0 if attempts >= 3 else None

                def kill_group(_pgid: int, requested_signal: int) -> None:
                    nonlocal attempts
                    if requested_signal == fault_signal:
                        attempts += 1
                        if attempts <= 3:
                            raise PermissionError(errno.EPERM, "synthetic exit/reap race")
                        raise ProcessLookupError(errno.ESRCH, "synthetic group has exited")

                process.poll.side_effect = poll
                process.wait.return_value = 0
                clock = Mock(side_effect=count(step=0.01))
                with (
                    patch.dict(vars(agent.os), {"killpg": kill_group}),
                    patch.dict(vars(agent.time), {"monotonic": clock, "sleep": Mock()}),
                ):
                    agent.stop_process_group(process)
                self.assertEqual(attempts, 4)
                self.assertGreaterEqual(process.poll.call_count, 4)
                process.wait.assert_called_once_with(timeout=agent.CLEANUP_SECONDS)
                self.assertLessEqual(clock.call_count, 110)

    def test_cleanup_preserves_persistent_permission_denial_even_after_leader_exit(self) -> None:
        for fault_signal in (signal.SIGTERM, 0, signal.SIGKILL):
            for leader_status in (None, 0):
                with self.subTest(fault_signal=fault_signal, leader_status=leader_status):
                    process = Mock(pid=12345)
                    process.poll.return_value = leader_status
                    denial = PermissionError(errno.EPERM, "synthetic genuine permission denial")

                    def kill_group(_pgid: int, requested_signal: int) -> None:
                        if requested_signal == fault_signal:
                            raise denial

                    clock = Mock(side_effect=count(step=0.01))
                    with (
                        patch.dict(vars(agent.os), {"killpg": kill_group}),
                        patch.dict(vars(agent.time), {"monotonic": clock, "sleep": Mock()}),
                    ):
                        with self.assertRaises(PermissionError) as raised:
                            agent.stop_process_group(process)
                    self.assertIs(raised.exception, denial)
                    process.wait.assert_not_called()
                    self.assertLessEqual(clock.call_count, 110)

    def test_cleanup_does_not_retry_an_unrelated_os_error(self) -> None:
        process = Mock(pid=12345)
        failure = OSError(errno.EIO, "synthetic unrelated failure")
        kill_group = Mock(side_effect=failure)
        with patch.dict(vars(agent.os), {"killpg": kill_group}):
            with self.assertRaises(OSError) as raised:
                agent.stop_process_group(process)
        self.assertIs(raised.exception, failure)
        self.assertEqual(kill_group.call_count, 1)

    def test_wrong_host_and_missing_capabilities_fail_before_exec(self) -> None:
        for options in ({"version": "code 1.0 Every Code"}, {"help": "--json --output-last-message"}):
            with self.subTest(options=options):
                self.write_host(**options)
                result = self.invoke()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.record.with_suffix(".exec.json").exists())

    def test_required_selection_and_numeric_options(self) -> None:
        for arguments in (
            [], ["--host", "codex"], ["--host", "codex", "--model", "m", "--max-seconds", "nan"],
            ["--host", "codex", "--model", "m", "--max-seconds", "0"],
            ["--host", "codex", "--model", "m", "--context-window", "0"],
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 2)

    def test_malformed_yaml_error_does_not_echo_secret_values(self) -> None:
        self.config.write_text("secret: [synthetic-secret,\n")
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("synthetic-secret", result.stderr)
        self.assertFalse(self.record.with_suffix(".version.json").exists())

    def test_environment_helper_does_not_mutate_parent_environment(self) -> None:
        (self.root / "isolated").mkdir()
        with patch.dict(os.environ, {"CODE_HOME": "synthetic-parent-home", "OPENAI_API_KEY": "synthetic-parent-key"}):
            agent.isolated_environment(self.root / "isolated")
            self.assertEqual(os.environ["CODE_HOME"], "synthetic-parent-home")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "synthetic-parent-key")


if __name__ == "__main__":
    unittest.main()
