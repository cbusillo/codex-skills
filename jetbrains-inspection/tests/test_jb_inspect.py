#!/usr/bin/env python3
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
