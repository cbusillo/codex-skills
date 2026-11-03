#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Offline notification filtering and failure-safe watermark tests."""

import json
import argparse
from contextlib import redirect_stdout
from datetime import datetime
from types import SimpleNamespace
import io
from pathlib import Path
import tempfile
import tomllib
import unittest
from typing import Any
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch, create_autospec

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


def feeds(*comment_pages, opened_items=(), inline_items=(), reviews=(), repositories=()):
    pages = iter(comment_pages)

    def read(path, **_):
        if path == "/installation/repositories":
            return list(repositories)
        if path.endswith("/pulls/comments"):
            return list(inline_items)
        if path.endswith("/reviews"):
            return list(reviews)
        if not path.endswith("/issues/comments"):
            return list(opened_items)
        page = next(pages)
        if isinstance(page, Exception):
            raise page
        return page
    return read


def patch_attr(target: object, name: str, **kwargs: Any) -> Any:
    return patch.object(target, name, **kwargs)


class WatchClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class WatchTests(unittest.TestCase):
    @staticmethod
    def run_persistent_watch(args: argparse.Namespace, reader: Any) -> tuple[float, list[dict]]:
        clock = WatchClock()
        output = io.StringIO()
        deadline = foreign_watch.NOTICE_SETTLE_SECONDS + args.interval
        with (patch_attr(foreign_watch, "GitHubReader", return_value=reader),
              patch.object(foreign_watch, "utc_now", return_value=STARTED),
              patch.object(foreign_watch, "scan_mentions", side_effect=lambda _args, _repos, watermarks, seen, *_: ([], [], [], watermarks, seen)),
              patch.object(foreign_watch.time, "monotonic", side_effect=clock.monotonic),
              patch.object(foreign_watch.time, "sleep", side_effect=clock.sleep),
              redirect_stdout(output)):
            result = foreign_watch.watch(args, [REPO], SINCE, deadline)
        if result:
            raise AssertionError(output.getvalue())
        return clock.now, [notice for line in output.getvalue().splitlines() for notice in json.loads(line)["notices"]]

    def test_client_comment_returns_after_settling_without_waiting_for_poll_interval(self) -> None:
        for author in ("client", "client-agent[bot]"):
            for persistent in (False, True):
                with self.subTest(author=author, persistent=persistent), tempfile.TemporaryDirectory() as directory:
                    reader = Mock()
                    reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
                    feedback = comment(author, "Inventory and Helpdesk are missing. Holding Accept.", "2026-10-30T19:38:54Z")
                    reader.paged_json.side_effect = feeds([feedback], [])
                    reader.request.side_effect = getattr(foreign_watch, "GitHubReadShapeError")("external search unavailable")
                    args = argparse.Namespace(owner="director", state=Path(directory) / "state.json", since=None,
                        own_login=["director", "agent[bot]"], launchplane_login=[], interval=300,
                        once=False, persistent=persistent)
                    output = io.StringIO()
                    clock = WatchClock()
                    argv = ["foreign_watch.py", "--owner", args.owner, "--repo", REPO,
                            "--own-login", "director", "--own-login", "agent[bot]", "--state", str(args.state),
                            "--since", "2026-10-30T19:35:00Z", "--deadline-seconds", "60"]
                    if persistent:
                        argv.append("--persistent")
                    identities = {"status": "ok", "director_names": [], "director_ambiguous": False,
                                  "automation": {"client-agent[bot]": "Client"}}
                    with (patch_attr(foreign_watch, "GitHubReader", return_value=reader),
                          patch_attr(foreign_watch.radar_module(), "people_identities", return_value=identities),
                          patch.object(foreign_watch.sys, "argv", argv),
                          patch.object(foreign_watch, "utc_now", return_value="2026-10-30T19:40:00Z"),
                          patch.object(foreign_watch.time, "monotonic", side_effect=clock.monotonic),
                          patch.object(foreign_watch.time, "sleep", side_effect=clock.sleep),
                          redirect_stdout(output)):
                        result = foreign_watch.main()
                    self.assertEqual(result, 0)
                    self.assertEqual(clock.now, foreign_watch.NOTICE_SETTLE_SECONDS)
                    [notice] = json.loads(output.getvalue().splitlines()[0])["notices"]
                    self.assertEqual(notice["url"], feedback["html_url"])
                    self.assertEqual(notice["author"], author)
                    state = json.loads(args.state.read_text())
                    self.assertEqual(state["watermarks"][REPO], "2026-10-30T19:40:00Z")

    def test_linear_linkback_is_logged_without_ending_persistent_watch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = Mock()
            linkback = comment("linear-code[bot]", '<!-- linear-linkback -->\n<p><a href="https://linear.app/team/issue/TEST-1">TEST-1</a></p>')
            linkback = {**linkback, "user": {"login": "linear-code[bot]", "type": "Bot"}}
            reader.paged_json.side_effect = feeds([linkback], [], opened_items=[opened("director")])
            args = argparse.Namespace(owner="director", state=Path(directory) / "state.json", since=None,
                                      own_login=["director", "agent[bot]"], launchplane_login=[], interval=10,
                                      once=False, persistent=True)
            elapsed, logged = self.run_persistent_watch(args, reader)
            self.assertEqual(elapsed, foreign_watch.NOTICE_SETTLE_SECONDS + args.interval)
            self.assertEqual([notice["url"] for notice in logged], [linkback["html_url"]])

    def test_own_body_edit_is_logged_without_ending_persistent_watch(self) -> None:
        for login, actor_type, opener in (("AGENT", "Bot", "client"), ("DIRECTOR", "User", "client"),
                                          ("DIRECTOR", "User", "director")):
            with self.subTest(login=login, opener=opener), tempfile.TemporaryDirectory() as directory:
                reader = Mock()
                issue = {**opened(opener, created="2026-10-01T00:00:00Z"), "node_id": "issue"}
                reader.paged_json.side_effect = feeds([], [], opened_items=[issue])
                reader.graphql_json.return_value = ApiResult(ok=True, status=200, body={"data": {"nodes": [
                    {"lastEditedAt": SINCE, "editor": {"login": login, "__typename": actor_type}}
                ]}})
                args = argparse.Namespace(owner="director", state=Path(directory) / "state.json", since=None,
                                          own_login=["director", "agent[bot]"], launchplane_login=[], interval=10,
                                          once=False, persistent=True)
                elapsed, logged = self.run_persistent_watch(args, reader)
                self.assertEqual(elapsed, foreign_watch.NOTICE_SETTLE_SECONDS + args.interval)
                self.assertEqual([notice["url"] for notice in logged], [issue["html_url"]] if opener == "client" else [])
                if logged:
                    self.assertEqual(logged[0]["author"].casefold(), "agent[bot]" if actor_type == "Bot" else "director")
                    foreign_watch.route_notices(args, logged, {}, reader, event=False, persist=Mock())
                self.assertIsNone(foreign_watch.make_notice(REPO, {**comment("director", updated=STARTED), "created_at": SINCE, "editor": {"login": "director"}}, args.own_login, []))

    def test_restart_does_not_rewind_past_an_unchanged_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = Mock()
            args = argparse.Namespace(owner="director", state=Path(directory) / "state.json", since=SINCE,
                                      own_login=["director", "agent[bot]"], launchplane_login=[], interval=10,
                                      once=False, persistent=True)
            for updated, wakes in ((SINCE, True), (SINCE, False), ("2026-10-09T12:06:00Z", True)):
                with self.subTest(updated=updated, wakes=wakes):
                    reader.paged_json.side_effect = feeds(*[[comment(updated=updated)]] * 3)
                    elapsed, logged = self.run_persistent_watch(args, reader)
                    self.assertEqual(elapsed, foreign_watch.NOTICE_SETTLE_SECONDS if wakes else foreign_watch.NOTICE_SETTLE_SECONDS + args.interval)
                    self.assertEqual(bool(logged), wakes)
                    self.assertGreater(foreign_watch.timestamp(json.loads(args.state.read_text())["watermarks"][REPO]), foreign_watch.timestamp(SINCE))

    def test_own_questions_stay_logged_without_waking_and_bursts_settle_once(self) -> None:
        for kind, wakes, settle_error in (("director_question", False, False),
                                          ("foreign", True, False), ("foreign", True, True)):
            with self.subTest(kind=kind, settle_error=settle_error), tempfile.TemporaryDirectory() as directory:
                clock = WatchClock()
                author = "agent[bot]" if kind == "director_question" else "client"
                first = {"url": "first", "kind": kind, "author": author}
                second = {**first, "url": "second"}
                args = argparse.Namespace(owner="director", state=Path(directory) / "state.json", since=None,
                                          own_login=["agent[bot]"], launchplane_login=[], interval=foreign_watch.NOTICE_SETTLE_SECONDS,
                                          once=False, persistent=True)
                errors = [{"retryable": True, "retry_at": None}] if settle_error else []
                scans = [([first], [], {}), ([second], errors, {}), *[([], [], {})] * 20]
                output = io.StringIO()
                with (patch_attr(foreign_watch, "GitHubReader", return_value=Mock()),
                      patch.object(foreign_watch, "scan", side_effect=scans),
                      patch.object(foreign_watch, "scan_mentions", side_effect=lambda _args, _repos, watermarks, seen, *_: ([], [], [], watermarks, seen)),
                      patch.object(foreign_watch.time, "monotonic", side_effect=clock.monotonic),
                      patch.object(foreign_watch.time, "sleep", side_effect=clock.sleep),
                      redirect_stdout(output)):
                    self.assertEqual(foreign_watch.watch(args, [REPO], SINCE, 20), int(settle_error))
                logged = [notice for line in output.getvalue().splitlines() for notice in json.loads(line)["notices"]]
                self.assertEqual(logged, [first, second])
                self.assertEqual(clock.now, foreign_watch.NOTICE_SETTLE_SECONDS if wakes else 20)
                self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["errors"], errors)

    def test_own_mentions_stay_logged_without_waking_on_thread_author_confusion(self) -> None:
        own_reply = {**comment("AGENT[bot]", "@director please decide", STARTED), "node_id": "own", "created_at": STARTED}
        foreign_reply = {**comment(body="@director please decide", updated=STARTED), "node_id": "foreign", "created_at": STARTED}
        previous_reply = {**foreign_reply, "node_id": "previous", "created_at": SINCE, "updated_at": SINCE}
        lagged = "2026-10-09T11:59:00Z"
        edited_reply = {**own_reply, "created_at": SINCE, "edited_at": STARTED, "editor": "client"}
        for opener, created, comments, editor, prior, pull, wakes in (
                ("agent[bot]", SINCE, [own_reply], None, None, False, False),
                ("client", "2026-10-01T00:00:00Z", [own_reply], None, SINCE, False, False),
                ("client", SINCE, [own_reply], None, None, False, True),
                ("agent[bot]", SINCE, [foreign_reply], None, None, False, True),
                ("agent[bot]", SINCE, [foreign_reply, own_reply], None, None, False, True),
                ("agent[bot]", SINCE, [previous_reply, own_reply], None, SINCE, False, False),
                ("agent[bot]", SINCE, [own_reply], "client", SINCE, False, True),
                ("agent[bot]", SINCE, [], "agent[bot]", SINCE, False, False),
                ("agent[bot]", SINCE, [own_reply], None, None, True, True),
                ("agent[bot]", SINCE, [], None, None, False, True),
                ("client", lagged, [own_reply], None, None, False, True),
                ("agent[bot]", lagged, [{**previous_reply, "created_at": lagged}, own_reply], None, None, False, True),
                ("agent[bot]", SINCE, [edited_reply], None, SINCE, False, True)):
            with self.subTest(opener=opener, editor=editor, prior=prior, pull=pull, wakes=wakes), tempfile.TemporaryDirectory() as directory:
                clock = WatchClock()
                reader = Mock()
                thread: dict[str, Any] = {**mention(REPO, 1), "node_id": "thread", "created_at": created, "user": {"login": opener}}
                if pull:
                    thread["pull_request"] = {}
                reader.request.return_value = SimpleNamespace(body={"items": [thread], "total_count": 1})
                reader.get_json.return_value = thread
                reader.paged_json.return_value = comments
                def versions(_query: str, variables: dict, **_: Any) -> ApiResult:
                    nodes = []
                    for identifier in variables["ids"]:
                        source = next(source for source in [thread, *comments] if source["node_id"] == identifier)
                        actor = str((editor if identifier == "thread" else source.get("editor")) or "")
                        nodes.append({"lastEditedAt": STARTED if actor else None, "editor": {
                            "login": actor.removesuffix("[bot]"), "__typename": "Bot" if actor.endswith("[bot]") else "User"
                        } if actor else None})
                    return ApiResult(ok=True, status=200, body={"data": {"nodes": nodes}})
                reader.graphql_json.side_effect = versions
                args = argparse.Namespace(owner="director", repo=["director/other"], state=Path(directory) / "state.json",
                                          since=None, own_login=["agent[bot]"], launchplane_login=[],
                                          interval=foreign_watch.NOTICE_SETTLE_SECONDS, once=False, persistent=True)
                if prior:
                    foreign_watch.save_state(args.state, {}, {thread["html_url"]: prior})
                output = io.StringIO()
                with (patch_attr(foreign_watch, "GitHubReader", return_value=reader),
                      patch.object(foreign_watch, "scan", side_effect=lambda _repos, watermarks, *_: ([], [], watermarks)),
                      patch.object(foreign_watch.time, "monotonic", side_effect=clock.monotonic),
                      patch.object(foreign_watch.time, "sleep", side_effect=clock.sleep), redirect_stdout(output)):
                    self.assertEqual(foreign_watch.watch(args, args.repo, SINCE, 20), 0)
                notices = [notice for line in output.getvalue().splitlines() for notice in json.loads(line)["notices"]]
                self.assertEqual([notice["url"] for notice in notices], [thread["html_url"]])
                self.assertEqual(clock.now, foreign_watch.NOTICE_SETTLE_SECONDS if wakes else 20)

    def test_persistent_read_retry_preserves_watermark_and_provider_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [0.0]
            reader = Mock()
            reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
            reader.paged_json.side_effect = feeds([comment()], getattr(foreign_watch, "GitHubReadError")(
                "provider unavailable", result=ApiResult(ok=False, status=503, body=None,
                    failure=FailureDetail(cause="network_provider_failure", message="unavailable",
                                          retryable=True, fallback_eligible=False, disposition="retry")),
                diagnostics={"requests": [{"retryAt": 20}]},
            ), [], ValueError("terminal"))
            args = argparse.Namespace(owner="director", state=Path(directory) / "state.json", since=None,
                own_login=["director"], launchplane_login=[], interval=5, once=False, persistent=True)

            def sleep(seconds):
                now[0] += seconds

            with (patch.object(foreign_watch, "GitHubReader", return_value=reader),
                  patch.object(foreign_watch.time, "monotonic", side_effect=lambda: now[0]),
                  patch.object(foreign_watch.time, "time", side_effect=lambda: now[0]),
                  patch.object(foreign_watch.time, "sleep", side_effect=sleep) as sleeps,
                  patch.object(foreign_watch, "utc_now", return_value=STARTED),
                  redirect_stdout(io.StringIO())):
                self.assertEqual(foreign_watch.watch(args, ["director/healthy", REPO], SINCE, 100), 1)
            self.assertEqual(sleeps.call_args_list[0].args, (20,))
            failed_reads = [call for call in reader.paged_json.call_args_list if call.args[0] == f"repos/{REPO}/issues/comments"]
            self.assertEqual(failed_reads[0].kwargs["params"], failed_reads[1].kwargs["params"])
            state = json.loads(args.state.read_text())
            self.assertEqual(state["watermarks"][REPO], SINCE)
            self.assertEqual(state["watermarks"]["director/healthy"], STARTED)

    @staticmethod
    def scan(reader, client_agents=()):
        return foreign_watch.scan([REPO], {REPO: SINCE}, ["director", "agent[bot]"],
                                  ["delivery[bot]"], STARTED, reader, client_agents)

    def test_filter_and_review_records(self):
        reader = Mock()
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
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
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
        reader.paged_json.side_effect = feeds([], opened_items=[
            opened(), opened("director"), opened("client-agent[bot]", "Bot"), opened("dependabot[bot]", "Bot"), opened("delivery[bot]", "Bot"), opened("agent[bot]", "Bot"), opened(created="2026-10-09T11:00:00Z")])
        notices, errors, _ = self.scan(reader, {"client-agent[bot]"})
        self.assertEqual(errors, [])
        self.assertEqual([(n["author"], n["url"], n["excerpt"]) for n in notices],
                         [(author, "https://github.com/director/project/pull/50", "Add tvOS support")
                          for author in ("client", "client-agent[bot]", "dependabot[bot]", "delivery[bot]")])

    def test_sweep_recovers_inline_and_bare_review_on_an_old_pull(self):
        reader = Mock()
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
        inline = {**comment(), "html_url": f"https://github.com/{REPO}/pull/50#discussion_r42"}
        review = {**comment(), "html_url": f"https://github.com/{REPO}/pull/50#pullrequestreview-43", "created_at": SINCE}
        old_pull = {**opened(created="2026-10-08T00:00:00Z"), "number": 50, "pull_request": {}}
        reader.paged_json.side_effect = feeds([], opened_items=[old_pull], inline_items=[inline], reviews=[review, {"body": ""}])
        notices, errors, watermarks = self.scan(reader)
        self.assertEqual(errors, [])
        self.assertEqual({row["url"] for row in notices}, {inline["html_url"], review["html_url"]})
        self.assertEqual(watermarks[REPO], STARTED)

    def test_opening_versions_are_batched_before_recovery(self):
        reader = Mock()
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
        first = {**opened(), "node_id": "first"}
        second = {**opened(), "node_id": "second", "html_url": f"https://github.com/{REPO}/issues/51"}
        reader.paged_json.side_effect = feeds([], opened_items=[first, second])
        reader.graphql_json.return_value = ApiResult(ok=True, status=200, body={"data": {"nodes": [{"lastEditedAt": STARTED}, {"lastEditedAt": STARTED}]}})
        notices, errors, _ = self.scan(reader)
        self.assertEqual(errors, [])
        self.assertEqual({item["url"] for item in notices}, {first["html_url"], second["html_url"]})
        reader.graphql_json.assert_called_once()
        self.assertEqual([item["kind"] for item in notices], ["foreign_opened", "foreign"] * 2)
        reader.paged_json.side_effect = feeds([], opened_items=[{**first, "created_at": "2026-10-08T00:00:00Z"}])
        reader.graphql_json.return_value = ApiResult(ok=True, status=200, body={"data": {"nodes": [{"lastEditedAt": STARTED}]}})
        [edit] = self.scan(reader)[0]
        self.assertEqual((edit["kind"], edit["updated_at"]), ("foreign", STARTED))

    def test_version_failures_keep_retryability_and_deleted_nodes_are_skipped(self):
        reader = Mock()
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
        source = {**opened(), "node_id": "source"}
        reader.paged_json.side_effect = feeds([], opened_items=[source])
        retry_at = 100.0
        reader.diagnostics.return_value = {"requests": [{"retryAt": retry_at}]}
        reader.graphql_json.return_value = ApiResult(ok=False, status=502, body=None, failure=FailureDetail(cause="network_provider_failure", message="unavailable", retryable=True, fallback_eligible=False, disposition="retry"))
        notices, errors, watermarks = self.scan(reader)
        self.assertEqual(notices, [])
        self.assertTrue(errors[0]["retryable"])
        self.assertEqual(errors[0]["retry_at"], retry_at)
        self.assertEqual(watermarks[REPO], SINCE)
        reader.paged_json.side_effect = feeds([], opened_items=[source])
        reader.graphql_json.return_value = ApiResult(ok=True, status=200, body={"data": {"nodes": [None]}})
        notices, errors, watermarks = self.scan(reader)
        self.assertEqual((notices, errors), ([], []))
        self.assertEqual(watermarks[REPO], STARTED)

    def test_worker_questions_surface_on_the_next_scan_without_ordinary_own_posts(self) -> None:
        reader = Mock()
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
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
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
        correction = "If I made a mistake on the csv sheet,\n\ncan I send the corrected one?"
        reader.paged_json.side_effect = feeds([comment(body=correction), comment(body="word " * 200)])
        short, long = self.scan(reader)[0]
        self.assertEqual(short["excerpt"], "If I made a mistake on the csv sheet, can I send the corrected one?")
        self.assertTrue(long["excerpt"].startswith("word word"))
        self.assertLessEqual(len(long["excerpt"]), foreign_watch.EXCERPT_CHARS)

    def test_failure_preserves_only_failed_repository_watermark(self):
        reader = Mock()
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
        reader.paged_json.side_effect = feeds(foreign_watch.GitHubReadShapeError("partial"), [])
        notices, errors, watermarks = foreign_watch.scan(
            [REPO, "director/healthy"], {REPO: SINCE, "director/healthy": SINCE},
            ["director"], [], STARTED, reader)
        self.assertEqual(notices, [])
        self.assertEqual(errors[0]["repository"], REPO)
        self.assertEqual(watermarks, {REPO: SINCE, "director/healthy": STARTED})

    def test_invalid_comment_retains_watermark_and_discards_partial_notices(self):
        reader = Mock()
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
        for bad in ({}, {**comment(), "updated_at": "invalid"}, {**comment(), "user": []}):
            with self.subTest(bad=bad):
                reader.paged_json.side_effect = feeds([comment(), bad])
                notices, errors, watermarks = self.scan(reader)
                self.assertEqual(notices, [])
                self.assertTrue(errors)
                self.assertEqual(watermarks[REPO], SINCE)

    def test_old_comments_excluded_and_edits_reported(self):
        reader = Mock()
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
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
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
        reader.paged_json.side_effect = feeds([{**comment(), "user": None},
            comment(body='Quoted: <!-- launchplane:product-review:id -->')])
        notices, errors, watermarks = self.scan(reader)
        self.assertEqual(errors, [])
        self.assertIsNone(notices[0]["author"])
        self.assertEqual([n["kind"] for n in notices], ["foreign", "foreign"])
        self.assertEqual(watermarks[REPO], STARTED)

    def test_quota_retry_time_and_failed_read_retention(self):
        reader = Mock()
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
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
            reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
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
            self.assertEqual(factory.call_args.kwargs["gh_prefix_args"], foreign_watch.github_access.github_read.automation_only_gh_prefix_args())
            self.assertIn(REPO, json.loads(path.read_text())["watermarks"])

    def test_default_scope_is_the_installations_active_repositories_of_the_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "w.json"
            foreign_watch.save_state(state, {REPO: STARTED, "director/old": STARTED})
            reader = Mock()
            reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
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
            reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
            reader.paged_json.return_value = []
            _, _, values = foreign_watch.scan([REPO], values, ["director"], [], STARTED, reader)
            foreign_watch.save_state(path, values)
            self.assertEqual(foreign_watch.load_state(path, ["director/other"], STARTED)["director/other"], SINCE)

    def test_foreign_marker_remains_foreign(self):
        reader = Mock()
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
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
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
        reader.paged_json.side_effect = feeds(list(comments))
        args = argparse.Namespace(owner="director", state=state, since=SINCE, own_login=["director"],
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
    def scan(get=None, watermark=SINCE, seen=None):
        reader = Mock()
        reader.request.return_value = SimpleNamespace(body={"items": [], "total_count": 0, "incomplete_results": False})
        get = get or director_get([mention("client/private", 4), mention(REPO, 1)])
        reader.request.side_effect = lambda _method, endpoint, **_: SimpleNamespace(body=get(endpoint))
        reader.paged_json.return_value = [{"body": "@director, please decide"}]
        reader.get_json.return_value = {}
        args = argparse.Namespace(owner="director", director_login=None, mention_approval=None, director_names=["Pat"])
        result = foreign_watch.scan_mentions(args, [REPO], {foreign_watch.MENTIONS: watermark}, seen or {},
                                             ["director", "agent[bot]"], STARTED, reader)
        return result, reader, get

    def test_owner_alone_finds_old_untrusted_mentions_without_approval(self) -> None:
        old = mention("client/private", 8, updated="2026-09-01T11:59:00Z")
        (notices, gaps, errors, watermarks, seen), _, get = self.scan(get=director_get([old]))
        self.assertEqual((gaps, errors), ([], []))
        self.assertEqual([(n["repository"], n["number"], n["untrusted"]) for n in notices], [("client/private", 8, True)])
        self.assertNotIn("body", notices[0])
        self.assertEqual(watermarks[foreign_watch.MENTIONS], STARTED)
        self.assertTrue(all(call.args[0] != "user" for call in get.call_args_list))
        (again, _, _, _, _), _, _ = self.scan(get=director_get([old]), watermark=STARTED, seen=seen)
        self.assertEqual(again, [])
        edited = {**old, "updated_at": STARTED}
        (changed, _, _, _, _), _, _ = self.scan(get=director_get([edited]), watermark=STARTED, seen=seen)
        self.assertEqual([n["number"] for n in changed], [8])

    def test_search_gap_keeps_watermark(self) -> None:
        get = Mock(side_effect=getattr(foreign_watch, "director_mentions").MentionSearchError("rate limited"))
        (notices, gaps, errors, watermarks, _), _, _ = self.scan(get=get)
        self.assertEqual((notices, errors), ([], []))
        self.assertTrue(gaps)
        self.assertEqual(watermarks[foreign_watch.MENTIONS], SINCE)

    def test_cli_without_director_login_uses_configured_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = create_autospec(getattr(foreign_watch, "GitHubReader"), instance=True)
            reader.request.return_value = SimpleNamespace(body={"items": [mention("client/private", 9)], "total_count": 1, "incomplete_results": False})
            reader.paged_json.return_value = []
            reader.get_json.return_value = {}
            output = io.StringIO()
            with patch.object(foreign_watch.sys, "argv", ["foreign_watch.py", "--owner", "director",
                    "--repo", REPO, "--own-login", "director", "--state", str(Path(directory) / "w.json"),
                    "--since", SINCE, "--once"]), \
                 patch.object(foreign_watch, "GitHubReader", return_value=reader), redirect_stdout(output):
                self.assertEqual(foreign_watch.main(), 0)
            line = json.loads(output.getvalue())
            self.assertEqual((line["errors"], line["coverage_gaps"]), ([], []))
            self.assertEqual([row["number"] for row in line["notices"]], [9])


if __name__ == "__main__":
    unittest.main()
