# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Reopen event boundaries and the wait parser's shared authority contract."""
import datetime as dt
import json
import unittest
from unittest.mock import Mock

import github_plan_trigger as trigger
import github_plan_wait as wait

NOW = dt.datetime(2026, 10, 16, 12, tzinfo=dt.UTC)


def record(kind, **fields):
    return {"version": 1, "kind": kind, "evidence": "Director's recorded deferral", **fields}


class TriggerTests(unittest.TestCase):
    def test_record_text_stays_hidden_and_legacy_records_still_read(self) -> None:
        event = record("recurrence", symptom="failure --> @someone --!>")
        encoded = trigger.marker(event)
        self.assertEqual(encoded.count(">"), 1)
        legacy = trigger.PREFIX + json.dumps(event, sort_keys=True) + " -->"
        for body in (encoded, legacy):
            with self.subTest(body=body):
                issue = {"user": {"login": "owner"}, "body": body}
                self.assertEqual(trigger.current(issue, [], {"owner"}), event)

    def test_date_fires_at_boundary_and_not_before_including_timezone(self):
        for at, expected in (("2026-10-16", True), (NOW.isoformat(), True),
                             ("2026-10-16T09:00:00-04:00", False), ("2026-10-17", False)):
            with self.subTest(at=at):
                self.assertEqual(trigger.fired(record("date", at=at), now=NOW, one=Mock(), listing=Mock()), expected)

    def test_issue_close_and_actual_pr_merge_are_distinct(self):
        for kind, path in (("issue_closed", "issues"), ("pr_merged", "pull")):
            for state, merged, expected in (("open", None, False), ("closed", None, kind == "issue_closed"),
                                            ("closed", NOW.isoformat(), True)):
                one = Mock(return_value={"state": state, "merged_at": merged})
                with self.subTest(kind=kind, state=state, merged=merged):
                    self.assertEqual(trigger.fired(record(kind, url=f"https://github.com/owner/repo/{path}/2"),
                                                  now=NOW, one=one, listing=Mock()), expected)
                    self.assertEqual(one.call_args.args[0], f"repos/owner/repo/{'issues' if kind == 'issue_closed' else 'pulls'}/2")

    def test_only_published_stable_releases_at_or_above_minimum_fire(self):
        event = record("release_published", repo="owner/repo", minimum_tag="v1.9.0")
        for tag, draft, prerelease, published, expected in (
                ("v1.8.99", False, False, NOW.isoformat(), False),
                ("v1.9", False, False, NOW.isoformat(), True),
                ("v1.10.0", False, False, NOW.isoformat(), True),
                ("v2.0.0", True, False, None, False),
                ("v2.0.0", False, True, NOW.isoformat(), False),
                ("v2.0.0-rc.1", False, False, NOW.isoformat(), False)):
            with self.subTest(tag=tag, draft=draft, prerelease=prerelease):
                listing = Mock(return_value=[{"tag_name": tag, "draft": draft, "prerelease": prerelease, "published_at": published}])
                self.assertEqual(trigger.fired(event, now=NOW, one=Mock(), listing=listing), expected)

    def test_advisory_needs_patch_for_each_affected_range_and_is_not_withdrawn(self):
        event = record("advisory_fixed", ghsa_id="GHSA-cfgh-jmpq-rvwx")
        for patches, withdrawn, expected in (([], None, False), ([None], None, False),
                (["1.2.3", None], None, False), (["1.2.3", "2.0.1"], None, True),
                (["1.2.3"], NOW.isoformat(), False)):
            with self.subTest(patches=patches, withdrawn=withdrawn):
                self.assertEqual(trigger.fired(event, now=NOW, listing=Mock(), one=Mock(return_value={
                    "withdrawn_at": withdrawn, "vulnerabilities": [{"first_patched_version": p} for p in patches]})), expected)

    def test_recurrence_never_reads_or_machine_checks_the_symptom(self):
        one, listing = Mock(), Mock()
        self.assertFalse(trigger.fired(record("recurrence", symptom="exact failure: endpoint returned false"),
                                       now=NOW, one=one, listing=listing))
        one.assert_not_called()
        listing.assert_not_called()

    def test_bad_input_and_missing_event_facts_are_not_silently_unfired(self):
        for event in (record("date", at="2026-10-16T12:00:00"), record("date", at="not a date"),
                      record("issue_closed", url="https://github.com/o/r/pull/2"),
                      record("issue_closed", url="https://github.com/o/r/issues/2#issuecomment-1"),
                      record("release_published", repo="o/r", minimum_tag="latest"),
                      record("advisory_fixed", ghsa_id="invalid"), record("recurrence", symptom=" ")):
            with self.subTest(event=event), self.assertRaises(ValueError):
                trigger.validate(event)
        for event in (record("issue_closed", url="https://github.com/o/r/issues/2"),
                      record("pr_merged", url="https://github.com/o/r/pull/2"),
                      record("advisory_fixed", ghsa_id="GHSA-cfgh-jmpq-rvwx")):
            with self.subTest(event=event), self.assertRaises(ValueError):
                trigger.fired(event, now=NOW, one=Mock(return_value={}), listing=Mock())

    def test_shared_authority_rules_examples_and_waits_remain_independent(self):
        earlier = record("date", at="2026-10-01")
        later = record("date", at="2026-11-01")
        issue = {"user": {"login": "owner"}, "body": trigger.marker(earlier)}
        def comment(body, author="owner"):
            return {"user": {"login": author}, "body": body}
        for body in ("> " + trigger.marker(later), "```\n" + trigger.marker(later) + "\n```",
                     "<details>\n" + trigger.marker(later) + "\n</details>"):
            self.assertEqual(trigger.current(issue, [comment(body)], {"owner"}), earlier)
        self.assertEqual(trigger.current(issue, [comment(trigger.marker(later), "outsider")], {"owner"}), earlier)
        self.assertEqual(trigger.current(issue, [comment(trigger.marker(later))], {"owner"}), later)
        clear = wait.reviewed(evidence="No hold")
        issue["body"] += "\n" + wait.marker(clear)
        self.assertEqual(wait.current(issue, [], {"owner"}), clear)
        self.assertEqual(trigger.current(issue, [], {"owner"}), earlier)
        self.assertIsNone(trigger.current({}, [], {"owner"}))
        for body in (trigger.PREFIX + "{} -->", trigger.marker(earlier) + "\n" + trigger.marker(later)):
            with self.assertRaises(ValueError): trigger.current(issue, [comment(body)], {"owner"})


    def test_clear_record_supersedes_a_fired_event_without_machine_reads(self):
        old = {"user": {"login": "owner"}, "body": trigger.marker(record("date", at="2020-01-01"))}
        cleared = {"version": 1, "state": "clear", "evidence": "Work handled after reopening"}
        comments = [{"user": {"login": "owner"}, "body": trigger.marker(cleared)}]
        selected = trigger.current(old, comments, {"owner"})
        self.assertEqual(selected, cleared)
        assert selected is not None
        one, listing = Mock(), Mock()
        self.assertFalse(trigger.fired(selected, now=NOW, one=one, listing=listing))
        one.assert_not_called()
        listing.assert_not_called()


if __name__ == "__main__":
    unittest.main()
