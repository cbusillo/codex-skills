#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Synthetic provider fixtures exercise evidence extraction, not instruction text."""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from search_history import bound, discover_roots, eastern, scan
from extract_rollout_memory import events_from_record


def eastern_instant(text):
    """Read an ET display stamp back as the instant it names."""
    local = datetime.strptime(text, "%Y-%m-%d %I:%M:%S %p ET")
    return local.replace(tzinfo=ZoneInfo("America/New_York"))


def codex(text, role="user", timestamp="2026-10-01T14:20:00Z", kind="response_item"):
    payload = {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}
    if kind == "event_msg":
        payload = {"type": role + "_message", "message": text}
    return {"type": kind, "timestamp": timestamp, "payload": payload}


def claude(text, role="user", timestamp="2026-10-02T14:20:00Z", **extra):
    return {"type": role, "timestamp": timestamp, "sessionId": "synthetic-claude",
            "message": {"role": role, "content": [{"type": "text", "text": text}]}, **extra}


class SearchHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)

    def write(self, name, records):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        return path

    def search(self, **kwargs):
        return scan([self.root], queries=kwargs.pop("queries", ["preview"]), signals=kwargs.pop("signals", []), **kwargs)

    def test_both_providers_exact_text_and_chronological_sources(self):
        original = "Use preview first.\nKeep  two spaces and ‘quotes’."
        first = self.write("codex.jsonl", [{"type": "session_meta", "payload": {"id": "synthetic-codex"}}, codex(original)])
        later = "Actually, no longer require preview for this tool."
        self.write("claude.jsonl", [claude(later), claude("The Director decided preview is optional.", role="assistant")])
        result = self.search()
        self.assertEqual([hit["text"] for hit in result["hits"]], [original, later, "The Director decided preview is optional."])
        self.assertEqual(result["hits"][0]["source"], str(first))
        self.assertEqual(result["hits"][0]["position"], 2)
        self.assertEqual([hit["provider"] for hit in result["hits"]], ["codex", "claude", "claude"])
        self.assertEqual(result["hits"][0]["session"], "synthetic-codex")
        self.assertEqual(result["hits"][1]["session"], "synthetic-claude")
        self.assertEqual(result["hits"][2]["attribution"], "agent_text")
        self.assertEqual(result["hits"][0]["attribution"], "user_candidate")
        self.assertEqual(eastern_instant(result["hits"][0]["time_et"]),
                         datetime.fromisoformat(result["hits"][0]["timestamp"]))

    def test_codex_event_msg_and_mirror_do_not_inflate_patterns(self):
        text = "Preview is frustrating."
        self.write("codex.jsonl", [codex(text, kind="event_msg"), codex(text),
                                   codex(text, kind="event_msg", timestamp="2026-10-03T14:20:00Z")])
        result = self.search()
        self.assertEqual(result["matched_messages"], 2)
        self.assertEqual(result["mirrored_messages_omitted"], 1)

    def test_shared_reader_handles_current_and_legacy_codex_messages(self):
        current = codex("preview", kind="event_msg")
        legacy = {"payload": {"msg": {"type": "user_message", "text": "preview"}}}
        for record in (current, legacy):
            events = list(events_from_record(record, self.root / "source.jsonl", 1, None))
            self.assertEqual([(event.role, event.text) for event in events], [("user", "preview")])

    def test_injections_summaries_thinking_and_tool_results_are_not_direct_quotes(self):
        tool = claude("placeholder")
        tool["message"]["content"] = [{"type": "tool_result", "content": "preview frustrated"},
                                         {"type": "thinking", "thinking": "preview frustrated"}]
        self.write("claude.jsonl", [claude("preview", isMeta=True), claude("preview", isCompactSummary=True), tool])
        self.write("codex.jsonl", [codex("# AGENTS.md instructions\npreview"),
                                   {"type": "response_item", "payload": {"type": "function_call_output", "output": "preview"}}])
        self.assertEqual(self.search()["hits"], [])

    def test_real_discussion_of_instructions_survives_memory_noise_filter(self):
        quote = "Available skills are frustrating; preview is blocked."
        self.write("claude.jsonl", [claude(quote)])
        self.assertEqual(self.search()["hits"][0]["text"], quote)
        self.assertEqual(list(events_from_record(claude(quote), self.root / "s.jsonl", 1, None)), [])

    def test_sidechain_user_text_is_flagged(self):
        self.write("claude.jsonl", [claude("preview", isSidechain=True)])
        self.write("codex.jsonl", [{"type": "session_meta", "payload": {"id": "helper", "source": {"subagent": {}}}}, codex("preview")])
        self.assertTrue(all(hit["sidechain"] for hit in self.search()["hits"]))

    def test_date_bounds_filter_messages_without_borrowing_file_mtime(self):
        self.write("source.jsonl", [codex("preview", timestamp="2026-09-01T00:00:00Z"),
                                   codex("preview", timestamp="2026-10-02T00:00:00Z"),
                                   codex("preview", timestamp=None), codex("preview", timestamp="2026-10-02T00:00:00")])
        result = self.search(since=bound("2026-10-01T00:00:00Z"), until=bound("2026-10-03T00:00:00Z"))
        self.assertEqual(len(result["hits"]), 1)
        self.assertTrue(any(item["kind"] == "unknown_message_timestamps" for item in result["limitations"]))
        unknown = [hit for hit in self.search()["hits"] if hit["timestamp"] is None]
        self.assertEqual(len(unknown), 2)
        self.assertTrue(all(hit["time_et"] is None for hit in unknown))

    def test_signals_are_candidates_with_query_intersection(self):
        self.write("source.jsonl", [codex("preview: DIRECTION.md keeps blocking work"),
                                   codex("preview is frustrating"), codex("actually use preview instead"),
                                   codex("another topic is frustrating")])
        for signal in ("direction-friction", "frustration", "changed-decision"):
            hits = self.search(signals=[signal])["hits"]
            self.assertEqual(len(hits), 1)
            self.assertIn(signal, hits[0]["signals"])
        self.assertEqual(len(self.search(queries=["missing", "preview"])["hits"]), 3)

    def test_limits_malformed_and_unknown_formats_are_explicit(self):
        path = self.write("source.jsonl", [codex("preview"), codex("preview"), {"type": "unfamiliar", "text": "preview"}])
        with path.open("a") as handle:
            handle.write("{broken\n")
        result = self.search(max_hits=1)
        self.assertEqual(len(result["hits"]), 1)
        self.assertFalse(result["coverage_complete"])
        self.assertTrue({"hit_limit", "malformed_records", "unsupported_records"}.issubset({item["kind"] for item in result["limitations"]}))
        self.assertTrue(any(item["kind"] == "byte_limit" for item in self.search(max_bytes=10)["limitations"]))
        self.write("second.jsonl", [codex("preview")])
        self.assertTrue(any(item["kind"] == "file_limit" for item in self.search(max_files=1)["limitations"]))

    def test_missing_roots_and_overlapping_roots(self):
        path = self.write("source.jsonl", [codex("preview")])
        result = scan([self.root, path, self.root / "absent"], queries=["preview"], signals=[])
        self.assertEqual(len(result["hits"]), 1)
        self.assertTrue(any(item["kind"] == "missing_root" for item in result["limitations"]))
        unsupported = self.root / "session.txt"
        unsupported.write_text("preview")
        result = scan([unsupported], queries=["preview"], signals=[])
        self.assertFalse(result["coverage_complete"])
        self.assertTrue(any(item["kind"] == "unsupported_file" for item in result["limitations"]))

    def test_json_array_uses_record_index(self):
        path = self.root / "transcript.json"
        path.write_text(json.dumps([codex("preview"), claude("preview")], indent=2))
        result = self.search()
        self.assertEqual([hit["position"] for hit in result["hits"]], [1, 2])
        self.assertTrue(all(hit["position_kind"] == "record_index" for hit in result["hits"]))

    def test_discovery_includes_default_each_account_env_and_archives(self):
        for layout in (".codex-accounts/one", ".codex-accounts/two", ".claude-accounts/one"):
            (self.root / layout).mkdir(parents=True)
        custom = self.root / "custom"
        roots = discover_roots(self.root, {"CODEX_HOME": str(custom), "CLAUDE_CONFIG_DIR": str(custom)})
        for suffix in (".codex/sessions", ".codex/archived_sessions", ".claude/projects",
                       ".codex-accounts/one/sessions", ".codex-accounts/two/archived_sessions",
                       ".claude-accounts/one/projects", "custom/sessions", "custom/projects"):
            self.assertIn(self.root / suffix, roots)

    def test_file_allowance_samples_each_provider_before_filling_first_root(self):
        for index in range(3):
            self.write(f"codex/{index}.jsonl", [codex("preview")])
        self.write("claude/only.jsonl", [claude("preview")])
        result = scan([self.root / "codex", self.root / "claude"], queries=["preview"], signals=[], max_files=2)
        self.assertEqual({hit["provider"] for hit in result["hits"]}, {"codex", "claude"})
        self.assertFalse(result["coverage_complete"])
        self.assertTrue(any(item["kind"] == "file_limit" for item in result["limitations"]))

    def test_legacy_messages_have_provider_and_known_bookkeeping_is_not_unknown(self):
        self.write("legacy.jsonl", [{"timestamp": "2026-10-01T14:20:00Z",
                                    "payload": {"msg": {"type": "user_message", "text": "preview"}}},
                                   {"type": "event_msg", "payload": {"type": "token_count", "count": 4}},
                                   {"type": "turn_context", "payload": {"cwd": "synthetic"}}])
        result = self.search()
        self.assertEqual(result["hits"][0]["provider"], "codex")
        self.assertTrue(result["coverage_complete"])

    def test_cli_rejects_ambiguous_dates_and_emits_json_without_changing_source(self):
        path = self.write("source.jsonl", [codex("preview")])
        original = path.read_bytes()
        cli = Path(__file__).with_name("search_history.py")
        result = subprocess.run([sys.executable, str(cli), "--root", str(path), "--query", "preview"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["hits"][0]["text"], "preview")
        self.assertEqual(path.read_bytes(), original)
        for flags in (["--since", "2026-10-01"], ["--max-bytes", "0"],
                      ["--since", "2026-10-03T00:00:00Z", "--until", "2026-10-01T00:00:00Z"]):
            result = subprocess.run([sys.executable, str(cli), "--query", "preview", *flags], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)

    def test_et_daylight_and_standard_time_follow_zone_database(self):
        for source in ("2026-07-01T14:00:00Z", "2026-12-01T14:00:00Z"):
            with self.subTest(source=source):
                self.assertEqual(eastern_instant(eastern(source)), datetime.fromisoformat(source))


if __name__ == "__main__":
    unittest.main()
