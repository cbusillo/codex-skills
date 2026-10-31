# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
from __future__ import annotations

import copy
import datetime as dt
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

    def test_direction_uses_only_current_actor_or_wait_and_preserves_decided_history(self) -> None:
        historical = {"user": {"login": "bot"}, "body": "For the direction session: decide the design."}
        self.assertEqual([action["kind"] for action in actions(issue("agent:direction"))], ["direction", "nudge"])
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
            sweep.apply("owner/repo", 1, action, trusted=TRUSTED, now=NOW, one=Mock(), listing=Mock(), run=run, post=post)
        )
        post.assert_not_called()
        self.assertEqual(run.call_count, 1)

    def test_resolution_returns_an_event_actor_to_agent_without_losing_next_action(self) -> None:
        wait = waits.reviewed(evidence="Window", waiting_for="Window", ends_when="Date arrives", not_before="2026-10-30")
        item = issue("event:window", wait)
        action = actions(item)[0]
        item.update(url="https://github.com/owner/repo/issues/1", comments=[], provenance={"author": "owner"})
        run, post = Mock(return_value={"issue": item}), Mock()
        self.assertTrue(
            sweep.apply("owner/repo", 1, action, trusted=TRUSTED, now=NOW, one=Mock(), listing=Mock(), run=run, post=post)
        )
        body = post.call_args.args[2]
        self.assertEqual(sweep.next_actor.read(sweep.next_actor.record({**item, "body": body}, [], TRUSTED)["status"]), "agent")
        self.assertIn(sweep.next_actor.record(item, [], TRUSTED)["action"], body)
        self.assertEqual(waits.current(item, [{"body": body, "user": {"login": "bot"}}], TRUSTED)["state"], "clear")

    def test_second_failure_creates_one_agent_issue_and_success_or_running_breaks_the_streak(self) -> None:
        pair = [
            {
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
        self.assertEqual(sweep.next_actor.record({"body": body, "user": {"login": "owner"}}, [], TRUSTED)["actor"], "agent")
        tracked = {"number": 8, "state": "open", "body": body}
        run.reset_mock()
        sweep.scheduled_issue("owner/repo", pair, candidates=[tracked], run=run, post=post)
        run.assert_not_called()
        post.assert_not_called()
        tracked["state"] = "closed"
        with patch.object(sweep.subprocess, "run", return_value=Mock(returncode=0)) as reopen:
            sweep.scheduled_issue("owner/repo", pair, candidates=[tracked], run=run, post=post)
        self.assertIn("reopen", reopen.call_args.args[0])
        self.assertIn(pair[0]["html_url"], post.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
