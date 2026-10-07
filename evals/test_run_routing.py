# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyYAML==6.0.3",
# ]
# ///
"""Routing grades reject missing owners, policy redirects, and duplicate context."""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("run_routing", Path(__file__).with_name("run-routing.py"))
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def call(name: str, arguments: dict[str, str]) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": arguments}]}}


class RoutingSessionIsolationTests(unittest.TestCase):
    def test_disposable_sessions_suppress_supervisor_alerts_on_both_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog"
            (catalog / "hooks").mkdir(parents=True)
            (catalog / "hooks" / "hooks.json").write_text('{"hooks":{"PreToolUse":[]}}')
            (catalog / "skills").mkdir()
            case = root / "case.yaml"
            case.write_text("name: alert-isolation\nexecution:\n  prompt: test\n  timeout_seconds: 1\n")
            captured = []
            real_run = subprocess.run

            def launch(argv, **kwargs):
                if argv[0] in ("claude", "codex"):
                    captured.append((argv[0], kwargs["env"]))
                    return subprocess.CompletedProcess(argv, 0)
                return real_run(argv, **kwargs)

            with mock.patch.dict(os.environ, {"HOME": str(root), "CODEX_HOME": str(root / "config")}), mock.patch.object(runner.subprocess, "run", side_effect=launch), mock.patch.object(runner, "score_run", return_value={}):
                for host in ("claude", "codex"):
                    runner.run_case(host, catalog, case, root / host, None)
            self.assertEqual([host for host, env in captured], ["claude", "codex"])
            for host, env in captured:
                self.assertEqual(env["SESSION_ALERTS_DISABLED"], "1")


class RoutingScoreTests(unittest.TestCase):
    def test_compound_reads_credit_only_delivered_source_on_both_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog"
            skill = catalog / "skills/github/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("Fixture source with a complete read requirement.\n")
            command = f"cat {skill}; rg absent missing.txt"
            events = [{"command": command, "allowed": True}]
            turns = [{"expect": {"owner": "github", "read": "SKILL.md"}}]
            for host in ("codex", "claude"):
                for output, passed in [(skill.read_text(), True), ("cat: permission denied", False),
                                       ("Fixture source", False)]:
                    if host == "codex":
                        messages = [{"type": "item.completed", "item": {
                            "type": "command_execution", "command": command,
                            "exit_code": 1, "aggregated_output": output}}]
                    else:
                        use = call("Bash", {"command": command})
                        use["message"]["content"][0]["id"] = "read"
                        messages = [use, {"type": "user", "message": {"content": [{
                            "type": "tool_result", "tool_use_id": "read",
                            "is_error": True, "content": output}]}}]
                    (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [
                        {"type": "turn_marker", "turn": 1}, *messages])))
                    (root / "shell-events.jsonl").write_text("\n".join(map(json.dumps, events)))
                    self.assertEqual(runner.score_turns(host, turns, root, catalog)["passed"], passed,
                                     (host, output))

    def test_native_read_delivers_a_skill_without_the_base_directory_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = runner.ROOT / "skills/github/SKILL.md"
            use = call("Read", {"file_path": str(path)})
            use["message"]["content"][0]["id"] = "read"
            result = {"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": "read", "content": "source"}]}}
            seen = runner.observe("claude", [use, result], [], root, runner.ROOT)
            self.assertEqual(seen["loaded"], ["github"])

    def test_forbidden_reads_include_native_tools_and_failed_shell_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expect = {"owner": [], "forbid_read": r"\.env"}
            for name, arguments in [("Read", {"file_path": "/private/.env"}),
                                    ("Grep", {"path": "/private/.env", "pattern": "TOKEN"}),
                                    ("Bash", {"command": "cat /private/.env"})]:
                seen = runner.observe("claude", [call(name, arguments)], [], root, runner.ROOT)
                self.assertFalse(all(runner.decision_checks(seen, expect).values()), name)
            seen = runner.observe("codex", [], [{"command": "cat /private/.env", "allowed": False}],
                                  root, runner.ROOT)
            self.assertFalse(all(runner.decision_checks(seen, expect).values()))

    def test_system_skill_cache_is_not_the_maintained_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog"
            for path in (root / "account/skills/.system/skill-creator/SKILL.md",
                         catalog / "skills/.system/skill-creator/SKILL.md"):
                command = f"cat {path}"
                seen = runner.observe("codex", [{"type": "item.completed", "item": {
                    "type": "command_execution", "command": command, "exit_code": 0}}],
                    [{"command": command, "allowed": True}], root, catalog)
                self.assertTrue(seen["foreign_skill_reads"])
                self.assertFalse(seen["loaded"])

    def test_wrapped_codex_commands_correlate_with_raw_hook_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            source = workspace / "official.md"
            source.write_text("Frozen official documentation.\n")
            for exit_code in (0, 1):
                command = "cat official.md" + ("; rg absent missing.txt" if exit_code else "")
                item_command = "/bin/zsh -lc " + runner.shlex.quote(command)
                seen = runner.observe("codex", [{"type": "item.completed", "item": {
                    "type": "command_execution", "command": item_command,
                    "exit_code": exit_code, "aggregated_output": source.read_text()}}],
                    [{"command": command, "allowed": True}], root, runner.ROOT)
                self.assertTrue(runner.decision_checks(seen, {"read": "official.md"})["read_before_operation"])
                if not exit_code:
                    self.assertTrue(runner.decision_checks(seen, {"prior": "official.md"})["prior_read"])

    def test_search_patterns_are_not_forbidden_read_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expect = {"forbid_read": r"\.env|history|cloudflare"}
            for name, arguments in [("Grep", {"pattern": "cloudflare", "path": "."}),
                                    ("Bash", {"command": "rg -n cloudflare ."})]:
                seen = runner.observe("claude", [call(name, arguments)], [], root, runner.ROOT)
                self.assertTrue(all(runner.decision_checks(seen, expect).values()))
            seen = runner.observe("claude", [call("Grep", {"pattern": "TOKEN", "path": ".", "glob": "*.env"})],
                                  [], root, runner.ROOT)
            self.assertFalse(all(runner.decision_checks(seen, expect).values()))

    def test_local_fact_requires_both_read_delivery_and_the_fixture_value(self) -> None:
        case = runner.yaml.safe_load((runner.ROOT / "evals/multi-turn/docs-concision-adjacent/turns.yaml").read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            for name, content in case["fixture_files"].items():
                (workspace / name).write_text(content)
            expect = case["turns"][0]["expect"]
            value = runner.re.search(expect["answer_from_fixture"]["capture"],
                                     case["fixture_files"]["local.py"])[1]
            for host in ("codex", "claude"):
                correct = expect["answer_from_fixture"]["forms"][0].format(value=value)
                for delivered, final, passed in [
                    (True, correct, True), (False, correct, False),
                    (True, f"\u0060MAX_RETRIES\u0060 is \u0060{value}\u0060.", True),
                    (True, correct.rstrip("."), True),
                    (True, f"MAX_RETRIES in local.py is set to {value}.", True),
                    (True, f"In local.py, MAX_RETRIES is set to {value}.", True),
                    (False, f"In local.py, MAX_RETRIES is set to {value}.", False),
                    (True, f"In local.py, MAX_RETRIES is set to {int(value) + 1}.", False),
                    (True, f"In local.py, MAX_RETRIES is set to {value}, or {int(value) + 1}.", False),
                    (True, f"In local.py, MAX_RETRIES is set to {value}. MAX_RETRIES is {int(value) + 1}.", False),
                    (False, "I can't read the file.", False),
                    (True, f"I cannot determine the value; it might be {value}.", False),
                    (True, "MAX_RETRIES is an unsupported value.", False),
                ]:
                    command = "cat local.py"
                    if host == "codex":
                        messages = [{"type": "item.completed", "item": {
                            "type": "command_execution", "command": command,
                            "exit_code": 0 if delivered else 1,
                            "aggregated_output": case["fixture_files"]["local.py"] if delivered else "refused"}},
                            {"type": "item.completed", "item": {"type": "agent_message", "text": final}}]
                    else:
                        use = call("Read", {"file_path": "local.py"})
                        use["message"]["content"][0]["id"] = "read"
                        messages = [use, {"type": "user", "message": {"content": [{
                            "type": "tool_result", "tool_use_id": "read",
                            "is_error": not delivered, "content": "source" if delivered else "refused"}]}},
                            {"type": "result", "result": final}]
                    (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [
                        {"type": "turn_marker", "turn": 1}, *messages])))
                    (root / "shell-events.jsonl").write_text(json.dumps({"command": command, "allowed": delivered}))
                    self.assertEqual(runner.score_turns(host, case["turns"], root)["passed"], passed,
                                     (host, delivered, final))
            # The source is authoritative, rather than a second hardcoded expected value.
            content = case["fixture_files"]["local.py"].replace(value, str(int(value) + 1))
            (workspace / "local.py").write_text(content)
            seen = runner.observe("claude", [{"type": "result", "result": correct}], [], root, runner.ROOT)
            self.assertFalse(runner.decision_checks(seen, expect)["fixture_answer"])
            updated_value = runner.re.search(expect["answer_from_fixture"]["capture"], content)[1]
            seen["final"] = f"In local.py, MAX_RETRIES is set to {updated_value}."
            self.assertTrue(runner.decision_checks(seen, expect)["fixture_answer"])

    def test_owner_must_precede_required_read_on_read_only_turns(self) -> None:
        expect = {"owner_before_read": {"owner": "docs-lookup", "read": r"private-context\.md"}}
        for sequence, passed in [
            ([("skill", "docs-lookup"), ("read", "private-context.md")], True),
            ([("read", "private-context.md"), ("skill", "docs-lookup")], False),
            ([("skill", "github-plan"), ("read", "private-context.md")], False),
            ([("skill", "docs-lookup")], False),
            ([("skill", "docs-lookup"), ("read", "local-context.toml"),
              ("read", "private-context.md")], True),
            ([("read", "private-context.md"), ("skill", "docs-lookup"),
              ("read", "private-context.md")], False),
        ]:
            seen = {"operations": [], "sequence": sequence}
            self.assertEqual(runner.decision_checks(seen, expect)["owner_before_read"], passed)

    def test_missing_context_accepts_variants_and_rejects_an_authority_guess(self) -> None:
        expect = runner.yaml.safe_load((runner.ROOT / "evals/multi-turn/docs-concision-missing-context/turns.yaml").read_text())["turns"][0]["expect"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for final, passed in [
                ("The private docs source isn't configured; provide the local docs route.", True),
                ("The private docs source is unconfigured. Ask for its path.", True),
                ("I can't establish authority without the missing context.", True),
                ("Context is missing, so I cannot decide whether to use the Cloudflare dashboard; provide docs.", True),
                ("Context is missing; create the TXT record in the Cloudflare dashboard.", False),
                ("Use the Cloudflare dashboard.", False),
                ("I cannot inspect live records; the TXT record is managed in Cloudflare DNS for the zone.", False),
            ]:
                seen = runner.observe("claude", [{"type": "result", "result": final}], [], root, runner.ROOT)
                checks = runner.decision_checks(seen, {key: expect[key] for key in ("final_any", "final_none")})
                self.assertEqual(all(checks.values()), passed, final)

    def test_claude_redirect_is_not_first_try_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            messages = [
                call("Bash", {"command": "gh pr checks 17"}),
                call("Skill", {"skill": "shared:babysit-pr"}),
                {"type": "user", "message": {"content": [{"type": "text", "text": f"Base directory for this skill: {runner.ROOT / 'skills/babysit-pr'}\n"}]}},
                call("Bash", {"command": "uv run gh_pr_watch.py --pr 17 --watch"}),
            ]
            trace = root / "trace.jsonl"
            trace.write_text("\n".join(map(json.dumps, messages)))
            score = runner.score_run("claude", "github-ci-watch", root)
            self.assertFalse(score["checks"]["owner_before_first_operation"])
            self.assertFalse(score["checks"]["helper_first"])
            trace.write_text("\n".join(map(json.dumps, messages[1:])))
            self.assertTrue(runner.score_run("claude", "github-ci-watch", root)["passed"])

    def test_codex_requires_a_successful_read_before_the_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            read = f"cat {runner.ROOT}/skills/github/SKILL.md"
            (root / "shell-events.jsonl").write_text("\n".join(map(json.dumps, [
                {"command": read, "allowed": True},
                {"command": "uv run /catalog/skills/github/scripts/gh-pr.py merge 17", "allowed": False},
            ])))
            trace = root / "trace.jsonl"
            for exit_code, passed in [(1, False), (0, True)]:
                trace.write_text(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": read, "exit_code": exit_code}}))
                self.assertEqual(runner.score_run("codex", "direction-merge", root)["passed"], passed)

    def test_a_foreign_catalog_cannot_supply_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = "cat /installed/skills/github/SKILL.md"
            (root / "shell-events.jsonl").write_text("\n".join(map(json.dumps, [
                {"command": command, "allowed": True},
                {"command": "uv run /catalog/skills/github/scripts/gh-pr.py merge 17", "allowed": False},
            ])))
            (root / "trace.jsonl").write_text(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": command, "exit_code": 0}}))
            score = runner.score_run("codex", "direction-merge", root)
            self.assertFalse(score["checks"]["tested_catalog_only"])
            self.assertFalse(score["checks"]["owner_before_first_operation"])

    def test_merge_arguments_are_checked_by_the_real_parser(self) -> None:
        self.assertTrue(runner.valid_merge_arguments(runner.ROOT, "uv run gh-pr.py --repo owner/repo merge 17 --method merge"))
        self.assertFalse(runner.valid_merge_arguments(runner.ROOT, "uv run gh-pr.py merge 17 --repo owner/repo --method merge"))

    def test_a_load_in_an_earlier_turn_does_not_cover_a_later_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {"type": "user", "message": {"content": [{"type": "text", "text": f"Base directory for this skill: {runner.ROOT / 'skills/github'}\n"}]}}
            launched = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "Launching skill: shared:github"}]}}
            merge = call("Bash", {"command": "uv run gh-pr.py --repo owner/repo merge 18 --method merge"})
            turns = [{"expect": {"owner": "github", "helper": "gh-pr.py"}}] * 2
            first = [{"type": "turn_marker", "turn": 1}, call("Skill", {"skill": "shared:github"}), launched, base, merge]
            for second, passed in [([merge], False), ([call("Skill", {"skill": "shared:github"}), launched, merge], True)]:
                (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [*first, {"type": "turn_marker", "turn": 2}, *second])))
                score = runner.score_turns("claude", turns, root)
                self.assertTrue(score["checks"]["turn1_owner_before_first_operation"])
                self.assertEqual(score["checks"]["turn2_owner_before_first_operation"], passed)

    def test_a_repeat_invocation_needs_an_earlier_load_from_the_tested_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launched = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "Launching skill: shared:github"}]}}
            merge = call("Bash", {"command": "uv run gh-pr.py merge 18"})
            (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [{"type": "turn_marker", "turn": 1}, launched, merge])))
            score = runner.score_turns("claude", [{"expect": {"owner": "github", "helper": "gh-pr.py"}}], root)
            self.assertFalse(score["checks"]["turn1_owner_before_first_operation"])

    def test_an_owner_without_one_helper_must_precede_the_first_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {"type": "user", "message": {"content": [{"type": "text", "text": f"Base directory for this skill: {runner.ROOT / 'skills/work-closeout'}\n"}]}}
            read = call("Bash", {"command": "git status"})
            remove = call("Bash", {"command": "git worktree remove ../task"})
            turns = [{"expect": {"owner": "work-closeout"}}]
            for messages, passed in [([read, remove, base], False), ([read, base, remove], True), ([read], False)]:
                (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [{"type": "turn_marker", "turn": 1}, *messages])))
                self.assertEqual(runner.score_turns("claude", turns, root)["passed"], passed)

    def test_decision_grades_judge_the_operations_and_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {"type": "user", "message": {"content": [{"type": "text", "text": f"Base directory for this skill: {runner.ROOT / 'skills/babysit-pr'}\n"}]}}
            expect = {"owner": ["github", "babysit-pr"], "operation": "--watch", "require": "--pr 21", "forbid": "--once", "final": "watching"}
            turns = [{"expect": expect}]
            for command, final, passed in [("uv run gh_pr_watch.py --pr 21 --watch", "Still watching.", True),
                                           ("uv run gh_pr_watch.py --pr 21 --once", "Still watching.", False),
                                           ("uv run gh_pr_watch.py --pr 21 --watch", "Done.", False)]:
                messages = [{"type": "turn_marker", "turn": 1}, base, call("Bash", {"command": command}),
                            {"type": "result", "result": final}]
                (root / "trace.jsonl").write_text("\n".join(map(json.dumps, messages)))
                self.assertEqual(runner.score_turns("claude", turns, root)["passed"], passed)

    def test_prior_proofs_must_come_before_the_first_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {"type": "user", "message": {"content": [{"type": "text", "text": f"Base directory for this skill: {runner.ROOT / 'skills/work-closeout'}\n"}]}}
            listing = call("Bash", {"command": "git ls-files --others --exclude-standard -z"})
            fast_forward = call("Bash", {"command": "git merge --ff-only 0123456789abcdef0123456789abcdef01234567"})
            turns = [{"expect": {"owner": "work-closeout", "prior": ["ls-files", "ls-files.*-z"]}}]
            failed_listing = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git ls-files --others --exclude-standard -z"}}]}}
            error = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "fatal"}]}}
            for messages, passed in [([base, listing, fast_forward], True), ([base, fast_forward, listing], False),
                                     ([base, failed_listing, error, fast_forward], False)]:
                (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [{"type": "turn_marker", "turn": 1}, *messages])))
                self.assertEqual(runner.score_turns("claude", turns, root)["passed"], passed)

    def test_the_boundary_refuses_git_writes_behind_read_subcommands(self) -> None:
        from shell_boundary import read_only
        self.assertTrue(read_only("git -C repo ls-files --others --exclude-standard -z"))
        for command in ("git hash-object -w notes.txt", "git branch -D main", "git config user.name x",
                        "git -c core.hooksPath=/dev/null merge --ff-only 0123", "git worktree remove task"):
            self.assertFalse(read_only(command), command)

    def test_the_boundary_allows_discovery_reads(self) -> None:
        from shell_boundary import read_only
        for command in (
            "git remote -v", "git remote", "git remote get-url --all origin",
            "find .. -name AGENTS.md -o -name .github",
            "find . -maxdepth 3 -type f -iname '*.md' -print0",
            "find . ! -path './.git/*' -print",
            "find . -path './.git' -prune -o -type f -print",
            r"find . -type f \( -name '*.md' -o -name '*.py' \) -print",
            "grep -nE 'add_argument|--repo' script.py",
            "grep -r --include='*.md' 'Finish Line' .",
            "rg --files | rg 'execution-scope.md$|repo-workflow.md$'",
            'rg "execution-scope.md$" .',
            "pwd && rg --files -g 'SKILL.md' | sed -n '1,80p'",
            "uv run skills/github/scripts/gh-pr.py --help",
            "uv run skills/github/scripts/gh-pr.py view -h",
            "test -e .git/MERGE_HEAD", "test ! -e .git/rebase-merge", "[ -d .git/sequencer ]",
        ):
            with self.subTest(command=command):
                self.assertTrue(read_only(command))

    def test_the_boundary_allows_sed_line_selections(self) -> None:
        from shell_boundary import read_only
        for command in (
            "sed -n '1,80p' file", "sed -n 10p file", "sed -n p file",
            "sed -n '1p; 3,5p' file other",
            "sed -n -e '1,80p' file", "sed -n '1,80p;' file",
            "cat file | sed -n '1,5p'", "sed -n '1,5p' 'file with spaces'",
        ):
            with self.subTest(command=command):
                self.assertTrue(read_only(command))

    def test_the_boundary_refuses_sed_writes_execution_and_extra_scripts(self) -> None:
        from shell_boundary import read_only
        # Classify only: none of these programs is executed.
        for command in (
            "sed -n '1w out.txt' file", "sed -n '1e touch x' file",
            "sed -n '1p; 2w out.txt' file", "sed -n 's/a/b/w out.txt' file",
            "sed -n 's/a/b/e' file", "sed -n 'p' -e 'w out.txt' file",
            "sed -n 'p' -f program.sed file", "sed -n 'p' -i file",
            "sed -n 'p' --in-place file", "sed -n -f program.sed file", "sed -n",
            "sed -n -e '1w out.txt' file", "sed -n -e '1p' -e 'e touch x' file",
            "sed -n -e",
        ):
            with self.subTest(command=command):
                self.assertFalse(read_only(command))

    def test_the_boundary_refuses_actions_near_discovery_reads(self) -> None:
        from shell_boundary import read_only
        for command in (
            "git remote update", "git remote add upstream example",
            "git remote set-url origin example", "git remote show origin",
            "find . -exec echo {} ';'", "find . -execdir echo {} +",
            "find . -delete", "find . -fprint results", "find . -ok echo {} ';'",
            "find . -name", "find . -unknown",
            r"find . \( -name '*.md'", r"find . \) -print",
            r"find . \( -exec echo {} + \)",
            "(cat file)", "find .; (curl example.com)",
            "grep text file > results", "grep text file 2>/dev/null",
            "grep text file 2>&1", "grep text file &>results",
            "cat < file", "cat file >> results",
            "rg '$VAR' .", 'rg "$VAR" .', "rg $VAR .",
            "rg '${VAR}' .", 'rg "$(pwd)" .', "rg '$(pwd)' .",
            'rg "$((1+1))" .', "rg `pwd` .", "rg 'unterminated",
            "find . -name '*.md' | sh", "grep text file; curl example.com",
            "grep text file && python3 -c 'print(1)'",
            "uv run skills/github/scripts/gh-pr.py view 45",
            "uv run skills/github/scripts/gh-pr.py --repo o/r merge 45 --help",
            "uv run --with x gh-pr.py --help", "uv run gh-pr.sh --help",
            "uv run skills/skill-creator/scripts/validate-skill-behavior.py --help",
            "test -e a -o -e b", "[ -e a", "test -n x",
        ):
            with self.subTest(command=command):
                self.assertFalse(read_only(command))

    def test_usage_sums_each_hosts_reported_tokens(self) -> None:
        claude = [{"type": "result", "usage": {"input_tokens": 5, "cache_read_input_tokens": 90, "cache_creation_input_tokens": 5, "output_tokens": 7}}] * 2
        codex = [{"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 4}}]
        self.assertEqual(runner.usage(claude), {"input": 200, "cached_input": 180, "output": 14})
        self.assertEqual(runner.usage(codex), {"input": 100, "cached_input": 80, "output": 4})

    def test_a_required_reference_is_read_before_the_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = {"type": "user", "message": {"content": [{"type": "text", "text": f"Base directory for this skill: {runner.ROOT / 'skills/github'}\n"}]}}
            turns = [{"expect": {"owner": "github", "read": "cli-reference\\.md"}}]
            view = call("Bash", {"command": "uv run scripts/gh-pr.py view 23"})

            def read(name: str, arguments: dict[str, str], error: bool = False) -> list[dict]:
                use = call(name, arguments)
                use["message"]["content"][0]["id"] = arguments.get("file_path", arguments.get("command"))
                return [use, {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": use["message"]["content"][0]["id"], "is_error": error}]}}]

            by_tool = read("Read", {"file_path": "/catalog/skills/github/references/cli-reference.md"})
            by_shell = read("Bash", {"command": "sed -n 170,230p references/cli-reference.md"})
            failed = read("Read", {"file_path": "/catalog/skills/github/references/cli-reference.md"}, error=True)
            listing = read("Bash", {"command": "ls references/cli-reference.md"})
            for messages, passed in [([base, *by_tool, view], True), ([base, *by_shell, view], True),
                                     ([base, view, *by_tool], False), ([base, view], False),
                                     ([base, *failed, view], False), ([base, *listing, view], False)]:
                (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [{"type": "turn_marker", "turn": 1}, *messages])))
                self.assertEqual(runner.score_turns("claude", turns, root)["passed"], passed)

    def test_a_pipe_is_a_read_only_when_every_stage_is(self) -> None:
        self.assertTrue(runner.read_only("rg --files -g 'SKILL.md' | sed -n '1,80p'"))
        self.assertFalse(runner.read_only("cat script | sh"))
        self.assertFalse(runner.read_only("ls | xargs rm"))

    def test_duplicate_startup_context_fails_the_evaluation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hook = {"type": "system", "subtype": "hook_response", "output": "# Using Skills\n"}
            (root / "trace.jsonl").write_text("\n".join(map(json.dumps, [hook, hook])))
            self.assertFalse(runner.score_run("claude", "merge-discussion-only", root)["passed"])


if __name__ == "__main__":
    unittest.main()
