#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Exercise native-session closeout against disposable GitHub and transcript records."""
import contextlib
import io
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

import check_claims as helper

CURRENT_NATIVE_PENDING = helper.native_ownership.pending_ownership

ACTOR = "fixture[bot]"
SESSION = "native-session"
REPO = "test/a"
SOURCE = {"worker": "worker", "session": SESSION, "branch": "work/task", "claimed_at": "2026-01-01T00:00:00Z"}


def comment(identifier, body, actor=ACTOR, at="2026-01-01T00:01:00Z"):
    return {"id": identifier, "body": body, "user": {"login": actor}, "created_at": at, "updated_at": at}


def pull(number=2, branch=SOURCE["branch"], **changes):
    return {"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}",
            "body": "Fixes #1", "state": "open", "user": {"login": ACTOR},
            "head": {"ref": branch, "repo": {"full_name": REPO}},
            "base": {"repo": {"full_name": REPO}}, **changes}


class ClaimCloseoutTests(unittest.TestCase):

    def test_unrelated_helper_issue_number_is_not_a_claim_receipt(self):
        from test_claim_receipts import history, SESSION as native_session
        for harness in ("codex", "claude"):
            rows = history(harness, {"operation": "github.plan.update_section", "issue": 2214, "ok": True})
            for row in rows:
                row["timestamp"] = SOURCE["claimed_at"]
            entry = {"session_id": native_session, "harness": harness, "_transcript_records": rows}
            self.comments = [comment(10, helper.claim.marker({**SOURCE, "session": native_session}))]
            with patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
                self.assertFalse(helper.check(self.reader, [], entry, ACTOR)["claims_clear"])
                self.comments.append(comment(11, "Released claim 10\nSession: " + native_session,
                                             at="2026-01-01T00:02:00Z"))
                self.assertTrue(helper.check(self.reader, [], entry, ACTOR)["claims_clear"])

    def test_native_family_reuses_receipt_parsing_without_reusing_a_later_snapshot(self):
        from test_claim_receipts import history, claim as native_claim, SESSION as native_session
        from test_claim_history import records
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                rows = history(harness, native_claim(REPO))
                nodes = [{"session_id": native_session, "harness": harness, "records": rows}]
                nodes.extend({"session_id": f"child-{i}", "harness": harness,
                              "records": records(harness, f"child-{i}")}
                             for i in range(4))
                for node in nodes:
                    node["transcript"] = f"/fixture/{node['session_id']}.jsonl"
                    node["digest"] = helper.claim_history.fingerprint(node["records"])
                entry = {"session_id": native_session, "harness": harness, "_claim_histories": nodes}
                self.comments = [comment(10, helper.claim.marker({**SOURCE, "session": native_session}))]
                original = helper.claim_receipts.outputs
                with patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
                    with patch.object(helper.claim_receipts, "outputs", wraps=original) as single_parse:
                        helper.check(self.reader, [], {**entry, "_claim_histories": nodes[:1]}, ACTOR)
                    single_check_count = sum(call.args[0] is rows for call in single_parse.call_args_list)
                    with patch.object(helper.claim_receipts, "outputs", wraps=original) as single_parse:
                        helper.claim_receipts.acceptance(rows, harness, native_session, histories=nodes[:1])
                    single_acceptance_count = sum(call.args[0] is rows for call in single_parse.call_args_list)
                    with patch.object(helper.claim_receipts, "outputs", wraps=original) as parse:
                        held = helper.check(self.reader, [], entry, ACTOR)
                    self.assertFalse(held["claims_clear"], held)
                    # Parsing work scales with histories, not histories times identities.
                    check_parse_count = sum(call.args[0] is rows for call in parse.call_args_list)
                    self.comments.append(comment(11, "Released claim 10", at="2026-01-01T00:02:00Z"))
                    released = helper.check(self.reader, [], entry, ACTOR)
                    self.assertTrue(released["claims_clear"], released)
                    with patch.object(helper.claim_receipts, "outputs", wraps=original) as parse:
                        verdict = helper.claim_receipts.acceptance(rows, harness, native_session, histories=nodes)
                    self.assertFalse(verdict["clear"], verdict)
                    self.assertEqual(check_parse_count, single_check_count)
                    self.assertEqual(sum(call.args[0] is rows for call in parse.call_args_list), single_acceptance_count)
                shared_counts = []
                self.comments = [comment(10, helper.claim.marker({**SOURCE, "session": native_session})),
                                 comment(11, "Released claim 10", at="2026-01-01T00:02:00Z")]
                parent = history(harness, native_claim(REPO))
                signature = tuple(map(id, parent))
                for count in (1, 2):
                    child = history(harness, *[{} for _ in range(count)])
                    child_session = "child" if harness == "codex" else native_session
                    if harness == "codex":
                        child[0]["payload"]["id"] = child_session
                    for index in range(1, 2 * count, 2):
                        command = "gh pr create --repo test/a --head earlier-worker"
                        if harness == "codex":
                            child[index]["payload"]["arguments"] = json.dumps({"cmd": command})
                        else:
                            child[index]["message"]["content"][0]["input"] = {"command": command}
                    family = [
                        {"session_id": native_session, "harness": harness, "records": parent},
                        {"session_id": child_session, "harness": harness, "records": child,
                         "transcript": "/fixture/child.jsonl", "digest": helper.claim_history.fingerprint(child),
                         "preceding_histories": [{"harness": harness, "records": parent}]}]
                    parsed_shared = []
                    def parsing(records, family_harness, **kwargs):
                        if tuple(map(id, records)) == signature:
                            parsed_shared.append(records)
                        return original(records, family_harness, **kwargs)
                    with patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])), \
                            patch.object(helper.claim_receipts, "outputs", side_effect=parsing):
                        report = helper.check(self.reader, [], {**entry, "_claim_histories": family}, ACTOR)
                    self.assertTrue(report["claims_clear"], report)
                    shared_counts.append(len(parsed_shared))
                self.assertEqual(*shared_counts)

    def creation_history(self, harness, command, *, claim_first=True, timestamp="2026-01-01T02:00:00Z"):
        from test_claim_receipts import history, claim as native_claim, output, SESSION as native_session
        rows = history(harness, native_claim(REPO), {})
        if harness == "codex":
            rows[3]["payload"]["arguments"] = json.dumps({"cmd": command})
        else:
            rows[3]["message"]["content"][0]["input"] = {"command": command}
        rows[4] = output({"exit_code": 0, "output": ""}, harness, "tool-1")
        if not claim_first:
            rows[1:5] = [*rows[3:5], *rows[1:3]]
        for row in rows:
            row["timestamp"] = timestamp
        self.comments = [comment(10, helper.claim.marker({**SOURCE, "session": native_session})),
                         comment(11, "Released claim 10\nSession: " + native_session, at="2026-01-01T00:02:00Z")]
        return {"session_id": native_session, "harness": harness, "_transcript_records": rows}

    def test_compound_pr_creation_requires_split_branch_handoff(self):
        from test_claim_receipts import history, claim as native_claim, output, SESSION as native_session
        for harness in ("codex", "claude"):
            for creation in ("git push origin feat && gh pr create --repo test/a --head feat",
                             "gh pr create --repo=test/a -H feat | cat",
                             "git push origin feat && gh pr create --repo test/a"):
                with self.subTest(harness=harness, creation=creation):
                    self.comments = [comment(10, helper.claim.marker({**SOURCE, "session": native_session})),
                                     comment(11, "Released claim 10\nSession: " + native_session,
                                             at="2026-01-01T00:02:00Z")]
                    self.pulls = [pull(3, branch="feat")]
                    rows = history(harness, native_claim(REPO), {})
                    if harness == "codex":
                        rows[3]["payload"]["arguments"] = json.dumps({"cmd": creation})
                    else:
                        rows[3]["message"]["content"][0]["input"] = {"command": creation}
                    rows[4] = output({"exit_code": 0, "output": self.pulls[0]["html_url"]}, harness, "tool-1")
                    for row in rows:
                        row["timestamp"] = SOURCE["claimed_at"]
                    entry = {"session_id": native_session, "harness": harness, "_transcript_records": rows}
                    with patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
                        report = helper.check(self.reader, [], entry, ACTOR)
                    self.assertFalse(report["claims_clear"], report)
                    self.comments[-1]["body"] += "\n" + self.pulls[0]["html_url"]
                    with patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
                        self.assertTrue(helper.check(self.reader, [], entry, ACTOR)["claims_clear"])
                        self.pulls = []
                        self.comments[-1]["body"] = "Released claim 10\nSession: " + native_session
                        self.assertTrue(helper.check(self.reader, [], entry, ACTOR)["claims_clear"])

    def test_completed_wrapped_claim_reaches_inventory_and_still_requires_exact_release(self):
        from test_claim_receipts import history, output, SESSION as native_session
        source = comment(10, helper.claim.marker({**SOURCE, "session": native_session}))
        release = comment(11, "Released claim 10\n\nSession: " + native_session, at="2026-01-01T00:02:00Z")
        for harness in ("codex", "claude"):
            for launcher in ("timeout 300 uv run", "uv run --directory /repo"):
                with self.subTest(harness=harness, launcher=launcher):
                    self.comments = [source]
                    rows = history(harness, {})
                    command = launcher + " /catalog/gh-plan.py --repo test/a claim 1 > claim.json"
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                    for row in rows:
                        row["timestamp"] = SOURCE["claimed_at"]
                    entry = {"session_id": native_session, "harness": harness, "_transcript_records": rows}
                    with patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
                        held = helper.check(self.reader, [], entry, ACTOR)
                        self.assertFalse(held["claims_clear"], held)
                        self.assertTrue(held["coverage"]["complete"], held)
                        self.comments.append(release)
                        released = helper.check(self.reader, [], entry, ACTOR)
                    self.assertTrue(released["claims_clear"], released)
                    self.assertEqual({r["claim_comment"] for r in released["released_claims"]}, {10})

    def test_finished_claim_with_ordinary_tests_and_reads_reaches_terminal_inventory(self):
        from test_claim_receipts import ordinary_read_history, history, output, claim as native_claim, SESSION as native_session
        self.comments = [comment(10, helper.claim.marker({**SOURCE, "session": native_session})),
                         comment(11, "Released claim 10\n\nSession: " + native_session, at="2026-01-01T00:02:00Z")]
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                rows = history(harness, native_claim(REPO))
                reads = ordinary_read_history(harness)
                end = 2 if harness == "codex" else 1
                rows[-end:-end] = reads[1:-end]
                for row in rows:
                    row["timestamp"] = SOURCE["claimed_at"]
                entry = {"session_id": native_session, "harness": harness, "_transcript_records": rows}
                with patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
                    # The reviewed head raised "ownership command has no terminal
                    # native result" before it could even read the exact release.
                    report = helper.check(self.reader, [], entry, ACTOR)
                self.assertTrue(report["claims_clear"], report)
                self.assertTrue(report["coverage"]["complete"], report)
                self.assertEqual({r["claim_comment"] for r in report["released_claims"]}, {10})
                if harness == "claude":
                    continue  # Claude completion is exercised through its real task tracker below.
                # A historical running record is rechecked, never a permanent
                # refusal before the live GitHub inventory.
                pending = history(harness, {})[1:3]
                command = "uv run scripts/gh-plan.py --repo test/a claim 1 > claim.json"
                if harness == "codex":
                    pending[0]["payload"].update(call_id="pending", arguments=json.dumps({"cmd": command}))
                else:
                    pending[0]["message"]["content"][0].update(id="pending", input={"command": command})
                pending[1] = output("Process running with session ID 123", harness, "pending")
                rows[-end:-end] = pending
                self.reader.reset_mock()
                native = helper.native_ownership
                with patch.object(native, "pending_ownership", wraps=CURRENT_NATIVE_PENDING), \
                        patch.object(native, "observe", side_effect=[
                            ([], {native_session: {"state": "live"}}, {}),
                            ([], {native_session: {"state": "ended"}}, {}),
                            ([], {native_session: {"state": "ended"}}, {})]) as observed, \
                        patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
                    waiting = helper.check(self.reader, [], entry, ACTOR)
                    self.assertFalse(waiting["claims_clear"], waiting)
                    self.assertTrue(waiting["pending"], waiting)
                    finished = helper.check(self.reader, [], entry, ACTOR)
                    self.assertTrue(finished["claims_clear"], finished)
                    self.assertFalse(finished["pending"], finished)
                    self.comments.pop()  # Gone does not release a real claim.
                    held = helper.check(self.reader, [], entry, ACTOR)
                    self.assertFalse(held["claims_clear"], held)
                    self.assertFalse(held["pending"], held)
                    self.assertEqual(observed.call_count, 3)
                self.reader.paged_json.assert_called()
                self.comments.append(comment(11, "Released claim 10\n\nSession: " + native_session,
                                             at="2026-01-01T00:02:00Z"))

    def test_claude_completed_background_task_reaches_live_complete_inventory(self):
        from test_claim_receipts import history, output, SESSION as native_session
        rows = history("claude", {})
        rows[1]["message"]["content"][0]["input"] = {"command": "uv run test_sample.py", "run_in_background": True}
        rows[2] = output({}, "claude", "tool-0")
        rows[2]["toolUseResult"] = {"backgroundTaskId": "watcher"}
        for row in rows:
            row["timestamp"] = SOURCE["claimed_at"]
        self.comments = [comment(10, helper.claim.marker({**SOURCE, "session": native_session})),
                         comment(11, "Released claim 10\nSession: " + native_session, at="2026-01-01T00:02:00Z")]
        entry = {"session_id": native_session, "harness": "claude", "_transcript_records": rows}
        with patch.object(helper.native_ownership, "pending_ownership", wraps=CURRENT_NATIVE_PENDING), \
                patch.object(helper.native_ownership, "observe", return_value=([], {}, {})), \
                patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
            self.assertTrue(helper.check(self.reader, [], entry, ACTOR)["pending"])
            rows[-1:-1] = [{"type": "user", "timestamp": SOURCE["claimed_at"], "message": {"content":
                "<task-notification><task-id>watcher</task-id><status>completed</status></task-notification>"}}]
            finished = helper.check(self.reader, [], entry, ACTOR)
            self.assertTrue(finished["claims_clear"], finished)
            self.assertTrue(finished["coverage"]["complete"], finished)
            self.comments.pop()
            self.assertFalse(helper.check(self.reader, [], entry, ACTOR)["claims_clear"])

    def test_shorthand_redirected_client_claim_is_inventoried_with_its_own_actor(self):
        from test_claim_receipts import history, claim as native_claim, output, SESSION as native_session
        foreign, number, client_actor = "client/repo", 7, "client-fixture[bot]"
        self.inventory.return_value["repositories"][foreign] = [client_actor]
        local = [comment(10, helper.claim.marker({**SOURCE, "session": native_session})),
                 comment(11, "Released claim 10", at="2026-01-01T00:02:00Z")]
        held = comment(20, helper.claim.marker({**SOURCE, "session": native_session}), client_actor)
        client_comments = [held]
        client = Mock(expected_actor=client_actor)
        client.get_json.return_value = {"body": "", "number": number}
        client.paged_json.side_effect = lambda path, **kw: [] if path.endswith("/pulls") else client_comments
        self.reader.paged_json.side_effect = lambda path, **kw: [{"full_name": REPO}] if path == "/installation/repositories" else [] if path.endswith("/pulls") else local
        for harness, suffix in ((h, s) for h in ("codex", "claude") for s in ("", " && python3 - <<'PY'\nprint('ok')\nPY")):
            rows = history(harness, native_claim(REPO), native_claim(foreign, number, 20))
            command = f'uv run /catalog/gh-plan.py claim {foreign}#{number} > claim.json' + suffix
            if harness == "codex":
                rows[3]["payload"]["arguments"] = json.dumps({"cmd": command})
            else:
                rows[3]["message"]["content"][0]["input"] = {"command": command}
            rows[4] = output({}, harness, "tool-1")
            for row in rows: row["timestamp"] = SOURCE["claimed_at"]
            entry = {"session_id": native_session, "harness": harness, "_transcript_records": rows}
            with patch.object(helper.sessions, "discover", side_effect=lambda reader, owner, entries, repos: ({(REPO, 1)} if repos == [REPO] else {(foreign, 7)}, repos)):
                report = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=lambda repo, _: client if repo == foreign else self.reader)
                self.assertFalse(report["claims_clear"], report)
                self.assertIn(foreign, report["coverage"]["repositories"])
                client_comments.append(comment(21, "Released claim 20", client_actor, at="2026-01-01T00:02:00Z"))
                released = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=lambda repo, _: client if repo == foreign else self.reader)
                self.assertTrue(released["claims_clear"], released)
                self.assertTrue(released["coverage"]["complete"])
                self.assertEqual({r["repository"] for r in released["released_claims"]}, {REPO, foreign})
                client_comments.pop()

    def setUp(self):
        from test_claim_receipts import UV_RUN_HELP
        helper.claim_receipts.uv_run_options.cache_clear()
        uv_help = patch.object(helper.claim_receipts, "runtime_uv_help", return_value=UV_RUN_HELP)
        uv_help.start()
        self.addCleanup(uv_help.stop)
        self.addCleanup(helper.claim_receipts.uv_run_options.cache_clear)
        self.native_pending = self.enterContext(patch.object(helper.native_ownership, "pending_ownership", return_value=[]))
        self.source = comment(10, "Claimed by worker\n\n" + helper.claim.marker(SOURCE))
        self.release = comment(11, "Released claim 10\n\nSession: " + SESSION, at="2026-01-01T00:02:00Z")
        self.comments = [self.source]
        self.issue = {"body": "", "number": 1}
        self.pulls = []
        self.reader = Mock()
        self.reader.expected_actor = ACTOR
        self.reader.deadline_at = None
        self.reader.get_json.side_effect = self.get
        self.reader.paged_json.side_effect = self.paged
        self.inventory = self.enterContext(patch.object(helper.github_identity, "automation_repositories",
            return_value={"repositories": {REPO: [ACTOR]}, "installations": []}))
        self.enterContext(patch.object(helper, "repository_reader", return_value=self.reader))

    def get(self, path, **kwargs):
        if "/pulls/" in path:
            return next(p for p in self.pulls if p["number"] == int(path.rsplit("/", 1)[1]))
        return self.issue

    def paged(self, path, **kwargs):
        if path == "/installation/repositories":
            return [{"full_name": REPO}]
        return self.pulls if path.endswith("/pulls") else self.comments

    def check(self):
        return helper.check_record(self.reader, REPO, 1, SESSION, ACTOR)

    def test_a_confirmed_no_write_refusal_does_not_make_inventory_incomplete(self):
        from test_claim_receipts import history, SESSION as native_session
        for harness in ("codex", "claude"):
            refused = {"operation": helper.claim_receipts.CLAIM_OPERATION, "ok": False,
                       "write_outcome": "not_started", "completed_steps": []}
            rows = history(harness, refused)
            command = "cd /repo && uv run scripts/gh-plan.py claim 1 2>&1"
            if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            else: rows[1]["message"]["content"][0]["input"] = {"command": command}
            for row in rows: row["timestamp"] = SOURCE["claimed_at"]
            entry = {"session_id": native_session, "harness": harness, "_transcript_records": rows}
            with patch.object(helper.sessions, "discover", return_value=([], [REPO])):
                report = helper.check(self.reader, [], entry, ACTOR)
            self.assertTrue(report["claims_clear"], report)
            self.assertTrue(report["coverage"]["complete"])

    def test_pending_ownership_is_rechecked_after_remote_inventory(self):
        from test_claim_receipts import history, SESSION as native_session
        for harness in ("codex", "claude"):
            for command in ("uv run scripts/gh-plan.py --repo test/a claim 1 > claim.json",
                            "uv run scripts/gh-plan.py --repo test/a claim 1 &",
                            "uv run scripts/gh-plan.py claim test/a#1 > claim.json 2>&1 &",
                            "nohup gh pr create --title x &"):
                rows = history(harness, {"operation": helper.claim_receipts.CLAIM_OPERATION})
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    rows[2]["payload"]["output"] = "Process running with session ID 123" if command.endswith("json") else ""
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                    rows[2]["message"]["content"][0]["content"] = "Process running with session ID 123" if command.endswith("json") else ""
                for row in rows:
                    row["timestamp"] = SOURCE["claimed_at"]
                entry = {"session_id": native_session, "harness": harness, "_transcript_records": rows}
                with patch.object(helper.native_ownership, "pending_ownership", wraps=CURRENT_NATIVE_PENDING), \
                        patch.object(helper.native_ownership, "observe", return_value=([], {}, {})) as observed, \
                        patch.object(helper.sessions, "discover", return_value=([], [REPO])):
                    report = helper.check(self.reader, [], entry, ACTOR)
                    self.assertFalse(report["claims_clear"], report)
                    self.assertTrue(report["pending"], report)
                    observed.assert_called_once()
        self.inventory.assert_called()

    def test_held_claim_blocks_even_when_hygiene_and_transcript_say_safe(self):
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness):
                records = ([{"type": "assistant", "sessionId": SESSION, "message": {"stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "Finished.\nSafe to exit: yes"}]}}] if harness == "claude" else [
                            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Finished.\nSafe to exit: yes"}},
                            {"type": "event_msg", "payload": {"type": "task_complete"}}])
                self.assertTrue(helper.session_record.summarize(records, harness)["safe_verdict"])
                with self.assertRaises(ValueError):
                    self.check()

    def test_exact_authored_release_is_read_back(self):
        self.comments.append(self.release)
        result = self.check()
        self.assertEqual([r["claim_comment"] for r in result], [self.source["id"]])
        self.assertEqual(result[0]["retained_prs"], [])

    def test_the_claim_helpers_bare_exact_recovery_release_is_accepted(self):
        self.comments.append({**self.release, "body": "Released claim 10"})
        self.assertEqual(self.check()[0]["claim_comment"], self.source["id"])

    def recovered_release(self):
        evidence = comment(88, f"Closed session {SESSION}\nEnded at: 2026-01-01T00:02:00Z\nSafe to exit: yes", at="2026-01-01T00:03:00Z")
        evidence["issue_url"] = f"https://api.github.com/repos/{REPO}/issues/1"
        body = helper.release.prepare_release(self.source, evidence, self.comments,
            {"sessions": [], "worktrees": []}, actor=ACTOR, role="supervisor", releaser_session="supervisor-fixture",
            evidence_url=f"https://github.com/{REPO}/issues/1#issuecomment-88", retained_prs=[p["html_url"] for p in self.pulls],
            checked_prs=[p["html_url"] for p in self.pulls], related=[])
        self.comments.append(comment(89, body, at="2026-01-01T00:04:00Z"))
        get = self.get
        self.reader.get_json.side_effect = lambda path, **kw: evidence if path.endswith("/issues/comments/88") else {"commit": {"committer": {"date": "2026-01-01T00:01:30Z"}}} if "/commits/" in path else get(path, **kw)
        return evidence

    def test_verified_supervisor_recovery_can_be_read_back_by_the_source(self):
        evidence = self.recovered_release()
        self.assertEqual(self.check()[0]["claim_comment"], self.source["id"])
        evidence["updated_at"] = "2026-01-01T00:05:00Z"
        with self.assertRaises(ValueError):
            self.check()

    def test_recovered_retained_pr_is_verified_and_can_get_a_fresh_authored_handoff(self):
        self.pulls = [{**pull(), "head": {**pull()["head"], "sha": "fixture-sha"}}]
        self.recovered_release()
        self.assertEqual(self.check()[0]["retained_prs"], [self.pulls[0]["html_url"]])
        self.comments.append(comment(90, "Released claim 10\n" + self.pulls[0]["html_url"], at="2026-01-01T00:05:00Z"))
        self.assertTrue(self.check()[0]["release_url"].endswith("issuecomment-90"))

    def test_omitted_recovery_pr_needs_a_later_exact_authored_handoff(self):
        self.recovered_release()
        self.pulls = [pull(3, branch="work/computed-split", created_at="2026-01-01T00:01:30Z")]
        with self.assertRaises(ValueError):
            self.check()
        handoff = comment(90, f"Handoff from {SOURCE['worker']}\nclaim: {self.source['id']}\nSession: {SESSION}\n{self.pulls[0]['html_url']}",
                          at="2026-01-01T00:05:00Z")
        self.comments.append(handoff)
        self.assertEqual(self.check()[0]["retained_prs"], [self.pulls[0]["html_url"]])

    def test_prose_handoff_does_not_release(self):
        self.comments.append(comment(11, "Handing off to Supervisor. Safe to exit: yes"))
        with self.assertRaises(ValueError):
            self.check()

    def test_another_identity_cannot_release(self):
        self.comments.append({**self.release, "user": {"login": "other[bot]"}})
        with self.assertRaises(ValueError):
            self.check()

    def test_source_edit_invalidates_old_release(self):
        self.comments.extend([self.release])
        self.source["updated_at"] = "2026-01-01T00:03:00Z"
        with self.assertRaises(ValueError):
            self.check()

    def test_unverified_retained_pr_blocks_released_claim(self):
        self.comments.append(self.release)
        self.pulls = [pull()]
        with self.assertRaises(ValueError):
            self.check()

    def test_retained_pr_handoff_allows_exit_before_merge(self):
        self.comments.append({**self.release, "body": self.release["body"] + "\nRetained PR: " + pull()["html_url"]})
        self.pulls = [pull()]
        self.assertEqual(self.check()[0]["retained_prs"], [self.pulls[0]["html_url"]])

    def test_named_pr_must_independently_link_issue_and_match_identity(self):
        self.comments.append({**self.release, "body": self.release["body"] + "\n" + pull()["html_url"]})
        for bad in (pull(body="Unrelated"), pull(user={"login": "other[bot]"}),
                    pull(base={"repo": {"full_name": "test/other"}})):
            self.pulls = [bad]
            with self.subTest(pull=bad), self.assertRaises(ValueError):
                self.check()

    def test_every_retained_pr_needs_handoff(self):
        self.comments.append({**self.release, "body": self.release["body"] + "\n" + pull()["html_url"]})
        self.pulls = [pull(), pull(3)]
        with self.assertRaises(ValueError):
            self.check()

    def test_named_split_branch_pr_is_verified_too(self):
        self.pulls = [pull(), pull(3, branch="work/split", body="Unrelated")]
        self.comments.append({**self.release, "body": self.release["body"] + "\n" +
                              "\n".join(p["html_url"] for p in self.pulls)})
        with self.assertRaises(ValueError):
            self.check()
        self.pulls[1]["body"] = "Fixes #1"
        self.assertEqual(len(self.check()[0]["retained_prs"]), len(self.pulls))

    def test_split_handoffs_can_be_in_separate_authored_comments(self):
        self.pulls = [pull(), pull(3, branch="work/split")]
        self.comments.append({**self.release, "body": self.release["body"] + "\n" + self.pulls[0]["html_url"]})
        self.comments.append(comment(12, f"Handoff from {SOURCE['worker']}\nclaim: {self.source['id']}\nSession: {SESSION}\n{self.pulls[1]['html_url']}", at="2026-01-01T00:03:00Z"))
        self.assertEqual(set(self.check()[0]["retained_prs"]), {p["html_url"] for p in self.pulls})

    def test_bare_dependency_issue_is_not_a_retained_pr(self):
        for mention in ("Blocked on #99", "Superseded by PR #99"):
            self.comments = [self.source, {**self.release, "body": self.release["body"] + "\n" + mention}]
            self.pulls = [pull(99, branch="unrelated", body="Unrelated", user={"login": "other[bot]"})]
            self.assertEqual(self.check()[0]["retained_prs"], [])

    def test_native_pr_creation_exposes_an_unnamed_split_branch(self):
        self.comments.append(self.release)
        self.pulls = [pull(3, branch="work/split")]
        with self.assertRaises(ValueError):
            helper.check_record(self.reader, REPO, 1, SESSION, ACTOR)

    def test_canonical_split_branch_pr_requires_handoff_without_a_creation_hint(self):
        self.comments.append(self.release)
        self.pulls = [pull(3, branch="work/computed-split", created_at="2026-01-01T00:01:30Z")]
        with self.assertRaises(ValueError):
            self.check()
        self.comments[-1]["body"] += "\nRetained PR: " + self.pulls[0]["html_url"]
        self.assertEqual(self.check()[0]["retained_prs"], [self.pulls[0]["html_url"]])

    def test_canonical_pr_attribution_uses_server_claim_lifetime(self):
        self.comments.append(self.release)
        # A later repeated release cannot expand the original ownership window.
        self.comments.append(comment(12, self.release["body"], at="2026-01-01T00:05:00Z"))
        for created, actor, retained in (("2026-01-01T00:00:30Z", ACTOR, False),
                                          ("2026-01-01T00:03:00Z", ACTOR, False),
                                          ("2026-01-01T00:01:30Z", "human", False),
                                          ("2026-01-01T00:01:30Z", ACTOR, True),
                                          (None, ACTOR, True)):
            with self.subTest(created=created, actor=actor):
                self.pulls = [pull(3, branch="work/independent", created_at=created, user={"login": actor})]
                if retained:
                    with self.assertRaises(ValueError):
                        self.check()
                else:
                    self.assertEqual(self.check()[0]["retained_prs"], [])

    def test_later_session_bound_handoff_is_supported(self):
        self.comments.append(self.release)
        self.comments.append(comment(12, f"Handoff from {SOURCE['worker']}\nclaim: {self.source['id']}\nSession: {SESSION}\n{pull()['html_url']}", at="2026-01-01T00:03:00Z"))
        self.pulls = [pull()]
        self.assertTrue(self.check())

    def test_pr_only_claim_does_not_need_a_self_link(self):
        self.issue["pull_request"] = {}
        self.pulls = [pull(1, body="Dependency bump", user={"login": "dependabot[bot]"})]
        self.comments.append({**self.release, "body": self.release["body"] + "\n" + self.pulls[0]["html_url"]})
        self.assertEqual(self.check()[0]["retained_prs"], [self.pulls[0]["html_url"]])

    def test_pr_only_refresh_can_use_a_different_task_branch(self):
        self.issue["pull_request"] = {}
        self.pulls = [pull(1, branch="dependabot/package", body="Dependency bump", user={"login": "dependabot[bot]"})]
        self.comments.append({**self.release, "body": self.release["body"] + "\n" + self.pulls[0]["html_url"]})
        self.assertTrue(self.check())

    def test_pr_only_claim_rejects_a_fork_work_record(self):
        self.issue["pull_request"] = {}
        self.pulls = [pull(1, head={"ref": SOURCE["branch"], "repo": {"full_name": "other/fork"}})]
        self.comments.append({**self.release, "body": self.release["body"] + "\n" + self.pulls[0]["html_url"]})
        with self.assertRaises(ValueError):
            self.check()

    def test_closed_pr_claim_needs_release_but_no_open_pr_handoff(self):
        self.issue["pull_request"] = {}
        self.pulls = [pull(1, state="closed")]
        with self.assertRaises(ValueError):
            self.check()
        self.comments.append(self.release)
        self.assertTrue(self.check())

    def test_status_only_ownership_is_not_nothing_to_release(self):
        self.comments = []
        self.issue["body"] = helper.claim.marker(SOURCE)
        with self.assertRaises(ValueError):
            self.check()

    def test_other_session_claims_remain_untouched(self):
        self.comments = [comment(20, helper.claim.marker({**SOURCE, "session": "other-session"}))]
        self.assertEqual(self.check(), [])
        self.reader.post_json.assert_not_called()

    def test_global_inventory_accounts_for_cross_repo_closed_issues_and_prs(self):
        entries = [("test/a", 1), ("test/b", 5), ("test/b", 6)]
        self.inventory.return_value["repositories"]["test/b"] = [ACTOR]
        self.reader.paged_json.side_effect = lambda *_a, **_k: [{"full_name": "test/a"}, {"full_name": "test/b"}]
        with patch.object(helper.sessions, "discover", return_value=(entries, ["test/a", "test/b"])), \
                patch.object(helper, "check_record", side_effect=[[], ValueError("held"), []]) as check:
            result = helper.check(self.reader, "test", {"session_id": SESSION, "harness": "codex"}, ACTOR)
        self.assertFalse(result["claims_clear"])
        self.assertEqual(check.call_count, len(entries))
        self.assertTrue(any("held" in reason for reason in result["blocked"]), result)

    def test_complete_discovery_reads_all_owner_repositories_and_closed_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "native.jsonl"
            transcript.write_text(json.dumps({"type": "session_meta", "timestamp": SOURCE["claimed_at"], "payload": {"id": SESSION}}) + "\n")
            def paged(path, **kwargs):
                if path == "/installation/repositories":
                    return [{"full_name": "test/a"}, {"full_name": "test/b"}]
                if path.endswith("/issues"):
                    return [{"number": 1, "state": "closed", "body": helper.claim.marker(SOURCE)}] if kwargs["params"].get("state") == "all" else []
                if path.endswith("/issues/comments"):
                    return []  # Closed status discovery is the only route.
                return self.comments
            self.reader.paged_json.side_effect = paged
            result = helper.check(self.reader, "test", {"session_id": SESSION, "harness": "codex", "transcript": str(transcript)}, ACTOR)
        self.assertFalse(result["claims_clear"])
        self.assertEqual(len(result["blocked"]), len(result["coverage"]["repositories"]))

    def test_incomplete_app_inventory_prevents_vacuous_no_receipt_all_clear(self):
        self.inventory.side_effect = helper.github_identity.GitHubAppError("inventory incomplete")
        with self.assertRaises(helper.github_identity.GitHubAppError):
            helper.check(self.reader, [], {"session_id": SESSION, "harness": "codex"}, ACTOR)

    def test_default_inventory_uses_its_resolved_client_actor(self):
        client_actor = "client-fixture[bot]"
        self.inventory.return_value["repositories"][REPO] = [client_actor]
        self.reader.expected_actor = client_actor
        self.source["user"]["login"] = client_actor
        self.comments.append({**self.release, "user": {"login": client_actor}})
        def pages(path, **kw):
            if path == "/installation/repositories": return [{"full_name": REPO}]
            if path.endswith("/issues/comments"):
                return [{**c, "issue_url": f"https://api.github.com/repos/{REPO}/issues/1"} for c in self.comments]
            if path.endswith("/issues") or path.endswith("/pulls"): return []
            return self.comments
        self.reader.paged_json.side_effect = pages
        entry = {"harness": "codex", "session_id": SESSION, "_transcript_records": [
            {"type": "session_meta", "timestamp": SOURCE["claimed_at"], "payload": {"id": SESSION}}]}
        report = helper.check(self.reader, "test", entry, ACTOR)
        self.assertTrue(report["claims_clear"])
        self.assertEqual([r["claim_comment"] for r in report["released_claims"]], [self.source["id"]])

    def test_missing_receipts_still_discover_a_held_default_installation_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "native.jsonl"
            path.write_text(json.dumps({"type": "session_meta", "timestamp": SOURCE["claimed_at"], "payload": {"id": SESSION}}) + "\n")
            def pages(endpoint, **kw):
                if endpoint == "/installation/repositories": return [{"full_name": REPO}]
                if endpoint.endswith("/issues/comments"):
                    return [{**self.source, "issue_url": f"https://api.github.com/repos/{REPO}/issues/1"}]
                return [] if endpoint.endswith("/issues") else self.comments
            self.reader.paged_json.side_effect = pages
            report = helper.check(self.reader, [], {"harness": "codex", "session_id": SESSION, "transcript": str(path)}, ACTOR)
            self.assertFalse(report["claims_clear"])

    def test_uncertain_renewal_reads_its_original_record_outside_the_time_window(self):
        from test_claim_receipts import history
        value = {"operation": helper.claim_receipts.CLAIM_OPERATION, "ok": False, "write_outcome": "unknown",
                 "claim": {"session": SESSION}, "issue": {"repo": REPO, "number": 1}}
        records = history("codex", value)
        for record in records: record["timestamp"] = "2026-01-02T00:00:00Z"
        self.comments.append(self.release)
        entry = {"session_id": SESSION, "harness": "codex", "_transcript_records": records}
        original_pages = self.paged
        self.reader.paged_json.side_effect = lambda path, **kw: [] if path.endswith("/issues/comments") or path.endswith("/issues") else original_pages(path, **kw)
        report = helper.check(self.reader, [], entry, ACTOR)
        self.assertTrue(report["claims_clear"], report)
        self.assertTrue(report["receipt_reconciliation"])
        self.assertEqual([r["claim_comment"] for r in report["released_claims"]], [self.source["id"]])

    def test_a_second_installation_and_its_client_actor_are_checked(self):
        from test_claim_receipts import claim as native_claim, history, SESSION as native_session
        foreign, client_actor = "second/b", "client-fixture[bot]"
        self.inventory.return_value = {"repositories": {REPO: [ACTOR], foreign: [client_actor]}, "installations": []}
        native = [native_claim(REPO), native_claim(foreign, 2, 20)]
        entry = {"harness": "codex", "session_id": native_session, "_transcript_records": history("codex", *native)}
        for record in entry["_transcript_records"]: record["timestamp"] = SOURCE["claimed_at"]
        def rows(repo, identifier, actor):
            source = comment(identifier, helper.claim.marker({**SOURCE, "session": native_session}), actor)
            return [source, comment(identifier + 1, f"Released claim {identifier}", actor, at="2026-01-01T00:02:00Z")]
        local_comments, client_comments = rows(REPO, 10, ACTOR), rows(foreign, 20, client_actor)
        client = Mock(expected_actor=client_actor)
        def missing():
            return helper.GitHubReadError("fixture record unavailable", result=SimpleNamespace(status=404), diagnostics={})
        def client_get(path, **kw):
            if path != f"/repos/{foreign}/issues/2": raise missing()
            return {"body": "", "number": 2}
        def local_get(path, **kw):
            if path != f"/repos/{REPO}/issues/1": raise missing()
            return {"body": "", "number": 1}
        client.get_json.side_effect = client_get
        self.reader.get_json.side_effect = local_get
        def pages(repo, comments, endpoint, **kw):
            if endpoint == "/installation/repositories":
                if repo == foreign: raise AssertionError("foreign installation enumeration must not use the default token")
                return [{"full_name": REPO}]
            if endpoint.endswith("/issues/comments"):
                return [{**c, "issue_url": f"https://api.github.com/repos/{repo}/issues/{2 if repo == foreign else 1}"} for c in comments]
            if endpoint.endswith("/issues") or endpoint.endswith("/pulls"): return []
            return comments
        self.reader.paged_json.side_effect = lambda path, **kw: pages(REPO, local_comments, path, **kw)
        client.paged_json.side_effect = lambda path, **kw: pages(foreign, client_comments, path, **kw)
        report = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=lambda repo, _: client if repo == foreign else self.reader)
        self.assertTrue(report["claims_clear"], report)
        self.assertEqual({r["repository"] for r in report["released_claims"]}, {REPO, foreign})
        # Current native completion cannot hide a held claim in a second App
        # installation merely because its source was computed.
        opaque_records = json.loads(json.dumps(entry["_transcript_records"]))
        opaque_records[3]["payload"].update(name="functions.exec", arguments=None,
            input='const command="uv run /catalog/gh-plan.py --repo second/b claim 2";text(await tools.exec_command({cmd:command}));')
        client_comments.pop()
        computed = helper.check(self.reader, [], {**entry, "_transcript_records": opaque_records}, ACTOR,
                                reader_for_repo=lambda repo, _: client if repo == foreign else self.reader)
        self.assertFalse(computed["claims_clear"], computed)
        client_comments.append(comment(21, "Released claim 20", client_actor, at="2026-01-01T00:02:00Z"))
        records = history("claude", native[0])
        records[-1:-1] = [
            {"type": "assistant", "sessionId": native_session, "message": {"content": [{"type": "tool_use", "name": "Bash", "id": "redirected-child", "input": {"command": "cd /foreign-repo && uv run /catalog/gh-plan.py claim 2 > claim.json"}}]}},
            {"type": "user", "sessionId": native_session, "message": {"content": [{"type": "tool_result", "tool_use_id": "redirected-child", "content": ""}]}},
        ]
        for record in records: record["timestamp"] = SOURCE["claimed_at"]
        entry = {"harness": "claude", "session_id": native_session, "_transcript_records": records}
        select = lambda repo, _: client if repo == foreign else self.reader
        report = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=select)
        self.assertTrue(report["claims_clear"], report)
        self.assertTrue(report["coverage"]["complete"], report)
        client_comments.pop()
        held = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=select)
        self.assertFalse(held["claims_clear"], held)
        client_comments.append(comment(21, "Released claim 20", ACTOR, at="2026-01-01T00:02:00Z"))
        wrong_release = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=select)
        self.assertFalse(wrong_release["claims_clear"], wrong_release)
        client_comments[-1]["user"]["login"] = client_actor
        self.assertTrue(helper.check(self.reader, [], entry, ACTOR, reader_for_repo=select)["claims_clear"])
        client.paged_json.side_effect = missing()
        incomplete = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=select)
        self.assertFalse(incomplete["claims_clear"], incomplete)
        self.assertFalse(incomplete["coverage"]["complete"], incomplete)
        self.assertTrue(any(foreign in reason for reason in incomplete["blocked"]))
        client.paged_json.side_effect = lambda path, **kw: pages(foreign, client_comments, path, **kw)
        self.assertTrue(helper.check(self.reader, [], entry, ACTOR, reader_for_repo=select)["claims_clear"])

    def test_complete_canonical_inventory_resolves_opaque_scope_without_borrowing_a_release(self):
        from test_claim_receipts import claim as native_claim, history, SESSION as native_session
        self.source["body"] = helper.claim.marker({**SOURCE, "session": native_session})
        self.comments.append(self.release)
        self.release["body"] = "Released claim 10\n\nSession: " + native_session
        def pages(path, **kw):
            if path == "/installation/repositories": return [{"full_name": REPO}]
            if path.endswith("/issues/comments"):
                return [{**c, "issue_url": f"https://api.github.com/repos/{REPO}/issues/1"} for c in self.comments]
            if path.endswith("/issues") or path.endswith("/pulls"): return []
            return self.comments
        self.reader.paged_json.side_effect = pages
        for harness in ("codex", "claude"):
            rows = history(harness, native_claim(REPO), native_claim(REPO))
            command = "cd /foreign && uv run /catalog/gh-plan.py claim 1 --worker worker --branch work/task > claim.json"
            if harness == "codex":
                rows[3]["payload"]["arguments"] = json.dumps({"cmd": command})
                rows[4]["payload"]["output"] = ""
            else:
                rows[3]["message"]["content"][0]["input"] = {"command": command}
                rows[4]["message"]["content"][0]["content"] = ""
            for row in rows: row["timestamp"] = SOURCE["claimed_at"]
            entry = {"harness": harness, "session_id": native_session, "_transcript_records": rows,
                     "_extra_repositories": [REPO]}
            report = helper.check(self.reader, [], entry, ACTOR)
            self.assertTrue(report["claims_clear"], report)
            self.assertTrue(report["coverage"]["complete"])
            # Batched spelling cannot change canonical ownership.
            batched = f"uv run /catalog/gh-plan.py claim 1; {command}"
            if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": batched})
            else: rows[1]["message"]["content"][0]["input"] = {"command": batched}
            entry["_transcript_records"] = rows[:3] + rows[5:]
            self.assertTrue(helper.check(self.reader, [], entry, ACTOR)["claims_clear"])
            self.comments.pop()
            self.assertFalse(helper.check(self.reader, [], entry, ACTOR)["claims_clear"])
            self.comments.append(self.release)

    def test_options_first_claim_scope_cannot_be_filled_by_an_older_released_claim(self):
        from test_claim_receipts import history, claim as native_claim, output, SESSION as native_session
        foreign = "client/repo"
        local_comments = [comment(10, helper.claim.marker({**SOURCE, "session": native_session})),
                          comment(11, "Released claim 10\nSession: " + native_session, at="2026-01-01T00:02:00Z")]
        client_actor = "client-fixture[bot]"
        self.inventory.return_value["repositories"][foreign] = [client_actor]
        client_comments = [comment(20, helper.claim.marker({**SOURCE, "session": native_session}), client_actor)]
        client = Mock(expected_actor=client_actor)
        client.get_json.return_value = {"body": "", "number": 7}
        client.paged_json.side_effect = lambda path, **kw: [] if path.endswith("/pulls") else client_comments
        self.reader.paged_json.side_effect = lambda path, **kw: ([{"full_name": REPO}] if path == "/installation/repositories"
            else [] if path.endswith("/pulls") else local_comments)
        options = "--worker w --session " + native_session + " --branch b --next-action n "
        for harness in ("codex", "claude"):
            for operand in (foreign + "#7", "$ISSUE", "7"):
                with self.subTest(harness=harness, operand=operand):
                    rows = history(harness, native_claim(REPO), {})
                    command = "uv run /catalog/gh-plan.py claim " + options + operand + " > claim.json"
                    if harness == "codex":
                        rows[3]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[3]["message"]["content"][0]["input"] = {"command": command}
                    rows[4] = output({"exit_code": 0, "output": ""}, harness, "tool-1")
                    for row in rows:
                        row["timestamp"] = SOURCE["claimed_at"]
                    entry = {"harness": harness, "session_id": native_session, "_transcript_records": rows,
                             "_extra_repositories": [REPO]}
                    with patch.object(helper.sessions, "discover", side_effect=lambda reader, owner, entries, repos: ({(REPO, 1)} if repos == [REPO] else {(foreign, 7)}, repos)):
                        report = helper.check(self.reader, [], entry, ACTOR,
                                              reader_for_repo=lambda repo, _: client if repo == foreign else self.reader)
                    self.assertFalse(report["claims_clear"], report)
                    self.assertTrue(report["coverage"]["complete"], report)
                    if operand == foreign + "#7":
                        self.assertIn(foreign, report["coverage"]["repositories"])
                        client_comments.append(comment(21, "Released claim 20\nSession: " + native_session,
                                                       client_actor, at="2026-01-01T00:02:00Z"))
                        with patch.object(helper.sessions, "discover", side_effect=lambda reader, owner, entries, repos: ({(REPO, 1)} if repos == [REPO] else {(foreign, 7)}, repos)):
                            released = helper.check(self.reader, [], entry, ACTOR,
                                                    reader_for_repo=lambda repo, _: client if repo == foreign else self.reader)
                        self.assertTrue(released["claims_clear"], released)
                        self.assertEqual({r["repository"] for r in released["released_claims"]}, {REPO, foreign})
                        client_comments.pop()
                    self.assertFalse(any("/None" in str(call) for call in self.reader.get_json.call_args_list))

    def test_dynamic_claim_with_its_own_canonical_receipt_still_requires_release(self):
        from test_claim_receipts import history, claim as native_claim, SESSION as native_session
        source = comment(10, helper.claim.marker({**SOURCE, "session": native_session}))
        release = comment(11, "Released claim 10\nSession: " + native_session, at="2026-01-01T00:02:00Z")
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                self.comments = [source]
                rows = history(harness, native_claim(REPO))
                command = "uv run /catalog/gh-plan.py claim --worker w $ISSUE --session " + native_session
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                for row in rows:
                    row["timestamp"] = SOURCE["claimed_at"]
                entry = {"harness": harness, "session_id": native_session, "_transcript_records": rows}
                with patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
                    held = helper.check(self.reader, [], entry, ACTOR)
                    self.assertFalse(held["claims_clear"], held)
                    self.assertTrue(held["coverage"]["complete"], held)
                    self.comments.append(release)
                    released = helper.check(self.reader, [], entry, ACTOR)
                self.assertTrue(released["claims_clear"], released)
                self.assertTrue(released["coverage"]["complete"], released)
                self.assertEqual({r["claim_comment"] for r in released["released_claims"]}, {10})

    def test_codex_child_thread_ownership_is_included_in_the_parent_readback(self):
        from test_claim_history import records, claim_history
        from test_claim_receipts import claim as native_claim
        child = "native-child"
        value = native_claim(REPO)
        value["claim"]["session"] = child
        root_records = records("codex", SESSION, delegate=True)
        child_records = records("codex", child, value, parent=SESSION)
        self.source["body"] = helper.claim.marker({**SOURCE, "session": child})
        def pages(path, **kw):
            if path == "/installation/repositories": return [{"full_name": REPO}]
            if path.endswith("/issues/comments"):
                return [{**c, "issue_url": f"https://api.github.com/repos/{REPO}/issues/1"} for c in self.comments]
            if path.endswith("/issues") or path.endswith("/pulls"): return []
            return self.comments
        self.reader.paged_json.side_effect = pages
        nodes = [{"session_id": SESSION, "harness": "codex", "records": root_records},
                 {"session_id": child, "harness": "codex", "records": child_records,
                  "transcript": "/fixture/child.jsonl", "digest": claim_history.fingerprint(child_records)}]
        entry = {"session_id": SESSION, "harness": "codex", "_claim_histories": nodes,
                 "_transcript_records": root_records + child_records, "_extra_repositories": [REPO]}
        self.assertFalse(helper.check(self.reader, [], entry, ACTOR)["claims_clear"])
        self.comments.append({**self.release, "body": "Released claim 10\nSession: " + child})
        report = helper.check(self.reader, [], entry, ACTOR)
        self.assertTrue(report["claims_clear"], report)
        self.assertEqual(set(report["coverage"]["session_ids"]), {SESSION, child})
        self.assertEqual([r["source_session"] for r in report["released_claims"]], [child])

    def test_claude_sidechain_claims_are_included_in_the_native_inventory(self):
        from test_claim_receipts import history, claim as native_claim
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / (SESSION + ".jsonl")
            parent.write_text(json.dumps({"type": "user", "sessionId": SESSION}) + "\n")
            child = Path(tmp) / SESSION / "subagents" / "agent-fixture.jsonl"
            child.parent.mkdir(parents=True)
            value = native_claim(REPO)
            value["claim"]["session"] = SESSION
            records = history("claude", value)
            # Native delegated records carry the parent's inherited identity.
            for record in records: record["sessionId"] = SESSION
            child.write_text("".join(json.dumps(r) + "\n" for r in records))
            sources, errors = helper.claim_receipts.sources(helper.receipt_history(parent, "claude", SESSION), "claude", SESSION)
            self.assertFalse(errors)
            self.assertEqual([(r["repository"], r["claim_comment"]) for r in sources], [(REPO, 10)])

    def test_cli_can_finish_on_both_native_transcript_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            for harness in ("codex", "claude"):
                now = [1000.0]
                original_snapshot = helper.transcript_snapshot
                def slow_snapshot(*args, **kwargs):
                    now[0] += 3600
                    return original_snapshot(*args, **kwargs)
                def first_read(path, **kwargs):
                    self.assertGreater(self.reader.deadline_at, now[0])
                    return self.paged(path, **kwargs)
                self.reader.paged_json.side_effect = first_read
                transcript = Path(tmp) / "native.jsonl"
                identity = {"type": "session_meta", "payload": {"id": SESSION}} if harness == "codex" else {"type": "user", "sessionId": SESSION}
                transcript.write_text(json.dumps({**identity, "timestamp": SOURCE["claimed_at"]}) + "\n")
                output = io.StringIO()
                with patch.object(helper.github_agent, "running_agent", return_value=harness), \
                        patch.object(helper.github_agent, "native_sessions", return_value={"native": SESSION}), \
                        patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                        patch.object(helper, "GitHubReader", autospec=True, return_value=self.reader), \
                        patch.object(helper.time, "time", side_effect=lambda: now[0]), \
                        patch.object(helper, "transcript_snapshot", side_effect=slow_snapshot), \
                        patch.object(helper.sessions, "discover", return_value=([], [REPO])), contextlib.redirect_stdout(output):
                    self.reader.deadline_at = now[0] + 1
                    code = helper.main(["--owner", "test", "--harness", harness, "--session", SESSION, "--transcript", str(transcript)])
                self.assertEqual(code, 0, output.getvalue())
                self.assertTrue(json.loads(output.getvalue())["claims_clear"])

    def test_claude_symlink_snapshot_proof_matches_the_canonical_ledger(self):
        from test_claim_history import records
        from test_claim_receipts import claim as native_claim
        with tempfile.TemporaryDirectory() as tmp:
            canonical = Path(tmp) / "canonical"
            canonical.mkdir()
            alias = Path(tmp) / "alias"
            alias.symlink_to(canonical, target_is_directory=True)
            root = canonical / (SESSION + ".jsonl")
            child = canonical / SESSION / "subagents" / "agent-fixture.jsonl"
            child.parent.mkdir(parents=True)
            value = native_claim(REPO)
            value["claim"]["session"] = SESSION
            child.write_text("".join(json.dumps(r) + "\n" for r in records("claude", SESSION, value)))
            self.comments.append(self.release)
            for checker_path, ledger_path in ((alias / root.name, root), (root, alias / root.name)):
                with self.subTest(checker_path=checker_path):
                    root_rows = records("claude", SESSION, delegate=True)
                    root.write_text("".join(json.dumps(r) + "\n" for r in root_rows))
                    output = io.StringIO()
                    with patch.object(helper.github_agent, "running_agent", return_value="claude"), \
                            patch.object(helper.github_agent, "native_sessions", return_value={"native": SESSION}), \
                            patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                            patch.object(helper, "GitHubReader", return_value=self.reader), \
                            patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])), \
                            contextlib.redirect_stdout(output):
                        code = helper.main(["--harness", "claude", "--session", SESSION,
                                            "--transcript", str(checker_path)])
                    report = json.loads(output.getvalue())
                    self.assertEqual(code, 0, report)
                    self.assertTrue(report["claims_clear"], report)
                    root.write_text("".join(json.dumps(r) + "\n" for r in records("claude", SESSION, report, delegate=True)))
                    status = helper.session_record.session_status({"harness": "claude", "session_id": SESSION,
                                                                  "transcript": str(ledger_path)})
                    self.assertTrue(status["safe_verdict"], status)

    def test_cli_malformed_codex_child_metadata_is_a_structured_refusal(self):
        from test_claim_history import records
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root, child = home / "root.jsonl", home / "child.jsonl"
            root.write_text("".join(json.dumps(r) + "\n" for r in records("codex", SESSION, delegate=True)))
            with sqlite3.connect(home / "state_5.sqlite") as db:
                db.executescript("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT); "
                                 "CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT PRIMARY KEY);")
                db.execute("INSERT INTO threads VALUES(?,?,?)", (SESSION, str(root), json.dumps("cli")))
                db.execute("INSERT INTO threads VALUES(?,?,?)", ("child", str(child), json.dumps(
                    {"subagent": {"thread_spawn": {"parent_thread_id": SESSION}}})))
                db.execute("INSERT INTO thread_spawn_edges VALUES(?,?)", (SESSION, "child"))
            for source in ("cli", {"subagent": "worker"}, {"subagent": {"thread_spawn": "worker"}}):
                with self.subTest(source=source):
                    rows = records("codex", "child", parent=SESSION)
                    rows[0]["payload"]["source"] = source
                    child.write_text("".join(json.dumps(r) + "\n" for r in rows))
                    output = io.StringIO()
                    with patch.object(helper.github_agent, "running_agent", return_value="codex"), \
                            patch.object(helper.github_agent, "native_sessions", return_value={"native": SESSION}), \
                            patch.object(helper.claim_history, "codex_homes", return_value=[home]), \
                            patch.object(helper, "GitHubReader") as reader, contextlib.redirect_stdout(output):
                        code = helper.main(["--harness", "codex", "--session", SESSION,
                                            "--transcript", str(root)])
                    report = json.loads(output.getvalue())
                    self.assertNotEqual(code, 0)
                    self.assertFalse(report["claims_clear"], report)
                    self.assertFalse(report["coverage"]["complete"], report)
                    reader.assert_not_called()

    def test_cli_has_no_all_clear_on_partial_inventory_or_auth_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "native.jsonl"
            transcript.write_text(json.dumps({"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z", "payload": {"id": SESSION}}) + "\n")
            for error in (ValueError("partial inventory"), OSError("permission denied")):
                output = io.StringIO()
                with patch.object(helper.github_agent, "native_sessions", return_value={"CODEX_THREAD_ID": SESSION}), \
                        patch.object(helper.github_identity, "automation_login", return_value=ACTOR), \
                        patch.object(helper, "GitHubReader", return_value=self.reader), \
                        patch.object(helper.sessions, "discover", side_effect=error), contextlib.redirect_stdout(output):
                    code = helper.main(["--owner", "test", "--harness", "codex", "--session", SESSION, "--transcript", str(transcript)])
                result = json.loads(output.getvalue())
                self.assertNotEqual(code, 0)
                self.assertFalse(result["claims_clear"])
                self.assertFalse(result["coverage"]["complete"])

    def test_cli_rejects_a_different_native_session_before_reading_github(self):
        with patch.object(helper.github_agent, "native_sessions", return_value={"CODEX_THREAD_ID": "other"}), \
                patch.object(helper, "GitHubReader") as reader, contextlib.redirect_stdout(io.StringIO()):
            code = helper.main(["--owner", "test", "--harness", "codex", "--session", SESSION, "--transcript", "/unused"])
        self.assertNotEqual(code, 0)
        reader.assert_not_called()

    def test_cli_refuses_missing_identity_and_wrong_harness_without_github(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "old-native.jsonl"
            transcript.write_text(json.dumps({"type": "session_meta", "timestamp": SOURCE["claimed_at"], "payload": {"id": SESSION}}) + "\n")
            for family, native in (("codex", {}), ("claude", {"native": SESSION})):
                with patch.object(helper.github_agent, "running_agent", return_value=family), \
                        patch.object(helper.github_agent, "native_sessions", return_value=native), \
                        patch.object(helper, "GitHubReader", return_value=self.reader) as reader, \
                        patch.object(helper.sessions, "discover", return_value=([], [REPO])), contextlib.redirect_stdout(io.StringIO()):
                    code = helper.main(["--owner", "test", "--harness", "codex", "--session", SESSION, "--transcript", str(transcript)])
                self.assertNotEqual(code, 0)
                reader.assert_not_called()

    def test_live_fork_snapshot_keeps_raw_coverage_but_requires_native_ancestry_context(self):
        from test_claim_history import records
        from test_claim_receipts import claim
        parent, child = 'parent-session', 'child-session'
        source = claim()
        source['claim']['session'] = child
        inherited = records('codex', parent)
        inherited[0]['payload']['source'] = 'vscode'
        local = records('codex', child, source, parent=parent)
        boundary = len(inherited) + 1
        local[0]['payload'].update(forked_from_id=parent, subagent_history_start_ordinal=boundary)
        marker = {'type': 'event_msg', 'payload': {'type': 'thread_settings_applied', 'thread_id': child}}
        raw = [local[0], *inherited, marker, *local[1:]]
        for index, row in enumerate(raw):
            row['ordinal'] = index
        context = {'parent': parent, 'ancestors': (parent,)}
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / 'fork.jsonl'
            transcript.write_text(''.join(json.dumps(row) + '\n' for row in raw))
            for wrong_session in (parent, child, 'unrelated-session'):
                with self.subTest(root_identity=wrong_session), self.assertRaises(ValueError):
                    helper.transcript_snapshot(transcript, 'codex', wrong_session)
            snapshot = helper.transcript_snapshot(transcript, 'codex', child, context)
            self.assertEqual(snapshot, raw)
            owned = helper.claim_history.child_records(snapshot, 'codex', child, context)
            self.assertFalse(helper.claim_receipts.sources(owned, 'codex', parent)[0])
            self.assertEqual(len(helper.claim_receipts.sources(owned, 'codex', child)[0]), 1)
            for invalid in ({'parent': 'unrelated-session', 'ancestors': (parent,)},
                            {'parent': parent, 'ancestors': ()}):
                with self.subTest(context=invalid), self.assertRaises(ValueError):
                    helper.transcript_snapshot(transcript, 'codex', child, invalid)

    def test_live_fork_snapshot_as_own_root_uses_indexed_identity_and_local_claims(self):
        from test_claim_history import NativeHistoryTests, records
        from test_claim_receipts import claim
        fixture = NativeHistoryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        parent_claim, child_claim = claim(), claim('example/b', 2, 20)
        parent_claim['claim']['session'] = 'root'
        child_claim['claim']['session'] = 'child'
        parent = records('codex', 'root', parent_claim)
        raw = fixture.fork_rows(parent, child_claim)
        fixture.write(fixture.root, parent)
        fixture.write(fixture.child, raw)
        snapshot = helper.transcript_snapshot(fixture.child, 'codex', 'child')
        self.assertEqual({row['payload']['id'] for row in snapshot if row.get('type') == 'session_meta'}, {'child'})
        self.assertFalse(helper.claim_receipts.sources(snapshot, 'codex', 'root')[0])
        self.assertEqual(len(helper.claim_receipts.sources(snapshot, 'codex', 'child')[0]), 1)
        with self.assertRaises(ValueError):
            helper.transcript_snapshot(fixture.child, 'codex', 'root')
        with sqlite3.connect(fixture.database) as database:
            database.execute("DELETE FROM thread_spawn_edges WHERE child_thread_id='child'")
        with self.assertRaises(ValueError):
            helper.transcript_snapshot(fixture.child, 'codex', 'child')

    def test_live_transcript_snapshot_ignores_only_an_unfinished_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "native.jsonl"
            identity = {"type": "session_meta", "payload": {"id": SESSION}}
            transcript.write_text(json.dumps(identity) + '\n{"type":')
            self.assertEqual(helper.transcript_snapshot(transcript, "codex", SESSION), [identity])
            transcript.write_text(json.dumps(identity) + '\n{"type":\n')
            with self.assertRaises(ValueError):
                helper.transcript_snapshot(transcript, "codex", SESSION)

    def test_native_receipts_add_an_account_missing_from_the_arguments(self):
        from pathlib import Path
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "supervisor/scripts"))
        from test_claim_receipts import claim as native_claim, history, SESSION as native_session
        entry = {"harness": "codex", "session_id": native_session,
                 "_transcript_records": history("codex", native_claim("test/a"), native_claim("second/b", 2, 20))}
        with patch.object(helper.sessions, "discover", side_effect=[([], ["second/b"]), ([], ["test/a"])]) as discover:
            result = helper.check(self.reader, "test", entry, ACTOR, reader_for_repo=lambda *_: self.reader)
        self.assertEqual({call.args[1] for call in discover.call_args_list}, {"test", "second"})
        self.assertFalse(result["claims_clear"])  # Missing source readback cannot be an empty all-clear.

    def test_foreign_reads_pr_creation_and_admission_links_do_not_expand_claim_inventory(self):
        from test_claim_receipts import history, claim as native_claim, output, SESSION as native_session
        self.comments = [comment(10, helper.claim.marker({**SOURCE, "session": native_session})),
                         comment(11, "Released claim 10\nSession: " + native_session, at="2026-01-01T00:02:00Z")]
        for harness in ("codex", "claude"):
            cases = [(command, "") for command in (
                "gh issue view -R upstream/x 5",
                "gh pr view https://github.com/upstream/x/pull/9",
                "uv run gh-plan.py --repo upstream/x show 1",
                "gh pr create --repo upstream/x",
                "uv run gh-pr.py --repo upstream/x create",
                "uv run gh-plan.py --repo test/a claim 1 --admission-link https://github.com/upstream/x/issues/9",
            )]
            for receipt_session in (native_session, "another-native-session"):
                saved = native_claim("upstream/x", 9)
                saved["claim"]["session"] = receipt_session
                saved["issue"] = {"repo": "upstream/x", "number": 9}
                cases.append(("uv run gh-plan.py --repo test/a claim 1; cat old-claim.json",
                              json.dumps([native_claim(REPO), saved])))
            for command, stdout in cases:
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, native_claim(REPO), {})
                    if harness == "codex":
                        rows[3]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[3]["message"]["content"][0]["input"] = {"command": command}
                    rows[4] = output({"exit_code": 0, "output": stdout}, harness, "tool-1")
                    for row in rows:
                        row["timestamp"] = SOURCE["claimed_at"]
                    entry = {"session_id": native_session, "harness": harness, "_transcript_records": rows}
                    foreign_reader = Mock(side_effect=lambda repo, _: self.reader if repo == REPO else (_ for _ in ()).throw(AssertionError("unrelated repository must not be read")))
                    with patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
                        report = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=foreign_reader)
                    self.assertTrue(report["claims_clear"], report)
                    self.assertEqual(report["coverage"]["repositories"], [REPO])
                    self.assertEqual({call.args[0] for call in foreign_reader.call_args_list}, {REPO})

    def test_child_process_handle_cannot_borrow_a_parent_claim_invocation(self):
        from test_claim_receipts import history, claim as native_claim, output, SESSION as native_session
        root = history("codex", {})
        root[1]["payload"]["arguments"] = json.dumps({"cmd": "uv run gh-plan.py --repo test/a claim 1"})
        root[2] = output({"session_id": 123}, "codex", "tool-0")
        root[-2:-2] = [
            {"payload": {"type": "function_call", "name": "write_stdin", "call_id": "root-poll", "arguments": '{"session_id":123}'}},
            output({"exit_code": 0, "output": ""}, "codex", "root-poll"),
        ]
        child = history("codex")
        child[0]["payload"]["id"] = "child-native-session"
        saved = {**native_claim("upstream/x", 7), "issue": {"repo": "upstream/x", "number": 7}}
        child[-2:-2] = [
            {"payload": {"type": "function_call", "name": "write_stdin", "call_id": "child-poll", "arguments": '{"session_id":123}'}},
            output(saved, "codex", "child-poll"),
        ]
        for row in root + child:
            row["timestamp"] = SOURCE["claimed_at"]
        nodes = [{"session_id": native_session, "harness": "codex", "records": root},
                 {"session_id": "child-native-session", "harness": "codex", "records": child,
                  "transcript": "/fixture/child.jsonl", "digest": helper.claim_history.fingerprint(child)}]
        entry = {"session_id": native_session, "harness": "codex", "_claim_histories": nodes,
                 "_transcript_records": root + child}
        self.comments = [comment(10, helper.claim.marker({**SOURCE, "session": native_session})),
                         comment(11, "Released claim 10\nSession: " + native_session, at="2026-01-01T00:02:00Z")]
        foreign_reader = Mock(side_effect=lambda repo, _: self.reader if repo == REPO else (_ for _ in ()).throw(AssertionError("a different node's process cannot supply a receipt")))
        with patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
            report = helper.check(self.reader, [], entry, ACTOR, reader_for_repo=foreign_reader)
        self.assertTrue(report["claims_clear"], report)
        self.assertEqual(report["coverage"]["repositories"], [REPO])
        self.assertEqual({call.args[0] for call in foreign_reader.call_args_list}, {REPO})

    def test_child_canonical_receipt_may_use_the_inherited_parent_session(self):
        from test_claim_receipts import history, claim as native_claim, SESSION as native_session
        root = history("codex")
        child = history("codex", native_claim(REPO))
        child[0]["payload"]["id"] = "child-native-session"
        for row in root + child:
            row["timestamp"] = SOURCE["claimed_at"]
        nodes = [{"session_id": native_session, "harness": "codex", "records": root},
                 {"session_id": "child-native-session", "harness": "codex", "records": child,
                  "transcript": "/fixture/child.jsonl", "digest": helper.claim_history.fingerprint(child)}]
        entry = {"session_id": native_session, "harness": "codex", "_claim_histories": nodes,
                 "_transcript_records": root + child}
        self.comments = [comment(10, helper.claim.marker({**SOURCE, "session": native_session}))]
        with patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
            self.assertFalse(helper.check(self.reader, [], entry, ACTOR)["claims_clear"])
            self.comments.append(comment(11, "Released claim 10\nSession: " + native_session, at="2026-01-01T00:02:00Z"))
            report = helper.check(self.reader, [], entry, ACTOR)
        self.assertTrue(report["claims_clear"], report)
        self.assertTrue(report["coverage"]["complete"], report)

    def test_dynamic_issue_scope_uses_complete_canonical_inventory(self):
        from test_claim_receipts import history, output, SESSION as native_session
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                rows = history(harness, {})
                command = "uv run gh-plan.py --repo test/a claim \"$ISSUE\" > claim.json"
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                for row in rows:
                    row["timestamp"] = SOURCE["claimed_at"]
                entry = {"session_id": native_session, "harness": harness, "_transcript_records": rows,
                         "_extra_repositories": [REPO]}
                self.comments = [comment(10, helper.claim.marker({**SOURCE, "session": native_session})),
                                 comment(11, "Released claim 10\nSession: " + native_session, at="2026-01-01T00:02:00Z")]
                with patch.object(helper.sessions, "discover", return_value=({(REPO, 1)}, [REPO])):
                    report = helper.check(self.reader, [], entry, ACTOR)
                self.assertTrue(report["claims_clear"], report)
                self.assertTrue(report["coverage"]["complete"], report)


if __name__ == "__main__":
    unittest.main()
