#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["tree-sitter==0.26.0", "tree-sitter-bash==0.25.1"]
# ///
from __future__ import annotations

import json
import unittest

import check_claims

SESSION = "native-session"
COMMAND = "uv run gh-plan.py --repo test/a claim 5 --session native-session"


def invocation(source: str, output: object = "") -> list[dict]:
    return [
        {"payload": {"type": "custom_tool_call", "name": "exec", "call_id": "call", "input": source}},
        {"payload": {"type": "custom_tool_call_output", "call_id": "call", "output": output}},
    ]


class RecordedClaimOutcomeTests(unittest.TestCase):
    def test_single_wrapped_claim_uses_its_prewrite_refusal(self) -> None:
        command = 'tools.exec_command({"cmd":"uv run gh-plan.py claim 5","max_output_tokens":1000})'
        refusal = {"operation": "github.plan.claim", "ok": False, "write_outcome": "not_started", "completed_steps": []}
        output = [{"type": "input_text", "text": json.dumps({"output": json.dumps(refusal), "exit_code": 1})}]
        for source in (
            f"text(await {command});",
            f"text((await {command}).output);",
            f"const r=await {command}; text(r.output);",
        ):
            with self.subTest(source=source):
                report = check_claims.history.literal_targets(invocation(source, output), SESSION)
                self.assertEqual(report, {"targets": set(), "unresolved_invocations": []})

    def test_wrapped_unknown_writes_and_additional_executions_stay_conditional(self) -> None:
        refusal = {"operation": "github.plan.claim", "ok": False, "write_outcome": "not_started"}
        command = 'tools.exec_command({cmd:"uv run gh-plan.py claim 5"})'
        for source, result in (
            (f"text(await {command});", {"session_id": 123, "output": ""}),
            (f"text(await {command});", {**refusal, "write_outcome": "unknown"}),
            (f"text(await {command});", {**refusal, "completed_steps": ["post_claim"]}),
            (f"text(await {command});", {**refusal, "claim_comment": {"id": 10}}),
            (f"text(await {command}); text(await {command});", refusal),
            (f"for (const n of [1,2]) text(await {command});", refusal),
            (f"while (again()) text(await {command});", refusal),
        ):
            with self.subTest(source=source, result=result):
                report = check_claims.history.literal_targets(invocation(source, json.dumps(result)), SESSION)
                self.assertTrue(report["unresolved_invocations"])

    def test_literal_command_arrays_bound_every_possible_target(self) -> None:
        commands = json.dumps(["uv run gh-plan.py --repo test/a wait 5", COMMAND])
        sources = [
            f"for(const cmd of {commands}) {{const r=await tools.exec_command({{cmd}});text(r.output);if(r.exit_code!==0)exit();}}",
            f"const cmds={commands}; for(const r of await Promise.allSettled(cmds.map(cmd=>tools.exec_command({{cmd}})))) text(r);",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(
                    check_claims.history.literal_targets(invocation(source), SESSION),
                    {"targets": {("test/a", 5)}, "unresolved_invocations": []},
                )

    def test_literal_repository_pairs_with_store_and_output_reads_are_bounded(self) -> None:
        source = (
            'store("evidenceDir","/private/scratch"); const subjects=[["a",1],["b",2]];'
            "const results=await Promise.allSettled(subjects.map(([repo,n])=>tools.exec_command({"
            "cmd:`uv run gh-plan.py --repo test/${repo} show ${n} --full > /private/scratch/${repo}-${n}.json`})));"
            "for(let i=0;i<results.length;i++)text({subject:subjects[i],...results[i]});"
        )
        self.assertEqual(
            check_claims.history.literal_targets(invocation(source), SESSION), {"targets": set(), "unresolved_invocations": []}
        )
        source = source.replace("show ${n}", "claim ${n}")
        self.assertEqual(
            check_claims.history.literal_targets(invocation(source), SESSION),
            {"targets": {("test/a", 1), ("test/b", 2)}, "unresolved_invocations": []},
        )

    def test_array_mutations_and_dynamic_execution_are_not_bounded(self) -> None:
        sources = [
            f"const cmds=[{json.dumps(COMMAND)}]; cmds.push(load('extra')); await Promise.all(cmds.map(cmd=>tools.exec_command({{cmd}})));",
            f"for(const cmd of [{json.dumps(COMMAND)}]) {{const run=tools.exec_command; await run({{cmd}});}}",
            f"const cmds=[{json.dumps(COMMAND)}]; await Promise.all(cmds.map(cmd=>tools.exec_command({{cmd:cmd=load('extra')}})));",
            'store("x",run()); const subjects=[["a",1]]; await Promise.all(subjects.map(([repo,n])=>tools.exec_command({cmd:`uv run gh-plan.py --repo test/${repo} claim ${n}`})));',
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(check_claims.history.literal_targets(invocation(source), SESSION)["unresolved_invocations"])

    def test_native_parse_failure_proves_the_cell_did_not_execute(self) -> None:
        source = 'text(await tools.exec_command({cmd:"cat <<EOF\\ngh-plan.py claim status\nEOF"}));'
        output = [
            {"type": "input_text", "text": "Script failed\nWall time 0.0 seconds\nOutput:\n"},
            {"type": "input_text", "text": "Script error:\nSyntaxError: Invalid or unexpected token"},
        ]
        self.assertEqual(
            check_claims.history.literal_targets(invocation(source, output), SESSION),
            {"targets": set(), "unresolved_invocations": []},
        )
        source_with_target = "text(await tools.exec_command({cmd:" + json.dumps(COMMAND) + ',workdir:"broken\npath"}));'
        self.assertEqual(
            check_claims.history.literal_targets(invocation(source_with_target, output), SESSION),
            {"targets": {("test/a", 5)}, "unresolved_invocations": []},
        )
        for continuation in ("\\\n", "\\\r\n"):
            valid = source.replace("status\nEOF", "status" + continuation + "EOF")
            self.assertTrue(check_claims.history.literal_targets(invocation(valid, output), SESSION)["unresolved_invocations"])

    def test_runtime_syntax_errors_cannot_erase_a_prior_claim(self) -> None:
        source = 'text(await tools.exec_command({cmd:"uv run gh-plan.py claim 5"})); eval("invalid(");'
        output = [
            {"type": "input_text", "text": "Script failed\nWall time 0.0 seconds\nOutput:\n"},
            {"type": "input_text", "text": "Script error:\nSyntaxError: Invalid or unexpected token"},
        ]
        sources = [
            source,
            source + "return;",
            source.replace("claim 5", r"claim 5 && sed 's/x/\1/' f"),
            source + "const regexp=/(?i:x)/;",
        ]
        for cell in sources:
            with self.subTest(cell=cell):
                self.assertTrue(check_claims.history.literal_targets(invocation(cell, output), SESSION)["unresolved_invocations"])

    def test_dynamic_dispatch_does_not_hide_sibling_claims(self) -> None:
        sources = [
            '#!function run()\nrun(load("claim"));',
            '// data\u2028eval(load("claim"));',
            'function run(){}; var run=[].filter.constructor; var run=run("return eval"); var run=run(); run(load("claim"));',
            "text({\"tools.exec_command({cmd:load('claim')})\":{toJSON:eval}});",
            'text("function eval ("); eval(load("claim"));',
            'const r=/function eval ()/; eval(load("claim"));',
            'text(`${eval(load("claim"))}`);',
            'const xs=[1]; await Promise.all(xs.map(n=>tools.exec_command({cmd:`uv run gh-plan.py --repo test/a claim ${n}`,max_output_tokens:(async()=>{}).constructor(load("claim"))()})));',
            'const xs=[{text:eval}]; await Promise.all(xs.map(({text})=>text(load("claim"))));',
            'function run(text){text(load("claim"));} run(eval);',
            "\\u0065val('tools.exec_command({cmd:\"uv run gh-plan.py claim 5\"})');",
            "await (async()=>{}).constructor('return tools.exec_command({cmd:\"uv run gh-plan.py claim 5\"})')()();",
            'await (async()=>{}).constructor(load("claim"))()();',
            'const xs=[{text:eval}]; await Promise.all(xs.map(({text})=>text(load("claim"))));',
            'for(const cmd of [{includes:eval}]){text(cmd.includes("claim"));}',
            'const run=tools[load("method")]; await run({cmd:load("claim")});',
            'Function("return tools.exec_comm"+"and")()({cmd:"gh-plan.py claim 5"});',
            'globalThis["tools"]["exec_"+"command"]({cmd:"gh-plan.py claim 5"});',
        ]
        for source in sources:
            for prefix in ("", f"for(const cmd of [{json.dumps(COMMAND)}]) {{await tools.exec_command({{cmd}});}} "):
                with self.subTest(source=source, prefix=prefix):
                    self.assertTrue(
                        check_claims.history.literal_targets(invocation(prefix + source), SESSION)["unresolved_invocations"]
                    )


if __name__ == "__main__":
    unittest.main()
