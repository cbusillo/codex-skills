# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
from __future__ import annotations

import argparse
import io
import json
import tempfile
import threading
import urllib.error
from datetime import timedelta
from email.message import Message
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, patch

import foreign_watch as watch
import routine_check
import attention_events
if TYPE_CHECKING:
    from skills.github.scripts.github_plan_claim import marker
    from skills.github.scripts.github_api import ApiResult
else:
    from github_plan_claim import marker
    from github_api import ApiResult

REPO = "director/project"
URL = f"https://github.com/{REPO}/issues/7#issuecomment-42"
STAMP = "2026-10-31T02:00:00Z"
SESSION = "00000000-0000-0000-0000-000000000001"
BRANCH = "work/message"


def patch_attr(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    return patch.object(target, name, *args, **kwargs)


def notice(url: str = URL) -> dict:
    return {
        "repository": REPO,
        "author": "client",
        "url": url,
        "updated_at": STAMP,
        "kind": "foreign",
        "excerpt": "Please help",
        "untrusted": True,
    }


def claim(identifier: int = 1, session: str = SESSION, branch: str = BRANCH) -> dict:
    record = {"worker": "test-worker", "session": session, "branch": branch, "claimed_at": "2026-10-31T01:00:00Z"}
    return {
        "id": identifier,
        "user": {"login": "agent[bot]"},
        "created_at": "2026-10-31T01:00:00Z",
        "updated_at": "2026-10-31T01:00:00Z",
        "body": marker(record),
    }


class MessageRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.args = argparse.Namespace(
            owner="director",
            own_login=["director", "agent[bot]"],
            launchplane_login=[],
            ledger=self.root / "ledger.json",
            notify_config=self.root / "services.json",
            state=self.root / "state.json",
            miss_issue="director/catalog#9",
            event="issue_comment",
            event_payload={},
        )
        self.entry: dict[str, Any] = {
            "repository": REPO,
            "supervisor_owned": True,
            "issue": 7,
            "branch": BRANCH,
            "session_id": SESSION,
            "harness": "codex",
            "transcript": str(self.root / "transcript.jsonl"),
        }
        self.args.ledger.write_text(json.dumps([self.entry]))
        self.supervisor = {"session_id": "00000000-0000-0000-0000-000000000002", "role": "supervisor"}
        self.enterContext(patch_attr(watch.services, "supervisor_recipient", return_value=self.supervisor))
        self.enterContext(patch_attr(watch.services, "supervisor_lock", side_effect=lambda _: nullcontext()))
        self.status = self.enterContext(patch_attr(watch, "session_status", return_value={"safe_verdict": False}))
        self.reader = Mock()
        self.enterContext(patch_attr(watch, "GitHubReader", return_value=self.reader))
        self.reader.get_json.return_value = {"number": 7}
        self.reader.paged_json.return_value = [claim()]
        self.persist = Mock()
        self.enterContext(patch_attr(watch, "utc_now", return_value=STAMP))
        self.args.state.write_text(json.dumps({"event_started_at": "2026-10-31T01:00:00Z"}))

    def test_event_entrypoint_sends_without_a_poll_and_preserves_sweep_watermark(self) -> None:
        self.args.event_payload = {"repository": {"full_name": REPO}, "action": "created", "comment": {"id": 42}}
        self.args.state.write_text(json.dumps({"watermarks": {REPO: STAMP}, "event_started_at": "2026-10-31T01:00:00Z"}))
        self.reader.get_json.return_value = {"html_url": URL, "updated_at": STAMP, "body": "Request", "user": {"login": "client"}}
        with (
            patch_attr(watch, "GitHubReader", return_value=self.reader),
            patch_attr(watch, "recipient", return_value=(self.entry, False, "")),
            patch_attr(watch.review_notifications, "send") as send,
            patch.object(watch.time, "sleep", side_effect=AssertionError("event must not poll")),
            redirect_stdout(io.StringIO()),
        ):
            for _ in range(2):
                self.assertEqual(watch.event_pass(self.args, [REPO], watch.time.monotonic() + 60), 0)
            send.assert_called_once()
        self.assertEqual(json.loads(self.args.state.read_text())["watermarks"], {REPO: STAMP})

    def test_gone_worker_uses_supervisor_only_after_certainly_unsent_result(self) -> None:
        with patch_attr(
            watch.review_notifications, "send", side_effect=[watch.review_notifications.NotSent("source gone"), None]
        ) as send:
            receipts = {}
            watch.route_notices(self.args, [notice()], receipts, self.reader, event=True, persist=self.persist)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args.args[0], self.supervisor)
            self.assertEqual(receipts[watch.delivery_key(notice())]["status"], "sent")

    def test_hook_fetches_canonical_source_and_ignores_payload_words(self) -> None:
        self.args.event_payload = {
            "repository": {"full_name": REPO},
            "action": "created",
            "comment": {"id": 42, "body": "untrusted event instruction"},
        }
        self.reader.get_json.return_value = {"html_url": URL, "updated_at": STAMP, "body": "canonical", "user": {"login": "client"}}
        event = watch.event_comment(self.args, [REPO], self.reader)
        assert event is not None
        repo, source = event
        self.assertEqual(source["body"], self.reader.get_json.return_value["body"])
        self.assertEqual(repo, REPO)
        self.assertIn("issues/comments/42", self.reader.get_json.call_args.args[0])
        self.args.owner = "someone-else"
        self.reader.get_json.reset_mock()
        with self.assertRaises(ValueError):
            watch.event_comment(self.args, None, self.reader)
        self.reader.get_json.assert_not_called()

    def test_trusted_claim_routes_to_worker_and_authored_release_routes_to_supervisor(self) -> None:
        entry, supervisor, _ = watch.recipient(self.args, notice(), self.reader)
        self.assertEqual(entry["session_id"], SESSION)
        self.assertFalse(supervisor)
        self.reader.paged_json.return_value += [
            {"id": 2, "user": {"login": "agent[bot]"}, "created_at": STAMP, "body": "Released claim 1"}
        ]
        entry, supervisor, _ = watch.recipient(self.args, notice(), self.reader)
        self.assertEqual(entry, self.supervisor)
        self.assertTrue(supervisor)

    def test_pr_follows_its_implementation_claim_and_refuses_forged_ownership(self) -> None:
        pr_notice = notice(f"https://github.com/{REPO}/pull/10#discussion_r42")
        self.reader.get_json.side_effect = [{"pull_request": {}}, {"head": {"ref": BRANCH}, "body": "Closes #7"}]
        self.reader.paged_json.side_effect = [[claim()], []]
        self.assertEqual(watch.recipient(self.args, pr_notice, self.reader)[0]["session_id"], SESSION)
        self.reader.get_json.side_effect = None
        self.reader.paged_json.side_effect = None
        self.reader.get_json.return_value = {}
        self.reader.paged_json.return_value = [{**claim(), "user": {"login": "outside"}}]
        self.assertTrue(watch.recipient(self.args, notice(), self.reader)[1])

    def test_finished_worker_and_ambiguous_claims_go_to_supervisor(self) -> None:
        self.status.return_value = {"safe_verdict": True}
        self.assertTrue(watch.recipient(self.args, notice(), self.reader)[1])
        self.status.return_value = {"safe_verdict": False}
        self.reader.paged_json.return_value += [claim(2, "another-session")]
        self.assertTrue(watch.recipient(self.args, notice(), self.reader)[1])

    def test_pr_only_claim_routes_without_an_implementation_issue_in_the_ledger(self) -> None:
        entry = {key: value for key, value in self.entry.items() if key != "issue"}
        self.args.ledger.write_text(json.dumps([entry]))
        self.reader.get_json.side_effect = [{"pull_request": {}}, {"head": {"ref": BRANCH}, "body": ""}]
        self.assertEqual(watch.recipient(self.args, notice(f"https://github.com/{REPO}/pull/10#discussion_r42"), self.reader)[0]["session_id"], SESSION)

    def test_delivery_reserved_before_send_and_only_once_per_source_edit(self) -> None:
        deliveries = {}
        seen = []

        def send(*_args: Any, **_kwargs: Any) -> None:
            seen.append(json.loads(json.dumps(deliveries)))

        with patch_attr(watch.review_notifications, "send", side_effect=send) as sender:
            for _ in range(2):
                watch.route_notices(self.args, [notice()], deliveries, self.reader, event=True, persist=self.persist)
            self.assertEqual(sender.call_count, 1)
            self.assertIn(watch.delivery_key(notice()), seen[0])
            changed = {**notice(), "updated_at": "2026-10-31T02:01:00Z"}
            watch.route_notices(self.args, [changed], deliveries, self.reader, event=True, persist=self.persist)
            self.assertEqual(sender.call_count, 2)

    def test_permission_refusal_is_spent_and_sweep_records_it_without_redelivery(self) -> None:
        deliveries = {}
        with (
            patch_attr(watch.review_notifications, "send", side_effect=PermissionError("denied")) as sender,
            patch_attr(watch, "audit_miss") as audit,
        ):
            watch.route_notices(self.args, [notice()], deliveries, self.reader, event=True, persist=self.persist)
            watch.route_notices(self.args, [notice()], deliveries, self.reader, event=False, persist=self.persist)
            self.assertEqual(sender.call_count, 1)
            audit.assert_called_once()

    def test_certainly_unsent_busy_worker_retries_and_success_preserves_hook_receipt(self) -> None:
        deliveries = {}
        with patch_attr(
            watch.review_notifications, "send", side_effect=[watch.review_notifications.NotSent("busy", needs_attention=False), None]
        ) as sender:
            watch.route_notices(self.args, [notice()], deliveries, self.reader, event=True, persist=self.persist)
            self.assertNotEqual(deliveries[watch.delivery_key(notice())]["status"], "sent")
            with patch_attr(watch, "audit_miss") as audit:
                watch.route_notices(self.args, [notice()], deliveries, self.reader, event=False, persist=self.persist)
                audit.assert_not_called()
            self.assertEqual(sender.call_count, 2)
            self.assertTrue(deliveries[watch.delivery_key(notice())]["hook_seen"])

    def test_sweep_miss_and_audit_uncertainty_never_repeats_write(self) -> None:
        deliveries = {}
        self.args.state.write_text(json.dumps({"watermarks": {REPO: STAMP}, "event_started_at": "2026-10-31T01:00:00Z"}))
        with (
            patch_attr(watch.review_notifications, "send"),
            patch.object(watch.subprocess, "run", return_value=Mock(returncode=1, stdout="")) as post,
        ):
            for _ in range(2):
                watch.route_notices(self.args, [notice()], deliveries, self.reader, event=False, persist=self.persist)
            self.assertEqual(post.call_count, 1)
            receipt = deliveries[watch.delivery_key(notice())]
            self.assertTrue(receipt.get("audit_error"))
        watch.save_state(self.args.state, {REPO: STAMP}, deliveries=deliveries)
        watch.save_state(self.args.state, {REPO: STAMP})
        self.assertEqual(watch.load_deliveries(self.args.state), deliveries)

    def test_supervisor_lock_contention_remains_retryable_before_send(self) -> None:
        receipts = {}
        with (
            patch_attr(watch, "recipient", return_value=(self.supervisor, True, "owner_not_live")),
            patch_attr(watch.services, "supervisor_lock", side_effect=ValueError("binding in progress")),
            patch_attr(watch.review_notifications, "send") as send,
        ):
            watch.route_notices(self.args, [notice()], receipts, self.reader, event=True, persist=self.persist)
        send.assert_not_called()
        with (
            patch_attr(watch, "recipient", return_value=(self.supervisor, True, "owner_not_live")),
            patch_attr(watch.review_notifications, "send") as send,
            patch_attr(watch, "audit_miss"),
        ):
            watch.route_notices(self.args, [notice()], receipts, self.reader, event=False, persist=self.persist)
        send.assert_called_once()

    def test_audit_timeout_keeps_rest_of_batch_and_private_links_out_of_issue(self) -> None:
        receipts = {}
        batch = [notice(), notice(URL.replace("42", "43"))]
        with (
            patch_attr(watch.review_notifications, "send") as send,
            patch.object(watch.subprocess, "run", side_effect=watch.subprocess.TimeoutExpired("audit", 1)) as post,
        ):
            watch.route_notices(self.args, batch, receipts, self.reader, event=False, persist=self.persist)
        self.assertEqual(send.call_count, len(batch))
        for call in post.call_args_list:
            self.assertNotIn(REPO, call.kwargs["input"])
            self.assertNotIn(URL, call.kwargs["input"])
        self.assertTrue(all(item["routing"].get("audit_error") for item in batch))

    def test_sweep_checkpoint_before_batch_does_not_consume_undelivered_notices(self) -> None:
        self.args.state.write_text(json.dumps({"watermarks": {REPO: STAMP}, "event_started_at": "2026-10-31T01:00:00Z"}))
        self.args.since = None
        self.args.once = True
        self.args.interval = 1
        newer = "2026-10-31T03:00:00Z"

        def interrupt(*_args: Any, persist: Any, **_kwargs: Any) -> None:
            persist()
            raise RuntimeError("interrupted during delivery")

        with (
            patch_attr(watch, "GitHubReader", return_value=self.reader),
            patch_attr(watch, "scan", return_value=([notice()], [], {REPO: newer})),
            patch_attr(watch, "scan_mentions", return_value=([], [], [], {REPO: newer}, {})),
            patch_attr(watch, "route_notices", side_effect=interrupt),
        ):
            with self.assertRaises(RuntimeError):
                watch.watch(self.args, [REPO], STAMP, watch.time.monotonic() + 60)
        self.assertEqual(json.loads(self.args.state.read_text())["watermarks"][REPO], STAMP)

    def test_radar_routing_error_preserves_collected_evidence(self) -> None:
        report = {"attention": [{"repo": REPO, "comment_url": URL, "updated_at": STAMP, "author": "client"}]}
        self.args.foreign_state = self.args.state
        with patch_attr(watch, "watch_lock", side_effect=ValueError("locked")):
            watch.route_report(self.args, report, self.reader, [])
        self.assertEqual(report["attention"][0]["comment_url"], URL)
        self.assertTrue(report["routing_errors"])

    def test_intake_progresses_during_delivery_and_keeps_a_newer_edit(self) -> None:
        hint = {"repository": REPO, "source_kind": "comment", "source_id": 42, "request_number": 7, "delivery_id": "one"}
        watch.enqueue_event(self.args, [REPO], hint)
        self.reader.get_json.return_value = {"html_url": URL, "updated_at": STAMP, "body": "canonical", "user": {"login": "client"}}
        entered = threading.Event()
        release = threading.Event()

        def send(*_args: Any, **_kwargs: Any) -> None:
            entered.set()
            release.wait(3)

        with (
            patch_attr(watch, "GitHubReader", return_value=self.reader),
            patch_attr(watch, "recipient", return_value=(self.entry, False, "")),
            patch_attr(watch.review_notifications, "send", side_effect=send),
            redirect_stdout(io.StringIO()),
        ):
            thread = threading.Thread(target=watch.drain_events, args=(self.args, [REPO], watch.time.monotonic() + 30))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                watch.enqueue_event(self.args, [REPO], {**hint, "delivery_id": "two"})
            finally:
                release.set()
                thread.join(3)
        pending = json.loads(self.args.state.read_text())["pending_events"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(next(iter(pending.values()))["delivery_id"], "two")

    def test_uncertain_native_send_is_confirmed_by_its_exact_user_message(self) -> None:
        receipts = {}
        with patch_attr(watch.review_notifications, "send", side_effect=ValueError("unknown send")) as send:
            watch.route_notices(self.args, [notice()], receipts, self.reader, event=True, persist=self.persist)
            receipt = receipts[watch.delivery_key(notice())]
            Path(self.entry["transcript"]).write_text(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": receipt["delivery_message"]}}) + "\n")
            self.status.return_value = {"safe_verdict": False, "transcript": self.entry["transcript"]}
            watch.route_notices(self.args, [notice()], receipts, self.reader, event=True, persist=self.persist)
        send.assert_called_once()
        self.assertEqual(receipt["status"], "sent")

    def test_misses_distinguish_downtime_and_subscribed_intervals_without_historical_noise(self) -> None:
        self.args.state.write_text(json.dumps({"event_started_at": STAMP, "event_connections": [{"start": STAMP, "end": STAMP}], "event_transport": "disconnected"}))
        self.assertFalse(watch.missed_event_cause(self.args, {**notice(), "updated_at": "2026-10-30T00:00:00Z"}))
        self.assertNotEqual(watch.missed_event_cause(self.args, notice()), watch.missed_event_cause(self.args, {**notice(), "updated_at": "2026-10-31T03:00:00Z"}))

    def test_deleted_source_is_removed_and_provider_cooldown_survives_restart(self) -> None:
        hint = {"repository": REPO, "source_kind": "comment", "source_id": 42, "request_number": 7, "delivery_id": "one"}
        watch.enqueue_event(self.args, [REPO], hint)
        gone = watch.GitHubReadError("gone", result=Mock(spec=ApiResult, status=404), diagnostics={})
        with patch_attr(watch, "event_pass", side_effect=gone):
            watch.drain_events(self.args, [REPO], watch.time.monotonic() + 30)
        self.assertFalse(json.loads(self.args.state.read_text())["pending_events"])
        watch.enqueue_event(self.args, [REPO], hint)
        retry_at = watch.time.time() + 300
        limited = watch.GitHubReadError("cooldown", result=Mock(spec=ApiResult, status=429, failure=Mock(retryable=True)), diagnostics={"requests": [{"retryAt": retry_at}]})
        with patch_attr(watch, "event_pass", side_effect=limited) as event:
            watch.drain_events(self.args, [REPO], watch.time.monotonic() + 30)
            watch.drain_events(self.args, [REPO], watch.time.monotonic() + 30)
        event.assert_called_once()
        self.assertEqual(json.loads(self.args.state.read_text())["next_event_read_at"], retry_at)

    def test_opening_event_and_sweep_use_actual_body_edit_and_equivalent_timestamps(self) -> None:
        source = {
            "html_url": URL.split("#")[0],
            "title": "Request",
            "body": "edited",
            "node_id": "node",
            "created_at": STAMP,
            "updated_at": "2026-10-31T08:00:00Z",
            "user": {"login": "client", "type": "User"},
        }
        self.args.event = "issues"
        self.args.event_payload = {
            "repository": {"full_name": REPO},
            "action": "edited",
            "issue": {"number": 7},
            "changes": {"body": {}},
        }
        self.reader.get_json.return_value = source
        self.reader.graphql_json.return_value = Mock(ok=True, body={"data": {"node": {"lastEditedAt": "2026-10-31T02:10:00Z"}}})
        event = watch.event_comment(self.args, [REPO], self.reader)
        self.assertIsNotNone(event)
        assert event is not None
        self.reader.paged_json.return_value = [source]
        [sweep] = watch.read_opened(self.reader, REPO, STAMP)
        self.assertEqual(
            watch.delivery_key({"url": event[1]["html_url"], "updated_at": event[1]["updated_at"]}),
            watch.delivery_key({"url": sweep["html_url"], "updated_at": sweep["updated_at"]}),
        )
        self.assertEqual(event[1]["updated_at"], self.reader.graphql_json.return_value.body["data"]["node"]["lastEditedAt"])

    def test_radar_skips_answered_rows_and_does_not_audit_activation_history(self) -> None:
        self.args.foreign_state = self.args.state
        rows = [{"repo": REPO, "comment_url": URL, "updated_at": STAMP, "author": "client", "later_reply_url": "reply"}, {"repo": REPO, "comment_url": URL.replace("42", "43"), "updated_at": STAMP, "author": "client", "comment_kind": "issue_body", "thread_has_replies": True}]
        with patch_attr(watch.review_notifications, "send") as send:
            watch.route_report(self.args, {"attention": rows}, self.reader, self.args.own_login)
        send.assert_not_called()
        self.args.state.write_text(json.dumps({"event_started_at": "2026-10-31T03:00:00Z"}))
        with patch_attr(watch, "recipient", return_value=(self.supervisor, True, "owner_finished")), patch_attr(watch.review_notifications, "send"), patch_attr(watch, "audit_miss") as audit:
            watch.route_notices(self.args, [notice()], {}, self.reader, event=False, persist=self.persist)
        audit.assert_not_called()

    def test_pending_pointer_keeps_dispatch_records_and_unrelated_watermarks(self) -> None:
        other = "director/other"
        self.args.since, self.args.once, self.args.interval = None, True, 1
        initial = "2026-10-31T01:00:00Z"
        batch = [notice(), {**notice(), "repository": other, "url": URL.replace(REPO, other), "kind": "launchplane", "change_request": {"record_id": "request"}}]
        self.args.state.write_text(json.dumps({"watermarks": {REPO: initial, other: initial}}))
        with patch_attr(watch, "scan", return_value=(batch, [], {REPO: STAMP, other: STAMP})), patch_attr(watch, "scan_mentions", return_value=([], [], [], {REPO: STAMP, other: STAMP}, {"seen": STAMP})), patch_attr(watch.review_notifications, "send", side_effect=watch.review_notifications.NotSent("busy", needs_attention=False)), redirect_stdout(io.StringIO()):
            watch.watch(self.args, [REPO, other], initial, watch.time.monotonic() + 10)
        state = json.loads(self.args.state.read_text())
        self.assertEqual(state["watermarks"][REPO], initial)
        self.assertEqual(state["watermarks"][other], STAMP)
        self.assertIn("request", state["change_requests_dispatched"])
        self.assertIn("seen", state["mention_seen"])

    def test_observed_connection_history_survives_reconnects_and_never_extends_after_observation(self) -> None:
        initial = watch.timestamp(STAMP)
        for index in range(80):
            at = (initial + timedelta(minutes=index)).isoformat()
            with patch_attr(watch, "utc_now", return_value=at):
                opened = watch.connection_observation(self.args, None, connected=True)
                watch.connection_observation(self.args, opened, connected=False)
        self.assertEqual(watch.missed_event_cause(self.args, notice()), "not_notified_while_subscribed")
        before = json.loads(self.args.state.read_text())["event_connections"][-1]["end"]
        with patch_attr(watch, "utc_now", return_value="2026-10-31T06:00:00Z"):
            watch.connection_observation(self.args, None, connected=False)
        self.assertEqual(json.loads(self.args.state.read_text())["event_connections"][-1]["end"], before)

    def test_unstarted_or_failed_event_stream_backs_off_and_stops_on_configuration_error(self) -> None:
        stop, wake = threading.Event(), threading.Event()
        fake_stop = Mock(wraps=stop)
        fake_stop.wait.side_effect = lambda _: stop.set()
        with patch_attr(attention_events, "messages", return_value=iter([{"type": "disconnected"}])):
            watch.receive_events(self.args, [REPO], wake, fake_stop)
        fake_stop.wait.assert_called_once()
        self.assertTrue(wake.is_set())
        stop.clear()
        with patch_attr(attention_events, "messages", side_effect=attention_events.ConfigurationError("missing configuration")), redirect_stdout(io.StringIO()):
            watch.receive_events(self.args, [REPO], wake, stop)
        self.assertTrue(stop.is_set())
        self.assertEqual(json.loads(self.args.state.read_text())["event_transport"], "disconnected")
        stop.clear()
        with patch_attr(attention_events, "messages", side_effect=RuntimeError("unexpected source failure")):
            with self.assertRaises(RuntimeError):
                watch.receive_events(self.args, [REPO], wake, stop)
        self.assertTrue(stop.is_set())

    def test_attention_client_frames_messages_bounds_lines_and_preserves_auth_refusal(self) -> None:
        settings = {"service_url": "https://service.example.test", "token": "synthetic"}
        with patch_attr(attention_events.operator, "resolve_settings", return_value=settings), patch_attr(attention_events.operator, "safe_urlopen", return_value=io.BytesIO(b'{"type":"connected"}\n')):
            self.assertEqual(list(attention_events.messages(self.args)), [{"type": "connected"}])
        with patch_attr(attention_events.operator, "resolve_settings", return_value=settings), patch_attr(attention_events.operator, "safe_urlopen", side_effect=urllib.error.HTTPError(settings["service_url"], 403, "denied", Message(), None)):
            with self.assertRaises(PermissionError):
                list(attention_events.messages(self.args))
        with patch_attr(attention_events.operator, "resolve_settings", return_value=settings), patch_attr(attention_events.operator, "safe_urlopen", return_value=io.BytesIO(b'x' * (attention_events.MAX_EVENT_LINE_BYTES + 1))):
            with self.assertRaises(ValueError):
                list(attention_events.messages(self.args))

    def test_routine_reader_routes_inline_review_without_new_collector(self) -> None:
        row = {
            "repo": REPO,
            "comment_url": f"https://github.com/{REPO}/pull/10#discussion_r42",
            "updated_at": STAMP,
            "author": "client",
            "body_excerpt": "Please revise",
            "comment_kind": "review_comment",
        }
        self.args.foreign_state = self.args.state
        with (
            patch_attr(watch, "recipient", return_value=(self.entry, False, "")),
            patch_attr(watch.review_notifications, "send") as sender,
            patch_attr(watch, "audit_miss"),
        ):
            watch.route_report(
                self.args, {"attention": [row, {**row, "later_reply_url": "reply"}]}, self.reader, ["director", "agent[bot]"]
            )
            sender.assert_called_once()
            self.assertIn(row["comment_url"], sender.call_args.args[1])
        digest = routine_check.digest({"comments": {"attention": [{**row, "routing": {"status": "attempted", "cause": "denied"}}]}})
        self.assertIn("denied", digest)


if __name__ == "__main__":
    unittest.main()
