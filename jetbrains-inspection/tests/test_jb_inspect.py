#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import importlib.util
import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch


TEST_CACHE = tempfile.TemporaryDirectory(prefix="jetbrains-inspection-tests-")
os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = TEST_CACHE.name
os.environ["JB_INSPECT_OUTCOME_LOG"] = "0"
os.environ["JB_INSPECT_UNKNOWN_LOG"] = "0"
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "jb-inspect.py"
ATTRIBUTION_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "attribution" / "cases.json"
SPEC = importlib.util.spec_from_file_location("jb_inspect", SCRIPT_PATH)
jb_inspect: Any = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["jb_inspect"] = jb_inspect
SPEC.loader.exec_module(jb_inspect)


def write_json(path: Path, payload: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def attribution_cases() -> list[dict]:
    return json.loads(ATTRIBUTION_FIXTURE_PATH.read_text(encoding="utf-8"))


def make_config_dir(home: Path, name: str) -> Path:
    path = home / "Library" / "Application Support" / "JetBrains" / name
    (path / "options").mkdir(parents=True)
    return path


def make_app(base: Path, name: str, bundle_name: str, bundle_id: str, version: str) -> Path:
    path = base / f"{name}.app"
    contents = path / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleName": bundle_name,
                "CFBundleIdentifier": bundle_id,
                "CFBundleShortVersionString": version,
            },
            handle,
        )
    return path


def helper_args(**overrides):
    values = {
        "ide": None,
        "project_key": None,
        "project_path": None,
        "worktree_path": None,
        "cwd": None,
        "project": None,
        "session_id": None,
        "port": None,
        "open": False,
        "no_worktree_check": False,
    }
    values.update(overrides)
    return Namespace(**values)


def claimed_lifecycle_result(route: dict, close_proof: str = "proof-1"):
    return (
        {
            "status": "claimed",
            "ownership_proven": True,
            "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
        },
        close_proof,
        True,
        route,
    )


def unowned_lifecycle_result(route: dict):
    return (
        {
            "status": "not_owned",
            "ownership_proven": False,
            "reason": "project_preexisted",
            "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
        },
        None,
        False,
        route,
    )


def plugin_claim_body(route: dict, lease_id: str, close_proof: str = "proof-1", owned: bool = True):
    body = {
        "status": "claimed" if owned else "not_owned",
        "ownership_proven": owned,
        "lease_id": lease_id,
        "route": route,
        "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
    }
    if owned:
        body["close_token"] = close_proof
    else:
        body["reason"] = "project_preexisted"
    return body


def scope_capture_diagnostic(*files: dict) -> dict:
    return {
        "scope_kind": "files",
        "scope_resolution_status": "files_resolved",
        "scope_file_requested_count": len(files),
        "scope_file_resolved_count": len(files),
        "scope_file_diagnostics": list(files),
    }


def qualification_payload(
    *,
    since: str = "2026-07-26T00:00:00.000Z",
    after_event_id: str | None = None,
    helper_revision: str = "sha256:" + "1" * 64,
    plugin_build_fingerprint: str = "a" * 40 + "-clean",
    deployment_manifest_sha256: str = "sha256:" + "2" * 64,
) -> dict:
    boundary = {"since": since}
    if after_event_id is not None:
        boundary["after_event_id"] = after_event_id
    return {
        "schema_version": 1,
        "boundary": boundary,
        "helper_revision": helper_revision,
        "plugin_build_fingerprint": plugin_build_fingerprint,
        "deployment_manifest_sha256": deployment_manifest_sha256,
    }


def qualification_event(
    event_id: str,
    *,
    assessment_id: str | None = None,
    timestamp: str = "2026-07-26T00:00:01.000Z",
    verdict: str = "GREEN",
    inspection_run_id: int = 1,
    repo_path_hash: str = "sha256:" + "3" * 64,
    project_key_hash: str = "sha256:" + "4" * 64,
    event_kind: str = "inspection_assessment",
    command: str = "inspect-closeout",
) -> dict:
    assessment_id = assessment_id or f"assessment-{event_id}"
    scope_descriptor = {
        "scope": "changed_files",
        "include_unversioned": True,
        "changed_files_mode": "all",
        "profile": "",
        "severity": "all",
        "problem_type": "all",
        "file_pattern": "all",
        "allow_text_only_coverage": False,
        "include_stale": False,
        "limit": 100,
        "offset": 0,
    }
    decisive = verdict in {"GREEN", "RED"}
    classification = "decisive" if decisive else "legitimate_fail_closed"
    response_code = "no_matching_findings" if verdict == "GREEN" else "actionable_findings" if verdict == "RED" else "timeout"
    cleanup_status = "closed"
    attribution = {
        "schema_version": 1,
        "source": "plugin",
        "observed_by": "helper",
        "classification": classification,
        "code": response_code,
        "phase": "problems",
        "endpoint": "/api/inspection/problems",
        "http_status": 200,
        "request_id": f"request-{event_id}",
        "client_run_id": assessment_id,
        "session_id": "session-1",
        "project_instance_id": "project-instance-1",
        "project_key_hash": project_key_hash,
        "inspection_run_id": inspection_run_id,
        "plugin_version": "1.13.17",
        "plugin_build_fingerprint": "a" * 40 + "-clean",
        "ide_product_code": "PY",
        "ide_version": "2026.2",
        "ide_channel": "eap",
        "ide_channel_source": "plugin_attribution",
        "helper_revision": "sha256:" + "1" * 64,
        "cleanup_status": cleanup_status,
    }
    return {
        "schema_version": 2,
        "event_id": event_id,
        "event_kind": event_kind,
        "assessment_id": assessment_id,
        "timestamp": timestamp,
        "timestamp_ms": jb_inspect.parse_iso_timestamp_ms(timestamp),
        "command": command,
        "verdict": verdict,
        "bucket": "clean" if verdict == "GREEN" else "findings" if verdict == "RED" else "ide_not_ready",
        "verdict_reason": response_code,
        "status": "results_available" if decisive else "running",
        "scope": "changed_files",
        "scope_descriptor": scope_descriptor,
        "scope_descriptor_sha256": jb_inspect.canonical_json_sha256(scope_descriptor),
        "repo_path_hash": repo_path_hash,
        "worktree_root_hash": "sha256:" + "5" * 64,
        "repo_head_sha": "b" * 40,
        "ide": "PyCharm",
        "ide_channel": "eap",
        "ide_channel_source": "plugin_attribution",
        "ide_product_code": "PY",
        "ide_version": "2026.2",
        "project_key_hash": project_key_hash,
        "project_instance_id": "project-instance-1",
        "plugin_build_fingerprint": "a" * 40 + "-clean",
        "plugin_version": "1.13.17",
        "helper_revision": "sha256:" + "1" * 64,
        "deployment_manifest_sha256": "sha256:" + "2" * 64,
        "failure_phase": "problems",
        "attribution_class": classification,
        "response_code": response_code,
        "endpoint": "/api/inspection/problems",
        "http_status": 200,
        "observed_by": "helper",
        "client_run_id": assessment_id,
        "request_id": f"request-{event_id}",
        "session_id": "session-1",
        "inspection_run_id": inspection_run_id,
        "inspection_started": True,
        "inspection_attribution": attribution,
        "cleanup_status": cleanup_status,
        "internal_attempts": [
            {
                "attempt_index": 0,
                "terminal": True,
                "verdict": verdict,
                "verdict_reason": response_code,
                "bucket": "clean" if verdict == "GREEN" else "findings" if verdict == "RED" else "ide_not_ready",
                "retry": False,
                "attribution_class": classification,
                "phase": "problems",
                "inspection_run_id": inspection_run_id,
                "cleanup_status": cleanup_status,
            }
        ],
    }


class ParserCommandAliasTest(unittest.TestCase):
    def test_preferred_commands_parse_and_canonicalize(self):
        parser = jb_inspect.build_parser()
        commands = {
            "list-projects": "list",
            "resolve-route": "route",
            "prepare-worktree": "prepare",
            "agent-inspect": "agent",
            "inspect": "run",
            "inspect-closeout": "closeout",
            "get-status": "status",
            "get-problems": "problems",
            "start-inspection": "trigger",
            "wait-for-inspection": "wait",
            "claim-worktree": "claim",
            "cleanup-helper-leases": "cleanup-leases",
        }

        for command, canonical in commands.items():
            with self.subTest(command=command):
                args = parser.parse_args([command])
                self.assertEqual(jb_inspect.canonical_command(args.command), canonical)

    def test_compatibility_commands_parse_as_preferred_commands(self):
        parser = jb_inspect.build_parser()
        commands = {
            "list": ("list-projects", "list"),
            "route": ("resolve-route", "route"),
            "prepare": ("prepare-worktree", "prepare"),
            "open-worktree": ("prepare-worktree", "prepare"),
            "run": ("inspect", "run"),
            "closeout": ("inspect-closeout", "closeout"),
            "status": ("get-status", "status"),
            "problems": ("get-problems", "problems"),
            "trigger": ("start-inspection", "trigger"),
            "wait": ("wait-for-inspection", "wait"),
            "claim": ("claim-worktree", "claim"),
            "cleanup-leases": ("cleanup-helper-leases", "cleanup-leases"),
        }

        for command, (preferred, canonical) in commands.items():
            with self.subTest(command=command):
                args = jb_inspect.parse_cli_args(parser, [command])
                self.assertEqual(args.command, preferred)
                self.assertEqual(args.command_input, preferred)
                self.assertEqual(jb_inspect.canonical_command(args.command), canonical)

    def test_help_lists_only_preferred_commands(self):
        parser = jb_inspect.build_parser()

        help_text = parser.format_help()

        for command in ("list-projects", "resolve-route", "prepare-worktree", "agent-inspect", "inspect", "inspect-closeout", "get-status", "get-problems"):
            self.assertIn(command, help_text)
        self.assertNotIn("Legacy alias", help_text)
        choices = parser._subparsers._group_actions[0].choices
        for command in ("list", "route", "prepare", "run", "closeout", "status", "problems", "trigger", "wait", "claim", "cleanup-leases"):
            self.assertNotIn(command, choices)

    def test_lifecycle_commands_always_open(self):
        parser = jb_inspect.build_parser()

        args = parser.parse_args(["inspect", "--repo", "/tmp/repo", "--scope", "changed_files"])

        self.assertEqual(jb_inspect.canonical_command(args.command), "run")
        self.assertEqual(args.repo, "/tmp/repo")
        self.assertEqual(args.scope, "changed_files")
        self.assertTrue(args.open)
        for command in ("prepare-worktree", "agent-inspect", "inspect", "inspect-closeout"):
            with self.subTest(command=command), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args([command, "--no-open"])

    def test_assessment_commands_accept_text_only_coverage_override(self):
        parser = jb_inspect.build_parser()

        for command in ("wait-for-inspection", "get-status", "get-problems", "agent-inspect", "inspect", "inspect-closeout"):
            with self.subTest(command=command):
                args = parser.parse_args([command, "--allow-text-only-coverage"])
                self.assertTrue(args.allow_text_only_coverage)

    def test_cleanup_command_has_lifecycle_lock_timeout(self):
        parser = jb_inspect.build_parser()

        args = parser.parse_args(["cleanup-helper-leases", "--lifecycle-lock-timeout-ms", "1234"])

        self.assertEqual(args.lifecycle_lock_timeout_ms, 1234)

    def test_summarize_outcomes_accepts_strict_qualification_options(self):
        parser = jb_inspect.build_parser()

        args = parser.parse_args([
            "summarize-outcomes",
            "--qualification-file",
            "/tmp/qualification.json",
            "--sample-size",
            "50",
        ])

        self.assertEqual(args.qualification_file, "/tmp/qualification.json")
        self.assertEqual(args.sample_size, 50)


class BuildContextTest(unittest.TestCase):
    def test_reads_github_config_for_jetbrains_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github").mkdir()
            write_json(
                root / ".github" / "github.json",
                {
                    "qualityGate": {
                        "inspection": {
                            "ide": "IntelliJ IDEA",
                            "profile": "Codex Skills Readiness",
                            "scopePreference": ["directory", "whole_project"],
                        }
                    },
                    "jetbrains": {
                        "mainWorktreePath": "~/Developer/example-main",
                        "openProjectPath": "packages/app",
                        "worktreeStrategy": "prefer-current",
                    },
                },
            )
            (root / "packages" / "app").mkdir(parents=True)

            args = Namespace(repo=str(root), ide=None, ide_app=None, scope=None, profile="")
            context = jb_inspect.build_context(args)

            self.assertEqual(context["ide"], "IntelliJ IDEA")
            self.assertEqual(context["scope"], "directory")
            self.assertEqual(args.profile, "Codex Skills Readiness")
            self.assertEqual(context["worktree_strategy"], "prefer-current")
            self.assertEqual(context["project_path"], str((root / "packages" / "app").resolve()))
            self.assertTrue(context["main_worktree"].endswith("Developer/example-main"))

    def test_nested_idea_project_remains_requested_project_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            nested = root / "test-fixtures" / "inspection-red-lane-webstorm"
            (nested / ".idea").mkdir(parents=True)

            args = Namespace(repo=str(nested), ide=None, ide_app=None, scope=None)
            context = jb_inspect.build_context(args)

            self.assertEqual(context["repo_path"], str(nested.resolve()))
            self.assertEqual(context["project_path"], str(nested.resolve()))
            self.assertEqual(context["worktree_root"], str(root.resolve()))
            self.assertEqual(context["lifecycle_target_path"], str(nested.resolve()))

    def test_nested_gradle_project_remains_requested_project_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / ".github").mkdir()
            write_json(root / ".github" / "github.json", {"jetbrains": {"openProjectPath": "."}})
            nested = root / "harness" / "workspace" / "project"
            nested.mkdir(parents=True)
            (nested / "settings.gradle").write_text("pluginManagement { repositories { gradlePluginPortal() } }\n", encoding="utf-8")

            args = Namespace(repo=str(nested), ide=None, ide_app=None, scope=None)
            context = jb_inspect.build_context(args)

            self.assertEqual(context["repo_path"], str(nested.resolve()))
            self.assertEqual(context["project_path"], str(nested.resolve()))
            self.assertEqual(context["worktree_root"], str(root.resolve()))
            self.assertEqual(context["lifecycle_target_path"], str(nested.resolve()))

    def test_ide_app_overrides_launch_app_without_changing_identity_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            args = Namespace(repo=str(root), ide="WebStorm", ide_app="WebStorm 2026.2 EAP", scope=None)
            context = jb_inspect.build_context(args)

            self.assertEqual(context["ide"], "WebStorm")
            self.assertEqual(context["ide_app"], "WebStorm 2026.2 EAP")

    def test_product_level_ide_resolves_latest_stable_app_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            applications = Path(tmp) / "Applications"
            applications.mkdir()
            stable_config = make_config_dir(home, "WebStorm2026.1")
            make_config_dir(home, "WebStorm2026.2")
            stable_app = make_app(applications, "WebStorm", "WebStorm", "com.jetbrains.WebStorm", "2026.1.3")
            make_app(applications, "WebStorm 2026.2 EAP", "WebStorm", "com.jetbrains.WebStorm-EAP", "EAP WS-262.8377.39")

            with patch.object(jb_inspect.sys, "platform", "darwin"), \
                patch.object(jb_inspect.Path, "home", return_value=home), \
                patch.object(jb_inspect, "discover_ide_app_candidates", return_value=[
                    jb_inspect.ide_app_candidate(stable_app),
                    jb_inspect.ide_app_candidate(applications / "WebStorm 2026.2 EAP.app"),
                ]):
                selection = jb_inspect.resolve_ide_selection({"ide": "WebStorm"})

        self.assertIsNotNone(selection)
        self.assertEqual(selection.channel, "stable")
        self.assertFalse(selection.public()["is_eap"])
        self.assertFalse(selection.public()["explicit_eap"])
        self.assertEqual(selection.version, (2026, 1, 3))
        self.assertEqual(selection.app_path, stable_app)
        self.assertEqual(selection.config_dir, stable_config)

    def test_exact_eap_selection_uses_eap_app_and_matching_config_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            applications = Path(tmp) / "Applications"
            applications.mkdir()
            eap_config = make_config_dir(home, "WebStorm2026.2")
            make_config_dir(home, "WebStorm2026.1")
            make_app(applications, "WebStorm", "WebStorm", "com.jetbrains.WebStorm", "2026.1.3")
            eap_app = make_app(applications, "WebStorm 2026.2 EAP", "WebStorm", "com.jetbrains.WebStorm-EAP", "EAP WS-262.8377.39")

            with patch.object(jb_inspect.sys, "platform", "darwin"), \
                patch.object(jb_inspect.Path, "home", return_value=home), \
                patch.object(jb_inspect, "discover_ide_app_candidates", return_value=[
                    jb_inspect.ide_app_candidate(applications / "WebStorm.app"),
                    jb_inspect.ide_app_candidate(eap_app),
                ]):
                selection = jb_inspect.resolve_ide_selection({"ide": "WebStorm", "ide_channel": "eap", "ide_version": "2026.2"})

        self.assertIsNotNone(selection)
        self.assertEqual(selection.channel, "eap")
        self.assertTrue(selection.public()["is_eap"])
        self.assertTrue(selection.public()["explicit_eap"])
        self.assertEqual(selection.version[:2], (2026, 2))
        self.assertEqual(selection.app_path, eap_app)
        self.assertEqual(selection.config_dir, eap_config)

    def test_product_level_ide_does_not_implicitly_fall_back_to_eap(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            applications = Path(tmp) / "Applications"
            applications.mkdir()
            make_config_dir(home, "WebStorm2026.2")
            eap_app = make_app(applications, "WebStorm 2026.2 EAP", "WebStorm", "com.jetbrains.WebStorm-EAP", "EAP WS-262.8377.39")

            with patch.object(jb_inspect.sys, "platform", "darwin"), \
                patch.object(jb_inspect.Path, "home", return_value=home), \
                patch.object(jb_inspect, "discover_ide_app_candidates", return_value=[jb_inspect.ide_app_candidate(eap_app)]):
                selection = jb_inspect.resolve_ide_selection({"ide": "WebStorm"})

        self.assertIsNotNone(selection)
        self.assertNotEqual(selection.channel, "eap")
        self.assertFalse(selection.public()["is_eap"])
        self.assertFalse(selection.public()["explicit_eap"])
        self.assertIsNone(selection.app_path)

    def test_exact_eap_selection_without_matching_app_does_not_fall_back_to_generic_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            applications = Path(tmp) / "Applications"
            applications.mkdir()
            eap_config = make_config_dir(home, "WebStorm2026.2")
            make_app(applications, "WebStorm", "WebStorm", "com.jetbrains.WebStorm", "2026.1.3")

            with patch.object(jb_inspect.sys, "platform", "darwin"), \
                patch.object(jb_inspect.Path, "home", return_value=home), \
                patch.object(jb_inspect, "discover_ide_app_candidates", return_value=[
                    jb_inspect.ide_app_candidate(applications / "WebStorm.app"),
                ]):
                selection = jb_inspect.resolve_ide_selection({"ide": "WebStorm", "ide_channel": "eap", "ide_version": "2026.2"})

        self.assertIsNotNone(selection)
        self.assertEqual(selection.config_dir, eap_config)
        self.assertIsNone(selection.app_name)
        self.assertIsNone(selection.app_path)

    def test_exact_ide_app_without_matching_candidate_does_not_pair_with_stable_app_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            applications = Path(tmp) / "Applications"
            applications.mkdir()
            make_config_dir(home, "WebStorm2026.2")
            make_app(applications, "WebStorm", "WebStorm", "com.jetbrains.WebStorm", "2026.1.3")

            with patch.object(jb_inspect.sys, "platform", "darwin"), \
                patch.object(jb_inspect.Path, "home", return_value=home), \
                patch.object(jb_inspect, "discover_ide_app_candidates", return_value=[
                    jb_inspect.ide_app_candidate(applications / "WebStorm.app"),
                ]):
                selection = jb_inspect.resolve_ide_selection({"ide": "WebStorm", "ide_app": "WebStorm 2026.2 EAP"})

        self.assertIsNotNone(selection)
        self.assertEqual(selection.app_name, "WebStorm 2026.2 EAP")
        self.assertIsNone(selection.app_path)


class WorktreeSafetyTest(unittest.TestCase):
    def test_rejects_route_outside_current_worktree(self):
        route = {"base_path": "/tmp/main-checkout"}
        context = {"worktree_root": "/tmp/linked-worktree", "worktree_strategy": "prefer-current"}
        args = Namespace(no_worktree_check=False)

        with self.assertRaises(jb_inspect.InspectError) as raised:
            jb_inspect.ensure_worktree_safe(route, context, args)

        self.assertIn("wrong tree", str(raised.exception))
        self.assertEqual(raised.exception.exit_code, 3)

    def test_allows_current_worktree_inside_open_project(self):
        route = {"base_path": "/tmp/main-checkout"}
        context = {"worktree_root": "/tmp/main-checkout/packages/app", "worktree_strategy": "prefer-current"}
        args = Namespace(no_worktree_check=False)

        jb_inspect.ensure_worktree_safe(route, context, args)

    def test_allows_open_project_inside_current_worktree(self):
        route = {"base_path": "/tmp/current-worktree/packages/app"}
        context = {"worktree_root": "/tmp/current-worktree", "worktree_strategy": "prefer-current"}
        args = Namespace(no_worktree_check=False)

        jb_inspect.ensure_worktree_safe(route, context, args)

    def test_approval_flag_allows_any_worktree(self):
        route = {"base_path": "/tmp/main-checkout"}
        context = {"worktree_root": "/tmp/linked-worktree", "worktree_strategy": "prefer-current"}
        args = Namespace(no_worktree_check=True)

        jb_inspect.ensure_worktree_safe(route, context, args)

    def test_exact_worktree_rejects_containing_project(self):
        route = {"base_path": "/tmp/main-checkout"}
        context = {"worktree_root": "/tmp/main-checkout/packages/app"}
        args = Namespace(no_worktree_check=False)

        with self.assertRaises(jb_inspect.InspectError):
            jb_inspect.ensure_exact_worktree(route, context, args)

    def test_route_sort_key_prefers_exact_worktree_for_equal_scores(self):
        context = {"worktree_root": "/tmp/repo/packages/app"}
        parent = {"score": 930, "base_path": "/tmp/repo"}
        child = {"score": 930, "base_path": "/tmp/repo/packages/app"}

        routes = sorted([parent, child], key=lambda route: jb_inspect.route_sort_key(route, context), reverse=True)

        self.assertEqual(routes[0], child)

    def test_route_sort_key_prefers_exact_nested_project_for_equal_scores(self):
        context = {"worktree_root": "/tmp/repo", "exact_route_path": "/tmp/repo/packages/app"}
        parent = {"score": 930, "base_path": "/tmp/repo"}
        child = {"score": 930, "base_path": "/tmp/repo/packages/app"}

        routes = sorted([parent, child], key=lambda route: jb_inspect.route_sort_key(route, context), reverse=True)

        self.assertEqual(routes[0], child)

    def test_route_sort_key_prefers_deeper_containing_project_for_equal_scores(self):
        context = {"worktree_root": "/tmp/repo/packages/app/src/main"}
        parent = {"score": 930, "base_path": "/tmp/repo"}
        child = {"score": 930, "base_path": "/tmp/repo/packages/app"}

        routes = sorted([parent, child], key=lambda route: jb_inspect.route_sort_key(route, context), reverse=True)

        self.assertEqual(routes[0], child)

    def test_flat_project_matches_exact_nested_project(self):
        project = {"base_path": "/tmp/repo/packages/app"}
        context = {"worktree_root": "/tmp/repo", "project_path": "/tmp/repo/packages/app", "exact_route_path": "/tmp/repo/packages/app"}

        self.assertTrue(jb_inspect.flat_project_matches_context(project, context))


class LifecycleTest(unittest.TestCase):
    def test_emit_redacts_sensitive_keys_from_json(self):
        payload = {
            "status": "prepared",
            "close_token": "value-that-must-not-print",
            "nested": {"password": "another-value-that-must-not-print", "project_key": "path:/tmp/repo"},
        }

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = jb_inspect.emit(payload, json_only=True, exit_code=0)

        self.assertEqual(exit_code, 0)
        body = json.loads(output.getvalue())
        self.assertEqual(body["close_token"], jb_inspect.REDACTED)
        self.assertEqual(body["nested"]["password"], jb_inspect.REDACTED)
        self.assertEqual(body["nested"]["project_key"], "path:/tmp/repo")
        self.assertNotIn("value-that-must-not-print", output.getvalue())
        self.assertNotIn("another-value-that-must-not-print", output.getvalue())

    def test_emit_strips_private_fields_from_json(self):
        payload = {"status": "prepared", "_control": {"secret": "private-value"}}

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = jb_inspect.emit(payload, json_only=True, exit_code=0)

        self.assertEqual(exit_code, 0)
        body = json.loads(output.getvalue())
        self.assertNotIn("_control", body)
        self.assertNotIn("private-value", output.getvalue())

    def test_prepare_lifecycle_does_not_return_private_close_control(self):
        original_create = jb_inspect.create_local_lease
        original_find = jb_inspect.find_exact_route
        original_ensure = jb_inspect.ensure_exact_worktree
        original_wait_ready = jb_inspect.wait_until_route_ready
        original_claim = jb_inspect.claim_lifecycle
        original_write = jb_inspect.write_lease
        try:
            route = {
                "project_key": "path:/tmp/repo",
                "base_path": "/tmp/repo",
                "project_instance_id": "session:1",
                "session_id": "session",
            }
            jb_inspect.create_local_lease = lambda context, state="preparing": {"lease_id": "lease-1", "state": state}
            jb_inspect.find_exact_route = lambda args, context: route
            jb_inspect.ensure_exact_worktree = lambda route, context, args: None
            jb_inspect.wait_until_route_ready = lambda args, context, route, timeout_ms: None
            jb_inspect.claim_lifecycle = lambda args, context, route, lease: claimed_lifecycle_result(
                route,
                "private-close-proof",
            )
            jb_inspect.write_lease = lambda lease: None

            prepared = jb_inspect.prepare_lifecycle(Namespace(prepare_timeout_ms=1), {"worktree_root": "/tmp/repo"})
        finally:
            jb_inspect.create_local_lease = original_create
            jb_inspect.find_exact_route = original_find
            jb_inspect.ensure_exact_worktree = original_ensure
            jb_inspect.wait_until_route_ready = original_wait_ready
            jb_inspect.claim_lifecycle = original_claim
            jb_inspect.write_lease = original_write

        self.assertEqual(prepared["status"], "prepared")
        self.assertNotIn("_control", prepared)
        self.assertNotIn("private-close-proof", json.dumps(prepared))

    def test_prepare_failure_releases_preexisting_lease_without_close(self):
        lease = {"lease_id": "lease-1", "state": "preparing"}
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        removed = []
        claimed = []

        with (
            patch.object(jb_inspect, "create_local_lease", return_value=lease),
            patch.object(jb_inspect, "find_exact_route", return_value=route),
            patch.object(jb_inspect, "matching_deferred_lease", return_value=None),
            patch.object(jb_inspect, "ensure_exact_worktree"),
            patch.object(
                jb_inspect,
                "wait_until_route_ready",
                side_effect=jb_inspect.InspectError(
                    "not ready",
                    3,
                    {"error_reason": "ide_not_ready_timeout", "last_status": {"indexing": True}},
                ),
            ),
            patch.object(
                jb_inspect,
                "claim_lifecycle",
                side_effect=lambda *args: claimed.append(args) or unowned_lifecycle_result(route),
            ),
            patch.object(jb_inspect, "write_lease"),
            patch.object(jb_inspect, "remove_lease", side_effect=lambda removed_lease: removed.append(removed_lease["lease_id"])),
        ):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.prepare_lifecycle_details(
                    helper_args(open=True, prepare_timeout_ms=1),
                    {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                )

        self.assertEqual(len(claimed), 1)
        self.assertEqual(removed, ["lease-1"])
        self.assertEqual(lease["state"], "released")
        self.assertEqual(raised.exception.payload["error_reason"], "ide_not_ready_timeout")
        self.assertEqual(raised.exception.payload["last_status"], {"indexing": True})
        self.assertEqual(raised.exception.payload["cleanup"], {"status": "not_needed", "reason": "project_preexisted"})

    def test_prepare_failure_after_open_acceptance_records_cleanup_pending(self):
        lease = {"lease_id": "lease-1", "state": "preparing"}
        written = []
        open_attempts = [
            {
                "method": "running_ide",
                "accepted": True,
                "ownership_registered": True,
                "identity": {"session_id": "session", "port": 63342},
                "endpoint_status": "opening",
            }
        ]

        with (
            patch.object(jb_inspect, "create_local_lease", return_value=lease),
            patch.object(jb_inspect, "find_exact_route", return_value=None),
            patch.object(jb_inspect, "ensure_trusted_auto_open_root"),
            patch.object(jb_inspect, "ensure_jetbrains_trusted_locations"),
            patch.object(jb_inspect, "open_project_for_lifecycle", return_value=("running_ide", open_attempts, True)),
            patch.object(
                jb_inspect,
                "wait_for_exact_route_after_open",
                side_effect=jb_inspect.InspectError("route missing", 3, {"error_reason": "project_open_blocked"}),
            ),
            patch.object(jb_inspect, "write_lease", side_effect=lambda written_lease: written.append(written_lease.copy())),
        ):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.prepare_lifecycle_details(
                    helper_args(open=True, prepare_timeout_ms=1),
                    {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                )

        self.assertTrue(lease["opened_by_helper"])
        self.assertEqual(lease["open_method"], "running_ide")
        self.assertEqual(lease["session_id"], "session")
        self.assertEqual(lease["state"], "cleanup_pending")
        self.assertEqual(lease["preparation_failure_stage"], "route_wait")
        self.assertEqual(written[-1]["state"], "cleanup_pending")
        self.assertEqual(raised.exception.payload["cleanup"]["status"], "deferred")
        self.assertEqual(raised.exception.payload["cleanup"]["reason"], "preparation_cleanup_pending")

    def test_prepare_failure_during_route_validation_does_not_close_unverified_route(self):
        lease = {"lease_id": "lease-1", "state": "preparing"}
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        cleanup_calls = []

        with (
            patch.object(jb_inspect, "create_local_lease", return_value=lease),
            patch.object(jb_inspect, "find_exact_route", return_value=None),
            patch.object(jb_inspect, "ensure_trusted_auto_open_root"),
            patch.object(jb_inspect, "ensure_jetbrains_trusted_locations"),
            patch.object(
                jb_inspect,
                "open_project_for_lifecycle",
                return_value=(
                    "running_ide",
                    [
                        {
                            "method": "running_ide",
                            "accepted": True,
                            "ownership_registered": True,
                            "identity": {"session_id": "session", "port": 1},
                        }
                    ],
                    True,
                ),
            ),
            patch.object(jb_inspect, "wait_for_exact_route_after_open", return_value=route),
            patch.object(jb_inspect, "ensure_exact_worktree", side_effect=jb_inspect.InspectError("wrong route", 3)),
            patch.object(jb_inspect, "reclaim_close_proof", side_effect=lambda *args: cleanup_calls.append("reclaim")),
            patch.object(
                jb_inspect,
                "cleanup_lifecycle",
                side_effect=lambda *args: cleanup_calls.append("close"),
            ),
            patch.object(jb_inspect, "write_lease"),
        ):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.prepare_lifecycle_details(
                    helper_args(open=True, prepare_timeout_ms=1),
                    {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                )

        self.assertEqual(cleanup_calls, [])
        self.assertEqual(lease["state"], "cleanup_pending")
        self.assertNotIn("project_instance_id", lease)
        self.assertEqual(raised.exception.payload["cleanup"]["status"], "deferred")
        self.assertEqual(raised.exception.payload["preparation_stage"], "route_validation")

    def test_prepare_readiness_failure_reclaims_and_closes(self):
        lease = {"lease_id": "lease-1", "state": "preparing"}
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        cleanup_calls = []

        with (
            patch.object(jb_inspect, "create_local_lease", return_value=lease),
            patch.object(jb_inspect, "find_exact_route", return_value=None),
            patch.object(jb_inspect, "ensure_trusted_auto_open_root"),
            patch.object(jb_inspect, "ensure_jetbrains_trusted_locations"),
            patch.object(
                jb_inspect,
                "open_project_for_lifecycle",
                return_value=(
                    "running_ide",
                    [
                        {
                            "method": "running_ide",
                            "accepted": True,
                            "ownership_registered": True,
                            "identity": {"session_id": "session", "port": 1},
                        }
                    ],
                    True,
                ),
            ),
            patch.object(jb_inspect, "wait_for_exact_route_after_open", return_value=route),
            patch.object(jb_inspect, "ensure_exact_worktree"),
            patch.object(
                jb_inspect,
                "claim_lifecycle",
                return_value=claimed_lifecycle_result(route, "proof-1"),
            ),
            patch.object(
                jb_inspect,
                "wait_until_route_ready",
                side_effect=jb_inspect.InspectError("not ready", 3, {"error_reason": "ide_not_ready_timeout"}),
            ),
            patch.object(
                jb_inspect,
                "cleanup_lifecycle",
                side_effect=lambda cleanup_lease, cleanup_route, proof: cleanup_calls.append(
                    (cleanup_lease["lease_id"], cleanup_route["project_instance_id"], proof)
                )
                or {"status": "closed"},
            ),
            patch.object(jb_inspect, "write_lease"),
        ):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.prepare_lifecycle_details(
                    helper_args(open=True, prepare_timeout_ms=1),
                    {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                )

        self.assertEqual(cleanup_calls, [("lease-1", "session:1", "proof-1")])
        self.assertEqual(raised.exception.payload["error_reason"], "ide_not_ready_timeout")
        self.assertEqual(raised.exception.payload["cleanup"], {"status": "closed"})

    def test_prepare_cleanup_error_does_not_mask_readiness_failure(self):
        lease = {"lease_id": "lease-1", "state": "preparing"}
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }

        with (
            patch.object(jb_inspect, "create_local_lease", return_value=lease),
            patch.object(jb_inspect, "find_exact_route", return_value=None),
            patch.object(jb_inspect, "ensure_trusted_auto_open_root"),
            patch.object(jb_inspect, "ensure_jetbrains_trusted_locations"),
            patch.object(
                jb_inspect,
                "open_project_for_lifecycle",
                return_value=(
                    "running_ide",
                    [
                        {
                            "method": "running_ide",
                            "accepted": True,
                            "ownership_registered": True,
                            "identity": {"session_id": "session", "port": 1},
                        }
                    ],
                    True,
                ),
            ),
            patch.object(jb_inspect, "wait_for_exact_route_after_open", return_value=route),
            patch.object(jb_inspect, "ensure_exact_worktree"),
            patch.object(
                jb_inspect,
                "claim_lifecycle",
                return_value=claimed_lifecycle_result(route, "proof-1"),
            ),
            patch.object(
                jb_inspect,
                "wait_until_route_ready",
                side_effect=jb_inspect.InspectError("not ready", 3, {"error_reason": "ide_not_ready_timeout"}),
            ),
            patch.object(jb_inspect, "cleanup_lifecycle", side_effect=RuntimeError("close crashed")),
            patch.object(jb_inspect, "write_lease"),
        ):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.prepare_lifecycle_details(
                    helper_args(open=True, prepare_timeout_ms=1),
                    {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                )

        self.assertEqual(raised.exception.payload["error_reason"], "ide_not_ready_timeout")
        self.assertEqual(raised.exception.payload["cleanup"]["status"], "deferred")
        self.assertEqual(raised.exception.payload["cleanup"]["cleanup_error"], "RuntimeError")
        self.assertEqual(lease["state"], "cleanup_pending")

    def test_prepare_claim_failure_records_cleanup_pending_when_reclaim_fails(self):
        lease = {"lease_id": "lease-1", "state": "preparing"}
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        written = []

        with (
            patch.object(jb_inspect, "create_local_lease", return_value=lease),
            patch.object(jb_inspect, "find_exact_route", return_value=None),
            patch.object(jb_inspect, "ensure_trusted_auto_open_root"),
            patch.object(jb_inspect, "ensure_jetbrains_trusted_locations"),
            patch.object(
                jb_inspect,
                "open_project_for_lifecycle",
                return_value=(
                    "running_ide",
                    [
                        {
                            "method": "running_ide",
                            "accepted": True,
                            "ownership_registered": True,
                            "identity": {"session_id": "session", "port": 1},
                        }
                    ],
                    True,
                ),
            ),
            patch.object(jb_inspect, "wait_for_exact_route_after_open", return_value=route),
            patch.object(jb_inspect, "ensure_exact_worktree"),
            patch.object(jb_inspect, "wait_until_route_ready"),
            patch.object(
                jb_inspect,
                "claim_lifecycle",
                side_effect=jb_inspect.InspectError("HTTP 500", 3, {"error_reason": "inspection_api_http_error"}),
            ),
            patch.object(
                jb_inspect,
                "reclaim_lifecycle_claim",
                return_value=(None, None, {"status": "unknown"}, route),
            ),
            patch.object(jb_inspect, "write_lease", side_effect=lambda written_lease: written.append(written_lease.copy())),
        ):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.prepare_lifecycle_details(
                    helper_args(open=True, prepare_timeout_ms=1),
                    {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                )

        self.assertEqual(lease["state"], "cleanup_pending")
        self.assertEqual(lease["project_instance_id"], "session:1")
        self.assertEqual(lease["session_id"], "session")
        self.assertEqual(lease["preparation_failure_stage"], "lifecycle_claim")
        self.assertEqual(written[-1]["state"], "cleanup_pending")
        self.assertEqual(raised.exception.payload["error_reason"], "inspection_api_http_error")
        self.assertEqual(raised.exception.payload["cleanup"]["status"], "deferred")

    def test_prepare_unproven_open_never_closes_project(self):
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        for outcome, method in (("already_open", "preexisting"), ("already_opening", "unproven_opening")):
            with self.subTest(outcome=outcome):
                lease = {"lease_id": f"lease-{outcome}", "state": "preparing"}
                close_calls = []
                open_attempts = [
                    {
                        "method": "running_ide",
                        "accepted": True,
                        "ownership_registered": False,
                        "open_outcome": outcome,
                    }
                ]
                with (
                    patch.object(jb_inspect, "create_local_lease", return_value=lease),
                    patch.object(jb_inspect, "find_exact_route", return_value=None),
                    patch.object(jb_inspect, "ensure_trusted_auto_open_root"),
                    patch.object(jb_inspect, "ensure_jetbrains_trusted_locations"),
                    patch.object(
                        jb_inspect,
                        "open_project_for_lifecycle",
                        return_value=(method, open_attempts, False),
                    ),
                    patch.object(jb_inspect, "wait_for_exact_route_after_open", return_value=route),
                    patch.object(jb_inspect, "ensure_exact_worktree"),
                    patch.object(
                        jb_inspect,
                        "claim_lifecycle",
                        return_value=unowned_lifecycle_result(route),
                    ),
                    patch.object(
                        jb_inspect,
                        "wait_until_route_ready",
                        side_effect=jb_inspect.InspectError(
                            "not ready",
                            3,
                            {"error_reason": "ide_not_ready_timeout"},
                        ),
                    ),
                    patch.object(jb_inspect, "call_lifecycle_close", side_effect=lambda *args: close_calls.append(args)),
                    patch.object(jb_inspect, "write_lease"),
                    patch.object(jb_inspect, "remove_lease"),
                ):
                    with self.assertRaises(jb_inspect.InspectError):
                        jb_inspect.prepare_lifecycle_details(
                            helper_args(open=True, prepare_timeout_ms=1),
                            {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                        )

                self.assertEqual(close_calls, [])
                self.assertFalse(lease["opened_by_helper"])
                self.assertEqual(lease["state"], "released")

    def test_prepare_registered_open_downgrades_when_claim_is_not_owned(self):
        lease = {"lease_id": "lease-1", "state": "preparing"}
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        open_attempts = [
            {
                "method": "running_ide",
                "accepted": True,
                "ownership_registered": True,
                "identity": {"session_id": "session", "port": 1},
            }
        ]

        with (
            patch.object(jb_inspect, "create_local_lease", return_value=lease),
            patch.object(jb_inspect, "find_exact_route", return_value=None),
            patch.object(jb_inspect, "ensure_trusted_auto_open_root"),
            patch.object(jb_inspect, "ensure_jetbrains_trusted_locations"),
            patch.object(
                jb_inspect,
                "open_project_for_lifecycle",
                return_value=("running_ide", open_attempts, True),
            ),
            patch.object(jb_inspect, "wait_for_exact_route_after_open", return_value=route),
            patch.object(jb_inspect, "ensure_exact_worktree"),
            patch.object(jb_inspect, "claim_lifecycle", return_value=unowned_lifecycle_result(route)),
            patch.object(jb_inspect, "wait_until_route_ready"),
            patch.object(jb_inspect, "write_lease"),
        ):
            prepared, prepared_lease, close_proof = jb_inspect.prepare_lifecycle_details(
                helper_args(open=True, prepare_timeout_ms=1),
                {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
            )

        self.assertFalse(prepared["opened_by_helper"])
        self.assertFalse(prepared_lease["opened_by_helper"])
        self.assertIsNone(close_proof)
        self.assertEqual(prepared["claim"]["status"], "not_owned")

    def test_prepare_rejects_route_from_different_open_session(self):
        lease = {"lease_id": "lease-1", "state": "preparing"}
        route = {
            "port": 2,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session-b:1",
            "session_id": "session-b",
        }
        cleanup_calls = []
        open_attempts = [
            {
                "method": "running_ide",
                "accepted": True,
                "ownership_registered": True,
                "identity": {"session_id": "session-a", "port": 1},
            }
        ]

        with (
            patch.object(jb_inspect, "create_local_lease", return_value=lease),
            patch.object(jb_inspect, "find_exact_route", return_value=None),
            patch.object(jb_inspect, "ensure_trusted_auto_open_root"),
            patch.object(jb_inspect, "ensure_jetbrains_trusted_locations"),
            patch.object(
                jb_inspect,
                "open_project_for_lifecycle",
                return_value=("running_ide", open_attempts, True),
            ),
            patch.object(jb_inspect, "wait_for_exact_route_after_open", return_value=route),
            patch.object(jb_inspect, "ensure_exact_worktree"),
            patch.object(jb_inspect, "reclaim_close_proof", side_effect=lambda *args: cleanup_calls.append("reclaim")),
            patch.object(jb_inspect, "cleanup_lifecycle", side_effect=lambda *args: cleanup_calls.append("close")),
            patch.object(jb_inspect, "write_lease"),
        ):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.prepare_lifecycle_details(
                    helper_args(open=True, prepare_timeout_ms=1),
                    {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                )

        self.assertTrue(raised.exception.payload["session_drift"])
        self.assertEqual(cleanup_calls, [])
        self.assertEqual(lease["session_id"], "session-a")
        self.assertNotIn("project_instance_id", lease)
        self.assertEqual(lease["state"], "cleanup_pending")

    def test_prepare_cleanup_retains_original_payload_fields(self):
        lease = {"lease_id": "lease-1", "state": "preparing"}
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        original_cleanup = {"status": "original"}
        original_lease = {"lease_id": "original"}

        with (
            patch.object(jb_inspect, "create_local_lease", return_value=lease),
            patch.object(jb_inspect, "find_exact_route", return_value=route),
            patch.object(jb_inspect, "matching_deferred_lease", return_value=None),
            patch.object(jb_inspect, "ensure_exact_worktree"),
            patch.object(
                jb_inspect,
                "claim_lifecycle",
                return_value=unowned_lifecycle_result(route),
            ),
            patch.object(
                jb_inspect,
                "wait_until_route_ready",
                side_effect=jb_inspect.InspectError(
                    "not ready",
                    3,
                    {
                        "error_reason": "ide_not_ready_timeout",
                        "cleanup": original_cleanup,
                        "lease": original_lease,
                    },
                ),
            ),
            patch.object(jb_inspect, "write_lease"),
            patch.object(jb_inspect, "remove_lease"),
        ):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.prepare_lifecycle_details(
                    helper_args(open=True, prepare_timeout_ms=1),
                    {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                )

        self.assertEqual(raised.exception.payload["cleanup"], original_cleanup)
        self.assertEqual(raised.exception.payload["lease"], original_lease)
        self.assertEqual(raised.exception.payload["preparation_cleanup"]["reason"], "project_preexisted")
        self.assertEqual(raised.exception.payload["preparation_lease"]["lease_id"], "lease-1")
        self.assertEqual(raised.exception.payload["context"]["worktree_root"], "/tmp/worktree")

    def test_prepare_interrupt_before_open_request_records_cleanup_pending(self):
        lease = {"lease_id": "lease-1", "state": "preparing"}
        writes = []

        def interrupt_first_write(written_lease):
            writes.append(written_lease.copy())
            if len(writes) == 1:
                raise KeyboardInterrupt()

        with (
            patch.object(jb_inspect, "create_local_lease", return_value=lease),
            patch.object(jb_inspect, "find_exact_route", return_value=None),
            patch.object(jb_inspect, "ensure_trusted_auto_open_root"),
            patch.object(jb_inspect, "ensure_jetbrains_trusted_locations"),
            patch.object(jb_inspect, "open_project_for_lifecycle") as open_project,
            patch.object(jb_inspect, "write_lease", side_effect=interrupt_first_write),
        ):
            with self.assertRaises(KeyboardInterrupt):
                jb_inspect.prepare_lifecycle_details(
                    helper_args(open=True, prepare_timeout_ms=1),
                    {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                )

        open_project.assert_not_called()
        self.assertFalse(lease["opened_by_helper"])
        self.assertEqual(lease["state"], "cleanup_pending")
        self.assertEqual(len(writes), 2)

    def test_prepare_interrupt_after_open_request_attempt_keeps_recoverable_lease(self):
        lease = {"lease_id": "lease-1", "state": "preparing"}
        written = []

        with (
            patch.object(jb_inspect, "create_local_lease", return_value=lease),
            patch.object(jb_inspect, "find_exact_route", return_value=None),
            patch.object(jb_inspect, "ensure_trusted_auto_open_root"),
            patch.object(jb_inspect, "ensure_jetbrains_trusted_locations"),
            patch.object(
                jb_inspect,
                "open_project_for_lifecycle",
                side_effect=KeyboardInterrupt(),
            ),
            patch.object(jb_inspect, "write_lease", side_effect=lambda value: written.append(value.copy())),
        ):
            with self.assertRaises(KeyboardInterrupt):
                jb_inspect.prepare_lifecycle_details(
                    helper_args(open=True, prepare_timeout_ms=1),
                    {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                )

        self.assertFalse(lease["opened_by_helper"])
        self.assertEqual(lease["state"], "cleanup_pending")
        self.assertEqual(lease["preparation_failure_stage"], "project_open")
        self.assertEqual(written[0]["state"], "open_requesting")
        self.assertEqual(written[-1]["state"], "cleanup_pending")

    def test_failed_preparation_close_results_remain_recoverable(self):
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        error = jb_inspect.InspectError("not ready", 3, {"error_reason": "ide_not_ready_timeout"})
        for close_status in ("failed", "skipped"):
            with self.subTest(close_status=close_status):
                lease = {
                    "lease_id": f"lease-{close_status}",
                    "state": "route_resolved",
                    "opened_by_helper": True,
                    "project_instance_id": "session:1",
                    "project_key": "path:/tmp/worktree",
                    "session_id": "session",
                    "lifecycle_target_path": "/tmp/worktree",
                }
                close_result = {"status": close_status, "reason": f"close_{close_status}"}
                with (
                    patch.object(
                        jb_inspect,
                        "reclaim_lifecycle_claim",
                        return_value=(
                            True,
                            "proof-1",
                            {"status": "claimed", "ownership_proven": True},
                            route,
                        ),
                    ),
                    patch.object(jb_inspect, "cleanup_lifecycle", return_value=close_result),
                    patch.object(jb_inspect, "write_lease"),
                ):
                    result = jb_inspect.cleanup_failed_preparation(
                        lease,
                        route,
                        None,
                        error,
                        "readiness_wait",
                    )

                self.assertEqual(result["status"], "deferred")
                self.assertEqual(result["close_result"], close_result)
                self.assertEqual(lease["state"], "cleanup_pending")

    def test_readiness_timeout_preserves_helper_owned_project_during_active_inspection(self):
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        lease = {
            "lease_id": "lease-active",
            "state": "ownership_claimed",
            "opened_by_helper": True,
            "project_instance_id": "session:1",
            "project_key": "path:/tmp/worktree",
            "session_id": "session",
            "lifecycle_target_path": "/tmp/worktree",
        }
        error = jb_inspect.InspectError(
            "not ready",
            3,
            {
                "error_reason": "ide_not_ready_timeout",
                "last_status": {"inspection_in_progress": True, "is_scanning": True, "indexing": False},
            },
        )

        with (
            patch.object(jb_inspect, "cleanup_lifecycle") as cleanup,
            patch.object(jb_inspect, "write_lease"),
        ):
            result = jb_inspect.cleanup_failed_preparation(
                lease,
                route,
                "proof-1",
                error,
                "readiness_wait",
            )

        cleanup.assert_not_called()
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["reason"], "indexing_or_inspection_still_running")
        self.assertEqual(lease["state"], "kept_warm_after_indexing_timeout")

    def test_write_lease_strips_private_fields_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cache = os.environ.get("JETBRAINS_INSPECTION_CACHE_DIR")
            os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = tmp
            try:
                lease = jb_inspect.create_local_lease({"worktree_root": "/tmp/repo"}, "prepared")
                lease["_private_data"] = "private-lease-value"
                jb_inspect.write_lease(lease)
                body = json.loads(jb_inspect.lease_path(lease).read_text(encoding="utf-8"))
            finally:
                if original_cache is None:
                    os.environ.pop("JETBRAINS_INSPECTION_CACHE_DIR", None)
                else:
                    os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = original_cache

            self.assertNotIn("_private_data", body)
            self.assertNotIn("private-lease-value", json.dumps(body))

    def test_claim_creates_local_lease_without_opening_ide(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cache = os.environ.get("JETBRAINS_INSPECTION_CACHE_DIR")
            os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = tmp
            try:
                context = {"repo_path": "/tmp/repo", "worktree_root": "/tmp/repo"}
                result = jb_inspect.command_claim(Namespace(), context)
            finally:
                if original_cache is None:
                    os.environ.pop("JETBRAINS_INSPECTION_CACHE_DIR", None)
                else:
                    os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = original_cache

            self.assertEqual(result["status"], "claimed")
            self.assertEqual(result["lease"]["state"], "claimed")

    def test_cleanup_skips_preexisting_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cache = os.environ.get("JETBRAINS_INSPECTION_CACHE_DIR")
            os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = tmp
            try:
                lease = jb_inspect.create_local_lease({"worktree_root": "/tmp/repo"}, "prepared")
                lease["opened_by_helper"] = False
                result = jb_inspect.cleanup_lifecycle(lease, {"project_key": "path:/tmp/repo"})
            finally:
                if original_cache is None:
                    os.environ.pop("JETBRAINS_INSPECTION_CACHE_DIR", None)
                else:
                    os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = original_cache

            self.assertEqual(result["status"], "not_needed")
            self.assertEqual(result["reason"], "project_preexisted")

    def test_prepare_without_open_reports_open_not_attempted(self):
        lease = {"lease_id": "lease-1", "state": "preparing", "opened_by_helper": False}

        with (
            patch.object(jb_inspect, "create_local_lease", return_value=lease),
            patch.object(jb_inspect, "find_exact_route", return_value=None),
            patch.object(jb_inspect, "write_lease"),
            patch.object(jb_inspect, "remove_lease"),
        ):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.prepare_lifecycle_details(
                    helper_args(open=False),
                    {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                )

        self.assertTrue(lease["open_not_attempted"])
        self.assertEqual(raised.exception.payload["preparation_cleanup"]["status"], "not_needed")
        self.assertEqual(raised.exception.payload["preparation_cleanup"]["reason"], "open_not_attempted")

    def test_closeout_defers_cleanup_when_helper_opened_project_is_still_indexing(self):
        cleanups = []
        states = []
        prepared = {
            "status": "prepared",
            "route": {"port": 1, "project_key": "path:/tmp/worktree", "base_path": "/tmp/worktree"},
            "opened_by_helper": True,
        }
        lease = {
            "opened_by_helper": True,
            "lease_id": "lease-1",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
        }

        def fake_run(args, context, route):
            return {
                "status": "timed_out",
                "verdict": "UNKNOWN",
                "verdict_reason": "timeout",
                "wait": {"timed_out": True, "indexing": True, "inspection_in_progress": True},
            }

        original_prepare = jb_inspect.prepare_lifecycle_details
        original_run = jb_inspect.run_inspection_on_route
        original_cleanup = jb_inspect.cleanup_lifecycle
        original_mark = jb_inspect.mark_lease_state
        jb_inspect.prepare_lifecycle_details = lambda args, context: (prepared, lease, "proof-1")
        jb_inspect.run_inspection_on_route = fake_run
        jb_inspect.cleanup_lifecycle = lambda cleanup_lease, route, close_proof: cleanups.append((cleanup_lease, route, close_proof)) or {"status": "closed"}
        jb_inspect.mark_lease_state = lambda state_lease, state: states.append((state_lease, state))
        try:
            result = jb_inspect.command_closeout(Namespace(keep_warm=False, lifecycle_lock_timeout_ms=0), {})
        finally:
            jb_inspect.prepare_lifecycle_details = original_prepare
            jb_inspect.run_inspection_on_route = original_run
            jb_inspect.cleanup_lifecycle = original_cleanup
            jb_inspect.mark_lease_state = original_mark

        self.assertEqual(cleanups, [])
        self.assertEqual(result["cleanup"]["status"], "deferred")
        self.assertTrue(result["cleanup_deferred"])
        self.assertEqual(states, [(lease, "kept_warm_after_indexing_timeout")])

    def test_prepare_reclaims_deferred_helper_lease_for_exact_route(self):
        removed = []
        written = []
        claimed = []
        created = {
            "lease_id": "new-lease",
            "state": "preparing",
            "lifecycle_target_path": "/tmp/worktree",
        }
        deferred = {
            "lease_id": "old-lease",
            "state": "kept_warm_after_indexing_timeout",
            "opened_by_helper": True,
            "lifecycle_target_path": "/tmp/worktree",
            "worktree_root": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
            "updated_at_ms": 20,
        }
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }

        original_create = jb_inspect.create_local_lease
        original_find = jb_inspect.find_exact_route
        original_matching = jb_inspect.matching_deferred_lease
        original_remove = jb_inspect.remove_lease
        original_wait = jb_inspect.wait_until_route_ready
        original_claim = jb_inspect.claim_lifecycle
        original_write = jb_inspect.write_lease
        jb_inspect.create_local_lease = lambda context, state: created.copy()
        jb_inspect.find_exact_route = lambda args, context: route.copy()
        jb_inspect.matching_deferred_lease = lambda context, exact_route: deferred.copy()
        jb_inspect.remove_lease = lambda lease: removed.append(lease["lease_id"])
        jb_inspect.wait_until_route_ready = lambda args, context, exact_route, timeout_ms: None

        def fake_claim(args, context, exact_route, lease):
            claimed.append(lease["lease_id"])
            return claimed_lifecycle_result(exact_route, "proof-1")

        jb_inspect.claim_lifecycle = fake_claim
        jb_inspect.write_lease = lambda lease: written.append(lease.copy())
        try:
            prepared, lease, close_proof = jb_inspect.prepare_lifecycle_details(
                helper_args(open=True, prepare_timeout_ms=1),
                {"worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
            )
        finally:
            jb_inspect.create_local_lease = original_create
            jb_inspect.find_exact_route = original_find
            jb_inspect.matching_deferred_lease = original_matching
            jb_inspect.remove_lease = original_remove
            jb_inspect.wait_until_route_ready = original_wait
            jb_inspect.claim_lifecycle = original_claim
            jb_inspect.write_lease = original_write

        self.assertEqual(removed, ["new-lease"])
        self.assertEqual(claimed, ["old-lease"])
        self.assertTrue(lease["opened_by_helper"])
        self.assertEqual(lease["open_method"], "reclaimed_deferred")
        self.assertEqual(prepared["open_method"], "reclaimed_deferred")
        self.assertTrue(prepared["opened_by_helper"])
        self.assertEqual(close_proof, "proof-1")
        self.assertEqual(written[-1]["lease_id"], "old-lease")

    def test_deferred_lease_matching_requires_exact_project_session_and_path(self):
        lease = {
            "state": "kept_warm_after_indexing_timeout",
            "opened_by_helper": True,
            "lifecycle_target_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        context = {"worktree_root": "/tmp/worktree"}
        route = {
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }

        self.assertTrue(jb_inspect.deferred_lease_matches_route(lease, context, route))
        self.assertTrue(jb_inspect.deferred_lease_matches_route(lease | {"state": "cleanup_pending"}, context, route))
        self.assertFalse(jb_inspect.deferred_lease_matches_route(lease | {"session_id": "other"}, context, route))
        self.assertFalse(jb_inspect.deferred_lease_matches_route(lease | {"project_instance_id": "session:2"}, context, route))
        self.assertFalse(jb_inspect.deferred_lease_matches_route(lease | {"lifecycle_target_path": "/tmp/other"}, context, route))

    def test_cleanup_leases_closes_stale_helper_owned_matching_route(self):
        removed = []
        closed = []
        claims = []
        lease = {
            "lease_id": "old-lease",
            "state": "kept_warm_after_indexing_timeout",
            "opened_by_helper": True,
            "pid": 999999,
            "lifecycle_target_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }

        original_read = jb_inspect.read_local_leases
        original_pid_alive = jb_inspect.pid_alive
        original_routes = jb_inspect.discover_routes_for_cleanup
        original_private = jb_inspect.private_http_get_body
        original_cleanup = jb_inspect.cleanup_lifecycle
        path = Path("/tmp/old-lease.json")
        jb_inspect.read_local_leases = lambda: [(path, lease.copy())]
        jb_inspect.pid_alive = lambda pid: False
        jb_inspect.discover_routes_for_cleanup = lambda args: ([route.copy()], {"session"})
        jb_inspect.private_http_get_body = lambda port, endpoint, params, timeout=jb_inspect.DEFAULT_TIMEOUT_SECONDS: claims.append((endpoint, params["lease_id"])) or plugin_claim_body(route, "old-lease")
        jb_inspect.cleanup_lifecycle = lambda cleanup_lease, cleanup_route, proof: closed.append((cleanup_lease["lease_id"], cleanup_route["project_instance_id"], proof)) or {"status": "closed"}
        try:
            with patch.object(Path, "unlink", lambda self, missing_ok=False: removed.append(str(self))):
                result = jb_inspect.cleanup_stale_helper_leases(Namespace(max_age_ms=86_400_000, dry_run=False))
        finally:
            jb_inspect.read_local_leases = original_read
            jb_inspect.pid_alive = original_pid_alive
            jb_inspect.discover_routes_for_cleanup = original_routes
            jb_inspect.private_http_get_body = original_private
            jb_inspect.cleanup_lifecycle = original_cleanup

        self.assertEqual(claims, [("lifecycle/claim", "old-lease")])
        self.assertEqual(closed, [("old-lease", "session:1", "proof-1")])
        self.assertEqual(removed, [str(path)])
        self.assertEqual(result["closed"][0]["lease_id"], "old-lease")

    def test_cleanup_command_removes_stale_never_opened_lease_without_route_discovery(self):
        removed = []
        path = Path("/tmp/never-opened-lease.json")
        lease = {
            "lease_id": "never-opened-lease",
            "state": "cleanup_pending",
            "preparation_failure_stage": "project_open",
            "preparation_failure_reason": "timeout",
            "opened_by_helper": False,
            "open_request_may_have_been_accepted": False,
            "open_attempts": [],
            "pid": 999_999,
            "updated_at_ms": jb_inspect.now_ms(),
        }

        with (
            patch.object(jb_inspect, "read_local_leases", return_value=[(path, lease)]),
            patch.object(jb_inspect, "pid_alive", return_value=False),
            patch.object(jb_inspect, "discover_routes_for_cleanup") as discover_routes,
            patch.object(Path, "unlink", lambda self, missing_ok=False: removed.append(str(self))),
        ):
            result = jb_inspect.cleanup_stale_helper_leases(
                Namespace(max_age_ms=86_400_000, dry_run=False)
            )

        discover_routes.assert_not_called()
        self.assertEqual(removed, [str(path)])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["removed"], [str(path)])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["unresolved"], [])

    def test_cleanup_stale_helper_lease_skips_when_reclaim_fails(self):
        lease = {
            "lease_id": "old-lease",
            "opened_by_helper": True,
            "project_instance_id": "session:1",
            "session_id": "session",
            "lifecycle_target_path": "/tmp/worktree",
        }
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        original_private = jb_inspect.private_http_get_body
        jb_inspect.private_http_get_body = lambda *args, **kwargs: (_ for _ in ()).throw(jb_inspect.InspectError("drift", 4))
        try:
            result = jb_inspect.cleanup_stale_helper_lease(lease, [route])
        finally:
            jb_inspect.private_http_get_body = original_private

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "close_failed")

    def test_cleanup_pending_lease_reclaims_matching_exact_identity(self):
        lease = {
            "lease_id": "pending-lease",
            "state": "cleanup_pending",
            "opened_by_helper": True,
            "lifecycle_target_path": "/tmp/worktree",
            "session_id": "session",
            "project_instance_id": "session:1",
        }
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
        }
        cleanup_calls = []

        with (
            patch.object(
                jb_inspect,
                "private_http_get_body",
                return_value=plugin_claim_body(route, "pending-lease"),
            ),
            patch.object(
                jb_inspect,
                "cleanup_lifecycle",
                side_effect=lambda cleanup_lease, cleanup_route, proof: cleanup_calls.append(
                    (cleanup_lease["lease_id"], cleanup_route["project_instance_id"], proof)
                )
                or {"status": "closed"},
            ),
        ):
            result = jb_inspect.cleanup_stale_helper_lease(lease, [route])

        self.assertEqual(cleanup_calls, [("pending-lease", "session:1", "proof-1")])
        self.assertEqual(result["status"], "closed")

    def test_cleanup_pending_lease_does_not_close_matching_path_in_new_session(self):
        lease = {
            "lease_id": "pending-lease",
            "state": "cleanup_pending",
            "opened_by_helper": True,
            "lifecycle_target_path": "/tmp/worktree",
            "session_id": "old-session",
            "project_instance_id": "old-session:1",
        }
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "new-session:1",
            "session_id": "new-session",
        }

        with patch.object(jb_inspect, "cleanup_lifecycle") as cleanup:
            result = jb_inspect.cleanup_stale_helper_lease(lease, [route])

        cleanup.assert_not_called()
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "ownership_route_unavailable")

    def test_cleanup_pending_lease_releases_after_original_ide_process_dies_without_route(self):
        lease = {
            "lease_id": "pending-lease",
            "state": "cleanup_pending",
            "opened_by_helper": True,
            "open_request_may_have_been_accepted": False,
            "lifecycle_target_path": "/tmp/worktree",
            "session_id": "old-session",
            "ide_port": 63343,
            "open_attempts": [
                {
                    "accepted": True,
                    "ownership_registered": True,
                    "lease_id": "pending-lease",
                    "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
                    "identity": {
                        "session_id": "old-session",
                        "port": 63343,
                        "pid": 753,
                    },
                }
            ],
        }

        with patch.object(jb_inspect, "pid_definitively_dead", return_value=True):
            result = jb_inspect.cleanup_stale_helper_lease(lease, [], set())

        self.assertEqual(result["status"], "not_needed")
        self.assertEqual(result["reason"], "original_ide_process_dead_no_route")
        self.assertTrue(result["released_without_close"])

    def test_cleanup_pending_lease_retains_dead_original_session_when_target_is_open_elsewhere(self):
        lease = {
            "lease_id": "pending-lease",
            "state": "cleanup_pending",
            "opened_by_helper": True,
            "open_request_may_have_been_accepted": False,
            "lifecycle_target_path": "/tmp/worktree",
            "session_id": "old-session",
            "ide_port": 63343,
            "open_attempts": [
                {
                    "accepted": True,
                    "ownership_registered": True,
                    "lease_id": "pending-lease",
                    "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
                    "identity": {
                        "session_id": "old-session",
                        "port": 63343,
                        "pid": 753,
                    },
                }
            ],
        }
        route = {
            "base_path": "/tmp/worktree",
            "session_id": "new-session",
            "project_instance_id": "new-session:1",
        }

        with patch.object(jb_inspect, "pid_definitively_dead", return_value=True):
            result = jb_inspect.cleanup_stale_helper_lease(lease, [route], {"new-session"})

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "ownership_route_unavailable")

    def test_cleanup_pending_lease_retains_unknown_or_live_original_ide_process(self):
        lease = {
            "lease_id": "pending-lease",
            "state": "cleanup_pending",
            "opened_by_helper": True,
            "open_request_may_have_been_accepted": False,
            "lifecycle_target_path": "/tmp/worktree",
            "session_id": "old-session",
            "ide_port": 63343,
            "open_attempts": [
                {
                    "accepted": True,
                    "ownership_registered": True,
                    "lease_id": "pending-lease",
                    "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
                    "identity": {
                        "session_id": "old-session",
                        "port": 63343,
                        "pid": 753,
                    },
                }
            ],
        }

        with patch.object(jb_inspect, "pid_definitively_dead", return_value=False):
            result = jb_inspect.cleanup_stale_helper_lease(lease, [], set())

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "ownership_route_unavailable")

    def test_cleanup_pending_lease_retains_ambiguous_or_conflicting_open_attempts(self):
        base_attempt = {
            "accepted": True,
            "ownership_registered": True,
            "lease_id": "pending-lease",
            "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
            "identity": {
                "session_id": "old-session",
                "port": 63343,
                "pid": 753,
            },
        }
        attempts = [
            [base_attempt | {"ownership_registered": False}],
            [base_attempt | {"request_may_have_been_accepted": True}],
            [base_attempt | {"identity": base_attempt["identity"] | {"pid": None}}],
            [base_attempt, base_attempt | {"identity": base_attempt["identity"] | {"pid": 754}}],
        ]

        for open_attempts in attempts:
            lease = {
                "lease_id": "pending-lease",
                "state": "cleanup_pending",
                "opened_by_helper": True,
                "open_request_may_have_been_accepted": False,
                "lifecycle_target_path": "/tmp/worktree",
                "session_id": "old-session",
                "ide_port": 63343,
                "open_attempts": open_attempts,
            }
            with self.subTest(open_attempts=open_attempts), patch.object(
                jb_inspect, "pid_definitively_dead", return_value=True
            ):
                result = jb_inspect.cleanup_stale_helper_lease(lease, [], set())

            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "ownership_route_unavailable")

    def test_cleanup_command_removes_dead_original_ide_lease_without_closing(self):
        removed = []
        path = Path("/tmp/pending-lease.json")
        lease = {
            "lease_id": "pending-lease",
            "state": "cleanup_pending",
            "opened_by_helper": True,
            "open_request_may_have_been_accepted": False,
            "lifecycle_target_path": "/tmp/worktree",
            "session_id": "old-session",
            "ide_port": 63343,
            "pid": 999_999,
            "updated_at_ms": jb_inspect.now_ms(),
            "open_attempts": [
                {
                    "accepted": True,
                    "ownership_registered": True,
                    "lease_id": "pending-lease",
                    "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
                    "identity": {
                        "session_id": "old-session",
                        "port": 63343,
                        "pid": 753,
                    },
                }
            ],
        }

        with (
            patch.object(jb_inspect, "read_local_leases", return_value=[(path, lease)]),
            patch.object(jb_inspect, "pid_alive", return_value=False),
            patch.object(jb_inspect, "pid_definitively_dead", return_value=True),
            patch.object(jb_inspect, "discover_routes_for_cleanup", return_value=([], set())),
            patch.object(jb_inspect, "cleanup_lifecycle") as cleanup,
            patch.object(jb_inspect, "private_http_get_body") as claim,
            patch.object(Path, "unlink", lambda self, missing_ok=False: removed.append(str(self))),
        ):
            result = jb_inspect.cleanup_stale_helper_leases(
                Namespace(max_age_ms=86_400_000, dry_run=False)
            )

        cleanup.assert_not_called()
        claim.assert_not_called()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["removed"], [str(path)])
        self.assertEqual(result["closed"], [])
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(removed, [str(path)])

    def test_pid_definitively_dead_fails_closed_for_unknown_process_state(self):
        with patch.object(jb_inspect.os, "kill", side_effect=ProcessLookupError()):
            self.assertTrue(jb_inspect.pid_definitively_dead(753))
        with patch.object(jb_inspect.os, "kill", side_effect=PermissionError()):
            self.assertFalse(jb_inspect.pid_definitively_dead(753))
        with patch.object(jb_inspect.os, "kill", side_effect=OSError("unknown")):
            self.assertFalse(jb_inspect.pid_definitively_dead(753))
        with patch.object(jb_inspect.os, "kill", return_value=None):
            self.assertFalse(jb_inspect.pid_definitively_dead(753))
        self.assertFalse(jb_inspect.pid_definitively_dead(0))

    def test_cleanup_pending_lease_with_legacy_or_request_identity_stays_fail_closed(self):
        leases = [
            {
                "lease_id": "legacy-lease",
                "state": "cleanup_pending",
                "opened_by_helper": False,
                "lifecycle_target_path": "/tmp/worktree",
            },
            {
                "lease_id": "requesting-lease",
                "state": "cleanup_pending",
                "opened_by_helper": False,
                "open_request_may_have_been_accepted": False,
                "open_attempts": [],
                "session_id": "session",
                "ide_port": 63342,
                "lifecycle_target_path": "/tmp/worktree",
            },
            {
                "lease_id": "ambiguous-lease",
                "state": "cleanup_pending",
                "opened_by_helper": False,
                "open_request_may_have_been_accepted": True,
                "open_attempts": [],
                "lifecycle_target_path": "/tmp/worktree",
            },
        ]

        for lease in leases:
            with self.subTest(lease_id=lease["lease_id"]):
                result = jb_inspect.cleanup_stale_helper_lease(lease, [], set())

                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["reason"], "ownership_route_unavailable")

    def test_cleanup_pending_without_project_identity_never_closes_same_session_route(self):
        lease = {
            "lease_id": "pending-lease",
            "state": "cleanup_pending",
            "opened_by_helper": True,
            "lifecycle_target_path": "/tmp/worktree",
            "session_id": "session",
        }
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:manual",
            "session_id": "session",
        }

        with (
            patch.object(
                jb_inspect,
                "private_http_get_body",
                return_value=plugin_claim_body(route, "pending-lease", owned=False),
            ),
            patch.object(jb_inspect, "cleanup_lifecycle") as cleanup,
        ):
            result = jb_inspect.cleanup_stale_helper_lease(lease, [route])

        cleanup.assert_not_called()
        self.assertEqual(result["status"], "not_needed")
        self.assertEqual(result["reason"], "ownership_not_proven")

    def test_cleanup_route_less_pending_lease_closes_only_after_live_ownership_proof(self):
        lease = {
            "lease_id": "pending-lease",
            "state": "cleanup_pending",
            "opened_by_helper": False,
            "lifecycle_target_path": "/tmp/worktree",
            "session_id": "session",
        }
        route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:owned",
            "session_id": "session",
        }
        cleanup_calls = []

        with (
            patch.object(
                jb_inspect,
                "private_http_get_body",
                return_value=plugin_claim_body(route, "pending-lease"),
            ),
            patch.object(jb_inspect, "write_lease"),
            patch.object(
                jb_inspect,
                "cleanup_lifecycle",
                side_effect=lambda cleanup_lease, cleanup_route, proof: cleanup_calls.append(
                    (cleanup_lease["opened_by_helper"], cleanup_route["project_instance_id"], proof)
                )
                or {"status": "closed"},
            ),
        ):
            result = jb_inspect.cleanup_stale_helper_lease(lease, [route], {"session"})

        self.assertEqual(cleanup_calls, [(True, "session:owned", "proof-1")])
        self.assertEqual(result["status"], "closed")
        self.assertEqual(lease["project_instance_id"], "session:owned")

    def test_cleanup_command_retains_unresolved_route_less_lease(self):
        path = Path("/tmp/pending-lease.json")
        lease = {
            "lease_id": "pending-lease",
            "state": "cleanup_pending",
            "opened_by_helper": True,
            "lifecycle_target_path": "/tmp/worktree",
            "session_id": "session",
            "pid": 999_999,
            "updated_at_ms": jb_inspect.now_ms(),
        }
        delayed_route = {
            "port": 1,
            "base_path": "/tmp/worktree",
            "project_key": "path:/tmp/worktree",
            "project_instance_id": "session:late",
            "session_id": "session",
        }

        with (
            patch.object(jb_inspect, "read_local_leases", return_value=[(path, lease)]),
            patch.object(jb_inspect, "pid_alive", return_value=False),
            patch.object(jb_inspect, "discover_routes_for_cleanup", return_value=([delayed_route], {"session"})),
            patch.object(
                jb_inspect,
                "private_http_get_body",
                return_value={"status": "claimed", "lease_id": "pending-lease", "route": delayed_route},
            ),
            patch.object(Path, "unlink") as unlink,
        ):
            result = jb_inspect.cleanup_stale_helper_leases(
                Namespace(max_age_ms=86_400_000, dry_run=False)
            )

        unlink.assert_not_called()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["unresolved"][0]["reason"], "ownership_unresolved")
        self.assertEqual(jb_inspect.classify_cleanup_leases_exit(result), 1)

    def test_cleanup_command_retains_leases_when_route_discovery_fails(self):
        path = Path("/tmp/pending-lease.json")
        lease = {
            "lease_id": "pending-lease",
            "state": "cleanup_pending",
            "opened_by_helper": True,
            "lifecycle_target_path": "/tmp/worktree",
            "project_instance_id": "session:1",
            "session_id": "session",
            "pid": 999_999,
            "updated_at_ms": jb_inspect.now_ms(),
        }

        with (
            patch.object(jb_inspect, "read_local_leases", return_value=[(path, lease)]),
            patch.object(jb_inspect, "pid_alive", return_value=False),
            patch.object(
                jb_inspect,
                "discover_routes_for_cleanup",
                side_effect=jb_inspect.InspectError("registry unavailable", 3),
            ),
            patch.object(Path, "unlink") as unlink,
        ):
            result = jb_inspect.cleanup_stale_helper_leases(
                Namespace(max_age_ms=86_400_000, dry_run=False)
            )

        unlink.assert_not_called()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["failed"][0]["reason"], "route_discovery_failed")

    def test_cleanup_discovery_adds_identity_session_to_project_routes(self):
        identity = {
            "port": 63342,
            "session_id": "session",
            "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
            "open_projects": [
                {
                    "base_path": "/tmp/worktree",
                    "project_instance_id": "session:1",
                }
            ],
        }

        with patch.object(jb_inspect, "discover_identities", return_value=[identity]):
            routes, sessions = jb_inspect.discover_routes_for_cleanup(Namespace(port=None))

        self.assertEqual(sessions, {"session"})
        self.assertEqual(routes[0]["session_id"], "session")
        self.assertEqual(
            routes[0]["ide"]["lifecycle_ownership_protocol"],
            jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
        )

    def test_cleanup_leases_uses_lifecycle_lock(self):
        events = []

        class FakeLock:
            def __enter__(self):
                events.append("enter")

            def __exit__(self, exc_type, exc, traceback):
                events.append("exit")

        with (
            patch.object(
                jb_inspect,
                "lifecycle_lock",
                side_effect=lambda timeout_ms: events.append(("timeout", timeout_ms)) or FakeLock(),
            ),
            patch.object(jb_inspect, "cleanup_stale_helper_leases", return_value={"status": "ok", "removed": []}),
        ):
            result = jb_inspect.command_cleanup_leases(
                Namespace(max_age_ms=1, dry_run=True, lifecycle_lock_timeout_ms=1234)
            )

        self.assertEqual(result, {"status": "ok", "removed": []})
        self.assertEqual(events, [("timeout", 1234), "enter", "exit"])

    def test_lifecycle_close_preserves_failed_close_attempt_diagnostics(self):
        original_private = jb_inspect.private_http_get_body
        jb_inspect.private_http_get_body = lambda port, endpoint, params, timeout=None: {
            "status": "skipped",
            "reason": "close_failed",
            "message": "declined",
            "close_attempts": [{"attempt": 1, "force_close_returned": False}],
        }
        try:
            result = jb_inspect.call_lifecycle_close({"port": 1}, {"project_key": "path:/tmp/worktree"})
        finally:
            jb_inspect.private_http_get_body = original_private

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "close_failed")
        self.assertEqual(result["message"], "declined")
        self.assertEqual(result["close_attempts"], [{"attempt": 1, "force_close_returned": False}])

    def test_lifecycle_close_preserves_success_identifiers(self):
        payload = {
            "status": "closed",
            "project_instance_id": "session:1",
            "project_key": "path:/tmp/worktree",
            "lease_id": "lease-1",
            "session_id": "session",
            "closed_at_ms": 1234,
            "close_attempts": [{"attempt": 1, "closed_verified": True}],
        }
        with patch.object(jb_inspect, "private_http_get_body", return_value=payload):
            result = jb_inspect.call_lifecycle_close({"port": 1}, {"project_key": "path:/tmp/worktree"})

        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["project_instance_id"], "session:1")
        self.assertEqual(result["project_key"], "path:/tmp/worktree")
        self.assertEqual(result["lease_id"], "lease-1")
        self.assertEqual(result["session_id"], "session")
        self.assertEqual(result["closed_at_ms"], 1234)
        self.assertEqual(result["close_attempts"], [{"attempt": 1, "closed_verified": True}])

    def test_lifecycle_close_preserves_http_conflict_payload(self):
        payload = {
            "status": "skipped",
            "reason": "close_failed",
            "message": "declined",
            "close_attempts": [{"attempt": 1, "force_close_returned": False}],
        }
        original_private = jb_inspect.private_http_get_body
        jb_inspect.private_http_get_body = lambda *args, **kwargs: (_ for _ in ()).throw(jb_inspect.InspectError("HTTP 409", 3, payload))
        try:
            result = jb_inspect.call_lifecycle_close({"port": 1}, {"project_key": "path:/tmp/worktree"})
        finally:
            jb_inspect.private_http_get_body = original_private

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "close_failed")
        self.assertEqual(result["close_attempts"], [{"attempt": 1, "force_close_returned": False}])

    def test_cleanup_leases_preserves_lease_when_close_fails(self):
        removed = []
        lease = {
            "lease_id": "old-lease",
            "state": "kept_warm_after_indexing_timeout",
            "opened_by_helper": True,
            "pid": 999999,
            "lifecycle_target_path": "/tmp/worktree",
            "project_instance_id": "session:1",
        }
        path = Path("/tmp/old-lease.json")

        original_read = jb_inspect.read_local_leases
        original_pid_alive = jb_inspect.pid_alive
        original_routes = jb_inspect.discover_routes_for_cleanup
        original_cleanup = jb_inspect.cleanup_stale_helper_lease
        jb_inspect.read_local_leases = lambda: [(path, lease.copy())]
        jb_inspect.pid_alive = lambda pid: False
        jb_inspect.discover_routes_for_cleanup = lambda args: (
            [{"port": 1, "base_path": "/tmp/worktree", "project_instance_id": "session:1", "session_id": "session"}],
            {"session"},
        )
        jb_inspect.cleanup_stale_helper_lease = lambda cleanup_lease, routes, observed_sessions=None: {"status": "failed", "reason": "close_failed", "lease_id": cleanup_lease["lease_id"]}
        try:
            with patch.object(Path, "unlink", lambda self, missing_ok=False: removed.append(str(self))):
                result = jb_inspect.cleanup_stale_helper_leases(Namespace(max_age_ms=86_400_000, dry_run=False))
        finally:
            jb_inspect.read_local_leases = original_read
            jb_inspect.pid_alive = original_pid_alive
            jb_inspect.discover_routes_for_cleanup = original_routes
            jb_inspect.cleanup_stale_helper_lease = original_cleanup

        self.assertEqual(removed, [])
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["failed"][0]["lease_id"], "old-lease")

    def test_problems_params_preserve_files_scope_selectors(self):
        args = helper_args(
            scope="files",
            files=["src/App.kt", "src/AppTest.kt"],
            directory=None,
            max_files=25,
            severity="all",
            problem_type="all",
            file_pattern="all",
            limit=100,
            offset=0,
            include_stale=False,
            project_key=None,
            project_path=None,
            worktree_path=None,
            cwd=None,
            project=None,
            session_id=None,
        )
        params = jb_inspect.problems_params(args, {"scope": "files"}, {"project_key": "path:/tmp/repo"})

        self.assertEqual(params["scope"], "files")
        self.assertEqual(params["files"], "src/App.kt\nsrc/AppTest.kt")
        self.assertEqual(params["max_files"], 25)

    def test_find_exact_route_returns_none_for_containing_project(self):
        original_resolve_route = jb_inspect.resolve_route
        jb_inspect.resolve_route = lambda args, context: {"base_path": "/tmp/repo"}
        try:
            route = jb_inspect.find_exact_route(
                Namespace(no_worktree_check=False, open=False),
                {"worktree_root": "/tmp/repo/packages/app"},
            )
        finally:
            jb_inspect.resolve_route = original_resolve_route

        self.assertIsNone(route)

    def test_closeout_runs_inspection_on_prepared_route(self):
        calls = []

        def fake_prepare(args, context):
            return {
                "status": "prepared",
                "route": {"port": 1, "project_key": "path:/tmp/worktree", "base_path": "/tmp/worktree"},
                "lease": {"opened_by_helper": False},
                "_lease": {"opened_by_helper": False},
            }

        def fake_run(args, context, route):
            calls.append(route)
            return {"status": "clean", "clean": True, "route": route}

        original_prepare = jb_inspect.prepare_lifecycle_details
        original_run = jb_inspect.run_inspection_on_route
        jb_inspect.prepare_lifecycle_details = lambda args, context: (fake_prepare(args, context), {"opened_by_helper": False}, None)
        jb_inspect.run_inspection_on_route = fake_run
        try:
            result = jb_inspect.command_closeout(Namespace(keep_warm=True), {})
        finally:
            jb_inspect.prepare_lifecycle_details = original_prepare
            jb_inspect.run_inspection_on_route = original_run

        self.assertEqual(result["status"], "clean")
        self.assertEqual(calls, [{"port": 1, "project_key": "path:/tmp/worktree", "base_path": "/tmp/worktree"}])
        self.assertNotIn("_lease", result["prepared"])

    def test_keep_warm_marks_helper_opened_project_as_not_cleanup_clean(self):
        route = {"port": 1, "project_key": "path:/tmp/worktree", "base_path": "/tmp/worktree"}
        lease = {"lease_id": "lease-1", "opened_by_helper": True}
        original_prepare = jb_inspect.prepare_lifecycle_details
        original_run = jb_inspect.run_inspection_on_route
        original_cleanup = jb_inspect.cleanup_lifecycle
        jb_inspect.prepare_lifecycle_details = lambda args, context: ({"status": "prepared", "route": route}, lease, None)
        jb_inspect.run_inspection_on_route = lambda args, context, active_route: {
            "status": "clean",
            "clean": True,
            "route": active_route,
        }
        jb_inspect.cleanup_lifecycle = lambda *args: self.fail("keep-warm must not close the helper-opened project")
        try:
            result = jb_inspect.command_closeout(Namespace(keep_warm=True), {})
        finally:
            jb_inspect.prepare_lifecycle_details = original_prepare
            jb_inspect.run_inspection_on_route = original_run
            jb_inspect.cleanup_lifecycle = original_cleanup

        self.assertEqual(result["verdict"], "GREEN")
        self.assertEqual(
            result["cleanup"],
            {"status": "kept_warm", "reason": "keep_warm_requested", "lease_id": "lease-1"},
        )
        self.assertFalse(result.get("cleanup_failed", False))

    def test_closeout_retries_retryable_unknown_once_before_cleanup(self):
        calls = []
        route = {"port": 1, "project_key": "path:/tmp/worktree", "base_path": "/tmp/worktree"}
        readiness = {"status": "ready", "ready": True, "exit_reason": "ready"}

        def fake_run(args, context, active_route):
            calls.append(active_route)
            if len(calls) == 1:
                return {
                    "status": "stale_results",
                    "verdict": "UNKNOWN",
                    "verdict_reason": "stale_results",
                    "bucket": "stale_results",
                    "retry_policy": {"retry": True, "max_attempts": 1, "wait_ms": 30000},
                    "cached_total_problems": 2,
                    "wait": {"completion_reason": "stale_results"},
                }
            return {
                "status": "findings",
                "verdict": "RED",
                "verdict_reason": "actionable_findings",
                "bucket": "actionable_findings",
                "total_problems": 1,
                "problems": [{"description": "real problem"}],
                "route": active_route,
            }

        original_prepare = jb_inspect.prepare_lifecycle_details
        original_run = jb_inspect.run_inspection_on_route
        original_readiness = jb_inspect.wait_for_internal_retry_readiness
        jb_inspect.prepare_lifecycle_details = lambda args, context: ({"status": "prepared", "route": route}, {"opened_by_helper": False}, None)
        jb_inspect.run_inspection_on_route = fake_run
        jb_inspect.wait_for_internal_retry_readiness = lambda args, context, active_route, first_result: readiness
        try:
            result = jb_inspect.command_closeout(Namespace(keep_warm=True), {})
        finally:
            jb_inspect.prepare_lifecycle_details = original_prepare
            jb_inspect.run_inspection_on_route = original_run
            jb_inspect.wait_for_internal_retry_readiness = original_readiness

        self.assertEqual(result["verdict"], "RED")
        self.assertEqual(result["total_problems"], 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["internal_retry_count"], 1)
        self.assertEqual(result["internal_retry_readiness"], readiness)
        self.assertTrue(result["recovered_from_unknown"])
        self.assertEqual(result["internal_retries"][0]["verdict_reason"], "stale_results")

    def test_closeout_does_not_internally_retry_full_inspection_timeout(self):
        calls = []
        timed_out = {
            "status": "timed_out",
            "verdict": "UNKNOWN",
            "verdict_reason": "timeout",
            "bucket": "ide_not_ready",
            "retry_policy": {"retry": True, "max_attempts": 1, "wait_ms": 30000},
            "wait": {"timed_out": True, "inspection_in_progress": True},
        }

        with patch.object(
            jb_inspect,
            "run_inspection_on_route",
            side_effect=lambda args, context, route: calls.append(route) or timed_out.copy(),
        ):
            result = jb_inspect.run_inspection_with_internal_retry(
                Namespace(),
                {},
                {"port": 63342, "project_key": "path:/tmp/worktree"},
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["verdict_reason"], "timeout")
        self.assertNotIn("internal_retry_count", result)

    def test_exhausted_internal_retry_is_not_advertised_again(self):
        attempts = iter(
            [
                {
                    "status": "stale_results",
                    "verdict": "UNKNOWN",
                    "verdict_reason": "stale_results",
                    "bucket": "stale_results",
                    "retry_policy": {"retry": True, "max_attempts": 1, "wait_ms": 0},
                },
                {
                    "status": "stale_results",
                    "verdict": "UNKNOWN",
                    "verdict_reason": "stale_results",
                    "bucket": "stale_results",
                    "retry_policy": {"retry": True, "max_attempts": 1, "wait_ms": 0},
                },
            ]
        )

        with (
            patch.object(jb_inspect, "run_inspection_on_route", side_effect=lambda *args: next(attempts)),
            patch.object(
                jb_inspect,
                "wait_for_internal_retry_readiness",
                return_value={"status": "ready", "ready": True, "exit_reason": "ready"},
            ),
        ):
            result = jb_inspect.run_inspection_with_internal_retry(Namespace(), {}, {"port": 63342})
        jb_inspect.apply_verdict(result)

        self.assertTrue(result["retry_exhausted"])
        self.assertFalse(result["retry_policy"]["retry"])
        self.assertEqual(result["retry_policy"]["max_attempts"], 0)
        self.assertIn("Do not attribute it to source edits", result["agent_result"]["next_action"])

    def test_internal_retry_treats_prior_stale_result_as_idle_but_resets_on_indexing(self):
        statuses = iter(
            [
                {"status": "indexing", "indexing": True},
                {"status": "stale_results", "results_may_be_stale": True},
                {"status": "indexing", "indexing": True},
                {"status": "stale_results", "results_may_be_stale": True},
                {"status": "stale_results", "results_may_be_stale": True},
                {"status": "stale_results", "results_may_be_stale": True},
            ]
        )
        times = iter([0, 0, 0, 1_000, 1_000, 2_000, 2_000, 3_000, 3_000, 4_000, 4_000, 5_000, 5_000])
        first_result = {
            "verdict_reason": "stale_results",
            "bucket": "stale_results",
            "retry_policy": {"wait_ms": 0},
        }

        with (
            patch.object(jb_inspect, "call_endpoint", side_effect=lambda route, endpoint, params: next(statuses)),
            patch.object(jb_inspect, "monotonic_ms", side_effect=lambda: next(times)),
            patch.object(jb_inspect.time, "sleep"),
        ):
            readiness = jb_inspect.wait_for_internal_retry_readiness(
                helper_args(poll_ms=1_000),
                {"worktree_root": "/tmp/worktree"},
                {"port": 63342, "project_key": "path:/tmp/worktree"},
                first_result,
            )

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(readiness["sample_count"], 6)
        self.assertEqual(readiness["stable_observations"], 3)
        self.assertEqual(readiness["first_status"]["status"], "indexing")
        self.assertEqual(readiness["last_status"]["status"], "stale_results")
        self.assertEqual(readiness["same_worktree_writer_observation"], "not_available")

    def test_internal_retry_skips_when_readiness_is_not_proven(self):
        calls = []
        stale_result = {
            "status": "stale_results",
            "verdict": "UNKNOWN",
            "verdict_reason": "stale_results",
            "bucket": "stale_results",
            "retry_policy": {"retry": True, "max_attempts": 1, "wait_ms": 30_000},
            "results_may_be_stale": True,
            "stale_reasons": ["project_changed_since_inspection"],
        }
        readiness = {
            "status": "timeout",
            "ready": False,
            "exit_reason": "indexing",
            "observation_scope": "ide_status_only",
            "same_worktree_writer_observation": "not_available",
        }

        with (
            patch.object(
                jb_inspect,
                "run_inspection_on_route",
                side_effect=lambda *args: calls.append(args) or stale_result.copy(),
            ),
            patch.object(jb_inspect, "wait_for_internal_retry_readiness", return_value=readiness),
        ):
            result = jb_inspect.run_inspection_with_internal_retry(Namespace(), {}, {"port": 63342})
        jb_inspect.apply_verdict(result)

        self.assertEqual(len(calls), 1)
        self.assertTrue(result["internal_retry_skipped"])
        self.assertEqual(result["internal_retry_count"], 0)
        self.assertNotIn("internal_retries", result)
        self.assertTrue(result["retry_exhausted"])
        self.assertFalse(result["retry_policy"]["retry"])
        self.assertIn("withheld its internal retry", result["agent_result"]["next_action"])
        self.assertIn("does not identify", result["unknown_diagnosis"]["not_proven"])
        self.assertEqual(result["unknown_diagnosis"]["readiness_barrier_exit_reason"], "indexing")
        self.assertEqual(len(jb_inspect.ordered_internal_attempts(result)), 1)
        record = jb_inspect.unknown_log_record(result)
        self.assertTrue(record["internal_retry_skipped"])
        self.assertEqual(record["internal_retry_readiness"]["exit_reason"], "indexing")
        self.assertEqual(record["unknown_diagnosis"]["classification"], "inspection_state_changed")

    def test_internal_retry_readiness_timeout_is_bounded(self):
        times = iter([0, 0, 0, 1_000, 1_000, 2_000, 2_000, 3_000, 3_000, 4_000, 4_000])
        first_result = {
            "verdict_reason": "stale_results",
            "bucket": "stale_results",
            "retry_policy": {"wait_ms": 0},
        }

        with (
            patch.object(jb_inspect, "INTERNAL_RETRY_READY_TIMEOUT_MS", 2_000),
            patch.object(jb_inspect, "call_endpoint", return_value={"status": "indexing", "indexing": True}),
            patch.object(jb_inspect, "monotonic_ms", side_effect=lambda: next(times)),
            patch.object(jb_inspect.time, "sleep"),
        ):
            readiness = jb_inspect.wait_for_internal_retry_readiness(
                helper_args(poll_ms=1_000),
                {"worktree_root": "/tmp/worktree"},
                {"port": 63342, "project_key": "path:/tmp/worktree"},
                first_result,
            )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "timeout")
        self.assertEqual(readiness["exit_reason"], "indexing")
        self.assertEqual(readiness["sample_count"], 4)
        self.assertEqual(readiness["timeout_ms"], 3_000)

    def test_internal_retry_readiness_probe_error_fails_closed(self):
        first_result = {
            "verdict_reason": "stale_results",
            "bucket": "stale_results",
            "retry_policy": {"wait_ms": 30_000},
        }
        error = jb_inspect.InspectError(
            "status unavailable",
            3,
            {"error_reason": "inspection_api_unavailable"},
        )

        with (
            patch.object(jb_inspect, "call_endpoint", side_effect=error),
            patch.object(jb_inspect, "monotonic_ms", side_effect=[0, 0, 0]),
        ):
            readiness = jb_inspect.wait_for_internal_retry_readiness(
                helper_args(poll_ms=1_000),
                {"worktree_root": "/tmp/worktree"},
                {"port": 63342, "project_key": "path:/tmp/worktree"},
                first_result,
            )

        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["status"], "unavailable")
        self.assertEqual(readiness["exit_reason"], "inspection_api_unavailable")
        self.assertEqual(readiness["sample_count"], 0)

    def test_timed_out_inspection_requests_cancellation_and_waits_for_settlement(self):
        cancel_params = []
        statuses = iter(
            [
                {"inspection_in_progress": True, "inspection_run_id": 7, "is_scanning": True},
                {"inspection_in_progress": False, "inspection_run_id": 7, "is_scanning": False, "indexing": False},
            ]
        )

        def fake_call(route, endpoint, params, timeout=None):
            if endpoint == "cancel":
                cancel_params.append(params)
                return {"status": "cancel_requested", "inspection_cancellation_requested": True}
            if endpoint == "status":
                return next(statuses)
            self.fail(f"unexpected endpoint: {endpoint}")

        with (
            patch.object(jb_inspect, "call_endpoint", side_effect=fake_call),
            patch.object(jb_inspect, "now_ms", side_effect=[0, 0, 1000]),
            patch.object(jb_inspect.time, "sleep"),
        ):
            result = jb_inspect.cancel_timed_out_inspection(
                Namespace(project_key=None, project_path=None, worktree_path=None, cwd=None, project=None, ide=None, session_id=None),
                {"worktree_root": "/tmp/worktree"},
                {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"},
                {"timed_out": True, "inspection_in_progress": True, "inspection_run_id": 7},
            )

        self.assertEqual(result["status"], "settled")
        self.assertTrue(result["requested"])
        self.assertTrue(result["settled"])
        self.assertFalse(result["last_status"]["inspection_in_progress"])
        self.assertEqual(cancel_params[0]["inspection_run_id"], 7)

    def test_cancellation_refuses_to_cancel_a_changed_run(self):
        calls = []

        def fake_call(route, endpoint, params, timeout=None):
            calls.append((endpoint, params))
            self.assertEqual(endpoint, "cancel")
            return {
                "status": "run_changed",
                "inspection_run_id": 8,
                "expected_inspection_run_id": 7,
            }

        with patch.object(jb_inspect, "call_endpoint", side_effect=fake_call):
            result = jb_inspect.cancel_timed_out_inspection(
                helper_args(),
                {"worktree_root": "/tmp/worktree"},
                {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"},
                {"timed_out": True, "inspection_in_progress": True, "inspection_run_id": 7},
            )

        self.assertEqual(result["status"], "run_changed")
        self.assertFalse(result["requested"])
        self.assertFalse(result["settled"])
        self.assertEqual(calls[0][1]["inspection_run_id"], 7)

    def test_wait_run_change_never_cancels_or_reads_replacement_run(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}
        calls = []

        def fake_call(active_route, endpoint, params, timeout=None):
            calls.append((endpoint, params))
            if endpoint == "trigger":
                return {"status": "triggered", "run_id": 7, "route": route}
            if endpoint == "wait":
                return {
                    "status": "run_changed",
                    "inspection_run_id": 8,
                    "expected_inspection_run_id": 7,
                    "inspection_in_progress": True,
                }
            self.fail(f"unexpected endpoint: {endpoint}")

        with patch.object(jb_inspect, "call_endpoint", side_effect=fake_call):
            result = jb_inspect.run_inspection_on_route(
                helper_args(timeout_ms=1000, poll_ms=1),
                {"worktree_root": "/tmp/worktree"},
                route,
            )

        self.assertEqual([endpoint for endpoint, _ in calls], ["trigger", "wait"])
        self.assertEqual(calls[1][1]["inspection_run_id"], 7)
        self.assertEqual(result["status"], "run_changed")
        self.assertEqual(result["expected_inspection_run_id"], 7)
        self.assertEqual(result["inspection_run_id"], 8)
        self.assertTrue(result["transport_state_unknown"])
        self.assertEqual(result["verdict_reason"], "run_changed")
        self.assertTrue(jb_inspect.should_defer_lifecycle_cleanup(result, {"opened_by_helper": True}))

    def test_unproven_wait_run_change_continues_with_the_accepted_run(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}
        calls = []

        def fake_call(active_route, endpoint, params, timeout=None):
            calls.append((endpoint, params))
            if endpoint == "trigger":
                return {"status": "triggered", "run_id": 7, "route": route}
            if endpoint == "wait":
                return {
                    "status": "run_changed",
                    "expected_inspection_run_id": 7,
                    "inspection_run_id": 7,
                    "inspection_triggered": True,
                    "inspection_in_progress": False,
                    "wait_completed": False,
                }
            if endpoint == "problems":
                return {
                    "status": "results_available",
                    "inspection_run_id": 7,
                    "snapshot_run_id": 7,
                    "total_problems": 1,
                    "problems": [{"description": "accepted run finding", "severity": "warning"}],
                }
            self.fail(f"unexpected endpoint: {endpoint}")

        with patch.object(jb_inspect, "call_endpoint", side_effect=fake_call):
            result = jb_inspect.run_inspection_on_route(
                helper_args(timeout_ms=1000, poll_ms=1),
                {"worktree_root": "/tmp/worktree"},
                route,
            )

        self.assertEqual([endpoint for endpoint, _ in calls], ["trigger", "wait", "problems"])
        self.assertEqual(result["status"], "findings")
        self.assertEqual(result["verdict"], "RED")
        self.assertEqual(result["verdict_reason"], "actionable_findings")
        self.assertNotEqual(result.get("error_reason"), "run_changed")

    def test_wait_rejects_snapshot_from_an_older_run(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}
        calls = []

        def fake_call(active_route, endpoint, params, timeout=None):
            calls.append((endpoint, params))
            if endpoint == "trigger":
                return {"status": "triggered", "run_id": 7, "route": route}
            if endpoint == "wait":
                return {
                    "status": "clean",
                    "wait_completed": True,
                    "inspection_in_progress": False,
                    "inspection_run_id": 7,
                    "snapshot_run_id": 6,
                    "clean_inspection": True,
                }
            self.fail(f"unexpected endpoint: {endpoint}")

        with patch.object(jb_inspect, "call_endpoint", side_effect=fake_call):
            result = jb_inspect.run_inspection_on_route(
                helper_args(timeout_ms=1000, poll_ms=1),
                {"worktree_root": "/tmp/worktree"},
                route,
            )

        self.assertEqual([endpoint for endpoint, _ in calls], ["trigger", "wait"])
        self.assertEqual(result["status"], "run_changed")
        self.assertEqual(result["expected_inspection_run_id"], 7)
        self.assertEqual(result["inspection_run_id"], 7)
        self.assertEqual(result["wait"]["snapshot_run_id"], 6)
        self.assertTrue(result["transport_state_unknown"])

    def test_completed_replacement_run_is_not_reported_as_the_accepted_run(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}
        calls = []

        def fake_call(active_route, endpoint, params, timeout=None):
            calls.append((endpoint, params))
            if endpoint == "trigger":
                return {"status": "triggered", "run_id": 7, "route": route}
            if endpoint == "wait":
                return {
                    "status": "results_available",
                    "wait_completed": True,
                    "inspection_in_progress": False,
                    "inspection_run_id": 7,
                    "snapshot_run_id": 7,
                }
            if endpoint == "problems":
                return {
                    "status": "results_available",
                    "inspection_in_progress": False,
                    "inspection_run_id": 8,
                    "snapshot_run_id": 8,
                    "total_problems": 0,
                    "problems": [],
                }
            self.fail(f"unexpected endpoint: {endpoint}")

        with patch.object(jb_inspect, "call_endpoint", side_effect=fake_call):
            result = jb_inspect.run_inspection_on_route(
                helper_args(timeout_ms=1000, poll_ms=1),
                {"worktree_root": "/tmp/worktree"},
                route,
            )

        self.assertEqual(result["status"], "run_changed")
        self.assertEqual(result["expected_inspection_run_id"], 7)
        self.assertEqual(result["inspection_run_id"], 8)
        self.assertTrue(result["transport_state_unknown"])
        problems_call = next(params for endpoint, params in calls if endpoint == "problems")
        self.assertEqual(problems_call["inspection_run_id"], 7)

    def test_active_accepted_run_keeps_prior_snapshot_as_stale(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}
        calls = []

        def fake_call(active_route, endpoint, params, timeout=None):
            calls.append((endpoint, params))
            if endpoint == "trigger":
                return {"status": "triggered", "run_id": 7, "route": route}
            if endpoint == "wait":
                return {
                    "status": "timed_out",
                    "timed_out": True,
                    "inspection_in_progress": True,
                    "inspection_run_id": 7,
                }
            if endpoint == "problems":
                return {
                    "status": "inspection_in_progress",
                    "inspection_in_progress": True,
                    "inspection_run_id": 7,
                    "snapshot_run_id": 6,
                    "total_problems": 0,
                    "problems": [],
                }
            self.fail(f"unexpected endpoint: {endpoint}")

        with (
            patch.object(jb_inspect, "call_endpoint", side_effect=fake_call),
            patch.object(
                jb_inspect,
                "cancel_timed_out_inspection",
                return_value={"status": "settlement_timeout", "requested": True, "settled": False},
            ),
        ):
            result = jb_inspect.run_inspection_on_route(
                helper_args(timeout_ms=1000, poll_ms=1),
                {"worktree_root": "/tmp/worktree"},
                route,
            )

        self.assertEqual([endpoint for endpoint, _ in calls], ["trigger", "wait", "problems"])
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertEqual(result["verdict_reason"], "timeout")
        self.assertNotEqual(result.get("error_reason"), "run_changed")

    def test_cancellation_settlement_rejects_completed_replacement_run(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}

        def fake_call(active_route, endpoint, params, timeout=None):
            if endpoint == "cancel":
                return {
                    "status": "cancel_requested",
                    "inspection_cancellation_requested": True,
                    "inspection_run_id": 7,
                }
            if endpoint == "status":
                return {
                    "status": "results_available",
                    "inspection_in_progress": False,
                    "inspection_run_id": 8,
                }
            self.fail(f"unexpected endpoint: {endpoint}")

        with patch.object(jb_inspect, "call_endpoint", side_effect=fake_call):
            result = jb_inspect.cancel_timed_out_inspection(
                helper_args(),
                {"worktree_root": "/tmp/worktree"},
                route,
                {"timed_out": True, "inspection_in_progress": True, "inspection_run_id": 7},
                expected_run_id=7,
            )

        self.assertEqual(result["status"], "run_changed")
        self.assertFalse(result["settled"])
        self.assertEqual(result["expected_inspection_run_id"], 7)
        self.assertEqual(result["inspection_run_id"], 8)

    def test_problems_transport_failure_defers_cleanup_for_unsettled_run(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}

        def fake_call(active_route, endpoint, params, timeout=None):
            if endpoint == "trigger":
                return {"status": "triggered", "run_id": 7, "route": route}
            if endpoint == "wait":
                return {
                    "status": "timed_out",
                    "timed_out": True,
                    "inspection_in_progress": True,
                    "inspection_run_id": 7,
                }
            if endpoint == "problems":
                raise jb_inspect.InspectError(
                    "Inspection API timed out on port 63342: timed out",
                    3,
                    {"error_reason": "inspection_api_timeout", "endpoint": "problems", "port": 63342},
                )
            self.fail(f"unexpected endpoint: {endpoint}")

        with (
            patch.object(jb_inspect, "call_endpoint", side_effect=fake_call),
            patch.object(
                jb_inspect,
                "cancel_timed_out_inspection",
                return_value={"status": "settlement_timeout", "requested": True, "settled": False},
            ),
        ):
            result = jb_inspect.run_inspection_on_route(
                helper_args(timeout_ms=1000, poll_ms=1),
                {"worktree_root": "/tmp/worktree"},
                route,
            )

        self.assertEqual(result["error_reason"], "inspection_api_timeout")
        self.assertEqual(result["endpoint"], "problems")
        self.assertTrue(result["transport_state_unknown"])
        self.assertTrue(jb_inspect.should_defer_lifecycle_cleanup(result, {"opened_by_helper": True}))

    def test_wait_transport_timeout_cancels_only_the_accepted_run(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}
        calls = []
        statuses = iter(
            [
                {"inspection_in_progress": True, "inspection_run_id": 7, "is_scanning": True},
                {"inspection_in_progress": False, "inspection_run_id": 7, "is_scanning": False, "indexing": False},
            ]
        )

        def fake_call(active_route, endpoint, params, timeout=None):
            calls.append((endpoint, params))
            if endpoint == "trigger":
                return {"status": "triggered", "run_id": 7, "route": route}
            if endpoint == "wait":
                raise jb_inspect.InspectError(
                    "Inspection API timed out on port 63342: timed out",
                    3,
                    {"error_reason": "inspection_api_timeout", "endpoint": "wait", "port": 63342},
                )
            if endpoint == "status":
                return next(statuses)
            if endpoint == "cancel":
                return {"status": "cancel_requested", "inspection_cancellation_requested": True, "inspection_run_id": 7}
            self.fail(f"unexpected endpoint: {endpoint}")

        with patch.object(jb_inspect, "call_endpoint", side_effect=fake_call):
            result = jb_inspect.run_inspection_on_route(
                helper_args(timeout_ms=1000, poll_ms=1),
                {"worktree_root": "/tmp/worktree"},
                route,
            )

        self.assertEqual(result["error_reason"], "inspection_api_timeout")
        self.assertEqual(result["timeout_endpoint"], "wait")
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertEqual(result["verdict_reason"], "inspection_api_timeout")
        self.assertEqual(result["cancellation"]["status"], "settled")
        self.assertTrue(result["cancellation"]["settled"])
        cancel_call = next(params for endpoint, params in calls if endpoint == "cancel")
        self.assertEqual(cancel_call["inspection_run_id"], 7)

    def test_trigger_transport_timeout_remains_ambiguous_after_idle_status_probe(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}

        def fake_call(active_route, endpoint, params, timeout=None):
            if endpoint == "trigger":
                raise jb_inspect.InspectError(
                    "Inspection API timed out on port 63342: timed out",
                    3,
                    {"error_reason": "inspection_api_timeout", "endpoint": "trigger", "port": 63342},
                )
            if endpoint == "status":
                return {"inspection_in_progress": False, "is_scanning": False, "indexing": False}
            self.fail(f"unexpected endpoint: {endpoint}")

        with patch.object(jb_inspect, "call_endpoint", side_effect=fake_call):
            result = jb_inspect.run_inspection_on_route(
                helper_args(timeout_ms=1000, poll_ms=1),
                {"worktree_root": "/tmp/worktree"},
                route,
            )

        self.assertTrue(result["transport_state_unknown"])
        self.assertTrue(jb_inspect.should_defer_lifecycle_cleanup(result, {"opened_by_helper": True}))
        self.assertEqual(result["verdict_reason"], "inspection_api_timeout")

    def test_unknown_transport_state_defers_owned_project_cleanup(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}
        lease = {"lease_id": "lease-1", "opened_by_helper": True, "state": "prepared"}
        timeout_result = {
            "status": "error",
            "error_reason": "inspection_api_timeout",
            "transport_state_unknown": True,
            "wait": {"timed_out": True},
        }

        with (
            patch.object(jb_inspect, "prepare_lifecycle_details", return_value=({"route": route}, lease, "proof-1")),
            patch.object(jb_inspect, "run_inspection_with_internal_retry", return_value=timeout_result),
            patch.object(jb_inspect, "cleanup_lifecycle") as cleanup,
            patch.object(
                jb_inspect,
                "defer_lifecycle_cleanup",
                return_value={"status": "deferred", "cleanup_deferred": True},
            ) as defer_cleanup,
        ):
            result = jb_inspect.command_closeout(
                helper_args(keep_warm=False, lifecycle_lock_timeout_ms=0),
                {},
            )

        cleanup.assert_not_called()
        defer_cleanup.assert_called_once_with(lease, timeout_result)
        self.assertEqual(result["cleanup"]["status"], "deferred")
        self.assertTrue(result["cleanup_deferred"])

    def test_inspection_exception_defers_owned_project_cleanup(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}
        lease = {"lease_id": "lease-1", "opened_by_helper": True, "state": "prepared"}
        error = jb_inspect.InspectError(
            "Inspection API returned invalid JSON",
            3,
            {"error_reason": "invalid_api_response", "endpoint": "problems"},
        )

        with (
            patch.object(jb_inspect, "prepare_lifecycle_details", return_value=({"route": route}, lease, "proof-1")),
            patch.object(jb_inspect, "run_inspection_with_internal_retry", side_effect=error),
            patch.object(jb_inspect, "cleanup_lifecycle") as cleanup,
            patch.object(
                jb_inspect,
                "defer_lifecycle_cleanup",
                return_value={"status": "deferred", "cleanup_deferred": True},
            ) as defer_cleanup,
        ):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.command_closeout(
                    helper_args(keep_warm=False, lifecycle_lock_timeout_ms=0),
                    {},
                )

        cleanup.assert_not_called()
        defer_cleanup.assert_called_once()
        self.assertEqual(raised.exception.payload["cleanup"]["status"], "deferred")
        self.assertTrue(raised.exception.payload["inspection_failure"]["transport_state_unknown"])
        self.assertEqual(raised.exception.payload["inspection_failure"]["error_reason"], "invalid_api_response")

    def test_keyboard_interrupt_defers_owned_project_cleanup_before_reraising(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}
        lease = {"lease_id": "lease-1", "opened_by_helper": True, "state": "prepared"}

        with (
            patch.object(jb_inspect, "prepare_lifecycle_details", return_value=({"route": route}, lease, "proof-1")),
            patch.object(jb_inspect, "run_inspection_with_internal_retry", side_effect=KeyboardInterrupt()),
            patch.object(jb_inspect, "cleanup_lifecycle") as cleanup,
            patch.object(
                jb_inspect,
                "defer_lifecycle_cleanup",
                return_value={"status": "deferred", "cleanup_deferred": True},
            ) as defer_cleanup,
        ):
            with self.assertRaises(KeyboardInterrupt):
                jb_inspect.command_closeout(
                    helper_args(keep_warm=False, lifecycle_lock_timeout_ms=0),
                    {},
                )

        cleanup.assert_not_called()
        defer_cleanup.assert_called_once()

    def test_generic_inspection_exception_becomes_structured_inspect_error(self):
        route = {"port": 63342, "project_key": "path:/tmp/worktree", "session_id": "session"}
        lease = {"lease_id": "lease-1", "opened_by_helper": True, "state": "prepared"}

        with (
            patch.object(jb_inspect, "prepare_lifecycle_details", return_value=({"route": route}, lease, "proof-1")),
            patch.object(jb_inspect, "run_inspection_with_internal_retry", side_effect=RuntimeError("boom")),
            patch.object(
                jb_inspect,
                "defer_lifecycle_cleanup",
                return_value={"status": "deferred", "cleanup_deferred": True},
            ),
        ):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.command_closeout(
                    helper_args(keep_warm=False, lifecycle_lock_timeout_ms=0),
                    {},
                )

        self.assertEqual(raised.exception.payload["error_type"], "RuntimeError")
        self.assertEqual(raised.exception.payload["error_message"], "boom")
        self.assertEqual(raised.exception.payload["cleanup"]["status"], "deferred")
        self.assertTrue(raised.exception.payload["inspection_failure"]["transport_state_unknown"])

    def test_settled_cancellation_allows_owned_project_cleanup(self):
        result = {
            "status": "timed_out",
            "verdict": "UNKNOWN",
            "cancellation": {
                "settled": True,
                "last_status": {"inspection_in_progress": False, "is_scanning": False, "indexing": False},
            },
            "wait": {"timed_out": True, "inspection_in_progress": True},
        }

        self.assertFalse(jb_inspect.should_defer_lifecycle_cleanup(result, {"opened_by_helper": True}))

    def test_run_uses_lifecycle_prepare_and_cleanup(self):
        calls = []
        cleanups = []

        prepared = {
            "status": "prepared",
            "route": {"port": 1, "project_key": "path:/tmp/worktree", "base_path": "/tmp/worktree"},
            "lease": {"opened_by_helper": True},
        }
        lease = {"opened_by_helper": True, "lease_id": "lease-1"}

        def fake_run(args, context, route):
            calls.append(route)
            return {"status": "clean", "clean": True, "route": route}

        def fake_cleanup(cleanup_lease, route, close_proof):
            cleanups.append((cleanup_lease, route, close_proof))
            return {"status": "closed"}

        original_prepare = jb_inspect.prepare_lifecycle_details
        original_run = jb_inspect.run_inspection_on_route
        original_cleanup = jb_inspect.cleanup_lifecycle
        jb_inspect.prepare_lifecycle_details = lambda args, context: (prepared, lease, "proof-1")
        jb_inspect.run_inspection_on_route = fake_run
        jb_inspect.cleanup_lifecycle = fake_cleanup
        try:
            args = Namespace(
                keep_warm=False,
                lifecycle_lock_timeout_ms=0,
            )
            result = jb_inspect.command_run(args, {})
        finally:
            jb_inspect.prepare_lifecycle_details = original_prepare
            jb_inspect.run_inspection_on_route = original_run
            jb_inspect.cleanup_lifecycle = original_cleanup

        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["cleanup"], {"status": "closed"})
        self.assertEqual(calls, [prepared["route"]])
        self.assertEqual(cleanups, [(lease, prepared["route"], "proof-1")])

    def test_http_get_redacts_sensitive_query_in_result_url(self):
        captured = {}
        original_urlopen = jb_inspect.urllib.request.urlopen

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self):
                return b'{"ok": true}'

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse()

        jb_inspect.urllib.request.urlopen = fake_urlopen
        try:
            result = jb_inspect.http_get(63342, "lifecycle/close", {"close_token": "private-close-proof", "project_key": "path:/tmp/repo"})
        finally:
            jb_inspect.urllib.request.urlopen = original_urlopen

        self.assertIn("private-close-proof", captured["url"])
        self.assertNotIn("private-close-proof", result.url)
        self.assertIn(urllib.parse.quote(jb_inspect.REDACTED), result.url)

    def test_http_get_returns_inspection_in_progress_conflict(self):
        payload = {
            "error": "inspection_in_progress",
            "status": "inspection_in_progress",
            "inspection_in_progress": True,
            "inspection_run_id": 42,
        }
        error = jb_inspect.urllib.error.HTTPError(
            "inspection-trigger",
            409,
            "Conflict",
            {},
            io.BytesIO(json.dumps(payload).encode()),
        )

        with patch.object(jb_inspect.urllib.request, "urlopen", side_effect=error):
            result = jb_inspect.http_get(63342, "trigger", {})

        self.assertEqual(result.status, 409)
        self.assertEqual(result.body, payload)

    def test_http_get_classifies_socket_timeout_as_busy_api(self):
        with patch.object(jb_inspect.urllib.request, "urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.http_get(63342, "lifecycle/open", {})

        self.assertEqual(raised.exception.payload["error_reason"], "inspection_api_timeout")
        self.assertEqual(raised.exception.payload["endpoint"], "lifecycle/open")

    def test_http_get_keeps_connection_refusal_as_unavailable_api(self):
        refused = jb_inspect.urllib.error.URLError(ConnectionRefusedError("connection refused"))
        with patch.object(jb_inspect.urllib.request, "urlopen", side_effect=refused):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.http_get(63342, "identity", {})

        self.assertEqual(raised.exception.payload["error_reason"], "inspection_api_unavailable")

    def test_open_in_ide_uses_background_flag_on_macos(self):
        with patch.object(jb_inspect.sys, "platform", "darwin"), patch.object(jb_inspect.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["open"], 0, "", "")
            jb_inspect.open_in_ide({"ide": "IntelliJ IDEA", "worktree_root": "/tmp/worktree"}, background=True)

        run.assert_called_once_with(["open", "-g", "-a", "IntelliJ IDEA", "/tmp/worktree"], check=False, capture_output=True, text=True)

    def test_open_in_ide_uses_explicit_app_for_macos_launch(self):
        with patch.object(jb_inspect.sys, "platform", "darwin"), patch.object(jb_inspect.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["open"], 0, "", "")
            jb_inspect.open_in_ide(
                {"ide": "WebStorm", "ide_app": "WebStorm 2026.2 EAP", "worktree_root": "/tmp/worktree"},
                background=True,
            )

        run.assert_called_once_with(["open", "-g", "-a", "WebStorm 2026.2 EAP", "/tmp/worktree"], check=False, capture_output=True, text=True)

    def test_open_in_ide_uses_resolved_app_path_when_available(self):
        app_path = Path("/Applications/WebStorm 2026.2 EAP.app")
        with patch.object(jb_inspect.sys, "platform", "darwin"), patch.object(jb_inspect.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["open"], 0, "", "")
            jb_inspect.open_in_ide(
                {
                    "ide": "WebStorm",
                    "ide_selection": {"app_path": str(app_path), "app_name": "WebStorm 2026.2 EAP"},
                    "worktree_root": "/tmp/worktree",
                },
                background=True,
            )

        run.assert_called_once_with(["open", "-g", "-n", "-a", str(app_path), "/tmp/worktree"], check=False, capture_output=True, text=True)

    def test_open_in_ide_uses_lifecycle_target_for_nested_project(self):
        with patch.object(jb_inspect.sys, "platform", "darwin"), patch.object(jb_inspect.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["open"], 0, "", "")
            jb_inspect.open_in_ide(
                {
                    "ide": "IntelliJ IDEA",
                    "worktree_root": "/tmp/harness-parent",
                    "lifecycle_target_path": "/tmp/harness-parent/workspace/project",
                },
                background=True,
            )

        run.assert_called_once_with(["open", "-g", "-a", "IntelliJ IDEA", "/tmp/harness-parent/workspace/project"], check=False, capture_output=True, text=True)

    def test_open_in_ide_reports_failed_macos_open(self):
        completed = subprocess.CompletedProcess(["open"], 1, "", "Unable to find application")
        with patch.object(jb_inspect.sys, "platform", "darwin"), patch.object(jb_inspect.subprocess, "run", return_value=completed):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.open_in_ide({"ide": "Missing IDE", "worktree_root": "/tmp/worktree"}, background=True)

        self.assertIn("Failed to ask macOS", str(raised.exception))
        self.assertEqual(raised.exception.payload["returncode"], 1)
        self.assertIn("Unable to find application", raised.exception.payload["stderr"])

    def test_bootstrap_ide_app_uses_hidden_launch_on_macos(self):
        with patch.object(jb_inspect.sys, "platform", "darwin"), patch.object(jb_inspect.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["open"], 0, "", "")
            jb_inspect.bootstrap_ide_app({"ide": "PyCharm", "worktree_root": "/tmp/worktree"}, background=True)

        run.assert_called_once_with(["open", "-g", "-j", "-a", "PyCharm"], check=False, capture_output=True, text=True)

    def test_bootstrap_ide_app_uses_explicit_app_for_hidden_launch(self):
        with patch.object(jb_inspect.sys, "platform", "darwin"), patch.object(jb_inspect.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["open"], 0, "", "")
            jb_inspect.bootstrap_ide_app({"ide": "WebStorm", "ide_app": "WebStorm 2026.2 EAP"}, background=True)

        run.assert_called_once_with(["open", "-g", "-j", "-a", "WebStorm 2026.2 EAP"], check=False, capture_output=True, text=True)

    def test_bootstrap_ide_app_uses_resolved_app_path(self):
        app_path = Path("/Applications/WebStorm 2026.2 EAP.app")
        with patch.object(jb_inspect.sys, "platform", "darwin"), patch.object(jb_inspect.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["open"], 0, "", "")
            jb_inspect.bootstrap_ide_app({"ide_selection": {"app_path": str(app_path), "app_name": "WebStorm 2026.2 EAP"}}, background=True)

        run.assert_called_once_with(["open", "-g", "-j", "-n", "-a", str(app_path)], check=False, capture_output=True, text=True)

    def test_bootstrap_ide_app_reports_failed_hidden_launch(self):
        completed = subprocess.CompletedProcess(["open"], 1, "", "Unable to find application")
        with patch.object(jb_inspect.sys, "platform", "darwin"), patch.object(jb_inspect.subprocess, "run", return_value=completed):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.bootstrap_ide_app({"ide": "Missing IDE", "worktree_root": "/tmp/worktree"}, background=True)

        self.assertIn("Failed to launch", str(raised.exception))
        self.assertEqual(raised.exception.payload["command"], ["open", "-g", "-j", "-a", "Missing IDE"])
        self.assertIn("Unable to find application", raised.exception.payload["stderr"])

    def test_auto_open_timeout_payload_names_trust_and_modal_causes(self):
        args = Namespace(background_open=True)
        original_trusted = jb_inspect.trusted_auto_open_roots
        original_diagnostic = jb_inspect.discover_diagnostic_identities
        jb_inspect.trusted_auto_open_roots = lambda: ["/tmp"]
        jb_inspect.discover_diagnostic_identities = lambda port: []
        try:
            payload = jb_inspect.auto_open_timeout_payload(
                args,
                {"ide": "PyCharm", "worktree_root": "/tmp/worktree"},
                300_000,
            )
        finally:
            jb_inspect.trusted_auto_open_roots = original_trusted
            jb_inspect.discover_diagnostic_identities = original_diagnostic

        self.assertTrue(payload["background_open"])
        self.assertEqual(payload["blocked_diagnostic"]["reason"], "jetbrains_project_open_blocked")
        self.assertEqual(payload["blocked_diagnostic"]["selected_trusted_root"], str(Path("/tmp").resolve()))
        self.assertIn("JetBrains trust", payload["likely_causes"][0])
        self.assertIn("new window", " ".join(payload["likely_causes"]))

    def test_cleanup_failure_surfaces_close_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cache = os.environ.get("JETBRAINS_INSPECTION_CACHE_DIR")
            original_private_http = jb_inspect.private_http_get_body
            os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = tmp
            try:
                lease = jb_inspect.create_local_lease({"worktree_root": "/tmp/repo"}, "prepared")
                lease.update(
                    {
                        "opened_by_helper": True,
                        "project_instance_id": "session:1",
                        "project_key": "path:/tmp/repo",
                    }
                )

                def fake_private_http(port, endpoint, params, timeout=None):
                    self.assertEqual(timeout, 35.0)
                    raise jb_inspect.InspectError("IDE session changed", 4, {"reason": "session_drift", "session_drift": True})

                jb_inspect.private_http_get_body = fake_private_http
                result = jb_inspect.cleanup_lifecycle(lease, {"port": 63342, "project_key": "path:/tmp/repo"}, "token")
            finally:
                jb_inspect.private_http_get_body = original_private_http
                if original_cache is None:
                    os.environ.pop("JETBRAINS_INSPECTION_CACHE_DIR", None)
                else:
                    os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = original_cache

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "session_drift")
        self.assertTrue(result["cleanup_failed"])

    def test_cleanup_http_500_is_failed_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cache = os.environ.get("JETBRAINS_INSPECTION_CACHE_DIR")
            original_private_http = jb_inspect.private_http_get_body
            os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = tmp
            try:
                lease = jb_inspect.create_local_lease(
                    {
                        "worktree_root": "/tmp/repo",
                        "client_run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    },
                    "prepared",
                )
                lease.update(
                    {
                        "opened_by_helper": True,
                        "project_instance_id": "session:1",
                        "project_key": "path:/tmp/repo",
                    }
                )
                attribution = {
                    "schema_version": 1,
                    "source": "plugin",
                    "classification": "tool_caused",
                    "code": "inspection_api_http_error",
                    "phase": "lifecycle_close",
                    "http_status": 500,
                    "request_id": "11111111-1111-4111-8111-111111111111",
                }

                def fake_private_http(port, endpoint, params, timeout=None):
                    raise jb_inspect.InspectError(
                        "HTTP 500 from inspection API",
                        3,
                        {
                            "status": "error",
                            "error_reason": "inspection_api_http_error",
                            "http_status": 500,
                            "inspection_attribution": attribution,
                        },
                    )

                jb_inspect.private_http_get_body = fake_private_http
                result = jb_inspect.cleanup_lifecycle(
                    lease,
                    {"port": 63342, "project_key": "path:/tmp/repo"},
                    "token",
                )
            finally:
                jb_inspect.private_http_get_body = original_private_http
                if original_cache is None:
                    os.environ.pop("JETBRAINS_INSPECTION_CACHE_DIR", None)
                else:
                    os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = original_cache

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup_failed"])
        self.assertEqual(result["reason"], "inspection_api_http_error")
        self.assertEqual(result["inspection_attribution"]["request_id"], "11111111-1111-4111-8111-111111111111")

    def test_cleanup_skipped_surfaces_successful_close_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_cache = os.environ.get("JETBRAINS_INSPECTION_CACHE_DIR")
            original_private_http = jb_inspect.private_http_get_body
            os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = tmp
            try:
                lease = jb_inspect.create_local_lease({"worktree_root": "/tmp/repo"}, "prepared")
                lease.update(
                    {
                        "opened_by_helper": True,
                        "project_instance_id": "session:1",
                        "project_key": "path:/tmp/repo",
                    }
                )

                def fake_private_http(port, endpoint, params, timeout=None):
                    self.assertEqual(timeout, 35.0)
                    return {"status": "skipped", "reason": "not_claimed"}

                jb_inspect.private_http_get_body = fake_private_http
                result = jb_inspect.cleanup_lifecycle(lease, {"port": 63342, "project_key": "path:/tmp/repo"}, "token")
            finally:
                jb_inspect.private_http_get_body = original_private_http
                if original_cache is None:
                    os.environ.pop("JETBRAINS_INSPECTION_CACHE_DIR", None)
                else:
                    os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = original_cache

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "not_claimed")
        self.assertTrue(result["cleanup_skipped"])
        self.assertFalse(result["cleanup_failed"])

    def test_cleanup_reason_prefers_error_reason_over_status(self):
        error = jb_inspect.InspectError(
            "Timed out waiting for lifecycle close.",
            3,
            {"status": {"status": "indexing"}, "error_reason": "timeout"},
        )

        self.assertEqual(jb_inspect.public_cleanup_reason(error), "timeout")

    def test_lifecycle_lock_times_out_when_already_held(self):
        if jb_inspect.fcntl is None:
            self.skipTest("fcntl locking is unavailable on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            original_cache = os.environ.get("JETBRAINS_INSPECTION_CACHE_DIR")
            os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = tmp
            holder = None
            try:
                path = jb_inspect.lifecycle_lock_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                holder = path.open("a+", encoding="utf-8")
                jb_inspect.fcntl.flock(holder.fileno(), jb_inspect.fcntl.LOCK_EX | jb_inspect.fcntl.LOCK_NB)

                with self.assertRaises(jb_inspect.InspectError) as raised:
                    with jb_inspect.lifecycle_lock(1):
                        pass
            finally:
                if holder is not None:
                    jb_inspect.fcntl.flock(holder.fileno(), jb_inspect.fcntl.LOCK_UN)
                    holder.close()
                if original_cache is None:
                    os.environ.pop("JETBRAINS_INSPECTION_CACHE_DIR", None)
                else:
                    os.environ["JETBRAINS_INSPECTION_CACHE_DIR"] = original_cache

        self.assertIn("lifecycle lock", str(raised.exception))
        self.assertEqual(raised.exception.payload["timeout_ms"], 1)

    def test_trusted_auto_open_allows_worktree_under_global_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "trusted"
            worktree = root / "repo"
            worktree.mkdir(parents=True)
            with patch.object(jb_inspect, "trusted_auto_open_roots", return_value=[str(root)]):
                jb_inspect.ensure_trusted_auto_open_root({"worktree_root": str(worktree)})

    def test_trusted_auto_open_uses_lifecycle_target_for_nested_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted-harness-runs"
            nested_project = trusted / "run" / "workspace" / "project"
            parent_repo = root / "code-prealign-new-skills"
            nested_project.mkdir(parents=True)
            parent_repo.mkdir()

            context = {
                "worktree_root": str(parent_repo),
                "project_path": str(nested_project),
                "exact_route_path": str(nested_project),
                "lifecycle_target_path": str(nested_project),
            }

            with patch.object(jb_inspect, "trusted_auto_open_roots", return_value=[str(trusted)]):
                jb_inspect.ensure_trusted_auto_open_root(context)

    def test_trusted_auto_open_rejects_untrusted_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            trusted = Path(tmp) / "trusted"
            worktree = Path(tmp) / "untrusted" / "repo"
            trusted.mkdir()
            worktree.mkdir(parents=True)

            with self.assertRaises(jb_inspect.InspectError) as raised:
                with patch.object(jb_inspect, "trusted_auto_open_roots", return_value=[str(trusted)]):
                    jb_inspect.ensure_trusted_auto_open_root({"worktree_root": str(worktree)})

        self.assertIn("outside trusted auto-open roots", str(raised.exception))

    def test_ensure_jetbrains_trusted_locations_updates_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "PyCharm2026.1"
            options_dir = config_dir / "options"
            options_dir.mkdir(parents=True)
            trusted_file = options_dir / "trusted-paths.xml"
            trusted_file.write_text(
                '<application><component name="Trusted.Paths.Settings"><option name="TRUSTED_PATHS"><list /></option></component></application>',
                encoding="utf-8",
            )
            worktree = Path(tmp) / "trusted" / "repo"
            worktree.mkdir(parents=True)
            original_config = os.environ.get("JETBRAINS_INSPECTION_IDE_CONFIG_DIR")
            os.environ["JETBRAINS_INSPECTION_IDE_CONFIG_DIR"] = str(config_dir)
            try:
                with (
                    patch.object(jb_inspect.sys, "platform", "darwin"),
                    patch.object(jb_inspect, "trusted_auto_open_roots", return_value=[str(worktree.parent)]),
                ):
                    result = jb_inspect.ensure_jetbrains_trusted_locations({"ide": "PyCharm", "worktree_root": str(worktree)})
                updated = trusted_file.read_text(encoding="utf-8")
            finally:
                if original_config is None:
                    os.environ.pop("JETBRAINS_INSPECTION_IDE_CONFIG_DIR", None)
                else:
                    os.environ["JETBRAINS_INSPECTION_IDE_CONFIG_DIR"] = original_config

        self.assertEqual(result["status"], "trusted")
        self.assertTrue(result["config_updates"][0]["trusted_locations"]["changed"])
        self.assertIn("/trusted", updated)
        self.assertIn("Trusted.Paths.Settings", updated)
        self.assertIn("Trusted.Paths", updated)

    def test_ensure_project_opening_policy_sets_new_window_without_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "PyCharm2026.1"
            options_dir = config_dir / "options"
            options_dir.mkdir(parents=True)
            general_file = options_dir / "ide.general.xml"
            general_file.write_text(
                '<application><component name="GeneralSettings"><option name="confirmOpenNewProject2" value="0" /></component></application>',
                encoding="utf-8",
            )

            result = jb_inspect.ensure_project_opening_policy(config_dir)
            updated = general_file.read_text(encoding="utf-8")

        self.assertTrue(result["changed"])
        self.assertIn('name="confirmOpenNewProject2" value="-1"', updated)

    def test_open_via_running_ide_calls_matching_lifecycle_open(self):
        calls = []
        original_discover = jb_inspect.discover_identities
        original_http_get = jb_inspect.http_get
        jb_inspect.discover_identities = lambda port: [
            {
                "port": 63341,
                "ide_name": "IntelliJ IDEA",
                "session_id": "s1",
                "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
            }
        ]

        def fake_http_get(port, endpoint, params, timeout=jb_inspect.DEFAULT_TIMEOUT_SECONDS):
            calls.append((port, endpoint, params))
            return jb_inspect.HttpResult(
                200,
                {
                    "status": "opening",
                    "opening_scheduled": True,
                    "opened": False,
                    "session_id": "s1",
                    "ownership_registered": True,
                    "lease_id": "lease-1",
                    "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
                },
                "url",
            )

        jb_inspect.http_get = fake_http_get
        attempts = []
        try:
            with patch.object(jb_inspect, "persist_open_request_identity"):
                result = jb_inspect.open_via_running_ide(
                    Namespace(port=None),
                    {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
                    attempts,
                    lease={"lease_id": "lease-1"},
                )
        finally:
            jb_inspect.discover_identities = original_discover
            jb_inspect.http_get = original_http_get

        self.assertTrue(result["ownership_registered"])
        self.assertEqual(result["open_outcome"], "helper_registered")
        self.assertEqual(calls[0][1], "lifecycle/open")
        self.assertEqual(calls[0][2]["worktree_path"], "/tmp/worktree")
        self.assertEqual(calls[0][2]["session_id"], "s1")
        self.assertEqual(calls[0][2]["lease_id"], "lease-1")
        self.assertEqual(attempts[0]["endpoint_status"], "opening")

    def test_open_via_running_ide_persists_identity_before_http_request(self):
        identity = {
            "port": 63341,
            "ide_name": "IntelliJ IDEA",
            "session_id": "s1",
            "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
        }
        response = {
            "status": "opening",
            "opening_scheduled": True,
            "session_id": "s1",
            "ownership_registered": True,
            "lease_id": "lease-1",
            "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
        }
        events = []
        lease = {"lease_id": "lease-1", "state": "open_requesting"}

        with (
            patch.object(jb_inspect, "discover_open_identities", return_value=[identity]),
            patch.object(
                jb_inspect,
                "persist_open_request_identity",
                side_effect=lambda *args: events.append("persist"),
            ),
            patch.object(
                jb_inspect,
                "http_get",
                side_effect=lambda port, endpoint, params, timeout: events.append(("http", params["lease_id"]))
                or jb_inspect.HttpResult(200, response, "url"),
            ),
        ):
            result = jb_inspect.open_via_running_ide(
                Namespace(port=None),
                {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/worktree"},
                lease=lease,
            )

        self.assertEqual(events, ["persist", ("http", "lease-1")])
        self.assertTrue(result["ownership_registered"])

    def test_open_via_running_ide_treats_response_timeout_as_ambiguous_acceptance(self):
        identity = {
            "port": 63341,
            "ide_name": "IntelliJ IDEA",
            "session_id": "s1",
            "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
        }
        lease = {"lease_id": "lease-1", "state": "open_requesting"}
        states = []

        with (
            patch.object(jb_inspect, "discover_open_identities", return_value=[identity]),
            patch.object(jb_inspect, "persist_open_request_identity"),
            patch.object(
                jb_inspect,
                "http_get",
                side_effect=jb_inspect.InspectError(
                    "Inspection API timed out on port 63341: timed out",
                    3,
                    {"error_reason": "inspection_api_timeout"},
                ),
            ),
            patch.object(jb_inspect, "mark_lease_state", side_effect=lambda active_lease, state: states.append((active_lease, state))),
        ):
            attempts = []
            result = jb_inspect.open_via_running_ide(
                Namespace(port=None),
                {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/worktree"},
                attempts=attempts,
                lease=lease,
            )

        self.assertEqual(result["open_outcome"], "response_unknown")
        self.assertTrue(result["request_may_have_been_accepted"])
        self.assertFalse(result["ownership_registered"])
        self.assertEqual(lease["open_attempts"], attempts)
        self.assertTrue(lease["open_request_may_have_been_accepted"])
        self.assertEqual(states[-1][1], "open_requesting")

    def test_ambiguous_open_evidence_survives_route_wait_failure(self):
        lease = {"lease_id": "lease-1", "state": "open_requesting", "opened_by_helper": False}
        attempts = [
            {
                "method": "running_ide",
                "accepted": False,
                "open_outcome": "response_unknown",
                "request_may_have_been_accepted": True,
                "ownership_registered": False,
            }
        ]

        with patch.object(
            jb_inspect,
            "mark_lease_state",
            side_effect=lambda active_lease, state: active_lease.update({"state": state}),
        ):
            jb_inspect.persist_preparation_lease(
                lease,
                state="open_requesting",
                stage="route_wait",
                opened_by_helper=False,
                open_method="running_ide",
                open_attempts=attempts,
            )

        self.assertTrue(lease["open_request_may_have_been_accepted"])
        with (
            patch.object(jb_inspect, "cleanup_lifecycle") as cleanup,
            patch.object(
                jb_inspect,
                "defer_failed_preparation_cleanup",
                return_value={"status": "deferred", "reason": "ownership_unresolved"},
            ) as defer_cleanup,
        ):
            result = jb_inspect.cleanup_failed_preparation(
                lease,
                None,
                None,
                jb_inspect.InspectError("route wait timed out", 3, {"error_reason": "project_open_blocked"}),
                "route_wait",
            )

        cleanup.assert_not_called()
        defer_cleanup.assert_called_once()
        self.assertEqual(result["status"], "deferred")

    def test_definitive_not_owned_claim_clears_ambiguous_open_evidence(self):
        lease = {
            "lease_id": "lease-1",
            "state": "route_resolved",
            "opened_by_helper": False,
            "open_request_may_have_been_accepted": True,
        }
        attempts = [{"request_may_have_been_accepted": True}]

        with patch.object(
            jb_inspect,
            "mark_lease_state",
            side_effect=lambda active_lease, state: active_lease.update({"state": state}),
        ):
            jb_inspect.persist_preparation_lease(
                lease,
                state="ownership_not_proven",
                stage="readiness_wait",
                opened_by_helper=False,
                open_method="running_ide",
                open_attempts=attempts,
                claim_metadata={"ownership_proven": False, "ownership_determined": True},
            )

        self.assertFalse(lease["open_request_may_have_been_accepted"])
        self.assertFalse(jb_inspect.lease_may_own_open_project(lease))

    def test_lifecycle_claim_ownership_ignores_legacy_close_token(self):
        claim = {"status": "claimed", "lease_id": "lease-1", "close_token": "legacy-proof"}

        ownership_proven, close_proof = jb_inspect.lifecycle_claim_ownership(
            claim,
            {"lease_id": "lease-1"},
        )

        self.assertIsNone(ownership_proven)
        self.assertIsNone(close_proof)
        self.assertNotIn("close_token", claim)

    def test_open_via_running_ide_does_not_claim_already_open_or_opening(self):
        original_discover = jb_inspect.discover_identities
        original_http_get = jb_inspect.http_get
        jb_inspect.discover_identities = lambda port: [
            {"port": 63341, "ide_name": "IntelliJ IDEA", "session_id": "s1"}
        ]
        responses = (
            ({"status": "already_open", "opened": False, "session_id": "s1"}, "already_open"),
            (
                {
                    "status": "opening",
                    "opened": False,
                    "opening_scheduled": False,
                    "reason": "already_opening",
                    "session_id": "s1",
                },
                "already_opening",
            ),
        )
        try:
            for body, outcome in responses:
                with self.subTest(outcome=outcome):
                    jb_inspect.http_get = lambda *args, response_body=body, **kwargs: jb_inspect.HttpResult(
                        200,
                        response_body,
                        "url",
                    )
                    result = jb_inspect.open_via_running_ide(
                        Namespace(port=None),
                        {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/worktree"},
                    )

                    self.assertEqual(result["open_outcome"], outcome)
                    self.assertFalse(result["ownership_registered"])
        finally:
            jb_inspect.discover_identities = original_discover
            jb_inspect.http_get = original_http_get

    def test_identity_matches_exact_eap_version_from_identity_metadata(self):
        context = {
            "ide": "WebStorm",
            "ide_selection": {
                "product_key": "webstorm",
                "exact": True,
                "channel": "eap",
                "version": "2026.2",
            },
        }
        identity = {"ide_name": "WebStorm", "ide_product_code": "WS", "ide_version": "2026.2 EAP"}

        self.assertTrue(jb_inspect.identity_matches_context(identity, context))

    def test_identity_rejects_exact_eap_when_running_identity_is_stable(self):
        context = {
            "ide": "WebStorm",
            "ide_selection": {
                "product_key": "webstorm",
                "exact": True,
                "channel": "eap",
                "version": "2026.2",
            },
        }
        identity = {"ide_name": "WebStorm", "ide_product_code": "WS", "ide_version": "2026.2"}

        self.assertFalse(jb_inspect.identity_matches_context(identity, context))

    def test_identity_rejects_exact_version_mismatch(self):
        context = {
            "ide": "WebStorm",
            "ide_selection": {
                "product_key": "webstorm",
                "exact": True,
                "channel": "stable",
                "version": "2026.1",
            },
        }
        identity = {"ide_name": "WebStorm", "ide_product_code": "WS", "ide_version": "2026.2"}

        self.assertFalse(jb_inspect.identity_matches_context(identity, context))

    def test_open_via_running_ide_sends_lifecycle_target_path(self):
        calls = []
        original_discover = jb_inspect.discover_identities
        original_http_get = jb_inspect.http_get
        jb_inspect.discover_identities = lambda port: [
            {
                "port": 63341,
                "ide_name": "IntelliJ IDEA",
                "session_id": "s1",
                "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
            }
        ]

        def fake_http_get(port, endpoint, params, timeout=jb_inspect.DEFAULT_TIMEOUT_SECONDS):
            calls.append((port, endpoint, params))
            return jb_inspect.HttpResult(
                200,
                {
                    "status": "opening",
                    "opening_scheduled": True,
                    "opened": False,
                    "session_id": "s1",
                    "ownership_registered": True,
                    "lease_id": "lease-1",
                    "lifecycle_ownership_protocol": jb_inspect.LIFECYCLE_OWNERSHIP_PROTOCOL,
                },
                "url",
            )

        jb_inspect.http_get = fake_http_get
        try:
            with patch.object(jb_inspect, "persist_open_request_identity"):
                result = jb_inspect.open_via_running_ide(
                    Namespace(port=None),
                    {
                        "ide": "IntelliJ IDEA",
                        "worktree_root": "/tmp/harness-parent",
                        "project_path": "/tmp/harness-parent/workspace/project",
                        "lifecycle_target_path": "/tmp/harness-parent/workspace/project",
                    },
                    lease={"lease_id": "lease-1"},
                )
        finally:
            jb_inspect.discover_identities = original_discover
            jb_inspect.http_get = original_http_get

        self.assertTrue(result["ownership_registered"])
        self.assertEqual(calls[0][1], "lifecycle/open")
        self.assertEqual(calls[0][2]["worktree_path"], "/tmp/harness-parent/workspace/project")

    def test_open_via_running_ide_ignores_other_ide_products(self):
        original_discover = jb_inspect.discover_identities
        original_diagnostic = jb_inspect.discover_diagnostic_identities
        original_http_get = jb_inspect.http_get
        jb_inspect.discover_identities = lambda port: [{"port": 63341, "ide_name": "WebStorm", "session_id": "s1"}]
        jb_inspect.discover_diagnostic_identities = lambda port: [{"port": 63341, "ide_name": "WebStorm", "session_id": "s1"}]

        def fake_http_get(port, endpoint, params, timeout=jb_inspect.DEFAULT_TIMEOUT_SECONDS):
            if endpoint == "lifecycle/open":
                raise AssertionError("should not call lifecycle/open for the wrong product")
            return jb_inspect.HttpResult(200, {"status": "ok"}, "url")

        jb_inspect.http_get = fake_http_get
        try:
            result = jb_inspect.open_via_running_ide(Namespace(port=None), {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/worktree"})
        finally:
            jb_inspect.discover_identities = original_discover
            jb_inspect.discover_diagnostic_identities = original_diagnostic
            jb_inspect.http_get = original_http_get

        self.assertFalse(result)

    def test_wait_for_matching_ide_identity_returns_target_product(self):
        original_discover = jb_inspect.discover_identities
        original_sleep = jb_inspect.time.sleep
        jb_inspect.discover_identities = lambda port: [
            {"port": 63341, "ide_name": "WebStorm", "session_id": "s1"},
            {"port": 63342, "ide_name": "PyCharm", "session_id": "s2"},
        ]
        jb_inspect.time.sleep = lambda seconds: None
        try:
            result = jb_inspect.wait_for_matching_ide_identity(Namespace(port=None, background_open=True), {"ide": "PyCharm"}, 100)
        finally:
            jb_inspect.discover_identities = original_discover
            jb_inspect.time.sleep = original_sleep

        self.assertEqual(result["session_id"], "s2")

    def test_wait_for_matching_ide_identity_uses_port_scan_when_registry_misses_target(self):
        original_discover = jb_inspect.discover_identities
        original_diagnostic = jb_inspect.discover_diagnostic_identities
        original_sleep = jb_inspect.time.sleep
        jb_inspect.discover_identities = lambda port: [
            {"port": 63342, "ide_name": "IntelliJ IDEA", "session_id": "idea-session"},
        ]
        jb_inspect.discover_diagnostic_identities = lambda port: [
            {"port": 63342, "ide_name": "IntelliJ IDEA", "session_id": "idea-session"},
            {"port": 63344, "ide_name": "PyCharm", "session_id": "py-session", "open_projects": []},
        ]
        jb_inspect.time.sleep = lambda seconds: None
        try:
            result = jb_inspect.wait_for_matching_ide_identity(Namespace(port=None, background_open=True), {"ide": "PyCharm"}, 100)
        finally:
            jb_inspect.discover_identities = original_discover
            jb_inspect.discover_diagnostic_identities = original_diagnostic
            jb_inspect.time.sleep = original_sleep

        self.assertEqual(result["session_id"], "py-session")

    def test_list_reports_zero_project_prompt_hint_for_discovered_identity(self):
        original_discover = jb_inspect.discover_identities
        jb_inspect.discover_identities = lambda port: [
            {
                "port": 63344,
                "ide_name": "PyCharm 2026.1.2",
                "ide_product_code": "PY",
                "session_id": "py-session",
                "open_projects": [],
            }
        ]
        try:
            result = jb_inspect.command_list(Namespace(port=None))
        finally:
            jb_inspect.discover_identities = original_discover

        self.assertEqual(result["count"], 0)
        self.assertIn("zero_project_hint", result)
        self.assertIn("Trust Project", result["zero_project_hint"])
        self.assertIn("safe-mode", result["zero_project_hint"])
        self.assertIn("open-project", result["zero_project_hint"])
        self.assertEqual(result["identities"][0]["open_project_count"], 0)

    def test_list_omits_zero_project_prompt_hint_without_identity(self):
        original_discover = jb_inspect.discover_identities
        jb_inspect.discover_identities = lambda port: []
        try:
            result = jb_inspect.command_list(Namespace(port=None))
        finally:
            jb_inspect.discover_identities = original_discover

        self.assertEqual(result["count"], 0)
        self.assertNotIn("zero_project_hint", result)

    def test_route_diagnostic_reports_other_ide_projects(self):
        original_discover = jb_inspect.discover_diagnostic_identities
        jb_inspect.discover_diagnostic_identities = lambda port: [
            {
                "port": 63341,
                "ide_name": "IntelliJ IDEA 2026.1.2",
                "ide_product_code": "IU",
                "plugin_version": "1.12.10",
                "session_id": "idea-session",
                "open_projects": [
                    {
                        "name": "jetbrains-inspection-api",
                        "project_key": "path:/Users/me/Developer/jetbrains-inspection-api",
                        "base_path": "/Users/me/Developer/jetbrains-inspection-api",
                    }
                ],
            }
        ]
        try:
            payload = jb_inspect.route_diagnostic_payload(
                Namespace(port=None),
                {"ide": "PyCharm", "worktree_root": "/Users/me/Developer/codex-skills", "project_path": "/Users/me/Developer/codex-skills"},
            )
        finally:
            jb_inspect.discover_diagnostic_identities = original_discover

        diagnostic = payload["route_diagnostic"]
        self.assertEqual(diagnostic["requested_ide"], "PyCharm")
        self.assertEqual(diagnostic["discovered_identity_count"], 1)
        self.assertEqual(diagnostic["matching_identity_count"], 0)
        self.assertEqual(diagnostic["discovered_project_count"], 1)
        self.assertEqual(diagnostic["matching_project_count"], 0)
        self.assertEqual(diagnostic["reason"], "different_jetbrains_product_running")
        self.assertEqual(diagnostic["other_projects"][0]["ide_product_code"], "IU")
        self.assertEqual(diagnostic["other_projects"][0]["plugin_version"], "1.12.10")
        self.assertIn("PyCharm", diagnostic["next_action"])
        self.assertIn("plugin installed and up to date", diagnostic["next_action"])

    def test_discover_identities_merges_registry_and_port_scan(self):
        original_registry = jb_inspect.registry_identities
        original_ports = jb_inspect.configured_ports
        original_identity = jb_inspect.identity_for_port
        jb_inspect.registry_identities = lambda: [
            {
                "port": 63342,
                "ide_name": "WebStorm",
                "session_id": "webstorm-session",
                "open_projects": [{"base_path": "/repo/webstorm"}],
            }
        ]
        jb_inspect.configured_ports = lambda: [63342, 63345]

        def fake_identity_for_port(port):
            if port == 63342:
                return {"port": 63342, "ide_name": "WebStorm", "session_id": "webstorm-session"}
            return {"port": 63345, "ide_name": "IntelliJ IDEA", "session_id": "idea-session"}

        jb_inspect.identity_for_port = fake_identity_for_port
        try:
            identities = jb_inspect.discover_identities(None)
        finally:
            jb_inspect.registry_identities = original_registry
            jb_inspect.configured_ports = original_ports
            jb_inspect.identity_for_port = original_identity

        sessions = {identity["session_id"] for identity in identities}
        self.assertEqual(sessions, {"webstorm-session", "idea-session"})
        self.assertEqual(len(identities), 2)
        webstorm = next(identity for identity in identities if identity["session_id"] == "webstorm-session")
        self.assertEqual(webstorm["open_projects"], [{"base_path": "/repo/webstorm"}])

    def test_discover_identities_with_explicit_port_does_not_scan_registry(self):
        original_registry = jb_inspect.registry_identities
        original_ports = jb_inspect.configured_ports
        original_identity = jb_inspect.identity_for_port
        calls = []
        jb_inspect.registry_identities = lambda: (_ for _ in ()).throw(AssertionError("registry should not be read"))
        jb_inspect.configured_ports = lambda: (_ for _ in ()).throw(AssertionError("ports should not be scanned"))

        def fake_identity_for_port(port):
            calls.append(port)
            return {"port": port, "ide_name": "IntelliJ IDEA", "session_id": "idea-session"}

        jb_inspect.identity_for_port = fake_identity_for_port
        try:
            identities = jb_inspect.discover_identities(63345)
        finally:
            jb_inspect.registry_identities = original_registry
            jb_inspect.configured_ports = original_ports
            jb_inspect.identity_for_port = original_identity

        self.assertEqual(calls, [63345])
        self.assertEqual(identities[0]["session_id"], "idea-session")

    def test_identity_for_port_rejects_mismatched_reported_port(self):
        original_http_get = jb_inspect.http_get
        jb_inspect.http_get = lambda port, endpoint, params: jb_inspect.HttpResult(
            200,
            {"port": 63342, "ide_name": "WebStorm", "session_id": "webstorm-session"},
            "url",
        )
        try:
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.identity_for_port(63345)
        finally:
            jb_inspect.http_get = original_http_get

        self.assertEqual(raised.exception.payload["error_reason"], "identity_port_mismatch")
        self.assertEqual(raised.exception.payload["requested_port"], 63345)
        self.assertEqual(raised.exception.payload["reported_port"], 63342)

    def test_identity_for_port_rejects_invalid_reported_port(self):
        original_http_get = jb_inspect.http_get
        jb_inspect.http_get = lambda port, endpoint, params: jb_inspect.HttpResult(
            200,
            {"port": "not-a-port", "ide_name": "WebStorm", "session_id": "webstorm-session"},
            "url",
        )
        try:
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.identity_for_port(63345)
        finally:
            jb_inspect.http_get = original_http_get

        self.assertEqual(raised.exception.payload["error_reason"], "invalid_identity_port")
        self.assertEqual(raised.exception.payload["requested_port"], 63345)
        self.assertEqual(raised.exception.payload["reported_port"], "not-a-port")

    def test_route_diagnostic_merges_registry_and_port_scan(self):
        original_registry = jb_inspect.registry_identities
        original_ports = jb_inspect.configured_ports
        original_identity = jb_inspect.identity_for_port
        jb_inspect.registry_identities = lambda: [
            {
                "port": 63342,
                "ide_name": "IntelliJ IDEA 2026.1.2",
                "ide_product_code": "IU",
                "session_id": "idea-session",
                "open_projects": [],
            }
        ]
        jb_inspect.configured_ports = lambda: [63342, 63344]

        def fake_identity_for_port(port):
            if port == 63342:
                return {
                    "port": 63342,
                    "ide_name": "IntelliJ IDEA 2026.1.2",
                    "ide_product_code": "IU",
                    "session_id": "idea-session",
                    "open_projects": [],
                }
            return {
                "port": 63344,
                "ide_name": "PyCharm 2026.1.2",
                "ide_product_code": "PY",
                "session_id": "py-session",
                "open_projects": [],
            }

        jb_inspect.identity_for_port = fake_identity_for_port
        try:
            payload = jb_inspect.route_diagnostic_payload(
                Namespace(port=None),
                {"ide": "PyCharm", "worktree_root": "/Users/me/Developer/mediaforce", "project_path": "/Users/me/Developer/mediaforce"},
            )
        finally:
            jb_inspect.registry_identities = original_registry
            jb_inspect.configured_ports = original_ports
            jb_inspect.identity_for_port = original_identity

        diagnostic = payload["route_diagnostic"]
        self.assertEqual(diagnostic["discovered_identity_count"], 2)
        self.assertEqual(diagnostic["matching_identity_count"], 1)
        self.assertEqual(diagnostic["matching_project_count"], 0)
        self.assertEqual(diagnostic["reason"], "target_ide_running_without_target_project")
        self.assertIn("exact worktree", diagnostic["next_action"])
        self.assertIn("Trust Project", diagnostic["next_action"])
        self.assertIn("safe-mode", diagnostic["next_action"])
        self.assertIn("open-project", diagnostic["next_action"])

    def test_resolve_route_reports_matching_project_route_unavailable(self):
        original_discover = jb_inspect.discover_identities
        original_http_get = jb_inspect.http_get
        original_diagnostic = jb_inspect.discover_diagnostic_identities
        target = "/Users/me/Developer/project"
        jb_inspect.discover_identities = lambda port: [
            {
                "port": 63342,
                "ide_name": "IntelliJ IDEA 2026.1.3",
                "ide_product_code": "IU",
                "session_id": "idea-session",
                "open_projects": [{"base_path": target, "project_key": f"path:{target}"}],
            }
        ]
        jb_inspect.discover_diagnostic_identities = jb_inspect.discover_identities

        def fake_http_get(port, endpoint, params, timeout=3.0):
            self.assertEqual(endpoint, "route")
            return jb_inspect.HttpResult(200, {"route": None}, "url")

        jb_inspect.http_get = fake_http_get
        try:
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.resolve_route(
                    helper_args(port=None, open=False, no_worktree_check=False),
                    {"ide": "IntelliJ IDEA", "worktree_root": target, "project_path": target},
                )
        finally:
            jb_inspect.discover_identities = original_discover
            jb_inspect.http_get = original_http_get
            jb_inspect.discover_diagnostic_identities = original_diagnostic

        self.assertEqual(raised.exception.payload["error_reason"], "matching_project_route_unavailable")
        self.assertEqual(raised.exception.payload["route_diagnostic"]["matching_project_count"], 1)

    def test_resolve_route_port_scan_finds_target_when_registry_has_other_ide(self):
        original_registry = jb_inspect.registry_identities
        original_ports = jb_inspect.configured_ports
        original_identity = jb_inspect.identity_for_port
        original_http_get = jb_inspect.http_get
        target = "/Users/me/Developer/project/worktrees/feature-odoo"
        calls = []
        jb_inspect.registry_identities = lambda: [
            {
                "port": 63342,
                "ide_name": "WebStorm 2026.1.3",
                "ide_product_code": "WS",
                "session_id": "webstorm-session",
                "open_projects": [
                    {
                        "project_key": "path:/Users/me/Developer/project",
                        "base_path": "/Users/me/Developer/project",
                    }
                ],
            }
        ]
        jb_inspect.configured_ports = lambda: [63342, 63345]

        def fake_identity_for_port(port):
            if port == 63342:
                return {
                    "port": 63342,
                    "ide_name": "WebStorm 2026.1.3",
                    "ide_product_code": "WS",
                    "session_id": "webstorm-session",
                    "open_projects": [],
                }
            return {
                "port": 63345,
                "ide_name": "IntelliJ IDEA 2026.2 EAP",
                "ide_product_code": "IU",
                "session_id": "idea-session",
                "open_projects": [],
            }

        def fake_http_get(port, endpoint, params, timeout=jb_inspect.DEFAULT_TIMEOUT_SECONDS):
            calls.append((port, endpoint, params))
            if endpoint != "route":
                raise AssertionError(f"unexpected endpoint: {endpoint}")
            if port == 63342:
                return jb_inspect.HttpResult(200, {"status": "missing", "route": None}, "webstorm-route")
            return jb_inspect.HttpResult(
                200,
                {
                    "status": "resolved",
                    "route": {
                        "port": 63345,
                        "project_key": f"path:{target}",
                        "base_path": target,
                        "session_id": "idea-session",
                        "ide": {"name": "IntelliJ IDEA 2026.2 EAP"},
                    },
                },
                "idea-route",
            )

        jb_inspect.identity_for_port = fake_identity_for_port
        jb_inspect.http_get = fake_http_get
        try:
            route = jb_inspect.resolve_route(
                Namespace(
                    port=None,
                    open=False,
                    project_key=f"path:{target}",
                    project_path=target,
                    worktree_path=target,
                    cwd=target,
                    project=None,
                    ide="IntelliJ IDEA",
                    session_id="idea-session",
                    no_worktree_check=False,
                ),
                {
                    "ide": "IntelliJ IDEA",
                    "worktree_root": target,
                    "project_path": target,
                    "exact_route_path": target,
                    "worktree_strategy": "prefer-current",
                },
            )
        finally:
            jb_inspect.registry_identities = original_registry
            jb_inspect.configured_ports = original_ports
            jb_inspect.identity_for_port = original_identity
            jb_inspect.http_get = original_http_get

        self.assertEqual(route["port"], 63345)
        self.assertEqual(route["base_path"], target)
        self.assertIn((63345, "route"), [(port, endpoint) for port, endpoint, _ in calls])

    def test_resolve_route_skips_wrong_channel_identity_for_exact_selection(self):
        original_discover = jb_inspect.discover_identities
        original_http_get = jb_inspect.http_get
        target = "/Users/me/Developer/project"
        calls = []
        jb_inspect.discover_identities = lambda port: [
            {
                "port": 63342,
                "ide_name": "WebStorm",
                "ide_product_code": "WS",
                "ide_version": "2026.1",
                "session_id": "stable-session",
            },
            {
                "port": 63344,
                "ide_name": "WebStorm",
                "ide_product_code": "WS",
                "ide_version": "2026.2 EAP",
                "session_id": "eap-session",
            },
        ]

        def fake_http_get(port, endpoint, params, timeout=jb_inspect.DEFAULT_TIMEOUT_SECONDS):
            calls.append((port, endpoint, params))
            if port == 63342:
                raise AssertionError("stable IDE should not be queried for exact EAP selection")
            return jb_inspect.HttpResult(
                200,
                {
                    "status": "resolved",
                    "route": {
                        "port": 63344,
                        "project_key": f"path:{target}",
                        "base_path": target,
                        "session_id": "eap-session",
                        "ide": {"name": "WebStorm 2026.2 EAP"},
                    },
                },
                "eap-route",
            )

        jb_inspect.http_get = fake_http_get
        try:
            route = jb_inspect.resolve_route(
                Namespace(
                    port=None,
                    open=False,
                    project_key=f"path:{target}",
                    project_path=target,
                    worktree_path=target,
                    cwd=target,
                    project=None,
                    ide="WebStorm",
                    session_id="eap-session",
                    no_worktree_check=False,
                ),
                {
                    "ide": "WebStorm",
                    "ide_selection": {
                        "product_key": "webstorm",
                        "exact": True,
                        "channel": "eap",
                        "version": "2026.2",
                    },
                    "worktree_root": target,
                    "project_path": target,
                    "exact_route_path": target,
                    "worktree_strategy": "prefer-current",
                },
            )
        finally:
            jb_inspect.discover_identities = original_discover
            jb_inspect.http_get = original_http_get

        self.assertEqual(route["port"], 63344)
        self.assertEqual([call[0] for call in calls], [63344])

    def test_route_diagnostic_for_no_instances_mentions_hidden_prompt_as_secondary_cause(self):
        original_discover = jb_inspect.discover_diagnostic_identities
        jb_inspect.discover_diagnostic_identities = lambda port: []
        try:
            payload = jb_inspect.route_diagnostic_payload(
                Namespace(port=None),
                {"ide": "PyCharm", "worktree_root": "/Users/me/Developer/mediaforce", "project_path": "/Users/me/Developer/mediaforce"},
            )
        finally:
            jb_inspect.discover_diagnostic_identities = original_discover

        diagnostic = payload["route_diagnostic"]
        self.assertEqual(diagnostic["reason"], "no_plugin_instances_discovered")
        self.assertTrue(diagnostic["next_action"].startswith("Launch the configured JetBrains IDE with the inspection plugin installed"))
        self.assertIn("Trust Project", diagnostic["next_action"])
        self.assertIn("safe-mode", diagnostic["next_action"])
        self.assertIn("open-project", diagnostic["next_action"])

    def test_open_project_for_lifecycle_uses_running_ide_without_bootstrap(self):
        calls = []
        original_running = jb_inspect.open_via_running_ide
        original_bootstrap = jb_inspect.bootstrap_ide_app
        original_wait = jb_inspect.wait_for_matching_ide_identity
        def fake_running(args, context, attempts=None, method="running_ide", lease=None):
            calls.append(("running", method))
            attempt = {"method": method, "accepted": True, "ownership_registered": True}
            if attempts is not None:
                attempts.append(attempt)
            return attempt

        jb_inspect.open_via_running_ide = fake_running
        jb_inspect.bootstrap_ide_app = lambda *args, **kwargs: calls.append("bootstrap")
        jb_inspect.wait_for_matching_ide_identity = lambda *args, **kwargs: calls.append("wait")
        try:
            result = jb_inspect.open_project_for_lifecycle(Namespace(port=None, background_open=True), {"ide": "IntelliJ IDEA"})
        finally:
            jb_inspect.open_via_running_ide = original_running
            jb_inspect.bootstrap_ide_app = original_bootstrap
            jb_inspect.wait_for_matching_ide_identity = original_wait

        self.assertEqual(result[0], "running_ide")
        self.assertEqual(result[1], [{"method": "running_ide", "accepted": True, "ownership_registered": True}])
        self.assertTrue(result[2])
        self.assertEqual(calls, [("running", "running_ide")])

    def test_open_project_for_lifecycle_treats_already_open_as_preexisting(self):
        attempt = {
            "method": "running_ide",
            "accepted": True,
            "ownership_registered": False,
            "open_outcome": "already_open",
        }
        with (
            patch.object(jb_inspect, "open_via_running_ide", return_value=attempt),
            patch.object(jb_inspect, "bootstrap_ide_app") as bootstrap,
        ):
            result = jb_inspect.open_project_for_lifecycle(
                Namespace(port=None, background_open=True),
                {"ide": "IntelliJ IDEA"},
            )

        bootstrap.assert_not_called()
        self.assertEqual(result, ("preexisting", [], False))

    def test_open_via_running_ide_returns_false_for_unavailable_explicit_port(self):
        original_discover = jb_inspect.discover_open_identities

        def fake_discover(args, context):
            raise jb_inspect.InspectError("Inspection API unavailable on port 63345: connection refused", 3)

        jb_inspect.discover_open_identities = fake_discover
        try:
            result = jb_inspect.open_via_running_ide(
                Namespace(port=63345),
                {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/worktree"},
            )
        finally:
            jb_inspect.discover_open_identities = original_discover

        self.assertIsNone(result)

    def test_open_via_running_ide_reraises_explicit_port_identity_mismatch(self):
        original_discover = jb_inspect.discover_open_identities

        def fake_discover(args, context):
            raise jb_inspect.InspectError(
                "Inspection API identity on port 63345 reported port 63342.",
                3,
                {"error_reason": "identity_port_mismatch", "requested_port": 63345, "reported_port": 63342},
            )

        jb_inspect.discover_open_identities = fake_discover
        try:
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.open_via_running_ide(
                    Namespace(port=63345),
                    {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/worktree"},
                )
        finally:
            jb_inspect.discover_open_identities = original_discover

        self.assertEqual(raised.exception.payload["error_reason"], "identity_port_mismatch")

    def test_open_project_for_lifecycle_bootstraps_then_lifecycle_opens(self):
        calls = []
        original_running = jb_inspect.open_via_running_ide
        original_bootstrap = jb_inspect.bootstrap_ide_app
        original_wait = jb_inspect.wait_for_matching_ide_identity
        original_now = jb_inspect.monotonic_ms
        original_sleep = jb_inspect.time.sleep

        def fake_running(args, context, attempts=None, method="running_ide", lease=None):
            calls.append(("running", method))
            accepted = len([call for call in calls if call[0] == "running"]) == 2
            attempt = {"method": method, "accepted": accepted, "ownership_registered": accepted}
            if attempts is not None:
                attempts.append(attempt)
            return attempt if accepted else None

        jb_inspect.open_via_running_ide = fake_running
        jb_inspect.bootstrap_ide_app = lambda context, background=True: calls.append(("bootstrap", background)) or {"method": "bootstrap_ide", "accepted": True}
        jb_inspect.wait_for_matching_ide_identity = lambda args, context, timeout_ms: calls.append(("wait", timeout_ms)) or {"port": 63342}
        jb_inspect.monotonic_ms = lambda: 0
        jb_inspect.time.sleep = lambda seconds: calls.append(("sleep", seconds))
        try:
            result = jb_inspect.open_project_for_lifecycle(Namespace(port=None, background_open=True, prepare_timeout_ms=1234), {"ide": "IntelliJ IDEA"})
        finally:
            jb_inspect.open_via_running_ide = original_running
            jb_inspect.bootstrap_ide_app = original_bootstrap
            jb_inspect.wait_for_matching_ide_identity = original_wait
            jb_inspect.monotonic_ms = original_now
            jb_inspect.time.sleep = original_sleep

        self.assertEqual(result[0], "bootstrapped_ide")
        self.assertTrue(result[2])
        self.assertEqual([attempt["method"] for attempt in result[1]], ["running_ide", "bootstrap_ide", "bootstrapped_ide"])
        self.assertEqual(calls, [("running", "running_ide"), ("bootstrap", True), ("wait", 1234), ("running", "bootstrapped_ide")])

    def test_open_project_for_lifecycle_retries_open_after_cold_bootstrap(self):
        calls = []
        original_running = jb_inspect.open_via_running_ide
        original_bootstrap = jb_inspect.bootstrap_ide_app
        original_wait = jb_inspect.wait_for_matching_ide_identity
        original_now = jb_inspect.now_ms
        original_sleep = jb_inspect.time.sleep
        ticks = iter([0, 0, 100, 200, 300])

        def fake_running(args, context, attempts=None, method="running_ide", lease=None):
            calls.append(("running", method))
            accepted = len([call for call in calls if call[0] == "running"]) == 4
            attempt = {"method": method, "accepted": accepted, "ownership_registered": accepted}
            if attempts is not None:
                attempts.append(attempt)
            return attempt if accepted else None

        jb_inspect.open_via_running_ide = fake_running
        jb_inspect.bootstrap_ide_app = lambda context, background=True: calls.append(("bootstrap", background)) or {"method": "bootstrap_ide", "accepted": True}
        jb_inspect.wait_for_matching_ide_identity = lambda args, context, timeout_ms: calls.append(("wait", timeout_ms)) or {"port": 63342}
        jb_inspect.now_ms = lambda: next(ticks)
        jb_inspect.time.sleep = lambda seconds: calls.append(("sleep", seconds))
        try:
            result = jb_inspect.open_project_for_lifecycle(Namespace(port=None, background_open=True, prepare_timeout_ms=1000), {"ide": "IntelliJ IDEA"})
        finally:
            jb_inspect.open_via_running_ide = original_running
            jb_inspect.bootstrap_ide_app = original_bootstrap
            jb_inspect.wait_for_matching_ide_identity = original_wait
            jb_inspect.now_ms = original_now
            jb_inspect.time.sleep = original_sleep

        self.assertEqual(result[0], "bootstrapped_ide")
        self.assertTrue(result[2])
        self.assertEqual([attempt["accepted"] for attempt in result[1]], [False, True, False, False, True])
        self.assertEqual(
            calls,
            [
                ("running", "running_ide"),
                ("bootstrap", True),
                ("wait", 1000),
                ("running", "bootstrapped_ide"),
                ("sleep", 1),
                ("running", "bootstrapped_ide"),
                ("sleep", 1),
                ("running", "bootstrapped_ide"),
            ],
        )

    def test_open_project_for_lifecycle_errors_when_bootstrapped_ide_rejects_open(self):
        original_running = jb_inspect.open_via_running_ide
        original_bootstrap = jb_inspect.bootstrap_ide_app
        original_wait = jb_inspect.wait_for_matching_ide_identity
        original_now = jb_inspect.now_ms
        original_sleep = jb_inspect.time.sleep
        ticks = iter([0, 0, 500, 1500])
        jb_inspect.open_via_running_ide = lambda args, context, attempts=None, method="running_ide", lease=None: (attempts.append({"method": method, "accepted": False}) if attempts is not None else None)
        jb_inspect.bootstrap_ide_app = lambda context, background=True: {"method": "bootstrap_ide", "accepted": True}
        jb_inspect.wait_for_matching_ide_identity = lambda args, context, timeout_ms: {"port": 63342}
        jb_inspect.now_ms = lambda: next(ticks)
        jb_inspect.time.sleep = lambda seconds: None
        try:
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.open_project_for_lifecycle(
                    Namespace(port=None, background_open=True, prepare_timeout_ms=1234),
                    {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/worktree"},
                )
        finally:
            jb_inspect.open_via_running_ide = original_running
            jb_inspect.bootstrap_ide_app = original_bootstrap
            jb_inspect.wait_for_matching_ide_identity = original_wait
            jb_inspect.now_ms = original_now
            jb_inspect.time.sleep = original_sleep

        self.assertIn("did not accept", str(raised.exception))
        self.assertEqual(raised.exception.payload["prepare_timeout_ms"], 1234)
        self.assertEqual([attempt["method"] for attempt in raised.exception.payload["open_attempts"]], ["running_ide", "bootstrap_ide", "bootstrapped_ide", "bootstrapped_ide"])

    def test_wait_for_exact_route_reports_project_open_blocked_after_scheduled_open(self):
        original_find = jb_inspect.find_exact_route
        original_sleep = jb_inspect.time.sleep
        original_trusted = jb_inspect.trusted_auto_open_roots
        original_diagnostic = jb_inspect.discover_diagnostic_identities
        jb_inspect.find_exact_route = lambda args, context: None
        jb_inspect.time.sleep = lambda seconds: None
        jb_inspect.trusted_auto_open_roots = lambda: ["/tmp"]
        jb_inspect.discover_diagnostic_identities = lambda port: [
            {
                "port": 63344,
                "ide_name": "PyCharm 2026.1.2",
                "ide_product_code": "PY",
                "session_id": "py-session",
                "open_projects": [],
            }
        ]
        try:
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.wait_for_exact_route(
                    Namespace(port=None, background_open=True),
                    {"ide": "PyCharm", "worktree_root": "/tmp/repo", "project_path": "/tmp/repo"},
                    1,
                )
        finally:
            jb_inspect.find_exact_route = original_find
            jb_inspect.time.sleep = original_sleep
            jb_inspect.trusted_auto_open_roots = original_trusted
            jb_inspect.discover_diagnostic_identities = original_diagnostic

        payload = jb_inspect.error_payload(raised.exception, Namespace(command="closeout"))
        self.assertEqual(payload["error_reason"], "project_open_blocked")
        self.assertEqual(payload["blocked_diagnostic"]["reason"], "jetbrains_project_open_blocked")
        self.assertTrue(payload["blocked_diagnostic"]["background_open"])
        self.assertEqual(payload["blocked_diagnostic"]["prepare_timeout_ms"], 1)
        self.assertEqual(payload["blocked_diagnostic"]["requested_ide"], "PyCharm")
        self.assertEqual(payload["blocked_diagnostic"]["target_worktree"], "/tmp/repo")
        self.assertEqual(payload["blocked_diagnostic"]["selected_trusted_root"], str(Path("/tmp").resolve()))
        self.assertEqual(payload["route_diagnostic"]["reason"], "target_ide_running_without_target_project")

    def test_wait_for_exact_route_after_registered_open_does_not_launch_app_fallback(self):
        original_find = jb_inspect.find_exact_route
        original_sleep = jb_inspect.time.sleep
        original_diagnostic = jb_inspect.discover_diagnostic_identities
        calls = []

        def fake_find(args, context):
            calls.append("find")
            return None

        jb_inspect.find_exact_route = fake_find
        jb_inspect.time.sleep = lambda seconds: None
        jb_inspect.discover_diagnostic_identities = lambda port: []
        open_attempts = [{"method": "running_ide", "accepted": True, "ownership_registered": True}]
        try:
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.wait_for_exact_route_after_open(
                    Namespace(port=None, background_open=True),
                    {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/repo", "project_path": "/tmp/repo"},
                    1,
                    open_attempts,
                )
        finally:
            jb_inspect.find_exact_route = original_find
            jb_inspect.time.sleep = original_sleep
            jb_inspect.discover_diagnostic_identities = original_diagnostic

        self.assertNotIn("fallback", calls)
        self.assertEqual(raised.exception.payload["open_attempts"], open_attempts)

    def test_wait_until_route_ready_requires_consecutive_ready_statuses(self):
        statuses = iter([
            {"indexing": True},
            {"indexing": False, "is_scanning": False},
            {"indexing": True},
            {"indexing": False, "is_scanning": False},
            {"indexing": False, "is_scanning": False},
            {"indexing": False, "is_scanning": False},
        ])
        calls = []
        original_call = jb_inspect.call_endpoint
        original_sleep = jb_inspect.time.sleep
        original_now = jb_inspect.now_ms
        jb_inspect.call_endpoint = lambda route, endpoint, params: calls.append(endpoint) or next(statuses)
        jb_inspect.time.sleep = lambda seconds: None
        jb_inspect.now_ms = lambda: 0
        try:
            jb_inspect.wait_until_route_ready(
                helper_args(port=None),
                {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/repo", "project_path": "/tmp/repo"},
                {"port": 63342},
                1_000,
            )
        finally:
            jb_inspect.call_endpoint = original_call
            jb_inspect.time.sleep = original_sleep
            jb_inspect.now_ms = original_now

        self.assertEqual(calls, ["status", "status", "status", "status", "status", "status"])

    def test_route_status_ready_requires_helper_owned_content_roots(self):
        self.assertFalse(
            jb_inspect.route_status_ready(
                {
                    "indexing": False,
                    "is_scanning": False,
                    "route": {
                        "lifecycle_readiness": {
                            "ready": False,
                            "reason": "no_content_roots",
                            "content_root_count": 0,
                        }
                    },
                }
            )
        )
        self.assertTrue(
            jb_inspect.route_status_ready(
                {
                    "indexing": False,
                    "is_scanning": False,
                    "route": {
                        "lifecycle_readiness": {
                            "ready": True,
                            "reason": "ready",
                            "content_root_count": 1,
                        }
                    },
                }
            )
        )

    def test_wait_until_route_ready_reports_missing_content_roots(self):
        for reason, content_root_count in (("no_content_roots", 0), ("content_roots_outside_target", 2)):
            with self.subTest(reason=reason):
                status = {
                    "indexing": False,
                    "is_scanning": False,
                    "route": {
                        "lifecycle_readiness": {
                            "ready": False,
                            "reason": reason,
                            "content_root_count": content_root_count,
                        }
                    },
                }
                times = iter([0, 0, 1, 1])
                with (
                    patch.object(jb_inspect, "call_endpoint", return_value=status),
                    patch.object(jb_inspect.time, "sleep"),
                    patch.object(jb_inspect, "monotonic_ms", side_effect=lambda: next(times)),
                ):
                    with self.assertRaises(jb_inspect.InspectError) as raised:
                        jb_inspect.wait_until_route_ready(
                            helper_args(port=None),
                            {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/repo", "project_path": "/tmp/repo"},
                            {"port": 63342},
                            0,
                        )

                self.assertEqual(raised.exception.payload["error_reason"], "project_content_roots_missing")
                self.assertEqual(raised.exception.payload["lifecycle_readiness"]["reason"], reason)

    def test_jetbrains_config_dirs_requires_ide_when_multiple_configs_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "Library" / "Application Support" / "JetBrains"
            for name in ("PyCharm2026.1", "IntelliJIdea2026.1"):
                (base / name / "options").mkdir(parents=True)
            with patch.dict(os.environ, {"JETBRAINS_INSPECTION_IDE_CONFIG_DIR": ""}, clear=False), \
                patch.object(jb_inspect.sys, "platform", "darwin"), \
                patch.object(jb_inspect.Path, "home", return_value=Path(tmp)):
                os.environ.pop("JETBRAINS_INSPECTION_IDE_CONFIG_DIR", None)
                with self.assertRaises(jb_inspect.InspectError) as raised:
                    jb_inspect.jetbrains_config_dirs({})

        self.assertIn("multiple IDE config directories", str(raised.exception))
        self.assertEqual(raised.exception.payload["error_reason"], "ide_selection_required")
        self.assertIn("Add preferred JetBrains IDE metadata", raised.exception.payload["next_action"])

    def test_jetbrains_config_dirs_matches_requested_ide(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "Library" / "Application Support" / "JetBrains"
            pycharm = base / "PyCharm2026.1"
            idea = base / "IntelliJIdea2026.1"
            (pycharm / "options").mkdir(parents=True)
            (idea / "options").mkdir(parents=True)
            with patch.dict(os.environ, {"JETBRAINS_INSPECTION_IDE_CONFIG_DIR": ""}, clear=False), \
                patch.object(jb_inspect.sys, "platform", "darwin"), \
                patch.object(jb_inspect.Path, "home", return_value=Path(tmp)):
                os.environ.pop("JETBRAINS_INSPECTION_IDE_CONFIG_DIR", None)
                result = jb_inspect.jetbrains_config_dirs({"ide": "IntelliJ IDEA"})

        self.assertEqual(result, [idea])

    def test_jetbrains_config_dirs_prefers_latest_stable_for_product_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            stable = make_config_dir(home, "PyCharm2026.2")
            make_config_dir(home, "PyCharm2026.1")
            with patch.dict(os.environ, {"JETBRAINS_INSPECTION_IDE_CONFIG_DIR": ""}, clear=False), \
                patch.object(jb_inspect.sys, "platform", "darwin"), \
                patch.object(jb_inspect.Path, "home", return_value=home):
                os.environ.pop("JETBRAINS_INSPECTION_IDE_CONFIG_DIR", None)
                result = jb_inspect.jetbrains_config_dirs({"ide": "PyCharm"})

        self.assertEqual(result, [stable])

    def test_jetbrains_config_dirs_honors_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "CustomConfig"
            override.mkdir()
            with patch.dict(os.environ, {"JETBRAINS_INSPECTION_IDE_CONFIG_DIR": str(override)}, clear=False):
                result = jb_inspect.jetbrains_config_dirs({"ide": "WebStorm"})

        self.assertEqual(result, [override.resolve()])


class AgentInspectContractTest(unittest.TestCase):
    def emit_agent_payload(self, payload, helper_exit_code=None):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(jb_inspect, "log_assessment_records"):
            exit_code = jb_inspect.emit_agent_result(
                payload,
                command="agent-inspect",
                helper_exit_code=helper_exit_code,
            )
        return exit_code, json.loads(output.getvalue())

    def test_red_exits_zero_with_compact_finding(self):
        exit_code, payload = self.emit_agent_payload(
            {
                "status": "findings",
                "total_problems": 1,
                "problems_shown": 1,
                "problems": [
                    {
                        "file": "/tmp/project/example.py",
                        "line": 4,
                        "severity": "warning",
                        "inspectionType": "ExampleInspection",
                        "category": "Python",
                        "description": "Example finding",
                    }
                ],
                "cleanup": {"status": "closed"},
            }
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["agent_result"]["verdict"], "RED")
        self.assertTrue(payload["agent_result"]["terminal"])
        self.assertFalse(payload["agent_result"]["retry_policy"]["retry"])
        self.assertEqual(payload["finding_count"], 1)
        self.assertEqual(payload["findings"][0]["description"], "Example finding")
        self.assertEqual(payload["findings"][0]["inspection"], "ExampleInspection")
        self.assertEqual(payload["findings"][0]["category"], "Python")
        self.assertEqual(payload["cleanup"]["status"], "closed")
        self.assertEqual(payload["helper_exit_code"], 1)

    def test_terminal_unknown_exits_zero(self):
        exit_code, payload = self.emit_agent_payload(
            {
                "status": "error",
                "error_reason": "target_project_not_open",
                "retry_exhausted": True,
                "cleanup": {"status": "not_needed"},
            },
            helper_exit_code=3,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["agent_result"]["verdict"], "UNKNOWN")
        self.assertTrue(payload["agent_result"]["terminal"])
        self.assertFalse(payload["agent_result"]["retry_policy"]["retry"])
        self.assertEqual(payload["helper_exit_code"], 3)

    def test_green_exits_zero_with_terminal_envelope(self):
        exit_code, payload = self.emit_agent_payload(
            {
                "status": "clean",
                "total_problems": 0,
                "problems_shown": 0,
                "problems": [],
                "cleanup": {"status": "closed"},
            }
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["agent_result"]["verdict"], "GREEN")
        self.assertTrue(payload["agent_result"]["terminal"])
        self.assertFalse(payload["agent_result"]["retry_policy"]["retry"])
        self.assertEqual(payload["finding_count"], 0)
        self.assertEqual(payload["findings"], [])
        self.assertFalse(payload["findings_truncated"])

    def test_retryable_unknown_remains_explicit(self):
        exit_code, payload = self.emit_agent_payload(
            {
                "status": "error",
                "error_reason": "timeout",
                "cleanup": {"status": "not_needed"},
            },
            helper_exit_code=1,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["agent_result"]["verdict"], "UNKNOWN")
        self.assertFalse(payload["agent_result"]["terminal"])
        self.assertTrue(payload["agent_result"]["retry_policy"]["retry"])

    def test_agent_guidance_uses_agent_inspect_command(self):
        _, payload = self.emit_agent_payload(
            {
                "status": "error",
                "error_reason": "cleanup_failed",
                "error_message": "Install the updated plugin before inspect-closeout.",
                "hint": jb_inspect.hint_for_error_reason("worktree_route_mismatch"),
                "cleanup": {"status": "failed"},
            }
        )

        serialized = json.dumps(payload)
        self.assertNotIn("inspect-closeout", serialized)
        self.assertIn("agent-inspect", payload["agent_result"]["next_action"])
        self.assertIn("agent-inspect", payload["agent_result"]["agent_report"])
        self.assertIn("agent-inspect", payload["diagnostic"]["error_message"])
        self.assertIn("agent-inspect", payload["diagnostic"]["hint"])

    def test_legacy_agent_result_shape_and_guidance_remain_unchanged(self):
        payload = {
            "command": "inspect-closeout",
            "status": "error",
            "error_reason": "cleanup_failed",
            "cleanup": {"status": "failed"},
        }

        jb_inspect.apply_verdict(payload)

        self.assertNotIn("terminal", payload["agent_result"])
        self.assertIn("inspect-closeout", payload["agent_result"]["next_action"])

    def test_usage_error_emits_terminal_envelope(self):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(jb_inspect, "log_assessment_records"):
            exit_code = jb_inspect.emit_agent_usage_error(
                "usage: jb-inspect.py agent-inspect\njb-inspect.py: error: unrecognized arguments: --no-wait-stale\n"
            )
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["agent_result"]["verdict"], "UNKNOWN")
        self.assertTrue(payload["agent_result"]["terminal"])
        self.assertFalse(payload["agent_result"]["retry_policy"]["retry"])
        self.assertIn("do not run another inspection command", payload["agent_result"]["next_action"])
        self.assertEqual(payload["diagnostic"]["error_reason"], jb_inspect.AGENT_USAGE_ERROR_REASON)
        self.assertIn("unrecognized arguments", payload["diagnostic"]["error_message"])

    def test_usage_error_is_logged_only_as_unknown_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome_log_path = Path(tmp) / "outcomes.jsonl"
            unknown_log_path = Path(tmp) / "unknown-verdicts.jsonl"
            output = io.StringIO()
            with (
                patch.dict(
                    os.environ,
                    {
                        "JB_INSPECT_OUTCOME_LOG": str(outcome_log_path),
                        "JB_INSPECT_UNKNOWN_LOG": str(unknown_log_path),
                    },
                ),
                redirect_stdout(output),
            ):
                exit_code = jb_inspect.emit_agent_usage_error(
                    "usage: jb-inspect.py agent-inspect\njb-inspect.py: error: unrecognized arguments: --no-wait-stale\n"
                )

            self.assertEqual(exit_code, 0)
            self.assertFalse(outcome_log_path.exists())
            records = [json.loads(line) for line in unknown_log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertTrue(records[0].get("client_run_id"))
            self.assertEqual(records[0].get("command"), "agent-inspect")
            self.assertEqual(records[0].get("verdict"), "UNKNOWN")

    def test_invented_flag_subprocess_returns_json_and_zero(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "agent-inspect", "--no-wait-stale"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["agent_result"]["verdict"], "UNKNOWN")
        self.assertTrue(payload["agent_result"]["terminal"])
        self.assertFalse(payload["agent_result"]["retry_policy"]["retry"])

    def test_agent_alias_invented_flag_returns_json_and_zero(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "agent", "--no-wait-stale"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["agent_result"]["verdict"], "UNKNOWN")
        self.assertFalse(payload["agent_result"]["retry_policy"]["retry"])

    def test_legacy_invented_flag_keeps_argparse_exit_and_stderr(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "inspect-closeout", "--no-wait-stale"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("unrecognized arguments: --no-wait-stale", result.stderr)

    def test_compact_findings_report_truncation(self):
        findings = [
            {
                "file": f"/tmp/project/example-{index}.py",
                "line": index + 1,
                "severity": "warning",
                "description": f"Finding {index}",
            }
            for index in range(25)
        ]
        _, payload = self.emit_agent_payload(
            {
                "status": "findings",
                "total_problems": 25,
                "problems_shown": 25,
                "problems": findings,
                "cleanup": {"status": "closed"},
            }
        )

        self.assertEqual(len(payload["findings"]), 20)
        self.assertEqual(payload["findings_limit"], 20)
        self.assertTrue(payload["findings_truncated"])

    def test_unexpected_agent_exception_emits_terminal_envelope(self):
        output = io.StringIO()
        with (
            patch.object(sys, "argv", [str(SCRIPT_PATH), "agent-inspect"]),
            patch.object(jb_inspect, "build_context", side_effect=ValueError("unexpected failure")),
            patch.object(jb_inspect, "log_assessment_records"),
            redirect_stdout(output),
        ):
            exit_code = jb_inspect.main()
        payload = json.loads(output.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["agent_result"]["verdict"], "UNKNOWN")
        self.assertTrue(payload["agent_result"]["terminal"])
        self.assertEqual(payload["helper_exit_code"], 3)
        self.assertEqual(payload["diagnostic"]["error_message"], "unexpected failure")

    def test_legacy_exit_contract_is_unchanged(self):
        self.assertEqual(jb_inspect.classify_run_exit({"status": "clean"}), 0)
        self.assertEqual(jb_inspect.classify_run_exit({"status": "findings"}), 1)
        self.assertEqual(jb_inspect.classify_run_exit({"status": "error"}), 1)


class ClassificationTest(unittest.TestCase):
    def test_clean_run_exits_zero(self):
        self.assertEqual(jb_inspect.classify_run_exit({"status": "clean"}), 0)

    def test_findings_exit_nonzero(self):
        self.assertEqual(jb_inspect.classify_run_exit({"status": "findings"}), 1)

    def test_stale_problems_exit_nonzero(self):
        result = {"status": "stale_results", "capture_incomplete": False, "results_may_be_stale": True}
        self.assertEqual(jb_inspect.classify_problems_exit(result), 1)

    def test_results_with_findings_exit_nonzero(self):
        result = {"status": "results_available", "problems": [{"description": "x"}]}
        self.assertEqual(jb_inspect.classify_problems_exit(result), 1)

    def test_run_status_uses_total_when_current_page_is_empty(self):
        problems = {"status": "results_available", "total_problems": 5, "problems": []}

        self.assertEqual(jb_inspect.classify_run_status({}, problems), "findings")

    def test_run_status_preserves_clean_wait_when_problems_has_no_results(self):
        wait = {"completion_reason": "clean", "clean_inspection": True, "inspection_verdict": "GREEN"}
        problems = {"status": "no_results", "total_problems": 0, "problems": []}

        self.assertEqual(jb_inspect.classify_run_status(wait, problems), "clean")

    def test_cleanup_leases_failed_cleanup_exits_nonzero(self):
        result = {"status": "ok", "failed": [{"status": "failed", "reason": "close_failed"}]}

        self.assertEqual(jb_inspect.classify_cleanup_leases_exit(result), 1)

    def test_wait_no_results_exits_nonzero(self):
        result = {"status": "no_results", "wait": {"completion_reason": "no_results"}}

        self.assertEqual(jb_inspect.classify_wait_exit(result), 1)

    def test_status_with_clean_result_exits_zero(self):
        body = {"clean_inspection": True, "is_scanning": False}
        result = {
            "status": jb_inspect.status_label(body),
            "clean": jb_inspect.classify_status_body_clean(body),
        }
        self.assertEqual(jb_inspect.classify_status_exit(result), 0)

    def test_status_with_results_without_verdict_is_unknown(self):
        body = {"has_inspection_results": True, "is_scanning": False}
        result = {
            "status": jb_inspect.status_label(body),
            "clean": jb_inspect.classify_status_body_clean(body),
        }
        self.assertFalse(result["clean"])
        self.assertEqual(jb_inspect.classify_status_exit(result), 1)

    def test_status_session_drift_exits_nonzero(self):
        body = {"session_drift": True, "clean_inspection": True}
        result = {"clean": jb_inspect.classify_status_body_clean(body)}
        self.assertEqual(jb_inspect.classify_status_exit(result), 1)

    def test_status_stale_exits_nonzero(self):
        body = {"results_may_be_stale": True, "has_inspection_results": True}
        result = {"clean": jb_inspect.classify_status_body_clean(body)}
        self.assertEqual(jb_inspect.classify_status_exit(result), 1)

    def test_status_in_progress_exits_nonzero(self):
        body = {"is_scanning": True}
        result = {"clean": jb_inspect.classify_status_body_clean(body)}
        self.assertEqual(jb_inspect.classify_status_exit(result), 1)

    def test_status_label_prefers_explicit_status(self):
        self.assertEqual(jb_inspect.status_label({"status": "custom"}), "custom")

    def test_status_label_synthesizes_from_boolean_state(self):
        cases = [
            ({"session_drift": True}, "session_drift"),
            ({"ambiguous": True}, "ambiguous"),
            ({"unavailable": True}, "unavailable"),
            ({"results_may_be_stale": True}, "stale_results"),
            ({"capture_incomplete": True}, "capture_incomplete"),
            ({"timed_out": True}, "timed_out"),
            ({"indexing": True}, "indexing"),
            ({"is_scanning": True}, "running"),
            ({"inspection_in_progress": True}, "running"),
            ({"clean_inspection": True}, "clean"),
            ({"has_inspection_results": True}, "results_available"),
        ]
        for body, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(jb_inspect.status_label(body), expected)

    def test_status_clean_exits_zero(self):
        body = {"status": "clean"}
        result = {"status": "clean", "clean": jb_inspect.classify_status_body_clean(body)}

        self.assertEqual(jb_inspect.classify_status_exit(result), 0)

    def test_status_results_available_without_proof_exits_nonzero(self):
        body = {"status": "results_available"}
        result = {"status": "results_available", "clean": jb_inspect.classify_status_body_clean(body)}

        self.assertEqual(jb_inspect.classify_status_exit(result), 1)

    def test_status_results_available_with_zero_count_exits_zero(self):
        body = {"status": "results_available", "total_problems": 0}
        result = {
            "status": "results_available",
            "clean": jb_inspect.classify_status_body_clean(body),
            "total_problems": 0,
        }

        self.assertEqual(jb_inspect.classify_status_exit(result), 0)

    def test_run_wait_blocker_overrides_plugin_green_verdict(self):
        problems = {
            "status": "results_available",
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON,
            "proof_failures": [jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON],
        }
        wait = {"timed_out": True}

        summary = jb_inspect.summarize_problems({}, {}, problems)
        summary["wait"] = wait
        summary["status"] = jb_inspect.classify_run_status(wait, problems)
        jb_inspect.apply_verdict(summary)

        self.assertEqual(summary["status"], "timed_out")
        self.assertEqual(summary["verdict"], "UNKNOWN")
        self.assertEqual(summary["verdict_reason"], "timeout")
        self.assertEqual(jb_inspect.classify_run_exit(summary), 1)

    def test_text_only_override_recovers_settled_semantic_coverage_wait_timeout(self):
        text_only_file = {
            "path": "/tmp/View.swift",
            "valid": True,
            "directory": False,
            "file_type": "TextMate",
            "psi_language": "textmate",
            "psi_class": "org.jetbrains.plugins.textmate.psi.TextMateFile",
            "in_content": True,
            "reasons": ["non_semantic_fallback"],
        }
        problems = {
            "status": "results_available",
            "snapshot_outcome": "clean_confirmed",
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON,
            "proof_failures": [jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON],
            "capture_diagnostic": {
                "scope_file_resolved_count": 1,
                "scope_file_diagnostics": [text_only_file],
                "scope_file_diagnostics_complete": True,
                "scope_file_semantic_evidence_complete": True,
                "scope_file_semantic_coverage": {
                    "schema_version": 1,
                    "evaluated_file_count": 1,
                    "unproven_file_count": 0,
                    "missing_file_count": 1,
                    "reason_counts": {"non_semantic_fallback": 1},
                    "missing_files": [text_only_file],
                    "metadata_file_count": 0,
                    "metadata_files": [],
                },
            },
        }
        wait = {
            "timed_out": True,
            "wait_completed": False,
            "completion_reason": "timeout",
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON,
        }

        summary = jb_inspect.summarize_problems({}, {}, problems, allow_text_only_coverage=True)
        summary["wait"] = wait
        summary["status"] = jb_inspect.classify_run_status(wait, problems)
        jb_inspect.apply_verdict(summary)

        self.assertEqual(summary["status"], "clean")
        self.assertEqual(summary["verdict"], "GREEN")
        self.assertEqual(summary["verdict_reason"], "text_only_coverage_allowed")
        self.assertTrue(summary["semantic_coverage_wait_timeout_recovered"])
        self.assertEqual(jb_inspect.classify_run_exit(summary), 0)

    def test_text_only_override_does_not_recover_running_wait_timeout(self):
        payload = {
            "status": "timed_out",
            "clean": True,
            "snapshot_outcome": "clean_confirmed",
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON,
            "proof_failures": [jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON],
            "semantic_coverage": {"status": "text_only_allowed"},
            "wait": {
                "status": "running",
                "timed_out": True,
                "wait_completed": False,
                "completion_reason": "timeout",
                "inspection_verdict": "UNKNOWN",
                "inspection_verdict_reason": "inspection_still_running",
            },
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["status"], "timed_out")
        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], "timeout")
        self.assertNotIn("semantic_coverage_wait_timeout_recovered", payload)

    def test_text_only_override_does_not_recover_transport_timeout(self):
        payload = {
            "status": "error",
            "error_reason": "inspection_api_timeout",
            "clean": True,
            "snapshot_outcome": "clean_confirmed",
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON,
            "proof_failures": [jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON],
            "semantic_coverage": {"status": "text_only_allowed"},
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], "inspection_api_timeout")
        self.assertNotIn("semantic_coverage_wait_timeout_recovered", payload)

    def test_status_findings_is_usable_but_not_clean(self):
        body = {"status": "findings"}
        result = {"status": "findings", "clean": jb_inspect.classify_status_body_clean(body)}
        self.assertFalse(result["clean"])
        self.assertEqual(jb_inspect.classify_status_exit(result), 0)

    def test_status_command_surfaces_blocker_flags(self):
        def fake_resolve_route(args, context):
            return {"port": 63343, "project_key": "path:/tmp/example"}

        def fake_call_endpoint(route, endpoint, params, timeout=None):
            return {
                "status": "findings",
                "is_scanning": True,
                "indexing": True,
                "inspection_in_progress": True,
                "session_drift": True,
                "ambiguous": True,
                "unavailable": True,
                "capture_incomplete": True,
                "results_may_be_stale": True,
                "timed_out": True,
            }

        original_resolve_route = jb_inspect.resolve_route
        original_call_endpoint = jb_inspect.call_endpoint
        jb_inspect.resolve_route = fake_resolve_route
        jb_inspect.call_endpoint = fake_call_endpoint
        try:
            result = jb_inspect.command_status(
                Namespace(
                    project_key=None,
                    session_id=None,
                    project_path=None,
                    worktree_path=None,
                    cwd=None,
                    project=None,
                    ide=None,
                ),
                {},
            )
        finally:
            jb_inspect.resolve_route = original_resolve_route
            jb_inspect.call_endpoint = original_call_endpoint

        self.assertEqual(result["status"], "findings")
        self.assertFalse(result["clean"])
        for flag in (
            "is_scanning",
            "indexing",
            "inspection_in_progress",
            "session_drift",
            "ambiguous",
            "unavailable",
            "capture_incomplete",
            "results_may_be_stale",
            "timed_out",
        ):
            with self.subTest(flag=flag):
                self.assertIs(result[flag], True)

    def test_status_command_preserves_plugin_verdict(self):
        def fake_resolve_route(args, context):
            return {"port": 63343, "project_key": "path:/tmp/example"}

        def fake_call_endpoint(route, endpoint, params, timeout=None):
            return {
                "has_inspection_results": True,
                "total_problems": 2,
                "inspection_verdict": "RED",
                "inspection_verdict_reason": "actionable_findings",
                "inspection_verdict_message": "Plugin found problems.",
                "inspection_verdict_next_action": "Fix them.",
            }

        original_resolve_route = jb_inspect.resolve_route
        original_call_endpoint = jb_inspect.call_endpoint
        jb_inspect.resolve_route = fake_resolve_route
        jb_inspect.call_endpoint = fake_call_endpoint
        try:
            result = jb_inspect.command_status(
                Namespace(
                    project_key=None,
                    session_id=None,
                    project_path=None,
                    worktree_path=None,
                    cwd=None,
                    project=None,
                    ide=None,
                ),
                {},
            )
        finally:
            jb_inspect.resolve_route = original_resolve_route
            jb_inspect.call_endpoint = original_call_endpoint

        self.assertEqual(result["status"], "results_available")
        self.assertFalse(result["clean"])
        self.assertEqual(result["verdict"], "RED")
        self.assertEqual(result["verdict_reason"], "actionable_findings")

    def test_status_results_available_without_zero_count_is_unknown(self):
        payload = {"status": "results_available", "clean": False, "problems": []}

        verdict = jb_inspect.verdict_for_payload(payload)

        self.assertEqual(verdict["verdict"], "UNKNOWN")

    def test_status_usable_values_with_blocker_flags_exit_nonzero(self):
        blocker_flags = (
            "is_scanning",
            "indexing",
            "inspection_in_progress",
            "session_drift",
            "ambiguous",
            "unavailable",
            "capture_incomplete",
            "results_may_be_stale",
            "timed_out",
        )
        for flag in blocker_flags:
            with self.subTest(flag=flag):
                body = {"status": "findings", flag: True}
                result = {
                    "status": "findings",
                    "clean": jb_inspect.classify_status_body_clean(body),
                    flag: True,
                }
                self.assertFalse(result["clean"])
                self.assertEqual(jb_inspect.classify_status_exit(result), 1)

    def test_status_unknown_explicit_values_exit_nonzero(self):
        for status in ("archived", "running", "failed", "cancelled", "pending_results"):
            with self.subTest(status=status):
                body = {"status": status}
                result = {"status": status, "clean": jb_inspect.classify_status_body_clean(body)}
                self.assertEqual(jb_inspect.classify_status_exit(result), 1)

    def test_status_unknown_explicit_value_ignores_cached_clean_flags(self):
        body = {"status": "failed", "clean_inspection": True, "has_inspection_results": True}
        result = {"status": "failed", "clean": jb_inspect.classify_status_body_clean(body)}
        self.assertEqual(jb_inspect.classify_status_exit(result), 1)


class EndpointUtilityTest(unittest.TestCase):
    def test_problems_params_passes_include_stale_when_requested(self):
        args = Namespace(
            project_key=None,
            session_id=None,
            project_path=None,
            worktree_path=None,
            cwd=None,
            project=None,
            ide=None,
            scope=None,
            severity="all",
            problem_type="all",
            file_pattern="all",
            limit=100,
            offset=0,
            include_stale=True,
        )

        params = jb_inspect.problems_params(args, {"scope": "changed_files"}, {})

        self.assertEqual(params["include_stale"], "true")

    def test_summarize_problems_withholds_normal_totals_for_stale_default(self):
        body = {
            "status": "stale_results",
            "results_may_be_stale": True,
            "cached_total_problems": 3,
            "cached_problems_shown": 0,
            "stale_reasons": ["project_changed_since_inspection"],
            "snapshot_change_kind": "snapshot_predates_current_trigger",
            "snapshot_run_id": 41,
        }

        summary = jb_inspect.summarize_problems({}, {}, body)

        self.assertEqual(summary["status"], "stale_results")
        self.assertFalse(summary["clean"])
        self.assertTrue(summary["results_may_be_stale"])
        self.assertEqual(summary["cached_total_problems"], 3)
        self.assertEqual(summary["cached_problems_shown"], 0)
        self.assertEqual(summary["snapshot_change_kind"], "snapshot_predates_current_trigger")
        self.assertEqual(summary["snapshot_run_id"], 41)
        self.assertNotIn("total_problems", summary)
        self.assertNotIn("problems_shown", summary)

    def test_summarize_problems_carries_capture_diagnostic(self):
        diagnostic = {
            "exit_reason": "deadline",
            "view_ready_ok": False,
            "successful_extraction_count": 2,
        }
        body = {
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_diagnostic": diagnostic,
        }

        summary = jb_inspect.summarize_problems({}, {}, body)

        self.assertEqual(summary["capture_diagnostic"], diagnostic)

    def test_summarize_problems_applies_text_only_coverage_override(self):
        body = {
            "status": "results_available",
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "GREEN",
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/example.swift",
                    "valid": True,
                    "directory": False,
                    "file_type": "textmate",
                    "psi_language": "textmate",
                    "psi_class": "org.jetbrains.plugins.textmate.psi.TextMateFile",
                    "in_content": True,
                    "in_source": False,
                }
            ),
        }

        summary = jb_inspect.summarize_problems({}, {}, body, allow_text_only_coverage=True)

        self.assertTrue(summary["allow_text_only_coverage"])
        self.assertEqual(summary["verdict"], "GREEN")
        self.assertEqual(summary["verdict_reason"], "text_only_coverage_allowed")
        self.assertEqual(summary["semantic_coverage"]["status"], "text_only_allowed")

    def test_summarize_problems_keeps_cached_stale_findings_separate(self):
        body = {
            "status": "stale_results",
            "results_may_be_stale": True,
            "include_stale": True,
            "cached_total_problems": 1,
            "cached_problems_shown": 1,
            "problems": [{"description": "Cached finding"}],
        }

        summary = jb_inspect.summarize_problems({}, {}, body)

        self.assertEqual(summary["status"], "stale_results")
        self.assertFalse(summary["clean"])
        self.assertTrue(summary["include_stale"])
        self.assertEqual(summary["cached_total_problems"], 1)
        self.assertEqual(summary["cached_problems_shown"], 1)
        self.assertEqual(summary["problems"], [{"description": "Cached finding"}])
        self.assertEqual(jb_inspect.classify_problems_exit(summary), 1)

    def test_summarize_problems_uses_total_for_empty_page_findings(self):
        body = {
            "status": "results_available",
            "total_problems": 5,
            "problems_shown": 0,
            "problems": [],
        }

        summary = jb_inspect.summarize_problems({}, {}, body)

        self.assertFalse(summary["clean"])
        self.assertEqual(summary["total_problems"], 5)
        self.assertEqual(summary["verdict"], "RED")
        self.assertEqual(summary["verdict_reason"], "actionable_findings")
        self.assertEqual(jb_inspect.classify_problems_exit(summary), 1)

    def test_summarize_problems_proof_failure_overrides_plugin_green_verdict(self):
        body = {
            "status": "results_available",
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "clean_confirmed",
            "proof_failures": ["resolved_project_path does not match requested_path"],
        }

        summary = jb_inspect.summarize_problems({}, {}, body)

        self.assertEqual(summary["verdict"], "UNKNOWN")
        self.assertEqual(summary["verdict_reason"], "inspection_proof_failed")
        self.assertEqual(summary["proof_failures"], ["resolved_project_path does not match requested_path"])

    def test_command_problems_preserves_requested_include_stale(self):
        calls = []

        def fake_resolve_route(args, context):
            return {"port": 63343, "project_key": "path:/tmp/example"}

        def fake_call_endpoint(route, endpoint, params, timeout=None):
            calls.append((endpoint, params))
            return {
                "status": "stale_results",
                "results_may_be_stale": True,
                "cached_total_problems": 1,
                "cached_problems_shown": 1,
                "problems": [{"description": "Cached finding"}],
            }

        original_resolve_route = jb_inspect.resolve_route
        original_call_endpoint = jb_inspect.call_endpoint
        jb_inspect.resolve_route = fake_resolve_route
        jb_inspect.call_endpoint = fake_call_endpoint
        try:
            result = jb_inspect.command_problems(
                Namespace(
                    project_key=None,
                    session_id=None,
                    project_path=None,
                    worktree_path=None,
                    cwd=None,
                    project=None,
                    ide=None,
                    scope=None,
                    severity="all",
                    problem_type="all",
                    file_pattern="all",
                    limit=100,
                    offset=0,
                    include_stale=True,
                ),
                {},
            )
        finally:
            jb_inspect.resolve_route = original_resolve_route
            jb_inspect.call_endpoint = original_call_endpoint

        self.assertEqual(calls[0][1]["include_stale"], "true")
        self.assertTrue(result["include_stale"])

    def test_command_problems_enriches_endpoint_error_with_context_and_route(self):
        route = {
            "port": 63343,
            "project_key": "path:/tmp/example",
            "base_path": "/tmp/example",
        }
        context = {
            "repo_path": "/tmp/example",
            "worktree_root": "/tmp/example",
            "project_path": "/tmp/example",
            "lifecycle_target_path": "/tmp/example",
            "scope": "changed_files",
        }

        def fake_resolve_route(args, current_context):
            return route

        def fake_call_endpoint(current_route, endpoint, params, timeout=None):
            raise jb_inspect.InspectError(
                "Inspection API returned invalid JSON: boom",
                3,
                {"error_reason": "invalid_api_response"},
            )

        original_resolve_route = jb_inspect.resolve_route
        original_call_endpoint = jb_inspect.call_endpoint
        jb_inspect.resolve_route = fake_resolve_route
        jb_inspect.call_endpoint = fake_call_endpoint
        try:
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.command_problems(
                    Namespace(
                        project_key=None,
                        session_id=None,
                        project_path=None,
                        worktree_path=None,
                        cwd=None,
                        project=None,
                        ide=None,
                        scope=None,
                        severity="all",
                        problem_type="all",
                        file_pattern="all",
                        limit=100,
                        offset=0,
                        include_stale=False,
                    ),
                    context,
                )
        finally:
            jb_inspect.resolve_route = original_resolve_route
            jb_inspect.call_endpoint = original_call_endpoint

        self.assertEqual(raised.exception.payload["context"]["repo_path"], "/tmp/example")
        self.assertEqual(raised.exception.payload["route"], route)
        self.assertEqual(raised.exception.payload["endpoint"], "problems")

    def test_wait_http_timeout_exceeds_plugin_timeout(self):
        self.assertEqual(jb_inspect.wait_http_timeout(60_000), 65.0)

    def test_call_endpoint_can_read_port_from_base_url(self):
        calls = []

        def fake_http_get(port, endpoint, params, timeout):
            calls.append((port, endpoint, params, timeout))
            return jb_inspect.HttpResult(200, {"ok": True}, "http://localhost:63343/api/inspection/status")

        original = jb_inspect.http_get
        jb_inspect.http_get = fake_http_get
        try:
            body = jb_inspect.call_endpoint(
                {"base_url": "http://localhost:63343/api/inspection"},
                "status",
                {"project_key": "path:/tmp/example"},
                timeout=12.5,
            )
        finally:
            jb_inspect.http_get = original

        self.assertEqual(body, {"ok": True})
        self.assertEqual(calls, [(63343, "status", {"project_key": "path:/tmp/example"}, 12.5)])

    def test_http_get_uses_numeric_loopback_to_avoid_localhost_wildcard_collisions(self):
        captured = []

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"status":"ok"}'

        def fake_urlopen(request, timeout):
            captured.append((request.full_url, timeout))
            return FakeResponse()

        original_urlopen = jb_inspect.urllib.request.urlopen
        jb_inspect.urllib.request.urlopen = fake_urlopen
        try:
            result = jb_inspect.http_get(63345, "identity", {"project_key": "path:/tmp/example"})
        finally:
            jb_inspect.urllib.request.urlopen = original_urlopen

        expected_prefix = "http://127.0.0.1:63345" + "/api" + "/inspection/identity?"
        self.assertEqual(result.body, {"status": "ok"})
        self.assertTrue(captured[0][0].startswith(expected_prefix))

    def test_status_command_passes_route_project_key_and_session_id(self):
        calls = []

        def fake_resolve_route(args, context):
            return {
                "port": 63343,
                "project_key": "path:/tmp/example",
                "session_id": "session-1",
                "base_path": "/tmp/example",
            }

        def fake_call_endpoint(route, endpoint, params, timeout=None):
            calls.append((route, endpoint, params, timeout))
            return {"clean_inspection": True, "is_scanning": False}

        original_resolve_route = jb_inspect.resolve_route
        original_call_endpoint = jb_inspect.call_endpoint
        jb_inspect.resolve_route = fake_resolve_route
        jb_inspect.call_endpoint = fake_call_endpoint
        try:
            result = jb_inspect.command_status(
                Namespace(
                    project_key=None,
                    session_id=None,
                    project_path=None,
                    worktree_path=None,
                    cwd=None,
                    project=None,
                    ide=None,
                ),
                {"ide": "WebStorm"},
            )
        finally:
            jb_inspect.resolve_route = original_resolve_route
            jb_inspect.call_endpoint = original_call_endpoint

        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["clean"], True)
        self.assertEqual(calls[0][1], "status")
        self.assertEqual(
            calls[0][2],
            {
                "project_key": "path:/tmp/example",
                "session_id": "session-1",
                "project_path": None,
                "worktree_path": None,
                "cwd": None,
                "project": None,
                "ide": "WebStorm",
            },
        )


class AttributionContractTest(unittest.TestCase):
    def test_golden_unknown_and_cleanup_cases_have_bounded_attribution(self):
        for case in attribution_cases():
            with self.subTest(case=case["name"]):
                payload = json.loads(json.dumps(case["payload"]))
                jb_inspect.apply_verdict(payload)

                attribution = payload["inspection_attribution"]
                expected = case["expected"]
                self.assertEqual(attribution["schema_version"], 1)
                self.assertEqual(attribution["classification"], expected["classification"])
                self.assertEqual(attribution["code"], expected["code"])
                self.assertEqual(attribution["phase"], expected["phase"])
                self.assertEqual(payload["bucket"], expected["helper_bucket"])
                self.assertEqual(payload["attribution_class"], expected["classification"])
                self.assertEqual(payload["failure_phase"], expected["phase"])
                self.assertIn("helper_revision", attribution)
                self.assertNotIn("close_token", json.dumps(payload))

    def test_endpoint_failure_preserves_http_response_attribution(self):
        case = next(item for item in attribution_cases() if item["name"] == "plugin-http-500")
        error_payload = dict(case["payload"])
        error_payload.update(
            {
                "endpoint": "status",
                "http_status": 500,
                "response_code": "inspection_api_http_error",
            }
        )

        result = jb_inspect.inspection_endpoint_failure_result(
            jb_inspect.InspectError("HTTP 500 from inspection API", 3, error_payload),
            {"project_key": "path:/repo"},
            {"run_id": 42},
            {"inspection_in_progress": False},
            None,
        )

        self.assertEqual(result["endpoint"], "status")
        self.assertEqual(result["http_status"], 500)
        self.assertEqual(result["response_code"], "inspection_api_http_error")
        self.assertEqual(result["inspection_attribution"]["request_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(result["client_run_id"], "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    def test_malformed_response_preserves_request_context(self):
        with self.assertRaises(jb_inspect.InspectError) as raised:
            jb_inspect.parse_http_json(
                b"{",
                "problems",
                200,
                "ffffffff-ffff-4fff-8fff-ffffffffffff",
            )

        payload = raised.exception.payload
        self.assertEqual(payload["endpoint"], "problems")
        self.assertEqual(payload["http_status"], 200)
        self.assertEqual(payload["response_code"], "invalid_api_response")
        self.assertEqual(payload["client_run_id"], "ffffffff-ffff-4fff-8fff-ffffffffffff")

    def test_unattributed_unknown_is_mechanically_a_tool_failure(self):
        payload = {"status": "mystery", "command": "get-status"}

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertTrue(payload["unattributed_unknown"])
        self.assertEqual(payload["inspection_attribution"]["classification"], "unattributed")
        self.assertEqual(payload["inspection_attribution"]["code"], "unattributed_unknown")
        self.assertEqual(payload["bucket"], "tool_bug")

    def test_prestart_configuration_attribution_records_cleanup_not_needed(self):
        payload = {
            "command": "inspect-closeout",
            "status": "error",
            "error_reason": "ide_selection_required",
            "context": {"client_run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
            "cleanup": {},
        }

        jb_inspect.apply_verdict(payload)

        attribution = payload["inspection_attribution"]
        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(attribution["source"], "helper")
        self.assertEqual(attribution["classification"], "configuration_blocked")
        self.assertEqual(attribution["code"], "ide_selection_required")
        self.assertEqual(attribution["phase"], "selection")
        self.assertEqual(attribution["cleanup_status"], "not_needed")
        self.assertEqual(attribution["cleanup_reason"], "inspection_not_started")

    def test_missing_project_content_roots_is_configuration_blocked(self):
        payload = {
            "command": "inspect-closeout",
            "status": "error",
            "error_reason": jb_inspect.PROJECT_CONTENT_ROOTS_MISSING_REASON,
        }

        jb_inspect.apply_verdict(payload)

        attribution = payload["inspection_attribution"]
        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], jb_inspect.PROJECT_CONTENT_ROOTS_MISSING_REASON)
        self.assertEqual(attribution["classification"], "configuration_blocked")
        self.assertEqual(attribution["phase"], "readiness_wait")
        self.assertEqual(payload["bucket"], "environment_blocked")
        self.assertNotIn("unattributed_unknown", payload)

    def test_outcome_record_keeps_provenance_without_paths_or_tokens(self):
        payload = {
            "status": "error",
            "error_reason": "inspection_api_http_error",
            "context": {
                "repo_path": "/private/repo",
                "worktree_root": "/private/worktree",
                "scope": "changed_files",
                "client_run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            },
            "route": {
                "project_name": "fixture",
                "project_key": "path:/private/worktree",
                "base_path": "/private/worktree",
                "project_instance_id": "project-instance-fixture",
                "session_id": "session-fixture",
                "ide": {
                    "name": "IntelliJ IDEA",
                    "product_code": "IU",
                    "version": "2026.1.4",
                    "plugin_version": "1.13.17",
                    "plugin_build_fingerprint": "fixture-fingerprint",
                },
            },
            "inspection_attribution": {
                "schema_version": 1,
                "source": "plugin",
                "classification": "tool_caused",
                "code": "inspection_api_http_error",
                "phase": "status",
                "request_id": "11111111-1111-4111-8111-111111111111",
                "client_run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            },
            "capture_diagnostic": {
                "scope_directory_requested": "/private/worktree/src",
            },
            "close_token": "must-not-leak",
        }
        jb_inspect.apply_verdict(payload)

        record = jb_inspect.outcome_log_record(payload, 3)
        unknown_record = jb_inspect.unknown_log_record(payload)
        serialized = json.dumps(record, sort_keys=True)
        unknown_serialized = json.dumps(unknown_record, sort_keys=True)

        self.assertEqual(record["helper_revision"], jb_inspect.helper_revision())
        self.assertEqual(record["plugin_build_fingerprint"], "fixture-fingerprint")
        self.assertEqual(record["ide_product_code"], "IU")
        self.assertEqual(record["ide_version"], "2026.1.4")
        self.assertEqual(record["failure_phase"], "status")
        self.assertEqual(record["attribution_class"], "tool_caused")
        self.assertEqual(record["request_id"], "11111111-1111-4111-8111-111111111111")
        self.assertIn("repo_path_hash", record)
        self.assertIn("worktree_root_hash", record)
        self.assertNotIn("/private/", serialized)
        self.assertNotIn("/private/", unknown_serialized)
        self.assertNotIn("must-not-leak", serialized)
        self.assertIn("scope_directory_requested_hash", unknown_record["capture_diagnostic"])

    def test_helper_and_manifest_revisions_use_full_content_sha256(self):
        self.assertRegex(jb_inspect.helper_revision(), r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(jb_inspect.stable_value_hash("repo") or "", r"^sha256:[0-9a-f]{64}$")
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "deployment.json"
            manifest.write_text('{"revision":1}\n', encoding="utf-8")
            first = jb_inspect.file_content_sha256(manifest)
            manifest.write_text('{"revision":2}\n', encoding="utf-8")
            second = jb_inspect.file_content_sha256(manifest)

        self.assertRegex(first or "", r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(second or "", r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(first, second)

    def test_green_and_red_preserve_plugin_attribution_and_authoritative_channel(self):
        for verdict, total_problems, reason in (
            ("GREEN", 0, "no_matching_findings"),
            ("RED", 1, "actionable_findings"),
        ):
            with self.subTest(verdict=verdict):
                payload = {
                    "status": "results_available",
                    "total_problems": total_problems,
                    "problems": [] if total_problems == 0 else [{"description": "fixture"}],
                    "context": {
                        "client_run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "ide_selection": {"channel": "stable"},
                    },
                    "route": {
                        "session_id": "session-1",
                        "project_instance_id": "project-1",
                        "ide": {
                            "channel": "eap",
                            "product_code": "PY",
                            "version": "2026.2",
                            "plugin_version": "1.13.17",
                            "plugin_build_fingerprint": "a" * 40 + "-clean",
                        },
                    },
                    "inspection_attribution": {
                        "schema_version": 1,
                        "source": "plugin",
                        "classification": "decisive",
                        "code": reason,
                        "phase": "problems",
                        "client_run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        "inspection_run_id": 7,
                        "plugin_version": "1.13.17",
                        "plugin_build_fingerprint": "a" * 40 + "-clean",
                        "ide_product_code": "PY",
                        "ide_version": "2026.2",
                        "ide_channel": "eap",
                    },
                    "cleanup": {"status": "closed"},
                }

                jb_inspect.apply_verdict(payload)

                attribution = payload["inspection_attribution"]
                self.assertEqual(payload["verdict"], verdict)
                self.assertEqual(attribution["source"], "plugin")
                self.assertEqual(attribution["classification"], "decisive")
                self.assertEqual(attribution["ide_channel"], "eap")
                self.assertEqual(attribution["ide_channel_source"], "plugin_attribution")
                self.assertEqual(attribution["helper_revision"], jb_inspect.helper_revision())
                self.assertEqual(attribution["cleanup_status"], "closed")

    def test_local_client_run_id_remains_authoritative_when_plugin_echo_differs(self):
        local_client_run_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        plugin_client_run_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        payload = {
            "status": "results_available",
            "total_problems": 0,
            "problems": [],
            "context": {
                "repo_path": "/repo",
                "worktree_root": "/repo",
                "client_run_id": local_client_run_id,
            },
            "route": {
                "project_key": "path:/repo",
                "project_instance_id": "project-1",
                "session_id": "session-1",
                "ide": {
                    "product_code": "PY",
                    "version": "2026.2",
                    "channel": "eap",
                    "plugin_version": "1.13.17",
                    "plugin_build_fingerprint": "a" * 40 + "-clean",
                },
            },
            "inspection_attribution": {
                "schema_version": 1,
                "source": "plugin",
                "classification": "decisive",
                "code": "no_matching_findings",
                "phase": "problems",
                "client_run_id": plugin_client_run_id,
                "inspection_run_id": 7,
                "plugin_version": "1.13.17",
                "plugin_build_fingerprint": "a" * 40 + "-clean",
                "ide_product_code": "PY",
                "ide_version": "2026.2",
                "ide_channel": "eap",
            },
            "cleanup": {"status": "closed"},
        }

        jb_inspect.apply_verdict(payload)
        record = jb_inspect.outcome_log_record(payload, 0)

        self.assertEqual(payload["evidence_ids"]["client_run_id"], local_client_run_id)
        self.assertEqual(payload["inspection_attribution"]["client_run_id"], plugin_client_run_id)
        self.assertEqual(record["client_run_id"], local_client_run_id)
        self.assertEqual(record["inspection_attribution"]["client_run_id"], plugin_client_run_id)
        self.assertIn("client_run_id", jb_inspect.plugin_attribution_mismatches(record))

    def test_outcome_record_v2_contains_identity_scope_and_internal_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "deployment.json"
            manifest.write_text('{"artifact":"fixture"}\n', encoding="utf-8")
            scope_descriptor = {"scope": "changed_files", "include_unversioned": True}
            payload = {
                "command": "inspect-closeout",
                "status": "results_available",
                "total_problems": 0,
                "problems": [],
                "context": {
                    "repo_path": "/repo",
                    "worktree_root": "/repo",
                    "repo_head_sha": "b" * 40,
                    "scope": "changed_files",
                    "scope_descriptor": scope_descriptor,
                    "scope_descriptor_sha256": jb_inspect.canonical_json_sha256(scope_descriptor),
                    "client_run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                },
                "route": {
                    "project_key": "path:/repo",
                    "project_instance_id": "project-1",
                    "session_id": "session-1",
                    "ide": {
                        "product_code": "PY",
                        "version": "2026.2",
                        "channel": "eap",
                        "plugin_version": "1.13.17",
                        "plugin_build_fingerprint": "a" * 40 + "-clean",
                    },
                },
                "inspection_attribution": {
                    "schema_version": 1,
                    "source": "plugin",
                    "classification": "decisive",
                    "code": "no_matching_findings",
                    "phase": "problems",
                    "client_run_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "inspection_run_id": 7,
                    "plugin_version": "1.13.17",
                    "plugin_build_fingerprint": "a" * 40 + "-clean",
                    "ide_product_code": "PY",
                    "ide_version": "2026.2",
                    "ide_channel": "eap",
                },
                "cleanup": {"status": "closed"},
            }
            jb_inspect.apply_verdict(payload)

            with patch.dict(os.environ, {jb_inspect.DEPLOYMENT_MANIFEST_ENV: str(manifest)}, clear=False):
                record = jb_inspect.outcome_log_record(payload, 0)
            manifest_sha256 = jb_inspect.file_content_sha256(manifest)

        self.assertEqual(record["schema_version"], 2)
        self.assertEqual(record["event_kind"], "inspection_assessment")
        self.assertEqual(record["assessment_id"], "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self.assertEqual(record["client_run_id"], record["assessment_id"])
        self.assertEqual(record["inspection_run_id"], 7)
        self.assertTrue(record["inspection_started"])
        self.assertEqual(record["repo_head_sha"], "b" * 40)
        self.assertEqual(record["scope_descriptor"], scope_descriptor)
        self.assertEqual(record["scope_descriptor_sha256"], jb_inspect.canonical_json_sha256(scope_descriptor))
        self.assertEqual(record["deployment_manifest_sha256"], manifest_sha256)
        self.assertRegex(record["event_id"], r"^[0-9a-f-]{36}$")
        self.assertRegex(record["timestamp"], r"\.\d{3}Z$")
        self.assertEqual(record["internal_attempts"][-1]["verdict"], "GREEN")
        self.assertTrue(record["internal_attempts"][-1]["terminal"])


class HumanOutputTest(unittest.TestCase):
    def test_verdict_for_clean_payload_is_green(self):
        payload = {"status": "clean", "clean": True, "total_problems": 0, "problems": []}

        verdict = jb_inspect.verdict_for_payload(payload)

        self.assertEqual(verdict["verdict"], "GREEN")
        self.assertEqual(verdict["verdict_reason"], "clean_confirmed")

    def test_verdict_for_current_zero_matching_results_is_green(self):
        payload = {"status": "results_available", "clean": True, "total_problems": 0, "problems": []}

        verdict = jb_inspect.verdict_for_payload(payload)

        self.assertEqual(verdict["verdict"], "GREEN")
        self.assertEqual(verdict["verdict_reason"], "no_matching_findings")

    def test_textmate_scope_overrides_plugin_green_with_semantic_coverage_unknown(self):
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON,
            "proof_failures": [jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON],
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/PlaybackValidation.swift",
                    "valid": True,
                    "directory": False,
                    "file_type": "textmate",
                    "psi_language": "textmate",
                    "psi_class": "org.jetbrains.plugins.textmate.psi.TextMateFile",
                    "in_content": True,
                    "in_source": False,
                }
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], "scope_semantic_coverage_missing")
        self.assertEqual(payload["bucket"], "environment_blocked")
        self.assertFalse(payload["retry_policy"]["retry"])
        self.assertEqual(payload["attribution_class"], "configuration_blocked")
        self.assertEqual(payload["semantic_coverage"]["missing_file_count"], 1)
        self.assertEqual(payload["semantic_coverage"]["files"][0]["requested_language_hint"], "swift")
        self.assertIn("language plugins", payload["verdict_next_action"])

        output = io.StringIO()
        with redirect_stdout(output):
            jb_inspect.print_human(payload)
        lines = output.getvalue().splitlines()
        self.assertIn(
            'SEMANTIC_COVERAGE_FILE: path=/tmp/PlaybackValidation.swift language_hint=swift file_type=textmate psi_language=textmate reasons=["non_semantic_fallback"]',
            lines,
        )
        self.assertFalse(any(line.startswith("SEMANTIC_COVERAGE_FILE: classification=") for line in lines))

    def test_mixed_semantic_and_textmate_scope_fails_closed(self):
        payload = {
            "status": "clean",
            "clean": True,
            "total_problems": 0,
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/tool.py",
                    "valid": True,
                    "directory": False,
                    "file_type": "Python",
                    "psi_language": "Python",
                    "psi_class": "com.jetbrains.python.psi.impl.PyFileImpl",
                    "in_content": True,
                    "in_source": False,
                },
                {
                    "path": "/tmp/view.swift",
                    "valid": True,
                    "directory": False,
                    "file_type": "TextMate",
                    "psi_language": "textmate",
                    "psi_class": "org.jetbrains.plugins.textmate.psi.TextMateFile",
                    "in_content": True,
                    "in_source": False,
                },
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["semantic_coverage"]["missing_file_count"], 1)
        self.assertTrue(payload["semantic_coverage"]["files"][0]["path"].endswith("view.swift"))

    def test_semantic_psi_with_in_source_false_stays_green(self):
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "no_matching_findings",
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/PlaybackValidation.swift",
                    "valid": True,
                    "directory": False,
                    "file_type": "NoctuleSwift",
                    "psi_language": "NoctuleSwift",
                    "psi_class": "swift.lang.psi.lt",
                    "in_content": True,
                    "in_source": False,
                }
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "GREEN")
        self.assertNotIn("semantic_coverage", payload)

    def test_idea_module_plaintext_in_content_is_classified_as_metadata(self):
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "no_matching_findings",
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/project.iml",
                    "valid": True,
                    "directory": False,
                    "file_type": "IDEA_MODULE",
                    "psi_language": "Plain text",
                    "psi_class": "com.intellij.psi.impl.source.PsiPlainTextFile",
                    "in_content": True,
                    "in_source": False,
                }
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "GREEN")
        self.assertEqual(payload["verdict_reason"], "project_metadata_coverage_not_required")
        self.assertEqual(payload["semantic_coverage"]["status"], "satisfied")
        self.assertEqual(payload["semantic_coverage"]["missing_file_count"], 0)
        self.assertEqual(payload["semantic_coverage"]["metadata_file_count"], 1)
        self.assertEqual(payload["semantic_coverage"]["metadata_files"][0]["classification"], "project_metadata")
        self.assertEqual(payload["semantic_coverage"]["files"], [])
        self.assertEqual(
            payload["verdict_next_action"],
            "No inspection action required for classified project metadata.",
        )

        output = io.StringIO()
        with redirect_stdout(output):
            jb_inspect.print_human(payload)
        text = output.getvalue()
        self.assertIn("SEMANTIC_COVERAGE: status=satisfied", text)
        self.assertIn("SEMANTIC_COVERAGE_METADATA_FILE:", text)
        self.assertIn("classification=project_metadata", text)
        self.assertIn("coverage_required=False", text)

    def test_mixed_semantic_scope_with_idea_module_metadata_stays_green(self):
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "no_matching_findings",
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/tool.py",
                    "valid": True,
                    "directory": False,
                    "file_type": "Python",
                    "psi_language": "Python",
                    "psi_class": "com.jetbrains.python.psi.impl.PyFileImpl",
                    "in_content": True,
                    "in_source": True,
                },
                {
                    "path": "/tmp/project.iml",
                    "valid": True,
                    "directory": False,
                    "file_type": "IDEA_MODULE",
                    "psi_language": "TEXT",
                    "psi_class": "com.intellij.psi.impl.light.LightPsiFile",
                    "in_content": True,
                    "in_source": False,
                },
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "GREEN")
        self.assertEqual(payload["semantic_coverage"]["status"], "satisfied")
        self.assertEqual(payload["semantic_coverage"]["missing_file_count"], 0)
        self.assertEqual(payload["semantic_coverage"]["metadata_file_count"], 1)
        metadata_file = payload["semantic_coverage"]["metadata_files"][0]
        self.assertEqual(metadata_file["psi_language"], "TEXT")
        self.assertEqual(metadata_file["psi_class"], "com.intellij.psi.impl.light.LightPsiFile")

    def test_explicitly_excluded_dependency_lockfile_is_classified_as_metadata(self):
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "no_matching_findings",
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/tool.py",
                    "valid": True,
                    "directory": False,
                    "file_type": "Python",
                    "psi_language": "Python",
                    "psi_class": "com.jetbrains.python.psi.impl.PyFileImpl",
                    "in_content": True,
                    "in_source": True,
                    "is_excluded": False,
                },
                {
                    "path": "/tmp/uv.lock",
                    "valid": True,
                    "directory": False,
                    "file_type": "PLAIN_TEXT",
                    "psi_language": "TEXT",
                    "psi_class": "com.intellij.psi.impl.source.PsiPlainTextFile",
                    "in_content": False,
                    "in_source": False,
                    "is_excluded": True,
                    "coverage_role": "excluded_dependency_lockfile",
                },
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "GREEN")
        self.assertEqual(payload["semantic_coverage"]["status"], "satisfied")
        self.assertEqual(payload["semantic_coverage"]["missing_file_count"], 0)
        self.assertEqual(payload["semantic_coverage"]["metadata_file_count"], 1)
        metadata_file = payload["semantic_coverage"]["metadata_files"][0]
        self.assertEqual(metadata_file["classification"], "excluded_dependency_lockfile")
        self.assertTrue(metadata_file["is_excluded"])

    def test_dependency_lockfile_without_explicit_exclusion_stays_unknown(self):
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON,
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/uv.lock",
                    "valid": True,
                    "directory": False,
                    "file_type": "PLAIN_TEXT",
                    "psi_language": "TEXT",
                    "psi_class": "com.intellij.psi.impl.source.PsiPlainTextFile",
                    "in_content": False,
                    "in_source": False,
                    "is_excluded": False,
                },
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertIn("outside_project_content", payload["semantic_coverage"]["files"][0]["reasons"])

    def test_excluded_lockfile_does_not_hide_source_outside_content(self):
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON,
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/uv.lock",
                    "valid": True,
                    "directory": False,
                    "file_type": "PLAIN_TEXT",
                    "psi_language": "TEXT",
                    "psi_class": "com.intellij.psi.impl.source.PsiPlainTextFile",
                    "in_content": False,
                    "in_source": False,
                    "is_excluded": True,
                    "coverage_role": "excluded_dependency_lockfile",
                },
                {
                    "path": "/tmp/tool.py",
                    "valid": True,
                    "directory": False,
                    "file_type": "Python",
                    "psi_language": "Python",
                    "psi_class": "com.jetbrains.python.psi.impl.PyFileImpl",
                    "in_content": False,
                    "in_source": False,
                    "is_excluded": False,
                },
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["semantic_coverage"]["missing_file_count"], 1)
        self.assertTrue(payload["semantic_coverage"]["files"][0]["path"].endswith("tool.py"))

    def test_malformed_dependency_lockfile_role_fails_closed(self):
        lockfile = {
            "path": "/tmp/uv.lock",
            "valid": True,
            "directory": False,
            "file_type": "PLAIN_TEXT",
            "psi_language": "TEXT",
            "psi_class": "com.intellij.psi.impl.source.PsiPlainTextFile",
            "in_content": False,
            "in_source": False,
            "is_excluded": False,
            "coverage_role": "excluded_dependency_lockfile",
        }
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON,
            "capture_diagnostic": scope_capture_diagnostic(lockfile),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertIn("invalid_metadata_role", payload["semantic_coverage"]["files"][0]["reasons"])

    def test_conflicting_dependency_lockfile_roles_fail_closed(self):
        lockfile = {
            "path": "/tmp/uv.lock",
            "valid": True,
            "directory": False,
            "file_type": "PLAIN_TEXT",
            "psi_language": "TEXT",
            "psi_class": "com.intellij.psi.impl.source.PsiPlainTextFile",
            "in_content": False,
            "in_source": False,
            "is_excluded": True,
            "coverage_role": "excluded_dependency_lockfile",
            "classification": "project_metadata",
        }
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON,
            "capture_diagnostic": scope_capture_diagnostic(lockfile),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertIn("invalid_metadata_role", payload["semantic_coverage"]["files"][0]["reasons"])

    def test_idea_module_requires_explicit_valid_true(self):
        missing = object()
        for label, valid in (("missing", missing), ("null", None), ("string", "true")):
            with self.subTest(label=label):
                file_diagnostic = {
                    "path": "/tmp/project.iml",
                    "directory": False,
                    "file_type": "IDEA_MODULE",
                    "psi_language": "Plain text",
                    "psi_class": "com.intellij.psi.impl.source.PsiPlainTextFile",
                    "in_content": True,
                    "in_source": False,
                }
                if valid is not missing:
                    file_diagnostic["valid"] = valid
                payload = {
                    "status": "results_available",
                    "clean": True,
                    "total_problems": 0,
                    "problems": [],
                    "inspection_verdict": "GREEN",
                    "inspection_verdict_reason": "no_matching_findings",
                    "capture_diagnostic": scope_capture_diagnostic(file_diagnostic),
                }

                jb_inspect.apply_verdict(payload)

                self.assertEqual(payload["verdict"], "UNKNOWN")
                self.assertEqual(payload["verdict_reason"], "scope_semantic_coverage_missing")
                self.assertEqual(
                    payload["semantic_coverage"]["files"][0]["reasons"],
                    ["non_semantic_fallback"],
                )

    def test_plaintext_code_scope_still_fails_closed(self):
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "no_matching_findings",
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/tool.py",
                    "valid": True,
                    "directory": False,
                    "file_type": "PLAIN_TEXT",
                    "psi_language": "Plain text",
                    "psi_class": "com.intellij.psi.impl.source.PsiPlainTextFile",
                    "in_content": True,
                    "in_source": False,
                }
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], "scope_semantic_coverage_missing")
        self.assertEqual(payload["semantic_coverage"]["files"][0]["reasons"], ["non_semantic_fallback"])
        self.assertIn("language plugins", payload["verdict_next_action"])

    def test_plaintext_scope_can_be_explicitly_allowed(self):
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "GREEN",
            "allow_text_only_coverage": True,
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/notes.txt",
                    "valid": True,
                    "directory": False,
                    "file_type": "PLAIN_TEXT",
                    "psi_language": "Plain text",
                    "psi_class": "com.intellij.psi.impl.source.PsiPlainTextFile",
                    "in_content": True,
                    "in_source": False,
                }
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "GREEN")
        self.assertEqual(payload["verdict_reason"], "text_only_coverage_allowed")
        self.assertEqual(payload["semantic_coverage"]["status"], "text_only_allowed")
        self.assertEqual(payload["attribution_class"], "decisive")
        self.assertEqual(payload["inspection_attribution"]["code"], "text_only_coverage_allowed")
        self.assertEqual(payload["inspection_attribution"]["source"], "helper")
        self.assertIn("explicitly allowed", payload["agent_report"])

    def test_text_only_override_does_not_allow_outside_project_content(self):
        payload = {
            "status": "clean",
            "clean": True,
            "total_problems": 0,
            "allow_text_only_coverage": True,
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/external.py",
                    "valid": True,
                    "directory": False,
                    "file_type": "Python",
                    "psi_language": "Python",
                    "psi_class": "com.jetbrains.python.psi.impl.PyFileImpl",
                    "in_content": False,
                    "in_source": False,
                }
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["semantic_coverage"]["files"][0]["reasons"], ["outside_project_content"])
        self.assertIn("module/content root", payload["verdict_next_action"])
        self.assertNotIn("language plugins", payload["verdict_next_action"])

    def test_idea_module_outside_content_is_not_exempted_by_text_only_override(self):
        payload = {
            "status": "clean",
            "clean": True,
            "total_problems": 0,
            "allow_text_only_coverage": True,
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/project.iml",
                    "valid": True,
                    "directory": False,
                    "file_type": "IDEA_MODULE",
                    "psi_language": "Plain text",
                    "psi_class": "com.intellij.psi.impl.source.PsiPlainTextFile",
                    "in_content": False,
                    "in_source": False,
                }
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(
            payload["semantic_coverage"]["files"][0]["reasons"],
            ["non_semantic_fallback", "outside_project_content"],
        )
        self.assertIn("module/content root", payload["verdict_next_action"])
        self.assertNotIn("language plugins", payload["verdict_next_action"])

    def test_mixed_outside_content_and_textmate_scope_reports_both_actions(self):
        payload = {
            "status": "clean",
            "clean": True,
            "total_problems": 0,
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/external.py",
                    "valid": True,
                    "directory": False,
                    "file_type": "Python",
                    "psi_language": "Python",
                    "psi_class": "com.jetbrains.python.psi.impl.PyFileImpl",
                    "in_content": False,
                    "in_source": False,
                },
                {
                    "path": "/tmp/view.swift",
                    "valid": True,
                    "directory": False,
                    "file_type": "TextMate",
                    "psi_language": "textmate",
                    "psi_class": "org.jetbrains.plugins.textmate.psi.TextMateFile",
                    "in_content": True,
                    "in_source": False,
                },
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertIn("module/content root", payload["verdict_next_action"])
        self.assertIn("language plugins", payload["verdict_next_action"])
        self.assertIn("--allow-text-only-coverage", payload["verdict_next_action"])

    def test_semantic_coverage_gap_does_not_hide_actionable_red_verdict(self):
        payload = {
            "status": "findings",
            "clean": False,
            "total_problems": 1,
            "problems": [{"description": "Broken"}],
            "inspection_verdict": "RED",
            "inspection_verdict_reason": "actionable_findings",
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/view.swift",
                    "valid": True,
                    "directory": False,
                    "file_type": "textmate",
                    "psi_language": "textmate",
                    "psi_class": "org.jetbrains.plugins.textmate.psi.TextMateFile",
                    "in_content": True,
                    "in_source": False,
                }
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "RED")
        self.assertEqual(payload["semantic_coverage"]["status"], "missing")

    def test_project_metadata_classification_does_not_hide_actionable_red_verdict(self):
        payload = {
            "status": "findings",
            "clean": False,
            "total_problems": 1,
            "problems": [{"description": "Broken"}],
            "inspection_verdict": "RED",
            "inspection_verdict_reason": "actionable_findings",
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/project.iml",
                    "valid": True,
                    "directory": False,
                    "file_type": "IDEA_MODULE",
                    "psi_language": "Plain text",
                    "psi_class": "com.intellij.psi.impl.source.PsiPlainTextFile",
                    "in_content": True,
                    "in_source": False,
                }
            ),
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "RED")
        self.assertEqual(payload["verdict_reason"], "actionable_findings")
        self.assertEqual(payload["semantic_coverage"]["status"], "satisfied")

    def test_verdict_prefers_plugin_provided_contract(self):
        payload = {
            "status": "results_available",
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": "plugin_specific_reason",
            "inspection_verdict_message": "Plugin supplied message.",
            "inspection_verdict_next_action": "Plugin supplied action.",
        }

        verdict = jb_inspect.verdict_for_payload(payload)

        self.assertEqual(verdict["verdict"], "UNKNOWN")
        self.assertEqual(verdict["verdict_reason"], "plugin_specific_reason")
        self.assertEqual(verdict["verdict_next_action"], "Plugin supplied action.")

    def test_broad_green_requires_native_proof_capability(self):
        payload = {
            "scope": "whole_project",
            "status": "clean",
            "clean": True,
            "total_problems": 0,
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "clean_confirmed",
            "route": {
                "ide": {
                    "plugin_version": "1.13.23",
                    "plugin_build_fingerprint": "a" * 40 + "-dirty",
                }
            },
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], "plugin_deployment_mismatch")
        self.assertEqual(payload["bucket"], "environment_blocked")
        self.assertFalse(payload["retry_policy"]["retry"])
        self.assertEqual(payload["deployment_mismatch"]["required_version"], 2)
        self.assertIn("Install a plugin", payload["agent_result"]["next_action"])

    def test_broad_green_accepts_native_proof_capability(self):
        payload = {
            "scope": "directory",
            "status": "clean",
            "clean": True,
            "total_problems": 0,
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "clean_confirmed",
            "route": {"ide": {"inspection_execution_proof_version": 2}},
        }

        verdict = jb_inspect.verdict_for_payload(payload)

        self.assertEqual(verdict["verdict"], "GREEN")
        self.assertEqual(verdict["verdict_reason"], "clean_confirmed")

    def test_verdict_for_findings_payload_is_red(self):
        payload = {"status": "findings", "clean": False, "total_problems": 1, "problems": [{"description": "Broken"}]}

        verdict = jb_inspect.verdict_for_payload(payload)

        self.assertEqual(verdict["verdict"], "RED")
        self.assertEqual(verdict["verdict_reason"], "actionable_findings")

    def test_verdict_for_capture_incomplete_payload_is_unknown_with_guidance(self):
        payload = {
            "status": "capture_incomplete",
            "clean": False,
            "capture_incomplete": True,
            "capture_incomplete_reason": "non_empty_unmapped_tree",
            "total_problems": 0,
            "problems": [],
        }

        verdict = jb_inspect.verdict_for_payload(payload)

        self.assertEqual(verdict["verdict"], "UNKNOWN")
        self.assertEqual(verdict["verdict_reason"], "non_empty_unmapped_tree")
        self.assertIn("plugin/helper bug", verdict["verdict_next_action"])

    def test_cleanup_failure_overrides_plugin_green_verdict(self):
        payload = {
            "status": "clean",
            "clean": True,
            "cleanup": {"status": "failed", "reason": "route_missing"},
            "cleanup_failed": True,
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "clean_confirmed",
        }

        verdict = jb_inspect.verdict_for_payload(payload)

        self.assertEqual(verdict["verdict"], "UNKNOWN")
        self.assertEqual(verdict["verdict_reason"], "cleanup_failed")

    def test_timeout_overrides_cleanup_failure_reason(self):
        payload = {
            "status": "timed_out",
            "cleanup": {"status": "failed", "reason": "close_failed"},
            "cleanup_failed": True,
            "wait": {"timed_out": True, "inspection_in_progress": True},
        }

        verdict = jb_inspect.verdict_for_payload(payload)

        self.assertEqual(verdict["verdict"], "UNKNOWN")
        self.assertEqual(verdict["verdict_reason"], "timeout")
        self.assertIn("larger timeout", verdict["verdict_next_action"])

    def test_proof_failure_overrides_plugin_green_verdict(self):
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "clean_confirmed",
            "proof_failures": [
                "resolved_project_path does not match requested_path",
                "scope_file_count is zero for changed_files",
            ],
        }

        verdict = jb_inspect.verdict_for_payload(payload)

        self.assertEqual(verdict["verdict"], "UNKNOWN")
        self.assertEqual(verdict["verdict_reason"], "inspection_proof_failed")

    def test_cleanup_skipped_overrides_clean_verdict(self):
        payload = {
            "status": "clean",
            "clean": True,
            "cleanup": {"status": "skipped", "reason": "missing_close_token"},
            "cleanup_skipped": True,
        }

        verdict = jb_inspect.verdict_for_payload(payload)

        self.assertEqual(verdict["verdict"], "UNKNOWN")
        self.assertEqual(verdict["verdict_reason"], "cleanup_skipped")

    def test_agent_result_for_retryable_timeout_unknown(self):
        payload = {"status": "timed_out", "timed_out": True}

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["bucket"], "ide_not_ready")
        self.assertTrue(payload["retry_policy"]["retry"])
        self.assertEqual(payload["retry_policy"]["max_attempts"], 1)
        self.assertEqual(payload["agent_result"]["bucket"], "ide_not_ready")

    def test_agent_result_for_busy_api_timeout_is_retryable(self):
        payload = {"status": "error", "error_reason": "inspection_api_timeout"}

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["bucket"], "ide_not_ready")
        self.assertTrue(payload["retry_policy"]["retry"])
        self.assertIn("busy", payload["agent_result"]["next_action"])

    def test_agent_result_for_tool_bug_unknown_does_not_retry(self):
        payload = {
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "non_empty_unmapped_tree",
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["bucket"], "tool_bug")
        self.assertFalse(payload["retry_policy"]["retry"])
        self.assertIn("inconclusive", payload["agent_report"])

    def test_legacy_broad_execution_proof_gap_is_terminal_environment_blocker(self):
        payload = {
            "scope": "whole_project",
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "execution_not_proven",
            "capture_diagnostic": {
                "execution_proof_skipped_reason": "whole_project_execution_not_proven",
            },
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["bucket"], "environment_blocked")
        self.assertFalse(payload["retry_policy"]["retry"])
        self.assertIn("Update the inspection plugin", payload["agent_result"]["next_action"])

    def test_aborted_native_proof_is_retryable_once_with_consistent_guidance(self):
        payload = {
            "scope": "whole_project",
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "execution_not_proven",
            "capture_diagnostic": {
                "execution_proof_skipped_reason": "native_inspection_not_completed",
            },
            "route": {"ide": {"inspection_execution_proof_version": 2}},
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["bucket"], "capture_not_ready")
        self.assertTrue(payload["retry_policy"]["retry"])
        self.assertIn("retry once", payload["agent_result"]["next_action"])

    def test_native_inspection_block_failure_is_terminal_tool_bug(self):
        payload = {
            "scope": "directory",
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "execution_not_proven",
            "capture_diagnostic": {
                "execution_proof_skipped_reason": "native_inspection_failures",
            },
            "route": {"ide": {"inspection_execution_proof_version": 2}},
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["bucket"], "tool_bug")
        self.assertFalse(payload["retry_policy"]["retry"])
        self.assertIn("Stop retrying", payload["agent_result"]["next_action"])

    def test_native_attestation_context_failure_is_environment_blocked(self):
        payload = {
            "scope": "whole_project",
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "execution_not_proven",
            "capture_diagnostic": {
                "execution_proof_skipped_reason": "native_attestation_context_creation_failed",
            },
            "route": {"ide": {"inspection_execution_proof_version": 2}},
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["bucket"], "environment_blocked")
        self.assertFalse(payload["retry_policy"]["retry"])
        self.assertIn("reinstall", payload["agent_result"]["next_action"])

    def test_native_unmapped_problem_count_is_terminal_tool_bug(self):
        payload = {
            "scope": "directory",
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "execution_not_proven",
            "capture_diagnostic": {
                "execution_proof_skipped_reason": "native_inspection_reported_unmapped_problems",
            },
            "route": {"ide": {"inspection_execution_proof_version": 2}},
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["bucket"], "tool_bug")
        self.assertFalse(payload["retry_policy"]["retry"])
        self.assertIn("Stop retrying", payload["agent_result"]["next_action"])

    def test_native_missing_file_scoped_completion_is_environment_blocked(self):
        payload = {
            "scope": "whole_project",
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "execution_not_proven",
            "capture_diagnostic": {
                "execution_proof_skipped_reason": "native_inspection_no_file_scoped_tools_completed",
            },
            "route": {"ide": {"inspection_execution_proof_version": 2}},
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["bucket"], "environment_blocked")
        self.assertFalse(payload["retry_policy"]["retry"])
        self.assertIn("active inspection profile", payload["agent_result"]["next_action"])

    def test_native_incomplete_scope_block_reason_is_environment_blocked(self):
        payload = {
            "scope": "whole_project",
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "execution_not_proven",
            "capture_diagnostic": {
                "execution_proof_skipped": False,
                "execution_proof_block_reason": "native_inspection_scope_incomplete",
            },
            "route": {"ide": {"inspection_execution_proof_version": 2}},
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["bucket"], "environment_blocked")
        self.assertFalse(payload["retry_policy"]["retry"])
        self.assertIn("requested scope", payload["agent_result"]["next_action"])

    def test_native_scope_enumeration_failure_is_terminal_tool_bug(self):
        payload = {
            "scope": "whole_project",
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "execution_not_proven",
            "capture_diagnostic": {
                "execution_proof_block_reason": "native_scope_enumeration_failed",
            },
            "route": {"ide": {"inspection_execution_proof_version": 2}},
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["bucket"], "tool_bug")
        self.assertFalse(payload["retry_policy"]["retry"])
        self.assertIn("could not enumerate", payload["agent_result"]["next_action"])

    def test_native_inspection_failure_is_terminal_tool_bug(self):
        payload = {
            "scope": "whole_project",
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "execution_not_proven",
            "capture_diagnostic": {
                "execution_proof_block_reason": "native_inspection_failures",
            },
            "route": {"ide": {"inspection_execution_proof_version": 2}},
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["bucket"], "tool_bug")
        self.assertFalse(payload["retry_policy"]["retry"])
        self.assertIn("native inspection run", payload["agent_result"]["next_action"])

    def test_worktree_mutation_evidence_reports_only_new_status_changes(self):
        before = {
            "status": "ok",
            "entries": {"existing.txt": " M"},
        }
        after = {
            "status": "ok",
            "entries": {
                "existing.txt": " M",
                ".idea/misc.xml": " M",
                ".idea/editor.xml": "??",
            },
        }

        evidence = jb_inspect.summarize_worktree_mutations(before, after)

        self.assertTrue(evidence["dirty_before"])
        self.assertTrue(evidence["dirty_after"])
        self.assertEqual(evidence["new_or_changed_path_count"], 2)
        self.assertEqual(evidence["tracked_change_count"], 1)
        self.assertEqual(evidence["untracked_change_count"], 1)
        self.assertEqual(evidence["new_or_changed_paths"], [".idea/editor.xml", ".idea/misc.xml"])

    def test_git_porcelain_z_preserves_special_paths_and_rename_destination(self):
        entries = jb_inspect.parse_git_porcelain_z(
            b" M path with spaces.txt\0"
            b"?? line\nbreak.txt\0"
            b"R  new \xe2\x86\x92 name.txt\0old name.txt\0"
        )

        self.assertEqual(
            entries,
            {
                "path with spaces.txt": " M",
                "line\nbreak.txt": "??",
                "new → name.txt": "R ",
            },
        )

    def test_agent_result_for_empty_model_capture_unknown_is_retryable(self):
        payload = {
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "verdict_reason": "inspection_trigger_empty_model",
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["bucket"], "capture_not_ready")
        self.assertTrue(payload["retry_policy"]["retry"])
        self.assertEqual(payload["agent_result"]["bucket"], "capture_not_ready")

    def test_agent_result_for_no_results_proof_failure_is_retryable(self):
        payload = {
            "status": "no_results",
            "proof_failures": ["no_results"],
            "wait": {"completion_reason": "no_results"},
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], "no_results")
        self.assertEqual(payload["bucket"], "capture_not_ready")
        self.assertTrue(payload["retry_policy"]["retry"])
        self.assertEqual(payload["agent_result"]["bucket"], "capture_not_ready")

    def test_mixed_proof_failures_stay_tool_bug(self):
        payload = {
            "status": "no_results",
            "proof_failures": [
                "no_results",
                "resolved_project_path does not match requested_path",
            ],
            "wait": {"completion_reason": "no_results"},
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], "inspection_proof_failed")
        self.assertEqual(payload["bucket"], "tool_bug")
        self.assertFalse(payload["retry_policy"]["retry"])

    def test_agent_result_for_unclassified_proof_failure_stays_tool_bug(self):
        payload = {
            "status": "unknown",
            "proof_failures": ["unexpected_contradiction"],
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], "inspection_proof_failed")
        self.assertEqual(payload["bucket"], "tool_bug")
        self.assertFalse(payload["retry_policy"]["retry"])

    def test_cleanup_failure_agent_result_preserves_inspection_result(self):
        payload = {
            "status": "clean",
            "clean": True,
            "inspection_result": {"verdict": "GREEN", "reason": "clean_confirmed"},
            "cleanup": {"status": "failed", "reason": "route_missing"},
            "cleanup_failed": True,
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["bucket"], "cleanup_not_clean")
        self.assertEqual(payload["inspection_result"]["verdict"], "GREEN")
        self.assertFalse(payload["retry_policy"]["retry"])

    def test_red_exit_semantics_differ_between_run_and_wait(self):
        payload = {"verdict": "RED", "verdict_reason": "actionable_findings"}

        self.assertEqual(jb_inspect.classify_run_exit(payload), 1)
        self.assertEqual(jb_inspect.classify_wait_exit(payload), 0)

    def test_print_human_is_concise_by_default(self):
        payload = {
            "status": "findings",
            "clean": False,
            "route": {
                "ide": {"name": "WebStorm"},
                "project_name": "example",
                "project_key": "path:/tmp/example",
                "base_path": "/tmp/example",
            },
            "total_problems": 1,
            "problems_shown": 1,
            "raw": {"large": "payload"},
            "problems": [{"severity": "warning", "file": "src/app.ts", "line": 12, "description": "Example finding"}],
        }
        output = io.StringIO()

        with redirect_stdout(output):
            jb_inspect.print_human(payload)

        text = output.getvalue()
        self.assertIn("ROUTE: WebStorm", text)
        self.assertIn("STATUS: findings", text)
        self.assertIn("VERDICT: RED", text)
        self.assertIn("AGENT_RESULT: bucket=actionable_findings retry=False", text)
        self.assertIn("NEXT_ACTION: Fix the reported findings", text)
        self.assertIn("SUMMARY: clean=False total_problems=1 problems_shown=1", text)
        self.assertIn("src/app.ts:12 Example finding", text)
        self.assertNotIn('"raw"', text)

    def test_status_human_output_is_concise(self):
        payload = {
            "status": "unknown",
            "clean": False,
            "route": {
                "ide": {"name": "IntelliJ IDEA"},
                "project_name": "example",
                "project_key": "path:/tmp/example",
                "base_path": "/tmp/example",
            },
            "capture_incomplete": True,
            "raw": {"large": "payload"},
        }
        output = io.StringIO()

        with redirect_stdout(output):
            jb_inspect.print_human(payload)

        text = output.getvalue()
        self.assertIn("ROUTE: IntelliJ IDEA", text)
        self.assertIn("STATUS: unknown", text)
        self.assertIn("VERDICT: UNKNOWN", text)
        self.assertIn("NEXT_ACTION:", text)
        self.assertIn("FLAGS: capture_incomplete", text)
        self.assertNotIn('"raw"', text)

    def test_human_output_summarizes_capture_diagnostic(self):
        payload = {
            "status": "capture_incomplete",
            "clean": False,
            "capture_incomplete": True,
            "capture_diagnostic": {
                "exit_reason": "deadline",
                "view_ready_ok": False,
                "observed_inspection_view": True,
                "inspection_view_updating": True,
                "successful_extraction_count": 3,
                "extraction_failure_count": 1,
                "polling_elapsed_ms": 60012,
            },
        }
        output = io.StringIO()

        with redirect_stdout(output):
            jb_inspect.print_human(payload)

        text = output.getvalue()
        self.assertIn("CAPTURE_DIAGNOSTIC:", text)
        self.assertIn("VERDICT: UNKNOWN", text)
        self.assertIn("exit_reason=deadline", text)
        self.assertIn("view_ready_ok=False", text)
        self.assertIn("successful_extraction_count=3", text)

    def test_human_output_identifies_missing_semantic_coverage(self):
        payload = {
            "status": "clean",
            "clean": True,
            "total_problems": 0,
            "capture_diagnostic": scope_capture_diagnostic(
                {
                    "path": "/tmp/PlaybackValidation.swift",
                    "valid": True,
                    "directory": False,
                    "file_type": "textmate",
                    "psi_language": "textmate",
                    "psi_class": "org.jetbrains.plugins.textmate.psi.TextMateFile",
                    "in_content": True,
                    "in_source": False,
                }
            ),
        }
        output = io.StringIO()

        with redirect_stdout(output):
            jb_inspect.print_human(payload)

        text = output.getvalue()
        self.assertIn("VERDICT: UNKNOWN reason=scope_semantic_coverage_missing", text)
        self.assertIn("SEMANTIC_COVERAGE: status=missing", text)
        self.assertIn("PlaybackValidation.swift", text)
        self.assertIn("language_hint=swift", text)
        self.assertIn("non_semantic_fallback", text)

    def test_human_output_explains_errors(self):
        payload = {
            "status": "error",
            "error_reason": "inspection_api_unavailable",
            "error_message": "No JetBrains inspection plugin instances discovered.",
            "command": "closeout",
            "exit_code": 3,
            "context": {
                "repo_path": "/tmp/repo",
                "worktree_root": "/tmp/repo",
                "ide": "PyCharm",
            },
            "hint": "Open the repo in PyCharm.",
        }
        output = io.StringIO()

        with redirect_stdout(output):
            jb_inspect.print_human(payload)

        text = output.getvalue()
        self.assertIn("STATUS: error", text)
        self.assertIn("VERDICT: UNKNOWN", text)
        self.assertIn("ERROR: reason=inspection_api_unavailable", text)
        self.assertIn("message=No JetBrains inspection plugin instances discovered.", text)
        self.assertIn("command=closeout", text)
        self.assertIn("CONTEXT: repo=/tmp/repo worktree=/tmp/repo ide=PyCharm", text)
        self.assertIn("HINT: Open the repo in PyCharm.", text)

    def test_human_output_prints_route_diagnostic(self):
        payload = {
            "status": "error",
            "error_reason": "timeout",
            "error_message": "Timed out waiting for the target JetBrains IDE plugin after hidden bootstrap.",
            "command": "closeout",
            "exit_code": 3,
            "context": {
                "repo_path": "/tmp/repo",
                "worktree_root": "/tmp/repo",
                "ide": "PyCharm",
            },
            "route_diagnostic": {
                "requested_ide": "PyCharm",
                "target_worktree": "/tmp/repo",
                "discovered_identity_count": 1,
                "matching_identity_count": 0,
                "discovered_project_count": 1,
                "matching_project_count": 0,
                "reason": "different_jetbrains_product_running",
                "identities": [
                    {
                        "ide_name": "IntelliJ IDEA 2026.1.2",
                        "ide_product_code": "IU",
                        "port": 63342,
                        "plugin_version": "1.12.10",
                        "plugin_build_fingerprint": "abc123-clean",
                        "open_project_count": 1,
                    }
                ],
                "other_projects": [
                    {
                        "ide_name": "IntelliJ IDEA 2026.1.2",
                        "ide_product_code": "IU",
                        "plugin_version": "1.12.10",
                        "plugin_build_fingerprint": "abc123-clean",
                        "name": "jetbrains-inspection-api",
                        "base_path": "/tmp/jetbrains-inspection-api",
                    }
                ],
                "next_action": "Open the worktree in PyCharm with the inspection plugin installed and up to date.",
            },
        }
        output = io.StringIO()

        with redirect_stdout(output):
            jb_inspect.print_human(payload)

        text = output.getvalue()
        self.assertIn("ROUTE_DIAGNOSTIC: requested_ide=PyCharm", text)
        self.assertIn("matching_identities=0", text)
        self.assertIn("reason=different_jetbrains_product_running", text)
        self.assertIn("ROUTE_IDENTITY: ide=IntelliJ IDEA 2026.1.2 product=IU", text)
        self.assertIn("plugin=1.12.10@abc123-clean", text)
        self.assertIn("ROUTE_OTHER_PROJECT: ide=IntelliJ IDEA 2026.1.2 product=IU plugin=1.12.10@abc123-clean name=jetbrains-inspection-api", text)
        self.assertIn("ROUTE_NEXT_ACTION: Open the worktree in PyCharm with the inspection plugin installed and up to date", text)

    def test_human_output_prints_blocked_project_open_diagnostic(self):
        payload = {
            "status": "error",
            "error_reason": "project_open_blocked",
            "error_message": "Timed out waiting for JetBrains IDE to open the exact worktree.",
            "command": "closeout",
            "exit_code": 3,
            "context": {"repo_path": "/tmp/repo", "worktree_root": "/tmp/repo", "ide": "PyCharm"},
            "blocked_diagnostic": {
                "reason": "jetbrains_project_open_blocked",
                "message": "JetBrains may be waiting on a Trust Project, safe-mode, or open-project prompt.",
                "requested_ide": "PyCharm",
                "target_worktree": "/tmp/repo",
                "background_open": True,
                "prepare_timeout_ms": 1234,
                "selected_trusted_root": "/tmp",
            },
        }
        output = io.StringIO()

        with redirect_stdout(output):
            jb_inspect.print_human(payload)

        text = output.getvalue()
        self.assertIn("PROJECT_OPEN_BLOCKED: reason=jetbrains_project_open_blocked", text)
        self.assertIn("requested_ide=PyCharm", text)
        self.assertIn("background_open=True", text)
        self.assertIn("prepare_timeout_ms=1234", text)
        self.assertIn("PROJECT_OPEN_BLOCKED_HINT: JetBrains may be waiting on a Trust Project, safe-mode, or open-project prompt.", text)

    def test_human_output_prints_zero_project_hint(self):
        payload = {
            "status": "ok",
            "projects": [],
            "count": 0,
            "zero_project_hint": jb_inspect.zero_project_hint(),
        }
        output = io.StringIO()

        with redirect_stdout(output):
            jb_inspect.print_human(payload)

        text = output.getvalue()
        self.assertIn("PROJECT_OPEN_HINT:", text)
        self.assertIn("Trust Project", text)
        self.assertIn("safe-mode", text)
        self.assertIn("open-project", text)

    def test_human_output_explains_status_bearing_timeout_errors(self):
        payload = jb_inspect.error_payload(
            jb_inspect.InspectError(
                "Timed out waiting for JetBrains indexing/scanning to settle.",
                3,
                {
                    "status": {"status": "indexing", "indexing": True, "is_scanning": False},
                    "route": {"ide": {"name": "PyCharm"}, "project_name": "repo"},
                },
            ),
            Namespace(command="closeout"),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            jb_inspect.print_human(payload)

        text = output.getvalue()
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_reason"], "timeout")
        self.assertEqual(payload["last_status"]["status"], "indexing")
        self.assertIn("STATUS: error", text)
        self.assertIn("ERROR: reason=timeout", text)
        self.assertIn("HINT: Increase the timeout", text)

    def test_inspect_error_payload_adds_reason_and_command(self):
        error = jb_inspect.InspectError("No JetBrains inspection plugin instances discovered.", 3)
        args = Namespace(command="closeout")

        payload = jb_inspect.error_payload(error, args)

        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_reason"], "inspection_api_unavailable")
        self.assertEqual(payload["error_message"], "No JetBrains inspection plugin instances discovered.")
        self.assertEqual(payload["command"], "inspect-closeout")
        self.assertEqual(payload["exit_code"], 3)
        self.assertIn("Open the repo", payload["hint"])

    def test_inspect_error_payload_classifies_target_project_not_open(self):
        error = jb_inspect.InspectError("No open JetBrains project matched this repo/worktree.", 3)

        payload = jb_inspect.error_payload(error, Namespace(command="resolve-route"))

        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_reason"], "target_project_not_open")
        self.assertEqual(payload["command"], "resolve-route")
        self.assertIn("inspect or prepare-worktree", payload["hint"])

    def test_inspect_error_payload_reports_input_alias_command(self):
        error = jb_inspect.InspectError("No open JetBrains project matched this repo/worktree.", 3)

        payload = jb_inspect.error_payload(error, Namespace(command="route", command_input="resolve-route"))

        self.assertEqual(payload["command"], "resolve-route")
        self.assertEqual(payload["error_reason"], "target_project_not_open")

    def test_inspect_error_payload_classifies_no_open_lifecycle_miss(self):
        error = jb_inspect.InspectError("Exact worktree is not open in a JetBrains IDE.", 3)

        payload = jb_inspect.error_payload(error, Namespace(command="run", command_input="inspect"))

        self.assertEqual(payload["command"], "inspect")
        self.assertEqual(payload["error_reason"], "target_project_not_open")
        self.assertIn("inspect or prepare-worktree", payload["hint"])

    def test_structured_route_error_reason_overrides_open_wording(self):
        error = jb_inspect.InspectError(
            "No open JetBrains project matched this repo/worktree.",
            3,
            {"error_reason": "target_project_not_open"},
        )

        payload = jb_inspect.error_payload(error, Namespace(command="resolve-route"))

        self.assertEqual(payload["error_reason"], "target_project_not_open")
        self.assertIn("inspect or prepare-worktree", payload["hint"])

    def test_inspect_error_payload_moves_structured_status_to_last_status(self):
        error = jb_inspect.InspectError(
            "Timed out waiting for JetBrains indexing/scanning to settle.",
            3,
            {"status": {"status": "indexing", "indexing": True}},
        )

        payload = jb_inspect.error_payload(error, Namespace(command="inspect-closeout"))

        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["last_status"], {"status": "indexing", "indexing": True})
        self.assertEqual(payload["error_reason"], "timeout")
        self.assertIn("Increase the timeout", payload["hint"])

    def test_inspect_error_payload_turns_scalar_status_into_reason(self):
        error = jb_inspect.InspectError("Lifecycle lock timed out.", 3, {"status": "timeout"})

        payload = jb_inspect.error_payload(error, Namespace(command="inspect-closeout"))

        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["reason"], "timeout")
        self.assertEqual(payload["error_reason"], "timeout")
        self.assertNotIn("last_status", payload)

    def test_json_error_payload_is_structured(self):
        payload = jb_inspect.error_payload(
            jb_inspect.InspectError("Inspection API returned invalid JSON: boom", 3),
            Namespace(command="get-problems"),
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = jb_inspect.emit(payload, json_only=True, exit_code=3)

        self.assertEqual(exit_code, 3)
        body = json.loads(output.getvalue())
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["error_reason"], "invalid_api_response")
        self.assertEqual(body["command"], "get-problems")
        self.assertEqual(body["exit_code"], 3)


class UnknownVerdictLogTest(unittest.TestCase):
    def test_emit_logs_unknown_verdict_with_rollout_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "unknown.jsonl"
            rollout_path = Path(tmp) / "rollout-123.jsonl"
            rollout_path.write_text('{"deployment":"fixture"}\n', encoding="utf-8")
            payload = {
                "command": "inspect-closeout",
                "status": "capture_incomplete",
                "capture_incomplete_reason": "non_empty_unmapped_tree",
                "verdict": "UNKNOWN",
                "verdict_reason": "non_empty_unmapped_tree",
                "verdict_message": "Inspection did not produce a trustworthy GREEN or RED result.",
                "verdict_next_action": "Treat this as a plugin/helper bug.",
                "context": {
                    "repo_path": "/repo",
                    "worktree_root": "/repo-wt",
                    "scope": "changed_files",
                },
                "route": {
                    "project_name": "repo-wt",
                    "project_key": "path:/repo-wt",
                    "base_path": "/repo-wt",
                    "ide": {"name": "IntelliJ IDEA"},
                },
                "capture_diagnostic": {"exit_reason": "non_empty_unmapped_tree"},
                "authorization": "secret-token",
            }

            output = io.StringIO()
            with patch.dict(os.environ, {
                jb_inspect.UNKNOWN_LOG_ENV: str(log_path),
                "JB_INSPECT_ROLLOUT_FILE": str(rollout_path),
            }, clear=False):
                with redirect_stdout(output):
                    exit_code = jb_inspect.emit(payload, json_only=False, exit_code=1)

            self.assertEqual(exit_code, 1)
            self.assertIn(f"UNKNOWN_LOG: {log_path.resolve()}", output.getvalue())
            records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["verdict"], "UNKNOWN")
            self.assertEqual(record["bucket"], "tool_bug")
            self.assertFalse(record["retry"])
            self.assertEqual(record["verdict_reason"], "non_empty_unmapped_tree")
            self.assertEqual(record["schema_version"], 2)
            self.assertEqual(record["rollout_file_hash"], jb_inspect.file_content_sha256(rollout_path))
            self.assertEqual(record["deployment_manifest_sha256"], jb_inspect.file_content_sha256(rollout_path))
            self.assertEqual(record["repo_path_hash"], jb_inspect.stable_value_hash("/repo"))
            self.assertEqual(record["ide"], "IntelliJ IDEA")
            self.assertEqual(record["capture_diagnostic"]["exit_reason"], "non_empty_unmapped_tree")
            self.assertNotIn("secret-token", log_path.read_text(encoding="utf-8"))
            self.assertNotIn("/repo-wt", log_path.read_text(encoding="utf-8"))

    def test_outcome_path_is_resolved_inside_routing_lock(self):
        order = []

        class RoutingLock:
            def __enter__(self):
                order.append("lock_enter")

            def __exit__(self, exc_type, exc, tb):
                order.append("lock_exit")

        payload = qualification_event("routing-lock")

        def resolve_path():
            order.append("resolve_path")
            return Path("/tmp/outcomes.jsonl")

        def append_record(path, record):
            order.append("append")
            self.assertEqual(path, Path("/tmp/outcomes.jsonl"))
            self.assertEqual(record["event_kind"], "inspection_assessment")

        with (
            patch.object(jb_inspect, "outcome_routing_lock", return_value=RoutingLock()),
            patch.object(jb_inspect, "outcome_log_path", side_effect=resolve_path),
            patch.object(jb_inspect, "append_jsonl_record", side_effect=append_record),
        ):
            jb_inspect.log_outcome(payload, 0)

        self.assertEqual(order, ["lock_enter", "resolve_path", "append", "lock_exit"])
        self.assertEqual(payload["outcome_log_path"], "/tmp/outcomes.jsonl")

    def test_unknown_and_outcome_records_share_one_routing_lock(self):
        order = []

        class RoutingLock:
            def __enter__(self):
                order.append("lock_enter")

            def __exit__(self, exc_type, exc, tb):
                order.append("lock_exit")

        payload = qualification_event("routing-lock-unknown", verdict="UNKNOWN")

        with (
            patch.object(jb_inspect, "outcome_routing_lock", return_value=RoutingLock()),
            patch.object(
                jb_inspect,
                "write_unknown_verdict_record",
                side_effect=lambda candidate: order.append("unknown_record"),
            ),
            patch.object(
                jb_inspect,
                "write_outcome_record",
                side_effect=lambda candidate, exit_code: order.append("outcome_record"),
            ),
        ):
            jb_inspect.log_assessment_records(payload, 1)

        self.assertEqual(order, ["lock_enter", "unknown_record", "outcome_record", "lock_exit"])

    @unittest.skipIf(jb_inspect.fcntl is None, "requires advisory file locking")
    def test_blocked_emitter_resolves_current_after_routing_lock_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            old_deployment = root / "old"
            new_deployment = root / "new"
            old_deployment.mkdir()
            new_deployment.mkdir()
            current = root / "current"
            current.symlink_to(old_deployment, target_is_directory=True)
            marker = root / "child-started"
            child_code = f"""
import importlib.util
import os
import sys
from pathlib import Path
script = Path({str(SCRIPT_PATH)!r})
spec = importlib.util.spec_from_file_location('jb_inspect_child', script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
Path({str(marker)!r}).write_text('started', encoding='utf-8')
module.log_outcome({{
    'command': 'inspect-closeout',
    'verdict': 'GREEN',
    'verdict_reason': 'no_matching_findings',
    'status': 'clean',
    'context': {{'client_run_id': 'routing-test', 'scope': 'changed_files'}},
}}, 0)
"""
            environment = os.environ.copy()
            environment.update({
                "JETBRAINS_INSPECTION_CACHE_DIR": str(cache),
                jb_inspect.OUTCOME_LOG_ENV: str(current / "outcomes.jsonl"),
                jb_inspect.UNKNOWN_LOG_ENV: "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            environment.pop(jb_inspect.DEPLOYMENT_MANIFEST_ENV, None)

            with patch.dict(os.environ, {"JETBRAINS_INSPECTION_CACHE_DIR": str(cache)}, clear=False):
                with jb_inspect.outcome_routing_lock(timeout_ms=5_000):
                    child = subprocess.Popen(
                        [sys.executable, "-c", child_code],
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(marker.exists(), "child did not reach the routing lock")
                    time.sleep(0.1)
                    replacement = root / ".current-new"
                    replacement.symlink_to(new_deployment, target_is_directory=True)
                    os.replace(replacement, current)

            stdout, stderr = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, f"stdout={stdout}\nstderr={stderr}")
            self.assertFalse((old_deployment / "outcomes.jsonl").exists())
            records = [
                json.loads(line)
                for line in (new_deployment / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["client_run_id"], "routing-test")

    def test_routing_lock_uses_windows_file_lock_fallback(self):
        class FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            def __init__(self):
                self.calls = []

            def locking(self, descriptor, mode, size):
                self.calls.append((mode, size))

        fake_msvcrt = FakeMsvcrt()
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(os.environ, {"JETBRAINS_INSPECTION_CACHE_DIR": tmp}, clear=False),
                patch.object(jb_inspect, "fcntl", None),
                patch.object(jb_inspect, "msvcrt", fake_msvcrt),
            ):
                with jb_inspect.outcome_routing_lock(timeout_ms=0):
                    pass

        self.assertEqual(fake_msvcrt.calls, [(fake_msvcrt.LK_NBLCK, 1), (fake_msvcrt.LK_UNLCK, 1)])

    @unittest.skipIf(jb_inspect.fcntl is None, "requires POSIX file locking")
    def test_outcome_append_lock_timeout_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "outcomes.jsonl"
            with patch.object(jb_inspect.fcntl, "flock", side_effect=BlockingIOError()):
                with self.assertRaisesRegex(TimeoutError, "outcome log lock"):
                    jb_inspect.append_jsonl_record(log_path, {"event_id": "blocked"}, lock_timeout_ms=0)

    def test_outcome_append_uses_windows_file_lock_fallback(self):
        class FakeMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            def __init__(self):
                self.calls = []

            def locking(self, descriptor, mode, size):
                self.calls.append((mode, size))

        fake_msvcrt = FakeMsvcrt()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "outcomes.jsonl"
            with (
                patch.object(jb_inspect, "fcntl", None),
                patch.object(jb_inspect, "msvcrt", fake_msvcrt),
            ):
                jb_inspect.append_jsonl_record(log_path, {"event_id": "windows"}, lock_timeout_ms=0)

            self.assertEqual(json.loads(log_path.read_text(encoding="utf-8")), {"event_id": "windows"})

        self.assertEqual(fake_msvcrt.calls, [(fake_msvcrt.LK_NBLCK, 1), (fake_msvcrt.LK_UNLCK, 1)])

    def test_outcome_append_windows_lock_timeout_is_bounded(self):
        class BlockingMsvcrt:
            LK_NBLCK = 1
            LK_UNLCK = 2

            def locking(self, descriptor, mode, size):
                raise OSError("busy")

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "outcomes.jsonl"
            with (
                patch.object(jb_inspect, "fcntl", None),
                patch.object(jb_inspect, "msvcrt", BlockingMsvcrt()),
            ):
                with self.assertRaisesRegex(TimeoutError, "outcome log lock"):
                    jb_inspect.append_jsonl_record(log_path, {"event_id": "blocked"}, lock_timeout_ms=0)

            self.assertEqual(log_path.read_bytes(), b"")

    def test_human_output_surfaces_outcome_log_error(self):
        output = io.StringIO()

        with redirect_stdout(output):
            jb_inspect.print_human({"status": "clean", "outcome_log_error": "routing lock timeout"})

        self.assertIn("OUTCOME_LOG_ERROR: routing lock timeout", output.getvalue())

    def test_rollout_attribution_requires_explicit_session_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "sessions" / "2026" / "rollout-other-session.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text("{}\n", encoding="utf-8")
            environment = {"CODE_HOME": tmp} | {name: "" for name in jb_inspect.ROLLOUT_FILE_ENVS}

            with patch.dict(os.environ, environment, clear=False):
                self.assertIsNone(jb_inspect.discover_rollout_file())

    def test_emit_does_not_log_green_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "unknown.jsonl"
            payload = {
                "status": "results_available",
                "total_problems": 0,
                "problems_shown": 0,
                "problems": [],
            }

            output = io.StringIO()
            with patch.dict(os.environ, {jb_inspect.UNKNOWN_LOG_ENV: str(log_path)}, clear=False):
                with redirect_stdout(output):
                    exit_code = jb_inspect.emit(payload, json_only=True, exit_code=0)

            self.assertEqual(exit_code, 0)
            self.assertFalse(log_path.exists())
            body = json.loads(output.getvalue())
            self.assertNotIn("unknown_log_path", body)

    def test_emit_logs_all_outcomes_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome_path = Path(tmp) / "outcomes.jsonl"
            payload = {
                "status": "results_available",
                "total_problems": 0,
                "problems_shown": 0,
                "problems": [],
                "context": {
                    "scope": "changed_files",
                    "ide_selection": {
                        "product": "IntelliJ IDEA",
                        "channel": "stable",
                        "version": "2026.1",
                        "explicit_eap": False,
                    },
                },
                "agent_result": {
                    "next_action": "No inspection action required for this scope/filter.",
                    "agent_report": "JetBrains inspection passed for the selected scope.",
                    "retry_policy": {"retry": False, "max_attempts": 0, "wait_ms": 0},
                },
            }

            output = io.StringIO()
            with patch.dict(os.environ, {jb_inspect.OUTCOME_LOG_ENV: str(outcome_path)}, clear=False):
                with redirect_stdout(output):
                    exit_code = jb_inspect.emit(payload, json_only=True, exit_code=0, command="inspect-closeout")

            self.assertEqual(exit_code, 0)
            body = json.loads(output.getvalue())
            self.assertEqual(body["bucket"], "clean")
            records = [json.loads(line) for line in outcome_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["verdict"], "GREEN")
            self.assertEqual(records[0]["bucket"], "clean")
            self.assertEqual(records[0]["command"], "inspect-closeout")
            self.assertEqual(records[0]["ide_channel"], "stable")
            self.assertFalse(records[0]["eap_explicit"])
            self.assertEqual(records[0]["retry_wait_ms"], 0)
            self.assertEqual(records[0]["next_action"], "No inspection action required for this scope/filter.")
            self.assertEqual(records[0]["agent_report"], "JetBrains inspection passed for the selected scope.")

    def test_summarize_outcome_log_counts_agent_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome_path = Path(tmp) / "outcomes.jsonl"
            outcome_path.write_text(
                "\n".join(
                    [
                        json.dumps({
                            "timestamp": "2026-07-04T00:00:00Z",
                            "command": "inspect-closeout",
                            "verdict": "UNKNOWN",
                            "bucket": "ide_not_ready",
                            "retry": True,
                            "ide_channel": "stable",
                            "cleanup_status": "deferred",
                            "repo_path": "/secret/repo",
                        }),
                        "not-json",
                        json.dumps({
                            "timestamp": "2026-07-04T00:01:00Z",
                            "command": "inspect-closeout",
                            "verdict": "GREEN",
                            "bucket": "clean",
                            "retry": False,
                            "ide_channel": "stable",
                            "cleanup_status": "closed",
                            "worktree_root": "/secret/worktree",
                        }),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            summary = jb_inspect.summarize_outcome_log(outcome_path, limit=1)

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["events"], 2)
            self.assertEqual(summary["invalid_lines"], 1)
            self.assertEqual(summary["summary"]["by_verdict"], {"GREEN": 1, "UNKNOWN": 1})
            self.assertEqual(summary["summary"]["by_bucket"], {"clean": 1, "ide_not_ready": 1})
            self.assertEqual(summary["summary"]["by_retry"], {"false": 1, "true": 1})
            self.assertEqual(summary["summary"]["by_cleanup_status"], {"closed": 1, "deferred": 1})
            self.assertEqual(summary["summary"]["retryable_unknowns"], 1)
            self.assertEqual(len(summary["recent"]), 1)
            self.assertEqual(summary["recent"][0]["bucket"], "clean")
            self.assertNotIn("repo_path", summary["recent"][0])
            self.assertNotIn("worktree_root", json.dumps(summary))

    def test_summarize_outcome_log_missing_path_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = jb_inspect.summarize_outcome_log(Path(tmp) / "missing.jsonl")

            self.assertEqual(summary["status"], "missing")
            self.assertEqual(summary["events"], 0)
            self.assertEqual(summary["summary"], jb_inspect.empty_outcome_summary())

    def test_command_summarize_outcomes_respects_disabled_env(self):
        with patch.dict(os.environ, {jb_inspect.OUTCOME_LOG_ENV: "0"}, clear=False):
            summary = jb_inspect.command_summarize_outcomes(Namespace(log_path=None, limit=10))

        self.assertEqual(summary["status"], "disabled")
        self.assertEqual(summary["events"], 0)

    def test_command_summarize_outcomes_reads_explicit_log_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome_path = Path(tmp) / "custom-outcomes.jsonl"
            outcome_path.write_text(
                json.dumps({"verdict": "GREEN", "bucket": "clean", "command": "inspect-closeout"}) + "\n",
                encoding="utf-8",
            )

            summary = jb_inspect.command_summarize_outcomes(Namespace(log_path=str(outcome_path), limit=5))

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["events"], 1)
            self.assertEqual(summary["summary"]["by_verdict"], {"GREEN": 1})

    def test_summarize_outcomes_emit_does_not_attach_assessment_verdict_or_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome_path = Path(tmp) / "outcomes.jsonl"
            summary = {"status": "ok", "events": 0, "invalid_lines": 0, "summary": jb_inspect.empty_outcome_summary(), "recent": []}
            output = io.StringIO()

            with patch.dict(os.environ, {jb_inspect.OUTCOME_LOG_ENV: str(outcome_path)}, clear=False):
                with redirect_stdout(output):
                    exit_code = jb_inspect.emit(summary, json_only=True, exit_code=0, command="summarize-outcomes", assess=False)

            self.assertEqual(exit_code, 0)
            body = json.loads(output.getvalue())
            self.assertEqual(body["command"], "summarize-outcomes")
            self.assertNotIn("verdict", body)
            self.assertFalse(outcome_path.exists())

    def test_summarize_outcomes_human_output_renders_counts_without_verdict(self):
        summary = {
            "status": "ok",
            "path": "/tmp/outcomes.jsonl",
            "events": 3,
            "invalid_lines": 1,
            "summary": {
                "by_verdict": {"GREEN": 2, "UNKNOWN": 1},
                "by_bucket": {"clean": 2, "stale_results": 1},
                "by_command": {"inspect-closeout": 3},
                "by_retry": {"false": 2, "true": 1},
                "by_ide_channel": {"stable": 3},
                "by_cleanup_status": {"closed": 3},
                "retryable_unknowns": 1,
            },
            "recent": [],
        }
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = jb_inspect.emit(
                summary,
                json_only=False,
                exit_code=0,
                command="summarize-outcomes",
                assess=False,
            )

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("STATUS: ok", rendered)
        self.assertIn("OUTCOMES: events=3 invalid_lines=1 path=/tmp/outcomes.jsonl", rendered)
        self.assertIn("BY_VERDICT: GREEN=2 UNKNOWN=1", rendered)
        self.assertIn("RETRYABLE_UNKNOWNS: 1", rendered)
        self.assertFalse(any(line.startswith("VERDICT:") for line in rendered.splitlines()))

    def test_outcome_log_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome_path = Path(tmp) / "outcomes.jsonl"
            payload = {"status": "clean", "clean": True, "total_problems": 0, "problems": []}

            with patch.dict(os.environ, {jb_inspect.OUTCOME_LOG_ENV: "0"}, clear=False):
                jb_inspect.log_outcome(payload, 0)

            self.assertFalse(outcome_path.exists())

    def test_emit_does_not_log_informational_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            outcome_path = Path(tmp) / "outcomes.jsonl"
            output = io.StringIO()

            with patch.dict(os.environ, {jb_inspect.OUTCOME_LOG_ENV: str(outcome_path)}, clear=False):
                with redirect_stdout(output):
                    exit_code = jb_inspect.emit({"status": "resolved"}, json_only=True, exit_code=0, command="resolve-route")

            self.assertEqual(exit_code, 0)
            self.assertFalse(outcome_path.exists())

    def test_outcome_log_error_is_nonfatal(self):
        payload = {"command": "inspect-closeout", "status": "clean", "clean": True}
        jb_inspect.apply_verdict(payload)

        with patch.dict(os.environ, {jb_inspect.OUTCOME_LOG_ENV: "/dev/null/outcomes.jsonl"}, clear=False):
            jb_inspect.log_outcome(payload, 0)

        self.assertIn("outcome_log_error", payload)

    def test_emit_does_not_log_informational_command_unknowns(self):
        cases = [
            ("list-projects", {"status": "ok", "projects": []}),
            ("resolve-route", {"status": "resolved", "route": {}}),
            ("start-inspection", {"status": "triggered"}),
            ("claim-worktree", {"status": "claimed"}),
            ("prepare-worktree", {"status": "prepared"}),
            ("cleanup-helper-leases", {"status": "ok", "removed": []}),
        ]
        for command, payload in cases:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                log_path = Path(tmp) / "unknown.jsonl"

                output = io.StringIO()
                with patch.dict(os.environ, {jb_inspect.UNKNOWN_LOG_ENV: str(log_path)}, clear=False):
                    with redirect_stdout(output):
                        exit_code = jb_inspect.emit(payload, json_only=True, exit_code=0, command=command)

                self.assertEqual(exit_code, 0)
                self.assertFalse(log_path.exists())
                body = json.loads(output.getvalue())
                self.assertEqual(body["command"], command)
                self.assertEqual(body["verdict"], "UNKNOWN")
                self.assertNotIn("unknown_log_path", body)

    def test_emit_does_not_log_informational_status_without_command(self):
        cases = ["ok", "prepared", "resolved", "triggered", "claimed"]
        for status in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                log_path = Path(tmp) / "unknown.jsonl"

                output = io.StringIO()
                with patch.dict(os.environ, {jb_inspect.UNKNOWN_LOG_ENV: str(log_path)}, clear=False):
                    with redirect_stdout(output):
                        exit_code = jb_inspect.emit({"status": status}, json_only=True, exit_code=0)

                self.assertEqual(exit_code, 0)
                self.assertFalse(log_path.exists())
                body = json.loads(output.getvalue())
                self.assertEqual(body["status"], status)
                self.assertEqual(body["verdict"], "UNKNOWN")
                self.assertNotIn("unknown_log_path", body)

    def test_emit_logs_error_unknown_even_when_command_is_resolve_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "unknown.jsonl"
            payload = {
                "command": "resolve-route",
                "status": "error",
                "error_reason": "ide_open_failed",
                "verdict": "UNKNOWN",
                "verdict_reason": "ide_open_failed",
            }

            with patch.dict(os.environ, {jb_inspect.UNKNOWN_LOG_ENV: str(log_path)}, clear=False):
                with redirect_stdout(io.StringIO()):
                    exit_code = jb_inspect.emit(payload, json_only=True, exit_code=1)

            self.assertEqual(exit_code, 1)
            records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["verdict_reason"], "ide_open_failed")

    def test_unknown_log_records_preferred_command_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "unknown.jsonl"
            payload = {
                "command": "route",
                "status": "error",
                "error_reason": "ide_open_failed",
                "verdict": "UNKNOWN",
                "verdict_reason": "ide_open_failed",
            }

            with patch.dict(os.environ, {jb_inspect.UNKNOWN_LOG_ENV: str(log_path)}, clear=False):
                jb_inspect.log_unknown_verdict(payload)

            records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["command"], "resolve-route")

    def test_unknown_log_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "unknown.jsonl"
            payload = {"status": "no_results", "verdict": "UNKNOWN", "verdict_reason": "no_results"}

            with patch.dict(os.environ, {jb_inspect.UNKNOWN_LOG_ENV: "0"}, clear=False):
                jb_inspect.log_unknown_verdict(payload)

            self.assertFalse(log_path.exists())
            self.assertNotIn("unknown_log_path", payload)

    def test_jsonl_append_uses_one_locked_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "events.jsonl"
            with patch.object(jb_inspect.os, "write", wraps=jb_inspect.os.write) as write_call:
                if jb_inspect.fcntl is None:
                    jb_inspect.append_jsonl_record(log_path, {"event_id": "one"})
                    lock_calls = []
                else:
                    with patch.object(jb_inspect.fcntl, "flock", wraps=jb_inspect.fcntl.flock) as lock_call:
                        jb_inspect.append_jsonl_record(log_path, {"event_id": "one"})
                    lock_calls = [call.args[1] for call in lock_call.call_args_list]
            written_record = json.loads(log_path.read_text(encoding="utf-8"))

        self.assertEqual(write_call.call_count, 1)
        if jb_inspect.fcntl is not None:
            self.assertEqual(
                lock_calls,
                [jb_inspect.fcntl.LOCK_EX | jb_inspect.fcntl.LOCK_NB, jb_inspect.fcntl.LOCK_UN],
            )
        self.assertEqual(written_record, {"event_id": "one"})

    def test_jsonl_append_rolls_back_partial_write_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "events.jsonl"
            log_path.write_bytes(b'{"event_id":"existing"}\n')
            original = log_path.read_bytes()
            real_write = jb_inspect.os.write
            call_count = 0

            def partial_then_fail(descriptor, payload):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return real_write(descriptor, bytes(payload[:5]))
                raise OSError("simulated append failure")

            with patch.object(jb_inspect.os, "write", side_effect=partial_then_fail):
                with self.assertRaisesRegex(OSError, "simulated append failure"):
                    jb_inspect.append_jsonl_record(log_path, {"event_id": "new"})

            self.assertEqual(log_path.read_bytes(), original)


class StrictOutcomeQualificationTest(unittest.TestCase):
    def summarize(self, events: list[dict | str], *, criteria: dict | None = None, sample_size: int = 1) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_path = root / "outcomes.jsonl"
            qualification_path = root / "qualification.json"
            log_path.write_text(
                "\n".join(event if isinstance(event, str) else json.dumps(event, sort_keys=True) for event in events) + "\n",
                encoding="utf-8",
            )
            write_json(qualification_path, criteria or qualification_payload())
            return jb_inspect.command_summarize_outcomes(
                Namespace(
                    log_path=str(log_path),
                    limit=10,
                    qualification_file=str(qualification_path),
                    sample_size=sample_size,
                )
            )

    def test_mixed_historical_logs_preserve_diagnostic_mode_and_strict_boundary(self):
        historical = {
            "timestamp": "2026-07-25T23:59:59Z",
            "command": "inspect-closeout",
            "verdict": "GREEN",
            "bucket": "clean",
        }
        current = qualification_event("current")
        observation = qualification_event(
            "observation",
            assessment_id="observation-assessment",
            timestamp="2026-07-26T00:00:02.000Z",
            event_kind="inspection_observation",
            command="get-status",
        )

        strict = self.summarize([historical, current, observation])

        self.assertEqual(strict["gate_status"], "pass")
        self.assertEqual(strict["sample_count"], 1)
        self.assertEqual(strict["qualifying_sample"][0]["assessment_id"], "assessment-current")
        self.assertEqual(strict["exclusion_counts"], {"non_assessment_command": 1})
        self.assertNotIn("2026-07-25", json.dumps(strict))

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "outcomes.jsonl"
            log_path.write_text(json.dumps(historical) + "\n" + json.dumps(current) + "\n", encoding="utf-8")
            diagnostic = jb_inspect.command_summarize_outcomes(Namespace(log_path=str(log_path), limit=10))

        self.assertEqual(diagnostic["status"], "ok")
        self.assertEqual(diagnostic["events"], 2)
        self.assertEqual(diagnostic["summary"]["by_verdict"], {"GREEN": 2})

    def test_boundary_event_and_exact_duplicate_handling_are_deterministic(self):
        before = qualification_event("before", timestamp="2026-07-26T00:00:01.000Z")
        anchor = qualification_event(
            "anchor",
            assessment_id="anchor-assessment",
            timestamp="2026-07-26T00:00:02.000Z",
            event_kind="inspection_observation",
            command="get-status",
        )
        current = qualification_event("current", timestamp="2026-07-26T00:00:03.000Z")
        criteria = qualification_payload(after_event_id="anchor")

        result = self.summarize([before, anchor, before, current, current], criteria=criteria)

        self.assertEqual(result["gate_status"], "pass")
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["qualifying_sample"][0]["assessment_id"], "assessment-current")
        self.assertEqual(result["exclusion_counts"], {"duplicate_event": 2})
        self.assertNotIn("assessment-before", json.dumps(result["groups"]))

    def test_timestamp_only_boundary_rejects_malformed_logs(self):
        result = self.summarize(['{"broken"', qualification_event("current")])

        self.assertEqual(result["gate_status"], "fail")
        self.assertEqual(result["error_reason"], "boundary_event_required_for_invalid_log")

        indeterminate = qualification_event("indeterminate")
        indeterminate.pop("timestamp")
        indeterminate.pop("timestamp_ms")
        indeterminate_result = self.summarize([indeterminate, qualification_event("current")])

        self.assertEqual(indeterminate_result["gate_status"], "fail")
        self.assertEqual(indeterminate_result["error_reason"], "boundary_event_required_for_invalid_log")

        boolean_timestamp = qualification_event("boolean-timestamp")
        boolean_timestamp.pop("timestamp")
        boolean_timestamp["timestamp_ms"] = True
        boolean_result = self.summarize([boolean_timestamp, qualification_event("current")])

        self.assertEqual(boolean_result["gate_status"], "fail")
        self.assertEqual(boolean_result["error_reason"], "boundary_event_required_for_invalid_log")

        mixed_timestamp = qualification_event(
            "mixed-timestamp",
            timestamp="2026-07-25T23:59:59.000Z",
        )
        mixed_timestamp["timestamp_ms"] = True
        mixed_result = self.summarize([mixed_timestamp, qualification_event("current")])

        self.assertEqual(mixed_result["gate_status"], "fail")
        self.assertEqual(mixed_result["error_reason"], "boundary_event_required_for_invalid_log")

    def test_boundary_event_exposes_post_boundary_invalid_json(self):
        anchor = qualification_event(
            "anchor",
            assessment_id="anchor-assessment",
            event_kind="inspection_observation",
            command="get-status",
        )
        current = qualification_event("current", timestamp="2026-07-26T00:00:03.000Z")
        result = self.summarize(
            [anchor, '{"broken"', current],
            criteria=qualification_payload(after_event_id="anchor"),
        )

        self.assertEqual(result["gate_status"], "fail")
        self.assertEqual(result["hard_failure_counts"]["invalid_json"], 1)

    def test_post_boundary_legacy_and_missing_event_identity_are_hard_exclusions(self):
        legacy = {
            "schema_version": 1,
            "timestamp": "2026-07-26T00:00:01.000Z",
            "command": "inspect-closeout",
            "verdict": "GREEN",
        }
        missing_event_id = qualification_event("missing-id", timestamp="2026-07-26T00:00:02.000Z")
        missing_event_id.pop("event_id")

        result = self.summarize([legacy, missing_event_id])

        self.assertEqual(result["gate_status"], "fail")
        self.assertEqual(result["hard_failure_counts"]["legacy_schema"], 1)
        self.assertEqual(result["hard_failure_counts"]["missing_event_id"], 1)

    def test_missing_identity_and_provenance_reasons_fail_closed(self):
        mutations = {
            "missing_client_run_id": ("client_run_id", "assessment_id"),
            "missing_inspection_run_id": ("inspection_run_id",),
            "missing_scope": ("scope_descriptor",),
            "missing_cleanup_status": ("cleanup_status",),
            "missing_ide_product_code": ("ide_product_code",),
            "missing_ide_version": ("ide_version",),
            "missing_ide_channel": ("ide_channel",),
        }
        for expected_reason, removed_keys in mutations.items():
            with self.subTest(reason=expected_reason):
                event = qualification_event(expected_reason)
                for key in removed_keys:
                    event.pop(key, None)
                result = self.summarize([event])

                self.assertEqual(result["gate_status"], "fail")
                self.assertEqual(result["hard_failure_counts"].get(expected_reason), 1)

        mismatched_assessment = qualification_event("assessment-mismatch")
        mismatched_assessment["assessment_id"] = "different-assessment"
        mismatch_result = self.summarize([mismatched_assessment])

        self.assertEqual(mismatch_result["gate_status"], "fail")
        self.assertEqual(mismatch_result["hard_failure_counts"].get("assessment_id_mismatch"), 1)

        for key, expected_reason in (
            ("repo_path_hash", "missing_repo_hash"),
            ("worktree_root_hash", "missing_worktree_hash"),
            ("project_key_hash", "missing_project_hash"),
        ):
            with self.subTest(invalid_hash=key):
                event = qualification_event(f"invalid-{key}")
                event[key] = "sha256:not-a-full-digest"
                if key == "project_key_hash":
                    event["inspection_attribution"][key] = event[key]
                result = self.summarize([event])

                self.assertEqual(result["gate_status"], "fail")
                self.assertEqual(result["hard_failure_counts"].get(expected_reason), 1)

    def test_artifact_mismatches_are_hard_failures(self):
        helper_mismatch = qualification_event("helper")
        helper_mismatch["helper_revision"] = "sha256:" + "3" * 64
        plugin_mismatch = qualification_event("plugin")
        plugin_mismatch["plugin_build_fingerprint"] = "c" * 40 + "-clean"
        deployment_mismatch = qualification_event("deployment")
        deployment_mismatch["deployment_manifest_sha256"] = "sha256:" + "4" * 64

        result = self.summarize([helper_mismatch, plugin_mismatch, deployment_mismatch])

        self.assertEqual(result["gate_status"], "fail")
        self.assertEqual(result["hard_failure_counts"]["helper_revision_mismatch"], 1)
        self.assertEqual(result["hard_failure_counts"]["plugin_fingerprint_mismatch"], 1)
        self.assertEqual(result["hard_failure_counts"]["deployment_manifest_mismatch"], 1)

    def test_rows_fail_when_plugin_attribution_or_authoritative_channel_is_absent(self):
        missing_attribution = qualification_event("missing-attribution")
        missing_attribution["inspection_attribution"] = {
            "source": "helper",
            "classification": "decisive",
        }
        missing_channel_source = qualification_event("missing-channel-source")
        missing_channel_source["ide_channel_source"] = "selector_fallback"

        result = self.summarize([missing_attribution, missing_channel_source], sample_size=2)

        self.assertEqual(result["gate_status"], "fail")
        self.assertEqual(result["hard_failure_counts"]["inspection_attribution_mismatch"], 2)

        unknown = qualification_event("unknown", verdict="UNKNOWN")
        unknown["inspection_attribution"]["classification"] = "unattributed"
        unknown_result = self.summarize([unknown])

        self.assertEqual(unknown_result["gate_status"], "fail")
        self.assertEqual(unknown_result["hard_failure_counts"]["inspection_attribution_mismatch"], 1)

        decisive = qualification_event("decisive")
        decisive["inspection_attribution"]["code"] = "actionable_findings"
        decisive_result = self.summarize([decisive])

        self.assertEqual(decisive_result["gate_status"], "fail")
        self.assertEqual(decisive_result["hard_failure_counts"]["inspection_attribution_mismatch"], 1)

        for field, mismatch in (
            ("endpoint", "/api/inspection/status"),
            ("http_status", 500),
            ("request_id", "different-request"),
            ("observed_by", "different-observer"),
            ("ide_channel_source", "selector_fallback"),
        ):
            with self.subTest(attribution_field=field):
                event = qualification_event(f"mismatch-{field}")
                event["inspection_attribution"][field] = mismatch
                result = self.summarize([event])

                self.assertEqual(result["gate_status"], "fail")
                self.assertEqual(result["hard_failure_counts"]["inspection_attribution_mismatch"], 1)

    def test_explicit_text_only_helper_attribution_qualifies_narrowly(self):
        event = qualification_event("text-only-helper")
        event["verdict_reason"] = "text_only_coverage_allowed"
        event["response_code"] = "text_only_coverage_allowed"
        event["scope_descriptor"]["allow_text_only_coverage"] = True
        event["scope_descriptor_sha256"] = jb_inspect.canonical_json_sha256(event["scope_descriptor"])
        event["inspection_attribution"].update({
            "source": "helper",
            "code": "text_only_coverage_allowed",
        })
        event["internal_attempts"][0].update({
            "verdict_reason": "text_only_coverage_allowed",
            "proof_failures": ["scope_semantic_coverage_missing"],
        })
        event["total_problems"] = 0

        result = self.summarize([event])

        self.assertEqual(result["gate_status"], "pass")
        self.assertEqual(result["hard_failure_count"], 0)

        for label, mutate in (
            ("override-disabled", lambda candidate: candidate["scope_descriptor"].update({"allow_text_only_coverage": False})),
            ("missing-proof", lambda candidate: candidate["internal_attempts"][0].pop("proof_failures")),
            ("malformed-proof", lambda candidate: candidate["internal_attempts"][0].update({"proof_failures": [{}]})),
            ("wrong-verdict-reason", lambda candidate: candidate.update({"verdict_reason": "no_matching_findings"})),
        ):
            with self.subTest(label=label):
                invalid = json.loads(json.dumps(event))
                mutate(invalid)
                invalid["scope_descriptor_sha256"] = jb_inspect.canonical_json_sha256(invalid["scope_descriptor"])
                invalid_result = self.summarize([invalid])

                self.assertEqual(invalid_result["gate_status"], "fail")
                self.assertEqual(invalid_result["hard_failure_counts"]["inspection_attribution_mismatch"], 1)

    def test_configuration_blocked_is_excluded_only_before_start_with_exact_selector_code(self):
        valid = qualification_event("valid-config", verdict="UNKNOWN")
        valid.update({
            "inspection_started": False,
            "attribution_class": "configuration_blocked",
            "response_code": "ide_selection_required",
            "failure_phase": "selection",
            "cleanup_status": "not_needed",
            "observed_by": "helper",
            "status": "error",
            "bucket": "policy_required",
            "verdict_reason": "ide_selection_required",
        })
        for key in (
            "inspection_run_id",
            "plugin_build_fingerprint",
            "plugin_version",
            "ide_product_code",
            "ide_version",
            "ide_channel",
            "ide_channel_source",
            "session_id",
            "project_instance_id",
            "endpoint",
            "http_status",
            "request_id",
        ):
            valid.pop(key, None)
        valid["inspection_attribution"] = {
            "schema_version": 1,
            "source": "helper",
            "observed_by": "helper",
            "classification": "configuration_blocked",
            "code": "ide_selection_required",
            "phase": "selection",
            "client_run_id": valid["client_run_id"],
            "helper_revision": valid["helper_revision"],
            "cleanup_status": "not_needed",
        }
        valid["internal_attempts"][0].update({
            "verdict": "UNKNOWN",
            "verdict_reason": "ide_selection_required",
            "bucket": "policy_required",
            "retry": False,
            "attribution_class": "configuration_blocked",
            "phase": "selection",
            "cleanup_status": "not_needed",
        })
        valid["internal_attempts"][0].pop("inspection_run_id", None)
        decisive = qualification_event("decisive", timestamp="2026-07-26T00:00:02.000Z")

        valid_result = self.summarize([valid, decisive])

        self.assertEqual(valid_result["gate_status"], "pass")
        self.assertEqual(valid_result["exclusion_counts"], {"configuration_blocked_before_start": 1})

        non_clean = json.loads(json.dumps(valid))
        non_clean["cleanup_status"] = "kept_warm"
        non_clean["inspection_attribution"]["cleanup_status"] = "kept_warm"
        non_clean["internal_attempts"][0]["cleanup_status"] = "kept_warm"
        non_clean_result = self.summarize([non_clean])
        self.assertEqual(non_clean_result["hard_failure_counts"]["non_clean_cleanup"], 1)

        malformed_attempts = json.loads(json.dumps(valid))
        malformed_attempts["internal_attempts"] = []
        malformed_result = self.summarize([malformed_attempts])
        self.assertEqual(malformed_result["hard_failure_counts"]["malformed_internal_attempts"], 1)

        attribution_mismatch = json.loads(json.dumps(valid))
        attribution_mismatch["inspection_attribution"]["code"] = "ide_config_missing"
        mismatch_result = self.summarize([attribution_mismatch])
        self.assertEqual(mismatch_result["hard_failure_counts"]["inspection_attribution_mismatch"], 1)

        for field, alias in (
            ("response_code", "ide-selection-required"),
            ("failure_phase", "Selection"),
        ):
            with self.subTest(noncanonical_field=field):
                noncanonical = json.loads(json.dumps(valid))
                noncanonical[field] = alias
                nested_field = "code" if field == "response_code" else "phase"
                noncanonical["inspection_attribution"][nested_field] = alias
                noncanonical_result = self.summarize([noncanonical])

                self.assertEqual(noncanonical_result["gate_status"], "fail")
                self.assertEqual(
                    noncanonical_result["hard_failure_counts"]["configuration_blocked_not_excludable"],
                    1,
                )

        invalid = qualification_event("invalid-config", verdict="UNKNOWN")
        invalid.update({
            "attribution_class": "configuration_blocked",
            "response_code": "timeout",
            "failure_phase": "wait",
            "inspection_started": True,
        })
        invalid["inspection_attribution"]["classification"] = "configuration_blocked"
        invalid_result = self.summarize([invalid])

        self.assertEqual(invalid_result["gate_status"], "fail")
        self.assertEqual(invalid_result["hard_failure_counts"]["configuration_blocked_after_start"], 1)

    def test_distinct_terminal_events_for_one_assessment_fail_closed(self):
        first = qualification_event("event-1", assessment_id="assessment-shared", verdict="UNKNOWN")
        first["internal_attempts"][0]["retry"] = True
        repeated = qualification_event(
            "event-2",
            assessment_id="assessment-shared",
            timestamp="2026-07-26T00:00:02.000Z",
            inspection_run_id=2,
        )

        result = self.summarize([first, first, repeated], sample_size=2)

        self.assertEqual(result["gate_status"], "fail")
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["assessment_groups"], 1)
        self.assertEqual(result["groups"][0]["event_count"], 2)
        self.assertFalse(result["groups"][0]["recovered_from_unknown"])
        self.assertEqual(result["hard_failure_counts"]["multiple_assessment_events"], 1)
        self.assertEqual(result["exclusion_counts"], {"duplicate_event": 1})

    def test_internal_unknown_to_decisive_recovery_stays_visible(self):
        event = qualification_event("recovery")
        event["internal_attempts"] = [
            {
                "attempt_index": 0,
                "terminal": False,
                "verdict": "UNKNOWN",
                "verdict_reason": "stale_results",
                "bucket": "stale_results",
                "retry": True,
                "inspection_run_id": 1,
            },
            {
                "attempt_index": 1,
                "terminal": True,
                "verdict": "GREEN",
                "verdict_reason": "no_matching_findings",
                "bucket": "clean",
                "retry": False,
                "inspection_run_id": 2,
                "cleanup_status": "closed",
            },
        ]

        result = self.summarize([event])

        self.assertEqual(result["gate_status"], "pass")
        self.assertTrue(result["groups"][0]["recovered_from_unknown"])
        self.assertEqual(result["summaries"]["recovered_from_unknown"], 1)
        self.assertEqual([attempt["verdict"] for attempt in result["groups"][0]["internal_attempts"]], ["UNKNOWN", "GREEN"])

    def test_conflicting_decisive_outcomes_and_hidden_terminal_failures_fail(self):
        green = qualification_event("green", assessment_id="assessment-conflict")
        red = qualification_event(
            "red",
            assessment_id="assessment-conflict",
            timestamp="2026-07-26T00:00:02.000Z",
            verdict="RED",
            inspection_run_id=2,
        )
        conflict = self.summarize([green, red], sample_size=2)

        self.assertEqual(conflict["gate_status"], "fail")
        self.assertTrue(conflict["groups"][0]["conflicting_decisive_outcomes"])
        self.assertEqual(conflict["hard_failure_counts"]["conflicting_outcomes"], 1)

        terminal = qualification_event("terminal", assessment_id="assessment-terminal", verdict="UNKNOWN")
        terminal["timestamp"] = "2026-07-26T00:00:02.000Z"
        terminal["timestamp_ms"] = jb_inspect.parse_iso_timestamp_ms(terminal["timestamp"])
        terminal["internal_attempts"][0]["verdict"] = "UNKNOWN"
        terminal["internal_attempts"][0]["retry"] = False
        terminal_result = self.summarize(
            [
                qualification_event("initial", assessment_id="assessment-terminal"),
                terminal,
            ],
            sample_size=2,
        )

        self.assertEqual(terminal_result["gate_status"], "fail")
        self.assertTrue(terminal_result["groups"][0]["hidden_terminal_failure"])
        self.assertEqual(terminal_result["hard_failure_counts"]["hidden_terminal_failure"], 1)

    def test_non_clean_cleanup_and_unattributed_unknown_count_as_gate_failures(self):
        cleanup = qualification_event("cleanup")
        cleanup["cleanup_status"] = "deferred"
        cleanup["internal_attempts"][0]["cleanup_status"] = "deferred"
        cleanup_result = self.summarize([cleanup])

        self.assertEqual(cleanup_result["gate_status"], "fail")
        self.assertEqual(cleanup_result["sample_count"], 1)
        self.assertEqual(cleanup_result["hard_failure_counts"]["non_clean_cleanup"], 1)

        kept_warm = qualification_event("kept-warm")
        kept_warm["cleanup_status"] = "kept_warm"
        kept_warm["inspection_attribution"]["cleanup_status"] = "kept_warm"
        kept_warm["internal_attempts"][0]["cleanup_status"] = "kept_warm"
        kept_warm_result = self.summarize([kept_warm])

        self.assertEqual(kept_warm_result["gate_status"], "fail")
        self.assertEqual(kept_warm_result["hard_failure_counts"]["non_clean_cleanup"], 1)

        unknown = qualification_event("unknown", verdict="UNKNOWN")
        unknown["attribution_class"] = "unattributed"
        unknown["unattributed_unknown"] = True
        unknown["inspection_attribution"]["classification"] = "unattributed"
        unknown_result = self.summarize([unknown])

        self.assertEqual(unknown_result["gate_status"], "fail")
        self.assertEqual(unknown_result["sample_count"], 1)
        self.assertEqual(unknown_result["hard_failure_counts"]["unattributed_unknown"], 1)

    def test_repeated_repository_and_project_concentration_is_reported(self):
        shared_repo = "sha256:" + "6" * 64
        shared_project = "sha256:" + "7" * 64
        events = [
            qualification_event("one", repo_path_hash=shared_repo, project_key_hash=shared_project),
            qualification_event(
                "two",
                timestamp="2026-07-26T00:00:02.000Z",
                repo_path_hash=shared_repo,
                project_key_hash=shared_project,
            ),
        ]

        result = self.summarize(events, sample_size=2)

        self.assertEqual(result["gate_status"], "pass")
        self.assertEqual(result["concentration"]["repeated_repositories"], {shared_repo: 2})
        self.assertEqual(result["concentration"]["repeated_projects"], {shared_project: 2})

    def test_gate_status_requires_sample_and_at_least_ninety_five_percent_decisive(self):
        incomplete = self.summarize([qualification_event("only")], sample_size=2)
        self.assertEqual(incomplete["gate_status"], "incomplete")
        self.assertEqual(incomplete["remaining_to_sample"], 1)

        passing_events = [
            qualification_event(
                f"pass-{index}",
                timestamp=f"2026-07-26T00:00:{index + 1:02d}.000Z",
            )
            for index in range(19)
        ]
        passing_events.append(
            qualification_event(
                "pass-unknown",
                timestamp="2026-07-26T00:00:20.000Z",
                verdict="UNKNOWN",
            )
        )
        passing = self.summarize(passing_events, sample_size=20)
        self.assertEqual(passing["gate_status"], "pass")
        self.assertEqual(passing["decisive_rate"], 0.95)

        failing_events = passing_events[:18] + [
            qualification_event("fail-unknown-1", timestamp="2026-07-26T00:00:19.000Z", verdict="UNKNOWN"),
            qualification_event("fail-unknown-2", timestamp="2026-07-26T00:00:20.000Z", verdict="UNKNOWN"),
        ]
        failing = self.summarize(failing_events, sample_size=20)
        self.assertEqual(failing["gate_status"], "fail")
        self.assertIn("decisive_rate_below_threshold", failing["gate_failures"])

    def test_first_sample_is_stable_when_later_events_fail(self):
        anchor = qualification_event(
            "anchor",
            assessment_id="anchor-assessment",
            event_kind="inspection_observation",
            command="get-status",
        )
        first = qualification_event("first", timestamp="2026-07-26T00:00:02.000Z")
        later_cleanup = qualification_event("later-cleanup", timestamp="2026-07-26T00:00:03.000Z")
        later_cleanup["cleanup_status"] = "deferred"
        later_cleanup["inspection_attribution"]["cleanup_status"] = "deferred"
        later_cleanup["internal_attempts"][0]["cleanup_status"] = "deferred"
        result = self.summarize(
            [anchor, first, later_cleanup, '{"broken"'],
            criteria=qualification_payload(after_event_id="anchor"),
        )

        self.assertEqual(result["gate_status"], "pass")
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["qualifying_sample"][0]["assessment_id"], "assessment-first")
        self.assertEqual(result["hard_failure_counts"], {})
        self.assertEqual(result["post_sample_hard_failure_counts"]["invalid_json"], 1)
        self.assertEqual(result["post_sample_hard_failure_counts"]["non_clean_cleanup"], 1)

    def test_frozen_sample_ignores_later_same_assessment_recovery(self):
        anchor = qualification_event(
            "anchor",
            assessment_id="anchor-assessment",
            event_kind="inspection_observation",
            command="get-status",
        )
        decisive = [
            qualification_event(
                f"decisive-{index}",
                timestamp=f"2026-07-26T00:00:{index + 2:02d}.000Z",
            )
            for index in range(19)
        ]
        first_unknown = qualification_event(
            "first-unknown",
            assessment_id="assessment-retry",
            timestamp="2026-07-26T00:00:21.000Z",
            verdict="UNKNOWN",
        )
        first_unknown["internal_attempts"][0]["retry"] = True
        recovery = qualification_event(
            "recovery",
            assessment_id="assessment-retry",
            timestamp="2026-07-26T00:00:23.000Z",
            inspection_run_id=2,
        )
        result = self.summarize(
            [anchor, *decisive, first_unknown, '{"broken"', recovery],
            criteria=qualification_payload(after_event_id="anchor"),
            sample_size=20,
        )

        self.assertEqual(result["gate_status"], "pass")
        self.assertEqual(result["sample_cutoff_line"], 21)
        self.assertEqual(result["decisive_rate"], 0.95)
        frozen = next(group for group in result["qualifying_sample"] if group["assessment_id"] == "assessment-retry")
        self.assertEqual(frozen["verdict"], "UNKNOWN")
        self.assertFalse(frozen["recovered_from_unknown"])
        full = next(group for group in result["groups"] if group["assessment_id"] == "assessment-retry")
        self.assertFalse(full["recovered_from_unknown"])
        self.assertEqual(result["post_sample_hard_failure_counts"]["invalid_json"], 1)
        self.assertEqual(result["post_sample_hard_failure_counts"]["multiple_assessment_events"], 1)

    def test_strict_command_exit_and_human_output_follow_gate_status(self):
        result = self.summarize([qualification_event("pass")])
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = jb_inspect.emit(
                result,
                json_only=False,
                exit_code=jb_inspect.summarize_outcomes_exit_code(result),
                command="summarize-outcomes",
                assess=False,
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("QUALIFICATION_GATE: status=pass sample=1/1", output.getvalue())
        self.assertFalse(any(line.startswith("VERDICT:") for line in output.getvalue().splitlines()))


class Issue458RegressionTest(unittest.TestCase):
    def test_unproven_conflicts_fail_closed_with_stable_attribution(self):
        cases = [
            (
                "legacy",
                "strict",
                {"status": "inspection_in_progress", "inspection_in_progress": True, "inspection_run_id": 42},
                "inspection_in_progress_scope_profile_proof_missing",
            ),
            (
                "legacy-error-only",
                "strict",
                {"error": "inspection_in_progress", "inspection_run_id": 42},
                "inspection_in_progress_scope_profile_proof_missing",
            ),
            (
                "legacy-flag-only",
                "strict",
                {"inspection_in_progress": True, "inspection_run_id": 42},
                "inspection_in_progress_scope_profile_proof_missing",
            ),
            (
                "missing-default-profile-proof",
                "",
                {"status": "inspection_in_progress", "requested_scope": "changed_files", "inspection_run_id": 42},
                "inspection_in_progress_scope_profile_proof_ambiguous",
            ),
            (
                "scope-mismatch",
                "strict",
                {
                    "status": "inspection_in_progress",
                    "requested_scope": "whole_project",
                    "requested_profile": "strict",
                    "inspection_run_id": 42,
                },
                "inspection_in_progress_scope_mismatch",
            ),
            (
                "missing-changed-files-selector-proof",
                "strict",
                {
                    "status": "inspection_in_progress",
                    "requested_scope": "changed_files",
                    "requested_profile": "strict",
                    "inspection_run_id": 42,
                },
                "inspection_in_progress_include_unversioned_proof_missing",
            ),
            (
                "profile-mismatch",
                "strict",
                {
                    "status": "inspection_in_progress",
                    "requested_scope": "changed_files",
                    "requested_profile": "default",
                    "inspection_run_id": 42,
                },
                "inspection_in_progress_profile_mismatch",
            ),
            (
                "include-unversioned-mismatch",
                "strict",
                {
                    "status": "inspection_in_progress",
                    "requested_scope": "changed_files",
                    "requested_profile": "strict",
                    "include_unversioned": False,
                    "inspection_run_id": 42,
                },
                "inspection_in_progress_include_unversioned_mismatch",
            ),
            (
                "changed-files-mode-mismatch",
                "strict",
                {
                    "status": "inspection_in_progress",
                    "requested_scope": "changed_files",
                    "requested_profile": "strict",
                    "changed_files_mode": "staged",
                    "inspection_run_id": 42,
                },
                "inspection_in_progress_changed_files_mode_mismatch",
            ),
            (
                "ambiguous-layouts",
                "strict",
                {
                    "status": "inspection_in_progress",
                    "requested_scope": "changed_files",
                    "requested_profile": "strict",
                    "scope": "whole_project",
                    "profile": "strict",
                    "inspection_run_id": 42,
                },
                "inspection_in_progress_scope_profile_proof_ambiguous",
            ),
            (
                "ambiguous-run-id",
                "strict",
                {
                    "status": "inspection_in_progress",
                    "requested_scope": "changed_files",
                    "requested_profile": "strict",
                    "requested_include_unversioned": True,
                    "requested_changed_files_mode": "all",
                    "inspection_run_id": 42,
                    "run_id": 43,
                },
                "inspection_in_progress_run_id_ambiguous",
            ),
        ]
        route = {"port": 63342, "project_key": "path:/tmp/repo", "session_id": "session"}

        for name, profile, trigger_response, expected_failure in cases:
            with self.subTest(name=name), patch.object(
                jb_inspect,
                "call_endpoint",
                return_value=trigger_response,
            ) as call:
                result = jb_inspect.run_inspection_on_route(
                    helper_args(scope="changed_files", profile=profile),
                    {"scope": "changed_files"},
                    route,
                )

            self.assertEqual(call.call_count, 1)
            self.assertEqual(result["status"], "inspection_in_progress_unproven")
            self.assertEqual(result["verdict"], "UNKNOWN")
            self.assertEqual(result["verdict_reason"], "inspection_proof_failed")
            self.assertIn(expected_failure, result["proof_failures"])
            self.assertTrue(result["transport_state_unknown"])
            self.assertEqual(result["inspection_attribution"]["code"], "inspection_proof_failed")
            self.assertEqual(result["inspection_attribution"]["phase"], "trigger")
            self.assertEqual(result["inspection_attribution"]["http_status"], 409)
            self.assertTrue(jb_inspect.should_defer_lifecycle_cleanup(result, {"opened_by_helper": True}))

    def test_matching_conflict_proof_adopts_only_the_proven_run(self):
        route = {"port": 63342, "project_key": "path:/tmp/repo", "session_id": "session"}
        calls = []

        def fake_call(active_route, endpoint, params, timeout=None):
            calls.append((endpoint, params))
            if endpoint == "trigger":
                return {
                    "status": "inspection_in_progress",
                    "inspection_in_progress": True,
                    "requested_scope": "changed_files",
                    "requested_profile": "strict",
                    "requested_include_unversioned": True,
                    "requested_changed_files_mode": "all",
                    "inspection_run_id": 42,
                    "route": route,
                }
            if endpoint == "wait":
                return {"status": "results_available", "inspection_run_id": 42, "snapshot_run_id": 42}
            if endpoint == "problems":
                return {
                    "status": "results_available",
                    "inspection_run_id": 42,
                    "snapshot_run_id": 42,
                    "total_problems": 0,
                    "problems": [],
                }
            self.fail(f"unexpected endpoint: {endpoint}")

        with patch.object(jb_inspect, "call_endpoint", side_effect=fake_call):
            result = jb_inspect.run_inspection_on_route(
                helper_args(scope="changed_files", profile="strict", timeout_ms=1000, poll_ms=1),
                {"scope": "changed_files"},
                route,
            )

        self.assertEqual(result["verdict"], "GREEN")
        self.assertEqual([endpoint for endpoint, _ in calls], ["trigger", "wait", "problems"])
        self.assertEqual(calls[1][1]["inspection_run_id"], 42)
        self.assertEqual(calls[2][1]["inspection_run_id"], 42)

    def test_matching_conflict_timeout_never_cancels_foreign_run(self):
        route = {"port": 63342, "project_key": "path:/tmp/repo", "session_id": "session"}
        calls = []

        def fake_call(active_route, endpoint, params, timeout=None):
            calls.append(endpoint)
            if endpoint == "trigger":
                return {
                    "status": "inspection_in_progress",
                    "inspection_in_progress": True,
                    "requested_scope": "changed_files",
                    "requested_profile": "strict",
                    "requested_include_unversioned": True,
                    "requested_changed_files_mode": "all",
                    "inspection_run_id": 42,
                    "route": route,
                }
            if endpoint == "wait":
                return {
                    "status": "timed_out",
                    "timed_out": True,
                    "inspection_in_progress": True,
                    "inspection_run_id": 42,
                }
            if endpoint == "problems":
                return {
                    "status": "running",
                    "inspection_in_progress": True,
                    "inspection_run_id": 42,
                    "problems": [],
                }
            self.fail(f"unexpected endpoint: {endpoint}")

        with patch.object(jb_inspect, "call_endpoint", side_effect=fake_call):
            result = jb_inspect.run_inspection_on_route(
                helper_args(scope="changed_files", profile="strict", timeout_ms=1000, poll_ms=1),
                {"scope": "changed_files"},
                route,
            )

        self.assertEqual(calls, ["trigger", "wait", "problems"])
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertEqual(result["cancellation"]["status"], "not_requested")
        self.assertEqual(result["cancellation"]["reason"], "foreign_run_not_owned")
        self.assertFalse(result["cancellation"]["requested"])

    def test_matching_conflict_transport_timeout_never_cancels_foreign_run(self):
        route = {"port": 63342, "project_key": "path:/tmp/repo", "session_id": "session"}
        calls = []

        def fake_call(active_route, endpoint, params, timeout=None):
            calls.append(endpoint)
            if endpoint == "trigger":
                return {
                    "status": "inspection_in_progress",
                    "inspection_in_progress": True,
                    "requested_scope": "changed_files",
                    "requested_profile": "strict",
                    "requested_include_unversioned": True,
                    "requested_changed_files_mode": "all",
                    "inspection_run_id": 42,
                    "route": route,
                }
            if endpoint == "wait":
                raise jb_inspect.InspectError(
                    "Inspection API timed out on port 63342: timed out",
                    3,
                    {"error_reason": "inspection_api_timeout", "endpoint": "wait", "port": 63342},
                )
            if endpoint == "status":
                return {
                    "status": "running",
                    "inspection_in_progress": True,
                    "inspection_run_id": 42,
                }
            self.fail(f"unexpected endpoint: {endpoint}")

        with patch.object(jb_inspect, "call_endpoint", side_effect=fake_call):
            result = jb_inspect.run_inspection_on_route(
                helper_args(scope="changed_files", profile="strict", timeout_ms=1000, poll_ms=1),
                {"scope": "changed_files"},
                route,
            )

        self.assertEqual(calls, ["trigger", "wait", "status"])
        self.assertEqual(result["verdict"], "UNKNOWN")
        self.assertEqual(result["cancellation"]["status"], "not_requested")
        self.assertEqual(result["cancellation"]["reason"], "foreign_run_not_owned")
        self.assertFalse(result["cancellation"]["requested"])

    def test_trigger_and_problems_preserve_scope_parameters(self):
        route = {"port": 63342, "project_key": "path:/tmp/repo", "session_id": "session"}
        for include_unversioned, changed_files_mode in ((False, "staged"), (True, "unstaged"), (True, "all")):
            calls = []

            def fake_call(active_route, endpoint, params, timeout=None):
                calls.append((endpoint, params))
                if endpoint == "trigger":
                    return {"status": "triggered", "run_id": 7, "route": route}
                if endpoint == "wait":
                    return {"status": "results_available", "inspection_run_id": 7, "snapshot_run_id": 7}
                if endpoint == "problems":
                    return {
                        "status": "results_available",
                        "inspection_run_id": 7,
                        "snapshot_run_id": 7,
                        "total_problems": 0,
                        "problems": [],
                    }
                self.fail(f"unexpected endpoint: {endpoint}")

            args = helper_args(
                scope="changed_files",
                include_unversioned=include_unversioned,
                changed_files_mode=changed_files_mode,
                profile="strict",
                max_files=25,
                timeout_ms=1000,
                poll_ms=1,
            )
            with self.subTest(include_unversioned=include_unversioned, changed_files_mode=changed_files_mode), patch.object(
                jb_inspect,
                "call_endpoint",
                side_effect=fake_call,
            ):
                jb_inspect.run_inspection_on_route(args, {"scope": "changed_files"}, route)

            trigger_request = calls[0][1]
            problems_request = calls[2][1]
            for key in ("scope", "include_unversioned", "changed_files_mode", "profile", "max_files"):
                self.assertEqual(problems_request[key], trigger_request[key])

    def test_truncated_semantic_diagnostics_override_green_with_stable_reason(self):
        semantic_file = {
            "path": "/tmp/a.py",
            "valid": True,
            "directory": False,
            "file_type": "Python",
            "psi_language": "Python",
            "psi_class": "com.jetbrains.python.psi.impl.PyFileImpl",
            "in_content": True,
        }
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "GREEN",
            "capture_diagnostic": {
                "scope_file_resolved_count": 3,
                "scope_file_diagnostics": [semantic_file, {**semantic_file, "path": "/tmp/b.py"}],
            },
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], jb_inspect.SEMANTIC_COVERAGE_TRUNCATED_REASON)
        self.assertEqual(payload["semantic_coverage"]["diagnostic_file_count"], 2)
        self.assertEqual(payload["semantic_coverage"]["unproven_file_count"], 1)
        self.assertEqual(payload["inspection_attribution"]["code"], jb_inspect.SEMANTIC_COVERAGE_TRUNCATED_REASON)
        self.assertEqual(payload["attribution_class"], "legitimate_fail_closed")
        self.assertEqual(payload["bucket"], "tool_bug")

    def test_aggregate_semantic_proof_allows_bounded_diagnostic_details(self):
        semantic_file = {
            "path": "/tmp/a.py",
            "valid": True,
            "directory": False,
            "file_type": "Python",
            "psi_language": "Python",
            "psi_class": "com.jetbrains.python.psi.impl.PyFileImpl",
            "in_content": True,
        }
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "no_matching_findings",
            "capture_diagnostic": {
                "scope_file_resolved_count": 30,
                "scope_file_diagnostics": [semantic_file, {**semantic_file, "path": "/tmp/b.py"}],
                "scope_file_diagnostics_truncated": True,
                "scope_file_diagnostics_complete": False,
                "scope_file_semantic_evidence_complete": True,
                "scope_file_semantic_coverage": {
                    "schema_version": 1,
                    "evaluated_file_count": 30,
                    "unproven_file_count": 0,
                    "missing_file_count": 0,
                    "reason_counts": {},
                    "missing_files": [],
                    "metadata_file_count": 0,
                    "metadata_files": [],
                },
            },
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "GREEN")
        self.assertEqual(payload["verdict_reason"], "no_matching_findings")
        self.assertNotIn("semantic_coverage", payload)

    def test_aggregate_semantic_proof_preserves_excluded_lockfile_role_beyond_detail_limit(self):
        semantic_file = {
            "path": "/tmp/a.py",
            "valid": True,
            "directory": False,
            "file_type": "Python",
            "psi_language": "Python",
            "psi_class": "com.jetbrains.python.psi.impl.PyFileImpl",
            "in_content": True,
            "is_excluded": False,
        }
        lockfile = {
            "path": "/tmp/uv.lock",
            "valid": True,
            "directory": False,
            "file_type": "PLAIN_TEXT",
            "psi_language": "TEXT",
            "psi_class": "com.intellij.psi.impl.source.PsiPlainTextFile",
            "in_content": False,
            "in_source": False,
            "is_excluded": True,
            "coverage_role": "excluded_dependency_lockfile",
            "classification": "excluded_dependency_lockfile",
            "coverage_required": False,
        }
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "no_matching_findings",
            "capture_diagnostic": {
                "scope_file_resolved_count": 30,
                "scope_file_diagnostics": [semantic_file],
                "scope_file_diagnostics_truncated": True,
                "scope_file_diagnostics_complete": False,
                "scope_file_semantic_evidence_complete": True,
                "scope_file_semantic_coverage": {
                    "schema_version": 1,
                    "evaluated_file_count": 30,
                    "unproven_file_count": 0,
                    "missing_file_count": 0,
                    "reason_counts": {},
                    "missing_files": [],
                    "metadata_file_count": 1,
                    "metadata_files": [lockfile],
                },
            },
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "GREEN")
        self.assertEqual(payload["semantic_coverage"]["status"], "satisfied")
        self.assertEqual(payload["semantic_coverage"]["metadata_file_count"], 1)
        self.assertEqual(
            payload["semantic_coverage"]["metadata_files"][0]["classification"],
            "excluded_dependency_lockfile",
        )

    def test_aggregate_semantic_proof_preserves_missing_file_beyond_detail_limit(self):
        semantic_file = {
            "path": "/tmp/a.py",
            "valid": True,
            "directory": False,
            "file_type": "Python",
            "psi_language": "Python",
            "psi_class": "com.jetbrains.python.psi.impl.PyFileImpl",
            "in_content": True,
        }
        text_only_file = {
            "path": "/tmp/View.swift",
            "valid": True,
            "directory": False,
            "file_type": "TextMate",
            "psi_language": "textmate",
            "psi_class": "org.jetbrains.plugins.textmate.psi.TextMateFile",
            "in_content": True,
            "reasons": ["non_semantic_fallback"],
        }
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON,
            "proof_failures": [jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON],
            "capture_diagnostic": {
                "scope_file_resolved_count": 30,
                "scope_file_diagnostics": [semantic_file, {**semantic_file, "path": "/tmp/b.py"}],
                "scope_file_diagnostics_truncated": True,
                "scope_file_diagnostics_complete": False,
                "scope_file_semantic_evidence_complete": True,
                "scope_file_semantic_coverage": {
                    "schema_version": 1,
                    "evaluated_file_count": 30,
                    "unproven_file_count": 0,
                    "missing_file_count": 1,
                    "reason_counts": {"non_semantic_fallback": 1},
                    "missing_files": [text_only_file],
                    "metadata_file_count": 0,
                    "metadata_files": [],
                },
            },
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON)
        self.assertEqual(payload["semantic_coverage"]["evaluated_file_count"], 30)
        self.assertEqual(payload["semantic_coverage"]["diagnostic_file_count"], 2)
        self.assertEqual(payload["semantic_coverage"]["unproven_file_count"], 0)
        self.assertEqual(payload["semantic_coverage"]["missing_file_count"], 1)
        self.assertEqual(payload["semantic_coverage"]["files"][0]["path"], "/tmp/View.swift")

    def test_aggregate_text_only_semantic_proof_can_be_explicitly_allowed(self):
        text_only_file = {
            "path": "/tmp/View.swift",
            "valid": True,
            "directory": False,
            "file_type": "TextMate",
            "psi_language": "textmate",
            "psi_class": "org.jetbrains.plugins.textmate.psi.TextMateFile",
            "in_content": True,
            "reasons": ["non_semantic_fallback"],
        }
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "problems": [],
            "inspection_verdict": "UNKNOWN",
            "inspection_verdict_reason": jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON,
            "proof_failures": [jb_inspect.SEMANTIC_COVERAGE_MISSING_REASON],
            "allow_text_only_coverage": True,
            "capture_diagnostic": {
                "scope_file_resolved_count": 30,
                "scope_file_diagnostics": [],
                "scope_file_diagnostics_truncated": True,
                "scope_file_semantic_evidence_complete": True,
                "scope_file_semantic_coverage": {
                    "schema_version": 1,
                    "evaluated_file_count": 30,
                    "unproven_file_count": 0,
                    "missing_file_count": 1,
                    "reason_counts": {"non_semantic_fallback": 1},
                    "missing_files": [text_only_file],
                    "metadata_file_count": 0,
                    "metadata_files": [],
                },
            },
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "GREEN")
        self.assertEqual(payload["verdict_reason"], "text_only_coverage_allowed")
        self.assertEqual(payload["semantic_coverage"]["status"], "text_only_allowed")

    def test_malformed_aggregate_semantic_proof_still_fails_closed(self):
        semantic_file = {
            "path": "/tmp/a.py",
            "valid": True,
            "directory": False,
            "file_type": "Python",
            "psi_language": "Python",
            "psi_class": "com.jetbrains.python.psi.impl.PyFileImpl",
            "in_content": True,
        }
        payload = {
            "status": "results_available",
            "clean": True,
            "total_problems": 0,
            "inspection_verdict": "GREEN",
            "capture_diagnostic": {
                "scope_file_resolved_count": 3,
                "scope_file_diagnostics": [semantic_file, {**semantic_file, "path": "/tmp/b.py"}],
                "scope_file_semantic_evidence_complete": True,
                "scope_file_semantic_coverage": {
                    "schema_version": 1,
                    "evaluated_file_count": 3,
                    "unproven_file_count": 0,
                    "missing_file_count": 0,
                    "reason_counts": {"non_semantic_fallback": 1},
                    "metadata_file_count": 0,
                },
            },
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], jb_inspect.SEMANTIC_COVERAGE_TRUNCATED_REASON)

    def test_inspection_inputs_changed_is_retryable_and_attributed(self):
        payload = {
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "inspection_inputs_changed",
            "total_problems": 0,
            "problems": [],
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["verdict_reason"], "inspection_inputs_changed")
        self.assertEqual(payload["bucket"], "capture_not_ready")
        self.assertTrue(payload["retry_policy"]["retry"])
        self.assertEqual(payload["attribution_class"], "legitimate_fail_closed")
        self.assertTrue(jb_inspect.should_retry_unknown_result(payload))

    def test_project_analysis_not_ready_is_retryable_and_attributed(self):
        payload = {
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "project_analysis_not_ready",
            "inspection_attribution": {
                "classification": "unattributed",
                "code": "project_analysis_not_ready",
            },
            "total_problems": 0,
            "problems": [],
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["bucket"], "capture_not_ready")
        self.assertTrue(payload["retry_policy"]["retry"])
        self.assertEqual(payload["retry_policy"]["max_attempts"], 3)
        self.assertEqual(payload["attribution_class"], "legitimate_fail_closed")
        self.assertNotIn("unattributed_unknown", payload)

    def test_language_sdk_missing_is_terminal_environment_blocker(self):
        payload = {
            "status": "capture_incomplete",
            "capture_incomplete": True,
            "capture_incomplete_reason": "language_sdk_missing",
            "total_problems": 0,
            "problems": [],
        }

        jb_inspect.apply_verdict(payload)

        self.assertEqual(payload["verdict"], "UNKNOWN")
        self.assertEqual(payload["bucket"], "environment_blocked")
        self.assertFalse(payload["retry_policy"]["retry"])
        self.assertEqual(payload["attribution_class"], "configuration_blocked")
        self.assertIn("language SDK", payload["verdict_next_action"])

    def test_truncation_handles_missing_malformed_and_text_only_diagnostics(self):
        cases = [
            [],
            [{"path": "/tmp/a.py", "valid": True, "file_type": "Python", "psi_language": "Python", "in_content": True}, "invalid"],
            [{"path": "/tmp/a.swift", "valid": True, "file_type": "TextMate", "psi_language": "TextMate", "in_content": True}],
        ]
        for diagnostics in cases:
            with self.subTest(diagnostics=diagnostics):
                coverage = jb_inspect.semantic_coverage_for_payload(
                    {
                        "allow_text_only_coverage": True,
                        "capture_diagnostic": {
                            "scope_file_resolved_count": len(diagnostics) + 1,
                            "scope_file_diagnostics": diagnostics,
                        },
                    }
                )

                self.assertIsNotNone(coverage)
                self.assertEqual(coverage["status"], "missing")
                self.assertEqual(coverage["reason"], jb_inspect.SEMANTIC_COVERAGE_TRUNCATED_REASON)

    def test_durable_redaction_hashes_embedded_paths(self):
        posix_path = "/Users/cbusillo/project/src/main.py"
        windows_path = r"C:\Users\cbusillo\project\main.py"
        quoted_path = "/Users/Chris Busillo/My Project/test.py"
        unquoted_path = "/Users/Chris Busillo/My Project/unquoted.py"
        unquoted_windows_path = r"C:\Users\Chris Busillo\My Project\unquoted.py"
        no_extension_path = "/Users/Chris Busillo/My Project"
        no_extension_windows_path = r"C:\Users\Chris Busillo\My Project"
        no_extension_file_uri = "file:///Users/Chris Busillo/My Project"
        file_uri = "file:///private/tmp/inspection.log"
        redacted = jb_inspect.redact_durable_log(
            {
                "capture_diagnostic": {
                    "exception_message": f"Read {posix_path}:12:4, then {windows_path}.",
                    "engine_errors": [
                        f'Failed at "{quoted_path}".',
                        f"Trace: {file_uri}!",
                        f"Read {unquoted_path}:9 before continuing.",
                        f"Read {unquoted_windows_path}:11 before continuing.",
                        no_extension_path,
                        no_extension_windows_path,
                        no_extension_file_uri,
                        f"Read {no_extension_path} before continuing.",
                    ],
                }
            }
        )["capture_diagnostic"]

        self.assertEqual(
            redacted["exception_message"],
            f"Read <path:{jb_inspect.stable_value_hash(posix_path)}>:12:4, then "
            f"<path:{jb_inspect.stable_value_hash(windows_path)}>.",
        )
        self.assertEqual(
            redacted["engine_errors"][0],
            f'Failed at "<path:{jb_inspect.stable_value_hash(quoted_path)}>".',
        )
        self.assertEqual(
            redacted["engine_errors"][1],
            f"Trace: <path:{jb_inspect.stable_value_hash(file_uri)}>!",
        )
        self.assertEqual(
            redacted["engine_errors"][2],
            f"Read <path:{jb_inspect.stable_value_hash(unquoted_path)}>:9 before continuing.",
        )
        self.assertEqual(
            redacted["engine_errors"][3],
            f"Read <path:{jb_inspect.stable_value_hash(unquoted_windows_path)}>:11 before continuing.",
        )
        self.assertEqual(
            redacted["engine_errors"][4],
            f"<path:{jb_inspect.stable_value_hash(no_extension_path)}>",
        )
        self.assertEqual(
            redacted["engine_errors"][5],
            f"<path:{jb_inspect.stable_value_hash(no_extension_windows_path)}>",
        )
        self.assertEqual(
            redacted["engine_errors"][6],
            f"<path:{jb_inspect.stable_value_hash(no_extension_file_uri)}>",
        )
        self.assertTrue(redacted["engine_errors"][7].startswith("Read <path:sha256:"))
        self.assertNotIn("Chris Busillo", redacted["engine_errors"][7])
        self.assertNotIn("My Project", redacted["engine_errors"][7])

    def test_durable_redaction_preserves_urls_routes_and_identifiers(self):
        values = [
            "See " + "ht" + "tps://example.com/docs/ref and " + "ht" + "tp://127.0.0.1:63342/api/inspection/status.",
            "Endpoints /api/v1/problems and /v2/projects/42 remain stable.",
            "Routes /users/42 and /projects/abc/status remain stable.",
            "Identifiers org/example/Foo, owner/repo/issues/458, scope/profile, and/or remain unchanged.",
            "Package-like /com/example/Foo.java identifiers remain unchanged.",
            "MIME application/x-www-form-urlencoded is not a local path.",
        ]

        self.assertEqual(jb_inspect.redact_durable_log(values), values)


if __name__ == "__main__":
    unittest.main()
