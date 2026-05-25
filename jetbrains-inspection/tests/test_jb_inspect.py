#!/usr/bin/env python3
import importlib.util
import io
import json
import os
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

            args = Namespace(repo=str(root), ide=None, scope=None)
            context = jb_inspect.build_context(args)

            self.assertEqual(context["ide"], "IntelliJ IDEA")
            self.assertEqual(context["scope"], "directory")
            self.assertEqual(context["worktree_strategy"], "prefer-current")
            self.assertEqual(context["project_path"], str((root / "packages" / "app").resolve()))
            self.assertTrue(context["main_worktree"].endswith("Developer/example-main"))


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

    def test_route_sort_key_prefers_deeper_containing_project_for_equal_scores(self):
        context = {"worktree_root": "/tmp/repo/packages/app/src/main"}
        parent = {"score": 930, "base_path": "/tmp/repo"}
        child = {"score": 930, "base_path": "/tmp/repo/packages/app"}

        routes = sorted([parent, child], key=lambda route: jb_inspect.route_sort_key(route, context), reverse=True)

        self.assertEqual(routes[0], child)


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

    def test_open_in_ide_reports_failed_macos_open(self):
        completed = subprocess.CompletedProcess(["open"], 1, "", "Unable to find application")
        with patch.object(jb_inspect.sys, "platform", "darwin"), patch.object(jb_inspect.subprocess, "run", return_value=completed):
            with self.assertRaises(jb_inspect.InspectError) as raised:
                jb_inspect.open_in_ide({"ide": "Missing IDE", "worktree_root": "/tmp/worktree"}, background=True)

        self.assertIn("Failed to ask macOS", str(raised.exception))
        self.assertEqual(raised.exception.payload["returncode"], 1)
        self.assertIn("Unable to find application", raised.exception.payload["stderr"])

    def test_auto_open_timeout_payload_names_trust_and_modal_causes(self):
        args = Namespace(background_open=True)
        payload = jb_inspect.auto_open_timeout_payload(
            args,
            {"ide": "PyCharm", "worktree_root": "/tmp/worktree", "trusted_auto_open_roots": ["/tmp"]},
            300_000,
        )

        self.assertTrue(payload["background_open"])
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
                with patch.object(jb_inspect, "trusted_auto_open_roots", return_value=[str(worktree.parent)]):
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

    def test_open_via_running_ide_ignores_other_ide_products(self):
        original_discover = jb_inspect.discover_identities
        original_http_get = jb_inspect.http_get
        jb_inspect.discover_identities = lambda port: [{"port": 63341, "ide_name": "WebStorm", "session_id": "s1"}]
        jb_inspect.http_get = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not call"))
        try:
            result = jb_inspect.open_via_running_ide(Namespace(port=None), {"ide": "IntelliJ IDEA", "worktree_root": "/tmp/worktree"})
        finally:
            jb_inspect.discover_identities = original_discover
            jb_inspect.http_get = original_http_get

        self.assertFalse(result)

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

    def test_status_with_clean_result_exits_zero(self):
        body = {"clean_inspection": True, "is_scanning": False}
        result = {
            "status": jb_inspect.status_label(body),
            "clean": jb_inspect.classify_status_body_clean(body),
        }
        self.assertEqual(jb_inspect.classify_status_exit(result), 0)

    def test_status_with_results_is_informational(self):
        body = {"has_inspection_results": True, "is_scanning": False}
        result = {
            "status": jb_inspect.status_label(body),
            "clean": jb_inspect.classify_status_body_clean(body),
        }
        self.assertEqual(jb_inspect.classify_status_exit(result), 0)

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

    def test_status_explicit_ready_values_exit_zero(self):
        for status in ("clean", "results_available"):
            with self.subTest(status=status):
                body = {"status": status}
                result = {"status": status, "clean": jb_inspect.classify_status_body_clean(body)}
                self.assertEqual(jb_inspect.classify_status_exit(result), 0)

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
        self.assertIn("exit_reason=deadline", text)
        self.assertIn("view_ready_ok=False", text)
        self.assertIn("successful_extraction_count=3", text)


if __name__ == "__main__":
    unittest.main()
