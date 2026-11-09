#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["websockets==17.4"]
# ///
"""Provider/harness consistency, without real accounts or terminals."""

import argparse
import asyncio
import copy
import errno
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import account_choice
import agent_tools
import iterm_tab
import codex_session_proxy
from codex_session_proxy import MEMORY_SETTING
import tmux_terminal
import session_liveness
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "github/scripts"))


HARNESS_CHOICES = (
    ("openai", "codex", {"CODEX_HOME": "/fixture/codex"}),
    ("anthropic", "claude", {"CLAUDE_CONFIG_DIR": "/fixture/claude"}),
    ("google", "agy", {}),
)


def patch_attr(target: object, name: str, **kwargs: Any) -> Any:
    return patch.object(target, name, **kwargs)


class LaunchIssueTests(unittest.TestCase):
    def test_seat_addressed_briefs_use_the_existing_supervisor_launch_path(self) -> None:
        from github_plan_status.next_actor import SUPERVISOR_ROLES

        issue = "https://github.com/fixture/repo/issues/7"
        header = f"Anchor and claim: {issue}\n"
        task = "Launch this bounded work.\n"
        for role in SUPERVISOR_ROLES:
            status = f"Next actor: agent:supervisor-{role}\nBrief:\n```text\n{task}```"
            payload = {"ok": True, "issue": {"recorded_current_status": status, "provenance": {"current_status_managed": True}}}
            with self.subTest(role=role), patch.object(iterm_tab.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, json.dumps(payload))):
                self.assertEqual(iterm_tab.read_issue_brief(issue, header), header.rstrip("\n") + "\n\n" + task)
                with self.assertRaisesRegex(ValueError, "Next actor"):
                    iterm_tab.read_issue_brief(issue, header, "some-lead")

    def test_reporter_status_dispatch_uses_current_bot_source_and_preserves_holds(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "dispatch_plan", Path(__file__).resolve().parents[2] / "github/scripts/gh-plan.py")
        assert spec and spec.loader
        plan: Any = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plan)
        issue_url = "https://github.com/fixture/repo/issues/7"
        lead = "fixture-lead"
        task = "### Goal\nKeep  spacing and `quotes`.\n<!-- Preserve authored context. -->\n"
        status = f"Next actor: agent:{lead}\nBrief:\n```text\n{task}```"
        issue: dict[str, Any] = {"repo": "fixture/repo", "number": 7, "title": "Human request", "state": "open", "labels": [],
                 "user": {"login": "reporter"}, "body": "Keep the human request unchanged.\n",
                 "updated_at": "2026-01-01T00:00:00Z"}
        managed: dict[str, Any] = {"id": 8, "user": {"login": "fixture-bot[bot]"}, "body": "## Current Status\n" + status,
                   "created_at": "2026-01-02T00:00:00Z"}
        clear = plan.github_plan_wait.reviewed(evidence="Fictional request reviewed; no holds")
        ownership = {"complete": True, "records": [], "pulls": [],
                     "inventory": {"local_branches": [], "remote_branches": [], "worktrees": [], "sessions": []}}
        with tempfile.TemporaryDirectory() as folder:
            command, brief = Path(folder) / "command", Path(folder) / "header"
            command.write_text("agy")
            brief.write_text(f"Brief from the Supervisor. Trial token: FIXTURE.\nAnchor and claim: {issue_url}\n"
                             f"Rules: {iterm_tab.BRIEF_RULES_URL}\n")
            args = argparse.Namespace(command_file=[command], brief_file=[brief], brief_issue=[issue_url], lead_session_id=lead)
            for case in ("current", "untrusted", "human-owner", "stale", "recipient", "missing-brief", "wait", "blocker", "closed"):
                current, comment, wait = copy.deepcopy(issue), copy.deepcopy(managed), clear
                blockers = []
                comments = [comment]
                if case == "untrusted":
                    comments.append({**comment, "id": 10, "user": {"login": "outsider"},
                                     "created_at": "2026-01-04T00:00:00Z",
                                     "body": comment["body"].replace(lead, "another-lead")})
                elif case == "human-owner":
                    comment["user"]["login"] = "fixture"
                elif case == "stale":
                    current["body"] += "## Current Status\nNext actor: agent\n"
                    comment["created_at"] = "2025-12-31T00:00:00Z"
                elif case == "recipient":
                    comment["body"] = comment["body"].replace(lead, "another-lead")
                elif case == "missing-brief":
                    comment["body"] = "## Current Status\nNext actor: agent:" + lead
                elif case == "wait":
                    wait = plan.github_plan_wait.reviewed(evidence="Fictional device test pending",
                        waiting_for="Device test", ends_when="Tester reports the result")
                elif case == "blocker":
                    blockers = [{"repo": "fixture/repo", "number": 6, "state": "open"}]
                elif case == "closed":
                    current["state"] = "closed"
                wait_comment = {"id": 9, "user": {"login": "fixture-bot[bot]"}, "body": plan.github_plan_wait.marker(wait),
                                "created_at": "2026-01-03T00:00:00Z"}
                def read(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
                    with patch_attr(plan, "emit") as emitted:
                        plan.cmd_show(plan.build_parser().parse_args(argv[3:]))
                    return subprocess.CompletedProcess(argv, 0, json.dumps(emitted.call_args.args[0]))
                with (self.subTest(case=case),
                      patch.multiple(plan, get_issue=Mock(return_value=("automation-gh", current)),
                          EXPECTED_ACTOR="fixture-bot[bot]", load_config=Mock(return_value={}),
                          collect_paged_rest_items=Mock(return_value=("automation-gh", [*comments, wait_comment])),
                          read_planning_graph=Mock(return_value=None), next_graph_reader=Mock(return_value=None),
                          read_issue_relationships=Mock(return_value=("automation-gh",
                              {"blocked_by": blockers, "blocking": [], "sub_issues": []})),
                          next_status_body_time=Mock(return_value=current["updated_at"]),
                          read_queue_ownership=Mock(return_value=ownership),
                          wait_authors=Mock(return_value={"fixture", "fixture-bot[bot]"}),
                          api_json=Mock(side_effect=AssertionError("Unexpected network or write")),
                          rest_edit_issue=Mock(side_effect=AssertionError("Human request edit"))),
                      patch_attr(plan.github_identity, "configured_bot_logins", return_value=["fixture-bot[bot]"]),
                      patch.object(iterm_tab.subprocess, "run", side_effect=read)):
                    if case in {"current", "untrusted"}:
                        commands, _ = iterm_tab.prepare_launches(args)
                        prompt = next(word.removeprefix("--prompt-interactive=") for word in shlex.split(commands[0])
                                      if word.startswith("--prompt-interactive="))
                        self.assertTrue(prompt.endswith("\n\n" + task))
                    else:
                        with self.assertRaises(ValueError) as refused:
                            iterm_tab.prepare_launches(args)
                        self.assertIsNone(refused.exception.__cause__)

    def test_child_issue_brief_reaches_worker_verbatim_under_supervisor_header(self) -> None:
        issue = "https://github.com/fixture/repo/issues/7"
        task = ("Keep  two spaces.\n\n### Goal\nUse `quotes`, 'apostrophes' and $literal.\n> Preserve this.\n"
                "Read-only anchor: https://github.com/fixture/direction/issues/9\nAdmission: task context only\n"
                f"Rules: {iterm_tab.BRIEF_RULES_URL}\n```sh\necho café\n```\n\n")
        header = f"Brief from the Supervisor. Trial token: CHILD.\nAnchor and claim: {issue}\nAdmission: milestone\nRules: {iterm_tab.BRIEF_RULES_URL}\n"
        status = f"Next actor: agent:supervisor\nNext action: Launch this brief.\n\nBrief:\n````text\n{task}`````"
        with tempfile.TemporaryDirectory() as folder:
            command, brief = Path(folder) / "command", Path(folder) / "header"
            command.write_text("agy")
            brief.write_text(header)
            args = argparse.Namespace(command_file=[command], brief_file=[brief], brief_issue=[issue])
            for newline in ("\n", "\r\n"):
                payload: dict[str, Any] = {"ok": True, "issue": {"recorded_current_status": status.replace("\n", newline),
                           "provenance": {"current_status_managed": True},
                           "body": "## Current Status\n" + status.replace(task, "Wrong original-request brief.\n"),
                           "labels": [], "launch_check": {"allowed": True}}}
                with self.subTest(newline=newline), patch.object(iterm_tab.subprocess, "run", return_value=
                        subprocess.CompletedProcess([], 0, json.dumps(payload))):
                    commands, _ = iterm_tab.prepare_launches(args)
                    words = shlex.split(commands[0])
                    prompt = next(word.removeprefix("--prompt-interactive=") for word in words if word.startswith("--prompt-interactive="))
                    self.assertEqual(prompt, header.rstrip("\n") + "\n\n" + task)
            args.lead_session_id = "milestone-lead"
            payload["issue"]["recorded_current_status"] = status.replace("agent:supervisor", "agent:" + args.lead_session_id)
            with patch.object(iterm_tab.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload))):
                words = shlex.split(iterm_tab.prepare_launches(args)[0][0])
                prompt = next(word.removeprefix("--prompt-interactive=") for word in words if word.startswith("--prompt-interactive="))
                self.assertEqual(prompt, header.rstrip("\n") + "\n\n" + task)
            payload["issue"]["recorded_current_status"] = status
            with patch.object(iterm_tab.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload))):
                with self.assertRaisesRegex(ValueError, "Next actor"):
                    iterm_tab.prepare_launches(args)
            del args.lead_session_id
            payload["issue"]["provenance"]["current_status_managed"] = False
            with patch.object(iterm_tab.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload))):
                with self.assertRaisesRegex(ValueError, "managed Current Status"):
                    iterm_tab.prepare_launches(args)
            payload["issue"]["provenance"]["current_status_managed"] = True
            brief.write_text(header.replace(iterm_tab.BRIEF_RULES_URL, "missing"))
            with patch.object(iterm_tab.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload))):
                with self.assertRaisesRegex(ValueError, "brief must link"):
                    iterm_tab.prepare_launches(args)

    def test_stopped_launch_is_refused_on_both_backends_before_any_account_or_terminal_effect(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            command, brief = root / "command", root / "brief"
            command.write_text("codex")
            brief.write_text("Admission: milestone\nAnchor and claim: https://github.com/fixture/repo/issues/7\nRules: " + iterm_tab.BRIEF_RULES_URL)
            args = argparse.Namespace(command="new", window_id="chosen", command_file=[command], brief_file=[brief],
                                      account_provider="openai", account_config=None, account=None, tmux_socket="fixture")
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            refusal = {"ok": True, "issue": {"launch_check": {
                "allowed": False, "status": "active", "ownership": {"conflicts": [], "artifacts": []},
                "error": {"code": "claim_admission_unlinked", "message": "Track evidence missing"}}}}
            with (patch.dict(sys.modules, {"iterm2": SimpleNamespace()}),
                  patch.object(iterm_tab.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(refusal))),
                  patch.object(account_choice, "select", side_effect=AssertionError("Stopped work reached account selection")) as select,
                  patch.object(iterm_tab, "preflight_codex_daemon") as daemon,
                  patch.object(account_choice, "prepare_launch") as prepare,
                  patch.object(account_choice, "record_launch") as receipt,
                  patch.object(tmux_terminal, "Terminal") as terminal):
                for backend in ("iterm", "tmux", "tmux"):
                    with self.subTest(backend=backend), self.assertRaisesRegex(ValueError, "claim_admission_unlinked"):
                        asyncio.run(iterm_tab.operate(app, args) if backend == "iterm" else tmux_terminal.operate(args))
                select.assert_not_called()
                daemon.assert_not_called()
                prepare.assert_not_called()
                receipt.assert_not_called()
                terminal.return_value.run.assert_not_called()
                window.async_create_tab.assert_not_called()

    def test_family_mismatch_refuses_both_brief_routes_before_native_creation(self) -> None:
        issue = "https://github.com/fixture/repo/issues/7"
        with tempfile.TemporaryDirectory() as folder:
            command, brief = Path(folder) / "command", Path(folder) / "brief"
            brief.write_text(f"Anchor and claim: {issue}\nRules: {iterm_tab.BRIEF_RULES_URL}")
            args = argparse.Namespace(command="new", window_id="chosen", command_file=[command], brief_file=[brief],
                                      account_config=None, account=None, tmux_socket="fixture")
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            payload: dict[str, Any] = {"ok": True, "issue": {"recorded_current_status": "Next actor: agent:supervisor\nBrief:\n```text\nDo the task.\n```",
                       "provenance": {"current_status_managed": True}, "labels": [], "launch_check": {"allowed": True}}}
            with (patch.dict(sys.modules, {"iterm2": SimpleNamespace()}),
                  patch.object(iterm_tab.subprocess, "run", side_effect=lambda *_a, **_k:
                               subprocess.CompletedProcess([], 0, json.dumps(payload))),
                  patch.object(account_choice, "select", side_effect=AssertionError("Mismatch reached account selection")) as select,
                  patch.object(iterm_tab, "preflight_codex_daemon") as daemon,
                  patch.object(account_choice, "prepare_launch") as prepare,
                  patch.object(account_choice, "record_launch") as receipt,
                  patch.object(tmux_terminal, "Terminal") as terminal):
                for provider, agent, _ in HARNESS_CHOICES[:2]:
                    assigned = "claude" if agent == "codex" else "codex"
                    command.write_text(f"cd /fixture/repo && exec {agent}")
                    args.account_provider = provider
                    payload["issue"]["labels"] = [f"agent:{assigned}"]
                    for route in ("file", "issue"):
                        args.brief_issue = [issue] if route == "issue" else []
                        for backend in ("iterm", "tmux"):
                            with self.subTest(agent=agent, route=route, backend=backend):
                                with self.assertRaisesRegex(ValueError, f"agent:{assigned}"):
                                    asyncio.run(iterm_tab.operate(app, args) if backend == "iterm" else tmux_terminal.operate(args))
                select.assert_not_called()
                daemon.assert_not_called()
                prepare.assert_not_called()
                receipt.assert_not_called()
                terminal.return_value.run.assert_not_called()
                window.async_create_tab.assert_not_called()

    def test_family_matches_unlabeled_and_explicit_override_preserve_brief_delivery(self) -> None:
        issue = "https://github.com/fixture/repo/issues/7"
        reason = "Director authorized this session in the supplied brief"
        with tempfile.TemporaryDirectory() as folder:
            command, brief = Path(folder) / "command", Path(folder) / "brief"
            brief.write_text(f"Anchor and claim: {issue}\nRules: {iterm_tab.BRIEF_RULES_URL}")
            args = argparse.Namespace(command_file=[command], brief_file=[brief])
            payload: dict[str, Any] = {"ok": True, "issue": {"recorded_current_status": "Next actor: agent:supervisor\nBrief:\n```text\nDo the task.\n```",
                       "provenance": {"current_status_managed": True}, "labels": [], "launch_check": {"allowed": True}}}
            with patch.object(iterm_tab.subprocess, "run", side_effect=lambda *_a, **_k:
                              subprocess.CompletedProcess([], 0, json.dumps(payload))):
                for _, agent, _ in HARNESS_CHOICES:
                    command.write_text(f"cd /fixture/repo && exec {agent}")
                    for route in ("file", "issue"):
                        args.brief_issue = [issue] if route == "issue" else []
                        for labels in ([], [f"agent:{agent}"]) if agent != "agy" else ([],):
                            payload["issue"]["labels"] = labels
                            args.agent_override = None
                            with self.subTest(agent=agent, route=route, labels=labels):
                                commands, _ = iterm_tab.prepare_launches(args)
                                self.assertIn("Anchor and claim:", commands[0])
                        if agent == "agy":
                            continue
                        payload["issue"]["labels"] = ["agent:claude" if agent == "codex" else "agent:codex"]
                        args.agent_override = reason
                        with self.subTest(agent=agent, route=route, override=True):
                            commands, _ = iterm_tab.prepare_launches(args)
                            words = shlex.split(commands[0])
                            prompt = words[-1]
                            claim_line = next(line for line in prompt.splitlines() if line.startswith("--agent-override "))
                            self.assertEqual(shlex.split(claim_line), ["--agent-override", reason])

    def test_override_instructions_only_reach_workers_using_the_family_exception(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            command, brief = Path(folder) / "command", Path(folder) / "brief"
            command.write_text("claude")
            args = argparse.Namespace(command_file=[command], brief_file=[brief], agent_override="Explicit Director instruction")
            payload: dict[str, Any] = {"ok": True, "issue": {"labels": ["agent:codex"], "launch_check": {"allowed": True}}}
            with patch.object(iterm_tab.subprocess, "run", side_effect=lambda *_a, **_k:
                              subprocess.CompletedProcess([], 0, json.dumps(payload))):
                for anchor, labels in (("Read-only anchor:", ["agent:codex"]), ("Anchor:", ["agent:claude"]), ("Anchor:", []), ("Context:", [])):
                    brief.write_text(f"{anchor} https://github.com/fixture/repo/issues/7\nRules: {iterm_tab.BRIEF_RULES_URL}")
                    payload["issue"]["labels"] = labels
                    with self.subTest(anchor=anchor, labels=labels):
                        prepared, _ = iterm_tab.prepare_launches(args)
                        self.assertNotIn("--agent-override", prepared[0])
            for reason in ("", " ", "Decision\non two lines", "Decision\ron two lines"):
                args.agent_override = reason
                with (self.subTest(reason=reason), patch.object(iterm_tab.subprocess, "run") as reader,
                      self.assertRaisesRegex(ValueError, "--agent-override")):
                    iterm_tab.prepare_launches(args)
                reader.assert_not_called()

    def test_batch_assignment_uses_each_command_instead_of_inherited_lead_family(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            commands, briefs = [root / "codex", root / "claude"], [root / "first", root / "second"]
            for number, (command, brief) in enumerate(zip(commands, briefs, strict=True), 7):
                command.write_text(command.name)
                brief.write_text(f"Anchor: https://github.com/fixture/repo/issues/{number}\nRules: {iterm_tab.BRIEF_RULES_URL}")
            args = argparse.Namespace(command_file=commands, brief_file=briefs)
            for second_assignment in ("codex", "claude"):
                responses = [{"ok": True, "issue": {"labels": [f"agent:{assigned}"], "launch_check": {"allowed": True}}}
                             for assigned in ("codex", second_assignment)]
                with (self.subTest(second_assignment=second_assignment),
                      patch.dict(os.environ, {"CODEX_SESSION_ID": "lead", "CLAUDECODE": "", "CLAUDE_CODE_SESSION_ID": ""}),
                      patch.object(iterm_tab.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 0, json.dumps(row))
                                                                                 for row in responses])):
                    if second_assignment == "codex":
                        with self.assertRaisesRegex(ValueError, "agent:" + second_assignment):
                            iterm_tab.prepare_launches(args)
                    else:
                        self.assertEqual(len(iterm_tab.prepare_launches(args)[0]), len(commands))

    def test_assignment_override_does_not_bypass_unknown_labels_or_other_launch_refusals(self) -> None:
        anchor = "Anchor: https://github.com/fixture/repo/issues/7"
        payload: dict[str, Any] = {"ok": True, "issue": {"launch_check": {"allowed": True}}}
        with patch.object(iterm_tab.subprocess, "run", side_effect=lambda *_a, **_k:
                          subprocess.CompletedProcess([], 0, json.dumps(payload))):
            for override in (None, "Explicit Director instruction"):
                with self.subTest(override=override), self.assertRaisesRegex(ValueError, "assignment unavailable"):
                    iterm_tab.check_launch_issue(anchor, command_text="claude", agent_override=override)
            payload["issue"]["labels"] = ["agent:codex"]
            iterm_tab.check_launch_issue(anchor.replace("Anchor:", "Read-only anchor:"), command_text="claude")
            payload["issue"]["launch_check"] = {"allowed": False, "error": {"message": "Fixture custody conflict"}}
            with self.assertRaisesRegex(ValueError, "Fixture custody conflict"):
                iterm_tab.check_launch_issue(anchor, command_text="claude", agent_override="Explicit Director instruction")

    def test_changed_prerequisite_allows_a_fresh_check_and_read_only_work_does_not_claim(self) -> None:
        brief = "Anchor: https://github.com/fixture/repo/issues/7"
        cleared = {"ok": True, "issue": {"labels": [], "launch_check": {"allowed": True}}}
        parser = runpy.run_path(str(Path(__file__).resolve().parents[2] / "github/scripts/gh-plan.py"))["build_parser"]()
        calls = []
        def read(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
            helper = str(Path(__file__).resolve().parents[2] / "github/scripts/gh-plan.py")
            calls.append(parser.parse_args(argv[argv.index(helper) + 1:]))
            return subprocess.CompletedProcess(argv, 0, json.dumps(cleared))
        with patch.object(iterm_tab.subprocess, "run", side_effect=read):
            token = "successor-worker"
            iterm_tab.check_launch_issue(f"Brief from the Supervisor. Trial token: {token}.\n" + brief)
            self.assertEqual(calls[-1].worker, token)
            iterm_tab.check_launch_issue(f"GUI task: com.example.fixture\nBrief from the Supervisor. Trial token: {token}.\n" + brief)
            self.assertEqual(calls[-1].worker, token)
            iterm_tab.check_launch_issue(brief)
            self.assertEqual(calls[-1].issue, brief.split()[-1])
            self.assertFalse(calls[-1].read_only)
            iterm_tab.check_launch_issue(brief.replace("Anchor:", "Read-only anchor:"))
            self.assertTrue(calls[-1].read_only)
            iterm_tab.check_launch_issue(brief + "\nRead-only anchor: https://github.com/fixture/repo/issues/8\n"
                                         "Wait resolution: Existing scoped authority", wait_resolved="Existing scoped authority")
            self.assertFalse(calls[-2].read_only)
            self.assertEqual(calls[-2].wait_resolved, "Existing scoped authority")
            self.assertTrue(calls[-1].read_only)
            self.assertIsNone(calls[-1].wait_resolved)
            for anchor, resume in (("- Anchor:", "- Resume from: 123"), ("**Anchor:**", "**Resume from:** 123")):
                iterm_tab.check_launch_issue(brief.replace("Anchor:", anchor) + "#issuecomment-123\n" + resume)
                self.assertEqual(calls[-1].resume_from, "123")
            source_url = "https://github.com/fixture/repo/issues/6#issuecomment-123"
            iterm_tab.check_launch_issue(brief + "\nResume from: " + source_url)
            self.assertEqual(calls[-1].resume_from, source_url)
            for value in ("<https://github.com/fixture/repo/issues/7>", "[#7](https://github.com/fixture/repo/issues/7)",
                          "https://github.com/fixture/repo/issues/7/"):
                iterm_tab.check_launch_issue("Anchor: " + value)
                self.assertEqual(calls[-1].issue, brief.split()[-1])
        link = "https://github.com/fixture/direction/issues/9#issuecomment-123"
        with patch.object(iterm_tab.subprocess, "run", side_effect=read):
            for declaration, kind in (("Admission for this item: milestone (" + link + ")", "milestone"),
                                      ("Admission: milestone (" + link + ")", "milestone"),
                                      ("**Admission:** spare-capacity; " + link, "spare-capacity"),
                                      ("- Admission: repeat-stop; " + link + " https://github.com/fixture/repo/issues/8", "repeat-stop")):
                iterm_tab.check_launch_issue(brief + "\n" + declaration)
                self.assertEqual(calls[-1].admission, kind)
                self.assertEqual(calls[-1].admission_link[0], link)
            iterm_tab.check_launch_issue(brief.replace("Anchor:", "Read-only anchor:") + "\nAdmission: milestone")
            self.assertIsNone(calls[-1].admission)
            iterm_tab.check_launch_issue(brief + "\nAdmission: repeat-stop; " + link +
                                        "\n  Context: https://github.com/fixture/repo/issues/88")
            self.assertEqual(calls[-1].admission_link, [link])
        for missing in ("Implement export", "Anchor: https://github.com/fixture/repo/pull/7", "Milestone lead for the Track"):
            with self.subTest(missing=missing), patch.object(iterm_tab.subprocess, "run") as reader:
                iterm_tab.check_launch_issue(missing)
                reader.assert_not_called()
        for malformed in ("fixture/repo#7", "", "https://github.com/fixture/repo/issues/7/other"):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                iterm_tab.check_launch_issue("Anchor: " + malformed)

    def test_multiple_admission_declarations_refuse_before_reading_launch_state(self) -> None:
        brief = "Anchor: https://github.com/fixture/repo/issues/7\nAdmission: milestone\nAdmission: spare-capacity"
        with patch.object(iterm_tab.subprocess, "run") as reader, self.assertRaises(ValueError):
            iterm_tab.check_launch_issue(brief)
        reader.assert_not_called()

    def test_read_only_anchor_rejects_contradictory_execution_fields_before_preflight(self) -> None:
        anchor = "https://github.com/fixture/repo/issues/7"
        for extra in (f"Anchor: {anchor}", f"Anchor and claim: {anchor}", "Resume from: 123",
                      "Resume from: invalid", "Wait resolution: Existing scoped authority", "Wait resolution:"):
            with self.subTest(extra=extra), patch.object(iterm_tab.subprocess, "run") as reader:
                with self.assertRaises(ValueError):
                    iterm_tab.check_launch_issue(f"Read-only anchor: {anchor}\n{extra}")
                reader.assert_not_called()

    def test_read_only_refusal_preserves_the_unreadable_evidence_cause(self) -> None:
        for location in ("ownership", "derived"):
            cause = "Fixture evidence parse failed"
            issue = {"launch_check": {"allowed": False, "ownership": {"error": cause} if location == "ownership" else {}},
                     "derived": {"error": cause} if location == "derived" else {}}
            with self.subTest(location=location), patch.object(iterm_tab.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "issue": issue}))):
                with self.assertRaises(ValueError) as caught:
                    iterm_tab.check_launch_issue("Read-only anchor: https://github.com/fixture/repo/issues/7")
                self.assertIn(cause, str(caught.exception))

    def test_claim_refusal_code_and_details_reach_the_launcher(self) -> None:
        anchor = "Anchor: https://github.com/fixture/repo/issues/7"
        for code in ("claim_wait_unresolved", "claim_milestone_graph_incomplete", "claim_preflight_unavailable"):
            detail = "Fixture specific recovery step"
            payload = {"ok": True, "issue": {"launch_check": {
                "allowed": False, "error": {"code": code, "message": detail}}}}
            with patch.object(iterm_tab.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload))):
                with self.assertRaises(ValueError) as caught:
                    iterm_tab.check_launch_issue(anchor)
                self.assertIn(code, str(caught.exception))
                self.assertIn(detail, str(caught.exception))

    def test_missing_or_failed_state_read_never_launches(self) -> None:
        for code, output in ((1, "{}"), (0, "{}"), (0, "invalid"), (0, '{"ok": true, "issue": {}}')):
            with self.subTest(code=code, output=output), patch.object(iterm_tab.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], code, output)), self.assertRaises(ValueError):
                iterm_tab.check_launch_issue("Anchor: https://github.com/fixture/repo/issues/7")
        for failure in (FileNotFoundError("fixture uv missing"), subprocess.TimeoutExpired("fixture", 120)):
            with self.subTest(failure=failure), patch.object(iterm_tab.subprocess, "run", side_effect=failure), self.assertRaises(ValueError):
                iterm_tab.check_launch_issue("Anchor: https://github.com/fixture/repo/issues/7")


class ResumeLaunchTests(unittest.TestCase):
    def test_plain_install_resume_preserves_source_home(self) -> None:
        with patch.object(account_choice, "load_config", return_value={"snapshot_command": None, "accounts": []}):
            commands, choices = iterm_tab.prepare_launches(self.args)
            self.assertTrue(commands)
            choice = choices[0]
            assert choice is not None
            self.assertEqual(choice["env"], self.account["env"])
            self.select.return_value = {**self.choice, "env": {"CODEX_HOME": str(self.root / "other")}}
            with self.assertRaises(ValueError):
                iterm_tab.prepare_launches(self.args)

    def test_preparation_read_deadline_leaves_account_probes_and_writes_their_own_bounds(self) -> None:
        def select(*args: object) -> dict:
            time.sleep(0.1)
            return self.choice

        self.select.side_effect = select
        self.prepare.side_effect = lambda *args: time.sleep(0.1)
        with patch.object(iterm_tab, "LAUNCH_TIMEOUT", 0.05):
            commands, choices = iterm_tab.prepare_launches(self.args)
        self.assertEqual(choices, [self.choice])
        self.assertEqual(len(commands), 1)
        self.prepare.assert_called_once_with(self.choice)

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp", prefix="resume-tab-"))).resolve()
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root), "CODEX_HOME": str(self.root / "account")}))
        self.home = self.root / "account"
        self.session = str(uuid.uuid4())
        transcript = self.home / "sessions" / (self.session + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.session}}) + "\n")
        self.endpoint = iterm_tab.codex_daemon_endpoint(self.home)
        self.endpoint.parent.mkdir()
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(self.endpoint))
        self.endpoint.chmod(0o600)
        self.entry = {"session_id": self.session, "harness": "codex", "supervisor_owned": True,
                      "codex_home": str(self.home), "transcript": str(transcript), "pid": 123}
        self.enterContext(patch.object(session_liveness, "process_absent", return_value=True))
        self.enterContext(patch.object(session_liveness, "inventory", return_value=[]))
        self.ledger = self.root / "ledger.json"
        self.ledger.write_text(json.dumps([self.entry]))
        self.args = argparse.Namespace(resume_session_id=self.session, ledger=self.ledger,
                                    command_file=None, brief_file=None, account_provider="openai",
                                    account_config=None, account=None)
        self.account = {"provider": "openai", "name": "original", "env": {"CODEX_HOME": str(self.home)}}
        self.choice = {**self.account, "storage_root": self.root, "account_id": "openai-ab12"}
        self.enterContext(patch.object(account_choice, "load_config", return_value={"accounts": [self.account]}))
        self.select = self.enterContext(patch.object(account_choice, "select", return_value=self.choice))
        self.prepare = self.enterContext(patch.object(account_choice, "prepare_launch"))
        self.inspect = self.enterContext(patch.object(iterm_tab, "inspect_codex_daemon"))
        self.start = self.enterContext(patch.object(iterm_tab, "check_codex_daemon"))
        import github_plan_sessions
        self.enterContext(patch.object(github_plan_sessions, "configured_homes", return_value=[self.home]))
        import github_plan_closure
        self.log = self.root / "closed.json"
        self.enterContext(patch.object(github_plan_closure, "closed_log", return_value=self.log))

    def test_resume_uses_exact_source_helper_and_existing_receipt_route(self):
        commands, choices = iterm_tab.prepare_launches(self.args)
        argv = shlex.split(commands[0])
        helper = str(Path(iterm_tab.__file__).with_name("codex-session.py").resolve())
        self.assertIn(helper, argv)
        self.assertEqual(argv[argv.index("--session-id") + 1], self.session)
        self.assertEqual(argv[argv.index("--codex-home") + 1], str(self.home))
        self.select.assert_called_once_with("openai", None, self.account["name"])
        self.prepare.assert_called_once_with(choices[0])
        self.inspect.assert_called_once()
        self.assertEqual(self.inspect.call_args.args[0], self.home)
        self.start.assert_not_called()
        self.assertEqual(choices[0]["resume_session_id"], self.session)

    def retired_lead_source(self) -> dict:
        import release_session_claims
        self.enterContext(patch_attr(release_session_claims, "inventory", return_value=[]))
        self.enterContext(patch.object(account_choice, "inventory_accounts", return_value=[self.account]))
        transcript = self.home / "sessions" / (self.session + ".jsonl")
        records = [json.loads(transcript.read_text()),
                   {"type": "event_msg", "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {"type": "agent_message", "message": "Safe to exit: yes"}},
                   {"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z",
                    "payload": {"type": "task_complete"}}]
        transcript.write_text("".join(json.dumps(row) + "\n" for row in records))
        source = {**self.entry, "tty": "fixture", "lead_session_id": "lead", "lead_launch_id": "consumed",
                  "ended_at": "2026-01-01T00:00:02Z", "verdict_at": "2026-01-01T00:00:01Z",
                  "process_exit_verified": True}
        self.log.write_text(json.dumps([source]))
        self.log.chmod(0o600)
        self.ledger.write_text(json.dumps([{"session_id": "lead", "supervisor_owned": True,
                                          "worker_cap": 2, "worker_launches": {"pending": 1}}]))
        return source

    def test_retired_lead_source_restores_without_reusing_a_consumed_reservation(self) -> None:
        source = self.retired_lead_source()
        before = self.log.read_bytes()
        iterm_tab.append_launch_ledger(self.ledger, source)
        rows = json.loads(self.ledger.read_text())
        self.assertEqual(rows[-1], source)
        self.assertEqual(rows[0]["worker_launches"], {"pending": 1})
        self.assertEqual(self.log.read_bytes(), before)
        with self.assertRaises(ValueError):
            iterm_tab.append_launch_ledger(self.ledger, source)

    def test_restoration_keeps_cap_and_canonical_closed_authority(self) -> None:
        source = self.retired_lead_source()
        closed = self.log.read_bytes()
        rows = json.loads(self.ledger.read_text())
        self.ledger.write_text(json.dumps([*rows, {**rows[0], "session_id": "other"}]))
        ledger = self.ledger.read_bytes()
        for changes in ({"lead_session_id": "other"}, {"lead_session_id": None}, {"lead_launch_id": "pending"},
                        {"codex_home": str(self.root)}, {"pid": 456}, {"transcript": str(self.root / "other.jsonl")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                iterm_tab.append_launch_ledger(self.ledger, {**source, **changes})
            self.assertEqual(self.ledger.read_bytes(), ledger)
        for changes in ({"process_exit_verified": False}, {"resumed_at": "2026-01-01T00:00:03Z"},
                        {"successor_admitted": True}, {"ended_at": None}):
            changed = {**source, **changes}
            self.log.write_text(json.dumps([changed]))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                iterm_tab.append_launch_ledger(self.ledger, changed)
            self.assertEqual(self.ledger.read_bytes(), ledger)
        for records in ([], [source, source]):
            self.log.write_text(json.dumps(records))
            with self.subTest(records=records), self.assertRaises(ValueError):
                iterm_tab.append_launch_ledger(self.ledger, source)
            self.assertEqual(self.ledger.read_bytes(), ledger)
        self.log.write_bytes(closed)
        rows = json.loads(ledger)
        rows[0]["worker_cap"] = 1
        self.ledger.write_text(json.dumps(rows))
        capped = self.ledger.read_bytes()
        with self.assertRaises(ValueError):
            iterm_tab.append_launch_ledger(self.ledger, source)
        self.assertEqual(self.ledger.read_bytes(), capped)

    def test_restoration_refuses_live_changed_frozen_or_unknown_source_without_effects(self) -> None:
        import release_session_claims
        from check_operation import closure
        source = self.retired_lead_source()
        ledger, closed = self.ledger.read_bytes(), self.log.read_bytes()
        with patch_attr(release_session_claims, "inventory", return_value=[(source["pid"], source["tty"], "codex")]):
            with self.assertRaises(ValueError):
                iterm_tab.append_launch_ledger(self.ledger, source)
        for state in ({"successor_admitted": True}, {"action_pending": True, "action_at": "2026-01-02T00:00:00Z"},
                      {"action_pending": True, "action_at": "2025-12-31T23:59:59Z", "claim_admission_unknown": {}}):
            with closure.ownership(self.session, create=True):
                closure.write_lease_state(self.session, {"session_id": self.session, **state})
            with self.subTest(state=state), self.assertRaises(ValueError):
                iterm_tab.append_launch_ledger(self.ledger, source)
        closure.lease_path(self.session).write_text("")
        transcript = Path(source["transcript"])
        transcript.write_text(transcript.read_text() + json.dumps({"type": "event_msg", "timestamp": "2026-01-02T00:00:00Z",
                                                                 "payload": {"type": "user_message", "message": "continue"}}) + "\n")
        with self.assertRaises(ValueError):
            iterm_tab.append_launch_ledger(self.ledger, source)
        self.assertEqual(self.ledger.read_bytes(), ledger)
        self.assertEqual(self.log.read_bytes(), closed)

    def test_restoration_preserves_busy_lease_and_native_identity_refusals(self) -> None:
        from check_operation import closure
        source = self.retired_lead_source()
        ledger, closed = self.ledger.read_bytes(), self.log.read_bytes()
        with closure.ownership(self.session, create=True):
            pass
        with closure.lease_path(self.session).open("r+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(closure.LeaseBusy):
                iterm_tab.append_launch_ledger(self.ledger, source)
        for key, value in (("codex_home", str(self.root)), ("session_id", str(uuid.uuid4()))):
            changed = {**source, key: value}
            self.log.write_text(json.dumps([changed]))
            with self.subTest(key=key), self.assertRaises(ValueError):
                iterm_tab.append_launch_ledger(self.ledger, changed)
        self.assertEqual(self.ledger.read_bytes(), ledger)
        self.log.write_bytes(closed)

    def test_restoration_validation_leaves_shared_ledger_available_for_other_workers(self) -> None:
        import release_session_claims
        source = self.retired_lead_source()
        validate = release_session_claims.validate_closure
        unrelated = {"session_id": str(uuid.uuid4()), "harness": "codex", "supervisor_owned": True}

        def validate_with_registration(entry: dict, supervisor: str) -> dict:
            iterm_tab.append_launch_ledger(self.ledger, unrelated)
            return validate(entry, supervisor)

        with patch_attr(release_session_claims, "validate_closure", side_effect=validate_with_registration):
            iterm_tab.append_launch_ledger(self.ledger, {**source, "tty": "/dev/" + source["tty"]})
        self.assertEqual(json.loads(self.ledger.read_text())[-2:], [unrelated, source])

    def test_restoration_refuses_newer_activity_in_each_configured_transcript_copy(self) -> None:
        source = self.retired_lead_source()
        ledger = self.ledger.read_bytes()
        original = Path(source["transcript"]).read_text()
        other = self.root / "other-account"
        accounts = [self.account, {**self.account, "name": "other", "env": {"CODEX_HOME": str(other)}}]
        for root in (self.home / "archived_sessions", other / "sessions"):
            root.mkdir(parents=True)
            copy = root / (self.session + ".jsonl")
            copy.write_text(original + json.dumps({"type": "event_msg", "timestamp": "2026-01-02T00:00:00Z",
                                                   "payload": {"type": "user_message", "message": "continue"}}) + "\n")
            with patch.object(account_choice, "inventory_accounts", return_value=accounts):
                with self.subTest(root=root), self.assertRaises(ValueError):
                    iterm_tab.append_launch_ledger(self.ledger, source)
            self.assertEqual(self.ledger.read_bytes(), ledger)
            copy.unlink()

    def test_missing_or_live_original_pid_refuses_before_launch_preparation(self) -> None:
        for pid in (None, 0, True, 123):
            with self.subTest(pid=pid), patch.object(session_liveness, "process_absent", return_value=False):
                self.ledger.write_text(json.dumps([{**self.entry, "pid": pid}]))
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_launches(self.args)
        self.select.assert_not_called()
        self.prepare.assert_not_called()

    def test_supervisor_owned_resume_supplies_the_label_to_its_child(self) -> None:
        commands, _ = iterm_tab.prepare_launches(self.args)
        child = self.root / "uv"
        child.write_text('#!/bin/sh\nprintf "%s" "$DISCORD_BLUE_SESSION_KIND"\n')
        child.chmod(0o700)
        result = subprocess.run(["/bin/sh", "-c", commands[0]], env={"PATH": str(self.root) + ":/usr/bin:/bin"},
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "spawned")

    def test_frozen_or_duplicate_source_refuses_before_receipts_and_daemon_reads(self):
        for rows in ([{"session_id": self.session, "successor_admitted": True}],
                     [{"session_id": self.session}, {"session_id": self.session}]):
            with self.subTest(rows=rows):
                self.log.write_text(json.dumps(rows))
                self.log.chmod(0o600)
                original = self.log.read_bytes()
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_launches(self.args)
                self.assertEqual(self.log.read_bytes(), original)
        self.select.assert_not_called()
        self.prepare.assert_not_called()
        self.inspect.assert_not_called()

    def test_tilde_ledger_home_matches_the_same_source_account(self):
        self.ledger.write_text(json.dumps([{**self.entry, "codex_home": "~/account"}]))
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            commands, choices = iterm_tab.prepare_launches(self.args)
        argv = shlex.split(commands[0])
        self.assertEqual(argv[argv.index("--codex-home") + 1], str(self.home))
        self.assertEqual(choices[0]["env"], self.account["env"])

    def test_daemon_failures_stay_on_source_and_refuse_before_receipt_preparation(self):
        from websockets.exceptions import InvalidHandshake, ConnectionClosedError
        for failure in (InvalidHandshake("fixture handshake"), ConnectionClosedError(None, None),
                        iterm_tab.DaemonRefusal("draining", 2), TimeoutError(),
                        ConnectionRefusedError(errno.ECONNREFUSED, "fixture"),
                        FileNotFoundError(errno.ENOENT, "fixture")):
            with self.subTest(failure=type(failure).__name__):
                self.inspect.side_effect = failure
                with self.assertRaises(ValueError) as caught:
                    iterm_tab.prepare_launches(self.args)
                self.assertIn(str(self.home), str(caught.exception))
                self.assertNotIn("--account", str(caught.exception))
        self.prepare.assert_not_called()
        self.start.assert_not_called()

    def test_missing_source_socket_refuses_before_account_or_launch_effects(self):
        self.endpoint.unlink()
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        self.select.assert_not_called()
        self.prepare.assert_not_called()
        self.inspect.assert_not_called()
        self.start.assert_not_called()

    def test_bad_identity_and_launch_options_refuse_before_receipt_preparation(self):
        for changes in ({"supervisor_owned": False}, {"harness": "claude"}, {"codex_home": "relative"},
                        {"session_id": str(uuid.uuid4())}, {"transcript": str(self.root / "foreign.jsonl")}):
            with self.subTest(changes=changes):
                self.ledger.write_text(json.dumps([{**self.entry, **changes}]))
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_launches(self.args)
        self.ledger.write_text(json.dumps([self.entry, self.entry]))
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        self.ledger.write_text(json.dumps([self.entry]))
        for field, value in (("account_provider", "anthropic"), ("account", "other"),
                             ("command_file", [self.root / "command"]), ("brief_file", [self.root / "brief"]),
                             ("require_browser", True), ("computer_use", "required"), ("computer_use", "unused"), ("ledger", None)):
            with self.subTest(field=field), patch.object(self.args, field, value, create=True):
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_launches(self.args)
        self.select.assert_not_called()
        self.prepare.assert_not_called()

    def test_bad_transcript_socket_and_account_selection_have_no_launch_effects(self):
        transcript = Path(self.entry["transcript"])
        original = transcript.read_text()
        transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": str(uuid.uuid4())}}) + "\n")
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        transcript.write_text(original)
        self.endpoint.chmod(0o622)
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        self.endpoint.chmod(0o600)
        self.select.return_value = {**self.choice, "env": {"CODEX_HOME": str(self.root)}}
        with self.assertRaises(ValueError):
            iterm_tab.prepare_launches(self.args)
        self.inspect.assert_not_called()
        self.prepare.assert_not_called()

    def test_resume_metadata_keeps_the_guarded_generation_and_terminal_identity(self):
        import github_plan_closure as closure
        from datetime import datetime, timezone
        born = datetime.now(timezone.utc)
        generation = {"pid": 456, "launch_pid": 456, "tty": "fixture-tty", "codex_home": str(self.home),
                      "launch_generation": "guarded-generation", "launch_started_at": born.isoformat()}
        self.choice["resume_session_id"] = self.session
        with patch.object(closure, "log_rows", return_value=[{"session_id": self.session, "resumed_generation": generation}]), \
                patch.object(closure, "process_started_at", return_value=born):
            result = iterm_tab.source_launch_metadata({"session_id": "terminal-id", "tty": "/dev/fixture-tty"}, self.choice)
            self.assertEqual({key: result[key] for key in generation}, generation)
            self.assertEqual(result["native_session_id"], self.session)
            self.assertEqual(result["session_id"], "terminal-id")
            for change in ({"tty": "wrong-tty"}, {"codex_home": str(self.root)}, {"launch_started_at": "old"}):
                with self.subTest(change=change), patch.object(closure, "log_rows", return_value=[
                        {"session_id": self.session, "resumed_generation": {**generation, **change}}]):
                    failed = iterm_tab.source_launch_metadata({"session_id": "terminal-id", "tty": "/dev/fixture-tty"}, self.choice)
                    self.assertTrue(failed["launch_identity_unavailable"])


class ProviderLaunchTests(unittest.TestCase):
    def test_managed_claude_process_receives_its_launch_ledger_over_an_inherited_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            child, command, brief = root / "claude", root / "command", root / "brief"
            child.write_text(f'#!{sys.executable}\nimport os\nprint(os.getenv({iterm_tab.LAUNCH_LEDGER_ENV!r}))\n')
            child.chmod(0o700)
            command.write_text(f"cd {shlex.quote(str(root))} && exec {shlex.quote(str(child))}")
            brief.write_text("Read-only anchor: https://github.com/fixture/repo/issues/7\nRules: " + iterm_tab.BRIEF_RULES_URL)
            ledger = root / "shared ledger.json"
            args = argparse.Namespace(command_file=[command], brief_file=[brief], ledger=ledger,
                                      account_provider="anthropic", account_config=None, account=None)
            choice = {"provider": "anthropic", "env": {}, "source": "cli-login"}
            with (patch.object(account_choice, "select", return_value=choice),
                  patch.object(iterm_tab, "folder_trust_writes", return_value=[]),
                  patch.object(iterm_tab, "check_claude_tools")):
                commands, _ = iterm_tab.prepare_launch_batch(args)
            result = subprocess.run(["/bin/sh", "-c", commands[0]], check=True, capture_output=True, text=True,
                                    env={**os.environ, iterm_tab.LAUNCH_LEDGER_ENV: "/unrelated/inherited-ledger"})
            self.assertEqual(result.stdout.strip(), str(ledger))

    def test_new_launch_preflight_prepares_a_fresh_ledger_location(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            command = Path(folder) / "command"
            command.write_text("cd /repo && exec claude")
            ledger = Path(folder) / "new-location" / "ledger.json"
            args = argparse.Namespace(ledger=ledger, account_provider="anthropic", command_file=[command])
            with patch.object(iterm_tab, "fresh_claude_session", side_effect=RuntimeError) as prepare:
                with self.assertRaises(RuntimeError):
                    iterm_tab.prepare_launch_batch(args)
                prepare.assert_called_once()
            self.assertTrue(ledger.parent.is_dir())
            self.assertFalse(ledger.exists())

    def test_lead_launch_records_its_owner_on_the_worker_ledger_row(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            args = argparse.Namespace(ledger=ledger, lead_session_id="milestone-lead", lead_launch_id="launch")
            ledger.write_text(json.dumps([{"session_id": args.lead_session_id, "supervisor_owned": True,
                                          "worker_cap": 2, "worker_launches": {args.lead_launch_id: 1}}]))
            native = {"session_id": "worker", "transcript": str(Path(folder) / "worker.jsonl"),
                      "launch_pid": 123, "launch_generation": "generation", "launch_started_at": "started",
                      "codex_home": folder, "tty": "fixture"}
            receipt = {**native, "session_id": "terminal", "native_receipt": str(Path(folder) / "receipt.json")}
            with patch.object(codex_session_proxy, "read_launch_receipt", return_value=native):
                asyncio.run(iterm_tab.register_fresh_launch(args, receipt, None))
            row = next(row for row in json.loads(ledger.read_text()) if row["session_id"] == native["session_id"])
            self.assertEqual(row["lead_session_id"], args.lead_session_id)
            self.assertEqual(receipt["lead_session_id"], args.lead_session_id)

    def test_lead_cap_refuses_overflow_before_accounts_or_terminals_on_both_backends(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            ledger = Path(folder) / "ledger.json"
            lead = {"session_id": "lead", "supervisor_owned": True, "worker_cap": 2}
            worker = {"session_id": "worker", "lead_session_id": lead["session_id"]}
            other = {"session_id": "other", "lead_session_id": "another-lead"}
            args = argparse.Namespace(command="new", window_id="chosen", ledger=ledger,
                                      lead_session_id=lead["session_id"], command_file=[Path(folder) / "absent"],
                                      tmux_socket="fixture")
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            for rows, files in (([lead, worker, {**worker, "session_id": "worker2"}, other], 1),
                                ([lead, worker, other], 2)):
                ledger.write_text(json.dumps(rows))
                args.command_file = [Path(folder) / "absent"] * files
                for backend in ("iterm", "tmux"):
                    with (self.subTest(backend=backend, files=files),
                          patch.dict(sys.modules, {"iterm2": SimpleNamespace()}),
                          patch.object(account_choice, "select") as select,
                          patch.object(tmux_terminal, "Terminal") as terminal):
                        operation = iterm_tab.operate(app, args) if backend == "iterm" else tmux_terminal.operate(args)
                        with self.assertRaisesRegex(ValueError, "worker cap"):
                            asyncio.run(operation)
                        select.assert_not_called()
                        window.async_create_tab.assert_not_called()
                        terminal.return_value.run.assert_not_called()

            @iterm_tab.guard_lead_launch
            async def register_worker(options: argparse.Namespace) -> None:
                await asyncio.to_thread(iterm_tab.append_launch_ledger, options.ledger, {
                    "session_id": "unrelated-worker", "harness": "codex", "supervisor_owned": True})
                iterm_tab.append_launch_ledger(options.ledger, {
                    **worker, **iterm_tab.lead_metadata(options), "harness": "codex", "supervisor_owned": True})

            args.command_file = [Path(folder) / "absent"]
            ledger.write_text(json.dumps([lead, other]))
            asyncio.run(register_worker(args))
            rows = json.loads(ledger.read_text())
            self.assertIn(worker["session_id"], [row["session_id"] for row in rows])
            self.assertIn("unrelated-worker", [row["session_id"] for row in rows])
            self.assertEqual(rows[0]["worker_launches"], {})
            for cap in (None, 0, True, "2"):
                ledger.write_text(json.dumps([{**lead, "worker_cap": cap}, other]))
                with self.subTest(cap=cap), self.assertRaisesRegex(ValueError, "worker cap unavailable"):
                    asyncio.run(register_worker(args))

            for submission in ("not_attempted", "unknown", "submitted"):
                @iterm_tab.guard_lead_launch
                async def fail_launch(options: argparse.Namespace) -> None:
                    options.lead_launch_started = True
                    raise iterm_tab.LaunchFailure([], {"mutation_started": True, "submission": submission,
                        "launch": {**iterm_tab.lead_metadata(options), "session_id": "retained-worker",
                                   "harness": "codex", "supervisor_owned": True}}, ValueError("startup unknown"))

                ledger.write_text(json.dumps([{**lead, "worker_cap": 1}, other]))
                with self.subTest(submission=submission), self.assertRaises(iterm_tab.LaunchFailure) as failed:
                    asyncio.run(fail_launch(args))
                if submission == "not_attempted":
                    self.assertEqual(json.loads(ledger.read_text())[0]["worker_launches"], {})
                    continue
                with self.assertRaisesRegex(ValueError, "worker cap"):
                    asyncio.run(register_worker(args))
                iterm_tab.append_launch_ledger(ledger, failed.exception.detail["failed_launch"]["launch"])
                self.assertEqual(json.loads(ledger.read_text())[0]["worker_launches"], {})
                with self.assertRaisesRegex(ValueError, "worker cap"):
                    asyncio.run(register_worker(args))

            @iterm_tab.guard_lead_launch
            async def interrupted_launch(options: argparse.Namespace) -> None:
                options.lead_launch_started = True
                raise asyncio.CancelledError

            ledger.write_text(json.dumps([lead, other]))
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(interrupted_launch(args))
            self.assertTrue(json.loads(ledger.read_text())[0]["worker_launches"])
            iterm_tab.settle_lead_launch(args, 0)
            self.assertEqual(json.loads(ledger.read_text())[0]["worker_launches"], {})

            ledger.write_text(json.dumps([lead, other]))
            with patch.object(iterm_tab, "settle_lead_launch", side_effect=ValueError("ledger busy")):
                with self.assertRaises(iterm_tab.LaunchFailure) as retained:
                    asyncio.run(fail_launch(args))
            self.assertEqual(retained.exception.detail["failed_launch"]["launch"]["lead_launch_id"], args.lead_launch_id)
            self.assertFalse(retained.exception.detail["reservation_settled"])

            refusal = ValueError("fixture preflight refusal")

            @iterm_tab.guard_lead_launch
            async def refuse_before_launch(_options: argparse.Namespace) -> None:
                raise refusal

            ledger.write_text(json.dumps([lead, other]))
            with self.assertRaises(ValueError) as caught:
                asyncio.run(refuse_before_launch(args))
            self.assertIs(caught.exception, refusal)
            self.assertEqual(json.loads(ledger.read_text())[0]["worker_launches"], {})

    def setUp(self):
        self.enterContext(patch.object(iterm_tab, "check_launch_issue"))
        # Generic launch cases must not discover the host's private opt-in.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        from check_operation import closure
        self.enterContext(patch_attr(closure, "closed_log", return_value=Path(temp.name) / "closed.json"))
        opt_in = patch.object(iterm_tab, "CHROME_OPT_IN", Path(temp.name) / "absent")
        opt_in.start()
        self.addCleanup(opt_in.stop)

    def test_required_browser_needs_a_supported_provider_before_account_selection(self):
        for provider in (None, "google"):
            with tempfile.TemporaryDirectory() as folder:
                command = Path(folder) / "launch"
                brief = Path(folder) / "brief"
                command.write_text("agy")
                brief.write_text("Read the approved page")
                args = SimpleNamespace(command_file=[command], brief_file=[brief],
                                       account_provider=provider, require_browser=True)
                with patch.object(account_choice, "select") as select:
                    with self.assertRaises(ValueError):
                        iterm_tab.prepare_launches(args)
                    select.assert_not_called()

    def test_conflicting_claude_flags_refuse_before_browser_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            opt = Path(folder) / "opt"
            opt.touch()
            with (patch.object(iterm_tab, "CHROME_OPT_IN", opt),
                  patch.object(agent_tools, "connected_browsers") as probe):
                with self.assertRaises(ValueError):
                    iterm_tab.check_claude_tools("cd / && exec claude --chrome",
                                                {"provider": "anthropic", "env": {}})
                probe.assert_not_called()

    def test_claude_project_lookup_resolves_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            repo, alias, home = (root / name for name in ("repo", "alias", "home"))
            repo.mkdir()
            home.mkdir()
            alias.symlink_to(repo, target_is_directory=True)
            opt = root / "opt"
            opt.touch()
            (home / ".claude.json").write_text(json.dumps({
                "mcpServers": {agent_tools.CHROME_SERVER: {"command": "fixture"}},
                "projects": {str(repo): {"disabledMcpServers": [agent_tools.CHROME_SERVER]}}}))
            choice = {"provider": "anthropic", "env": {"CLAUDE_CONFIG_DIR": str(home)}}
            with (patch.object(iterm_tab, "CHROME_OPT_IN", opt),
                  patch.object(agent_tools, "connected_browsers", return_value=[{}]) as probe):
                with self.assertRaises(ValueError):
                    iterm_tab.check_claude_tools(f"cd {shlex.quote(str(alias))} && exec claude",
                                                choice, require_browser=True)
                iterm_tab.check_claude_tools(f"cd {shlex.quote(str(alias))} && exec claude", choice)
                self.assertEqual(len(choice["tools"]["warnings"]), 1)
                probe.assert_not_called()
                for command in ("claude", "cd relative && claude"):
                    with self.assertRaises(ValueError):
                        iterm_tab.check_claude_tools(command, choice, require_browser=True)

    def test_launch_output_omits_target_project_and_expands_home_for_redaction(self):
        choice = {"provider": "anthropic", "env": {"CLAUDE_CONFIG_DIR": "~/fixture-account"},
                  "tool_directory": "/private/fixture-project",
                  "resume_source": {"transcript": "/private/fixture-project/native.jsonl"},
                  "tools": {"warnings": [str(Path.home() / "fixture-account") + " unavailable"]}}
        result = json.dumps(account_choice.public(choice))
        self.assertNotIn("fixture-project", result)
        self.assertNotIn(str(Path.home()), result)

    def test_managed_launch_preserves_direct_prefix_directory_and_literal_prompt(self):
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"}}
        endpoint = "unix://" + str(iterm_tab.codex_daemon_endpoint(Path(choice["env"]["CODEX_HOME"])))
        prepared = iterm_tab.with_account("cd /fixture && exec env TERM=fixture codex --remote " + shlex.quote(endpoint), choice,
                                         "literal $(touch NEVER)", coordinate=True)
        # Parse the shell sections rather than asserting a rendered command.
        parts = shlex.split(prepared)
        self.assertEqual(parts[parts.index("cd") + 1], "/fixture")
        self.assertIn("TERM=fixture", parts)
        native = parts[parts.index("--") + 1:]
        self.assertEqual(native, ["codex", "--remote", endpoint, "-c", f"{MEMORY_SETTING}=false",
                                  "--", "literal $(touch NEVER)"])

    def test_codex_launch_records_account_and_verified_process_generation(self):
        import github_plan_closure
        source = {"session_id": "terminal-generation", "tty": "/dev/fixture-tty"}
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"}}
        with patch.object(session_liveness, "inventory", return_value=[(456, "fixture-tty", "codex --remote unix:///observed"),
                                                              (457, "fixture-tty", "python3 /fixture/codex-session.py launch --codex-home /fixture")]), \
                patch_attr(github_plan_closure, "process_started_at") as started, \
                patch_attr(github_plan_closure, "log_rows", side_effect=FileNotFoundError("fixture log absent")):
            from datetime import datetime, timezone
            started.return_value = datetime(2026, 10, 1, tzinfo=timezone.utc)
            result = iterm_tab.source_launch_metadata(source, choice)
        self.assertEqual(result["pid"], 456)
        self.assertTrue(result["launch_generation"])
        self.assertIsInstance(result["launch_generation"], str)
        self.assertEqual(result["codex_home"], choice["env"]["CODEX_HOME"])

    def test_terminal_resume_cannot_bypass_coordination(self):
        with self.assertRaises(ValueError):
            iterm_tab.validate_resume_input("/resume source")

    def test_claude_resume_and_pasted_reference_keep_their_own_meaning(self):
        target = SimpleNamespace(session_id="fixture-target", async_get_variable=AsyncMock(return_value="/dev/fixture"),
                                 async_send_text=AsyncMock())
        with patch.object(session_liveness, "inventory", return_value=[(456, "fixture", "claude --model opus")]):
            asyncio.run(iterm_tab.send(target, "/resume source", submit=False))
        self.assertEqual(target.async_send_text.call_args.args[0], "/resume source")
        pasted = "The command is:\n/resume source"
        asyncio.run(iterm_tab.send(target, pasted, submit=False))
        self.assertIn(pasted, target.async_send_text.call_args.args[0])
        with patch.object(session_liveness, "inventory", return_value=[(456, "fixture", "codex --remote unix:///fixture")]):
            with self.assertRaises(ValueError):
                asyncio.run(iterm_tab.send(target, "/resume source", submit=False))

    def test_cold_launch_uses_live_proxy_identity_before_native_exec(self):
        import github_plan_closure as closure
        from datetime import datetime, timezone
        born = datetime(2026, 10, 1, tzinfo=timezone.utc)
        source = {"session_id": "terminal-generation", "tty": "/dev/fixture"}
        choice = {"provider": "openai", "env": {"CODEX_HOME": "/fixture/account"}}
        with patch.object(session_liveness, "inventory", return_value=[
                (456, "fixture", "python3 /fixture/codex-session.py launch --codex-home /fixture/account"),
                (457, "fixture", "python3 /fixture/codex-session.py native --codex-home /fixture/account")]), \
                patch.object(closure, "process_started_at", return_value=born), \
                patch.object(closure, "log_rows", return_value=[{"record_kind": "managed_proxy", "launch_pid": 456,
                    "codex_home": choice["env"]["CODEX_HOME"], "launch_started_at": born.isoformat(),
                    "launch_generation": "guarded-generation"}]):
            result = iterm_tab.source_launch_metadata(source, choice)
        self.assertEqual(result["pid"], 456)
        self.assertEqual(result["launch_generation"], "guarded-generation")
        self.assertNotIn("launch_identity_unavailable", result)

    def test_wrong_harness_refuses_with_or_without_a_brief(self):
        for provider, expected, env in HARNESS_CHOICES:
            for _, agent, _ in HARNESS_CHOICES:
                if agent == expected:
                    continue
                for command in (agent, f"cd / && exec /fixture/{agent}"):
                    for brief in (None, "literal brief"):
                        with self.subTest(provider=provider, command=command, brief=brief):
                            with self.assertRaises(ValueError):
                                iterm_tab.with_account(command, {"provider": provider, "env": env}, brief)

    def test_mismatched_later_batch_item_refuses_before_side_effects_on_both_backends(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            files = [root / "first", root / "second"]
            brief = root / "brief"
            brief.write_text("literal task")
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            for provider, expected, env in HARNESS_CHOICES:
                choice = {"provider": provider, "env": env}
                for _, wrong, _ in HARNESS_CHOICES:
                    if wrong == expected:
                        continue
                    files[0].write_text(expected)
                    files[1].write_text(f"cd / && exec /fixture/{wrong}")
                    args = argparse.Namespace(command="new", window_id="chosen", command_file=files,
                                              brief_file=[brief, brief], account_provider=provider,
                                              account_config=None, account=None, tmux_socket="fixture")
                    for backend in ("iterm", "tmux"):
                        terminal = Mock()
                        with (self.subTest(provider=provider, wrong=wrong, backend=backend),
                              patch.dict(sys.modules, {"iterm2": SimpleNamespace()}),
                              patch.object(tmux_terminal, "Terminal", return_value=terminal),
                              patch.object(account_choice, "select_batch", return_value=[choice, choice]),
                              patch.object(iterm_tab, "check_codex_project"),
                              patch.object(iterm_tab, "check_codex_daemon") as daemon,
                              patch.object(account_choice, "prepare_launch") as prepare,
                              patch.object(account_choice, "record_launch") as receipt):
                            with self.assertRaises(ValueError):
                                asyncio.run(iterm_tab.operate(app, args) if backend == "iterm"
                                            else tmux_terminal.operate(args))
                            daemon.assert_not_called()
                            prepare.assert_not_called()
                            receipt.assert_not_called()
                            window.async_create_tab.assert_not_called()
                            terminal.run.assert_not_called()

    def test_matching_commands_execute_with_selected_environment_and_literal_brief(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            brief = "--task's $(touch NEVER) `touch NEVER`\nnext\tline"
            for provider, agent, env in HARNESS_CHOICES:
                child = root / agent
                child.write_text(f'#!{sys.executable}\nimport json, os, sys\n'
                                 'print(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}))\n')
                child.chmod(0o700)
                executable = shlex.quote(str(child))
                for command in (executable, "exec " + executable,
                                "command " + executable, "env TERM=fixture " + executable,
                                f"cd {shlex.quote(str(root))} && exec {executable}"):
                    with self.subTest(provider=provider, command=command):
                        prepared = iterm_tab.with_account(command, {"provider": provider, "env": env}, brief)
                        completed = subprocess.run(["/bin/sh", "-c", prepared], cwd=root,
                                                   env={**os.environ, "CLAUDE_CONFIG_DIR": "/inherited"},
                                                   check=True, capture_output=True, text=True)
                        received = json.loads(completed.stdout)
                        if agent == "agy":
                            self.assertEqual(received["argv"], ["--dangerously-skip-permissions", "--prompt-interactive=" + brief])
                        else:
                            self.assertEqual(received["argv"][-2:], ["--", brief])
                        for key, value in env.items():
                            self.assertEqual(received["env"][key], value)
            self.assertFalse((root / "NEVER").exists())

    def test_pinned_chrome_launch_exports_single_route_and_refuses_explicit_override(self):
        with tempfile.TemporaryDirectory() as folder:
            opt_in = Path(folder) / "chrome.toml"
            opt_in.touch()
            child = Path(folder) / "claude"
            child.write_text(f'#!{sys.executable}\nimport os,json\nprint(json.dumps(dict(os.environ)))\n')
            child.chmod(0o700)
            choice = {"provider": "anthropic", "env": {}}
            with patch.object(iterm_tab, "CHROME_OPT_IN", opt_in):
                command = iterm_tab.with_account(str(child), choice)
                result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, check=True,
                                        env={**os.environ, agent_tools.CHROME_ENV: "1"})
                self.assertEqual(json.loads(result.stdout)[agent_tools.CHROME_ENV], "0")
                for command in ("claude --chrome", f"{agent_tools.CHROME_ENV}=1 claude"):
                    with self.subTest(command=command), self.assertRaises(ValueError):
                        iterm_tab.with_account(command, choice)

    def test_browser_preflight_checks_selected_home_and_caches_batch_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            opt_in = root / "chrome.toml"
            opt_in.touch()
            repo = (root / "repository with spaces").resolve()
            home = root / "selected"
            home.mkdir()
            server = {"command": "fixture"}
            (home / ".claude.json").write_text(json.dumps({
                "mcpServers": {agent_tools.CHROME_SERVER: server},
                "projects": {str(repo): {}}}))
            choice = {"provider": "anthropic", "env": {"CLAUDE_CONFIG_DIR": str(home)}}
            command = "cd " + shlex.quote(str(repo)) + " && claude"
            with (patch.object(iterm_tab, "CHROME_OPT_IN", opt_in),
                  patch.dict(sys.modules, {"iterm2": SimpleNamespace()}),
                  patch.object(agent_tools, "connected_browsers", return_value=[{"id": "fixture"}]) as probe):
                cache = {}
                for _ in range(2):
                    iterm_tab.check_claude_tools(command, choice, probes=cache, require_browser=True)
                self.assertEqual(probe.call_count, 1)
                probe.assert_called_with(server)
                self.assertEqual(choice["tools"]["warnings"], [])
                self.assertEqual(choice["tools"]["browsers"], 1)
                probe.return_value = []
                with self.assertRaises(ValueError):
                    iterm_tab.check_claude_tools(command, choice, require_browser=True)
                iterm_tab.check_claude_tools(command, choice)
                self.assertTrue(choice["tools"]["warnings"])
                probe.reset_mock()
                iterm_tab.check_claude_tools(command, choice, without_browser=True)
                probe.assert_not_called()

    def test_required_browser_refuses_before_receipt_or_terminal_on_both_backends(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launch, brief, opt_in = (root / name for name in ("launch", "brief", "chrome.toml"))
            launch.write_text("claude")
            brief.write_text("Read one page in the Agents browser.")
            opt_in.touch()
            choice = {"provider": "anthropic", "env": {}}
            args = argparse.Namespace(command="new", window_id="chosen", command_file=[launch],
                                      brief_file=[brief], account_provider="anthropic", account_config=None,
                                      account=None, tmux_socket="fixture", require_browser=True)
            window = SimpleNamespace(window_id="chosen", async_create_tab=AsyncMock())
            app = SimpleNamespace(terminal_windows=[window], current_terminal_window=None)
            with (patch.object(iterm_tab, "CHROME_OPT_IN", opt_in),
                  patch.dict(sys.modules, {"iterm2": SimpleNamespace()}),
                  patch.object(agent_tools, "claude_problems", return_value=[]),
                  patch.object(agent_tools, "chrome_server", return_value={"command": "fixture"}),
                  patch.object(agent_tools, "connected_browsers", return_value=[]),
                  patch.object(account_choice, "select", return_value=choice),
                  patch.object(account_choice, "prepare_launch") as prepare,
                  patch.object(account_choice, "record_launch") as receipt,
                  patch.object(tmux_terminal, "Terminal") as terminal):
                for backend in ("iterm", "tmux"):
                    with self.subTest(backend=backend), self.assertRaises(ValueError):
                        asyncio.run(iterm_tab.operate(app, args) if backend == "iterm"
                                    else tmux_terminal.operate(args))
                prepare.assert_not_called()
                receipt.assert_not_called()
                terminal.return_value.run.assert_not_called()
                window.async_create_tab.assert_not_called()

    def test_local_codex_uses_selected_marketplace_and_preserves_account_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / ".codex" / agent_tools.BUNDLED_MARKETPLACE
            source.mkdir(parents=True)
            account = root / "account with spaces"
            account.mkdir()
            config = account / "config.toml"
            config.write_text("# preserve fixture config\n")
            before = config.read_bytes()
            auth = account / "auth.json"
            auth.write_text("opaque fixture; never read by launcher")
            opt_in = root / "chrome.toml"
            opt_in.touch()
            plugins = [{"name": n, "marketplaceName": "openai-bundled", "installed": True, "enabled": True}
                       for n in agent_tools.CODEX_PLUGINS]
            choice = {"provider": "openai", "env": {"CODEX_HOME": str(account)}}
            with (patch.object(iterm_tab, "CHROME_OPT_IN", opt_in),
                  patch.object(Path, "home", return_value=root),
                  patch.object(agent_tools, "codex_plugins", return_value=plugins) as inventory):
                command = iterm_tab.prepare_codex_tools("codex -C /fixture/repo", choice, required=True)
                link = account / agent_tools.BUNDLED_MARKETPLACE
                self.assertEqual(link.resolve(), source.resolve())
                self.assertEqual(config.read_bytes(), before)
                self.assertTrue(auth.is_file())
                inventory.assert_called_once_with(account, link, Path("/fixture/repo"))
                words = shlex.split(command)
                self.assertIn("--no-daemon", words)
                import tomllib
                override = tomllib.loads(words[words.index("-c") + 1])
                self.assertEqual(override["marketplaces"]["openai-bundled"]["source"], str(link))
                self.assertEqual(choice["env"][agent_tools.CODEX_SOURCE_ENV], str(link))
                inventory.return_value = []
                with self.assertRaises(ValueError):
                    iterm_tab.prepare_codex_tools("codex -C /fixture/repo", choice, required=True)
                iterm_tab.prepare_codex_tools("codex -C /fixture/repo", choice)
                self.assertTrue(choice["tools"]["warnings"])
                inventory.side_effect = ValueError("fixture discovery unavailable")
                command = iterm_tab.prepare_codex_tools("codex -C /fixture/repo", choice)
                self.assertIn("--no-daemon", shlex.split(command))
                self.assertEqual(choice["tools"]["route"], "local")
                self.assertTrue(choice["tools"]["warnings"])
                inventory.side_effect = None
                command = iterm_tab.prepare_codex_tools("codex --no-daemon -C /fixture/repo", choice)
                self.assertEqual(shlex.split(command).count("--no-daemon"), 1)
                fallback = {"provider": "openai", "env": {"CODEX_HOME": str(account)}}
                with patch.object(iterm_tab.gui_launch, "marketplace", side_effect=ValueError("fixture setup unavailable")):
                    command = iterm_tab.prepare_codex_tools("codex -C /fixture/repo", fallback)
                self.assertNotIn("--no-daemon", shlex.split(command))
                self.assertEqual(fallback["tools"]["route"], "daemon")
                with patch.object(iterm_tab.gui_launch, "marketplace", side_effect=ValueError("fixture setup unavailable")):
                    command = iterm_tab.prepare_codex_tools("codex --no-daemon -C /fixture/repo", fallback)
                self.assertEqual(fallback["tools"]["route"], "local")
                self.assertIn("--no-daemon", shlex.split(command))
                with patch.object(iterm_tab, "CHROME_OPT_IN", root / "absent"):
                    fresh = {"provider": "openai", "env": {"CODEX_HOME": str(account)}}
                    iterm_tab.prepare_codex_tools("codex --no-daemon -C /fixture/repo", fresh)
                self.assertEqual(fresh["tools"]["route"], "local")
                inventory.reset_mock()
                remote = "codex --remote unix:// -C /fixture/repo"
                self.assertEqual(iterm_tab.prepare_codex_tools(remote, choice, required=True), remote)
                inventory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
