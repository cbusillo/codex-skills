#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Offline notification filtering and failure-safe watermark tests."""

import json
import argparse
from contextlib import redirect_stdout
from datetime import datetime
import io
from pathlib import Path
import tempfile
import tomllib
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

import foreign_watch
from github_api import ApiResult, FailureDetail
from iterm_tab import validate_brief_rules

SINCE = "2026-10-09T12:00:00Z"
STARTED = "2026-10-09T12:05:00Z"
REPO = "director/project"


def comment(login="client", body="feedback", updated=SINCE):
    return {"user": {"login": login}, "body": body, "updated_at": updated,
            "html_url": "https://github.com/director/project/issues/1#issuecomment-2"}


def opened(login="client", kind="User", created=SINCE):
    return {"user": {"login": login, "type": kind}, "title": "Add tvOS support", "body": None,
            "created_at": created, "updated_at": STARTED,
            "html_url": "https://github.com/director/project/pull/50"}


def feeds(*comment_pages, opened_items=(), repositories=()):
    pages = iter(comment_pages)

    def read(path, **_):
        if path == "/installation/repositories":
            return list(repositories)
        if not path.endswith("/issues/comments"):
            return list(opened_items)
        page = next(pages)
        if isinstance(page, Exception):
            raise page
        return page
    return read


class WatchTests(unittest.TestCase):
    def test_persistent_read_retry_preserves_watermark_and_provider_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [0.0]
            reader = Mock()
            reader.paged_json.side_effect = feeds(foreign_watch.GitHubReadError(
                "provider unavailable", result=ApiResult(ok=False, status=503, body=None,
                    failure=FailureDetail(cause="network_provider_failure", message="unavailable",
                                          retryable=True, fallback_eligible=False, disposition="retry")),
                diagnostics={"requests": [{"retryAt": 20}]},
            ), [comment()], foreign_watch.GitHubReadShapeError("terminal"))
            args = argparse.Namespace(state=Path(directory) / "state.json", since=None,
                own_login=["director"], launchplane_login=[], interval=5, once=False, persistent=True)

            def sleep(seconds):
                now[0] += seconds

            with (patch.object(foreign_watch, "GitHubReader", return_value=reader),
                  patch.object(foreign_watch.time, "monotonic", side_effect=lambda: now[0]),
                  patch.object(foreign_watch.time, "time", side_effect=lambda: now[0]),
                  patch.object(foreign_watch.time, "sleep", side_effect=sleep) as sleeps,
                  patch.object(foreign_watch, "utc_now", return_value=STARTED),
                  redirect_stdout(io.StringIO())):
                self.assertEqual(foreign_watch.watch(args, [REPO], SINCE, 100), 1)
            self.assertEqual(sleeps.call_args_list[0].args, (20,))
            self.assertEqual(reader.paged_json.call_args_list[0].kwargs["params"],
                             reader.paged_json.call_args_list[1].kwargs["params"])
            self.assertEqual(json.loads(args.state.read_text())["watermarks"][REPO], STARTED)

    def scan(self, reader):
        return foreign_watch.scan([REPO], {REPO: SINCE}, ["director", "agent[bot]"],
                                  ["delivery[bot]"], STARTED, reader)

    def test_filter_and_review_records(self):
        reader = Mock()
        reader.paged_json.side_effect = feeds([comment("Director"), comment("agent[bot]"),
            comment(), comment("delivery[bot]"),
            comment("agent[bot]", "<!-- launchplane:product-review:decision-1 -->")])
        notices, errors, watermarks = self.scan(reader)
        self.assertEqual([n["kind"] for n in notices], ["foreign"] + ["launchplane"] * 2)
        self.assertTrue(all(n["untrusted"] and "body" not in n for n in notices))
        self.assertEqual(errors, [])
        self.assertEqual(watermarks[REPO], STARTED)
        for call in reader.paged_json.call_args_list:
            self.assertLess(foreign_watch.timestamp(call.kwargs["params"]["since"]), foreign_watch.timestamp(SINCE))

    def test_issue_or_pull_request_opened_by_an_outside_person_is_a_notice(self):
        reader = Mock()
        reader.paged_json.side_effect = feeds([], opened_items=[
            opened(), opened("director"), opened("dependabot[bot]", "Bot"), opened(created="2026-10-09T11:00:00Z")])
        notices, errors, _ = self.scan(reader)
        self.assertEqual(errors, [])
        self.assertEqual([(n["author"], n["url"], n["excerpt"]) for n in notices],
                         [("client", "https://github.com/director/project/pull/50", "Add tvOS support")])

    def test_worker_questions_surface_on_the_next_scan_without_ordinary_own_posts(self) -> None:
        reader = Mock()
        bodies = ["Owner question: Q906. Approve the recovery?",
                  "  **Director question:** Q918. Accept again?"]
        reader.paged_json.side_effect = feeds([
            *(comment("AGENT[bot]", body, STARTED) for body in bodies),
            comment("agent[bot]", "Progress: Owner question: was answered.", STARTED),
            comment("agent[bot]", "Director decision: Q905 approved.", STARTED),
            comment("agent[bot]", bodies[0], "2026-10-09T11:00:00Z"),
            comment("client", bodies[0], STARTED),
        ])
        notices, errors, watermarks = self.scan(reader)
        self.assertEqual(errors, [])
        self.assertEqual([notice["kind"] for notice in notices],
                         ["director_question", "director_question", "foreign"])
        for notice, body in zip(notices, bodies):
            self.assertEqual(notice["url"], comment()["html_url"])
            self.assertEqual(notice["excerpt"], foreign_watch.excerpt(body))
            self.assertTrue(notice["untrusted"])
        self.assertEqual(watermarks[REPO], STARTED)

    def test_notice_carries_a_bounded_excerpt_of_the_comment(self):
        reader = Mock()
        correction = "If I made a mistake on the csv sheet,\n\ncan I send the corrected one?"
        reader.paged_json.side_effect = feeds([comment(body=correction), comment(body="word " * 200)])
        short, long = self.scan(reader)[0]
        self.assertEqual(short["excerpt"], "If I made a mistake on the csv sheet, can I send the corrected one?")
        self.assertTrue(long["excerpt"].startswith("word word"))
        self.assertLessEqual(len(long["excerpt"]), foreign_watch.EXCERPT_CHARS)

    def test_failure_preserves_only_failed_repository_watermark(self):
        reader = Mock()
        reader.paged_json.side_effect = feeds(foreign_watch.GitHubReadShapeError("partial"), [])
        notices, errors, watermarks = foreign_watch.scan(
            [REPO, "director/healthy"], {REPO: SINCE, "director/healthy": SINCE},
            ["director"], [], STARTED, reader)
        self.assertEqual(notices, [])
        self.assertEqual(errors[0]["repository"], REPO)
        self.assertEqual(watermarks, {REPO: SINCE, "director/healthy": STARTED})

    def test_invalid_comment_retains_watermark_and_discards_partial_notices(self):
        reader = Mock()
        for bad in ({}, {**comment(), "updated_at": "invalid"}, {**comment(), "user": []}):
            with self.subTest(bad=bad):
                reader.paged_json.side_effect = feeds([comment(), bad])
                notices, errors, watermarks = self.scan(reader)
                self.assertEqual(notices, [])
                self.assertTrue(errors)
                self.assertEqual(watermarks[REPO], SINCE)

    def test_old_comments_excluded_and_edits_reported(self):
        reader = Mock()
        reader.paged_json.side_effect = feeds([comment(updated="2026-10-09T11:59:59Z"), comment(updated=STARTED)])
        self.assertEqual(len(self.scan(reader)[0]), 1)

    def test_state_roundtrip_and_corruption_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            self.assertEqual(foreign_watch.load_state(path, [REPO], SINCE), {REPO: SINCE})
            foreign_watch.save_state(path, {REPO: STARTED}, {"https://github.com/client/private/issues/8": STARTED})
            self.assertEqual(foreign_watch.load_state(path, [REPO], SINCE), {REPO: STARTED})
            self.assertEqual(foreign_watch.load_mention_seen(path),
                             {"https://github.com/client/private/issues/8": STARTED})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for data in ([], {"watermarks": []}, {"watermarks": {REPO: "invalid"}}):
                path.write_text(json.dumps(data))
                with self.assertRaises((ValueError, TypeError)):
                    foreign_watch.load_state(path, [REPO], SINCE)

    def test_deleted_author_is_untrusted_and_marker_quote_does_not_reclassify(self):
        reader = Mock()
        reader.paged_json.side_effect = feeds([{**comment(), "user": None},
            comment(body='Quoted: <!-- launchplane:product-review:id -->')])
        notices, errors, watermarks = self.scan(reader)
        self.assertEqual(errors, [])
        self.assertIsNone(notices[0]["author"])
        self.assertEqual([n["kind"] for n in notices], ["foreign", "foreign"])
        self.assertEqual(watermarks[REPO], STARTED)

    def test_quota_retry_time_and_failed_read_retention(self):
        reader = Mock()
        failure = foreign_watch.GitHubReadShapeError("quota")
        failure.diagnostics = {"requests": [{"retryAt": 12345}]}
        reader.paged_json.side_effect = failure
        notices, errors, watermarks = self.scan(reader)
        self.assertEqual(errors[0]["retry_at"], 12345)
        self.assertEqual(watermarks[REPO], SINCE)
        self.assertEqual(notices, [])

    def test_state_file_has_one_active_watcher(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            with foreign_watch.watch_lock(path):
                with self.assertRaises(ValueError):
                    with foreign_watch.watch_lock(path):
                        self.fail("second watcher acquired the same state")
            with foreign_watch.watch_lock(path):
                pass

    def test_once_cli_reads_one_feed_and_persists_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            reader = Mock()
            reader.paged_json.side_effect = feeds([comment()])
            output = io.StringIO()
            with patch.object(foreign_watch.sys, "argv", ["foreign_watch.py", "--owner", "director",
                    "--repo", REPO, "--own-login", "director", "--state", str(path), "--since", SINCE, "--once"]), \
                 patch.object(foreign_watch, "GitHubReader", return_value=reader) as factory, \
                 redirect_stdout(output):
                self.assertEqual(foreign_watch.main(), 0)
            self.assertEqual(len(json.loads(output.getvalue())["notices"]), 1)
            matrix = tomllib.loads((Path(foreign_watch.__file__).resolve().parents[2]
                                   / "github/references/operation-matrix.toml").read_text())
            self.assertIn(factory.call_args.kwargs["operation"], {row["id"] for row in matrix["operations"]})
            self.assertGreater(factory.call_args.kwargs["deadline_at"], 0)
            self.assertEqual(factory.call_args.kwargs["gh_prefix_args"], foreign_watch.automation_only_gh_prefix_args())
            self.assertIn(REPO, json.loads(path.read_text())["watermarks"])

    def test_default_scope_is_the_installations_active_repositories_of_the_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "w.json"
            foreign_watch.save_state(state, {REPO: STARTED, "director/old": STARTED})
            reader = Mock()
            reader.paged_json.side_effect = feeds([], [comment()], repositories=[
                {"full_name": REPO, "archived": False}, {"full_name": "director/new", "archived": False},
                {"full_name": "director/old", "archived": True}, {"full_name": "client/app", "archived": False}])
            output = io.StringIO()
            with patch.object(foreign_watch.sys, "argv", ["foreign_watch.py", "--owner", "director",
                    "--own-login", "director", "--state", str(state), "--since", SINCE, "--once"]), \
                 patch.object(foreign_watch, "GitHubReader", return_value=reader), redirect_stdout(output):
                self.assertEqual(foreign_watch.main(), 0)
            self.assertEqual(json.loads(state.read_text())["watermarks"]["director/old"], STARTED)
            first_since = reader.paged_json.call_args_list[1].kwargs["params"]["since"]
            self.assertLess(foreign_watch.timestamp(first_since), foreign_watch.timestamp(SINCE))
            read = {call.args[0] for call in reader.paged_json.call_args_list}
            self.assertEqual(read, {"/installation/repositories", f"repos/{REPO}/issues/comments", f"repos/{REPO}/issues",
                                    "repos/director/new/issues/comments", "repos/director/new/issues"})
            self.assertEqual([n["repository"] for n in json.loads(output.getvalue())["notices"]], ["director/new"])

    def test_expired_deadline_starts_no_read(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(foreign_watch.time, "monotonic", side_effect=[0, 1801]), \
             patch.object(foreign_watch, "GitHubReader") as factory, patch.object(foreign_watch.sys, "argv", [
                 "foreign_watch.py", "--owner", "director", "--repo", REPO, "--own-login", "director",
                 "--state", str(Path(directory) / "watch.json")]):
            self.assertEqual(foreign_watch.main(), 0)
            factory.assert_not_called()

    def test_narrower_run_preserves_unlisted_watermarks_and_explicit_since_rewinds(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.json"
            foreign_watch.save_state(path, {REPO: STARTED, "director/other": SINCE})
            values = foreign_watch.load_state(path, [REPO], SINCE, rewind=True)
            self.assertEqual(values, {REPO: SINCE, "director/other": SINCE})
            reader = Mock()
            reader.paged_json.return_value = []
            _, _, values = foreign_watch.scan([REPO], values, ["director"], [], STARTED, reader)
            foreign_watch.save_state(path, values)
            self.assertEqual(foreign_watch.load_state(path, ["director/other"], STARTED)["director/other"], SINCE)

    def test_foreign_marker_remains_foreign(self):
        reader = Mock()
        reader.paged_json.side_effect = feeds([comment(body="<!-- launchplane:product-review:fake -->")])
        notices = self.scan(reader)[0]
        self.assertEqual(notices[0]["kind"], "foreign")
        self.assertTrue(notices[0]["review_marker"])



def client_review(decision, record, reason="Labels print sideways"):
    metadata = {"record_id": record, "decision": decision, "repository": REPO, "pull_request_number": 5,
                "head_sha": "a" * 40, "reason": reason}
    body = (f"<!-- launchplane:product-review:{record} -->\n"
            f"<!-- launchplane:owner-review {json.dumps(metadata)} -->\n## Client review from Launchplane")
    return {**comment("delivery[bot]", body),
            "html_url": f"https://github.com/{REPO}/pull/5#issuecomment-{len(record)}"}


class ChangeRequestTests(unittest.TestCase):
    def watch_once(self, state, *comments):
        reader = Mock()
        reader.paged_json.side_effect = feeds(list(comments))
        args = argparse.Namespace(state=state, since=SINCE, own_login=["director"],
                                  launchplane_login=["delivery[bot]"], interval=5, once=True)
        output = io.StringIO()
        with (patch.object(foreign_watch, "GitHubReader", return_value=reader),
              patch.object(foreign_watch, "utc_now", return_value=STARTED), redirect_stdout(output)):
            foreign_watch.watch(args, [REPO], SINCE, foreign_watch.time.monotonic() + 60)
        return [n["change_request"] for n in json.loads(output.getvalue())["notices"] if "change_request" in n]

    def test_new_change_request_dispatches_once_and_accept_never(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "watch.json"
            requested = client_review("changes_requested", "decision-1", "Show ```label``` button")
            accepted = client_review("accepted", "decision-22", "Looks good")
            first = self.watch_once(state, requested, accepted)
            self.assertEqual(self.watch_once(state, requested, accepted), [])
        self.assertEqual([(r["record_id"], r["pr"], r["reviewed_commit"], r["feedback"]) for r in first],
                         [("decision-1", 5, "a" * 40, "Show ```label``` button")])
        brief = first[0]["worker_brief"]
        self.assertIn("````text\nShow ```label``` button\n````", brief)
        validate_brief_rules(brief)

    def test_change_request_from_an_unconfigured_author_has_no_brief(self):
        foreign = {**client_review("changes_requested", "decision-1"), "user": {"login": "client"}}
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.watch_once(Path(directory) / "watch.json", foreign), [])


APPROVAL = "https://github.com/director/meta/issues/7#issuecomment-99"


def mention(repo, number, updated=STARTED):
    return {"repository_url": f"https://api.github.com/repos/{repo}", "number": number,
            "html_url": f"https://github.com/{repo}/issues/{number}", "updated_at": updated,
            "user": {"login": "client"}, "body": "@director please decide"}


def director_get(items):
    def get(endpoint):
        if endpoint == "user":
            return {"login": "director"}
        return {"items": items, "total_count": len(items), "incomplete_results": False}
    return Mock(side_effect=get)


class MentionTests(unittest.TestCase):
    @staticmethod
    def scan(approval=APPROVAL, get=None, watermark=SINCE, seen=None):
        reader = Mock()
        reader.get_json.return_value = {
            "id": 99, "user": {"login": "director"}, "created_at": SINCE,
            "body": "Q100 yes",
            "issue_url": "https://api.github.com/repos/director/meta/issues/7"}
        reader.paged_json.return_value = [{
            "id": 98, "user": {"login": "agent[bot]"}, "created_at": "2026-10-08T00:00:00Z",
            "body": "Owner question: **Q100** — may the watcher search GitHub for mentions "
                    "of you under your own login, unattended?",
            "issue_url": "https://api.github.com/repos/director/meta/issues/7"}]
        args = argparse.Namespace(director_login="director", mention_approval=approval)
        get = get or director_get([mention("client/private", 4), mention(REPO, 1)])
        with patch.object(foreign_watch.director_mentions, "director_get", get):
            result = foreign_watch.scan_mentions(args, [REPO], {foreign_watch.MENTIONS: watermark}, seen or {},
                                                 ["director", "agent[bot]"], STARTED, reader)
        return result, reader, get

    def test_mention_outside_watched_list_is_an_untrusted_notice(self):
        (notices, gaps, errors, watermarks, _), reader, _ = self.scan()
        self.assertEqual((gaps, errors), ([], []))
        self.assertEqual([(n["repository"], n["number"]) for n in notices], [("client/private", 4)])
        self.assertTrue(notices[0]["untrusted"])
        self.assertNotIn("body", notices[0])
        self.assertEqual(watermarks[foreign_watch.MENTIONS], STARTED)
        self.assertEqual(reader.get_json.call_args.args[0], "repos/director/meta/issues/comments/99")

    def test_late_indexed_mention_is_reported_once_across_passes(self):
        # Updated a minute before the watermark but only searchable now.
        late = mention("client/private", 8, updated="2026-10-09T11:59:00Z")
        get = director_get([late])
        (notices, _, _, _, seen), _, _ = self.scan(get=get)
        self.assertEqual([n["number"] for n in notices], [8])
        query = parse_qs(urlparse(get.call_args_list[1].args[0]).query)["q"][0]
        overlap_start = datetime.fromisoformat(SINCE) - foreign_watch.MENTION_OVERLAP
        self.assertIn(f"updated:>={overlap_start:%Y-%m-%dT%H:%M:%SZ}", query)
        (again, _, _, _, _), _, _ = self.scan(get=director_get([late]), watermark=STARTED, seen=seen)
        self.assertEqual(again, [])
        edited = mention("client/private", 8, updated="2026-10-09T12:04:00Z")
        (changed, _, _, _, _), _, _ = self.scan(get=director_get([edited]), watermark=STARTED, seen=seen)
        self.assertEqual([n["number"] for n in changed], [8])

    def test_unapproved_mentions_report_gap_without_searching(self):
        (notices, gaps, errors, watermarks, _), _, get = self.scan(approval=None)
        self.assertEqual((notices, errors), ([], []))
        self.assertEqual([gap["lane"] for gap in gaps], [foreign_watch.MENTIONS])
        get.assert_not_called()
        self.assertEqual(watermarks[foreign_watch.MENTIONS], SINCE)

    def test_search_failure_is_retryable_and_keeps_watermark(self):
        def rate_limited(endpoint):
            if endpoint == "user":
                return {"login": "director"}
            raise foreign_watch.director_mentions.MentionSearchError("rate limited")
        (notices, gaps, errors, watermarks, _), _, _ = self.scan(get=Mock(side_effect=rate_limited))
        self.assertEqual((notices, gaps), ([], []))
        self.assertTrue(errors[0]["retryable"])
        self.assertEqual(watermarks[foreign_watch.MENTIONS], SINCE)

    def test_cli_without_director_login_prints_the_gap_and_keeps_watching(self):
        with tempfile.TemporaryDirectory() as directory:
            reader = Mock()
            reader.paged_json.return_value = []
            output = io.StringIO()
            with patch.object(foreign_watch.sys, "argv", ["foreign_watch.py", "--owner", "director",
                    "--repo", REPO, "--own-login", "director", "--state", str(Path(directory) / "w.json"),
                    "--since", SINCE, "--once"]), \
                 patch.object(foreign_watch, "GitHubReader", return_value=reader), redirect_stdout(output):
                self.assertEqual(foreign_watch.main(), 0)
            line = json.loads(output.getvalue())
            self.assertEqual(line["errors"], [])
            self.assertEqual(line["coverage_gaps"][0]["lane"], foreign_watch.MENTIONS)


if __name__ == "__main__":
    unittest.main()
