#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Replay the week's stranded records and bounded read failures offline."""
import base64
import copy
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace
import sys

import direction_audit as audit
import direction_stranded as sweep
from skills.github.scripts import github_api


class StrandedTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(Path(__file__).with_name("fixtures").joinpath("stranded-week-2026-10-15.json").read_text())
        self.records = copy.deepcopy(self.fixture["records"])
        self.now = dt.datetime.fromisoformat(self.fixture["now"])
        self.trusted = {"shiny-code-app[bot]", "cbusillo"}

    def report(self, **kwargs):
        return sweep.report(self.records, now=self.now, age_hours=2, trusted=self.trusted,
                            owners={"cbusillo"}, **kwargs)

    def test_week_examples_are_reported_without_mutation(self):
        before = copy.deepcopy(self.records)
        report = self.report()
        self.assertEqual({r["number"] for r in report["findings"]},
                         {r["item"]["number"] for r in self.records})
        self.assertTrue(all(r["age_hours"] >= 2 and r["next_actor"] and r["url"] for r in report["findings"]))
        self.assertEqual(self.records, before)
        self.assertTrue(report["complete"])

    def test_related_thread_decision_resolves_question_but_bot_chatter_does_not(self):
        q = self.records[0]["comments"][0]
        answer = {"id": 99, "created_at": "2026-10-15T18:00:00Z", "body": "Q327 yes", "user": {"login": "shiny-code-app[bot]"}, "html_url": "https://github.com/cbusillo/direction/issues/1#issuecomment-99"}
        self.records[1]["comments"].append(answer)
        self.assertIn(3604, {r["number"] for r in self.report()["findings"]})
        answer["body"] = "Director decision: Q327 yes"
        self.assertNotIn(3604, {r["number"] for r in self.report()["findings"]})
        answer["user"]["login"] = "untrusted"
        self.assertIn(3604, {r["number"] for r in self.report()["findings"]})
        answer["user"]["login"] = "cbusillo"
        answer["body"] = f"Done: {q['html_url']}"
        self.assertNotIn(3604, {r["number"] for r in self.report()["findings"]})

    def test_closed_research_promise_survives_issue_closure_until_linked_report(self):
        promise = self.records[4]["comments"][0]
        self.assertIn(1791, {r["number"] for r in self.report()["findings"]})
        self.records[4]["comments"].append({"id": 100, "created_at": "2026-10-15T13:36:21Z",
            "user": {"login": "shiny-code-app[bot]"}, "body": f"Comparison posted for Q249: {promise['html_url']}",
            "html_url": "https://github.com/cbusillo/codex-skills/issues/1791#issuecomment-100"})
        self.assertNotIn(1791, {r["number"] for r in self.report()["findings"]})

    def test_unnumbered_question_context_does_not_borrow_another_question_id(self):
        self.records[1]["comments"][0]["body"] += "\n\nQ319 is a separate browser-sign-in question."
        self.records[0]["comments"].append({"id": 101, "created_at": "2026-10-15T18:00:00Z",
            "user": {"login": "shiny-code-app[bot]"}, "body": "Director decision: Q319 yes",
            "html_url": "https://github.com/cbusillo/launchplane/issues/3604#issuecomment-101"})
        self.assertIn(3639, {r["number"] for r in self.report()["findings"]})

    def test_valid_new_blocker_and_person_wait_preserve_blocked_view(self):
        record = self.records[3]
        record["blockers"].append({"state": "open"})
        self.assertNotIn(218, {r["number"] for r in self.report()["findings"]})
        record["blockers"].pop()
        record["item"]["body"] = sweep.github_plan_wait.marker(sweep.github_plan_wait.reviewed(
            evidence="Actual Director wait", waiting_for="Director", ends_when="A recorded decision arrives"))
        rows = [r for r in self.report()["findings"] if r["number"] == 218]
        self.assertFalse(any(r["kind"] == "stale_blocked_view" for r in rows))
        self.assertTrue(any(r["next_actor"] == "Director" for r in rows))
        self.assertTrue(all(r["age_hours"] is None for r in rows))

    def test_refs_and_partial_scope_are_review_only_and_other_branch_is_not_completion(self):
        record = self.records[2]
        row = next(r for r in self.report()["findings"] if r["number"] == 3678)
        self.assertEqual(row["kind"], "merged_link_needs_review")
        record["linked_pulls"][0]["body"] = "Fixes cbusillo/launchplane#3678"
        row = next(r for r in self.report()["findings"] if r["number"] == 3678)
        self.assertEqual(row["kind"], "open_after_completing_merge")
        self.assertTrue(row["review_required"])
        record["linked_pulls"][0]["base"]["ref"] = "release"
        self.assertNotIn(3678, {r["number"] for r in self.report()["findings"]})

    def test_push_and_renewed_wait_invalidate_current_head_ready(self):
        record = self.records[5]
        record["pull"]["head"]["sha"] = "c" * 40
        self.assertNotIn(1632, {r["number"] for r in self.report()["findings"]})
        record["pull"]["head"]["sha"] = "a" * 40
        record["comments"].append({"id": 200, "created_at": "2026-10-15T18:00:00Z", "user": {"login": "shiny-code-app[bot]"}, "body": "Engineering ready; waiting for a defect fix", "html_url": "https://github.com/cbusillo/codex-skills/pull/1632#issuecomment-200"})
        self.assertNotIn(1632, {r["number"] for r in self.report()["findings"]})

    def test_threshold_uses_original_event_and_unknown_age_stays_visible(self):
        self.records[0]["comments"][0]["created_at"] = (self.now - dt.timedelta(hours=1)).isoformat()
        self.assertNotIn(3604, {r["number"] for r in self.report()["findings"]})
        self.records[0]["comments"][0]["created_at"] = "invalid"
        row = next(r for r in self.report()["findings"] if r["number"] == 3604)
        self.assertIsNone(row["age_hours"])
        self.assertEqual(row["age_basis"], "unknown")

    def test_coverage_failure_is_in_markdown_even_with_zero_findings(self):
        self.records = []
        report = self.report(gaps=[{"source": "comments", "reason": "unavailable"}])
        self.assertFalse(report["complete"])
        rendered = sweep.markdown(report)
        self.assertIn(report["coverage_gaps"][0]["source"], rendered)
        self.assertIn(report["coverage_gaps"][0]["reason"], rendered)

    def test_policy_label_is_read_from_the_prs_actual_base_target(self):
        record = self.records[5]
        record["pull"]["base"] = {"ref": "integration"}
        record["train_policy"] = {"targets": [{"baseBranch": "integration", "readyLabel": "ship-this"}]}
        row = next(r for r in self.report()["findings"] if r["number"] == 1632)
        self.assertEqual(row["kind"], "ready_handoff_unrouted")
        record["item"]["labels"] = [{"name": "ship-this"}]
        row = next(r for r in self.report()["findings"] if r["number"] == 1632)
        self.assertEqual(row["kind"], "aged_ready_handoff")
        record["pull"]["base"]["ref"] = "unregistered"
        row = next(r for r in self.report()["findings"] if r["number"] == 1632)
        self.assertEqual(row["kind"], "ready_handoff_routing_unknown")

    def test_delivery_view_evidence_is_preserved_and_contract_failure_stays_visible(self):
        url = self.records[0]["comments"][0]["html_url"]
        delivery = {"url": url, "delivery": "acknowledged", "registration_url": url + "-registration",
                    "delivery_url": "https://discord.com/channels/1/2/3", "resurface": False}
        result = self.report(delivery_view={"complete": True, "questions": [delivery]})
        row = next(r for r in result["findings"] if r["number"] == 3604)
        self.assertEqual(row["evidence"][0]["delivery_url"], delivery["delivery_url"])
        self.assertFalse(row["evidence"][0]["resurface"])
        self.assertFalse(self.report(delivery_view={"complete": False, "questions": []})["complete"])

    def test_umbrella_merged_refs_are_one_candidate_per_issue(self):
        record = self.records[2]
        newer = {**record["linked_pulls"][0], "merged_at": "2026-10-15T20:00:00Z",
                 "html_url": "https://github.com/cbusillo/launchplane/pull/3700"}
        record["linked_pulls"].append(newer)
        rows = [r for r in self.report()["findings"] if r["number"] == 3678]
        self.assertEqual(len(rows), 1)
        self.assertIn(newer["html_url"], rows[0]["evidence"])


REGISTER = Path(__file__).with_name("fixtures").joinpath("operational-register-2026-10-14.txt").read_text()


class OverdueTriggerTests(unittest.TestCase):
    """Dated triggers that passed without anyone raising them (codex-skills#716, #790)."""

    def setUp(self):
        # 11:30 PM ET on October 14, when the register's misses were found by hand.
        self.now = dt.datetime(2026, 10, 15, 3, 30, tzinfo=dt.timezone.utc)
        self.register = {"repo": "example/ops", "path": "docs/register.md", "default_branch": "main", "text": REGISTER}

    def report(self, records=(), **kwargs):
        return sweep.report(list(records), now=self.now, age_hours=2, trusted={"shiny-code-app[bot]", "example"},
                            owners={"example"}, registers=[self.register], **kwargs)

    def flagged(self, report=None):
        report = report or self.report()
        return {(row["kind"], row["url"].rpartition("#")[2]) for row in report["findings"]}

    def titles(self, *names):
        entries = {e["title"]: e for e in sweep.register_entries(REGISTER)}
        return [sweep.anchor(entries[name]["title"]) for name in names]

    def test_overdue_check_and_owner_decision_are_reported_and_future_or_event_checks_are_not(self):
        camera, host = self.titles("Camera bridge rollback artifacts", "Host name resolution")
        report = self.report()
        self.assertEqual(self.flagged(report), {("overdue_operational_check", camera),
                                                ("operational_check_waits_on_owner", host)})
        self.assertTrue(report["complete"])
        self.assertTrue(all(row["url"].startswith("https://github.com/example/ops/blob/main/docs/register.md#")
                            for row in report["findings"]))
        self.assertIn("2026-10-13", sweep.markdown(report))

    def test_resolved_entry_removed_from_register_is_no_longer_reported(self):
        camera, host = self.titles("Camera bridge rollback artifacts", "Host name resolution")
        self.register["text"] = REGISTER.split("### Camera bridge rollback artifacts")[0]
        self.assertEqual(self.flagged(), {("operational_check_waits_on_owner", host)})

    def test_dated_check_is_reported_once_its_date_passes(self):
        garage, roaming = self.titles("Garage close and arrival observation", "Access point roaming fault")
        self.now = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)
        kinds = self.flagged()
        self.assertIn(("overdue_operational_check", garage), kinds)
        self.assertIn(("overdue_operational_check", roaming), kinds)

    def test_register_wording_variants_still_report(self):
        camera, host = self.titles("Camera bridge rollback artifacts", "Host name resolution")
        self.register["text"] = (REGISTER.replace("**Next check**:", "**Next check:**")
                                 .replace("the overdue 2026-10-13 observation", "October 13, 2026")
                                 .replace("- **", "* **")
                                 .replace("the owner decides on", "waiting for the Director to decide on"))
        self.assertEqual(self.flagged(), {("overdue_operational_check", camera),
                                          ("operational_check_waits_on_owner", host)})

    def test_entry_without_next_check_is_a_coverage_gap(self):
        self.register["text"] = "### Unscheduled\n\n- **Current action**: look at it.\n"
        report = self.report()
        self.assertFalse(report["complete"])
        self.assertEqual(report["findings"], [])

    def issue(self, number, status=None, *, structured=None, comments=()):
        body = f"## Current Status\n\n{status}\n" if status else ""
        if structured:
            body += sweep.github_plan_wait.marker(structured)
        return {"repo": "example/catalog", "comments": list(comments), "item": {
            "number": number, "state": "open", "user": {"login": "example"}, "body": body,
            "html_url": f"https://github.com/example/catalog/issues/{number}"}}

    def test_passed_parked_until_and_waiting_for_dates_are_reported(self):
        records = [
            self.issue(1, "State: parked.\nParked until: October 13, 2026 (3 days of validated running)."),
            self.issue(2, "Waiting for: the measurement after 2026-10-14T22:24:00Z."),
            self.issue(3, "Parked until: 2026-10-30, when the window opens."),
            self.issue(4, "Waiting for: None for the local checks (2026-10-12 scope comment)."),
            self.issue(5, "Waiting for: the Client's DNS change; since 2026-10-01"),
            self.issue(6, "Parked until: `next` skips a due follow-up a second time."),
        ]
        rows = [r for r in self.report(records)["findings"] if r["kind"] == "passed_dated_wait"]
        self.assertEqual({r["number"] for r in rows}, {1, 2})
        self.assertTrue(all(r["age_hours"] >= 2 and r["next_actor"] for r in rows))

    def test_structured_wait_record_supersedes_prose(self):
        reviewed = sweep.github_plan_wait.reviewed
        records = [
            self.issue(1, "Parked until: 2026-10-13.", structured=reviewed(evidence="Released by the Director")),
            self.issue(2, None, structured=reviewed(evidence="Parked", waiting_for="A quiet week of backups",
                                                    ends_when="2026-10-14, compare consecutive backups")),
            self.issue(3, None, structured=reviewed(evidence="Parked", waiting_for="A quiet week of backups",
                                                    ends_when="2026-10-20, compare consecutive backups")),
        ]
        rows = [r for r in self.report(records)["findings"] if r["kind"] == "passed_dated_wait"]
        self.assertEqual({r["number"] for r in rows}, {2})


class FakeReader:
    def __init__(self):
        self.paths = []
        self.remaining = 1000
        self.limit = None
        self.pages = {}
        self.repositories = {"repos/test/repo": {"archived": False, "default_branch": "main"}}
        self.files = {}

    def get_json(self, path, *, step):
        self.paths.append(path)
        if path == "rate_limit":
            return {"resources": {"core": {"remaining": self.remaining, "limit": self.limit}}}
        if path in self.repositories:
            return self.repositories[path]
        if "/contents/" in path:
            if path not in self.files:
                raise sweep.github_read.GitHubReadError("Resource not found", result=SimpleNamespace(
                    failure=SimpleNamespace(cause="not_found")), diagnostics={})
            return {"type": "file", "content": base64.b64encode(self.files[path].encode()).decode()}
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        key = (parsed.path, int(query.get("page", [1])[0]), query.get("state", [None])[0])
        if key in self.pages:
            return copy.deepcopy(self.pages[key])
        if (parsed.path.startswith("repos/") and
                (parsed.path.endswith(("/issues", "/comments", "/timeline", "/blocked_by")))):
            return []
        if path in {"repos/target/repo/issues/2", "repos/test/missing"}:
            raise unavailable("not_found")
        raise AssertionError(f"unconfigured fake read: {path}")


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        self.now = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)

    def collect(self, **kwargs):
        return sweep.collect(["test/repo"], reader=self.reader, now=self.now, **kwargs)

    def test_second_comment_page_is_read_and_closed_history_inventoried(self):
        self.reader.pages[("repos/test/repo/issues", 1, "open")] = [
            {"number": 1, "state": "open", "user": {"login": "test"}}]
        path = "repos/test/repo/issues/1/comments"
        self.reader.pages[(path, 1, None)] = [
            {"id": n, "body": "", "created_at": "2026-10-14T00:00:00Z", "user": {"login": "test"},
             "html_url": f"https://github.com/test/repo/issues/1#issuecomment-{n}"} for n in range(100)]
        self.reader.pages[(path, 2, None)] = [
            {"id": 101, "body": "Owner question: Q100 Should this continue?", "created_at": "2026-10-14T01:00:00Z", "user": {"login": "test"}, "html_url": "https://github.com/test/repo/issues/1#issuecomment-101"}]
        report = self.collect()
        self.assertFalse([gap for gap in report["coverage_gaps"] if gap["source"] != "question_delivery"])
        self.assertEqual(len(report["findings"]), 1)
        self.assertTrue(any(urlsplit(p).path == path and parse_qs(urlsplit(p).query).get("page") == ["2"] for p in self.reader.paths))
        self.assertTrue(any(parse_qs(urlsplit(p).query).get("state") == ["closed"]
                            and "since" in parse_qs(urlsplit(p).query) for p in self.reader.paths))

    def test_quota_reserve_and_request_limit_expose_unread_repositories(self):
        self.reader.remaining = 0
        report = self.collect()
        self.assertFalse(report["complete"])
        self.assertTrue(any(s["repo"] == "test/repo" for g in report["coverage_gaps"] for s in g.get("scopes", [])))
        self.assertEqual(self.reader.paths, ["rate_limit"])
        self.reader.remaining = 1000
        self.reader.paths.clear()
        report = self.collect(max_requests=2)
        self.assertFalse(report["complete"])
        self.assertLessEqual(len(self.reader.paths), 2)

    def test_page_cap_and_malformed_page_cannot_report_complete(self):
        path = ("repos/test/repo/issues", 1, "open")
        self.reader.pages[path] = [{"number": n, "state": "open"} for n in range(1, 101)]
        report = self.collect(max_pages=1)
        self.assertFalse(report["complete"])
        self.assertTrue(any(g["reason"] == "page_limit" for g in report["coverage_gaps"]))
        self.reader.pages[path] = {"unexpected": "object"}
        result = self.collect()
        self.assertTrue(any(g["source"].startswith(path[0]) for g in result["coverage_gaps"]))

    def test_audit_mode_reuses_collector_and_does_not_stamp_direction(self):
        with patch.object(sweep, "collect", return_value={"complete": True}) as collect, \
                patch.object(audit.github_read, "GitHubReader"), \
                patch.object(audit, "record_audit") as mark, patch("builtins.print"):
            self.assertEqual(audit.main(["--stranded-work-only", "--repo", "test/repo"]), 0)
        collect.assert_called_once()
        mark.assert_not_called()

    def test_owner_discovery_uses_existing_inventory_and_rotates_daily(self):
        with patch.object(audit, "owner_repositories", return_value=({"test/repo": {}}, False)) as discover:
            result = sweep.collect([], reader=self.reader, owner="test", now=self.now)
        discover.assert_called_once()
        self.assertEqual(result["repositories"], ["test/repo"])
        self.assertFalse(any(g["source"] == "test" for g in result["coverage_gaps"]))
        for day in (0, 1):
            reader = FakeReader()
            with patch.object(audit, "owner_repositories", return_value=({"test/other": {}, "test/repo": {}}, False)):
                # A one-read budget exposes the first repository without live metadata.
                result = sweep.collect([], reader=reader, owner="test", now=self.now + dt.timedelta(days=day), max_requests=1)
            sources = [s["repo"] for g in result["coverage_gaps"] for s in g.get("scopes", [])]
            if day == 0:
                first = sources[0]
            else:
                self.assertNotEqual(sources[0], first)

    def test_budget_aggregates_unread_records_instead_of_flooding_digest(self):
        self.reader.pages[("repos/test/repo/issues", 1, "open")] = [
            {"number": n, "state": "open"} for n in range(1, 60)]
        result = self.collect(max_requests=4)
        unread = [g for g in result["coverage_gaps"] if g.get("count")]
        self.assertEqual(len(unread), 1)
        self.assertEqual(unread[0]["count"] + result["attempted_records"],
                         len(self.reader.pages[("repos/test/repo/issues", 1, "open")]))
        self.assertLess(len(result["coverage_gaps"]), 5)

    def test_cross_repo_merged_pr_is_checked_against_its_own_default_branch(self):
        original = self.reader.get_json
        def read(path, *, step):
            if path == "repos/source/repo":
                return {"archived": False, "default_branch": "integration"}
            if path == "repos/source/repo/pulls/2":
                return {"body": "Fixes test/repo#1", "merged_at": "2026-10-14T01:00:00Z",
                        "base": {"ref": "integration"}, "html_url": "https://github.com/source/repo/pull/2"}
            return original(path, step=step)
        self.reader.get_json = read
        self.reader.pages[("repos/test/repo/issues", 1, "open")] = [{"number": 1, "state": "open"}]
        self.reader.pages[("repos/test/repo/issues/1/timeline", 1, None)] = [{"event": "cross-referenced", "source": {
            "issue": {"number": 2, "pull_request": {"url": "https://api.github.com/repos/source/repo/pulls/2"}, "repository_url": "https://api.github.com/repos/source/repo"}}}]
        result = self.collect()
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual(result["findings"][0]["kind"], "open_after_completing_merge")

    def test_expired_policy_read_does_not_launch_a_helper(self):
        with patch.object(sweep.subprocess, "run") as run:
            self.assertIsNone(sweep.train_policy("test/repo", deadline_at=0))
        run.assert_not_called()

    def test_queue_adapter_normalizes_rest_comments_without_host_state(self):
        fixture = json.loads(Path(__file__).with_name("fixtures").joinpath("stranded-week-2026-10-15.json").read_text())
        question = fixture["records"][0]["comments"][0]
        def inventory(source, queue, owner, authors, now=None, *, question_urls=None):
            self.assertEqual(queue[0]["url"], question["html_url"])
            self.assertEqual(queue[0]["author"], question["user"]["login"])
            return {"complete": True, "questions": []}
        self.reader.pages[("repos/test/repo/issues/123/comments", 1, None)] = [question]
        with patch.dict(sys.modules, {"skills.supervisor.scripts.question_delivery": SimpleNamespace(inventory=inventory, records=lambda *_: [])}):
            result = self.collect(question_queue="test/repo#123")
        self.assertTrue(result["complete"], result)

    def test_transport_quota_or_deadline_stop_is_aggregated_immediately(self):
        for cause in ("quota_reserve", "deadline_exceeded"):
            with self.subTest(cause=cause):
                reader = FakeReader()
                reader.pages[("repos/test/repo/issues", 1, "open")] = [{"number": n, "state": "open"} for n in range(1, 50)]
                reader.pages[("repos/test/repo/issues", 1, "open")][0].update({
                    "user": {"login": "test"}, "body": sweep.github_plan_wait.marker(sweep.github_plan_wait.reviewed(
                        evidence="A later comment may clear this body marker", waiting_for="Supervisor", ends_when="Reply recorded"))})
                original = reader.get_json
                def read(path, *, step):
                    if "/comments?" in path:
                        reader.paths.append(path)
                        raise sweep.github_read.GitHubReadError("Stopped bulk read", result=SimpleNamespace(
                            failure=SimpleNamespace(cause=cause)), diagnostics={})
                    return original(path, step=step)
                reader.get_json = read
                result = sweep.collect(["test/repo"], reader=reader, now=self.now)
                self.assertTrue(any(g["reason"] == cause for g in result["coverage_gaps"]))
                self.assertTrue(any(g.get("count", 0) > 40 for g in result["coverage_gaps"]))
                self.assertLess(len(reader.paths), 8)
                self.assertLess(len(result["coverage_gaps"]), 8)
                self.assertFalse(any(row["kind"] == "aged_structured_wait" for row in result["findings"]))

    def test_all_repositories_open_work_precedes_closed_history(self):
        self.reader.repositories["repos/test/other"] = {"archived": False, "default_branch": "main"}
        result = sweep.collect(["test/repo", "test/other"], reader=self.reader, now=self.now)
        states = [parse_qs(urlsplit(p).query)["state"][0] for p in self.reader.paths if "state=" in p]
        self.assertTrue(states and "open" in states and "closed" in states)
        self.assertLess(max(i for i, state in enumerate(states) if state == "open"),
                        min(i for i, state in enumerate(states) if state == "closed"))
        self.assertEqual(result["checked_records"], 0)

    def test_zero_comment_records_skip_comment_reads(self):
        self.reader.pages[("repos/test/repo/issues", 1, "open")] = [{"number": 1, "state": "open", "comments": 0}]
        self.collect()
        self.assertFalse(any("/comments" in p for p in self.reader.paths))

    def test_many_unread_repositories_have_one_gap_with_both_states(self):
        reader = FakeReader()
        reader.remaining = 0
        repos = [f"test/repo{n}" for n in range(40)]
        result = sweep.collect(repos, reader=reader, now=self.now)
        scope_gaps = [g for g in result["coverage_gaps"] if g.get("scopes")]
        self.assertEqual(len(scope_gaps), 1)
        self.assertEqual({s["repo"] for s in scope_gaps[0]["scopes"]}, set(repos))
        for repo in repos:
            self.assertTrue({"open", "reopen_triggers", "closed"} <=
                            {s["state"] for s in scope_gaps[0]["scopes"] if s["repo"] == repo})
        self.assertLess(len(result["coverage_gaps"]), 4)

    def test_queue_recovery_precedes_history_and_unread_is_not_missing(self):
        urls = [f"https://github.com/test/repo/issues/{n}#issuecomment-{n}" for n in (1, 2, 3)]
        def inventory(source, queue, owner, authors, now=None, *, question_urls=None):
            known = {c["url"] for c in source}
            missing = [u for u in urls[:2] if u not in known]
            rows = [{"url": u, "status": "needs_review" if u in known else "missing_source",
                     "created_at": "2026-10-14T00:00:00Z", "registration_url": "https://github.com/test/repo/issues/123#issuecomment-100",
                     "delivery": "unacknowledged", "resurface": True} for u in urls[:2]]
            self.assertEqual(question_urls, set(urls))
            return {"complete": not missing, "questions": rows, "missing_sources": missing}
        module = SimpleNamespace(inventory=inventory, records=lambda *_: [{"question_url": u} for u in urls],
                                 settled=lambda url, *_: url == urls[-1],
                                 source_item=lambda url: "test/repo#" + url.split("/issues/")[1].split("#")[0])
        for budget in (800, 3):
            reader = FakeReader()
            for n, url in enumerate(urls[:2], 1):
                reader.pages[(f"repos/test/repo/issues/{n}/comments", 1, None)] = [{"id": n, "html_url": url,
                    "created_at": "2026-10-14T00:00:00Z", "body": "Owner question: Should we continue?", "user": {"login": "test"}}]
            with patch.dict(sys.modules, {"skills.supervisor.scripts.question_delivery": module}):
                result = sweep.collect(["test/repo"], reader=reader, now=self.now,
                                       question_queue="test/repo#123", max_requests=budget)
            self.assertTrue(any(row["kind"] == "registered_question" for row in result["findings"]))
            self.assertFalse(any("/issues/3/comments" in path for path in reader.paths))
            if budget == 800:
                self.assertTrue(result["complete"], result)
                queue_index = next(i for i, path in enumerate(reader.paths) if "/issues/123/comments" in path)
                history_index = next(i for i, path in enumerate(reader.paths) if "state=closed" in path)
                self.assertLess(queue_index, history_index)
            else:
                self.assertTrue(any(g["reason"] == "unread_registered_sources" for g in result["coverage_gaps"]))
                self.assertFalse(any(g.get("missing_sources") for g in result["coverage_gaps"]))
                self.assertFalse(any(row["url"] == urls[1] for row in result["findings"]))

    def test_register_is_read_from_the_path_the_repository_declares(self):
        metadata = "repos/test/repo/contents/.github/github.json"
        result = self.collect()
        self.assertEqual(result["operational_registers"], [])
        self.assertFalse([g for g in result["coverage_gaps"] if g["source"] != "question_delivery"])
        self.reader.files[metadata] = json.dumps({"docs": {"openOperationalIssues": "ops/follow-ups.md"}})
        self.reader.files["repos/test/repo/contents/ops/follow-ups.md"] = REGISTER
        result = self.collect()
        self.assertEqual(result["operational_registers"], ["test/repo:ops/follow-ups.md"])
        self.assertTrue(any(r["kind"] == "overdue_operational_check" and "/blob/main/ops/follow-ups.md#" in r["url"]
                            for r in result["findings"]))
        for declared, files in (("ops/missing.md", {}), ("../outside.md", {})):
            reader = FakeReader()
            reader.files = {metadata: json.dumps({"docs": {"openOperationalIssues": declared}}), **files}
            result = sweep.collect(["test/repo"], reader=reader, now=self.now)
            self.assertFalse(result["complete"])
            self.assertEqual(result["operational_registers"], [])

    def test_sweep_reserve_agrees_with_the_shared_transport(self):
        self.reader.limit = 8000
        reserve = sweep.github_request_usage.bulk_read_reserve(self.reader.limit)
        self.reader.remaining = reserve
        result = self.collect()
        self.assertEqual(result["quota_reserve"], reserve)
        self.assertEqual(self.reader.paths, ["rate_limit"])


def unavailable(cause):
    return sweep.github_read.GitHubReadError("Unavailable", result=github_api.ApiResult(
        ok=False, status=404, body={}, failure=github_api.FailureDetail(
            cause=cause, message="offline fixture", retryable=False,
            fallback_eligible=False, disposition="stop")), diagnostics={})


class ReopenInventoryTests(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        self.now = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)

    def event(self, kind="date", **fields):
        return {"version": 1, "kind": kind, "evidence": "Deferral decision", **(fields or {"at": "2026-10-16"})}

    def issue(self, number, event=None, **fields):
        return {"number": number, "state": "closed", "user": {"login": "test"},
                "body": sweep.github_plan_trigger.marker(event) if event else "",
                "comments": 0, "updated_at": "2020-01-01T00:00:00Z", **fields}

    def collect(self, issues, **kwargs):
        self.reader.pages[("repos/test/repo/issues", 1, "closed")] = issues
        return sweep.collect(["test/repo"], reader=self.reader, now=self.now, **kwargs)

    def test_old_closed_issues_are_checked_without_age_threshold_or_history_cutoff(self):
        result = self.collect([self.issue(1, self.event()),
            self.issue(2, self.event(at="2026-10-17")),
            self.issue(3, self.event("recurrence", symptom="date 2020-01-01 causes exact error")),
            self.issue(4, self.event(), state="open"),
            self.issue(5, self.event(), pull_request={})], age_hours=1000)
        rows = [r for r in result["findings"] if r["kind"] == "reopen_trigger_fired"]
        self.assertEqual([r["number"] for r in rows], [1])
        self.assertIn("reopen_trigger_fired", sweep.markdown(result))
        self.assertEqual(rows[0]["evidence"][0]["trigger"], self.event())

    def test_comment_trigger_on_second_page_replaces_body_and_foreign_record_cannot_override(self):
        future = self.event(at="2027-01-01")
        comments = [{"id": n, "body": "", "created_at": "2026-01-01T00:00:00Z", "user": {"login": "test"},
                     "html_url": f"https://github.com/test/repo/issues/1#issuecomment-{n}"} for n in range(1, 101)]
        path = "repos/test/repo/issues/1/comments"
        self.reader.pages[(path, 1, None)] = comments
        self.reader.pages[(path, 2, None)] = [{**comments[0], "id": 101,
            "body": sweep.github_plan_trigger.marker(self.event())}]
        issue = self.issue(1, future, comments=101)
        self.assertEqual(len(self.collect([issue])["findings"]), 1)
        self.reader.pages[(path, 2, None)][0]["user"] = {"login": "outsider"}
        self.assertEqual(self.collect([issue])["findings"], [])

    def test_unread_comments_never_fall_back_to_an_old_fired_trigger(self):
        original = self.reader.get_json
        def read(path, *, step):
            if "/comments?" in path:
                raise unavailable("forbidden")
            return original(path, step=step)
        self.reader.get_json = read
        result = self.collect([self.issue(1, self.event(), comments=1)])
        self.assertFalse(result["complete"])
        self.assertEqual(result["findings"], [])
        self.assertTrue(any("comments" in g["source"] for g in result["coverage_gaps"]))

    def test_unreadable_target_and_malformed_record_are_coverage_gaps(self):
        result = self.collect([self.issue(1, self.event("issue_closed", url="https://github.com/target/repo/issues/2")),
                               self.issue(2, {"version": 1, "kind": "date"})])
        self.assertEqual(result["findings"], [])
        self.assertFalse(result["complete"])
        self.assertEqual({g["source"] for g in result["coverage_gaps"] if g["reason"] == "reopen_trigger_unverifiable"},
                         {"test/repo#1", "test/repo#2"})

    def test_owner_inventory_covers_archived_repositories_and_reports_unreadable_repositories(self):
        self.reader.repositories["repos/test/repo"]["archived"] = True
        self.reader.pages[("repos/test/repo/issues", 1, "closed")] = [self.issue(1, self.event())]
        repos = {"test/repo": {}, "test/missing": {}}
        with patch.object(audit, "owner_repositories", return_value=(repos, False)) as discover:
            result = sweep.collect([], reader=self.reader, owner="test", now=self.now)
        self.assertTrue(discover.call_args.kwargs["include_archived"])
        self.assertEqual({r["repo"] for r in result["findings"]}, {"test/repo"})
        self.assertTrue(any(g["source"] == "test/missing" for g in result["coverage_gaps"]))
        self.assertFalse(result["complete"])


    def test_index_finds_old_trigger_with_bounded_reads_and_preserves_recent_promises(self):
        original = self.reader.get_json
        trigger_issue = self.issue(1, self.event())
        promise = self.issue(2, comments=1, updated_at=self.now.isoformat())
        def read(path, *, step):
            parsed = urlsplit(path)
            query = parse_qs(parsed.query)
            if parsed.path == "repos/test/repo/issues" and query.get("state") == ["closed"]:
                self.reader.paths.append(path)
                # The old labeled issue and the recently updated promise are
                # separate populations. Unrelated old history costs no reads.
                return [trigger_issue] if query.get("labels") == [sweep.github_plan_trigger.INDEX_LABEL] else [promise]
            return original(path, step=step)
        self.reader.get_json = read
        self.reader.pages[("repos/test/repo/issues/2/comments", 1, None)] = [{
            "id": 1, "body": "Director decision: comparison promised\nDue: 2026-10-15T00:00:00Z",
            "user": {"login": "test"}, "created_at": "2026-10-14T00:00:00Z",
            "html_url": "https://github.com/test/repo/issues/2#issuecomment-1"}]
        result = self.collect([], max_requests=12)
        self.assertEqual({(r["kind"], r["number"]) for r in result["findings"]},
                         {("reopen_trigger_fired", 1), ("overdue_promise", 2)})
        self.assertFalse(any("budget" in g["reason"] for g in result["coverage_gaps"]))

    def test_primary_automation_author_matches_close_even_without_bot_alias_list(self):
        issue = self.issue(1, self.event(), user={"login": "primary-bot"})
        with patch.object(sweep.github_plan_wait.github_identity, "configured_bot_logins", return_value=()), \
                patch.object(sweep.github_plan_wait.github_identity, "automation_login", return_value="primary-bot"):
            result = self.collect([issue])
        self.assertEqual([r["number"] for r in result["findings"]], [1])

    def test_unread_release_listing_is_tied_to_the_closed_issue(self):
        event = self.event("release_published", repo="target/repo", minimum_tag="v2.0")
        original = self.reader.get_json
        def read(path, *, step):
            if path.startswith("repos/target/repo/releases"):
                raise unavailable("not_found")
            return original(path, step=step)
        self.reader.get_json = read
        result = self.collect([self.issue(1, event)])
        self.assertFalse(result["complete"])
        self.assertEqual(result["findings"], [])
        self.assertTrue(any(g["source"] == "test/repo#1" and g["reason"] == "reopen_trigger_unverifiable"
                            for g in result["coverage_gaps"]))


if __name__ == "__main__":
    unittest.main()
