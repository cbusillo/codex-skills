# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML==6.0.3"]
# ///
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import io
import json
import tempfile
import threading
import unittest
from contextlib import closing, nullcontext, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import foreign_watch as watch
import routine_check

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
        "body": f"<!-- github-plan:claim {json.dumps(record)} -->",
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
        self.entry = {
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
        self.reader.get_json.return_value = {"number": 7}
        self.reader.paged_json.return_value = [claim()]
        self.persist = Mock()

    def test_event_entrypoint_sends_without_a_poll_and_preserves_sweep_watermark(self) -> None:
        self.args.event_payload = {"repository": {"full_name": REPO}, "action": "created", "comment": {"id": 42}}
        self.args.state.write_text(json.dumps({"watermarks": {REPO: STAMP}}))
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
        self.reader.paged_json.side_effect = [[claim()]]
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

    def test_delivery_reserved_before_send_and_only_once_per_source_edit(self) -> None:
        deliveries = {}
        seen = []

        def send(*_args: Any, **_kwargs: Any) -> None:
            seen.append(json.loads(json.dumps(deliveries)))

        with patch_attr(watch.review_notifications, "send", side_effect=send) as sender:
            for _ in range(2):
                watch.route_notices(self.args, [notice()], deliveries, self.reader, event=True, persist=self.persist)
            self.assertEqual(sender.call_count, 1)
            self.assertTrue(all(row["status"] == "attempted" for row in seen[0].values()))
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
                audit.assert_called_once()
            self.assertEqual(sender.call_count, 2)
            self.assertTrue(deliveries[watch.delivery_key(notice())]["hook_seen"])

    def test_sweep_miss_and_audit_uncertainty_never_repeats_write(self) -> None:
        deliveries = {}
        self.args.state.write_text(json.dumps({"watermarks": {REPO: STAMP}}))
        with (
            patch_attr(watch.review_notifications, "send"),
            patch.object(watch.subprocess, "run", return_value=Mock(returncode=1)) as post,
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
        self.args.state.write_text(json.dumps({"watermarks": {REPO: STAMP}}))
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

    def test_signed_http_event_durably_wakes_existing_watch_and_routes_canonical_source(self) -> None:
        self.args.webhook_port = 0
        secret = "synthetic-test-secret"
        wake = threading.Event()
        payload = {"repository": {"full_name": REPO}, "action": "created", "comment": {"id": 42, "body": "secret words"}}
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        with patch.dict(watch.os.environ, {"FOREIGN_WATCH_WEBHOOK_SECRET": secret}):
            server = watch.webhook_server(self.args, [REPO], wake)
        server.timeout = 3
        self.addCleanup(server.server_close)
        self.reader.get_json.return_value = {"html_url": URL, "updated_at": STAMP, "body": "canonical", "user": {"login": "client"}}
        for signed, expected in (("sha256=wrong", 401), (signature, 202), (signature, 202)):
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            with closing(http.client.HTTPConnection(*server.server_address, timeout=3)) as connection:
                connection.request(
                    "POST", "/github/events", body, {"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": signed}
                )
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
            thread.join(timeout=3)
        self.assertTrue(wake.is_set())
        state = self.args.state.read_text()
        self.assertNotIn(payload["comment"]["body"], state)
        self.assertEqual(len(json.loads(state)["pending_events"]), 1)
        with (
            patch_attr(watch, "GitHubReader", return_value=self.reader),
            patch_attr(watch, "recipient", return_value=(self.entry, False, "")),
            patch_attr(watch.review_notifications, "send") as sender,
            redirect_stdout(io.StringIO()),
        ):
            watch.drain_events(self.args, [REPO], watch.time.monotonic() + 30)
            sender.assert_called_once()
            self.assertIn(URL, sender.call_args.args[1])
        self.assertFalse(json.loads(self.args.state.read_text())["pending_events"])

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
