#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise discovery, exact bindings and refusal-to-publish with synthetic PRs and real Git."""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pending_reviews as reviews
from github_plan_claim import marker

REPO = "example/tool"
BOT = "review-bot[bot]"
TRUSTED = {BOT}
SHA = "a" * 40
BRANCH = "work/change"
SESSION = "native-session"
STAMP = "2026-10-17T20:00:00Z"


def comment(ident, body, login=BOT):
    return {"id": ident, "body": body, "user": {"login": login}, "created_at": STAMP,
            "updated_at": STAMP, "issue_url": f"https://api.github.com/repos/{REPO}/issues/7"}


def ask():
    return comment(10, f"Supervisor runs the review.\nHead: {SHA}\n\nPrompt:\n\n```text\n"
                      "Review the changed helper. Give a realistic trigger and severity, or none.\n```\n")


def pull():
    return {"number": 7, "state": "open", "head": {"sha": SHA, "ref": BRANCH, "repo": {"full_name": REPO}}}


def entry():
    return {"session_id": SESSION, "repository": REPO, "issue": 3,
            "supervisor_owned": True, "harness": "codex"}


class FakeIO:
    def __init__(self):
        self.pr = pull()
        self.request = ask()
        self.comments = [self.request]
        self.sources = {10: self.request}
        self.claims = [comment(5, marker({"branch": BRANCH, "session": SESSION, "worker": "worker",
                                        "claimed_at": STAMP}))]
        self.calls = []
        self.after_review = lambda: None
        self.review_error = None
        self.review_text = "No blocking findings."
        self.extra_pulls = []

    def get_json(self, path, **kwargs):
        if "/issues/comments/" in path:
            return copy.deepcopy(self.sources[int(path.rsplit("/", 1)[1])])
        return copy.deepcopy(self.pr)

    def paged_json(self, path, **kwargs):
        self.calls.append(("read", path, kwargs))
        if kwargs["step_prefix"] == "author_claims":
            return copy.deepcopy(self.claims)
        if path.endswith("/pulls"):
            return [self.pr, *self.extra_pulls]
        return copy.deepcopy(self.comments)

    def review(self, author, worktree, prompt, output):
        self.calls.append(("review", author, prompt.read_text()))
        if self.review_error:
            raise self.review_error
        output.write_text(self.review_text)
        self.after_review()
        return {"ok": True, "provider": "anthropic" if author == "openai" else "openai",
                "model": "test-model", "model_source": "reported by the CLI"}

    def post(self, repo, number, body):
        self.calls.append(("post", repo, number, body.read_text()))
        self.comments.append(comment(11, body.read_text()))
        return {"ok": True, "outcome_certainty": "confirmed"}


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evidence = Path(self.temp.name) / "evidence"
        self.io = FakeIO()
        self.entries = [entry()]
        self.find = patch.object(reviews, "find_worktree", return_value=Path(self.temp.name))
        self.check = patch.object(reviews, "check_worktree")
        git_patch = patch.object(reviews, "git", return_value=BRANCH)
        git_patch.start()
        self.addCleanup(git_patch.stop)
        self.find.start()
        self.check.start()
        self.addCleanup(self.find.stop)
        self.addCleanup(self.check.stop)

    def dispatch(self, apply=True):
        return reviews.dispatch(self.io, REPO, 7, self.entries, Path(self.temp.name), TRUSTED, self.evidence, apply)

    def assert_no_post(self):
        self.assertFalse(any(c[0] == "post" for c in self.io.calls))

    def test_discovery_is_read_only_and_apply_reports_author_then_deduplicates(self):
        report = self.dispatch(apply=False)
        self.assertEqual(report["status"], "pending")
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))
        report = self.dispatch()
        self.assertEqual(report["author_session"], SESSION)
        self.assertEqual(report["status"], "posted")
        self.assertEqual(self.dispatch()["status"], "already_completed")
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)
        body = next(c[3] for c in self.io.calls if c[0] == "post")
        self.assertIsNone(reviews.review_receipt_head(body))
        self.assertEqual(self.evidence.joinpath(next(p.name for p in self.evidence.glob("*.md"))).read_text().strip(), self.io.review_text)

    def test_other_provider_from_author_harness(self):
        self.entries[0]["harness"] = "claude"
        self.dispatch()
        self.assertEqual(next(c[1] for c in self.io.calls if c[0] == "review"), "anthropic")

    def test_request_for_previous_head_never_launches(self):
        self.io.pr["head"]["sha"] = "b" * 40
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_head_moves_while_binding_session_refuses_before_review(self):
        def find(*args):
            self.io.pr["head"]["sha"] = "b" * 40
            return Path(self.temp.name)
        with patch.object(reviews, "find_worktree", side_effect=find), self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))
        self.assert_no_post()

    def test_moved_head_during_review_never_posts(self):
        self.io.after_review = lambda: self.io.pr["head"].update(sha="b" * 40)
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assert_no_post()

    def test_closed_pr_during_review_never_posts(self):
        self.io.after_review = lambda: self.io.pr.update(state="closed")
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assert_no_post()

    def test_dirty_worktree_during_review_never_posts(self):
        with patch.object(reviews, "check_worktree", side_effect=reviews.ReviewError("dirty")):
            with self.assertRaises(reviews.ReviewError):
                self.dispatch()
        self.assert_no_post()

    def test_failed_or_empty_review_never_posts(self):
        for error, text in [(reviews.ReviewError("permission denied"), ""), (None, " \n")]:
            with self.subTest(error=error):
                self.io.review_error, self.io.review_text = error, text
                self.io.request["updated_at"] = str(error) + text
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
                self.assert_no_post()

    def test_failed_or_interrupted_launch_is_not_replayed_on_next_sweep(self):
        self.io.review_error = reviews.ReviewError("permission denied")
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.io.review_error = None
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)
        self.assert_no_post()

    def test_unknown_post_is_not_replayed_on_next_sweep(self):
        self.io.post = Mock(side_effect=reviews.ReviewError("unknown write"))
        for _ in range(2):
            with self.assertRaises(reviews.ReviewError):
                self.dispatch()
        self.assertEqual(self.io.post.call_count, 1)
        self.assertEqual(sum(c[0] == "review" for c in self.io.calls), 1)
        self.assertEqual(next(self.evidence.glob("*.md")).read_text().strip(), self.io.review_text)

    def test_missing_prompt_or_head_refuses_before_launch(self):
        for body in [f"Supervisor runs the review.\nHead: {SHA}", "Supervisor runs the review.\nPrompt:\nRead the diff."]:
            with self.subTest(body=body):
                self.io.request["body"] = body
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
                self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_follows_linked_prompt_and_checks_it_again_before_post(self):
        self.io.request["body"] = (f"Supervisor runs the review.\nHead: {SHA}\n"
                                   f"https://github.com/{REPO}/pull/7#issuecomment-9")
        self.io.sources[9] = comment(9, "Prompt:\nInspect helpers and surrounding files.")
        self.dispatch()
        self.assertIn("Inspect helpers and surrounding files.", next(c[2] for c in self.io.calls if c[0] == "review"))

    def test_linked_prompt_edited_during_review_never_posts(self):
        self.io.request["body"] = (f"Supervisor runs the review.\nHead: {SHA}\n"
                                   f"https://github.com/{REPO}/pull/7#issuecomment-9")
        self.io.sources[9] = comment(9, "Prompt:\nInspect helpers.")
        self.io.after_review = lambda: self.io.sources[9].update(body="Prompt:\nDifferent scope.")
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assert_no_post()

    def test_cycle_cross_repo_link_and_untrusted_link_refuse(self):
        self.io.request["body"] = f"Supervisor runs the review.\nHead: {SHA}\nhttps://github.com/{REPO}/pull/7#issuecomment-9"
        for body, login in [(f"https://github.com/{REPO}/pull/7#issuecomment-10", BOT),
                            ("https://github.com/other/repo/issues/1#issuecomment-10", BOT),
                            ("Prompt:\nInspect helpers.", "stranger")]:
            with self.subTest(body=body, login=login):
                self.io.sources[9] = comment(9, body, login)
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
                self.assert_no_post()

    def test_unknown_and_ambiguous_author_session_never_guesses(self):
        original = copy.deepcopy(self.io.claims)
        for claims in [[], original + [comment(6, marker({"branch": BRANCH, "session": "second",
                                                          "worker": "other", "claimed_at": STAMP}))]]:
            with self.subTest(claims=claims):
                self.io.claims = claims
                self.entries = [entry(), {**entry(), "session_id": "second"}]
                with self.assertRaises(reviews.ReviewError):
                    self.dispatch()
                self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_new_issue_under_brief_parent_can_supply_author_claim(self):
        self.io.pr["body"] = "Closes #99"
        claims = self.io.paged_json
        def pages(path, **kwargs):
            if kwargs.get("step_prefix") == "author_claims" and not path.endswith("/99/comments"):
                return []
            return claims(path, **kwargs)
        self.io.paged_json = pages
        self.assertEqual(self.dispatch()["author_session"], SESSION)

    def test_successor_worktree_branch_claim_binds_to_original_pr_head(self):
        successor = "work/successor"
        self.io.claims = [comment(5, marker({"branch": successor, "session": SESSION,
                                            "worker": "worker", "claimed_at": STAMP}))]
        with patch.object(reviews, "git", return_value=successor):
            self.assertEqual(self.dispatch()["author_session"], SESSION)

    def test_new_request_during_review_never_posts(self):
        self.io.after_review = lambda: self.io.comments.append(comment(12, self.io.request["body"]))
        with self.assertRaises(reviews.ReviewError):
            self.dispatch()
        self.assert_no_post()

    def test_untrusted_request_does_not_trigger_inference(self):
        self.io.request["user"]["login"] = "stranger"
        self.assertEqual(self.dispatch()["status"], "no_request")
        self.assertFalse(any(c[0] == "review" for c in self.io.calls))

    def test_all_run_repositories_discovered_one_failure_does_not_hide_others(self):
        entries = [entry(), {**entry(), "repository": "example/another", "session_id": "other"}]
        reports = reviews.sweep(self.io, entries, {REPO: Path(self.temp.name)}, TRUSTED, self.evidence)
        paths = {c[1] for c in self.io.calls if c[0] == "read"}
        self.assertIn(f"/repos/{REPO}/pulls", paths)
        self.assertIn("/repos/example/another/pulls", paths)
        self.assertTrue(any(r["status"] == "pending" for r in reports))
        self.assertTrue(any(r["status"] == "refused" for r in reports))


class WorktreeTests(unittest.TestCase):
    def test_real_git_worktree_binding_includes_untracked_and_branch_head_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            checkout = root / "repo"
            def git(*args):
                result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                                        env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()
            git("init", "-b", "main", str(checkout))
            git("-C", str(checkout), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "--allow-empty", "-m", "fixture")
            git("-C", str(checkout), "remote", "add", "origin", f"https://github.com/{REPO}.git")
            path = root / "task"
            git("-C", str(checkout), "worktree", "add", "-b", BRANCH, str(path))
            head = git("-C", str(path), "rev-parse", "HEAD")
            git("-C", str(checkout), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "--allow-empty", "-m", "advance main")
            self.assertEqual(reviews.find_worktree(checkout, REPO, BRANCH, head), path.resolve())
            for repo, branch, sha in [("wrong/repo", BRANCH, head), (REPO, BRANCH, SHA)]:
                with self.subTest(repo=repo, branch=branch, sha=sha), self.assertRaises(reviews.ReviewError):
                    reviews.find_worktree(checkout, repo, branch, sha)
            self.assertEqual(reviews.find_worktree(checkout, REPO, "work/previous", head), path.resolve())
            second = root / "second"
            git("-C", str(checkout), "worktree", "add", "-b", "work/second", str(second), head)
            with self.assertRaises(reviews.ReviewError):
                reviews.find_worktree(checkout, REPO, "work/previous", head)
            self.assertEqual(reviews.find_worktree(checkout, REPO, "work/previous", head, second), second.resolve())
            (path / "untracked.txt").write_text("not committed")
            with self.assertRaises(reviews.ReviewError):
                reviews.find_worktree(checkout, REPO, BRANCH, head)


class LedgerTests(unittest.TestCase):
    def test_reviews_need_identity_not_a_completed_transcript(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.json"
            path.write_text(json.dumps([entry(), {"supervisor_owned": False}]))
            self.assertEqual(reviews.review_entries(path), [entry()])
            for value in [[entry(), entry()], [{**entry(), "session_id": ""}],
                          [{**entry(), "repository": "invalid"}], [{**entry(), "harness": "unknown"}], {}]:
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    with self.assertRaises(reviews.ReviewError):
                        reviews.review_entries(path)


class AdapterTests(unittest.TestCase):
    def test_failed_refused_incomplete_and_empty_outputs_never_succeed(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "review.md"
            prompt = Path(folder) / "prompt.md"
            prompt.write_text("Inspect source")
            io = reviews.IO.__new__(reviews.IO)
            valid = {"ok": True, "provider": "anthropic", "response_file": str(output)}
            for changes, code, text in [({"ok": False, "error": "denied"}, 1, "partial"),
                                        ({"outcome": "refused"}, 0, "partial"),
                                        ({"outcome": "incomplete"}, 0, "partial"),
                                        ({}, 0, " "), ({"provider": "openai"}, 0, "review")]:
                with self.subTest(changes=changes, code=code, text=text):
                    output.write_text(text)
                    result = subprocess.CompletedProcess([], code, json.dumps({**valid, **changes}), "")
                    with patch.object(reviews, "run", return_value=result), self.assertRaises(reviews.ReviewError):
                        io.review("openai", Path(folder), prompt, output)

    def test_provider_selection_passes_actual_author_and_other_provider_to_policy_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "result.md"
            output.write_text("none")
            io = reviews.IO.__new__(reviews.IO)
            for author, reviewer in [("openai", "anthropic"), ("anthropic", "openai")]:
                result = subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "provider": reviewer,
                                                                       "response_file": str(output)}), "")
                with patch.object(reviews, "run", return_value=result) as run:
                    io.review(author, root, root / "prompt.md", output)
                    command = run.call_args.args[0]
                    self.assertEqual(command[command.index("--author-provider") + 1], author)
                    self.assertEqual(command[command.index("--provider") + 1], reviewer)

    def test_post_unknown_does_not_replay(self):
        io = reviews.IO.__new__(reviews.IO)
        result = subprocess.CompletedProcess([], 1, '{"ok": false, "outcome_certainty": "unknown"}', "")
        with patch.object(reviews, "run", return_value=result) as run, self.assertRaises(reviews.ReviewError):
            io.post(REPO, 7, Path("/fixture/comment.md"))
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
