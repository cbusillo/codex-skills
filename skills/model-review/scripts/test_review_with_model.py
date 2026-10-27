#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run review_with_model.py against fake provider CLIs; no model access is needed."""

from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import review_with_model

SCRIPT = Path(__file__).with_name("review_with_model.py")

FAKE_CODEX = """#!/bin/sh
[ -n "$FAKE_REVIEW_ENV_FILE" ] && env > "$FAKE_REVIEW_ENV_FILE"
# Writes the answer to the file given after -o, like `codex exec`.
[ -n "$FAKE_CODEX_ARGV_FILE" ] && printf '%s\\n' "$@" > "$FAKE_CODEX_ARGV_FILE"
while [ $# -gt 0 ]; do
  [ "$1" = "-o" ] && out="$2"
  [ "$1" = "--output-schema" ] && [ -n "$FAKE_CODEX_SCHEMA_COPY" ] && cp "$2" "$FAKE_CODEX_SCHEMA_COPY"
  shift
done
if [ "${FAKE_CODEX_EVENTS+x}" ]; then
  printf '%s' "$FAKE_CODEX_EVENTS"
else
  printf '%s\\n' '{"type":"item.completed","item":{"type":"command_execution","status":"completed","exit_code":0}}'
fi
printf '%s' "$FAKE_ANSWER" > "$out"
printf '%s' "$FAKE_CODEX_STDERR" >&2
exit "${FAKE_CODEX_EXIT:-0}"
"""
FAKE_CLAUDE = """#!/bin/sh
[ -n "$FAKE_REVIEW_ENV_FILE" ] && env > "$FAKE_REVIEW_ENV_FILE"
[ -n "$FAKE_CLAUDE_ARGV_FILE" ] && printf '%s\\n' "$@" > "$FAKE_CLAUDE_ARGV_FILE"
printf '%s' "$FAKE_CLAUDE_JSON"
"""
READING_CLAUDE = """#!/bin/sh
[ -n "$FAKE_REVIEW_CALLS" ] && echo invoked >> "$FAKE_REVIEW_CALLS"
diff_path=$(printf '%s' "$2" | sed -n 's/^The changes to review are in \\(.*\\). Read that file.*/\\1/p')
if [ -n "$diff_path" ]; then
  case "$diff_path" in
    "$PWD"/.model-review-*/change.diff) cat "$diff_path" > "$FAKE_CLAUDE_DIFF_FILE" ;;
    *) printf '{"permission_denials":[{"tool_name":"Read","tool_input":{"file_path":"%s"}}]}' "$diff_path"; exit ;;
  esac
fi
cat "$PWD/example.txt" > "$FAKE_CLAUDE_SOURCE_FILE"
printf '%s' "$FAKE_CLAUDE_JSON"
"""
FAKE_AGY = """#!/bin/sh
pwd > "$FAKE_AGY_CWD_FILE"
[ -n "$FAKE_AGY_ARGV_FILE" ] && printf '%s\\n' "$@" > "$FAKE_AGY_ARGV_FILE"
[ -n "$FAKE_AGY_PROMPT_FILE" ] && printf '%s' "$2" > "$FAKE_AGY_PROMPT_FILE"
[ -n "$FAKE_AGY_DIFF_FILE" ] && {
  diff_path=$(printf '%s' "$2" | sed -n 's/^The changes to review are in \\([^ ]*\\). Read.*/\\1/p')
  [ -n "$diff_path" ] && cat "$diff_path" > "$FAKE_AGY_DIFF_FILE"
}
[ -n "$FAKE_AGY_INPUT_NAME" ] && {
  input_path=$(printf '%s' "$2" | grep -o "/[^ ]*$FAKE_AGY_INPUT_NAME" | head -1)
  printf '%s' "$input_path" > "$FAKE_AGY_INPUT_FILE.path"
  [ -n "$input_path" ] && cat "$input_path" > "$FAKE_AGY_INPUT_FILE"
}
echo "jetski: some banner text"
printf '%s' "$FAKE_AGY_JSON"
"""

RETRY_AGY = """#!/bin/sh
if [ -f "$FAKE_AGY_COUNT" ]; then
  echo retry >> "$FAKE_AGY_COUNT"
  printf '%s' "$2" > "$FAKE_AGY_RETRY_PROMPT"
  printf '%s' "$FAKE_AGY_RETRY_JSON"
else
  echo initial > "$FAKE_AGY_COUNT"
  printf '%s' "$FAKE_AGY_JSON"
fi
"""


def wire_varint(value: int) -> bytes:
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    return bytes(output) + bytes([value])


def wire_bytes(field: int, data: bytes) -> bytes:
    return wire_varint(field << 3 | 2) + wire_varint(len(data)) + data


def completed(report: str) -> str:
    return json.dumps({"status": "completed", "review": report})


# Context Panel's agent modes: the snapshot, and --record-launch reading one receipt from stdin.
FAKE_CONTEXT_PANEL = f"""#!{sys.executable}
import json, os, sys
if sys.argv[1:] == ["--account-snapshot"]:
    sys.stdout.write(open(os.environ["FAKE_SNAPSHOT_FILE"]).read())
    sys.exit(0)
name = "20261018T184448Z-supervisor-" + "0" * 32 + ".json"
with open(os.environ["FAKE_RECORD_FILE"], "w") as record:
    json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "name": name}}, record)
code = int(os.environ.get("FAKE_RECORD_EXIT", "0"))
if code:
    sys.stderr.write(os.environ.get("FAKE_RECORD_STDERR", "") + "\\n")
    sys.exit(code)
print(name)
"""


class ReviewWithModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.bin, self.home, self.repo = self.root / "bin", self.root / "home", self.root / "work" / "repo"
        for directory in (self.bin, self.home, self.repo):
            directory.mkdir(parents=True)
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("Review skills/x.md.\n")
        self.snapshot_file = self.root / "snapshot.json"
        self.storage = self.root / "context-panel"
        self.storage.mkdir()
        self.reader = self.bin / "snapshot-reader"
        self.reader.write_text('#!/bin/sh\ncat "$FAKE_SNAPSHOT_FILE"\n')
        self.reader.chmod(0o700)
        self.snapshot_file.write_text(json.dumps({
            "schemaVersion": 1,
            "accounts": [{"id": "openai-a", "provider": "openai", "label": "openai-test"},
                         {"id": "anthropic-b", "provider": "anthropic", "label": "anthropic-test"},
                         {"id": "google-c", "provider": "google", "label": "google-test"}],
            "answers": {"useNext": [{"provider": "openai", "accountID": "openai-a"},
                                   {"provider": "anthropic", "accountID": "anthropic-b"},
                                   {"provider": "google", "accountID": "google-c"}]},
        }))
        config = self.home / ".code/skill-data/supervisor.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            '[accounts]\nsnapshot_command = ' + json.dumps(
                [str(self.reader), "--storage-root", str(self.storage)]) + '\n'
            '[[accounts.account]]\nname = "openai-review"\nprovider = "openai"\n'
            'context_panel_label = "openai-test"\n[accounts.account.env]\nCODEX_HOME = ' +
            json.dumps(str(self.home / "openai-review")) + '\n'
            '[[accounts.account]]\nname = "anthropic-review"\nprovider = "anthropic"\n'
            'context_panel_label = "anthropic-test"\n[accounts.account.env]\nCLAUDE_CONFIG_DIR = ' +
            json.dumps(str(self.home / "anthropic-review")) + '\n'
            '[[accounts.account]]\nname = "google-review"\nprovider = "google"\n'
            'context_panel_label = "google-test"\nsingle_login = true\nenv = {}\n'
        )
        self.policy = config.with_name("model-review.toml")
        self.policy.write_text('version = 1\ndecision = "fixture decision"\n'
                               'providers = ["anthropic", "openai", "google"]\n')

    def tearDown(self) -> None:
        self.temp.cleanup()

    def install(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def run_helper(self, *args: str, **env: str) -> tuple[int, dict]:
        # PATH holds only the fakes plus the interpreter's directory, so an uninstalled provider stays uninstalled.
        environment = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "FAKE_SNAPSHOT_FILE": str(self.snapshot_file), **env}
        if args[0] in {"run", "check"} and "--author-provider" not in args:
            provider = args[args.index("--provider") + 1] if "--provider" in args else "openai"
            args = (*args, "--author-provider", "anthropic" if provider == "openai" else "openai")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=environment
        )
        return proc.returncode, json.loads(proc.stdout or "{}")

    def review(self, provider: str, **env: str) -> tuple[int, dict]:
        return self.run_helper(
            "run", "--provider", provider, "--repo", str(self.repo), "--prompt-file", str(self.prompt), **env
        )

    def test_standing_decision_blocks_primary_google_but_allows_supplementary(self) -> None:
        self.install("agy", FAKE_AGY)
        self.policy.write_text('version = 1\ndecision = "trial decision"\n'
                               'providers = ["anthropic"]\nsupplementary = ["google"]\n'
                               '[provider.google]\ntimeout = 1800\n')
        cwd_file = self.root / "agy-cwd"
        env = {"FAKE_AGY_CWD_FILE": str(cwd_file),
               "FAKE_AGY_JSON": json.dumps({"response": completed("none")})}
        code, result = self.review("google", **env)
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertFalse(cwd_file.exists())
        self.assertFalse((self.storage / "Launch Receipts").exists())
        code, result = self.run_helper(
            "run", "--author-provider", "openai", "--provider", "google", "--supplementary",
            "--repo", str(self.repo), "--prompt-file", str(self.prompt), **env)
        self.assertEqual((code, result["provider"], result["selection"]["role"]), (0, "google", "supplementary"))
        self.assertTrue(cwd_file.exists())
        self.assertTrue(Path(result["launch_receipt"]).exists())

    def test_default_never_selects_google_even_when_other_providers_are_unavailable(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        self.install("agy", FAKE_AGY)
        (self.repo / "probe.txt").write_text("fixture probe line\n")
        cwd_file = self.root / "google-cwd"
        snapshot = json.loads(self.snapshot_file.read_text())
        snapshot["answers"]["useNext"] = [r for r in snapshot["answers"]["useNext"] if r["provider"] != "anthropic"]
        self.snapshot_file.write_text(json.dumps(snapshot))
        for providers in (["google", "anthropic"], ["anthropic", "google"], ["google"]):
            self.policy.write_text('version = 1\ndecision = "explicit Google only"\nproviders = ' + json.dumps(providers) + '\n')
            for command in ("run", "check"):
                with self.subTest(providers=providers, command=command):
                    args = [command, "--repo", str(self.repo), "--author-provider", "openai"]
                    if command == "run":
                        args.extend(["--prompt-file", str(self.prompt)])
                    code, result = self.run_helper(
                        *args, FAKE_AGY_CWD_FILE=str(cwd_file),
                        FAKE_AGY_JSON=json.dumps({"response": completed("fixture probe line")}))
                    self.assertNotEqual(result.get("provider"), "google")
                    self.assertNotEqual(code, 0)
                    self.assertFalse(cwd_file.exists())
                    self.assertFalse((self.storage / "Launch Receipts").exists())

    def test_capacity_fallback_uses_only_the_allowed_other_provider_choice(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        self.install("codex", FAKE_CODEX)
        self.policy.write_text('version = 1\ndecision = "allow fallback"\nproviders = ["anthropic", "openai"]\n')
        snapshot = json.loads(self.snapshot_file.read_text())
        snapshot["answers"]["useNext"] = [r for r in snapshot["answers"]["useNext"] if r["provider"] != "anthropic"]
        snapshot["ranking"] = [{"provider": "anthropic", "nextCapacityAt": "2099-01-01T00:00:00Z"}]
        self.snapshot_file.write_text(json.dumps(snapshot))
        capture = self.root / "child-env"
        code, result = self.run_helper(
            "run", "--repo", str(self.repo), "--prompt-file", str(self.prompt), "--author-provider", "google",
            FAKE_REVIEW_ENV_FILE=str(capture), FAKE_ANSWER=completed("none"))
        self.assertEqual((code, result["provider"]), (0, "openai"))
        self.assertEqual(result["account"]["account_id"], next(r["accountID"] for r in snapshot["answers"]["useNext"]
                                                            if r["provider"] == "openai"))
        self.assertIn(snapshot["ranking"][0]["nextCapacityAt"], result["selection"]["skipped"][0]["reason"])
        self.assertTrue(capture.exists())
        self.assertEqual(len(list((self.storage / "Launch Receipts").glob("*.json"))), 1)
        self.policy.write_text(self.policy.read_text() + '[provider.openai]\ntimeout = 10\n')
        code, result = self.run_helper(
            "run", "--repo", str(self.repo), "--prompt-file", str(self.prompt), "--author-provider", "google", "--timeout", "20")
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertEqual((result["provider"], [r["provider"] for r in result["skipped"]]), ("openai", ["anthropic"]))
        self.assertIn(snapshot["ranking"][0]["nextCapacityAt"], result["skipped"][0]["reason"])
        self.assertEqual(len(list((self.storage / "Launch Receipts").glob("*.json"))), 1)

    def test_timeout_override_cannot_skip_an_available_preferred_provider(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        self.install("agy", FAKE_AGY)
        self.policy.write_text('version = 1\ndecision = "preferred provider"\nproviders = ["anthropic", "google"]\n'
                               '[provider.anthropic]\ntimeout = 10\n')
        capture = self.root / "child-env"
        code, result = self.run_helper("run", "--repo", str(self.repo), "--prompt-file", str(self.prompt),
                                       "--author-provider", "openai", "--timeout", "20", FAKE_REVIEW_ENV_FILE=str(capture))
        self.assertEqual((code, result["provider"], result["skipped"]), (1, "anthropic", []))
        self.assertFalse(capture.exists())
        self.assertFalse((self.storage / "Launch Receipts").exists())

    def test_exhausted_primary_does_not_promote_supplementary_or_use_author(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        self.install("codex", FAKE_CODEX)
        self.install("agy", FAKE_AGY)
        self.policy.write_text('version = 1\ndecision = "primary held"\n'
                               'providers = ["anthropic", "openai"]\nsupplementary = ["google"]\n')
        snapshot = json.loads(self.snapshot_file.read_text())
        snapshot["answers"]["useNext"] = [r for r in snapshot["answers"]["useNext"] if r["provider"] != "anthropic"]
        self.snapshot_file.write_text(json.dumps(snapshot))
        code, result = self.run_helper("run", "--repo", str(self.repo), "--prompt-file", str(self.prompt),
                                       "--author-provider", "openai")
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertEqual([r["provider"] for r in result["skipped"]], ["anthropic"])
        self.assertFalse((self.storage / "Launch Receipts").exists())

    def test_review_failure_never_triggers_provider_fallback(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        self.install("agy", FAKE_AGY)
        self.policy.write_text('version = 1\ndecision = "ordered providers"\nproviders = ["anthropic", "google"]\n')
        cwd_file = self.root / "google-cwd"
        code, result = self.run_helper(
            "run", "--repo", str(self.repo), "--prompt-file", str(self.prompt), "--author-provider", "openai",
            FAKE_CLAUDE_JSON=json.dumps({"result": json.dumps({"status": "refused", "review": ""})}),
            FAKE_AGY_CWD_FILE=str(cwd_file), FAKE_AGY_JSON=json.dumps({"response": completed("none")}))
        self.assertEqual((code, result["provider"], result["outcome"]), (1, "anthropic", "refused"))
        self.assertFalse(cwd_file.exists())
        self.assertEqual(len(list((self.storage / "Launch Receipts").glob("*.json"))), 1)

    def test_missing_or_malformed_decision_never_launches(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        capture = self.root / "child-env"
        bodies = [None, "invalid TOML", 'version = 1\nproviders = ["anthropic"]',
                  'version = 1\ndecision = "bad provider"\nproviders = [{name="anthropic"}]']
        for body in bodies:
            with self.subTest(body=body):
                self.policy.unlink(missing_ok=True)
                if body is not None:
                    self.policy.write_text(body)
                code, result = self.review("anthropic", FAKE_REVIEW_ENV_FILE=str(capture))
                self.assertEqual((code, result["ok"]), (1, False))
                self.assertFalse(capture.exists())
                self.assertFalse((self.storage / "Launch Receipts").exists())

    def test_policy_model_budget_and_actual_provenance_reach_the_review(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        requested, actual, timeout = "fixture-alias", "resolved-model", 123
        self.policy.write_text('version = 1\ndecision = "model choice"\nproviders = ["anthropic"]\n'
                               f'[provider.anthropic]\nmodel = "{requested}"\ntimeout = {timeout}\n')
        argv_file = self.root / "argv"
        code, result = self.review("anthropic", FAKE_CLAUDE_ARGV_FILE=str(argv_file),
                                   FAKE_CLAUDE_JSON=json.dumps({"result": completed("none"), "modelUsage": {actual: {}}}))
        self.assertEqual((code, result["model"], result["timeout"]), (0, actual, timeout))
        argv = argv_file.read_text().splitlines()
        self.assertEqual(argv[argv.index("--model") + 1], requested)
        self.assertEqual(result["effort"], "unknown")
        argv_file.unlink()
        for option, value in (("--model", "unapproved-model"), ("--timeout", str(timeout + 1))):
            code, result = self.run_helper("run", "--provider", "anthropic", "--repo", str(self.repo),
                                           "--prompt-file", str(self.prompt), option, value,
                                           FAKE_CLAUDE_ARGV_FILE=str(argv_file))
            self.assertEqual((code, result["ok"]), (1, False))
            self.assertFalse(argv_file.exists())

    def test_supplementary_probe_uses_its_role_and_can_shorten_the_review_limit(self) -> None:
        self.install("agy", FAKE_AGY)
        self.policy.write_text('version = 1\ndecision = "probe trial"\n'
                               'providers = ["anthropic"]\nsupplementary = ["google"]\n'
                               '[provider.google]\ntimeout = 1800\n')
        (self.repo / "probe.txt").write_text("fixture probe line\n")
        code, result = self.run_helper(
            "check", "--repo", str(self.repo), "--author-provider", "openai", "--provider", "google", "--supplementary", "--timeout", "12",
            FAKE_AGY_CWD_FILE=str(self.root / "cwd"), FAKE_AGY_JSON=json.dumps({"response": completed("fixture probe line")}))
        self.assertEqual((code, result["providers"][0]["provider"], result["providers"][0]["state"]),
                         (0, "google", "ready"))
        config = review_with_model.account_choice.load_config(self.policy.with_name("supervisor.toml"))
        with patch.object(review_with_model.account_choice, "load_config", return_value=config), \
                patch.object(review_with_model.account_choice, "read_snapshot", return_value=(json.loads(self.snapshot_file.read_text()), None)), \
                patch.object(review_with_model.shutil, "which", return_value="fake-agy"):
            selected = review_with_model.review_policy.select(review_with_model.review_policy.load_policy(self.policy),
                                                              "openai", "google", True, probe=True)
        self.assertLess(selected["timeout"], review_with_model.review_policy.load_policy(self.policy)["provider"]["google"]["timeout"])

    def test_missing_author_and_invalid_model_route_are_json_errors_before_capacity(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        code, result = self.run_helper(
            "run", "--repo", str(self.repo), "--prompt-file", str(self.prompt), "--author-provider", "openai", "--model", "fixture")
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertFalse((self.storage / "Launch Receipts").exists())
        # Call without the convenience wrapper's author default, like an older caller.
        proc = subprocess.run([sys.executable, str(SCRIPT), "run", "--provider", "anthropic",
                               "--repo", str(self.repo), "--prompt-file", str(self.prompt)],
                              capture_output=True, text=True, env={"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home),
                                  "FAKE_SNAPSHOT_FILE": str(self.snapshot_file), "FAKE_REVIEW_ENV_FILE": str(self.root / "child-env"),
                                  "FAKE_CLAUDE_JSON": json.dumps({"result": completed("none")})})
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["ok"]), (1, False))
        self.assertFalse((self.root / "child-env").exists())
        self.assertFalse((self.storage / "Launch Receipts").exists())

    def test_reviews_route_provider_home_and_write_count_only_receipt(self) -> None:
        self.install("codex", FAKE_CODEX)
        self.install("claude", FAKE_CLAUDE)
        capture = self.root / "child-env"
        for provider, variable in review_with_model.account_choice.ACCOUNT_VARIABLES.items():
            with self.subTest(provider=provider):
                code, result = self.review(
                    provider, CODEX_HOME=str(self.home / "caller-openai"),
                    CLAUDE_CONFIG_DIR=str(self.home / "caller-anthropic"),
                    OPENAI_API_KEY="fake-openai-key", CODEX_API_KEY="fake-codex-key",
                    ANTHROPIC_API_KEY="fake-anthropic-key", ANTHROPIC_AUTH_TOKEN="fake-auth",
                    CLAUDE_CODE_OAUTH_TOKEN="fake-oauth", CLAUDECODE="caller", KEEP_ME="inherited",
                    ANTHROPIC_BASE_URL="https://example.invalid", CLAUDE_CODE_USE_BEDROCK="1",
                    CLAUDE_CODE_USE_VERTEX="1", CLAUDE_CODE_USE_FOUNDRY="1",
                    CLAUDE_CODE_USE_MANTLE="1", CLAUDE_CODE_USE_ANTHROPIC_AWS="1",
                    ANTHROPIC_CUSTOM_HEADERS="Authorization: fake-header",
                    FAKE_REVIEW_ENV_FILE=str(capture), FAKE_ANSWER=completed("none"),
                    FAKE_CLAUDE_JSON=json.dumps({"result": completed("none")}),
                )
                self.assertEqual((code, result["ok"]), (0, True))
                child = dict(line.split("=", 1) for line in capture.read_text().splitlines())
                self.assertEqual(child[variable], str(self.home / f"{provider}-review"))
                self.assertEqual(child["KEEP_ME"], "inherited")
                self.assertNotIn("CLAUDECODE", child)
                for key in (("OPENAI_API_KEY", "CODEX_API_KEY") if provider == "openai" else
                            ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                             "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                             "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                             "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_ANTHROPIC_AWS",
                             "ANTHROPIC_CUSTOM_HEADERS")):
                    self.assertNotIn(key, child)
                receipt = json.loads(Path(result["launch_receipt"]).read_text())
                self.assertEqual(receipt["accountID"], result["account"]["account_id"])
                self.assertEqual(receipt["provider"], provider)
                self.assertEqual(result["account"]["name"], f"{provider}-review")
                self.assertEqual(set(receipt), {"schemaVersion", "provider", "accountID", "launchedAt"})
                self.assertNotIn(str(self.home), json.dumps(result["account"]))
                self.assertNotIn("fake-oauth", json.dumps(result))

    def test_each_review_reads_the_current_context_panel_choice(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        capture = self.root / "child-env"
        config = self.home / ".code/skill-data/supervisor.toml"
        config.write_text(config.read_text() +
                         '[[accounts.account]]\nname = "second-review"\nprovider = "anthropic"\n'
                         'context_panel_label = "second-test"\n[accounts.account.env]\n'
                         'CLAUDE_CONFIG_DIR = "~/second-home"\n')
        for account_id, label, home in (("anthropic-b", "anthropic-test", str(self.home / "anthropic-review")),
                                        ("anthropic-c", "second-test", str(self.home / "second-home"))):
            snapshot = json.loads(self.snapshot_file.read_text())
            snapshot["accounts"] = [{"id": account_id, "provider": "anthropic", "label": label}]
            snapshot["answers"]["useNext"] = [{"provider": "anthropic", "accountID": account_id}]
            self.snapshot_file.write_text(json.dumps(snapshot))
            code, result = self.review("anthropic", FAKE_REVIEW_ENV_FILE=str(capture),
                                       FAKE_CLAUDE_JSON=json.dumps({"result": completed("none")}))
            self.assertEqual((code, result["account"]["account_id"]), (0, account_id))
            child = dict(line.split("=", 1) for line in capture.read_text().splitlines())
            self.assertEqual(child["CLAUDE_CONFIG_DIR"], home)
        self.assertEqual(len(list((self.storage / "Launch Receipts").glob("*.json"))), 2)

    def test_default_anthropic_choice_removes_inherited_home_without_mutating_parent(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        config = self.home / ".code/skill-data/supervisor.toml"
        config.write_text(config.read_text().replace(
            'CLAUDE_CONFIG_DIR = ' + json.dumps(str(self.home / "anthropic-review")), ""))
        capture = self.root / "child-env"
        code, result = self.review("anthropic", CLAUDE_CONFIG_DIR="caller-home",
                                   FAKE_REVIEW_ENV_FILE=str(capture),
                                   FAKE_CLAUDE_JSON=json.dumps({"result": completed("none")}))
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertNotIn("CLAUDE_CONFIG_DIR=", capture.read_text())
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "caller-home", "KEEP_ME": "inherited"}):
            before = dict(os.environ)
            child = review_with_model.reviewer_environment({"provider": "anthropic", "env": {}})
            self.assertNotIn("CLAUDE_CONFIG_DIR", child)
            self.assertEqual(dict(os.environ), before)

    def test_unavailable_or_unmapped_choices_never_launch_reviewer_or_receipt(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        capture = self.root / "child-env"
        original = self.snapshot_file.read_text()
        for snapshot in ("not JSON", json.dumps({"schemaVersion": 1, "accounts": [], "answers": {}}),
                         original.replace("anthropic-test", "unmapped")):
            with self.subTest(snapshot=snapshot):
                self.snapshot_file.write_text(snapshot)
                code, result = self.review("anthropic", FAKE_REVIEW_ENV_FILE=str(capture))
                self.assertEqual((code, result["ok"]), (1, False))
                self.assertFalse(capture.exists())
                self.assertFalse((self.storage / "Launch Receipts").exists())
        self.snapshot_file.write_text(original)
        (self.home / ".code/skill-data/supervisor.toml").unlink()
        code, result = self.review("anthropic", FAKE_REVIEW_ENV_FILE=str(capture))
        self.assertEqual((code, result["ok"], result["phase"]), (1, False, "selection"))
        self.assertFalse(capture.exists())

    def test_receipt_failure_prevents_launch_and_provider_failure_retains_receipt(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        capture = self.root / "child-env"
        self.storage.rmdir()
        self.storage.write_text("storage is unavailable")
        code, result = self.review("anthropic", FAKE_REVIEW_ENV_FILE=str(capture))
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertNotIn("phase", result, "selection succeeded; the receipt step refused the launch")
        self.assertNotIn("launch_receipt", result)
        self.assertFalse(capture.exists())
        self.storage.unlink()
        self.storage.mkdir()
        code, result = self.review("anthropic", FAKE_CLAUDE_JSON=json.dumps({"is_error": True, "result": "limit"}))
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertTrue(Path(result["launch_receipt"]).is_file())

    def test_app_group_review_receipt_goes_through_the_readers_record_launch(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        app = self.bin / "Context Panel"
        app.write_text(FAKE_CONTEXT_PANEL)
        app.chmod(0o700)
        config = self.home / ".code/skill-data/supervisor.toml"
        config.write_text(config.read_text().replace(
            json.dumps([str(self.reader), "--storage-root", str(self.storage)]),
            json.dumps([str(app), "--account-snapshot"])))
        capture, received = self.root / "child-env", self.root / "received.json"
        for exit_code, reason in ((64, "receipt is not current"), (1, "storage unavailable")):
            with self.subTest(exit_code=exit_code):
                code, result = self.review("anthropic", FAKE_REVIEW_ENV_FILE=str(capture),
                                           FAKE_RECORD_FILE=str(received), FAKE_RECORD_EXIT=str(exit_code),
                                           FAKE_RECORD_STDERR=reason)
                self.assertEqual((code, result["ok"]), (1, False))
                self.assertIn(f"exit {exit_code}: {reason}", result["error"])
                self.assertNotIn("launch_receipt", result)
                self.assertFalse(capture.exists())
        code, result = self.review("anthropic", FAKE_REVIEW_ENV_FILE=str(capture), FAKE_RECORD_FILE=str(received),
                                   FAKE_CLAUDE_JSON=json.dumps({"result": completed("none")}))
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertTrue(capture.exists())
        record = json.loads(received.read_text())
        self.assertEqual(record["argv"], ["--record-launch", "--launcher", "supervisor"])
        receipt = json.loads(record["stdin"])
        self.assertEqual(set(receipt), {"schemaVersion", "provider", "accountID", "launchedAt"})
        self.assertEqual((receipt["provider"], receipt["accountID"]), ("anthropic", result["account"]["account_id"]))
        self.assertEqual(Path(result["launch_receipt"]).name, record["name"])
        self.assertFalse((self.storage / "Launch Receipts").exists())

    def test_each_provider_returns_its_review_and_the_model_that_ran(self) -> None:
        self.install("codex", FAKE_CODEX)
        self.install("claude", FAKE_CLAUDE)
        self.install("agy", FAKE_AGY)
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "gemini-test"}))
        claude_json = json.dumps({"is_error": False, "result": completed("none"), "modelUsage": {"claude-test": {}}})
        agy_json = json.dumps({"response": completed("none"), "denied_actions": []})
        cwd_file = self.root / "agy-cwd"
        for provider, model, env in (
            ("openai", None, {"FAKE_ANSWER": completed("none")}),
            ("anthropic", "claude-test", {"FAKE_CLAUDE_JSON": claude_json}),
            ("google", "gemini-test", {"FAKE_AGY_JSON": agy_json, "FAKE_AGY_CWD_FILE": str(cwd_file)}),
        ):
            with self.subTest(provider=provider):
                code, result = self.review(provider, **env)
                self.assertEqual((code, result["ok"], result["model"], result["response"]), (0, True, model, "none"))
        # The Google shell is confined to a temporary directory, not the repository root.
        self.assertNotEqual(str(self.repo), cwd_file.read_text().strip())
        self.assertFalse(list(self.repo.glob(".model-review-*")))
        # agy does not say which model ran, so the result must not pretend it did.
        self.assertIn("not reported", result["model_source"])

    def test_the_anthropic_reviewer_is_given_only_read_tools(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        argv_file = self.root / "claude-argv"
        ok = json.dumps({"is_error": False, "result": completed("none"), "modelUsage": {"claude-test": {}}})
        self.review("anthropic", FAKE_CLAUDE_JSON=ok, FAKE_CLAUDE_ARGV_FILE=str(argv_file))
        argv = argv_file.read_text().split("\n")
        # `--allowedTools` only extends a user's own allowlist; `--tools` is what removes the write tools.
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertNotIn("--allowedTools", argv)
        # `--tools` leaves configured MCP servers, which can write, unless the session ignores them.
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--mcp-config", argv)
        # Settings committed to the reviewed repository can run hooks before the review starts.
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "user")

    def test_the_openai_reviewer_starts_without_mcp_servers_or_the_users_config(self) -> None:
        self.install("codex", FAKE_CODEX)
        argv_file = self.root / "codex-argv"
        self.review("openai", FAKE_ANSWER=completed("none"), FAKE_CODEX_ARGV_FILE=str(argv_file))
        argv = argv_file.read_text().split("\n")
        # MCP servers run outside `-s read-only`; they come from the user's config, plugins, and apps.
        self.assertIn("--ignore-user-config", argv)
        disabled = {argv[i + 1] for i, arg in enumerate(argv) if arg == "--disable"}
        self.assertLessEqual({"plugins", "apps"}, disabled)
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")

    def test_anthropic_reads_source_and_diff_inside_checkout_and_cleans_its_scratch(self) -> None:
        self.install("claude", READING_CLAUDE)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        source = self.repo / "example.txt"
        source.write_text("before\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "example.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        diff_copy, source_copy = self.root / "diff-copy", self.root / "source-copy"
        env = {"FAKE_CLAUDE_JSON": json.dumps({"result": completed("none")}),
               "FAKE_CLAUDE_DIFF_FILE": str(diff_copy), "FAKE_CLAUDE_SOURCE_FILE": str(source_copy)}
        code, result = self.review("anthropic", **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertFalse(diff_copy.exists(), "a review with no changes needs no diff file")
        source.write_text("after\n")
        code, result = self.review("anthropic", **env)
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("uncommitted", result["error"])
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "change"], check=True)
        leftover = self.repo / ".model-review-Ab12_cd" / "change.diff"
        leftover.parent.mkdir()
        leftover.write_text("another review's scratch")
        code, result = self.review("anthropic", **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertIn("+after", diff_copy.read_text())
        self.assertEqual(source_copy.read_text(), "after\n")
        self.assertEqual(list(self.repo.glob(".model-review-*")), [leftover.parent])
        source_copy.unlink()
        (self.repo / "untracked.txt").write_text("ordinary project file")
        code, result = self.review("anthropic", **env)
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("untracked", result["error"])
        self.assertFalse(source_copy.exists(), "dirty reviews must not invoke the provider")
        self.assertTrue(leftover.exists(), "another review's scratch stays intact")

    def test_anthropic_denial_names_read_path_without_echoing_other_inputs(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        path = str(self.repo / ".model-review-example" / "change.diff")
        denials = [{"tool_name": "Read", "tool_input": {"file_path": path, "contents": "private contents"}},
                   {"tool_name": "Grep", "tool_input": {"path": str(self.repo), "pattern": "private pattern"}},
                   {"tool_name": "Read"}]
        code, result = self.review("anthropic", FAKE_CLAUDE_JSON=json.dumps({
            "result": completed("none"), "permission_denials": denials}))
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn({"tool": "Read", "path": path}, result["denied_targets"])
        self.assertIn({"tool": "Grep", "path": str(self.repo)}, result["denied_targets"])
        self.assertEqual(result["denied"], ["Grep", "Read"])
        self.assertNotIn("private contents", json.dumps(result))
        self.assertNotIn("private pattern", json.dumps(result))
        self.assertNotIn("response", result)
        self.assertFalse(list(self.repo.glob(".model-review-*")), "denials also clean scratch")

    def test_partial_read_refusals_are_unavailable_without_exposing_or_retrying_them(self) -> None:
        self.install("claude", READING_CLAUDE)
        source = self.repo / "example.txt"
        source.write_text("source actually read\n")
        source_copy, calls, output = self.root / "read-proof", self.root / "calls", self.root / "review.md"
        marker = self.home / ".code/model-review-fault.md"
        marker.write_text("planted finding must stay pending")
        # Vary the language, not one provider's exact refusal sentence. An unstructured
        # response cannot establish completion, even after successful real file reads.
        responses = [
            ("A safety check stopped this review after partial reads. No findings report is available.", "unavailable"),
            ("I inspected example.txt but cannot finish the requested assessment.", "unavailable"),
            (json.dumps({"status": "refused", "review": "WITHHELD-REPORT-CONTENT " * 500}), "refused"),
            (json.dumps({"status": "incomplete", "review": "WITHHELD-REPORT-CONTENT"}), "incomplete"),
        ]
        for response, outcome in responses:
            with self.subTest(response=response[:60]):
                code, result = self.run_helper(
                    "run", "--provider", "anthropic", "--repo", str(self.repo),
                    "--prompt-file", str(self.prompt), "--out", str(output),
                    FAKE_CLAUDE_JSON=json.dumps({"result": response, "modelUsage": {"claude-test": {}}}),
                    FAKE_CLAUDE_SOURCE_FILE=str(source_copy), FAKE_REVIEW_CALLS=str(calls))
                self.assertEqual((code, result["ok"]), (1, False))
                self.assertEqual(result["outcome"], outcome)
                self.assertEqual(result["model"], "claude-test")
                self.assertEqual(source_copy.read_text(), source.read_text())
                self.assertTrue(result["launch_receipt"])
                self.assertNotIn("response", result)
                self.assertNotIn("WITHHELD-REPORT-CONTENT", json.dumps(result))
                self.assertLess(len(result["error"]), 200)
                self.assertFalse(output.exists())
                self.assertTrue(marker.exists())
        self.assertEqual(len(calls.read_text().splitlines()), len(responses))
        # A completed no-findings review still delivers its report.
        marker.unlink()
        code, result = self.run_helper(
            "run", "--provider", "anthropic", "--repo", str(self.repo),
            "--prompt-file", str(self.prompt), "--out", str(output),
            FAKE_CLAUDE_JSON=json.dumps({"result": completed("No findings.")}),
            FAKE_CLAUDE_SOURCE_FILE=str(source_copy))
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertEqual(output.read_text(), "No findings.")

    def test_all_providers_require_a_completed_nonempty_report(self) -> None:
        self.install("codex", FAKE_CODEX)
        self.install("claude", FAKE_CLAUDE)
        self.install("agy", FAKE_AGY)
        for provider in review_with_model.PROVIDERS:
            # Whole-response fences are formatting, not evidence of interruption.
            for response in (completed("none"), "```json\n" + completed("none") + "\n```",
                             "```JSON\n" + completed("none") + "\n```"):
                with self.subTest(provider=provider, completed=response):
                    code, result = self.review(
                        provider, FAKE_ANSWER=response, FAKE_CLAUDE_JSON=json.dumps({"result": response}),
                        FAKE_AGY_JSON=json.dumps({"response": response}), FAKE_AGY_CWD_FILE=str(self.root / "cwd"))
                    self.assertEqual((code, result["ok"], result["response"]), (0, True, "none"))
            for response in (json.dumps({"status": "refused"}), json.dumps({"status": "incomplete"}),
                             completed(""), json.dumps({"status": "unknown", "review": "none"}),
                             "[]", "null", "not a completion result",
                             "The review was stopped.\n" + completed("none")):
                with self.subTest(provider=provider, response=response):
                    code, result = self.review(
                        provider, FAKE_ANSWER=response, FAKE_CLAUDE_JSON=json.dumps({"result": response}),
                        FAKE_AGY_JSON=json.dumps({"response": response}), FAKE_AGY_CWD_FILE=str(self.root / "cwd"))
                    self.assertEqual((code, result["ok"]), (1, False))
                    self.assertNotIn("response", result)

    def test_every_provider_cli_is_asked_to_enforce_the_result_schema(self) -> None:
        self.install("codex", FAKE_CODEX)
        self.install("claude", FAKE_CLAUDE)
        self.install("agy", FAKE_AGY)
        argv, schema_copy = self.root / "argv", self.root / "schema"
        for provider in review_with_model.PROVIDERS:
            with self.subTest(provider=provider):
                code, _ = self.review(
                    provider, FAKE_ANSWER=completed("none"), FAKE_CODEX_ARGV_FILE=str(argv),
                    FAKE_CODEX_SCHEMA_COPY=str(schema_copy), FAKE_CLAUDE_ARGV_FILE=str(argv),
                    FAKE_CLAUDE_JSON=json.dumps({"result": completed("none")}), FAKE_AGY_ARGV_FILE=str(argv),
                    FAKE_AGY_JSON=json.dumps({"response": completed("none")}), FAKE_AGY_CWD_FILE=str(self.root / "cwd"))
                self.assertEqual(code, 0)
                lines = argv.read_text().splitlines()
                if provider == "openai":
                    schema = json.loads(schema_copy.read_text())
                else:
                    schema = json.loads(lines[lines.index("--json-schema") + 1])
                self.assertEqual(schema, review_with_model.RESULT_SCHEMA)

    def test_structured_output_confirms_completion_despite_narrated_final_text(self) -> None:
        # Observed on Oct 15-16: valid completed objects behind a lead-in sentence, and report
        # strings with raw newlines. The CLI's validated object is the result; the text is not scraped.
        self.install("claude", FAKE_CLAUDE)
        self.install("agy", FAKE_AGY)
        narrated = "I have enough context to finish.\n" + completed("line one\nline two")
        structured = {"status": "completed", "review": "line one\nline two"}
        for provider in ("anthropic", "google"):
            with self.subTest(provider=provider):
                code, result = self.review(
                    provider, FAKE_CLAUDE_JSON=json.dumps({"result": narrated, "structured_output": structured}),
                    FAKE_AGY_JSON=json.dumps({"response": narrated, "structured_output": structured}),
                    FAKE_AGY_CWD_FILE=str(self.root / "cwd"))
                self.assertEqual((code, result["ok"], result["response"]), (0, True, "line one\nline two"))
                self.assertTrue(result["structured_output"])
            with self.subTest(provider=provider, status="refused"):
                refused = {"status": "refused", "review": "WITHHELD-REPORT-CONTENT"}
                code, result = self.review(
                    provider, FAKE_CLAUDE_JSON=json.dumps({"result": completed("none"), "structured_output": refused}),
                    FAKE_AGY_JSON=json.dumps({"response": completed("none"), "structured_output": refused}),
                    FAKE_AGY_CWD_FILE=str(self.root / "cwd"))
                self.assertEqual((code, result["outcome"]), (1, "refused"))
                self.assertNotIn("WITHHELD-REPORT-CONTENT", json.dumps(result))

    def test_unconfirmed_completion_reports_its_shape_without_content(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        cases = [
            ("I reviewed it.\n" + completed("WITHHELD-REPORT-CONTENT"), "text before the JSON object"),
            ('{"status": "completed", "review": "WITHHELD-REPORT-CONTENT\n"}', "invalid JSON"),
            ("WITHHELD-REPORT-CONTENT", "no JSON object"),
            (completed(" "), "completed with an empty review"),
        ]
        shapes = []
        for response, shape in cases:
            with self.subTest(shape=shape):
                code, result = self.review("anthropic", FAKE_CLAUDE_JSON=json.dumps({"result": response}))
                self.assertEqual((code, result["ok"], result["outcome"]), (1, False, "unavailable"))
                self.assertTrue(result["completion_shape"])
                shapes.append(result["completion_shape"])
                self.assertFalse(result["structured_output"])
                self.assertNotIn("WITHHELD-REPORT-CONTENT", json.dumps(result))
        self.assertEqual(len(set(shapes)), len(cases), "each unconfirmed shape is reported distinctly")

    def test_claude_structured_output_failure_withholds_its_final_text(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        code, result = self.review("anthropic", FAKE_CLAUDE_JSON=json.dumps({
            "is_error": True, "subtype": "error_max_structured_output_retries", "result": "WITHHELD-REPORT-CONTENT"}))
        self.assertEqual((code, result["outcome"], result["subtype"]), (1, "unavailable", "error_max_structured_output_retries"))
        self.assertNotIn("WITHHELD-REPORT-CONTENT", json.dumps(result))

    def test_google_does_not_retry_nonempty_refusal_with_command_denial(self) -> None:
        self.install("agy", RETRY_AGY)
        calls = self.root / "calls"
        code, result = self.review(
            "google", FAKE_AGY_COUNT=str(calls), FAKE_AGY_RETRY_PROMPT=str(self.root / "retry"),
            FAKE_AGY_JSON=json.dumps({"response": "I cannot complete this assessment.",
                                      "denied_actions": [{"action": "command"}]}),
            FAKE_AGY_RETRY_JSON=json.dumps({"response": completed("none")}))
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertEqual(len(calls.read_text().splitlines()), 1)
        self.assertNotIn("recovery", result)
        self.assertEqual(result["denied"], ["command"])
        self.assertIn("command_diagnostic", result)
        self.assertFalse(list(self.repo.glob(".model-review-*")), "denials also clean scratch")

    def test_google_does_not_retry_structured_refusal_with_command_denial(self) -> None:
        self.install("agy", RETRY_AGY)
        calls = self.root / "calls"
        code, result = self.review(
            "google", FAKE_AGY_COUNT=str(calls), FAKE_AGY_RETRY_PROMPT=str(self.root / "retry"),
            FAKE_AGY_JSON=json.dumps({"response": "", "structured_output": {"status": "refused", "review": ""},
                                      "denied_actions": [{"action": "command"}]}),
            FAKE_AGY_RETRY_JSON=json.dumps({"response": completed("none")}))
        self.assertEqual((code, result["ok"], result["outcome"]), (1, False, "unavailable"))
        self.assertEqual(len(calls.read_text().splitlines()), 1)
        self.assertNotIn("recovery", result)

    def saved_command(self, command: str, status: int = 7) -> str:
        conversation = "12345678-1234-1234-1234-123456789abc"
        directory = self.home / ".gemini/antigravity-cli/conversations"
        directory.mkdir(parents=True, exist_ok=True)
        call = wire_bytes(2, b"run_command") + wire_bytes(3, json.dumps({"CommandLine": command}).encode())
        blob = wire_bytes(5, wire_bytes(4, call))
        with sqlite3.connect(directory / f"{conversation}.db") as connection:
            connection.execute("CREATE TABLE steps (idx INTEGER, status INTEGER, step_payload BLOB)")
            connection.execute("INSERT INTO steps VALUES (0, 3, ?)", (blob,))
            connection.execute("INSERT INTO steps VALUES (1, ?, ?)", (status, blob))
            connection.execute("INSERT INTO steps VALUES (2, 7, ?)", (wire_bytes(5, b""),))
        return conversation

    def test_google_names_saved_command_and_retries_only_once(self) -> None:
        self.install("agy", RETRY_AGY)
        command = f"cat {self.repo}/large.py | grep -n -A 50 'example'"
        conversation = self.saved_command(command)
        denial = json.dumps({"status": "SUCCESS", "response": "", "conversation_id": conversation,
                             "denied_actions": [{"action": "command", "display_name": "RunCommand"}]})
        count, retry_prompt = self.root / "count", self.root / "retry-prompt"
        env = {"FAKE_AGY_JSON": denial, "FAKE_AGY_RETRY_JSON": denial,
               "FAKE_AGY_COUNT": str(count), "FAKE_AGY_RETRY_PROMPT": str(retry_prompt)}
        db = self.home / f".gemini/antigravity-cli/conversations/{conversation}.db"
        original = db.read_bytes()
        code, result = self.review("google", **env)
        self.assertEqual((code, result["ok"], result["denied_commands"]), (1, False, [command]))
        self.assertIn(command, result["error"])
        self.assertNotIn("hint", result)
        self.assertEqual(result["recovery"]["attempts"], 2)
        self.assertEqual(count.read_text().splitlines(), ["initial", "retry"])
        count.unlink()
        code, result = self.review("google", **{**env, "FAKE_AGY_RETRY_JSON": json.dumps({"response": completed("none")})})
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertEqual(result["recovery"]["denied_commands"], [command])
        self.assertEqual(db.read_bytes(), original, "diagnosis never changes the saved conversation")
        count.unlink()
        code, result = self.review("google", **{**env, "FAKE_AGY_RETRY_JSON": json.dumps({"response": ""})})
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn(command, result["error"], "an empty retry must preserve the original named refusal")
        self.assertEqual(result["recovery"]["denied_commands"], [command])

    def test_google_pending_command_is_named_but_read_denial_is_not_retried(self) -> None:
        self.install("agy", FAKE_AGY)
        command = "sed -n '1,3000p' /repo/large.py"
        conversation = self.saved_command(command, status=6)
        env = {"FAKE_AGY_CWD_FILE": str(self.root / "cwd")}
        denial = {"conversation_id": conversation, "denied_actions": [{"action": "command"}, {"action": "read_file"}]}
        code, result = self.review("google", FAKE_AGY_JSON=json.dumps(denial), **env)
        self.assertEqual((code, result["denied_commands"]), (1, [command]))
        self.assertNotIn("recovery", result)
        self.assertIn("hint", result)

    def test_google_retry_timeout_preserves_refusal_without_echoing_the_prompt(self) -> None:
        command = "cat /repo/large.py"
        conversation = self.saved_command(command)
        denial = json.dumps({"conversation_id": conversation, "denied_actions": [{"action": "command"}]})
        calls = [subprocess.CompletedProcess([], 0, denial, ""),
                 subprocess.TimeoutExpired(["agy", "-p", "private review prompt"], 31)]
        with patch.object(review_with_model, "AGY_SETTINGS", self.home / ".gemini/antigravity-cli/settings.json"), \
             patch.object(review_with_model, "run_cli", side_effect=calls), \
             patch.object(review_with_model.shutil, "which", return_value="agy"):
            result = review_with_model.review("google", "private review prompt", self.repo, None, 1)
        self.assertFalse(result["ok"])
        self.assertIn(command, result["error"])
        self.assertNotIn("private review prompt", result["error"])

    def test_google_missing_corrupt_or_invalid_conversation_keeps_denial_visible(self) -> None:
        self.install("agy", FAKE_AGY)
        for conversation in (None, "../../outside", "12345678-1234-1234-1234-123456789abc"):
            with self.subTest(conversation=conversation):
                denial = {"conversation_id": conversation, "denied_actions": [{"action": "command"}]}
                code, result = self.review("google", FAKE_AGY_JSON=json.dumps(denial),
                                           FAKE_AGY_CWD_FILE=str(self.root / "cwd"))
                self.assertEqual((code, result["denied_commands"]), (1, []))
                self.assertIn("command_diagnostic", result)
                self.assertEqual(result["recovery"]["attempts"], 2)
        self.assertFalse((self.home / ".gemini").exists(), "mode=ro must not create missing stores")
        self.install("agy", RETRY_AGY)
        denial = {"denied_actions": [{"action": "command"}]}
        code, result = self.review("google", FAKE_AGY_JSON=json.dumps(denial),
                                   FAKE_AGY_RETRY_JSON=json.dumps({"response": completed("none")}),
                                   FAKE_AGY_COUNT=str(self.root / "count"),
                                   FAKE_AGY_RETRY_PROMPT=str(self.root / "retry-prompt"))
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertIn("command_diagnostic", result["recovery"])
        self.install("agy", FAKE_AGY)
        conversation = self.saved_command("cat /repo/large.py")
        db = self.home / f".gemini/antigravity-cli/conversations/{conversation}.db"
        non_object_arguments = wire_bytes(5, wire_bytes(4, wire_bytes(2, b"run_command") + wire_bytes(3, b"[]")))
        for blob in (b"\x2a\xff", b"\x2a\x05ab", b"\x2a\x01\x00", non_object_arguments):
            with sqlite3.connect(db) as connection:
                connection.execute("UPDATE steps SET step_payload=? WHERE status=7", (blob,))
            denial = {"conversation_id": conversation, "denied_actions": [{"action": "command"}]}
            code, result = self.review("google", FAKE_AGY_JSON=json.dumps(denial),
                                       FAKE_AGY_CWD_FILE=str(self.root / "cwd"))
            self.assertEqual((code, result["ok"]), (1, False))
            self.assertIn("command_diagnostic", result)

    def commit_base_on_main(self, text: str) -> Path:
        """Make the reviewed repository one committed file whose HEAD is origin/main."""
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.com"], check=True)
        source = self.repo / "example.txt"
        source.write_text(text)
        subprocess.run(["git", "-C", str(self.repo), "add", "example.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        return source

    def test_inputs_outside_the_repository_are_copied_where_the_reviewer_can_read(self) -> None:
        self.install("agy", FAKE_AGY)
        self.commit_base_on_main("base\n")
        issue = self.root / "saved-issue.md"
        issue.write_text("# The pending question\n")
        seen = self.root / "agy-input"
        env = {"FAKE_AGY_JSON": json.dumps({"response": completed("none")}), "FAKE_AGY_CWD_FILE": str(self.root / "cwd"),
               "FAKE_AGY_INPUT_NAME": issue.name, "FAKE_AGY_INPUT_FILE": str(seen)}
        code, result = self.run_helper("run", "--provider", "google", "--repo", str(self.repo),
                                       "--prompt-file", str(self.prompt), "--input", str(issue), **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertEqual(seen.read_text(), "# The pending question\n")
        named = Path(Path(f"{seen}.path").read_text())
        self.assertTrue(named.is_relative_to(self.repo.resolve()), "inputs sit under the reviewer's read root")
        self.assertFalse(list(self.repo.glob(".model-review-*")), "copied inputs are removed with the scratch")
        code, result = self.run_helper("run", "--provider", "google", "--repo", str(self.repo),
                                       "--prompt-file", str(self.prompt), "--input", str(self.root / "absent.md"), **env)
        self.assertEqual((code, result["ok"], result["phase"]), (1, False, "inputs"))

    def test_links_in_the_request_are_reported_to_caller_and_reviewer(self) -> None:
        self.install("agy", FAKE_AGY)
        self.commit_base_on_main("base\n")
        sent = self.root / "agy-prompt"
        env = {"FAKE_AGY_JSON": json.dumps({"response": completed("none")}), "FAKE_AGY_CWD_FILE": str(self.root / "cwd"),
               "FAKE_AGY_PROMPT_FILE": str(sent)}
        code, result = self.review("google", **env)
        self.assertEqual(code, 0)
        self.assertNotIn("unreachable_links", result)
        without_links = sent.read_text()
        link = "https://github.com/OWNER/REPO/issues/1#issuecomment-2"
        self.prompt.write_text(f"Review skills/x.md. Context: {link}, and again ({link}).\n")
        code, result = self.review("google", **env)
        self.assertEqual((code, result["unreachable_links"]), (0, [link]))
        self.assertIn("--input", result["unreachable_links_hint"])
        told = sent.read_text().replace(f" Context: {link}, and again ({link}).", "")
        self.assertNotEqual(told, without_links, "the reviewer is told the links cannot be opened")

    def test_links_keep_their_own_brackets_and_lose_surrounding_prose(self) -> None:
        wiki = "https://en.wikipedia.org/wiki/Capability-based_security_(computing)"
        quoted = "https://example.com/it's-here"
        prompt = (f"See {wiki}. Also ({wiki}), [the page]({quoted}), '{quoted}' and "
                  f"<https://example.com/a>; https://example.com/b?x=1&y=[2].")
        self.assertEqual(review_with_model.unreachable_links(prompt),
                         [wiki, quoted, "https://example.com/a", "https://example.com/b?x=1&y=[2]"])

    def test_branch_diff_is_given_as_a_temporary_file_and_no_diff_still_runs(self) -> None:
        self.install("agy", FAKE_AGY)
        source = self.commit_base_on_main("before\n")
        subprocess.run(["git", "-C", str(self.repo), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"], check=True)
        prompt_file, diff_file = self.root / "agy-prompt", self.root / "agy-diff"
        env = {"FAKE_AGY_JSON": json.dumps({"response": completed("none")}), "FAKE_AGY_CWD_FILE": str(self.root / "cwd"),
               "FAKE_AGY_PROMPT_FILE": str(prompt_file), "FAKE_AGY_DIFF_FILE": str(diff_file)}
        code, result = self.review("google", **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertFalse(diff_file.exists(), "no diff file is named when the branch has no changes")
        source.write_text("after\n")
        code, result = self.review("google", **env)
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("uncommitted", result["error"])
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "change"], check=True)
        code, result = self.review("google", **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertIn("+after", diff_file.read_text())
        self.assertFalse(list(self.repo.glob(".model-review-*")), "the temporary diff must be removed")
        subprocess.run(["git", "-C", str(self.repo), "symbolic-ref", "--delete", "refs/remotes/origin/HEAD"], check=True)
        diff_file.unlink()
        code, result = self.review("google", **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertIn("+after", diff_file.read_text(), "origin/main works without origin/HEAD")
        source.write_bytes(b"after\xff\n")
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "binary change"], check=True)
        code, result = self.review("google", **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertIn("after\\xff", diff_file.read_text(), "non-UTF-8 bytes remain legible to reviewers")
        leftover = self.repo / ".model-review-Ab12_cd" / "change.diff"
        leftover.parent.mkdir()
        leftover.write_text("left by a killed or parallel reviewer\n")
        code, result = self.review("google", **env)
        self.assertEqual((code, result["response"]), (0, "none"))
        self.assertTrue(leftover.exists(), "the helper must not remove another review's scratch")
        (self.repo / "new.py").write_text("print('new')\n")
        code, result = self.review("google", **env)
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("untracked", result["error"])
        subdir = self.repo / "subdir"
        subdir.mkdir()
        code, result = self.run_helper(
            "run", "--provider", "google", "--repo", str(subdir), "--prompt-file", str(self.prompt), **env,
        )
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("untracked", result["error"], "a subdirectory target must check the whole worktree")

    def test_a_reviewer_that_could_write_or_was_denied_a_read_is_a_failure(self) -> None:
        self.install("claude", FAKE_CLAUDE)
        self.install("agy", FAKE_AGY)
        denied = json.dumps({"is_error": False, "result": "I could not open it.", "permission_denials": [{"tool_name": "Read"}]})
        code, result = self.review("anthropic", FAKE_CLAUDE_JSON=denied)
        self.assertEqual((code, result["denied"]), (1, ["Read"]))
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"permissions": {"allow": [f"write_file({self.repo})", "command(ls)"]}}))
        cwd_file = self.root / "agy-cwd"
        code, result = self.review("google", FAKE_AGY_JSON="{}", FAKE_AGY_CWD_FILE=str(cwd_file))
        self.assertEqual((code, result["rules"]), (1, [f"write_file({self.repo})"]))
        self.assertFalse(cwd_file.exists(), "agy must not be started when it could write")
        for write_tool in ("write_to_file", "replace_file_content"):
            settings.write_text(json.dumps({"permissions": {"allow": [f"{write_tool}({self.repo})"]}}))
            code, result = self.review("google", FAKE_AGY_JSON="{}", FAKE_AGY_CWD_FILE=str(cwd_file))
            self.assertEqual((code, result["rules"]), (1, [f"{write_tool}({self.repo})"]))
            self.assertFalse(cwd_file.exists())
        for command in ("find", "rg"):
            settings.write_text(json.dumps({"permissions": {"allow": [f"command({command})"]}}))
            code, result = self.review("google", FAKE_AGY_JSON="{}", FAKE_AGY_CWD_FILE=str(cwd_file))
            self.assertEqual((code, result["rules"]), (1, [f"command({command})"]))
            self.assertFalse(cwd_file.exists(), "agy must not start with an executable command allow rule")
            # A stale plain command grant is what `repair` removes, so the failure says to run it.
            self.assertEqual(result["stale_command_grants"], [f"command({command})"])
            self.assertIn("repair", result["hint"])
        # A write rule is never repaired automatically; the hint says to fix it by hand.
        settings.write_text(json.dumps({"permissions": {"allow": ["command(find)", f"write_file({self.repo})"]}}))
        code, result = self.review("google", FAKE_AGY_JSON="{}", FAKE_AGY_CWD_FILE=str(cwd_file))
        self.assertEqual(result["rules"], ["command(find)", f"write_file({self.repo})"])
        self.assertNotIn("repair", result["hint"])
        self.assertIn("Only you", result["hint"])
        # An allow value that is not a list is one confusing rule, not a rule per character.
        settings.write_text(json.dumps({"permissions": {"allow": "command(find)"}}))
        code, result = self.review("google", FAKE_AGY_JSON="{}", FAKE_AGY_CWD_FILE=str(cwd_file))
        self.assertEqual((code, len(result["rules"])), (1, 1))
        self.assertIn("not a list", result["rules"][0])
        settings.write_text(json.dumps({"permissions": {"allow": ["command(grep)", "command(ls)", "command(wc)"]}}))
        code, result = self.review(
            "google", FAKE_AGY_JSON=json.dumps({"response": completed("reviewed"), "denied_actions": []}),
            FAKE_AGY_CWD_FILE=str(cwd_file),
        )
        self.assertEqual((code, result["response"]), (0, "reviewed"))
        self.assertTrue(cwd_file.exists(), "safe command rules must let the reviewer run")

    def test_check_probes_any_text_file_and_does_not_pass_when_nothing_is_usable(self) -> None:
        (self.repo / "a.md").write_text("\n  \n")
        # A JSON file's first non-empty line is a lone brace. A reviewer that reads it answers "{",
        # so the probe must skip this file instead of expecting a later line the prompt never asked for.
        (self.repo / "a.json").write_text('{\n  "name": "shared"\n}\n')
        (self.repo / "b.txt").write_text("\nthe probe line\n")
        code, report = self.run_helper("check", "--repo", str(self.repo))
        self.assertEqual((code, report["probe"]), (1, str(self.repo / "b.txt")))
        self.assertEqual({item["state"] for item in report["providers"]}, {"not installed"})
        self.install("codex", FAKE_CODEX)
        code, report = self.run_helper("check", "--repo", str(self.repo), FAKE_ANSWER=completed("the probe line"))
        self.assertEqual(code, 0)
        self.assertIn("ready", {item["state"] for item in report["providers"]})

    def test_a_locked_out_reviewer_is_a_failure_that_says_how_to_fix_it(self) -> None:
        self.install("agy", FAKE_AGY)
        agy_json = json.dumps({"response": "", "denied_actions": [{"action": "read_file"}]})
        code, result = self.review("google", FAKE_AGY_JSON=agy_json, FAKE_AGY_CWD_FILE=str(self.root / "cwd"))
        self.assertEqual((code, result["ok"], result["denied"]), (1, False, ["read_file"]))
        # The suggested rule names this repository, not its parent: the parent could be a home directory.
        self.assertIn(f"read_file({self.repo})", result["hint"])
        self.assertNotIn(f"read_file({self.repo.parent})\"", result["hint"])
        self.assertIn("configure --read-root", result["hint"])
        self.assertFalse((self.home / ".gemini").exists(), "a failed run must not create or edit user settings")

    def test_an_empty_answer_or_a_provider_error_is_never_reported_as_no_findings(self) -> None:
        self.install("codex", FAKE_CODEX)
        self.install("claude", FAKE_CLAUDE)
        code, result = self.review("openai", FAKE_ANSWER="  \n")
        self.assertEqual((code, result["ok"], result["error"]), (1, False, "the reviewer returned nothing"))
        limit = json.dumps({"is_error": True, "result": "You've hit your monthly spend limit"})
        code, result = self.review("anthropic", FAKE_CLAUDE_JSON=limit)
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("spend limit", result["detail"])

    def test_openai_without_successful_shell_evidence_fails_even_with_an_answer(self) -> None:
        self.install("codex", FAKE_CODEX)
        refusal = "I couldn't perform the review: no file-reading tool, and you prohibited commands."
        final = {"type": "item.completed", "item": {"type": "agent_message", "text": refusal}}
        for events in ("", json.dumps(final), json.dumps({"type": "item.started", "item": {
                "type": "command_execution", "status": "in_progress"}}), json.dumps({
                "type": "item.completed", "item": {"type": "command_execution", "status": "failed",
                "exit_code": 1}})):
            with self.subTest(events=events):
                code, result = self.review("openai", FAKE_ANSWER=refusal, FAKE_CODEX_EVENTS=events)
                self.assertEqual((code, result["ok"], result["successful_commands"]), (1, False, 0))
                if 'command_execution' in events:
                    self.assertIn("attempted shell commands", result["error"])
                    self.assertNotIn("Do not forbid commands", result["error"])
                else:
                    self.assertIn("Do not forbid commands", result["error"])
                self.assertNotIn("response", result)
        code, result = self.review("openai", FAKE_ANSWER=completed("none"), FAKE_CODEX_EVENTS="not JSON")
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("JSONL tool evidence", result["error"])

    def test_openai_successful_shell_evidence_accepts_none_and_reports_requested_model_honestly(self) -> None:
        self.install("codex", FAKE_CODEX)
        code, result = self.run_helper("run", "--provider", "openai", "--repo", str(self.repo),
                                       "--prompt-file", str(self.prompt), "--model", "requested-model",
                                       FAKE_ANSWER=completed("none"))
        self.assertEqual((code, result["response"], result["successful_commands"]), (0, "none", 1))
        self.assertEqual(result["model"], "requested-model")
        self.assertIn("not reported", result["model_source"])
        code, result = self.review("openai", FAKE_ANSWER=completed("none"))
        self.assertEqual((code, result["model"], result["model_source"]), (0, None, "unknown"))

    def test_openai_jsonl_provider_errors_keep_their_reason(self) -> None:
        self.install("codex", FAKE_CODEX)
        for event in ({"type": "error", "message": "usage limit reached"},
                      {"type": "turn.failed", "error": {"message": "unknown model"}}):
            with self.subTest(event=event):
                code, result = self.review("openai", FAKE_CODEX_EVENTS=json.dumps(event), FAKE_CODEX_EXIT="1")
                self.assertEqual((code, result["ok"]), (1, False))
                self.assertEqual(result["detail"], event.get("message") or event["error"]["message"])

    def test_recorded_incomplete_review_preserves_the_execution_cause(self) -> None:
        fixture = json.loads(SCRIPT.with_name("fixtures").joinpath("openai-incomplete-2166.json").read_text())
        events = fixture["events"]
        proc = subprocess.CompletedProcess([], fixture["returncode"],
                                           "\n".join(map(json.dumps, events)), "")
        scratch = self.root / "scratch"
        scratch.mkdir()
        (scratch / "answer.md").write_text(fixture["answer"])
        elapsed = fixture["provenance"]["duration_seconds"]
        with patch.object(review_with_model, "time") as clock:
            clock.monotonic.return_value = 10

            def finish(*_args):
                clock.monotonic.return_value += elapsed
                return proc

            with patch.object(review_with_model, "run_cli", side_effect=finish) as run:
                result = review_with_model.completed_review(
                    review_with_model.review_openai("private prompt", self.repo, None, 1800, scratch))
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], json.loads(fixture["answer"])["status"])
        execution = result["execution"]
        self.assertEqual(execution["terminal_event"], "turn.completed")
        self.assertEqual(execution["last_event"], events[-1]["type"])
        self.assertEqual(execution["elapsed_seconds"], elapsed)
        self.assertEqual(execution["exit_code"], proc.returncode)
        self.assertTrue(execution["final_answer_produced"])
        self.assertEqual(execution["command_diagnostics"], ["github_connection_failed"])
        self.assertEqual(execution["failed_commands"], 0)  # The trailing cat hid gh's exit code.
        self.assertTrue(result["completion_shape"])
        self.assertNotIn("response", result)
        run.assert_called_once()

    def test_failed_command_count_includes_declines_without_an_exit_code(self) -> None:
        events = [
            {"type": "item.completed", "item": {"type": "command_execution", "status": status, "exit_code": code}}
            for status, code in [("completed", 0), ("failed", None), ("declined", None), ("failed", 1)]
        ]
        execution = review_with_model.openai_execution("\n".join(map(json.dumps, events)), "", 1, 0)
        self.assertEqual(execution["failed_commands"], 3)

    def test_incomplete_diagnostics_do_not_expose_command_or_partial_review_text(self) -> None:
        self.install("codex", FAKE_CODEX)
        output = self.root / "review.md"
        events = [
            {"type": "item.completed", "item": {"type": "command_execution", "status": "completed", "exit_code": 0,
                "command": "token=PRIVATE-COMMAND", "aggregated_output": "PRIVATE-SOURCE\n" * 1000 +
                "error connecting to api.github.com\n"}},
            {"type": "PRIVATE-EVENT-token=private"},
        ]
        code, result = self.run_helper(
            "run", "--provider", "openai", "--repo", str(self.repo), "--prompt-file", str(self.prompt),
            "--out", str(output), FAKE_CODEX_EVENTS="\n".join(map(json.dumps, events)),
            FAKE_ANSWER=json.dumps({"status": "incomplete", "review": "PRIVATE-PARTIAL" * 1000}))
        self.assertEqual(code, 1)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertFalse(output.exists())
        self.assertEqual(result["execution"]["last_event"], "unknown")
        self.assertIsNone(result["execution"]["terminal_event"])
        self.assertLess(len(json.dumps(result["execution"])), 500)

    def test_openai_timeout_keeps_terminal_evidence_without_the_partial_answer(self) -> None:
        stdout = b'{"type":"turn.started"}\n{"type":"item.started"}\n{"partial'
        scratch = self.root / "scratch"
        scratch.mkdir()
        (scratch / "answer.md").write_text("PRIVATE-PARTIAL")
        with patch.object(review_with_model, "run_cli", side_effect=subprocess.TimeoutExpired(
                ["codex", "private prompt"], 20, output=stdout, stderr=b"token=PRIVATE-STDERR")) as run:
            result = review_with_model.review_openai("private prompt", self.repo, None, 20, scratch)
        self.assertFalse(result["ok"])
        self.assertEqual(result["execution"]["jsonl_state"], "malformed")
        self.assertEqual(result["execution"]["last_event"], "item.started")
        self.assertIsNone(result["execution"]["terminal_event"])
        self.assertIsNone(result["execution"]["exit_code"])
        self.assertTrue(result["execution"]["final_answer_produced"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertGreaterEqual(result["execution"]["elapsed_seconds"], 0)
        run.assert_called_once()

    def test_openai_interrupted_reconnect_retains_distinct_stderr(self) -> None:
        self.install("codex", FAKE_CODEX)
        event = {"type": "error", "message": "Reconnecting... 2/5"}
        code, result = self.review("openai", FAKE_CODEX_EVENTS=json.dumps(event),
                                   FAKE_CODEX_EXIT="130", FAKE_CODEX_STDERR="interrupted by SIGINT")
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertIn("Reconnecting... 2/5", result["detail"])
        self.assertIn("interrupted by SIGINT", result["stderr_detail"])

    def test_openai_failure_details_are_bounded_and_redact_credentials(self) -> None:
        self.install("codex", FAKE_CODEX)
        event = {"type": "error", "message": "retry " * 100 + "token=private-provider-token"}
        stderr = "connection failed " * 100 + "Bearer private-bearer https://example.test/?key=private-url-key"
        code, result = self.review("openai", FAKE_CODEX_EVENTS=json.dumps(event),
                                   FAKE_CODEX_EXIT="1", FAKE_CODEX_STDERR=stderr)
        self.assertEqual((code, result["ok"]), (1, False))
        for detail in (result["detail"], result["stderr_detail"]):
            self.assertLessEqual(len(detail), 400)
            self.assertIn("redacted", detail)
            self.assertNotIn("private-", detail)
        # When stderr repeats the provider error, keep one copy.
        event["message"] = "usage limit reached"
        code, result = self.review("openai", FAKE_CODEX_EVENTS=json.dumps(event),
                                   FAKE_CODEX_EXIT="1", FAKE_CODEX_STDERR=event["message"] + "\n")
        self.assertEqual((code, result["detail"]), (1, event["message"]))
        self.assertNotIn("stderr_detail", result)

    def test_openai_cli_failures_redact_env_and_json_credentials(self) -> None:
        self.install("codex", FAKE_CODEX)
        for events in (json.dumps({"type": "error", "message": 'HTTP failure: "refresh_token": "private-json-value"'}),
                       "not JSON"):
            with self.subTest(events=events):
                code, result = self.review("openai", FAKE_CODEX_EVENTS=events, FAKE_CODEX_EXIT="1",
                                           FAKE_CODEX_STDERR="OPENAI_API_KEY=private-env-value")
                self.assertEqual((code, result["ok"]), (1, False))
                self.assertNotIn("private-", json.dumps(result))
                self.assertIn("redacted", json.dumps(result))
                self.assertNotIn("not JSON", json.dumps(result))

    def test_openai_redacts_escaped_error_body_and_long_token_before_truncating(self) -> None:
        self.install("codex", FAKE_CODEX)
        for stderr in (r'body: {\"refresh_token\":\"private-escaped-value\"}',
                       "token=" + "private-long-value-" * 50):
            with self.subTest(stderr=stderr):
                code, result = self.review("openai", FAKE_CODEX_EVENTS="", FAKE_CODEX_EXIT="1",
                                           FAKE_CODEX_STDERR=stderr)
                self.assertEqual((code, result["ok"]), (1, False))
                self.assertNotIn("private-", result["detail"])
                self.assertIn("redacted", result["detail"])
                self.assertLessEqual(len(result["detail"]), 400)

    def test_openai_recovered_stream_error_does_not_discard_a_completed_review(self) -> None:
        self.install("codex", FAKE_CODEX)
        events = [
            {"type": "error", "message": "Reconnecting... 1/5"},
            {"type": "item.completed", "item": {"type": "command_execution", "status": "completed",
                                                 "exit_code": 0}},
            {"type": "turn.completed", "usage": {}},
        ]
        code, result = self.review("openai", FAKE_CODEX_EVENTS="\n".join(map(json.dumps, events)), FAKE_ANSWER=completed("none"))
        self.assertEqual((code, result["ok"], result["response"]), (0, True, "none"))

    def test_check_keeps_model_provenance_when_the_probe_answer_is_wrong(self) -> None:
        self.install("codex", FAKE_CODEX)
        (self.repo / "probe.txt").write_text("the probe line\n")
        code, result = self.run_helper("check", "--repo", str(self.repo), "--provider", "openai", FAKE_ANSWER=completed("none"))
        self.assertEqual((code, result["providers"][0]["state"]), (1, "not ready"))
        self.assertEqual(result["providers"][0]["model_source"], "unknown")

    def test_openai_reports_the_model_from_a_cli_reroute_notice(self) -> None:
        self.install("codex", FAKE_CODEX)
        events = [
            {"type": "item.completed", "item": {"type": "error",
                                                 "message": "model rerouted: requested -> replacement (Safety)"}},
            {"type": "item.completed", "item": {"type": "command_execution", "status": "completed",
                                                 "exit_code": 0}},
            {"type": "turn.completed", "usage": {}},
        ]
        code, result = self.review("openai", FAKE_CODEX_EVENTS="\n".join(map(json.dumps, events)), FAKE_ANSWER=completed("none"))
        self.assertEqual((code, result["model"], result["model_source"]),
                         (0, "replacement", "reported by the CLI (rerouted)"))

    def test_google_reports_the_model_agy_resolved_in_cli_log(self) -> None:
        self.install("agy", FAKE_AGY)
        base = self.home / ".gemini" / "antigravity-cli"
        base.mkdir(parents=True)
        (base / "settings.json").write_text(json.dumps({"model": "retired-alias"}))
        conversation = "12345678-1234-1234-1234-123456789abc"
        other = "87654321-4321-4321-4321-cba987654321"
        env = {"FAKE_AGY_JSON": json.dumps({"response": completed("none"), "conversation_id": conversation, "denied_actions": []}),
               "FAKE_AGY_CWD_FILE": str(self.root / "cwd")}
        rerouted_log = (
            'I1013 16:57:41.887720 1 model_resolver.go:155] model alias "retired-alias" resolved to "replacement"\n'
            'I1013 16:57:41.888060 1 model_config_manager.go:340] Propagating selected model override to backend: label="replacement"\n'
            f"I1013 16:57:42.943221 1 server.go:1219] Created conversation {conversation}\n"
        )
        (base / "cli.log").write_text(rerouted_log)
        code, result = self.review("google", **env)
        self.assertNotEqual(result["model"], "retired-alias")
        self.assertEqual((code, result["model"], result["model_source"]),
                         (0, "replacement", "reported by the CLI (rerouted)"))
        selected_model = "selected (High)"
        (base / "cli.log").write_text(
            f'I1013 17:14:50.632344 165 model_config_manager.go:340] Propagating selected model override to backend: label="{selected_model}"\n'
            f"I1013 17:14:50.865659 413 server.go:1219] Created conversation {conversation}\n"
        )
        code, result = self.review("google", **env)
        self.assertEqual((code, result["model"], result["model_source"]),
                         (0, selected_model, "reported by the CLI"))
        self.assertEqual(result["effort"], "high")
        # A cli.log pointing at another session's conversation falls back to the run's own log or request.
        (base / "cli.log").write_text(
            'I1013 17:15:00.000000 1 model_config_manager.go:340] Propagating selected model override to backend: label="other-session"\n'
            f"I1013 17:15:01.000000 1 server.go:1219] Created conversation {other}\n"
        )
        code, result = self.review("google", **env)
        self.assertEqual((code, result["model"], result["model_source"]),
                         (0, "retired-alias", "requested, not reported by the CLI"))
        (base / "log").mkdir()
        (base / "log" / "cli-20261013_165740.log").write_text(rerouted_log)
        code, result = self.review("google", **env)
        self.assertEqual((code, result["model"], result["model_source"]),
                         (0, "replacement", "reported by the CLI (rerouted)"))

    def test_google_isolates_concurrent_model_logs_and_rejects_shared_log_attribution(self) -> None:
        base = self.home / ".gemini" / "antigravity-cli"
        (base / "log").mkdir(parents=True)
        first_conversation = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        second_conversation = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        shared_log_text = (
            'I1015 20:00:00.100000 1 model_config_manager.go:340] Propagating selected model override to backend: label="resolved-first"\n'
            f"I1015 20:00:00.200000 1 server.go:1233] Created conversation {first_conversation}\n"
            'I1015 20:00:00.300000 2 model_config_manager.go:340] Propagating selected model override to backend: label="resolved-second"\n'
            f"I1015 20:00:00.400000 2 server.go:1233] Created conversation {second_conversation}\n"
        )
        for shared_path in (base / "cli.log", base / "log" / "cli-20261015_200000.log"):
            shared_path.write_text(shared_log_text)
        self.install(
            "agy",
            """#!/bin/sh
log_file=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--log-file" ]; then
    log_file="$2"
    shift 2
    continue
  fi
  shift
done
if [ -n "$log_file" ] && [ -n "$FAKE_AGY_OWN_LOG" ]; then
  printf '%s' "$FAKE_AGY_OWN_LOG" > "$log_file"
fi
printf '%s' "$FAKE_AGY_JSON"
""",
        )

        def run_one(requested: str, resolved: str, conversation: str, write_own_log: bool,
                    log_conversation: str | None = None) -> tuple[int, dict]:
            recorded_conversation = log_conversation or conversation
            own_log = (
                f'I1015 20:00:00.100000 1 model_config_manager.go:340] Propagating selected model override to backend: label="{resolved}"\n'
                f'I1015 20:00:00.150000 1 model_config_manager.go:340] Propagating selected model override to backend: label="{resolved}"\n'
                f"I1015 20:00:00.200000 1 server.go:1233] Created conversation {recorded_conversation}\n"
            )
            return self.run_helper(
                "run", "--provider", "google", "--repo", str(self.repo),
                "--prompt-file", str(self.prompt), "--model", requested,
                FAKE_AGY_OWN_LOG=own_log if write_own_log else "",
                FAKE_AGY_JSON=json.dumps({"response": completed("none"), "conversation_id": conversation, "denied_actions": []}),
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(run_one, "requested-first", "resolved-first", first_conversation, True)
            second_future = pool.submit(run_one, "requested-second", "resolved-second", second_conversation, True)
            first_code, first_result = first_future.result()
            second_code, second_result = second_future.result()

        self.assertEqual(
            (first_code, first_result["model"], first_result["model_source"]),
            (0, "resolved-first", "reported by the CLI"),
        )
        self.assertEqual(
            (second_code, second_result["model"], second_result["model_source"]),
            (0, "resolved-second", "reported by the CLI"),
        )
        self.assertFalse(list(self.repo.glob(".model-review-*")), "per-run agy logs inside scratch must be cleaned up")

        # When only a shared multi-conversation log is available, neither run may certify the other's label.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(run_one, "requested-first", "resolved-first", first_conversation, False)
            second_future = pool.submit(run_one, "requested-second", "resolved-second", second_conversation, False)
            first_code, first_result = first_future.result()
            second_code, second_result = second_future.result()

        self.assertEqual(
            (first_code, first_result["model"], first_result["model_source"]),
            (0, "requested-first", "requested, not reported by the CLI"),
        )
        self.assertEqual(
            (second_code, second_result["model"], second_result["model_source"]),
            (0, "requested-second", "requested, not reported by the CLI"),
        )

        # A log with one Created conversation line but conflicting model labels or reroutes is also unconfirmed.
        (base / "log" / "cli-20261015_200000.log").unlink()
        for conflicting_log in (
            (
                'I1015 20:00:00.100000 1 model_config_manager.go:340] Propagating selected model override to backend: label="resolved-first"\n'
                f"I1015 20:00:00.200000 1 server.go:1233] Created conversation {first_conversation}\n"
                'I1015 20:00:00.300000 2 model_config_manager.go:340] Propagating selected model override to backend: label="resolved-second"\n'
            ),
            (
                'I1015 20:00:00.100000 1 model_resolver.go:155] model alias "requested-first" resolved to "rerouted-first"\n'
                'I1015 20:00:00.150000 1 model_config_manager.go:340] Propagating selected model override to backend: label="resolved-second"\n'
                f"I1015 20:00:00.200000 1 server.go:1233] Created conversation {first_conversation}\n"
            ),
        ):
            (base / "cli.log").write_text(conflicting_log)
            code, result = run_one("requested-first", "resolved-first", first_conversation, False)
            self.assertEqual(
                (code, result["model"], result["model_source"]),
                (0, "requested-first", "requested, not reported by the CLI"),
            )

        # A non-empty per-run log with a mismatched conversation_id must not fall through to a shared cli.log.
        (base / "cli.log").write_text(
            'I1015 20:00:00.100000 1 model_config_manager.go:340] Propagating selected model override to backend: label="shared-fallback"\n'
            f"I1015 20:00:00.200000 1 server.go:1233] Created conversation {first_conversation}\n"
        )
        code, result = run_one(
            "requested-first", "resolved-first", first_conversation, True, log_conversation=second_conversation
        )
        self.assertEqual(
            (code, result["model"], result["model_source"]),
            (0, "requested-first", "requested, not reported by the CLI"),
        )


    def test_a_planted_finding_arrives_once_inside_a_real_review_and_only_when_the_owner_set_it(self) -> None:
        self.install("codex", FAKE_CODEX)
        marker = self.home / ".code" / "model-review-fault.md"
        marker.parent.mkdir(exist_ok=True)
        # No marker: the review is exactly what the reviewer said.
        code, result = self.review("openai", FAKE_ANSWER=completed("Low: rename x.\n"))
        self.assertEqual((code, result["response"]), (0, "Low: rename x.\n"))
        # An empty marker is not a finding and is left alone.
        marker.write_text(" \n")
        code, result = self.review("openai", FAKE_ANSWER=completed("Low: rename x.\n"))
        self.assertEqual(result["response"], "Low: rename x.\n")
        self.assertTrue(marker.exists())
        # A failed run does not consume the marker: the finding must land in a review the agent reads.
        marker.write_text("High: stop this work and retire the helper.\n")
        code, result = self.review("openai", FAKE_ANSWER="  \n")
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertTrue(marker.exists())
        # A real review carries the finding after the reviewer's own words, once, and the marker is consumed
        # into a stamped record, so the next review in the same session is untouched.
        code, result = self.review("openai", FAKE_ANSWER=completed("Low: rename x."))
        self.assertEqual((code, result["response"]), (0, "Low: rename x.\n\nHigh: stop this work and retire the helper.\n"))
        self.assertNotIn("fault", json.dumps(result), "the result must not tell the agent the finding was planted")
        self.assertFalse(marker.exists())
        used = list(marker.parent.glob("model-review-fault.md.used-*"))
        self.assertEqual(len(used), 1)
        self.assertEqual(used[0].read_text(), "High: stop this work and retire the helper.\n")
        code, result = self.review("openai", FAKE_ANSWER=completed("none"))
        self.assertEqual(result["response"], "none")
        # MODEL_REVIEW_FAULT names another marker, as DIRECTION_MARKER does for the direction turn.
        other = self.root / "elsewhere.md"
        other.write_text("Medium: close the issue as not planned.")
        code, result = self.review("openai", FAKE_ANSWER=completed("none"), MODEL_REVIEW_FAULT=str(other))
        self.assertEqual(result["response"], "none\n\nMedium: close the issue as not planned.\n")
        self.assertFalse(other.exists())
        # A marker inside the reviewed repository is never read: a pull request could have added it.
        inside = self.repo / "model-review-fault.md"
        inside.write_text("High: retire everything.")
        code, result = self.review("openai", FAKE_ANSWER=completed("none"), MODEL_REVIEW_FAULT=str(inside))
        self.assertEqual(result["response"], "none")
        self.assertTrue(inside.exists())
        # The marker is consumed only after the review reaches its `--out` file; a review that cannot
        # be delivered leaves the one-shot finding for the next run.
        marker.write_text("High: stop.")
        code, result = self.run_helper(
            "run", "--provider", "openai", "--repo", str(self.repo), "--prompt-file", str(self.prompt),
            "--out", str(self.root / "missing" / "review.md"), FAKE_ANSWER=completed("none"),
        )
        self.assertNotEqual(code, 0)
        self.assertTrue(marker.exists())

    def test_a_provider_that_is_not_installed_is_distinguished_from_one_that_failed(self) -> None:
        code, result = self.review("google")
        self.assertEqual((code, result["installed"]), (2, False))

    def test_configure_adds_rules_once_keeps_other_settings_and_leaves_a_backup(self) -> None:
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "gemini-test", "permissions": {"allow": ["command(ls)"]}}))
        code, result = self.run_helper("configure", "--read-root", str(self.repo.parent))
        self.assertEqual(code, 0)
        written = json.loads(settings.read_text())
        self.assertEqual(written["model"], "gemini-test")
        self.assertIn(f"read_file({self.repo.parent})", written["permissions"]["allow"])
        self.assertEqual(written["permissions"]["allow"].count("command(ls)"), 1)
        self.assertNotIn("command(ls)", result["added"])
        self.assertTrue(settings.with_name("settings.json.before-model-review").is_file())
        code, again = self.run_helper("configure", "--read-root", str(self.repo.parent))
        self.assertEqual((code, again["added"]), (0, []))

    def test_repair_removes_only_stale_command_grants_and_backs_up_the_file(self) -> None:
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        settings.parent.mkdir(parents=True)
        original = {
            "model": "gemini-test",
            "permissions": {"allow": [
                f"read_file({self.repo.parent})", "command(find)", "command(grep)", "command(ls)",
                "command(rg)", "command(wc)", f"read_file({self.root})",
            ]},
            "statusLine": {"type": "command", "command": "/usr/bin/true"},
        }
        settings.write_text(json.dumps(original))
        code, result = self.run_helper("repair")
        self.assertEqual((code, result["ok"], result["removed"]), (0, True, ["command(find)", "command(rg)"]))
        written = json.loads(settings.read_text())
        self.assertEqual(written["permissions"]["allow"], [
            f"read_file({self.repo.parent})", "command(grep)", "command(ls)", "command(wc)", f"read_file({self.root})",
        ])
        self.assertEqual((written["model"], written["statusLine"]), (original["model"], original["statusLine"]))
        backup = Path(result["backup"])
        self.assertEqual(backup.parent, settings.parent)
        self.assertTrue(backup.name.startswith("settings.json.before-model-review-repair-"))
        self.assertEqual(json.loads(backup.read_text()), original, "the backup is the file as it was")
        backups = list(settings.parent.glob("settings.json.before-model-review-repair-*"))
        # Repaired settings let the reviewer start, and a second repair changes nothing and adds no backup.
        self.install("agy", FAKE_AGY)
        cwd_file = self.root / "agy-cwd"
        code, review = self.review(
            "google", FAKE_AGY_JSON=json.dumps({"response": completed("reviewed"), "denied_actions": []}),
            FAKE_AGY_CWD_FILE=str(cwd_file),
        )
        self.assertEqual((code, review["response"]), (0, "reviewed"))
        code, again = self.run_helper("repair")
        self.assertEqual((code, again["removed"], "backup" in again), (0, [], False))
        self.assertEqual(list(settings.parent.glob("settings.json.before-model-review-repair-*")), backups)
        # A dotfiles-style symlink stays a symlink: the repair lands in the real file behind it.
        real = self.root / "dotfiles" / "agy-settings.json"
        real.parent.mkdir()
        real.write_text(json.dumps(original))
        settings.unlink()
        settings.symlink_to(real)
        code, result = self.run_helper("repair")
        self.assertEqual((code, result["removed"]), (0, ["command(find)", "command(rg)"]))
        self.assertTrue(settings.is_symlink(), "the link must not be replaced by a plain file")
        self.assertNotIn("command(find)", json.loads(real.read_text())["permissions"]["allow"])
        self.assertEqual(Path(result["backup"]).parent, real.parent)

    def test_repair_refuses_ambiguous_rules_and_unusable_settings_without_changing_anything(self) -> None:
        settings = self.home / ".gemini" / "antigravity-cli" / "settings.json"
        code, result = self.run_helper("repair")
        self.assertEqual((code, result["ok"]), (1, False))
        self.assertFalse(settings.parent.exists(), "a refused repair creates nothing")
        settings.parent.mkdir(parents=True)
        # A grant for the user's own program is theirs, not a retired reviewer grant: never removed.
        for ambiguous in (f"write_file({self.repo})", "command(find -delete)", "command(rg --pre=sh)", "command(git)", 7):
            with self.subTest(ambiguous=ambiguous):
                body = json.dumps({"permissions": {"allow": ["command(find)", ambiguous, "command(ls)"]}})
                settings.write_text(body)
                code, result = self.run_helper("repair")
                self.assertEqual((code, result["ok"], result["ambiguous"]), (1, False, [str(ambiguous)]))
                self.assertEqual(result["would_remove"], ["command(find)"])
                self.assertEqual(settings.read_text(), body, "an ambiguous rule stops the whole repair")
                self.assertFalse(list(settings.parent.glob("settings.json.before-*")))
        for body in ('{"permissions": {"allow": "command(find)"}}', '{"permissions": []}', "[]", "not json"):
            with self.subTest(body=body):
                settings.write_text(body)
                code, result = self.run_helper("repair")
                self.assertEqual((code, result["ok"]), (1, False))
                self.assertEqual(settings.read_text(), body)
                self.assertFalse(list(settings.parent.glob("settings.json.before-*")))
        # Nothing stale and nothing ambiguous: the file is not rewritten and no backup is made.
        body = json.dumps({"model": "gemini-test"})
        settings.write_text(body)
        code, result = self.run_helper("repair")
        self.assertEqual((code, result["removed"]), (0, []))
        self.assertEqual(settings.read_text(), body)
        self.assertFalse(list(settings.parent.glob("settings.json.before-*")))


if __name__ == "__main__":
    unittest.main()
