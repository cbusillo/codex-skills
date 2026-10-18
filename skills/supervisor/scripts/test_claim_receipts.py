# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Native receipt fixtures exercise shutdown acceptance on both harnesses."""
import json
import asyncio
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

import claim_receipts as receipts
import session_record

SESSION = "native-fixture"


def claim(repo="example/a", number=1, identifier=10):
    return {"operation": receipts.CLAIM_OPERATION, "ok": True, "claim": {"session": SESSION},
            "claim_comment": {"comment": {"url": f"https://github.com/{repo}/issues/{number}#issuecomment-{identifier}"}}}


def proof(*sources, clear=True):
    return {"operation": receipts.CHECK_OPERATION, "ok": clear, "claims_clear": clear,
            "session_id": SESSION, "harness": "codex", "coverage": {"complete": True},
            "released_claims": [{"repository": repo, "record": number, "claim_comment": identifier}
                                for repo, number, identifier in sources]}


def output(value, harness, call_id="tool"):
    # Real composed Codex output: native text block -> exec result -> helper JSON.
    encoded = json.dumps({"output": json.dumps(value)})
    if harness == "codex":
        return {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": call_id,
                "output": [{"type": "input_text", "text": encoded}]}}
    return {"type": "user", "sessionId": SESSION, "message": {"content": [
        {"type": "tool_result", "tool_use_id": call_id, "content": encoded}]}}


def history(harness, *values):
    start = {"type": "session_meta", "payload": {"id": SESSION}} if harness == "codex" else {"type": "user", "sessionId": SESSION}
    end = ([{"type": "event_msg", "payload": {"type": "agent_message", "message": "Safe to exit: yes"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}}] if harness == "codex" else
           [{"type": "assistant", "sessionId": SESSION, "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Safe to exit: yes"}]}}])
    middle = []
    for index, value in enumerate(values):
        call_id = f"tool-{index}"
        if value.get("operation") == receipts.CHECK_OPERATION:
            command = "uv run scripts/check_claims.py"
        elif value.get("operation") == receipts.CLAIM_OPERATION:
            url = ((value.get("claim_comment") or {}).get("comment") or {}).get("url", "")
            match = receipts.COMMENT.fullmatch(url)
            number = int(match[2]) if match else (value.get("issue") or {}).get("number", 1)
            command = f"uv run scripts/gh-plan.py claim {number}"
        elif value.get("operation") == "github.pr.create":
            command = "uv run scripts/gh-pr.py create"
        else:
            command = "uv run scripts/gh-plan.py show 1"
        invocation = ({"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": call_id, "arguments": json.dumps({"cmd": command})}}
                      if harness == "codex" else {"type": "assistant", "sessionId": SESSION, "message": {"content": [{"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]}})
        middle.extend([invocation, output(value, harness, call_id)])
    return [start, *middle, *end]


class ReceiptTests(unittest.TestCase):
    def test_batched_numeric_claims_cannot_share_one_canonical_receipt(self):
        command = "cd /local && uv run /catalog/gh-plan.py claim 2; cd /foreign && uv run /catalog/gh-plan.py claim 2 > claim.json"
        for harness in ("codex", "claude"):
            rows = history(harness, claim(number=2))
            if harness == "codex":
                rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            else:
                rows[1]["message"]["content"][0]["input"] = {"command": command}
            self.assertEqual(len(receipts.unscoped_claim_invocations(rows, harness)), 2)
        rows = history("codex", claim(number=2))
        rows[1]["payload"].update(name="functions.exec", arguments='await Promise.all([tools.exec_command({cmd:"uv run /catalog/gh-plan.py claim 2"}), tools.exec_command({cmd:"uv run /catalog/gh-plan.py claim 2 > claim.json"})])')
        self.assertEqual(len(receipts.unscoped_claim_invocations(rows, "codex")), 2)

    def test_shutdown_refuses_a_claimed_session_that_only_says_safe(self):
        for harness in ("codex", "claude"):
            result = session_record.summarize(history(harness, claim()), harness)
            self.assertTrue(result["declared_safe_verdict"])
            self.assertFalse(result["safe_verdict"])

    def test_verified_disposition_is_accepted_on_both_harnesses(self):
        for harness in ("codex", "claude"):
            value = {**proof(("example/a", 1, 10)), "harness": harness}
            result = session_record.summarize(history(harness, claim(), value), harness)
            self.assertTrue(result["safe_verdict"])

    def test_missing_second_account_cannot_be_all_clear(self):
        records = history("codex", claim(), claim("other/b", 2, 20), proof(("example/a", 1, 10)))
        self.assertFalse(session_record.summarize(records, "codex")["safe_verdict"])

    def test_a_new_claim_or_pr_after_the_proof_invalidates_it(self):
        value = proof(("example/a", 1, 10))
        for later in (claim("example/a", 2, 11), {"operation": "github.pr.create", "ok": True, "url": "https://github.com/example/a/pull/3"}):
            self.assertFalse(session_record.summarize(history("codex", claim(), value, later), "codex")["safe_verdict"])

    def test_partial_failed_or_other_session_proof_does_not_pass(self):
        for change in ({"ok": False}, {"session_id": "other"}, {"harness": "claude"}, {"coverage": {"complete": False}}):
            value = {**proof(("example/a", 1, 10)), **change}
            self.assertFalse(session_record.summarize(history("codex", claim(), value), "codex")["safe_verdict"])

    def test_assistant_prose_and_quoted_issue_receipts_are_not_proof(self):
        value = proof(("example/a", 1, 10))
        records = history("codex", claim(), {"operation": "github.plan.show", "issue": {"body": json.dumps(value)}})
        self.assertFalse(session_record.summarize(records, "codex")["safe_verdict"])
        records = history("codex", claim())
        records.insert(-2, {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "text", "text": json.dumps(value)}]}})
        self.assertFalse(session_record.summarize(records, "codex")["safe_verdict"])

    def test_sessions_without_claims_keep_the_existing_exit_path(self):
        for harness in ("codex", "claude"):
            self.assertTrue(session_record.summarize(history(harness), harness)["safe_verdict"])

    def test_reading_a_saved_proof_does_not_attest_a_fresh_check(self):
        records = history("codex", claim(), proof(("example/a", 1, 10)))
        records[-4]["payload"]["arguments"] = json.dumps({"cmd": "cat saved-proof.json"})
        self.assertFalse(session_record.summarize(records, "codex")["safe_verdict"])

    def test_a_later_failed_attempt_cannot_reuse_the_old_proof(self):
        records = history("codex", claim(), proof(("example/a", 1, 10)))
        records[-2:-2] = [
            {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "bad-check", "arguments": json.dumps({"cmd": "uv run scripts/check_claims.py --bad-argument"})}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "bad-check", "output": "argument parsing failed"}},
        ]
        self.assertFalse(session_record.summarize(records, "codex")["safe_verdict"])

    def test_codex_orchestration_poll_binds_to_the_original_check(self):
        records = history("codex", claim())
        call = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "launch", "input":
                'text(await tools.exec_command({cmd:"uv run scripts/check_claims.py"}));'}}
        started = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "launch", "output": json.dumps({"session_id": 123, "output": ""})}}
        poll = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "poll", "input":
                'text(await tools.write_stdin({session_id:123}));'}}
        records[-2:-2] = [call, started, poll, output(proof(("example/a", 1, 10)), "codex", "poll")]
        self.assertTrue(session_record.summarize(records, "codex")["safe_verdict"])

    def test_plain_codex_process_handle_binds_a_long_running_check(self):
        records = history("codex", claim(), proof(("example/a", 1, 10)))
        launch, result = records[-4:-2]
        result["payload"]["output"] = "Process running with session ID 123"
        records[-2:-2] = [
            {"type": "response_item", "payload": {"type": "function_call", "name": "write_stdin", "call_id": "poll", "arguments": json.dumps({"session_id": 123})}},
            output(proof(("example/a", 1, 10)), "codex", "poll")]
        self.assertTrue(session_record.summarize(records, "codex")["safe_verdict"])

    def test_tool_prompts_and_echoed_commands_are_not_check_invocations(self):
        for harness in ("codex", "claude"):
            records = history(harness, claim(), {**proof(("example/a", 1, 10)), "harness": harness})
            call = records[3]
            if harness == "codex":
                call["payload"]["arguments"] = json.dumps({"cmd": 'echo "uv run scripts/check_claims.py"; cat saved.json'})
            else:
                block = call["message"]["content"][0]
                block.update(name="Agent", input={"prompt": "uv run scripts/check_claims.py"})
            self.assertFalse(session_record.summarize(records, harness)["safe_verdict"])

    def test_a_heredoc_mention_is_data_and_claude_error_output_is_not_proof(self):
        records = history("codex", claim(), proof(("example/a", 1, 10)))
        records[3]["payload"]["arguments"] = json.dumps({"cmd": "cat <<'EOF'\nuv run scripts/check_claims.py\nEOF\ncat saved.json"})
        self.assertFalse(session_record.summarize(records, "codex")["safe_verdict"])
        records = history("claude", claim(), {**proof(("example/a", 1, 10)), "harness": "claude"})
        records[4]["message"]["content"][0]["is_error"] = True
        self.assertFalse(session_record.summarize(records, "claude")["safe_verdict"])

    def test_a_checker_report_cannot_replace_unread_child_history(self):
        records = history("claude")
        records[1:1] = [
            {"type": "assistant", "sessionId": SESSION, "message": {"content": [{"type": "tool_use", "name": "Agent", "id": "child", "input": {"prompt": "complete assigned work"}}]}},
            {"type": "user", "sessionId": SESSION, "message": {"content": [{"type": "tool_result", "tool_use_id": "child", "content": "completed"}]}},
        ]
        self.assertFalse(session_record.summarize(records, "claude")["safe_verdict"])
        checked = history("claude", {**proof(), "harness": "claude"})
        records[-1:-1] = checked[1:-1]
        self.assertFalse(session_record.summarize(records, "claude")["safe_verdict"])

    def test_a_redirected_claim_or_delegation_needs_a_complete_inventory(self):
        for harness in ("codex", "claude"):
            records = history(harness, claim())
            records[2] = output({}, harness, "tool-0")
            self.assertFalse(session_record.summarize(records, harness)["safe_verdict"])
            check = {**proof(), "harness": harness}
            records = history(harness, claim(), check)
            records[2] = output({}, harness, "tool-0")
            self.assertTrue(session_record.summarize(records, harness)["safe_verdict"])

    def test_unknown_claim_write_can_be_reconciled_by_complete_github_readback(self):
        value = {"operation": receipts.CLAIM_OPERATION, "claim": {"session": SESSION}, "ok": False, "write_outcome": "unknown"}
        report = {**proof(), "receipt_reconciliation": True}
        self.assertTrue(session_record.summarize(history("codex", value, report), "codex")["safe_verdict"])

    def test_plain_gh_pr_create_after_a_proof_invalidates_it(self):
        records = history("codex", claim(), proof(("example/a", 1, 10)))
        records[-2:-2] = [{"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "create", "arguments": json.dumps({"cmd": "gh pr create --title example"})}}]
        self.assertFalse(session_record.summarize(records, "codex")["safe_verdict"])
        url = "https://github.com/example/a/pull/3"
        records[-2:-2] = [{"type": "response_item", "payload": {"type": "function_call_output", "call_id": "create", "output": url}}]
        self.assertEqual(receipts.created_prs(records, "codex"), {url})

    def test_brace_heavy_output_does_not_hide_a_receipt(self):
        value = proof()
        self.assertEqual(list(receipts.envelopes("{" * 20000 + json.dumps(value))), [value])

    def test_missing_native_identity_does_not_erase_claim_receipts(self):
        records = history("codex", claim())[1:]
        self.assertFalse(session_record.summarize(records, "codex")["safe_verdict"])

    def test_ended_session_recovery_preserves_the_original_declaration(self):
        import release_session_claims
        import finished_map
        import close_ttys
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                transcript = Path(tmp) / "native.jsonl"
                transcript.write_text("".join(json.dumps({**r, "timestamp": "2026-01-01T00:00:00Z"}) + "\n" for r in history(harness, claim())))
                entry = {"harness": harness, "session_id": SESSION, "transcript": str(transcript),
                         "pid": 123, "tty": "ttys-fixture", "supervisor_owned": True,
                         "handoff_url": "https://github.com/example/a/issues/1#issuecomment-12",
                         "process_exit_verified": True, "ended_at": "2026-01-01T00:10:00Z"}
                ledger = Path(tmp) / "ledger.json"
                ledger.write_text(json.dumps([entry]))
                self.assertFalse(finished_map.candidates(ledger)[0]["candidate"])
                with self.assertRaises(ValueError):
                    asyncio.run(close_ttys.close(None, entry, False, True, True))
                with patch.object(release_session_claims, "inventory", return_value=[]):
                    result = release_session_claims.validate_closure(entry, "supervisor-fixture")
                self.assertTrue(result["declared_safe_verdict"])
                self.assertFalse(result["safe_verdict"])

    def test_a_claim_write_without_its_source_receipt_is_not_clear(self):
        value = {"operation": receipts.CLAIM_OPERATION, "claim": {"session": SESSION}, "ok": False,
                 "write_outcome": "unknown"}
        self.assertFalse(session_record.summarize(history("codex", value), "codex")["safe_verdict"])


if __name__ == "__main__":
    unittest.main()
