#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline close gate over unanswered outside comments."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, cast

os.environ["CODEX_SKILLS_ENV_FILE"] = "/definitely/missing/codex-skills-test.env"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import github_close_attention as gate
from github_api import ApiResult
from github_read import GitHubReader, GitHubReadError

REPO = "director/project"
ISSUE_URL = f"https://github.com/{REPO}/issues/7"


class FakeReader:
    def __init__(self, *, issue: dict[str, Any] | None = None, pages: dict[str, list[dict[str, Any]]] | None = None,
                 objects: dict[str, dict[str, Any]] | None = None, missing: tuple[str, ...] = ()) -> None:
        self.issue = issue or {"state": "open", "body": ""}
        self.pages = pages or {}
        self.objects = objects or {}
        self.missing = missing

    def get_json(self, path: str, **_kwargs: Any) -> dict[str, Any]:
        if path in self.missing:
            raise GitHubReadError("Not Found", result=ApiResult(ok=False, status=404, body=None), diagnostics={})
        if path == f"repos/{REPO}/issues/7":
            return self.issue
        return self.objects.get(path, {"state": "open"})

    def paged_json(self, path: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return self.pages.get(path, [])


def fake_reader(**kwargs: Any) -> GitHubReader:
    return cast(GitHubReader, cast(object, FakeReader(**kwargs)))


def row(number: int = 7, *, author: str = "client", author_class: str = "person", addressed: bool = False,
        later_reply: str | None = None, created: str = "2026-10-17T17:33:49Z") -> dict[str, Any]:
    return {"repo": REPO, "number": number, "author": author, "author_class": author_class,
            "comment_url": f"https://github.com/{REPO}/issues/{number}#issuecomment-{number}00",
            "created_at": created, "updated_at": created, "body_excerpt": "Can I send a corrected sheet?",
            "publicly_addressed": addressed, "later_reply_url": later_reply}


def radar(*rows: dict[str, Any], status: str = "attention", seen: list[list[str]] | None = None,
          handled: tuple[dict[str, Any], ...] = ()):
    def run(threads: list[str], _gh_cmd: str | None = None) -> dict[str, Any]:
        if seen is not None:
            seen.append(threads)
        return {"status": status, "attention": list(rows), "handled": list(handled), "coverage": {"errors": []}}
    return run


def degraded_thread(thread: str):
    repo, number = thread.split("#")
    error = {"repo": repo, "lane": "thread",
             "error": f"GitHub command failed: gh api repos/{repo}/issues/{number}\nHTTP 404"}
    output = json.dumps({"status": "degraded", "attention": [], "handled": [], "coverage": {"errors": [error]}})

    def run(threads: list[str], gh_cmd: str | None = None) -> dict[str, Any]:
        return gate.run_radar(threads, gh_cmd, lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 3, output, ""))
    return run


class CloseAttentionTests(unittest.TestCase):
    def test_refuses_over_an_unanswered_comment_on_the_issue(self) -> None:
        with self.assertRaises(gate.CloseAttentionError) as refused:
            gate.check(REPO, 7, reader=fake_reader(), radar=radar(row()))
        self.assertEqual(refused.exception.cause, "unanswered_comments")
        self.assertIn(row()["comment_url"], str(refused.exception))
        self.assertIn("Can I send a corrected sheet?", str(refused.exception))

    def test_refuses_over_an_unanswered_comment_on_a_linked_issue(self) -> None:
        reader = fake_reader(
            issue={"state": "open", "body": "Builds on #9 and https://github.com/other/repo/issues/3.",
                   "parent_issue_url": f"https://api.github.com/repos/{REPO}/issues/2"},
            pages={f"repos/{REPO}/issues/7/sub_issues": [{"html_url": f"https://github.com/{REPO}/issues/10"}],
                   f"repos/{REPO}/issues/7/dependencies/blocked_by": [
                       {"html_url": f"https://github.com/{REPO}/issues/9"},
                       {"html_url": "https://github.com/client/product/issues/4"}]})
        seen: list[list[str]] = []
        with self.assertRaises(gate.CloseAttentionError) as refused:
            gate.check(REPO, 7, reader=reader, radar=radar(row(9), seen=seen))
        # Native links count in any account; body references only in this one (other/repo#3 is left out).
        self.assertEqual(seen, [[f"{REPO}#7", f"{REPO}#9", "client/product#4", f"{REPO}#10", f"{REPO}#2"]])
        self.assertIn(f"client on {REPO}#9", str(refused.exception))

    def test_closing_a_linked_thread_does_not_answer_its_comment(self) -> None:
        reader = fake_reader(pages={f"repos/{REPO}/issues/7/sub_issues": [{"html_url": f"https://github.com/{REPO}/issues/9"}]})
        with self.assertRaises(gate.CloseAttentionError) as refused:
            gate.check(REPO, 7, reader=reader, radar=radar(status="clear", handled=(row(9),)))
        self.assertIn(f"client on {REPO}#9", str(refused.exception))
        answered = gate.check(REPO, 7, reader=reader, radar=radar(status="clear", handled=(
            row(9, addressed=True, later_reply=f"https://github.com/{REPO}/issues/9#issuecomment-901"),)))
        self.assertEqual(answered["result"], "passed")

    def test_replied_and_automation_comments_pass(self) -> None:
        result = gate.check(REPO, 7, reader=fake_reader(), radar=radar(
            row(later_reply="https://github.com/director/project/issues/7#issuecomment-800"),
            row(author="deploy-bot", author_class="automation")))
        self.assertEqual(result["result"], "passed")

    def test_a_targeted_response_without_a_later_posted_reply_refuses(self) -> None:
        # The radar's targeted response follows edit time, so an edited older comment can be one.
        with self.assertRaises(gate.CloseAttentionError):
            gate.check(REPO, 7, reader=fake_reader(), radar=radar(row(addressed=True)))

    def test_incomplete_coverage_refuses(self) -> None:
        def degraded(_threads: list[str], _gh_cmd: str | None = None) -> dict[str, Any]:
            return {"status": "degraded", "attention": [],
                    "coverage": {"errors": [{"repo": REPO, "lane": "issue_comments", "error": "HTTP 502"}]}}
        with self.assertRaises(gate.CloseAttentionError) as refused:
            gate.check(REPO, 7, reader=fake_reader(), radar=degraded)
        self.assertEqual(refused.exception.cause, "coverage_unknown")
        self.assertIn("HTTP 502", str(refused.exception))

    def test_missing_body_reference_refuses_with_its_fix_until_corrected(self) -> None:
        missing = fake_reader(issue={"state": "open", "body": "Done in #471 and #9."},
                             missing=(f"repos/{REPO}/issues/471",))
        with self.assertRaises(gate.CloseAttentionError) as refused:
            gate.check(REPO, 7, reader=missing, radar=degraded_thread(f"{REPO}#471"))
        self.assertEqual(refused.exception.cause, "missing_references")
        self.assertEqual(refused.exception.payload["missing_references"], [f"{REPO}#471"])
        self.assertIn("#471", str(refused.exception))
        self.assertNotIn(f"{REPO}#9", str(refused.exception))
        self.assertNotIn("--unanswered-decision", str(refused.exception))
        corrected = fake_reader(issue={"state": "open", "body": "Done in director/other#471 and #9."})
        self.assertEqual(gate.check(REPO, 7, reader=corrected, radar=radar(status="clear"))["result"], "passed")

    def test_missing_reference_keeps_other_blockers_and_their_route(self) -> None:
        missing = fake_reader(issue={"state": "open", "body": "Done in #471 and #9."},
                              missing=(f"repos/{REPO}/issues/471",))
        unreadable = degraded_thread(f"{REPO}#471")

        def blocked(threads: list[str], gh_cmd: str | None = None) -> dict[str, Any]:
            payload = unreadable(threads, gh_cmd)
            payload["attention"] = [row(9)]
            payload["coverage"]["errors"].append(
                {"repo": REPO, "lane": "issue_comments", "error": f"gh api repos/{REPO}/issues/9/comments: HTTP 502"})
            return payload
        with self.assertRaises(gate.CloseAttentionError) as refused:
            gate.check(REPO, 7, reader=missing, radar=blocked)
        self.assertEqual(refused.exception.cause, "unanswered_comments")
        self.assertEqual(refused.exception.payload["missing_references"], [f"{REPO}#471"])
        self.assertIn(f"client on {REPO}#9", str(refused.exception))
        self.assertIn("HTTP 502", str(refused.exception))
        self.assertIn("--unanswered-decision", str(refused.exception))

    def test_existing_unreadable_thread_names_it_and_keeps_the_director_route(self) -> None:
        reader = fake_reader(issue={"state": "open", "body": "Done in #9."})
        with self.assertRaises(gate.CloseAttentionError) as refused:
            gate.check(REPO, 7, reader=reader, radar=degraded_thread(f"{REPO}#9"))
        self.assertEqual(refused.exception.cause, "coverage_unknown")
        self.assertIn(f"repos/{REPO}/issues/9", str(refused.exception))
        self.assertIn("--unanswered-decision", str(refused.exception))

    def test_closed_issue_is_not_checked(self) -> None:
        def never(_threads: list[str], _gh_cmd: str | None = None) -> dict[str, Any]:
            raise AssertionError("an already-closed issue needs no comment check")
        result = gate.check(REPO, 7, issue={"state": "closed"}, radar=never)
        self.assertEqual(result["result"], "skipped")

    @staticmethod
    def decide(comment: dict[str, Any], reactions: list[dict[str, Any]] | None = None,
               url: str = f"{ISSUE_URL}#issuecomment-55") -> dict[str, Any]:
        reader = fake_reader(objects={f"repos/{REPO}/issues/comments/55": comment},
                            pages={f"repos/{REPO}/issues/comments/55/reactions": reactions or []})
        return gate.check(REPO, 7, reader=reader, radar=radar(row()), decision_url=url)

    def test_only_a_later_director_comment_or_approved_record_overrides(self) -> None:
        after, before = "2026-10-17T18:00:00Z", "2026-10-17T17:00:00Z"
        on_issue = f"https://api.github.com/repos/{REPO}/issues/7"
        director = {"user": {"login": "director"}, "body": "Close it.", "created_at": after, "updated_at": after,
                    "issue_url": on_issue}
        record = {"user": {"login": "agent[bot]"}, "body": "Director decision: close #7 now.",
                  "created_at": after, "updated_at": after, "issue_url": on_issue}
        thumbs = [{"content": "+1", "user": {"login": "director"}, "created_at": "2026-10-17T18:05:00Z"}]
        self.assertEqual(self.decide(director)["decision"]["approval"], "director_comment")
        self.assertEqual(self.decide(record, thumbs)["decision"]["approval"], "director_reaction")
        refused = {
            "predates the comment": ({**director, "created_at": before, "updated_at": before}, None),
            "agent record without the Director's approval": (record, None),
            "edited record": ({**record, "updated_at": "2026-10-17T18:10:00Z"}, thumbs),
            "someone else's thumbs-up": (record, [{**thumbs[0], "user": {"login": "agent[bot]"}}]),
            "the Director's comment on another issue": ({**director, "issue_url": on_issue[:-1] + "8"}, None),
        }
        for case, (comment, reactions) in refused.items():
            with self.subTest(case), self.assertRaises(gate.CloseAttentionError):
                self.decide(comment, reactions)
        with self.assertRaises(gate.CloseAttentionError):
            self.decide(director, url=f"https://github.com/{REPO}/issues/8#issuecomment-55")

    def test_radar_failure_is_degraded_coverage(self) -> None:
        commands: list[list[str]] = []
        routes: list[str] = []

        def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            commands.append(command)
            routes.append(kwargs["env"]["GITHUB_UNANSWERED_COMMENTS_GH"])
            return subprocess.CompletedProcess(command, 3, '{"status": "degraded", "error": "auth"}', "")
        payload = gate.run_radar([f"{REPO}#7", f"{REPO}#9"], "fake-gh", run)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(commands[0][-4:], ["--thread", f"{REPO}#7", "--thread", f"{REPO}#9"])
        self.assertIn("--include-handled", commands[0])
        self.assertIn("--no-cutoff", commands[0])
        self.assertEqual(commands[0][commands[0].index("--director-login") + 1], "director")
        self.assertEqual(routes, ["fake-gh"])


if __name__ == "__main__":
    unittest.main()
