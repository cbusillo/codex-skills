#!/usr/bin/env python3
import importlib.util
import json
import sys
import tempfile
import unittest
from argparse import Namespace
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


if __name__ == "__main__":
    unittest.main()
