#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Session start prints the shared loop only for adopted direction repositories."""

from __future__ import annotations

import datetime as dt
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().parent / "direction_check_hook.py"
sys.path.insert(0, str(HOOK.parent))

import direction_check_hook as hook  # noqa: E402

NOW = dt.datetime(2026, 9, 22, 12, tzinfo=dt.timezone.utc)
MARKER = Path("/x/direction-last-check.json")


def ago(**kwargs: float) -> dt.datetime:
    return NOW - dt.timedelta(**kwargs)


def marker(turn: dt.datetime | None = None, **audits: dt.datetime) -> dict[str, object]:
    return {"turn": turn, "audits": {repo.replace("__", "/"): stamp for repo, stamp in audits.items()}}


class AgentToolsReminderTests(unittest.TestCase):
    def test_tool_checker_exceptions_do_not_escape_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".local").mkdir()
            (root / ".local/chrome.toml").touch()
            for error in (RecursionError(), SyntaxError(), TypeError()):
                with mock.patch.object(hook.runpy, "run_path", side_effect=error):
                    self.assertTrue(hook.agent_tools_warning(root, root, {"CLAUDECODE": "1"}))

    def test_unrequested_desktop_tools_and_unverified_daemon_setup_are_quiet(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".local").mkdir()
            (root / ".local/chrome.toml").touch()
            config = root / ".claude.json"
            config.write_text(json.dumps({"mcpServers": {"catalog-chrome": {"command": "fixture"}}}))
            env = {"CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": str(root)}
            self.assertEqual(hook.agent_tools_warning(root, root, env), "")
            self.assertEqual(hook.agent_tools_warning(root, root, {"CODEX_HOME": str(root)}), "")

    def test_claude_startup_reports_tools_once_without_processes_or_setting_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".local").mkdir()
            (root / ".local/chrome.toml").touch()
            home = root / "account"
            home.mkdir()
            config = home / ".claude.json"
            config.write_text(json.dumps({"claudeInChromeDefaultEnabled": True}))
            env = {"CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": str(home)}
            before = config.read_bytes()
            with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no live probes")):
                warning = hook.agent_tools_warning(root, root, env)
            self.assertEqual(len(warning.splitlines()), 1)
            self.assertTrue(warning)
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(hook.agent_tools_warning(root, root, {"CODEX_HOME": str(home)}), "")
            (root / ".local/chrome.toml").unlink()
            self.assertEqual(hook.agent_tools_warning(root, root, env), "")

    def test_static_diagnostic_is_wired_into_session_start_only(self):
        for kwargs, emitted in (({}, True), ({"skills_only": True}, False), ({"turn": True}, False)):
            output = io.StringIO()
            with (mock.patch.object(hook, "agent_tools_warning", return_value="fixture-tool-warning") as check,
                  mock.patch.object(hook, "marker_path", return_value=Path("/missing/fixture-marker")),
                  mock.patch.object(hook, "direction_root", return_value=Path.cwd()),
                  mock.patch.object(hook, "origin_repo", return_value=None),
                  mock.patch.object(hook, "memory_reminder", return_value=""),
                  contextlib.redirect_stdout(output)):
                self.assertEqual(hook.main(**kwargs), 0)
            self.assertEqual("fixture-tool-warning" in output.getvalue(), emitted)
            self.assertEqual(check.called, emitted)


class MemoryReminderTests(unittest.TestCase):
    def test_session_start_reports_due_memory_on_both_harnesses_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = root / "memory"
            memory.mkdir()
            registry = memory / "MEMORY.md"
            registry.write_text("one two three four")
            state = root / "memory.json"
            state.write_text(json.dumps({"memory_root": str(memory), "max_words": 3}))
            direction = root / "direction.json"
            direction.write_text(json.dumps({"turn": dt.datetime.now(dt.timezone.utc).isoformat()}))
            base = {"HOME": str(root), "PATH": "/usr/bin:/bin", "MEMORY_MARKER": str(state),
                    "DIRECTION_MARKER": str(direction)}
            expected = hook.memory_reminder(NOW, base)
            self.assertTrue(expected)
            for harness in ({"CLAUDECODE": "1"}, {"CODEX_HOME": str(root / "profile")}):
                env = {**base, **harness}
                before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                result = subprocess.run([sys.executable, str(HOOK), "--catalog-root", str(root)],
                    cwd=root, env=env, text=True, capture_output=True, check=True)
                self.assertIn(expected, result.stdout)
                self.assertEqual({p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}, before)
            registry.write_text("one two three")
            result = subprocess.run([sys.executable, str(HOOK), "--catalog-root", str(root)],
                cwd=root, env=base, text=True, capture_output=True, check=True)
            registry.unlink()
            absent = subprocess.run([sys.executable, str(HOOK), "--catalog-root", str(root)],
                cwd=root, env=base, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, absent.stdout)

    def test_age_and_size_boundaries_and_supported_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = root / "memory"
            memory.mkdir()
            registry = memory / "MEMORY.md"
            registry.write_text("one two three")
            state = root / "memory-check.json"
            env = {"HOME": str(root), "MEMORY_MARKER": str(state)}
            marker = {"memory_root": str(memory), "last_distilled_at": NOW.isoformat(),
                      "max_age_days": 2, "max_words": 3}
            state.write_text(json.dumps(marker))
            self.assertEqual(hook.memory_reminder(NOW + dt.timedelta(days=2), env), "")
            self.assertTrue(hook.memory_reminder(NOW + dt.timedelta(days=2, seconds=1), env))
            registry.write_text("one two\nthree four")
            self.assertTrue(hook.memory_reminder(NOW, env))
            registry.write_text("one")
            state.write_text(json.dumps({**marker, "last_distilled_at": (NOW + dt.timedelta(days=1)).isoformat()}))
            self.assertEqual(hook.memory_reminder(NOW, env), "")

    def test_defaults_use_shared_memory_and_do_not_split_by_harness_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            memory = root / ".codex" / "memories"
            memory.mkdir(parents=True)
            (memory / "MEMORY.md").write_text("word " * (hook.MEMORY_MAX_WORDS + 1))
            for extra in ({"CODEX_HOME": str(root / "other-profile")}, {"CLAUDECODE": "1"}, {}):
                with self.subTest(extra=extra):
                    self.assertTrue(hook.memory_reminder(NOW, {"HOME": str(root), **extra}))
            state = root / ".code" / hook.MEMORY_MARKER_NAME
            state.parent.mkdir()
            (memory / "MEMORY.md").write_text("word " * hook.MEMORY_MAX_WORDS)
            state.write_text(json.dumps({"last_distilled_at": (NOW - dt.timedelta(days=hook.MEMORY_STALE_DAYS)).isoformat()}))
            self.assertEqual(hook.memory_reminder(NOW, {"HOME": str(root)}), "")
            self.assertTrue(hook.memory_reminder(NOW + dt.timedelta(seconds=1), {"HOME": str(root)}))

    def test_missing_or_malformed_optional_state_never_breaks_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "memory.json"
            env = {"HOME": str(root), "MEMORY_MARKER": str(state)}
            self.assertEqual(hook.memory_reminder(NOW, env), "")
            for value in ("invalid JSON", "[]", '{"max_words": true}', '{"max_age_days": -1}'):
                state.write_text(value)
                self.assertEqual(hook.memory_reminder(NOW, env), "")
            state.write_text(json.dumps({"memory_root": str(root) + "\0"}))
            self.assertEqual(hook.memory_reminder(NOW, env), "")


class IsolatedDirectionHookTests(unittest.TestCase):
    def setUp(self) -> None:
        # Direction-only scenarios must not inspect the host's installed memory.
        patcher = mock.patch.object(hook, "memory_reminder", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)


class ReminderTests(IsolatedDirectionHookTests):
    def test_startup_keeps_commands_and_reference_without_the_procedure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "loop.md"
            for fence in ("text", "console"):
                with self.subTest(fence=fence):
                    path.write_text(f"# Fixture loop\n\n```{fence}\ngo    start work\nland  land work\n```\n\nDetailed fixture procedure.\n")
                    output = io.StringIO()
                    with mock.patch.dict(os.environ, {"CLAUDECODE": "0"}), mock.patch.object(hook, "LOOP_PATH", path), mock.patch.object(hook, "direction_root", return_value=Path.cwd()), mock.patch.object(hook, "read_marker", return_value=marker(turn=dt.datetime.now(dt.timezone.utc))), mock.patch.object(hook, "origin_repo", return_value=None), contextlib.redirect_stdout(output):
                        self.assertEqual(hook.main(), 0)
                    text = output.getvalue()
                    if fence == "text":
                        self.assertIn("```text\ngo    start work\nland  land work\n```", text)
                    self.assertIn(str(path), text)
                    self.assertNotIn("Detailed fixture procedure", text)

    def test_compaction_prints_only_the_claude_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {"HOME": directory, "DIRECTION_MARKER": str(Path(directory) / "missing.json"), "CLAUDECODE": "1"}
            result = subprocess.run([sys.executable, str(HOOK), "--skills-only"], cwd=directory, env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), hook.SKILLS_PROTOCOL_PATH.read_text().strip())
            env.pop("CLAUDECODE")
            result = subprocess.run([sys.executable, str(HOOK), "--skills-only"], cwd=directory, env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, "")

    def test_each_claude_prompt_gets_one_line_pointing_to_the_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {"HOME": directory, "DIRECTION_MARKER": str(Path(directory) / "missing.json"), "CLAUDECODE": "1"}
            result = subprocess.run([sys.executable, str(HOOK), "--turn"], cwd=directory, env=env, text=True, capture_output=True, check=True)
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertIn(str(hook.SKILLS_PROTOCOL_PATH), result.stdout)
            env.pop("CLAUDECODE")
            result = subprocess.run([sys.executable, str(HOOK), "--turn"], cwd=directory, env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, "")

    def test_skills_protocol_is_claude_only_and_independent_of_direction_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker_path = root / hook.MARKER_NAME
            marker_path.write_text(json.dumps({"turn": dt.datetime.now(dt.timezone.utc).isoformat()}))
            env = {"HOME": str(root), "DIRECTION_MARKER": str(marker_path), "PATH": "/usr/bin:/bin"}
            for host_env, expected in (
                ({"CLAUDECODE": "1"}, hook.SKILLS_PROTOCOL_PATH.read_text().strip()),
                ({"CODEX_HOME": str(root)}, ""),
                ({"CLAUDECODE": "0"}, ""),
            ):
                with self.subTest(host_env=host_env):
                    result = subprocess.run([sys.executable, str(HOOK), "--catalog-root", str(root)], cwd=root, env={**env, **host_env}, text=True, capture_output=True, check=True)
                    self.assertEqual(result.stdout.strip(), expected)

    def test_missing_protocol_does_not_hide_the_loop_or_reminder(self) -> None:
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), mock.patch.object(hook, "SKILLS_PROTOCOL_PATH", Path("/missing/protocol.md")), mock.patch.object(hook, "direction_root", return_value=Path.cwd()), mock.patch.object(hook, "read_marker", return_value={}), mock.patch.object(hook, "resolve_account", return_value=None), contextlib.redirect_stdout(output):
            self.assertEqual(hook.main(), 0)
        self.assertIn(str(hook.LOOP_PATH), output.getvalue())
        self.assertIn(hook.REMINDER_LINES["turn", "due"], output.getvalue())

    def test_current_checks_print_nothing(self) -> None:
        m = {**marker(ago(hours=3)), "turn_repo": "owner/direction", "weekly": ago(days=6), "weekly_record": "owner/direction"}
        self.assertEqual(hook.reminder(m, NOW, MARKER, "owner"), "")
        self.assertEqual(hook.turn_state(m, NOW, "owner"), "current")

    def test_repository_audits_do_not_add_reminders(self) -> None:
        repos = {f"owner__r{number}": ago(days=9) for number in range(1, 6)}
        text = hook.reminder(marker(ago(days=3), **repos), NOW, MARKER, "owner")
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn(hook.REMINDER_LINES["turn", "due"], text)
        for number in range(1, 6):
            self.assertNotIn(f"owner/r{number}", text)

    def test_weekly_audit_does_not_replace_the_daily_turn(self) -> None:
        fresh_turn = {**marker(ago(hours=2)), "turn_repo": "owner/direction", "weekly": ago(days=8), "weekly_record": "owner/direction"}
        fresh_audit = {**marker(ago(days=2)), "turn_repo": "owner/direction", "weekly": ago(days=2), "weekly_record": "owner/direction"}
        self.assertEqual(hook.reminder(fresh_turn, NOW, MARKER, "owner"), "")
        self.assertEqual(len(hook.reminder(fresh_audit, NOW, MARKER, "owner").splitlines()), 1)
        self.assertIn(hook.REMINDER_LINES["turn", "due"], hook.reminder(fresh_audit, NOW, MARKER, "owner"))


    def test_no_account_shows_only_the_turn_line(self) -> None:
        self.assertEqual(hook.reminder(marker(ago(hours=2)), NOW, MARKER), "")
        self.assertEqual(hook.turn_state(marker(ago(days=2)), NOW, None), "due")
        self.assertEqual(len(hook.reminder(marker(ago(days=2)), NOW, MARKER).splitlines()), 1)

    def test_missing_marker_asks_for_a_turn(self) -> None:
        self.assertEqual(hook.turn_state({"turn": None, "audits": {}}, NOW, None), "due")

    def test_legacy_turn_has_unknown_coverage_and_is_still_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "marker.json"
            path.write_text(json.dumps({"turn": ago(hours=1).isoformat(), "turn_repo": ["invalid"]}))
            read = hook.read_marker(path)
            self.assertNotIn("turn_repo", read)
            self.assertIsNone(hook.marker_account(read))
            self.assertEqual(hook.reminder(read, NOW, path), "")

    def test_marker_path_is_shared_across_hosts(self) -> None:
        # Codex sets CODEX_HOME for its hooks; Claude Code sets neither. Both must read one file.
        self.assertEqual(hook.marker_path({"CODEX_HOME": "/h/.codex", "HOME": "/h"}), Path("/h/.code/direction-last-check.json"))
        self.assertEqual(hook.marker_path({"CODE_HOME": "/x", "HOME": "/h"}), Path("/h/.code/direction-last-check.json"))
        self.assertEqual(hook.marker_path({"HOME": "/h"}), Path("/h/.code/direction-last-check.json"))
        self.assertEqual(hook.marker_path({"DIRECTION_MARKER": "/shared/m.json", "CODEX_HOME": "/y", "HOME": "/h"}), Path("/shared/m.json"))

    def test_malformed_and_naive_stamps_do_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            self.assertEqual(hook.read_marker(path), {"turn": None, "audits": {}})
            path.write_text("not json")
            self.assertEqual(hook.read_marker(path), {"turn": None, "audits": {}})
            path.write_text(json.dumps({"turn": "yesterday", "audits": {"o/r": "2026-09-20T10:00:00", "bad": 5}}))
            read = hook.read_marker(path)
            self.assertIsNone(read["turn"])
            audits = read["audits"]
            assert isinstance(audits, dict)
            self.assertEqual(list(audits), ["o/r"])
            self.assertEqual(hook.turn_state(read, NOW, None), "due",
                             "the audit must not replace an unparseable turn stamp")
            path.write_text(json.dumps({"turn": "2026-09-22T10:00:00", "turn_repo": "o/direction", "weekly_audit": {"ran": "2026-09-20T10:00:00", "record": "o/direction"}}))
            self.assertEqual(hook.turn_state(hook.read_marker(path), NOW, "o"), "current",
                             "naive stamps are read as UTC, not a crash")

    def test_adopted_repo_needs_a_direction_file_and_an_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            nested = root / "a"
            nested.mkdir()
            self.assertIsNone(hook.direction_root(nested))
            (root / "DIRECTION.md").write_text("# Direction\n")
            self.assertEqual(hook.direction_root(nested), root.resolve())
            self.assertIsNone(hook.origin_repo(root.resolve()), "no origin, no repo key")
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "git@github.com:owner/repo.git"], check=True)
            self.assertEqual(hook.origin_repo(root.resolve()), "owner/repo")

    def test_loop_prints_from_reference_only_in_direction_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            nested = root / "nested"
            nested.mkdir()
            marker_path = root / hook.MARKER_NAME
            marker_path.write_text(json.dumps({"turn": dt.datetime.now(dt.timezone.utc).isoformat(), "audits": {}}))
            env = {"HOME": str(root), "DIRECTION_MARKER": str(marker_path), "PATH": "/usr/bin:/bin"}
            def run(cwd: Path) -> subprocess.CompletedProcess[str]:
                return subprocess.run([sys.executable, str(HOOK), "--catalog-root", str(root)], cwd=cwd, env=env, capture_output=True, text=True, check=True)

            self.assertEqual(run(nested).stdout, "")
            (root / "DIRECTION.md").write_text("# Direction\n")
            expected = hook.loop_summary(hook.LOOP_PATH.read_text())
            self.assertEqual(run(nested).stdout.strip(), expected)
            self.assertEqual(run(root).stdout.strip(), expected)

    def test_missing_loop_reference_keeps_overdue_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "DIRECTION.md").write_text("# Direction\n")
            output = io.StringIO()
            with mock.patch.object(hook, "LOOP_PATH", root / "missing.md"), mock.patch.object(hook, "marker_path", return_value=root / "missing-marker.json"), mock.patch("pathlib.Path.cwd", return_value=root), contextlib.redirect_stdout(output):
                self.assertEqual(hook.main(), 0)
            self.assertIn(hook.REMINDER_LINES["turn", "due"], output.getvalue())

    def test_hook_process_never_blocks_and_ignores_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {"HOME": tmp, "DIRECTION_MARKER": str(Path(tmp) / hook.MARKER_NAME), "PATH": "/usr/bin:/bin"}
            proc = subprocess.run([sys.executable, str(HOOK), "--catalog-root", str(root)], input='{"hook_event_name":"SessionStart"}', capture_output=True, text=True, env=env, cwd=tmp)
            self.assertEqual(proc.returncode, 0)
            self.assertIn(hook.REMINDER_LINES["turn", "due"], proc.stdout)
            (Path(tmp) / hook.MARKER_NAME).write_text(json.dumps({"turn": dt.datetime.now(dt.timezone.utc).isoformat(), "audits": {}}))
            proc = subprocess.run([sys.executable, str(HOOK), "--catalog-root", str(root)], stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, cwd=tmp)
            self.assertEqual((proc.returncode, proc.stdout), (0, ""))


class OverallDirectionTests(IsolatedDirectionHookTests):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        self.repo = self.tmp / "product"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", "git@github.com:owner/product.git"], check=True)
        self.marker = self.tmp / hook.MARKER_NAME
        self.audit("owner/direction")

    def audit(self, *repos: str) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        self.marker.write_text(json.dumps({"turn": now, "turn_repo": "owner/direction", "audits": {repo: now for repo in repos},
                                           "weekly_audit": {"ran": now, "record": "owner/direction"}}))

    def run_hook(self) -> str:
        output = io.StringIO()
        with (mock.patch.dict(os.environ, {"CLAUDECODE": "0"}),
              mock.patch.object(hook, "run_reader") as reader,
              mock.patch.object(hook, "marker_path", return_value=self.marker),
              mock.patch("pathlib.Path.cwd", return_value=self.repo), contextlib.redirect_stdout(output)):
            self.assertEqual(hook.main(), 0)
        reader.assert_not_called()
        return output.getvalue().strip()

    def test_current_direction_reminder_does_not_deliver_direction_or_read_it(self) -> None:
        text = self.run_hook()
        self.assertEqual(text, "")

    def test_own_direction_file_keeps_todays_output_without_a_read(self) -> None:
        (self.repo / "DIRECTION.md").write_text("# Direction\n")
        self.audit("owner/direction", "owner/product")
        self.assertEqual(self.run_hook(), hook.loop_summary(hook.LOOP_PATH.read_text()))

    def test_owner_without_an_audited_direction_repository_sees_nothing_and_costs_no_read(self) -> None:
        self.audit("owner/product", "other/direction")
        self.assertEqual(self.run_hook(), "")

    def sibling_checkout(self) -> Path:
        local = self.tmp / "direction"
        subprocess.run(["git", "init", "-q", str(local)], check=True)
        subprocess.run(["git", "-C", str(local), "remote", "add", "origin", "https://github.com/owner/direction.git"], check=True)
        return local

    def test_a_sibling_checkout_counts_as_set_up_without_an_audit(self) -> None:
        self.sibling_checkout()
        self.audit()
        marker_data = hook.read_marker(self.marker)
        self.assertTrue(hook.direction_set_up("owner", self.repo, marker_data)[0])
        self.assertEqual(self.run_hook(), "")


class SharedRecordTests(IsolatedDirectionHookTests):
    """The hook reads the shared records only when a local stamp is stale, and never by folder."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        self.calls = self.tmp / "calls"

    def reader(self, issues: object = None, error: str | None = None) -> Path:
        script = self.tmp / "reader"
        (self.tmp / "issues.json").write_text(json.dumps(issues))
        action = f"echo '{error}' >&2; exit 1" if error else f'cat "{self.tmp / "issues.json"}"'
        script.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\n{action}\n')
        script.chmod(0o755)
        return script

    def seen(self, local: dict[str, object], reader: Path, owner: str | None = "owner") -> dict[str, object]:
        with mock.patch.object(hook, "GH_READER", reader):
            return hook.with_shared_records(local, NOW, owner)

    @staticmethod
    def record(stamp: str, **issue: object) -> dict[str, object]:
        body = f"{hook.TURN_RECORD_TAG}\nLast daily turn: {stamp} covering owner/direction\n"
        return {"number": 1, "body": body, "author_association": "OWNER", "user": {"type": "User"}, **issue}


    def fresh(self, turn: dt.datetime | None = None) -> dict[str, object]:
        return {**marker(turn or ago(hours=2)), "turn_repo": "owner/direction", "weekly": ago(days=1), "weekly_record": "owner/direction"}

    def test_weekly_stamp_does_not_prompt_or_trigger_a_shared_read(self) -> None:
        for weekly in ({}, {"weekly": ago(days=8), "weekly_record": "owner/direction"}, {"weekly": "malformed"}):
            with self.subTest(weekly=weekly):
                local = {**marker(ago(hours=2)), "turn_repo": "owner/direction", **weekly}
                with mock.patch.object(hook, "GH_READER", self.reader([])):
                    self.assertEqual(hook.session_reminder(local, NOW, None, MARKER), "")
                self.assertFalse(self.calls.exists())

    def test_current_checks_cost_no_read(self) -> None:
        local = self.fresh()
        self.assertIs(self.seen(local, self.reader([])), local)
        self.assertFalse(self.calls.exists())

    def test_the_records_live_with_this_machines_account(self) -> None:
        local = {**marker(ago(days=2)), "turn_repo": "director/start"}
        self.assertEqual(hook.marker_account(local), "director")
        self.seen(local, self.reader([]), hook.marker_account(local))
        self.assertIn("repos/director/direction/issues", self.calls.read_text())

    def test_no_record_keeps_the_turn_due(self) -> None:
        seen = self.seen(marker(ago(days=2)), self.reader([]))
        self.assertEqual(hook.turn_state(seen, NOW, "owner"), "due")

    def test_one_read_takes_the_newer_turn(self) -> None:
        records = [self.record("2026-09-22T11:00:00Z")]
        seen = self.seen(marker(ago(days=2)), self.reader(records))
        self.assertEqual(hook.turn_state(seen, NOW, "owner"), "current")
        self.assertEqual(hook.reminder(seen, NOW, MARKER, "owner"), "")
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)

    def test_an_untrusted_or_older_record_does_not_clear_it(self) -> None:
        forged = self.record("2026-09-22T11:00:00Z", author_association="NONE")
        older = self.record("2026-09-19T11:00:00Z", number=3)
        seen = self.seen({**marker(ago(days=2)), "turn_repo": "owner/direction"}, self.reader([forged, older]))
        self.assertEqual(seen["turn"], ago(days=2))
        self.assertEqual(hook.turn_state(seen, NOW, "owner"), "due")

    def test_a_stalled_credential_helper_cannot_hold_session_start(self) -> None:
        reader = self.tmp / "stalled"
        reader.write_text("#!/bin/sh\nsleep 30 &\nsleep 30\n")
        reader.chmod(0o755)
        started = time.monotonic()
        with mock.patch.object(hook, "OVERALL_READ_TIMEOUT", 0.5):
            seen = self.seen(marker(ago(days=2)), reader)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(seen["shared_error"], "GitHub did not answer in time")

    def test_unreadable_records_are_unconfirmed_not_overdue(self) -> None:
        seen = self.seen(marker(None, owner__repo=ago(days=9)), self.reader(error="gh: Not Found (HTTP 404)"))
        self.assertEqual(hook.turn_state(seen, NOW, "owner"), "unconfirmed")
        self.assertEqual(len(hook.reminder(seen, NOW, MARKER, "owner").splitlines()), 1)

    def test_another_accounts_turn_does_not_count_for_the_selected_account(self) -> None:
        local = {**marker(ago(hours=2)), "turn_repo": "first/direction", "weekly": ago(hours=1), "weekly_record": "second/direction"}
        owner = hook.resolve_account(local, None)
        self.assertEqual(owner, "second")
        seen = self.seen(local, self.reader([]), owner)
        self.assertIn("repos/second/direction/issues", self.calls.read_text(), "first's turn must not skip second's shared read")
        self.assertEqual(hook.turn_state(seen, NOW, owner), "due")
        text = hook.reminder(seen, NOW, MARKER, owner)
        self.assertIn(hook.REMINDER_LINES["turn", "due"], text)
        unreadable = self.seen(local, self.reader(error="gh: HTTP 502"), owner)
        self.assertEqual(hook.turn_state(unreadable, NOW, owner), "unconfirmed")
        self.assertIn(hook.REMINDER_LINES["turn", "unconfirmed"], hook.reminder(unreadable, NOW, MARKER, owner))

    def test_a_newer_turn_of_another_account_does_not_override_the_shared_turn(self) -> None:
        local = {**marker(ago(hours=1)), "turn_repo": "first/direction", "weekly": ago(days=1), "weekly_record": "second/direction"}
        shared = self.record("2026-09-21T20:00:00Z", body=f"{hook.TURN_RECORD_TAG}\nLast daily turn: 2026-09-21T20:00:00Z covering second/direction\n")
        seen = self.seen(local, self.reader([shared]), "second")
        self.assertEqual(seen["turn"], dt.datetime(2026, 9, 21, 20, tzinfo=dt.timezone.utc), "the shared turn of second wins")
        self.assertEqual(seen["turn_repo"], "second/direction")
        self.assertEqual(hook.turn_state(seen, NOW, "second"), "current")


class AccountResolutionTests(IsolatedDirectionHookTests):
    """The folder decides nothing once the marker names the account."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        self.reads: list[tuple[str, ...]] = []

    def checkout(self, name: str, origin: str, *, direction: bool = False) -> Path:
        root = self.tmp / name
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", f"git@github.com:{origin}.git"], check=True)
        if direction:
            (root / "DIRECTION.md").write_text("# Direction\n")
        return root.resolve()

    def session(self, local: dict[str, object], checkout: Path | None) -> str:
        def read(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            self.reads.append(args)
            return subprocess.CompletedProcess(list(args), 0, "[]", "")

        with mock.patch.object(hook, "run_reader", read):
            return hook.session_reminder(local, NOW, checkout, MARKER)

    def test_reminder_is_identical_in_any_folder_for_the_same_marker(self) -> None:
        local = {**marker(ago(days=2), owner__adopted=ago(days=9)), "turn_repo": "owner/direction"}
        folders = (self.checkout("adopted", "owner/adopted", direction=True), self.checkout("product", "owner/product"),
                   self.checkout("elsewhere", "someone/else"), None)
        texts = {self.session(local, folder) for folder in folders}
        self.assertEqual(len(texts), 1)
        text = texts.pop()
        self.assertEqual(len(text.splitlines()), 1)
        self.assertNotIn("owner/adopted", text)
        self.assertEqual({args[1].split("?")[0] for args in self.reads}, {"repos/owner/direction/issues"},
                         "only the account's own shared records are read")

    def test_the_newest_recorded_stamp_names_the_account(self) -> None:
        older_turn = {**marker(ago(days=5)), "turn_repo": "first/direction", "weekly": ago(days=1), "weekly_record": "second/direction"}
        self.assertEqual(hook.resolve_account(older_turn, None), "second")
        newer_turn = {**older_turn, "turn": ago(hours=1)}
        self.assertEqual(hook.resolve_account(newer_turn, None), "first")

    def test_an_empty_marker_outside_any_repository_has_no_account(self) -> None:
        empty = {"turn": None, "audits": {}}
        self.assertIsNone(hook.resolve_account(empty, None))
        text = self.session(empty, None)
        self.assertEqual(hook.turn_state(empty, NOW, None), "due")
        self.assertEqual(len(text.splitlines()), 1)
        self.assertEqual(self.reads, [])

    def test_an_empty_marker_in_a_fork_shows_no_audit_line(self) -> None:
        fork = self.checkout("fork", "contributor/catalog")
        empty = {"turn": None, "audits": {}}
        self.assertIsNone(hook.resolve_account(empty, fork))
        self.assertEqual(len(self.session(empty, fork).splitlines()), 1)
        self.assertEqual(self.reads, [], "a fork's owner without OWNER/direction costs no read")

    def test_an_empty_marker_uses_the_folder_owner_only_with_direction_set_up(self) -> None:
        empty = {"turn": None, "audits": {}}
        product = self.checkout("product", "owner/product")
        self.assertIsNone(hook.resolve_account(empty, product))
        sibling = self.checkout("direction", "owner/direction")
        self.assertEqual(hook.resolve_account(empty, sibling), "owner")
        self.assertEqual(hook.resolve_account({**empty, "audits": {"owner/direction": NOW}}, product), "owner")
        with mock.patch.object(hook, "local_overall_checkout", return_value=sibling):
            self.assertEqual(hook.resolve_account(empty, product), "owner")
            self.assertEqual(len(self.session(empty, product).splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
