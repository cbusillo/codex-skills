#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
import io
import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import catalog_landing as landing
import services
from check_operation import GateFailed, Operation
from session_record import TranscriptChanged

from skills.github.scripts import github_api

operation_run = Operation.run
reconcile_runtime = landing.reconcile_runtime


class CatalogLandingTests(unittest.TestCase):
    def setUp(self) -> None:
        metadata = patch.dict(vars(github_api.github_catalog_health), {"marker_fields": lambda: "catalog=unknown"})
        metadata.start()
        self.addCleanup(metadata.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config: dict[str, Any] = {"repo": "owner/catalog", "issue": 1, "source_worktree": str(self.root),
                                       "runtime_worktree": str(self.root), "ledger": str(self.root / "ledger.json"),
                                       "supervisor_session": "controller"}
        runtime = patch.object(Operation, "run", side_effect=lambda gate, *args, **kwargs:
                               {"ok": True} if gate.startswith("supersede_") else {"status": "already_current", "after_sha": "tip"})
        self.runner = runtime.start()
        self.addCleanup(runtime.stop)
        reconcile = patch.object(landing, "reconcile_runtime", return_value=(self.root, "tip"))
        reconcile.start()
        self.addCleanup(reconcile.stop)

    def test_discovery_preserves_first_parent_order_and_refuses_missing_cursor(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        commits = []
        for number in range(3):
            subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-q", "--allow-empty", "-m", str(number)], check=True)
            commits.append(landing.git(self.root, "rev-parse", "HEAD"))
        self.assertEqual(landing.pending_landings(self.root, commits[0], commits[-1]), commits[1:])
        with self.assertRaises(GateFailed):
            landing.pending_landings(self.root, "absent", commits[-1])

    def test_interrupted_bootstrap_retains_its_landing_and_posts_before_advancing(self) -> None:
        self.config["notify_config"] = str(self.root / "unbound-services.json")
        with (patch.object(landing, "pending_landings", return_value=["first"]) as discover,
              patch.object(landing, "git", return_value="script.py"),
              patch.object(landing, "verify", return_value={"ok": True, "runtime_sha": "tip"}) as verify,
              patch.object(landing, "post_result", side_effect=[GateFailed("report", "interrupted"), "comment"]) as post):
            with self.assertRaises(GateFailed):
                landing.poll(self.config, self.root)
            self.assertIsNone(json.loads((self.root / "cursor.json").read_text())["last_sha"])
            self.assertTrue(landing.poll(self.config, self.root)["ok"])
            self.assertEqual(discover.call_count, 1)
            self.assertEqual(verify.call_count, 1)
            self.assertEqual(verify.call_args.args[0]["supervisor_session"], self.config["supervisor_session"])
            self.assertEqual(post.call_count, 2)
            self.assertEqual(json.loads((self.root / "cursor.json").read_text())["last_sha"], "first")

    def test_merge_squash_and_train_history_retries_delayed_landing_association(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        tree = landing.git(self.root, "write-tree")

        def commit(subject: str, *parents: str) -> str:
            return landing.git(self.root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                               "commit-tree", tree, "-m", subject, *[arg for parent in parents for arg in ("-p", parent)])

        previous = commit("previous verified landing")
        branch = commit("lead fix", previous)
        branch = commit("lead follow-up", branch)
        merge = commit("Merge pull request #3223", previous, branch)
        squash = commit("Direction skill: owed turn (#3221)", merge)
        squash_merge = commit("Merge pull request #3225", merge, squash)
        train_branch = commit("product fix", squash_merge)
        train = commit("Launchplane merge train: merge PR #3229", squash_merge, train_branch)
        train_merge = commit("Merge pull request #3230", squash_merge, train)
        self.assertEqual(landing.pending_landings(self.root, previous, train_merge), [merge, squash_merge, train_merge])
        elapsed = 0.0

        def advance(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        pull = {"number": 3223, "merged_at": "merged", "merge_commit_sha": merge,
                "base": {"repo": {"full_name": self.config["repo"]}}}
        with (patch.object(landing.time, "time", side_effect=lambda: elapsed),
              patch.object(landing.time, "sleep", side_effect=advance),
              patch.object(landing.GitHubReader, "paged_json", side_effect=[[], [{**pull, "number": 999}], [pull]])):
            self.assertEqual(landing.landing_pull(landing.GitHubReader(deadline_at=10), self.config["repo"], merge,
                                                  expected_pr=pull["number"]), pull["number"])

    def test_runtime_reconciliation_uses_the_same_retrying_exact_pr_proof(self) -> None:
        self.config.update(pr=2, landing_sha="a" * 40)
        pull = {"number": 2, "merged_at": "merged", "merge_commit_sha": self.config["landing_sha"],
                "base": {"repo": {"full_name": self.config["repo"]}}}
        with (patch.object(landing.GitHubReader, "paged_json", side_effect=[[], [pull]]),
              patch.object(landing.time, "sleep"),
              patch.object(Operation, "run", side_effect=GateFailed("runtime", "fixture"))):
            with self.assertRaises(GateFailed) as failure:
                reconcile_runtime(self.config, Operation(self.root, ["runtime"]))
            self.assertEqual(failure.exception.gate, "runtime")

    def test_unmatched_direct_push_exhausts_discovery_deadline_without_accepting_related_pr(self) -> None:
        elapsed = 0.0

        def advance(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        unrelated = {"number": 2, "merged_at": "merged", "merge_commit_sha": "other",
                     "base": {"repo": {"full_name": self.config["repo"]}}}
        with (patch.object(landing.time, "time", side_effect=lambda: elapsed),
              patch.object(landing.time, "sleep", side_effect=advance),
              patch.object(landing.GitHubReader, "paged_json", return_value=[unrelated]) as read):
            reader = landing.GitHubReader(deadline_at=3)
            with self.assertRaises(GateFailed) as failure:
                landing.landing_pull(reader, self.config["repo"], "direct-push")
            self.assertEqual(failure.exception.gate, "discovery")
            self.assertIn("direct-push", str(failure.exception))
            self.assertGreater(read.call_count, 1)
            self.assertLessEqual(elapsed, reader.deadline_at)

    def test_cleared_service_rechecks_failed_batch_instead_of_replaying_failure(self) -> None:
        config = self.root / "services.json"
        config.write_text(json.dumps({"uv": "/fixture/uv", "services": {"catalog": {
            "argv": ["/fixture/catalog"], "cwd": str(self.root), "restart_seconds": 1}}}))
        stopped = threading.Event()
        log = io.StringIO()

        def child(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
            result = landing.poll(self.config, self.root)
            print(json.dumps(result))
            code = int(not result["ok"])
            if result["ok"]:
                stopped.set()
            return SimpleNamespace(pid=123, returncode=code, poll=lambda: code)

        with (patch.object(landing, "pending_landings", return_value=["first", "second"]),
              patch.object(landing, "git", return_value="script.py"),
              patch.object(landing, "verify", side_effect=[{"ok": False, "gate": "codex_handoff_request",
                  "reason": "transcript changed during read; check again"}, {"ok": True}]) as verify,
              patch.object(landing, "post_result", return_value="comment"),
              patch.object(services.threading, "Event", return_value=stopped),
              patch.object(services.subprocess, "Popen", side_effect=child),
              patch.object(services, "stop_child"), redirect_stdout(log)):
            command = ["--config", str(config), "--service", "catalog"]
            self.assertEqual(services.main(["run", *command]), 1)
            self.assertEqual(services.main(["run", *command]), 1)
            self.assertEqual(verify.call_count, 1)
            first = verify.call_args.args[1]
            self.assertEqual(services.main(["clear", *command]), 0)
            self.assertEqual(services.main(["run", *command]), 0)
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(verify.call_args.args[1].evidence, first.evidence)
            self.assertEqual(json.loads((self.root / "cursor.json").read_text())["last_sha"], "second")
        reports = [row for line in log.getvalue().splitlines() if "ok" in (row := json.loads(line))]
        self.assertEqual([row["ok"] for row in reports], [False, True])
        self.assertEqual(reports[-1]["landings"], ["first", "second"])

    def test_changing_transcript_retries_until_response_or_existing_deadline(self) -> None:
        complete = {"state": "turn_ended", "size": 10, "safe_verdict": True, "last_text": "landing"}
        elapsed = 0.0

        def now() -> float:
            return elapsed

        def advance(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds

        for final in (False, True):
            for settles in (False, True):
                elapsed = 0.0
                with (self.subTest(final=final, settles=settles),
                      patch.object(landing, "session_status", side_effect=[TranscriptChanged("appending"), complete]
                                   if settles else TranscriptChanged("appending")) as read,
                      patch.object(landing.time, "monotonic", side_effect=now),
                      patch.object(landing.time, "sleep", side_effect=advance)):
                    if settles:
                        self.assertEqual(landing.observe({}, "landing", final=final), complete)
                        self.assertEqual(read.call_count, 2)
                    else:
                        with self.assertRaises(GateFailed):
                            landing.observe({}, "landing", final=final)
        with patch.object(landing, "session_status", side_effect=ValueError("stable invalid JSON")):
            with self.assertRaises(ValueError):
                landing.observe({}, "landing")

    def test_only_human_documentation_skips_smoke_after_runtime_reconciliation(self) -> None:
        for index, files in enumerate(("README.md\ndocs/guide.md", "skills/thing/SKILL.md", "instructions/global.md", "README.md\nscript.py")):
            with (self.subTest(files=files), patch.object(landing, "pending_landings", return_value=["first", "second"]),
                  patch.object(landing, "git", return_value=files),
                  patch.object(landing, "reconcile_runtime", return_value=(self.root, "tip")) as reconcile,
                  patch.object(landing, "verify_smoke", return_value="handoff") as smoke,
                  patch.object(landing, "post_result", return_value="comment")):
                evidence = self.root / str(index)
                result = landing.poll(self.config, evidence)
                self.assertTrue(result["ok"])
                self.assertEqual(result["landings"], ["first", "second"])
                reconcile.assert_called_once()
                self.assertEqual(smoke.call_count, 0 if files.startswith("README.md\ndocs/") else 2)

    def test_completed_batch_checks_again_without_repeating_smoke_or_posting(self) -> None:
        with (patch.object(landing, "pending_landings", side_effect=[["first", "second"], []]),
              patch.object(landing, "git", return_value="script.py"),
              patch.object(landing, "verify", return_value={"ok": True}) as verify,
              patch.object(landing, "post_result", return_value="comment") as post):
            self.assertEqual(landing.poll(self.config, self.root)["landings"], ["first", "second"])
            self.assertEqual(landing.poll(self.config, self.root)["landings"], [])
            self.assertEqual(verify.call_count, 1)
            self.assertEqual(post.call_count, 1)

    def test_discovery_failures_remain_failed_without_saving_a_sticky_verification(self) -> None:
        with patch.object(landing, "pending_landings", side_effect=[GateFailed("discovery", "unavailable"), []]):
            with self.assertRaises(GateFailed):
                landing.poll(self.config, self.root)
            self.assertFalse((self.root / "cursor.json").exists())
            self.assertTrue(landing.poll(self.config, self.root)["ok"])

    def test_missing_supervisor_identity_stops_before_runtime_or_launch(self) -> None:
        self.config.pop("supervisor_session")
        with patch.object(landing, "pending_landings", return_value=[]), self.assertRaises(GateFailed) as failure:
            landing.poll(self.config, self.root)
        self.assertEqual(failure.exception.gate, "identity")
        self.runner.assert_not_called()


    def test_supersede_preserves_failed_attempt_and_includes_new_fix_only_after_closeout(self) -> None:
        with (patch.object(landing, "pending_landings", side_effect=[["B"], ["C"]]),
              patch.object(landing, "git", return_value="script.py"),
              patch.object(landing, "verify", side_effect=[{"ok": False, "gate": "smoke_acknowledgement"}, {"ok": True}]) as verify,
              patch.object(landing, "post_result", return_value="comment"),
              patch.object(landing, "require_closed_smokes", side_effect=[GateFailed("closeout", "unfinished"), None]) as closeout):
            self.assertFalse(landing.poll(self.config, self.root)["ok"])
            old = verify.call_args.args[1]
            retained = {path: path.read_bytes() for path in old.evidence.glob("*.json")}
            with self.assertRaises(GateFailed):
                landing.poll(self.config, self.root, supersede_failed=True)
            self.assertEqual(verify.call_count, 1)
            result = landing.poll(self.config, self.root, supersede_failed=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["landings"], ["B", "C"])
            self.assertEqual(json.loads((self.root / "cursor.json").read_text())["last_sha"], "C")
            self.assertEqual({path: path.read_bytes() for path in old.evidence.glob("*.json")}, retained)
            self.assertNotEqual(verify.call_args.args[1].evidence, old.evidence)
            self.assertEqual(closeout.call_count, 2)

    def test_unknown_report_is_settled_by_exact_read_without_another_write(self) -> None:
        operation = Operation(self.root, ["report"])
        result = {"ok": True, "landings": ["landing"], "runtime_sha": "runtime"}
        with patch.object(Operation, "run", operation_run), patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("comment", 360)):
            with self.assertRaises(GateFailed):
                landing.post_result(self.config, operation, result)
        report = (self.root / "result.md").read_text()
        posted = {"user": {"login": "agent[bot]"}, "body": report, "html_url": "original-comment"}
        with (patch.object(Operation, "run", operation_run), patch.object(subprocess, "run") as write,
              patch.object(landing.github_identity, "automation_login", return_value="agent[bot]"),
              patch.object(landing.GitHubReader, "paged_json", side_effect=[[], [posted, posted], [posted]])):
            for _ in range(2):
                with self.assertRaises(GateFailed):
                    landing.post_result(self.config, operation, result)
            self.assertEqual(landing.post_result(self.config, operation, result), posted["html_url"])
            write.assert_not_called()

    def test_prelaunch_read_failure_retries_without_explicit_resume(self) -> None:
        with (patch.object(landing, "pending_landings", return_value=["first"]),
              patch.object(landing, "reconcile_runtime", side_effect=[GateFailed("read", "unavailable"), (self.root, "tip")]),
              patch.object(landing, "git", return_value="script.py"),
              patch.object(landing, "verify", return_value={"ok": True}) as verify,
              patch.object(landing, "post_result", return_value="comment")):
            with self.assertRaises(GateFailed):
                landing.poll(self.config, self.root)
            self.assertFalse((self.root / "first/result.json").exists())
            self.assertTrue(landing.poll(self.config, self.root)["ok"])
            verify.assert_called_once()

    def test_uncertain_failed_report_is_reconciled_before_rechecking_smokes(self) -> None:
        def run(operation: Operation, gate: str, *args: Any, **kwargs: Any) -> Any:
            if gate.startswith("report_"):
                with patch.object(Operation, "run", operation_run):
                    return operation.run(gate, *args, **kwargs)
            return {"status": "already_current", "after_sha": "tip"}

        with (patch.object(Operation, "run", run),
              patch.object(landing, "pending_landings", return_value=["first"]),
              patch.object(landing, "git", return_value="script.py"),
              patch.object(landing, "verify", side_effect=[{"ok": False}, {"ok": True}]) as verify,
              patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("comment", 360)) as write,
              patch.object(landing.GitHubReader, "paged_json", return_value=[])):
            with self.assertRaises(GateFailed):
                landing.poll(self.config, self.root)
            with self.assertRaises(GateFailed):
                landing.poll(self.config, self.root)
            self.assertEqual(verify.call_count, 1)
            self.assertEqual(write.call_count, 1)
            self.assertIsNone(json.loads((self.root / "cursor.json").read_text())["last_sha"])

    def test_smoke_records_both_runtime_observations_without_claiming_loaded_version(self) -> None:
        with patch.object(landing, "verify_smoke", return_value="handoff"), patch.object(landing, "git", return_value="after"):
            result = landing.verify({**self.config, "landing_sha": "target"}, Operation(self.root, ["runtime"]), self.root, "before")
            self.assertEqual((result["runtime_sha"], result["runtime_after_sha"]), ("before", "after"))
            self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
