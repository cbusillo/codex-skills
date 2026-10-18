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
        command = "uv run scripts/check_claims.py" if value.get("operation") == receipts.CHECK_OPERATION else "uv run scripts/gh-plan.py claim 1"
        invocation = ({"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": call_id, "arguments": json.dumps({"cmd": command})}}
                      if harness == "codex" else {"type": "assistant", "sessionId": SESSION, "message": {"content": [{"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]}})
        middle.extend([invocation, output(value, harness, call_id)])
    return [start, *middle, *end]


class ReceiptTests(unittest.TestCase):
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
            {"type": "response_item", "payload": {"type": "function_call", "call_id": "bad-check", "arguments": json.dumps({"cmd": "uv run scripts/check_claims.py --bad-argument"})}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "bad-check", "output": "argument parsing failed"}},
        ]
        self.assertFalse(session_record.summarize(records, "codex")["safe_verdict"])

    def test_codex_orchestration_poll_binds_to_the_original_check(self):
        records = history("codex", claim())
        call = {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "launch", "input":
                'text(await tools.exec_command({cmd:"uv run scripts/check_claims.py"}));'}}
        started = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "launch", "output": json.dumps({"session_id": 123, "output": ""})}}
        poll = {"type": "response_item", "payload": {"type": "custom_tool_call", "call_id": "poll", "input":
                'text(await tools.write_stdin({session_id:123}));'}}
        records[-2:-2] = [call, started, poll, output(proof(("example/a", 1, 10)), "codex", "poll")]
        self.assertTrue(session_record.summarize(records, "codex")["safe_verdict"])

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
