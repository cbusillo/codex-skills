# /// script
# requires-python = ">=3.12"
# ///
"""The transcript measure credits loads only when they come before the step they own."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("transcript_measure", Path(__file__).with_name("transcript-measure.py"))
assert SPEC and SPEC.loader
measure = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = measure
SPEC.loader.exec_module(measure)

START = "2026-10-01T12:00:00Z"


def claude_prompt(text: str) -> dict:
    return {"type": "user", "timestamp": START, "entrypoint": "cli", "message": {"content": text}}


def claude_call(name: str, **arguments: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": arguments}]}}


def claude_result(text: str) -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": text}]}}


COMPACT = {"type": "system", "subtype": "compact_boundary"}


def codex_meta(source: str = "vscode", originator: str = "codex-tui") -> dict:
    return {"type": "session_meta", "payload": {"id": "codex-1", "timestamp": START, "source": source,
                                                 "originator": originator, "cwd": "/work/repo"}}


def codex_turn(turn_id: str) -> dict:
    return {"type": "turn_context", "payload": {"turn_id": turn_id}}


def codex_exec(*commands: str) -> dict:
    script = "\n".join(f"await tools.exec_command({{cmd:{json.dumps(c)}}});" for c in commands)
    return {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": script}}


def codex_shell(command: str) -> dict:
    return {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
                                                 "arguments": json.dumps({"cmd": command})}}


class TranscriptMeasureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.claude = root / "claude"
        self.codex = root / "codex"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def run_measure(self, *extra: str) -> tuple[dict, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            measure.main(["--since", "2026-09-30T00:00:00Z", "--claude-root", str(self.claude),
                          "--codex-root", str(self.codex), "--json", *extra])
        text = output.getvalue()
        return json.loads(text[: text.rindex("}") + 1]), text

    def test_claude_turns_and_compaction_need_their_own_reload(self) -> None:
        self.write(self.claude / "proj" / "s1.jsonl", [
            claude_prompt("merge PR 17"),
            claude_call("Skill", skill="shared:github"),
            claude_call("Bash", command="cd /repo && uv run skills/github/scripts/gh-pr.py --repo o/r merge 17"),
            claude_result("<task-notification> a background task finished"),
            {"type": "user", "message": {"content": "<task-notification>\n<task-id>x</task-id>"}},
            claude_call("Bash", command="gh pr merge 18"),  # same turn: no later-turn check
            claude_prompt("now merge PR 19"),
            claude_call("Bash", command="gh pr merge 19"),
            COMPACT,
            {"type": "user", "isCompactSummary": True, "message": {"content": "This session is being continued from a previous conversation"}},
            claude_call("Skill", skill="github"),
            claude_call("Bash", command="git push origin work/x"),
        ])
        report, _ = self.run_measure()
        github = report["skills"]["claude"]["github"]
        self.assertEqual((github["sessions"], github["before"]), (1, 1))
        self.assertEqual((github["later_turn"], github["later_turn_reloaded"]), (1, 0))
        self.assertEqual((github["after_compact"], github["after_compact_reloaded"]), (1, 1))
        self.assertEqual((github["loads"], github["rereads"]), (2, 1))
        self.assertEqual(report["totals"]["claude"], {"sessions": 1, "compactions": 1})

    def test_a_load_after_a_policy_block_is_a_recovery_and_a_miss_is_listed(self) -> None:
        self.write(self.claude / "proj" / "s2.jsonl", [
            claude_prompt("watch the PR"),
            claude_call("Bash", command="gh run list --branch x"),
            claude_result("Blocked by the `babysit-pr` skill's command policy `x`."),
            claude_call("Read", file_path="/catalog/skills/babysit-pr/SKILL.md"),
            claude_call("Bash", command="gh pr checks 4 --watch"),
            claude_call("Bash", command="uv run pytest -q"),
        ])
        report, text = self.run_measure("--misses", "python-uv-workflow")
        babysit = report["skills"]["claude"]["babysit-pr"]
        self.assertEqual((babysit["sessions"], babysit["before"], babysit["after_block"]), (1, 0, 1))
        self.assertEqual(report["skills"]["claude"]["python-uv-workflow"]["before"], 0)
        self.assertIn("s2 proj: uv run pytest -q", text)

    def test_loads_count_in_command_order_and_only_as_file_reads(self) -> None:
        self.write(self.claude / "proj" / "s5.jsonl", [
            claude_prompt("merge, then run the script"),
            claude_call("Bash", command="ls skills/python-uv-workflow/SKILL.md; gh pr merge 3; cat skills/github/SKILL.md"),
            claude_call("Bash", command="uv run scripts/example.py"),
            claude_call("Bash", command="uv run --quiet /catalog/skills/github/scripts/github_api.py call --method GET /rate_limit"),
        ])
        report, _ = self.run_measure()
        skills = report["skills"]["claude"]
        self.assertEqual((skills["github"]["sessions"], skills["github"]["before"]), (1, 0))
        self.assertEqual((skills["python-uv-workflow"]["sessions"], skills["python-uv-workflow"]["before"]), (1, 0))
        self.assertEqual(skills["github"]["loads"], 1)
        self.assertEqual(skills["python-uv-workflow"]["loads"], 0)

    def test_quoted_search_text_is_neither_a_step_nor_a_load(self) -> None:
        self.write(self.claude / "proj" / "s7.jsonl", [
            claude_prompt("find the helpers"),
            claude_call("Bash", command="rg 'gh pr merge|gh issue list' README.md; sed -n '/github/SKILL.md/p' README.md"),
            claude_call("Bash", command="uv run skills/github/scripts/gh-pr.py update-branch 4 && gh project list"),
        ])
        report, _ = self.run_measure()
        skills = report["skills"]["claude"]
        self.assertEqual((skills["github"]["sessions"], skills["github"]["loads"]), (1, 0))
        self.assertEqual(skills["github-plan"]["sessions"], 1)

    def test_a_proactive_load_is_not_a_recovery_when_a_later_block_hits(self) -> None:
        self.write(self.claude / "proj" / "s8.jsonl", [
            claude_prompt("merge PR 3"),
            claude_call("Skill", skill="github"),
            claude_call("Bash", command="gh pr merge 3"),
            claude_result("Blocked by the `github` skill's command policy `x`."),
            claude_call("Skill", skill="github"),
        ])
        report, _ = self.run_measure()
        github = report["skills"]["claude"]["github"]
        self.assertEqual((github["before"], github["after_block"]), (1, 0))

    def test_a_blocked_first_attempt_is_a_miss_and_its_recovery_is_counted(self) -> None:
        self.write(self.claude / "proj" / "s6.jsonl", [
            claude_prompt("merge PR 3"),
            claude_call("Bash", command="gh pr merge 3"),
            claude_result("Blocked by the `github` skill's command policy `x`."),
            claude_call("Skill", skill="github"),
            claude_call("Bash", command="uv run skills/github/scripts/gh-pr.py merge 3"),
        ])
        report, _ = self.run_measure()
        github = report["skills"]["claude"]["github"]
        self.assertEqual((github["sessions"], github["before"], github["after_block"]), (1, 0, 1))

    def test_mentions_and_headless_sessions_are_not_steps(self) -> None:
        self.write(self.claude / "proj" / "s3.jsonl", [
            claude_prompt("explain the helper"),
            claude_call("Bash", command="grep -n 'gh pr merge' README.md && cat <<'EOF' > notes.md\ngh pr merge 3\nEOF"),
        ])
        headless = [dict(claude_prompt("review"), entrypoint="sdk-cli"), claude_call("Bash", command="gh pr merge 3")]
        self.write(self.claude / "proj" / "s4.jsonl", headless)
        self.write(self.codex / "2026" / "rollout-exec.jsonl", [codex_meta("exec", "codex_exec"), codex_shell("gh pr merge 3")])
        report, _ = self.run_measure()
        self.assertEqual(report["skills"].get("claude", {}).get("github", {}).get("sessions", 0), 0)
        self.assertEqual(report["totals"]["claude"]["sessions"], 1)
        self.assertNotIn("codex", report["totals"])

    def test_codex_reads_of_skill_files_count_in_both_call_forms(self) -> None:
        self.write(self.codex / "2026" / "rollout-a.jsonl", [
            codex_meta(),
            codex_turn("t1"),
            codex_exec("sed -n '1,200p' \"skills/github/SKILL.md\"\ngh pr merge 3 --body \"done\""),
            {"type": "compacted", "payload": {}},
            codex_turn("t1"),
            codex_shell("gh pr merge 4"),
            codex_turn("t2"),
            codex_shell("cat /catalog/skills/github/SKILL.md && gh pr merge 5"),
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input":
                'await tools.apply_patch("*** Begin Patch\\n*** Update File: /repo/app.py\\n@@\\n-a\\n+b\\n*** End Patch");'}},
            codex_exec("git commit -am change"),
        ])
        report, _ = self.run_measure()
        github = report["skills"]["codex"]["github"]
        self.assertEqual((github["sessions"], github["before"]), (1, 1))
        self.assertEqual((github["after_compact"], github["after_compact_reloaded"]), (1, 0))
        self.assertEqual((github["later_turn"], github["later_turn_reloaded"]), (1, 1))
        self.assertEqual(report["skills"]["codex"]["jetbrains-inspection"]["sessions"], 1)
        self.assertEqual(report["totals"]["codex"], {"sessions": 1, "compactions": 1})

    def test_codex_exec_commands_decode_to_what_ran(self) -> None:
        command = 'cat "skills/github/SKILL.md"\ngh pr merge 3 --body "done"'
        single_quoted = """tools.exec_command({cmd:'cat "skills/github/SKILL.md"\\ngh pr merge 3 --body "done"'})"""
        for source in (codex_exec(command)["payload"]["input"], single_quoted):
            with self.subTest(source=source):
                self.assertEqual(measure.codex_commands({"type": "custom_tool_call", "input": source}), [command])

    def test_sessions_outside_the_window_or_excluded_are_skipped(self) -> None:
        early = dict(claude_prompt("merge"), timestamp="2026-09-01T00:00:00Z")
        self.write(self.claude / "proj" / "old.jsonl", [early, claude_call("Bash", command="gh pr merge 1")])
        self.write(self.claude / "proj" / "me.jsonl", [claude_prompt("merge"), claude_call("Bash", command="gh pr merge 2")])
        report, _ = self.run_measure("--exclude-session", "me")
        self.assertNotIn("claude", report["totals"])


if __name__ == "__main__":
    unittest.main()
