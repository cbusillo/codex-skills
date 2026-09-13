#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Focused offline tests for cleanup parking fixtures and fake GitHub traffic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from stat import S_IWGRP, S_IWOTH, S_IWUSR

import parking_fixture


ROOT = Path(__file__).resolve().parents[2]
GH_COMMENT = ROOT / "github/scripts/gh-comment"


def _state(path: Path) -> dict:
    value = parking_fixture._provider_state()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return value


def _events(state_path: Path) -> list[dict]:
    journal = state_path.with_suffix(".events.jsonl")
    if not journal.exists():
        return []
    return [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line]


class CleanupProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cleanup-provider-")
        self.base = Path(self.temporary.name)
        (self.base / "tmp").mkdir()
        (self.base / "home").mkdir()
        self.state_path = self.base / "provider/state.json"
        _state(self.state_path)
        self.shim = parking_fixture._write_provider_shim(self.base)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base / "home"),
            "TMPDIR": str(self.base / "tmp"),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GITHUB_API_GH": str(self.shim),
            "GH_COMMENT_GH": str(self.shim),
            "CLEANUP_FIXTURE_PROVIDER_STATE": str(self.state_path),
            "CODEX_AUTOMATION_LOGIN": "fixture-bot",
            "GH_COMMENT_PYTHON": sys.executable,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_repository_capability_reads_are_file_backed(self) -> None:
        observed = {}
        for repo in (parking_fixture.SOURCE_REPO, parking_fixture.OWNER_REPO):
            result = subprocess.run(
                [str(self.shim), "repo", "view", repo, "--json", "nameWithOwner,hasIssuesEnabled"],
                text=True,
                capture_output=True,
                env=self.env,
                timeout=10,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            observed[repo] = json.loads(result.stdout)
        self.assertFalse(observed[parking_fixture.SOURCE_REPO]["hasIssuesEnabled"])
        self.assertTrue(observed[parking_fixture.OWNER_REPO]["hasIssuesEnabled"])

    def test_actual_comment_helper_writes_exact_owner_and_supports_readback(self) -> None:
        body = "Park work/201 at 0123456789abcdef; review pending; adopt or discard after review."
        result = subprocess.run(
            [str(GH_COMMENT), "issue", "12", "--repo", parking_fixture.OWNER_REPO],
            input=body,
            text=True,
            capture_output=True,
            env=self.env,
            timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["ok"])
        self.assertEqual("fixture-bot", envelope["actor"])
        self.assertEqual("created", envelope["comment_action"])

        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(1, len(saved["comments"]))
        self.assertIn(body, saved["comments"][0]["body"])
        self.assertIn("github-skill-operation:", saved["comments"][0]["body"])

        readback = subprocess.run(
            [
                str(self.shim),
                "api",
                "--include",
                "--method",
                "GET",
                f"/repos/{parking_fixture.OWNER_REPO}/issues/12/comments?per_page=100&page=1",
            ],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=10,
        )
        self.assertEqual(0, readback.returncode, readback.stderr)
        payload = json.loads(readback.stdout.split("\n\n", 1)[1])
        self.assertIn(body, payload[0]["body"])

        events = _events(self.state_path)
        self.assertEqual(
            [
                ("GET", "user", 200),
                ("GET", f"repos/{parking_fixture.OWNER_REPO}/issues/12/comments", 200),
                ("POST", f"repos/{parking_fixture.OWNER_REPO}/issues/12/comments", 201),
                ("GET", f"repos/{parking_fixture.OWNER_REPO}/issues/12/comments", 200),
            ],
            [(event["method"], event["endpoint"], event["status"]) for event in events],
        )
        for credential in ("GH_TOKEN", "GITHUB_TOKEN", "CODEX_GITHUB_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
            self.assertNotIn(credential, self.env)

    def test_unspecified_write_is_rejected_and_recorded(self) -> None:
        before = self.state_path.read_bytes()
        result = subprocess.run(
            [str(self.shim), "api", "--include", "--method", "DELETE", f"/repos/{parking_fixture.OWNER_REPO}"],
            text=True,
            capture_output=True,
            env=self.env,
            timeout=10,
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("Operation unavailable in this fixture", result.stdout)
        self.assertEqual(before, self.state_path.read_bytes())
        self.assertEqual(
            {"method": "DELETE", "endpoint": f"repos/{parking_fixture.OWNER_REPO}", "status": 403},
            _events(self.state_path)[-1],
        )


class ParkingFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="cleanup-parking-fixture-")
        self.base = Path(self.temporary.name)
        self.catalog = self.base / "catalog"
        self.catalog.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reroute_exposes_disabled_source_without_mutating_configured_owner(self) -> None:
        case_root = self.base / "reroute"
        outcome_root = self.base / "reroute-outcomes"
        case_root.mkdir()
        outcome_root.mkdir()
        manifest, facts = parking_fixture.create_case(
            "parking_reroute",
            case_root,
            self.catalog,
            outcome_root=outcome_root,
        )
        details = facts["facts"]
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base),
            "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
            **manifest["environment"],
        }
        result = subprocess.run(
            [manifest["environment"]["GITHUB_API_GH"], "repo", "view", parking_fixture.SOURCE_REPO],
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(json.loads(result.stdout)["hasIssuesEnabled"])
        provider = json.loads(Path(details["provider_state"]).read_text(encoding="utf-8"))
        self.assertTrue(provider["repositories"][parking_fixture.OWNER_REPO]["has_issues"])
        self.assertEqual([], provider["comments"])
        events = _events(Path(details["provider_state"]))
        self.assertEqual(
            [{"method": "GET", "endpoint": f"repos/{parking_fixture.SOURCE_REPO}", "status": 200}],
            events,
        )
        self.assertTrue(Path(details["task_worktree"]).is_dir())

    def test_positive_case_supports_verified_push_comment_and_checkout_retirement(self) -> None:
        case_root = self.base / "positive"
        outcome_root = self.base / "positive-outcomes"
        case_root.mkdir()
        outcome_root.mkdir()
        manifest, facts = parking_fixture.create_case(
            "parking",
            case_root,
            self.catalog,
            outcome_root=outcome_root,
        )
        details = facts["facts"]
        worktree = Path(details["task_worktree"])
        primary = Path(details["primary_checkout"])
        parking_fixture._git(worktree, "push", "origin", parking_fixture.TASK_BRANCH)
        remote_sha = parking_fixture._git(
            worktree,
            "ls-remote",
            "--refs",
            "origin",
            details["remote_task_ref"],
        ).stdout.split()[0]
        self.assertEqual(details["task_sha"], remote_sha)

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base),
            "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GH_COMMENT_PYTHON": sys.executable,
            **manifest["environment"],
        }
        handoff = details["handoff"]
        body = "\n".join(
            (
                f"Branch: {details['task_branch']}",
                f"SHA: {details['task_sha']}",
                f"Intent: {handoff['intent']}",
                f"Review status: {handoff['review_status']}",
                f"Why retained: {handoff['retention_or_supersession']}",
                f"Next action: {handoff['next_action']}",
            )
        )
        comment = subprocess.run(
            [str(GH_COMMENT), "issue", "12", "--repo", parking_fixture.OWNER_REPO],
            input=body,
            text=True,
            capture_output=True,
            env=env,
            timeout=20,
        )
        self.assertEqual(0, comment.returncode, comment.stderr)
        saved = json.loads(Path(details["provider_state"]).read_text(encoding="utf-8"))
        self.assertIn(body, saved["comments"][0]["body"])

        parking_fixture._git(primary, "worktree", "remove", str(worktree))
        self.assertFalse(worktree.exists())
        self.assertEqual(details["primary_head"], parking_fixture._head(primary))
        self.assertEqual(details["task_sha"], parking_fixture._head(primary, parking_fixture.TASK_BRANCH))

    def test_cases_keep_runner_inputs_and_expected_facts_separate(self) -> None:
        for case_name in parking_fixture.CASES:
            with self.subTest(case=case_name):
                case_root = self.base / case_name
                case_root.mkdir()
                outcome_root = self.base / f"{case_name}-outcomes"
                outcome_root.mkdir()
                manifest, facts = parking_fixture.create_case(
                    case_name,
                    case_root,
                    self.catalog,
                    outcome_root=outcome_root,
                )
                self.assertEqual(
                    {"name", "workspace", "catalog", "prompts", "outcome", "environment"},
                    set(manifest),
                )
                self.assertEqual(
                    {"GITHUB_API_GH", "GH_COMMENT_GH", "CLEANUP_FIXTURE_PROVIDER_STATE", "CODEX_AUTOMATION_LOGIN"},
                    set(manifest["environment"]),
                )
                workspace = Path(manifest["workspace"])
                self.assertEqual(
                    {**parking_fixture.FIXTURE_MARKER, "case": manifest["name"]},
                    json.loads((workspace / ".cleanup-fixture").read_text(encoding="utf-8")),
                )
                self.assertFalse(parking_fixture._under(case_root / "facts.json", workspace))
                self.assertFalse(parking_fixture._under(case_root / "facts.json", self.catalog))
                adapter = Path(manifest["environment"]["GITHUB_API_GH"])
                self.assertFalse(parking_fixture._under(adapter, workspace))
                self.assertFalse(parking_fixture._under(adapter, self.catalog))
                self.assertFalse(adapter.stat().st_mode & (S_IWUSR | S_IWGRP | S_IWOTH))
                self.assertNotIn("facts.json", json.dumps(manifest))
                self.assertEqual(outcome_root.resolve(), Path(manifest["outcome"]).parent)
                self.assertEqual(facts, json.loads((case_root / "facts.json").read_text(encoding="utf-8")))
                self.assertTrue((workspace / "library").is_dir())
                self.assertTrue((workspace / "checkouts/201").is_dir())
                self.assertEqual("main", parking_fixture._git(workspace / "library", "branch", "--show-current").stdout.strip())
                self.assertEqual(
                    "",
                    parking_fixture._git(
                        workspace,
                        "--git-dir",
                        str(workspace / "remotes/library.git"),
                        "for-each-ref",
                        "--format=%(objectname)",
                        f"refs/heads/{parking_fixture.TASK_BRANCH}",
                    ).stdout.strip(),
                )
                effects = facts["facts"]["expected_effects"]
                self.assertEqual(case_name != "parking_scope", effects["push_allowed"])
                self.assertEqual(case_name == "parking", effects["issue_comment_required"])
                if case_name == "parking_reroute":
                    self.assertEqual([None, facts["facts"]["task_sha"]], effects["remote_task_ref_allowed"])
                    self.assertEqual(parking_fixture.SOURCE_REPO, effects["publication_scope_repo"])
                    self.assertFalse(effects["owner_issue_authorized"])


if __name__ == "__main__":
    unittest.main()
