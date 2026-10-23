#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise landed-worktree retirement and the sweep's finished-worktree scan with temporary repositories."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import github_plan_claim
import github_plan_lease
import landed_worktrees


NAMED = f"External worktree; {landed_worktrees.RETIRE_MARKER} <path>"
IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        isolated = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "DEV_WORKTREE_"))}
        isolated.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, HOME=str(self.base))
        patcher = mock.patch.dict(os.environ, isolated, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.remote = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)], check=True, capture_output=True)
        self.repo = self.base / "sample"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.repo)], check=True, capture_output=True)
        self.commit(self.repo, "Initial")
        self.git(self.repo, "push", "-q", "origin", "HEAD:main")
        self.git(self.repo, "remote", "set-head", "origin", "main")
        self.root = self.base / "worktrees"
        (self.root / "sample").mkdir(parents=True)

    def git(self, where, *args):
        return subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, where, message):
        self.git(where, *IDENTITY, "commit", "-q", "--allow-empty", "-m", message)
        return self.git(where, "rev-parse", "HEAD")

    def worktree(self, task, *, lock=NAMED, parent=None):
        path = (parent or self.root / "sample") / task
        self.git(self.repo, "worktree", "add", "-q", *(["--lock", "--reason", lock] if lock else []),
                 "-b", f"work/{task}", str(path), "origin/main")
        return path


class LandedDecisionTests(Fixture):
    def test_landed_branch_with_named_lock_is_a_candidate(self):
        path = self.worktree("done")
        head = self.commit(path, "Work")
        rows = landed_worktrees.landed_decisions(self.repo, "work/done", head, self.root)
        self.assertEqual([(r["path"], r["decision"]) for r in rows], [(str(path), "candidate")])

    def test_local_head_behind_the_pr_head_is_a_candidate(self):
        # The train often adds commits to the PR after the session ended.
        path = self.worktree("behind")
        self.commit(path, "Work")
        self.git(path, "push", "-q", "origin", "work/behind")
        clone = self.base / "train"
        subprocess.run(["git", "clone", "-q", "-b", "work/behind", str(self.remote), str(clone)], check=True, capture_output=True)
        pr_head = self.commit(clone, "Train refresh")
        self.git(clone, "push", "-q", "origin", "work/behind")
        self.git(self.repo, "fetch", "-q", "origin")
        rows = landed_worktrees.landed_decisions(self.repo, "work/behind", pr_head, self.root)
        self.assertEqual(rows[0]["decision"], "candidate")

    def test_commits_beyond_the_pr_head_are_kept(self):
        path = self.worktree("extra")
        pr_head = self.commit(path, "Landed")
        self.commit(path, "After the PR")
        rows = landed_worktrees.landed_decisions(self.repo, "work/extra", pr_head, self.root)
        self.assertEqual(rows[0]["decision"], "kept")

    def test_lock_without_retire_command_and_outside_root_are_kept(self):
        plain = self.worktree("plain", lock="Held by another job")
        outside = self.worktree("outside", parent=self.base)
        for path, branch in ((plain, "work/plain"), (outside, "work/outside")):
            rows = landed_worktrees.landed_decisions(self.repo, branch, self.git(path, "rev-parse", "HEAD"), self.root)
            self.assertEqual(rows[0]["decision"], "kept", rows)

    def test_other_branches_are_not_considered(self):
        self.worktree("unrelated")
        self.assertEqual(landed_worktrees.landed_decisions(self.repo, "work/landed", "0" * 40, self.root), [])

    def refreshed_pull(self):
        source = self.worktree("source")
        self.commit(source, "Source work")
        refresh = self.worktree("refresh")
        self.git(refresh, "merge", "-q", "work/source")
        pr_head = self.commit(refresh, "Refresh resolution")
        self.git(refresh, "push", "-q", "origin", "work/refresh:work/source")
        return refresh, pr_head, {self.git(source, "rev-parse", "HEAD"), pr_head}

    def test_refresh_worktree_on_a_pr_commit_is_a_candidate(self):
        refresh, pr_head, pr_commits = self.refreshed_pull()
        rows = landed_worktrees.landed_decisions(self.repo, "work/source", pr_head, self.root, pr_commits=pr_commits)
        self.assertIn((str(refresh), "candidate"), [(r["path"], r["decision"]) for r in rows])

    def test_fresh_worktree_on_the_default_branch_is_kept_even_when_the_pr_names_its_commit(self):
        fresh = self.worktree("fresh")
        main_tip = self.git(fresh, "rev-parse", "HEAD")
        rows = landed_worktrees.landed_decisions(self.repo, "work/landed", main_tip, self.root, pr_commits={main_tip})
        self.assertEqual([(r["path"], r["decision"]) for r in rows], [(str(fresh), "kept")])

    def test_worktrees_off_the_pr_commits_are_not_considered(self):
        _, pr_head, pr_commits = self.refreshed_pull()
        reviewer = self.worktree("review")
        self.commit(reviewer, "Unpushed review notes")
        rows = landed_worktrees.landed_decisions(self.repo, "work/source", pr_head, self.root, pr_commits=pr_commits)
        self.assertNotIn(str(reviewer), [r["path"] for r in rows])

    def test_open_claim_on_the_branch_keeps_the_worktree(self):
        refresh, pr_head, pr_commits = self.refreshed_pull()
        rows = landed_worktrees.landed_decisions(self.repo, "work/source", pr_head, self.root, pr_commits=pr_commits,
                                                 claims='{"branch": "work/refresh"}')
        self.assertEqual({r["path"]: r["decision"] for r in rows}[str(refresh)], "kept")


class RetireTests(unittest.TestCase):
    def runner(self, refuse_preview=False, refuse_apply=False):
        calls = []

        def run(path, *, dry_run):
            calls.append((path, dry_run))
            failed = refuse_preview if dry_run else refuse_apply
            return subprocess.CompletedProcess([], 1 if failed else 0, "", "Retire refused: dirty\n" if failed else "")
        return calls, run

    def test_apply_previews_then_retires_only_candidates(self):
        calls, run = self.runner()
        rows = landed_worktrees.retire([{"path": "/a", "decision": "candidate"}, {"path": "/b", "decision": "kept"}],
                                       apply=True, runner=run)
        self.assertEqual(calls, [("/a", True), ("/a", False)])
        self.assertEqual([r["decision"] for r in rows], ["retired", "kept"])

    def test_preview_only_never_retires(self):
        calls, run = self.runner()
        rows = landed_worktrees.retire([{"path": "/a", "decision": "candidate"}], apply=False, runner=run)
        self.assertEqual((calls, rows[0]["decision"]), ([("/a", True)], "would_retire"))

    def test_helper_refusals_keep_the_worktree(self):
        for refuse, expected_calls in (({"refuse_preview": True}, [("/a", True)]),
                                       ({"refuse_apply": True}, [("/a", True), ("/a", False)])):
            calls, run = self.runner(**refuse)
            rows = landed_worktrees.retire([{"path": "/a", "decision": "candidate"}], apply=True, runner=run)
            self.assertEqual((rows[0]["decision"], rows[0]["detail"]), ("kept", ["Retire refused: dirty"]))
            self.assertEqual(calls, expected_calls)


class MainTests(Fixture):
    def setUp(self):
        super().setUp()
        self.git(self.repo, "remote", "set-url", "origin", "https://github.com/o/sample.git")

    def run_main(self, pull, repo="o/sample", commits=(), discussions=None, bodies=None):
        config = self.base / "worktrees.json"
        config.write_text(json.dumps({"root": str(self.root), "hostHelper": str(self.base / "host-helper")}))
        discussions = discussions or {}
        reader = mock.Mock()
        reader.get_json.side_effect = lambda path, step: pull if path.endswith("/pulls/7") else {
            "body": (bodies or {}).get(path.rsplit("/", 1)[-1], "")}

        def paged_json(path, step_prefix):
            if path.endswith("/pulls/7/commits"):
                return [{"sha": sha} for sha in commits]
            discussion = discussions.get(path.split("/issues/")[1].split("/")[0])
            if isinstance(discussion, Exception):
                raise discussion
            return discussion or []

        reader.paged_json.side_effect = paged_json
        reader.graphql_json.return_value = mock.Mock(ok=True, body={"data": {"repository": {
            "defaultBranchRef": {"name": "main"},
            "pullRequest": {"merged": True, "baseRefName": "main", "url": "u", "mergeCommit": {"oid": "1" * 40},
                            "closingIssuesReferences": {"nodes": [{"number": 3, "repository": {"nameWithOwner": repo}}],
                                                        "pageInfo": {"hasNextPage": False}}}}}})
        read_errors = mock.Mock(GitHubReadError=type("ReadError", (Exception,), {}),
                                GitHubReadShapeError=type("ShapeError", (Exception,), {}),
                                GitHubReader=mock.Mock(return_value=reader))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"DEV_WORKTREE_CONFIG": str(config)}), \
                mock.patch.dict("sys.modules", {"github_read": read_errors}), \
                mock.patch.object(landed_worktrees, "run_retire") as run, redirect_stdout(out):
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            code = landed_worktrees.main(["--repo", repo, "--pr", "7", "--checkout", str(self.repo), "--apply"])
        return code, json.loads(out.getvalue()), run

    def test_open_pr_retires_nothing(self):
        path = self.worktree("open")
        code, result, run = self.run_main({"state": "open", "head": {"ref": "work/open", "sha": self.git(path, "rev-parse", "HEAD"),
                                                                      "repo": {"full_name": "o/sample"}}})
        self.assertEqual((code, result["ok"]), (2, False))
        run.assert_not_called()

    def test_merged_pr_retires_its_source_worktree(self):
        path = self.worktree("merged")
        code, result, run = self.run_main({"state": "closed", "merged_at": "2026-10-16T00:00:00Z",
                                           "head": {"ref": "work/merged", "sha": self.git(path, "rev-parse", "HEAD"),
                                                    "repo": {"full_name": "o/sample"}}})
        self.assertEqual(code, 0)
        self.assertEqual([r["decision"] for r in result["worktrees"]], ["retired"])
        self.assertEqual(run.call_args_list, [mock.call(str(path), dry_run=True), mock.call(str(path), dry_run=False)])

    def merged_refresh(self):
        self.worktree("source")
        refresh = self.worktree("refresh")
        pr_head = self.commit(refresh, "Refresh resolution")
        pull = {"state": "closed", "merged_at": "2026-10-23T00:00:00Z",
                "base": {"ref": "main", "repo": {"default_branch": "main"}},
                "head": {"ref": "work/source", "sha": pr_head, "repo": {"full_name": "o/sample"}}}
        return refresh, pull, [pr_head]

    def test_merged_pr_retires_the_refresh_worktree_that_pushed_its_head(self):
        refresh, pull, commits = self.merged_refresh()
        code, result, run = self.run_main(pull, commits=commits)
        self.assertEqual(code, 0)
        self.assertEqual({r["path"]: r["decision"] for r in result["worktrees"]}[str(refresh)], "retired")
        self.assertIn(mock.call(str(refresh), dry_run=False), run.call_args_list)

    def test_open_claim_on_a_closing_issue_keeps_the_refresh_worktree(self):
        refresh, pull, commits = self.merged_refresh()
        claim = {"id": 11, "user": {"login": "bot"}, "created_at": "2026-10-22T00:00:00Z",
                 "body": "Claimed by refresher\n" + github_plan_claim.marker(
                     {"worker": "refresher", "session": "s1", "branch": "work/refresh", "claimed_at": "2026-10-22T00:00:00Z"})}
        code, result, run = self.run_main(pull, commits=commits, discussions={"3": [claim]})
        self.assertEqual(code, 0)
        self.assertEqual({r["path"]: r["decision"] for r in result["worktrees"]}[str(refresh)], "kept")
        self.assertNotIn(mock.call(str(refresh), dry_run=False), run.call_args_list)

    def claimed_source(self, *, released):
        record = {"worker": "author", "session": "s1", "branch": "work/source", "claimed_at": "2026-10-22T00:00:00Z"}
        claim = {"id": 11, "user": {"login": "bot"}, "created_at": "2026-10-22T00:00:00Z",
                 "body": "Claimed by author\n" + github_plan_claim.marker(record)}
        release = {"id": 12, "user": {"login": "bot"}, "created_at": "2026-10-22T01:00:00Z",
                   "body": "Released claim 11\n\nHandoff: ready for the train."}
        status = "## Current Status\n\nNext action: train lands it.\n" + github_plan_claim.marker(record)
        return {"discussions": {"3": [claim, release] if released else [claim]}, "bodies": {"3": status}}

    def test_released_claim_left_in_current_status_does_not_keep_the_worktree(self):
        _, pull, commits = self.merged_refresh()
        code, result, run = self.run_main(pull, commits=commits, **self.claimed_source(released=True))
        source = str(self.root / "sample" / "source")
        self.assertEqual((code, {r["path"]: r["decision"] for r in result["worktrees"]}[source]), (0, "retired"))
        self.assertIn(mock.call(source, dry_run=False), run.call_args_list)

    def test_unreleased_claim_in_current_status_keeps_the_worktree(self):
        _, pull, commits = self.merged_refresh()
        code, result, run = self.run_main(pull, commits=commits, **self.claimed_source(released=False))
        source = str(self.root / "sample" / "source")
        self.assertEqual((code, {r["path"]: r["decision"] for r in result["worktrees"]}[source]), (0, "kept"))
        self.assertNotIn(mock.call(source, dry_run=False), run.call_args_list)

    def test_claim_whose_session_lease_expired_does_not_keep_the_worktree(self):
        _, pull, commits = self.merged_refresh()
        github_plan_lease.renew("s1")
        os.utime(github_plan_lease.lease_path("s1"), (0, 0))
        code, result, run = self.run_main(pull, commits=commits, **self.claimed_source(released=False))
        source = str(self.root / "sample" / "source")
        self.assertEqual({r["path"]: r["decision"] for r in result["worktrees"]}[source], "retired")

    def test_open_claim_on_an_issue_the_pr_refs_keeps_the_refresh_worktree(self):
        refresh, pull, commits = self.merged_refresh()
        claim = {"id": 12, "user": {"login": "bot"}, "created_at": "2026-10-22T00:00:00Z",
                 "body": "Claimed by refresher\n" + github_plan_claim.marker(
                     {"worker": "refresher", "session": "s1", "branch": "work/refresh", "claimed_at": "2026-10-22T00:00:00Z"})}
        code, result, run = self.run_main({**pull, "body": "Refs #5"}, commits=commits, discussions={"5": [claim]})
        self.assertEqual({r["path"]: r["decision"] for r in result["worktrees"]}[str(refresh)], "kept")
        self.assertNotIn(mock.call(str(refresh), dry_run=False), run.call_args_list)

    def test_fast_forward_landing_keeps_worktrees_on_its_commits(self):
        refresh, pull, commits = self.merged_refresh()
        code, result, run = self.run_main({**pull, "merge_commit_sha": commits[-1]}, commits=commits)
        self.assertNotIn(str(refresh), [r["path"] for r in result["worktrees"]])
        self.assertNotIn(mock.call(str(refresh), dry_run=False), run.call_args_list)

    def test_unreadable_claims_retire_nothing(self):
        _, pull, commits = self.merged_refresh()
        code, result, run = self.run_main(pull, commits=commits, discussions={"3": ValueError("Malformed claim marker")})
        self.assertEqual((code, result["ok"]), (2, False))
        run.assert_not_called()

    def test_checkout_of_another_repository_retires_nothing(self):
        # A sibling repository with the same branch name must not lose its worktree to this PR.
        path = self.worktree("shared")
        code, result, run = self.run_main({"state": "closed", "merged_at": "2026-10-16T00:00:00Z",
                                           "head": {"ref": "work/shared", "sha": self.git(path, "rev-parse", "HEAD"),
                                                    "repo": {"full_name": "o/other"}}}, repo="o/other")
        self.assertEqual((code, result["ok"]), (2, False))
        run.assert_not_called()

    def test_fork_head_retires_nothing(self):
        path = self.worktree("fork")
        code, result, run = self.run_main({"state": "closed", "head": {"ref": "work/fork", "sha": self.git(path, "rev-parse", "HEAD"),
                                                                        "repo": None}})
        self.assertEqual((code, result["worktrees"]), (0, []))
        run.assert_not_called()


class FinishedScanTests(Fixture):
    def test_scan_reports_landed_worktrees_and_reads_github_only_when_needed(self):
        self.worktree("untouched")
        merged = self.worktree("merged")
        merged_head = self.commit(merged, "Merged work")
        opened = self.worktree("open")
        self.commit(opened, "Open work")
        self.worktree("held", lock="Held by another job")
        unread = self.worktree("unread")
        self.commit(unread, "Unread work")
        # The scan reads only local refs; the URL names the repository its PR reads use.
        self.git(self.repo, "remote", "set-url", "origin", "https://github.com/o/sample.git")
        reads = []

        def pulls_for(_slug, branch):
            reads.append(branch)
            if branch == "work/unread":
                raise LookupError("request_budget_exhausted")
            if branch == "work/merged":
                return [{"state": "closed", "merged_at": "2026-10-15T00:00:00Z", "closed_at": "2026-10-15T00:00:00Z",
                         "number": 4, "html_url": "https://github.com/o/sample/pull/4", "head": {"sha": merged_head}}]
            return [{"state": "open", "head": {"sha": "0" * 40}}]

        found, gaps = landed_worktrees.finished(self.root, pulls_for)
        self.assertEqual(sorted((r["task"], r["basis"]) for r in found),
                         [("merged", "PR merged"), ("untouched", "in default branch")])
        self.assertEqual(sorted(reads), ["work/merged", "work/open", "work/unread"])
        self.assertEqual([g["reason"] for g in gaps], ["request_budget_exhausted"])
        self.assertIsNotNone(next(r for r in found if r["task"] == "untouched")["since"])
        reads.clear()
        self.assertEqual(landed_worktrees.finished(self.root, pulls_for, include=lambda slug: slug != "o/sample"), ([], []))
        self.assertEqual(reads, [])


if __name__ == "__main__":
    unittest.main()
