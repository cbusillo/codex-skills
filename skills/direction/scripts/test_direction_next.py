# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
from __future__ import annotations

import copy
import datetime as dt
import os
import time
import unittest
from unittest.mock import Mock, patch

import direction_next as sweep

from skills.github.scripts.github_plan_status import events, waits

NOW = dt.datetime(2026, 10, 31, 18, tzinfo=dt.UTC)
TRUSTED = {"owner", "bot"}


def issue(actor: str = "agent", wait: dict | None = None, **extra: object) -> dict:
    return {
        "number": 1,
        "state": "open",
        "user": {"login": "owner"},
        "updated_at": (NOW - dt.timedelta(days=2)).isoformat(),
        "body": f"## Current Status\nNext actor: {actor}\nNext action: Finish the requested change.\n\n"
        + waits.marker(wait or waits.reviewed(evidence="Reviewed")),
        **extra,
    }


def actions(item: dict, comments: list | None = None, one: Mock | None = None) -> list:
    return sweep.actions(item, comments or [], trusted=TRUSTED, now=NOW, one=one or Mock(), listing=Mock())


class NextSweepTests(unittest.TestCase):
    def test_shared_events_resolve_dates_and_keep_independent_person_waits(self) -> None:
        for date, expected in (("2026-11-01", False), ("2026-10-31", True)):
            record = waits.reviewed(
                evidence="Dated observation",
                waiting_for=["Observation window", "Client acceptance"],
                ends_when=["Window opens", "Client accepts"],
                not_before=date,
            )
            selected = actions(issue("event:observation", record))
            self.assertEqual(bool(selected), expected)
            if expected:
                self.assertEqual(selected[0]["kind"], "resolve")
                self.assertEqual(selected[0]["after"]["waits"], record["waits"][1:])
                self.assertEqual(selected[0]["after"]["replaces"], waits.record_digest(record))

    def test_reopen_and_wait_use_the_same_event_without_clearing_unknown_targets(self) -> None:
        event = {"version": 1, "kind": "pr_merged", "url": "https://github.com/owner/repo/pull/2", "evidence": "Source landing"}
        waiting = waits.reviewed(evidence="Source landing", waiting_for="Source PR landing", ends_when="PR merges", event=event)
        closed = issue(state="closed")
        closed["body"] += "\n" + events.marker(event)
        for target, expected in (
            ({"state": "open", "merged_at": None}, False),
            ({"state": "closed", "merged_at": None}, False),
            ({"state": "closed", "merged_at": NOW.isoformat()}, True),
        ):
            self.assertEqual(bool(actions(closed, one=Mock(return_value=target))), expected)
            self.assertEqual(bool(actions(issue("event:source", waiting), one=Mock(return_value=target))), expected)
        for item in (closed, issue("event:source", waiting)):
            with self.assertRaises(ValueError):
                actions(item, one=Mock(return_value={}))

    def test_nudges_are_once_per_next_action_and_never_deduped_by_foreign_comments(self) -> None:
        item = issue("agent:supervisor")
        result = actions(item)
        self.assertEqual(result[0]["kind"], "nudge")
        comment = {"user": {"login": "bot"}, "body": result[0]["marker"]}
        self.assertEqual(actions(item, [comment]), [])
        comment["user"]["login"] = "outsider"
        self.assertEqual(actions(item, [comment]), result)
        self.assertEqual(actions({**item, "updated_at": NOW.isoformat()}), [])
        self.assertEqual(actions(issue("person:owner")), [])
        self.assertEqual(actions(issue(issue_dependencies_summary={"blocked_by": 1})), [])

    def test_direction_uses_only_current_actor_or_wait_and_preserves_decided_history(self) -> None:
        historical = {"user": {"login": "bot"}, "body": "For the direction session: decide the design."}
        self.assertEqual({action["kind"] for action in actions(issue("agent:direction"))}, {"direction", "nudge"})
        self.assertEqual([action["kind"] for action in actions(issue("agent:direction", labels=["direction"]))], ["nudge"])
        self.assertFalse(any(action["kind"] == "direction" for action in actions(issue(), [historical])))
        wait = waits.reviewed(evidence="Trial audit", waiting_for="Weekly direction audit", ends_when="Director records audit")
        self.assertEqual(actions(issue("event:audit", wait)), [{"kind": "direction"}])

    def test_fresh_read_preserves_a_changed_wait_before_mutating(self) -> None:
        wait = waits.reviewed(evidence="Window", waiting_for="Window", ends_when="Date arrives", not_before="2026-10-30")
        item = issue("event:window", wait)
        action = actions(item)[0]
        newer = issue(
            "person:owner", waits.reviewed(evidence="New hold", waiting_for="Client acceptance", ends_when="Client accepts")
        )
        newer.update(url="https://github.com/owner/repo/issues/1", comments=[], provenance={"author": "owner"})
        run, post = Mock(return_value={"issue": newer}), Mock()
        self.assertFalse(
            sweep.apply(
                "owner/repo",
                1,
                action,
                trusted=TRUSTED,
                now=NOW,
                one=Mock(return_value=newer),
                listing=Mock(return_value=[]),
                run=run,
                post=post,
            )
        )
        post.assert_not_called()
        run.assert_not_called()

    def test_resolution_returns_an_event_actor_to_agent_without_losing_next_action(self) -> None:
        wait = waits.reviewed(evidence="Window", waiting_for="Window", ends_when="Date arrives", not_before="2026-10-30")
        item = issue("event:window", wait)
        item["body_recorded_at"] = (NOW - dt.timedelta(days=2)).isoformat()
        comments = [
            {
                "body": "## Current Status\nNext actor: event:window\nNext action: Review the newly supplied evidence.",
                "user": {"login": "bot"},
                "created_at": (NOW - dt.timedelta(days=1)).isoformat(),
            }
        ]
        action = actions(item, comments)[0]
        item.update(url="https://github.com/owner/repo/issues/1", comments=[], provenance={"author": "owner"})
        run, post = Mock(return_value={"issue": item}), Mock()
        self.assertTrue(
            sweep.apply(
                "owner/repo",
                1,
                action,
                trusted=TRUSTED,
                now=NOW,
                one=Mock(return_value=item),
                listing=Mock(return_value=comments),
                run=run,
                post=post,
            )
        )
        body = post.call_args.args[2]
        status = run.call_args_list[0].args[2]
        item["body"] = "## Current Status\n" + status + "\n" + waits.marker(wait)
        item["body_recorded_at"] = (NOW + dt.timedelta(days=1)).isoformat()
        self.assertEqual(sweep.next_actor.record(item, comments, TRUSTED)["actor"], "agent")
        self.assertEqual(sweep.next_actor.record(item, comments, TRUSTED)["action"], "Review the newly supplied evidence.")
        self.assertEqual(waits.current(item, [{"body": body, "user": {"login": "bot"}}], TRUSTED)["state"], "clear")

    def test_expired_parent_deadline_prevents_writes_and_restores_the_environment(self) -> None:
        writer = Mock()
        previous = str(time.time() - 1)
        with patch.dict(os.environ, {"GITHUB_RETRY_DEADLINE_AT": previous}):
            with self.assertRaises(ValueError), sweep.deadline(time.time() + 60):
                writer()
            self.assertEqual(os.environ["GITHUB_RETRY_DEADLINE_AT"], previous)
        writer.assert_not_called()
        with patch.dict(os.environ, {"GITHUB_RETRY_DEADLINE_AT": previous}), patch.object(sweep.subprocess, "run") as child:
            with self.assertRaises(ValueError):
                sweep.command("owner/repo", ["reconcile-status", "1", "--apply"])
        child.assert_not_called()

    def test_repeated_identical_trigger_gets_a_new_clear_after_its_new_record(self) -> None:
        event = {"version": 1, "kind": "date", "at": "2026-10-30", "evidence": "Same event recorded again"}
        cleared = events.clear("Daily sweep handled " + events.describe(event))
        clear_body = "Handled reopen event: " + events.describe(event) + ".\n\n" + events.marker(cleared)
        item = issue(state="closed")
        comments = [
            {"body": body, "user": {"login": "bot"}, "id": number}
            for number, body in enumerate((clear_body, events.marker(event)), 1)
        ]

        def create(_kind: str, _number: int, body: str, *, dedupe_body: bool = False, **_kwargs: object) -> None:
            if not dedupe_body or not any(row["body"] == body for row in comments):
                comments.append({"body": body, "user": {"login": "bot"}, "id": len(comments) + 1})

        action = actions(item, comments)[0]
        with (
            patch.object(sweep.github_comment, "comment", side_effect=create),
            patch.object(sweep.github_issue, "edit_issue"),
            patch.object(sweep.github_issue, "set_issue_state", side_effect=lambda *_args, **_kwargs: item.update(state="open")),
        ):
            sweep.apply(
                "owner/repo",
                1,
                action,
                trusted=TRUSTED,
                now=NOW,
                one=Mock(return_value=item),
                listing=Mock(return_value=comments),
                run=Mock(),
            )
        current = events.current(item, comments, TRUSTED)
        assert current is not None
        self.assertEqual(current["state"], "clear")
        item["state"] = "closed"
        self.assertEqual(actions(item, comments), [])

    def test_second_failure_creates_one_agent_issue_and_success_or_running_breaks_the_streak(self) -> None:
        pair = [
            {
                "id": number,
                "event": "schedule",
                "workflow_id": 7,
                "run_number": number,
                "status": "completed",
                "conclusion": "failure",
                "name": "Hygiene",
                "html_url": f"https://github.com/owner/repo/actions/runs/{number}",
            }
            for number in (2, 1)
        ]
        self.assertEqual(sweep.scheduled_failures(pair), [pair])
        for changed in ({"conclusion": "success"}, {"status": "in_progress"}, {"event": "push"}):
            sample = copy.deepcopy(pair)
            sample[0].update(changed)
            self.assertEqual(sweep.scheduled_failures(sample), [])
        run, post = Mock(return_value={"issue": {"number": 8}}), Mock()
        sweep.scheduled_issue("owner/repo", pair, candidates=[], run=run, post=post)
        body = run.call_args.args[2]
        first_title = run.call_args.args[1][1]
        second_pair = [{**row, "workflow_id": 8} for row in pair]
        sweep.scheduled_issue("owner/repo", second_pair, candidates=[], run=run, post=post)
        self.assertNotEqual(first_title, run.call_args.args[1][1])
        self.assertEqual(sweep.next_actor.record({"body": body, "user": {"login": "owner"}}, [], TRUSTED)["actor"], "agent")
        tracked = {"number": 8, "state": "open", "body": body}
        run.reset_mock()
        sweep.scheduled_issue("owner/repo", pair, candidates=[tracked], run=run, post=post)
        run.assert_not_called()
        post.assert_not_called()
        tracked["state"] = "closed"
        with patch.object(sweep.github_issue, "set_issue_state") as reopen:
            sweep.scheduled_issue("owner/repo", pair, candidates=[tracked], run=run, post=post)
            reopen.assert_not_called()
            pair[0] = {**pair[0], "id": 3, "run_number": 3}
            sweep.scheduled_issue("owner/repo", pair, candidates=[tracked], run=run, post=post)
            reopen.assert_called_once()
        self.assertIn(pair[0]["html_url"], post.call_args.args[2])
        tracked["_comments"] = [{"body": post.call_args.args[2]}]
        with patch.object(sweep.github_issue, "set_issue_state") as reopen:
            sweep.scheduled_issue("owner/repo", pair, candidates=[tracked], run=run, post=post)
            reopen.assert_not_called()

    def test_client_direction_is_not_routing_to_the_direction_session(self) -> None:
        for person in ("Justin's direction on pricing", "Client direction on the layout"):
            wait = waits.reviewed(evidence="Client choice", waiting_for=person, ends_when="Client replies")
            self.assertEqual(actions(issue("person:owner", wait)), [])

    def test_existing_dated_and_agent_waits_remain_visible_for_one_time_review(self) -> None:
        wait = {
            "version": 1,
            "state": "waiting",
            "evidence": "Historical record",
            "waits": [
                {"waiting_for": "Window until November 12, 2026", "ends_when": "Audit opens"},
                {"waiting_for": "Supervisor", "ends_when": "Completes routing"},
            ],
        }
        self.assertTrue(sweep.next_actor.migration("Next action: Supervisor routes source.", wait))
        self.assertFalse(sweep.next_actor.migration("Next actor: agent", waits.reviewed(evidence="Reviewed")))
        self.assertTrue(sweep.next_actor.migration("Next actor: event:window", waits.reviewed(evidence="Reviewed")))
        self.assertTrue(
            sweep.next_actor.migration("Next actor: agent\nParked until: 2026-10-30", waits.reviewed(evidence="Reviewed"))
        )

        for url, expected in (("https://github.com/owner/repo/pull/2", True), ("https://other.test/https://github.com/", False)):
            legacy = waits.reviewed(evidence="Source landing", waiting_for=url, ends_when="Source lands")
            self.assertEqual(bool(sweep.next_actor.migration("Next actor: agent", legacy)), expected)


if __name__ == "__main__":
    unittest.main()
