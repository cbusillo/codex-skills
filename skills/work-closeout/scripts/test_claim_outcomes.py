#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["tree-sitter==0.26.0", "tree-sitter-bash==0.25.1", "tree-sitter-javascript==0.25.0"]
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

    def test_native_output_string_reads_remain_bounded(self) -> None:
        for operation in ("trim()", "slice(0,100)", 'split("\\n")'):
            source = (
                'const r=await tools.exec_command({cmd:"uv run gh-plan.py --repo test/a show 5"});text(r.output.' + operation + ");"
            )
            with self.subTest(operation=operation):
                self.assertEqual(
                    check_claims.history.literal_targets(invocation(source), SESSION),
                    {"targets": set(), "unresolved_invocations": []},
                )

    def test_mutation_and_shadowing_cannot_hide_execution(self) -> None:
        sources = [
            'for(const cmd of ["x"]){function build(cmd){return cmd.includes("return eval");} function invoke(cmd){return cmd.includes("tools.exec_command({cmd:\'uv run gh-plan.py claim 5\'})");} invoke({includes:build({includes:build({includes:[].filter.constructor})})});}',
            '<!-- function run(){}\nrun(load("claim"));',
            "<!-- '\nawait tools.exec_command({cmd:\"uv run gh-plan.py claim 5\"});\n//'",
        ]
        for operator in ("++", "--", "<<=1", ">>=1", ">>>=1"):
            sources.append(
                'const s=[["a",1],["a",2]]; await Promise.all(s.map(([repo,n])=>tools.exec_command({'
                "cmd:`uv run gh-plan.py --repo test/${repo} claim ${n}`,max_output_tokens:bump()})));"
                "function bump(){s[1][1]" + operator + ";return 1000;}"
            )
        for assignment in ("for(s[1][1] of [3]);", "for(s[1][1] in {3:0});", "for([s[1][1]] of [[3]]);"):
            sources.append(
                'const s=[["a",1],["a",2]]; await Promise.all(s.map(([repo,n])=>tools.exec_command({'
                "cmd:`uv run gh-plan.py --repo test/${repo} claim ${n}`,max_output_tokens:bump()})));"
                "function bump(){" + assignment + "return 1000;}"
            )
        sources.append("text({\"tools.exec_command({cmd:'uv run gh-plan.py claim 5'})\":{toJSON:1?eval:0}});")
        for source in sources:
            for prefix in ("", f"text(await tools.exec_command({{cmd:{json.dumps(COMMAND)}}}));"):
                with self.subTest(source=source, prefix=prefix):
                    report = check_claims.history.literal_targets(invocation(prefix + source), SESSION)
                    self.assertTrue(report["unresolved_invocations"])
                    if prefix:
                        self.assertIn(("test/a", 5), report["targets"])

    def test_native_json_data_chains_remain_bounded(self) -> None:
        sources = [
            'const r=await tools.exec_command({cmd:"uv run gh-plan.py --repo test/a show 5"});const j=JSON.parse(r.output);'
            "text({keys:Object.keys(j),comments:j.comments?.map(x=>({body:x.body})),last:j.comments.slice(-1),"
            'selected:j.items.filter(x=>x.name.toLowerCase().includes("claim")).map(x=>x.name)});',
            'const results=await Promise.allSettled([tools.exec_command({cmd:"uv run gh-plan.py --repo test/a show 5"})]);'
            "for(let i=0;i<results.length;i++){const r=results[i];try{const j=JSON.parse(r.value.output);text(j);}catch{text(r);}}",
            'const r=load("receipt");const j=JSON.parse(r.value.output);text({keys:Object.keys(j),issues:j.issues});',
        ]
        for source in sources:
            for prefix in ("", f"text(await tools.exec_command({{cmd:{json.dumps(COMMAND)}}}));"):
                with self.subTest(source=source, prefix=prefix):
                    self.assertEqual(
                        check_claims.history.literal_targets(invocation(prefix + source), SESSION),
                        {"targets": {("test/a", 5)} if prefix else set(), "unresolved_invocations": []},
                    )

    def test_recorded_command_pairs_and_map_indices_are_bounded(self) -> None:
        pairs = json.dumps([["read", "uv run gh-plan.py --repo test/a wait --help"], ["claim", COMMAND]])
        sources = [
            f"for(const [name,cmd] of {pairs}){{text({{name,...await tools.exec_command({{cmd,max_output_tokens:7500}})}});}}",
            'const roots=["first","second"];const results=await Promise.allSettled(roots.map((root,index)=>tools.exec_command({'
            "cmd:`uv run gh-plan.py --repo test/a claim ${index+1} > /private/log-${index+1}.json`,"
            "workdir:`/private/${root}`})));results.forEach((r,i)=>text({name:roots[i],...r}));",
        ]
        for source, targets in zip(sources, ({("test/a", 5)}, {("test/a", 1), ("test/a", 2)}), strict=True):
            with self.subTest(source=source):
                self.assertEqual(
                    check_claims.history.literal_targets(invocation(source), SESSION),
                    {"targets": targets, "unresolved_invocations": []},
                )

    def test_structural_command_fields_cannot_be_replaced_by_decoys(self) -> None:
        sources = [
            'text(await tools.exec_command({a:1 /*, cmd:"echo ok", b:"*/, cmd:"uv run gh-plan.py --repo test/a claim 5", c:"x"}));',
            'text(await tools.exec_command({a:`${"`"}`,b:"`, cmd:\'echo ok\'}",cmd:"uv run gh-plan.py --repo test/a claim 5"}));',
        ]
        for source in sources:
            with self.subTest(source=source):
                for prefix in ("", 'text(await tools.exec_command({cmd:"uv run gh-plan.py --repo test/b claim 9"}));'):
                    report = check_claims.history.literal_targets(invocation(prefix + source), SESSION)
                    self.assertEqual(report["targets"], {("test/a", 5), ("test/b", 9)} if prefix else {("test/a", 5)})

    def test_literal_decoding_respects_lexical_shadows_and_overflow(self) -> None:
        sources = [
            f'for(const [,cmd] of [["echo ok",{json.dumps(COMMAND)}]]){{text(await tools.exec_command({{cmd}}));}}',
            f'const cmd={json.dumps(COMMAND)};try{{throw "uv run gh-plan.py --repo test/a claim 6";}}catch(cmd){{text(await tools.exec_command({{cmd}}));}}',
            "const n=5;for(let n=0;n<2;n++){text(await tools.exec_command({cmd:`uv run gh-plan.py --repo test/a claim ${n}`}));}",
            f'const cmd={json.dumps(COMMAND)};{{text(await tools.exec_command({{cmd}}));const cmd=load("other");}}',
            'const o={"0":"","1":"constructor","2":"constructor",length:3,map:[].reduce};const factory=o.map((acc,key)=>acc[key]);'
            'const f={includes:factory}.includes("return eval");const evaluate={includes:f}.includes("ignored");'
            "text({\"tools.exec_command({cmd:'uv run gh-plan.py claim 5'})\":{toJSON:evaluate}});",
        ]
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(check_claims.history.literal_targets(invocation(source), SESSION)["unresolved_invocations"])
        values = json.dumps(list(range(check_claims.history.LITERAL_LOOP_VALUES)))
        source = f"for(const a of [0,1]){{for(const b of {values}){{text(await tools.exec_command({{cmd:`uv run gh-plan.py --repo test/a claim ${{a+1}}${{b+1}}`}}));}}}}"
        self.assertTrue(check_claims.history.literal_targets(invocation(source), SESSION)["unresolved_invocations"])

    def test_interactive_capability_aliases_keep_the_pending_claim_context(self) -> None:
        initial = 'text(await tools.write_stdin({session_id:41,chars:"CMD=\\"uv run gh-plan.py claim 5\\"\\n"}));'
        for source in (
            'const w=tools["write_stdin"];await w({session_id:41,chars:"$CMD\\n"});',
            'const w=tools.write_stdin;await w({session_id:41,chars:"$CMD\\n"});',
            'const w=tools[load("method")];await w({session_id:41,chars:"$CMD\\n"});',
            r'text(await tools.wr\u0069te_stdin({session_id:41,chars:"$CMD\n"}));',
            r'text(await to\u006fls.write_stdin({session_id:41,chars:"$CMD\n"}));',
            'const t=tools;const m=load("method");await t[m]({session_id:41,chars:"$CMD\\n"});',
            'const m=load("method");await tools?.[m]({session_id:41,chars:"$CMD\\n"});',
            'await Object.values(tools)[0]({session_id:41,chars:"$CMD\\n"});',
            'await globalThis["to"+"ols"][load("method")]({session_id:41,chars:"$CMD\\n"});',
            'const a=[];a["con"+"structor"]["con"+"structor"](load("code"))();',
            r'const chars="$CMD\n";await tools.write_stdin({session_id:41,cmd:"echo ok",ch\u0061rs});',
        ):
            with self.subTest(source=source):
                rows = invocation(initial)
                rows.extend({"payload": {**row["payload"], "call_id": "followup"}} for row in invocation(source))
                report = check_claims.history.literal_targets(rows, SESSION)
                self.assertTrue(any(item["call_id"] == "followup" for item in report["unresolved_invocations"]))

    def test_comment_line_endings_and_invalid_keys_do_not_hide_claims(self) -> None:
        source = "// comment\rtext(await tools.exec_command({cmd:" + json.dumps(COMMAND) + "}));"
        self.assertIn(("test/a", 5), check_claims.history.literal_targets(invocation(source), SESSION)["targets"])
        for source in (
            'let i=0;i /*\n*/--> `\ntext(await tools.exec_command({cmd:"uv run gh-plan.py claim 5"}));\n//`;',
            'text(await tools.exec_command({"\x00":1,cmd:"uv run gh-plan.py claim 5"}));',
        ):
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
        sequence = source + 'text(await tools.exec_command({cmd:"git status --short"}));'
        self.assertEqual(
            check_claims.history.literal_targets(invocation(sequence, output), SESSION),
            {"targets": set(), "unresolved_invocations": []},
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
            'const box={includes:[].map.constructor};const f=box.includes("return eval");'
            'const evaluate={includes:f}.includes("ignored");'
            "text({\"tools.exec_command({cmd:'uv run gh-plan.py claim 5'})\":{toJSON:evaluate}});",
            'const o={"0":"","1":"constructor","2":"constructor",length:3,map:[].reduce};'
            'const factory=o.map((acc,key)=>acc[key]);const f={includes:factory}.includes("return eval");'
            'const evaluate={includes:f}.includes("ignored");'
            "text({\"tools.exec_command({cmd:'uv run gh-plan.py claim 5'})\":{toJSON:evaluate}});",
            'text({note:"gh-plan.py claim",eval});',
            'text({note:"gh-plan.py claim",toJSON});',
            'text({toJSON:()=>tools.exec_command({cmd:"uv run gh-plan.py claim 5"})});',
            'store("claim",()=>tools.exec_command({cmd:"uv run gh-plan.py claim 5"}));',
            'const xs=[1]; await Promise.all(xs.map(x=>()=>tools.exec_command({cmd:"uv run gh-plan.py claim 5"})));',
            'const r=load("claim");text(JSON.parse(r.output,r.reviver));',
            'const r=load("claim");r["constructor"](r.output);',
            'const r=load("claim");r["constr"+"uctor"](r.output);',
            'const r=load("claim");r.map?.(x=>text(x));',
            'with(load("claim")){text(value);}',
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
