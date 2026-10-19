#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline routing regressions through the real selection and creation paths."""

from argparse import Namespace
import subprocess
import unittest
from unittest.mock import patch

import github_agent as agent
import github_issue
import test_gh_plan_next as next_tests
import test_github_issue as issue_tests


class AgentTests(unittest.TestCase):
    def setUp(self):
        # Environment fixtures must not inherit the harness running this suite.
        self.parent_probe = patch.object(subprocess, "run")
        self.ps = self.parent_probe.start()
        self.ps.return_value = subprocess.CompletedProcess([], 1, "", "")
        self.addCleanup(self.parent_probe.stop)

    def test_remote_codex_daemon_resolves_inherited_claude_markers(self):
        inherited = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "supervisor",
                     "CODEX_THREAD_ID": "worker"}
        self.ps.side_effect = [subprocess.CompletedProcess([], 0, row, "") for row in
                               ("20 uv\n", "30 /bin/zsh\n", "1 /opt/app-server/bin/codex\n")]
        with patch.dict(agent.os.environ, inherited, clear=True), patch.object(agent.os, "getppid", return_value=10):
            detected = agent.running_agent()
        self.assertEqual(detected, "codex")
        self.assertIsNone(agent.exclusion({"labels": ["agent:codex"]}, detected))
        self.assertEqual(agent.exclusion({"labels": ["agent:claude"]}, detected)["exclusion"], "assigned_elsewhere")

    def test_detection_and_explicit_family(self):
        self.assertIsNone(agent.running_agent(environ={}))
        self.assertEqual(agent.running_agent(environ={"CLAUDECODE": "1"}), "claude")
        inherited = {"CLAUDECODE": "1", "CODEX_THREAD_ID": "session"}
        self.assertIsNone(agent.running_agent(environ=inherited))
        self.assertEqual(agent.running_agent("codex", environ=inherited), "codex")
        self.assertIsNotNone(agent.exclusion({"labels": ["agent:codex"]}, agent.running_agent(environ=inherited)))
        self.assertEqual(agent.running_agent("claude", environ=inherited), "claude")

    def test_nearest_native_harness_wins_without_session_markers(self):
        for family in agent.FAMILIES:
            other = next(name for name in agent.FAMILIES if name != family)
            self.ps.side_effect = [subprocess.CompletedProcess([], 0, row, "") for row in
                                   ("20 /bin/sh\n", f"30 /opt/bin/{family}\n", f"1 /opt/bin/{other}\n")]
            with patch.dict(agent.os.environ, {}, clear=True), patch.object(agent.os, "getppid", return_value=10):
                self.assertEqual(agent.running_agent(), family)
            self.ps.reset_mock()

    def test_unavailable_or_ambiguous_process_evidence_stays_unknown(self):
        cases = ([subprocess.CompletedProcess([], 1, "", "")],
                 [OSError("ps unavailable")],
                 [subprocess.TimeoutExpired("ps", 0.1)],
                 [subprocess.CompletedProcess([], 0, "invalid", "")],
                 [subprocess.CompletedProcess([], 0, "10 /bin/sh", "")],
                 [subprocess.CompletedProcess([], 0, "20 node", ""),
                  subprocess.CompletedProcess([], 0, "1 codex", "")],
                 [subprocess.CompletedProcess([], 0, "20 /opt/claude/versions/2.1.296", ""),
                  subprocess.CompletedProcess([], 0, "1 codex", "")])
        for rows in cases:
            with self.subTest(rows=rows), patch.dict(agent.os.environ, {}, clear=True), \
                    patch.object(agent.os, "getppid", return_value=10):
                self.ps.side_effect = rows
                self.assertIsNone(agent.running_agent())

    def test_probe_is_bounded_and_reads_no_arguments_or_environment(self):
        self.ps.side_effect = [subprocess.CompletedProcess([], 0, f"{pid + 1} /bin/sh", "")
                               for pid in range(10, 40)]
        with patch.dict(agent.os.environ, {}, clear=True), patch.object(agent.os, "getppid", return_value=10):
            self.assertIsNone(agent.running_agent())
        self.assertLess(self.ps.call_count, 30)
        for call in self.ps.call_args_list:
            requested_fields = call.args[0][call.args[0].index("-o") + 1].replace("=", "").split(",")
            self.assertTrue(set(requested_fields) <= {"pid", "ppid", "comm", "ucomm", "fname"})
            self.assertNotIn("-E", call.args[0])
            self.assertGreater(call.kwargs["timeout"], 0)

    def test_explicit_and_single_family_markers_do_not_probe_processes(self):
        with patch.dict(agent.os.environ, {"CLAUDECODE": "1"}, clear=True):
            self.assertEqual(agent.running_agent(), "claude")
            self.assertEqual(agent.running_agent("codex"), "codex")
        self.ps.assert_not_called()

    def test_local_next_both_directions_before_limit(self):
        for family, other, scan in (("codex", "claude", 1), ("claude", "codex", 1),
                                    ("codex", "claude", 50), ("claude", "codex", 50)):
            module = next_tests.load_module()
            items = [next_tests.issue(1, labels=["plan", "plan:active", f"agent:{other}"]),
                     next_tests.issue(2, labels=["plan", "plan:active", f"agent:{family}"]),
                     next_tests.issue(3)]
            result = {}
            with patch.multiple(module, default_repo=lambda _: "owner/repo",
                                load_config=lambda _: module.DEFAULT_CONFIG,
                                load_direction=lambda _: None,
                                collect_paged_rest_items=lambda path, **_k: ("bot", [{
                                    "user": {"login": "owner"}, "body": module.github_plan_wait.marker(
                                        module.github_plan_wait.reviewed(evidence="Routing fixture has no person/event holds"))}]
                                    if path.endswith("/comments") else items),
                                next_focus_context=lambda *_: ("bot", {}, {"available": True}),
                                read_next_issue_relationships=lambda *_: ("bot", next_tests.relationships(), []),
                                emit=result.update):
                module.cmd_next(Namespace(repo="owner/repo", agent=family, milestone=None,
                                          limit=1, scan_limit=scan))
            self.assertEqual([item["number"] for item in result["candidates"]], [2])
            self.assertEqual(result["candidate_count"], min(2, scan))
            self.assertEqual(result["excluded"][0]["agent_labels"], [f"agent:{other}"])

    def test_global_next_both_directions(self):
        for family, other in (("codex", "claude"), ("claude", "codex")):
            root = next_tests.track("someone/direction", 1, "First")
            leaves = [next_tests.global_issue("someone/product", 2, labels=[f"agent:{other}"]),
                      next_tests.global_issue("someone/product", 3, labels=[f"agent:{family}"]),
                      next_tests.global_issue("someone/product", 4)]
            edges = {("someone/direction", 1): next_tests.relationships(sub_issues=leaves)}
            with next_tests.global_fixture([root], leaves, edges) as (module, result, _):
                args = next_tests.next_args()
                args.agent = family
                args.limit = 1
                module.cmd_next(args)
            self.assertEqual([item["number"] for item in result["candidates"]], [3])
            self.assertEqual(result["candidate_count"], 2)
            self.assertFalse(any(item["number"] == 2 for item in result["available_candidates"]))
            self.assertTrue(any(item.get("number") == 2 and item["exclusion"] == "assigned_elsewhere"
                                for item in result["excluded"]))

    def test_global_graph_budget_preserves_both_family_leaves(self):
        for family, other in (("codex", "claude"), ("claude", "codex")):
            root = next_tests.track("someone/direction", 1, "First")
            leaves = [next_tests.global_issue("someone/product", 2, labels=[f"agent:{other}"]),
                      next_tests.global_issue("someone/product", 3, labels=[f"agent:{family}"])]
            edges = {(root["repo"], 1): next_tests.relationships(sub_issues=leaves)}
            with next_tests.global_fixture([root], leaves, edges) as (module, result, _):
                args = next_tests.next_args(scan_limit=2)
                args.agent = family
                module.cmd_next(args)
            self.assertEqual([item["number"] for item in result["candidates"]], [3])
            self.assertFalse(result["graph_context"]["truncated"])
            self.assertTrue(any(item.get("number") == 2 and item["exclusion"] == "assigned_elsewhere"
                                for item in result["excluded"]))

    def test_discovery_budget_preserves_both_family_leaves(self):
        for family, other in (("codex", "claude"), ("claude", "codex")):
            leaves = [next_tests.global_issue("someone/product", 2, labels=[f"agent:{other}"]),
                      next_tests.global_issue("someone/product", 3, labels=[f"agent:{family}"])]
            with next_tests.global_fixture([], [], {}, discovered=leaves) as (module, result, _):
                args = next_tests.next_args(scan_limit=1)
                args.agent = family
                module.cmd_next(args)
            self.assertEqual([item["number"] for item in result["candidates"]], [3])
            self.assertTrue(result["discovery_context"]["complete"])
            self.assertTrue(any(item.get("number") == 2 and item["exclusion"] == "assigned_elsewhere"
                                for item in result["excluded"]))

    def test_family_graph_allowance_is_bounded_and_traverses_dependencies(self):
        for family, other in (("codex", "claude"), ("claude", "codex")):
            root = next_tests.track("someone/direction", 1, "First")
            parent = next_tests.global_issue("someone/product", 2, labels=[f"agent:{other}"])
            blocker = next_tests.global_issue("someone/product", 3, labels=[f"agent:{family}"])
            edges = {(root["repo"], 1): next_tests.relationships(sub_issues=[parent]),
                     (parent["repo"], 2): next_tests.relationships(blocked_by=[blocker])}
            with next_tests.global_fixture([root], [parent, blocker], edges) as (module, result, _):
                args = next_tests.next_args(scan_limit=2)
                args.agent = family
                module.cmd_next(args)
            self.assertEqual([item["number"] for item in result["candidates"]], [3])
            self.assertEqual(result["candidates"][0]["via"][-1]["relationship"], "blocked_by")

            leaves = [next_tests.global_issue("someone/product", n, labels=[f"agent:{other}"])
                      for n in range(2, 12)]
            leaves.append(next_tests.global_issue("someone/product", 12, labels=[f"agent:{family}"]))
            edges = {(root["repo"], 1): next_tests.relationships(sub_issues=leaves)}
            with next_tests.global_fixture([root], leaves, edges) as (module, result, _):
                args = next_tests.next_args(scan_limit=2)
                args.agent = family
                module.cmd_next(args)
            self.assertFalse(result["candidates"])
            self.assertEqual(result["evaluated"], 2 * args.scan_limit)
            self.assertTrue(result["graph_context"]["truncated"])
            self.assertFalse(result["graph_context"]["complete"])

    def test_family_discovery_omissions_preserve_milestone_and_parent_waits(self):
        for family, other in (("codex", "claude"), ("claude", "codex")):
            parent = next_tests.global_issue("someone/product", 10,
                labels=["plan:waiting"], body="## Current Status\nWaiting for: Owner testing", waiting_for="Owner testing")
            leaves = [next_tests.global_issue("someone/product", n, labels=[f"agent:{other}"])
                      for n in (2, 3)]
            leaves[1]["milestone"] = next_tests.milestone_data(1, "First", created_at="2026-01-01T00:00:00Z")
            child = next_tests.global_issue("someone/product", 4, labels=[f"agent:{family}"])
            edges = {(parent["repo"], 10): next_tests.relationships(sub_issues=[child])}
            with next_tests.global_fixture([], [parent], edges, discovered=[*leaves, child]) as (module, result, _):
                args = next_tests.next_args(scan_limit=1)
                args.agent = family
                module.cmd_next(args)
            self.assertFalse(result["candidates"])
            self.assertTrue(any(item.get("number") == 4 and item["exclusion"] == "parent_waiting"
                                for item in result["excluded"]))
            self.assertEqual([item["number"] for item in result["discovery_context"]["unevaluated_milestone_issues"]], [3])
            self.assertFalse(result["candidate_coverage"]["complete"])
            self.assertFalse(result["tooling_capacity_context"]["admitted"])

    def test_other_family_frontier_still_controls_capacity_admission(self):
        for family, other in (("codex", "claude"), ("claude", "codex")):
            roots = [next_tests.track("someone/direction", 1, "First"),
                     next_tests.track("someone/direction", 2, "Second")]
            leaves = [next_tests.global_issue("someone/business", n, labels=[f"agent:{assigned}", "plan:waiting"], waiting_for="Customer testing",
                                              body="## Current Status\nWaiting for: Customer testing.")
                      for n, assigned in ((10, other), (11, family))]
            tool = next_tests.global_issue("someone/tools", 20, labels=[f"agent:{family}"])
            edges = {(root["repo"], root["number"]): next_tests.relationships(sub_issues=[leaf])
                     for root, leaf in zip(roots, leaves)}
            with next_tests.global_fixture(roots, leaves, edges, discovered=[tool], clear_waits=[tool]) as (module, result, _):
                args = next_tests.next_args(scan_limit=3)
                args.agent = family
                module.cmd_next(args)
                items = {item["number"]: item for item in [*result["candidates"], *result["excluded"]]}
                context = {"issues": {
                    **{f"someone/business#{n}": next_tests.reviewed(items[n], "waiting", waiting_on="person")
                       for n in (10, 11)},
                    "someone/tools#20": next_tests.reviewed(items[20], category="repeated_stop_tooling"),
                }}
                with patch.object(module, "next_selection_context", return_value=context):
                    module.cmd_next(args)
                    self.assertTrue(result["tooling_capacity_context"]["admitted"])
                    self.assertIn(20, [item["number"] for item in result["available_candidates"]])
                    context["issues"]["someone/business#10"]["state"] = "underway"
                    module.cmd_next(args)
                    self.assertFalse(result["tooling_capacity_context"]["admitted"])
                    self.assertNotIn(20, [item["number"] for item in result["available_candidates"]])

    def test_unknown_and_conflicting_assignment_require_resolution(self):
        self.assertIsNone(agent.exclusion({"labels": []}, None))
        self.assertIsNotNone(agent.exclusion({"labels": ["agent:codex"]}, None))
        self.assertIsNotNone(agent.exclusion({"labels": ["agent:codex", "agent:claude"]}, "codex"))
        with self.assertRaises(ValueError):
            agent.creation_labels(["agent:claude"], "codex")

    def test_creation_ensures_label_and_applies_assignment(self):
        for family in agent.FAMILIES:
            label = f"agent:{family}"

            def callback(method, path, body, **kwargs):
                self.assertEqual(kwargs["gh_cmd"], "fake-gh")
                if path == "/user":
                    return issue_tests.success({"login": "fixture-automation"})
                if path.endswith(f"/labels/agent%3A{family}"):
                    return issue_tests.failure(404, {"message": "Not Found"}, is_write=False)
                if path.endswith("/labels"):
                    self.assertEqual(body["name"], label)
                    return issue_tests.success(body, status=201)
                if method == "GET":
                    return issue_tests.success([])
                self.assertEqual(body["labels"], [label])
                return issue_tests.success({**issue_tests.issue_body(), "labels": [{"name": label}]}, status=201)

            def run(calls):
                payload = github_issue.create_issue("Assigned", "Work", repo="owner/repo",
                                                   agent=family, gh_cmd="fake-gh", expected_actor="fixture-automation")
                self.assertEqual(payload["labels"], [label])
                self.assertLess(next(i for i, call in enumerate(calls) if call["path"].endswith("/labels")),
                                next(i for i, call in enumerate(calls) if call["method"] == "POST" and call["path"].endswith("/issues")))

            issue_tests.with_call_stub(callback, run)

    def test_planning_creation_passes_assignment_to_shared_creation(self):
        module = next_tests.load_module()
        for family in agent.FAMILIES:
            args = module.build_parser().parse_args(["--repo", "owner/repo", "create", "Assigned", "--agent", family])
            captured = {}
            def create(title, body, **kwargs):
                captured.update(kwargs)
                return {"repo": "owner/repo", "number": 1, "url": "https://github.com/owner/repo/issues/1"}
            with patch.multiple(module, default_repo=lambda _: "owner/repo",
                                load_config=lambda _: {**module.DEFAULT_CONFIG, "projects": {"enabled": False}},
                                find_existing_plan_issues=lambda *_a, **_k: ("bot", []),
                                ensure_labels=lambda *_a, **_k: ("bot", []),
                                refresh_planning_targets=lambda *_a, **_k: {"ok": True, "coverage": "complete", "issues": []},
                                comment_route=lambda: ("bot", "fake-gh", "bot"), emit=lambda _: None), \
                    patch.object(module.github_issue_core, "create_issue", side_effect=create):
                module.cmd_create(args)
            self.assertIn(f"agent:{family}", captured["labels"])

    def test_creation_label_read_failure_never_creates_issue(self):
        def callback(method, path, body, **_kwargs):
            if path == "/user":
                return issue_tests.success({"login": "fixture-automation"})
            self.assertEqual(method, "GET")
            return issue_tests.failure(403, {"message": "Resource not accessible by integration"}, is_write=False)
        def run(calls):
            with self.assertRaises(github_issue.IssueError):
                github_issue.create_issue("Assigned", "Work", repo="owner/repo", agent="codex",
                                          gh_cmd="fake-gh", expected_actor="fixture-automation")
            self.assertFalse(any(call["method"] == "POST" for call in calls))
        issue_tests.with_call_stub(callback, run)

    def test_ensure_labels_includes_families_with_custom_plan_labels(self):
        module = next_tests.load_module()
        result = {}
        with patch.multiple(module, default_repo=lambda _: "owner/repo",
                            load_config=lambda _: {"labels": {"plan": "custom-plan"}},
                            ensure_labels=lambda *_: ("bot", []), emit=result.update):
            module.cmd_ensure_labels(Namespace(repo="owner/repo"))
        self.assertTrue(set(agent.LABEL_DEFS).issubset(result["ensured"]))
        self.assertIn("custom-plan", result["ensured"])


if __name__ == "__main__":
    unittest.main()
