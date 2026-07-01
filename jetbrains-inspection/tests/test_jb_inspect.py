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
import unittest
import urllib.parse
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "jb-inspect.py"
SPEC = importlib.util.spec_from_file_location("jb_inspect", SCRIPT_PATH)
jb_inspect = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["jb_inspect"] = jb_inspect
SPEC.loader.exec_module(jb_inspect)


def write_json(path: Path, payload: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle)


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


class ParserCommandAliasTest(unittest.TestCase):
    def test_preferred_commands_parse_and_canonicalize(self):
        parser = jb_inspect.build_parser()
        commands = {
            "list-projects": "list",
            "resolve-route": "route",
            "prepare-worktree": "prepare",
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

        for command in ("list-projects", "resolve-route", "prepare-worktree", "inspect", "inspect-closeout", "get-status", "get-problems"):
            self.assertIn(command, help_text)
        self.assertNotIn("Legacy alias", help_text)
        choices = parser._subparsers._group_actions[0].choices
        for command in ("list", "route", "prepare", "run", "closeout", "status", "problems", "trigger", "wait", "claim", "cleanup-leases"):
            self.assertNotIn(command, choices)

    def test_inspect_alias_has_lifecycle_and_scope_options(self):
        parser = jb_inspect.build_parser()

        args = parser.parse_args(["inspect", "--repo", "/tmp/repo", "--scope", "changed_files", "--no-open"])

        self.assertEqual(jb_inspect.canonical_command(args.command), "run")
        self.assertEqual(args.repo, "/tmp/repo")
        self.assertEqual(args.scope, "changed_files")
        self.assertFalse(args.open)


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

            args = Namespace(repo=str(root), ide=None, ide_app=None, scope=None)
            context = jb_inspect.build_context(args)

            self.assertEqual(context["ide"], "IntelliJ IDEA")
            self.assertEqual(context["scope"], "directory")
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
        self.assertEqual(selection.version[:2], (2026, 2))
        self.assertEqual(selection.app_path, eap_app)
        self.assertEqual(selection.config_dir, eap_config)

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
            jb_inspect.claim_lifecycle = lambda args, context, route, lease: ({"status": "claimed"}, "private-close-proof")
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

        run.assert_called_once_with(["open", "-g", "-a", str(app_path), "/tmp/worktree"], check=False, capture_output=True, text=True)

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

        run.assert_called_once_with(["open", "-g", "-j", "-a", str(app_path)], check=False, capture_output=True, text=True)

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

                def fake_private_http(port, endpoint, params):
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

                def fake_private_http(port, endpoint, params):
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
        jb_inspect.discover_identities = lambda port: [{"port": 63341, "ide_name": "IntelliJ IDEA", "session_id": "s1"}]

        def fake_http_get(port, endpoint, params, timeout=jb_inspect.DEFAULT_TIMEOUT_SECONDS):
            calls.append((port, endpoint, params))
            return jb_inspect.HttpResult(200, {"status": "opened"}, "url")

        jb_inspect.http_get = fake_http_get
        try:
            result = jb_inspect.open_via_running_ide(
                Namespace(port=None),
                {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/worktree", "project_path": "/tmp/worktree"},
            )
        finally:
            jb_inspect.discover_identities = original_discover
            jb_inspect.http_get = original_http_get

        self.assertTrue(result)
        self.assertEqual(calls[0][1], "lifecycle/open")
        self.assertEqual(calls[0][2]["worktree_path"], "/tmp/worktree")
        self.assertEqual(calls[0][2]["session_id"], "s1")

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
        jb_inspect.discover_identities = lambda port: [{"port": 63341, "ide_name": "IntelliJ IDEA", "session_id": "s1"}]

        def fake_http_get(port, endpoint, params, timeout=jb_inspect.DEFAULT_TIMEOUT_SECONDS):
            calls.append((port, endpoint, params))
            return jb_inspect.HttpResult(200, {"status": "opened"}, "url")

        jb_inspect.http_get = fake_http_get
        try:
            result = jb_inspect.open_via_running_ide(
                Namespace(port=None),
                {
                    "ide": "IntelliJ IDEA",
                    "worktree_root": "/tmp/harness-parent",
                    "project_path": "/tmp/harness-parent/workspace/project",
                    "lifecycle_target_path": "/tmp/harness-parent/workspace/project",
                },
            )
        finally:
            jb_inspect.discover_identities = original_discover
            jb_inspect.http_get = original_http_get

        self.assertTrue(result)
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
        jb_inspect.open_via_running_ide = lambda args, context: calls.append("running") or True
        jb_inspect.bootstrap_ide_app = lambda *args, **kwargs: calls.append("bootstrap")
        jb_inspect.wait_for_matching_ide_identity = lambda *args, **kwargs: calls.append("wait")
        try:
            result = jb_inspect.open_project_for_lifecycle(Namespace(port=None, background_open=True), {"ide": "IntelliJ IDEA"})
        finally:
            jb_inspect.open_via_running_ide = original_running
            jb_inspect.bootstrap_ide_app = original_bootstrap
            jb_inspect.wait_for_matching_ide_identity = original_wait

        self.assertEqual(result, "running_ide")
        self.assertEqual(calls, ["running"])

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

        self.assertFalse(result)

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
        original_now = jb_inspect.now_ms
        original_sleep = jb_inspect.time.sleep

        def fake_running(args, context):
            calls.append("running")
            return calls.count("running") == 2

        jb_inspect.open_via_running_ide = fake_running
        jb_inspect.bootstrap_ide_app = lambda context, background=True: calls.append(("bootstrap", background))
        jb_inspect.wait_for_matching_ide_identity = lambda args, context, timeout_ms: calls.append(("wait", timeout_ms)) or {"port": 63342}
        jb_inspect.now_ms = lambda: 0
        jb_inspect.time.sleep = lambda seconds: calls.append(("sleep", seconds))
        try:
            result = jb_inspect.open_project_for_lifecycle(Namespace(port=None, background_open=True, prepare_timeout_ms=1234), {"ide": "IntelliJ IDEA"})
        finally:
            jb_inspect.open_via_running_ide = original_running
            jb_inspect.bootstrap_ide_app = original_bootstrap
            jb_inspect.wait_for_matching_ide_identity = original_wait
            jb_inspect.now_ms = original_now
            jb_inspect.time.sleep = original_sleep

        self.assertEqual(result, "bootstrapped_ide")
        self.assertEqual(calls, ["running", ("bootstrap", True), ("wait", 1234), "running"])

    def test_open_project_for_lifecycle_retries_open_after_cold_bootstrap(self):
        calls = []
        original_running = jb_inspect.open_via_running_ide
        original_bootstrap = jb_inspect.bootstrap_ide_app
        original_wait = jb_inspect.wait_for_matching_ide_identity
        original_now = jb_inspect.now_ms
        original_sleep = jb_inspect.time.sleep
        ticks = iter([0, 0, 100, 200, 300])

        def fake_running(args, context):
            calls.append("running")
            return calls.count("running") == 4

        jb_inspect.open_via_running_ide = fake_running
        jb_inspect.bootstrap_ide_app = lambda context, background=True: calls.append(("bootstrap", background))
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

        self.assertEqual(result, "bootstrapped_ide")
        self.assertEqual(
            calls,
            [
                "running",
                ("bootstrap", True),
                ("wait", 1000),
                "running",
                ("sleep", 1),
                "running",
                ("sleep", 1),
                "running",
            ],
        )

    def test_open_project_for_lifecycle_errors_when_bootstrapped_ide_rejects_open(self):
        original_running = jb_inspect.open_via_running_ide
        original_bootstrap = jb_inspect.bootstrap_ide_app
        original_wait = jb_inspect.wait_for_matching_ide_identity
        original_now = jb_inspect.now_ms
        original_sleep = jb_inspect.time.sleep
        ticks = iter([0, 0, 500, 1500])
        jb_inspect.open_via_running_ide = lambda args, context: False
        jb_inspect.bootstrap_ide_app = lambda context, background=True: None
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
            "inspection_verdict": "GREEN",
            "inspection_verdict_reason": "no_matching_findings",
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
            self.assertEqual(record["verdict_reason"], "non_empty_unmapped_tree")
            self.assertEqual(record["rollout_file"], str(rollout_path))
            self.assertEqual(record["repo_path"], "/repo")
            self.assertEqual(record["ide"], "IntelliJ IDEA")
            self.assertEqual(record["capture_diagnostic"]["exit_reason"], "non_empty_unmapped_tree")
            self.assertNotIn("secret-token", log_path.read_text(encoding="utf-8"))

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


if __name__ == "__main__":
    unittest.main()
