#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["tree-sitter==0.26.0", "tree-sitter-bash==0.25.1", "tree-sitter-javascript==0.25.0"]
# ///
from __future__ import annotations

import json
import unittest
from pathlib import Path

import check_claims

SESSION = "native-session"
COMMAND = "uv run gh-plan.py --repo test/a claim 5 --session native-session"


def invocation(source: str, output: object = "") -> list[dict]:
    return [
        {"payload": {"type": "custom_tool_call", "name": "exec", "call_id": "call", "input": source}},
        {"payload": {"type": "custom_tool_call_output", "call_id": "call", "output": output}},
    ]


class RecordedClaimOutcomeTests(unittest.TestCase):
    def test_read_only_discovery_cells_do_not_create_claim_uncertainty(self) -> None:
        reads = [
            'await Promise.allSettled([(async()=>text(await tools.exec_command({cmd:"gh-plan.py claim --help"})))(),'
            '(async()=>text(await tools.exec_command({cmd:"gh-plan.py --repo test/a show 5 --full"})))()]);',
            'const results=await Promise.allSettled([tools.exec_command({cmd:"cat brief-rules.md"}),'
            'tools.exec_command({cmd:"rg claims MEMORY.md"}),tools.exec_command({cmd:"cat DIRECTION.md"}),'
            'tools.exec_command({cmd:"pwd; git status --short --branch; git remote -v; rg --files"})]);'
            'results.forEach((r,i)=>text({i,...r}));'
            r'text(ALL_TOOLS.filter(x=>/launchplane|github|skills\.|computer/i.test(x.name+" "+x.description))'
            '.map(x=>({name:x.name,description:x.description.slice(0,150)})));',
            'text(ALL_TOOLS.filter(x=>/github_fetch_issue$|github_fetch_issue_comments$|computer/.test(x.name))'
            '.map(x=>({name:x.name,description:x.description.slice(0,350)})));',
            'text(ALL_TOOLS.filter(x=>/computer|screenshot|app.*launch|window/i.test(x.name+" "+x.description))'
            '.map(x=>({name:x.name,description:x.description.slice(0,160)})));'
            'text((await tools.exec_command({cmd:"printenv CODEX_THREAD_ID"})).output);',
            'await Promise.allSettled(['
            '(async()=>{let r=await tools.mcp__codex_apps__github_fetch_issue('
            '{issue_number:5,repository_full_name:"test/a"});store("issue",r);text(r)})(),'
            '(async()=>{let r=await tools.mcp__codex_apps__github_fetch_issue_comments('
            '{issue_number:5,repo_full_name:"test/a"});store("comments",r);text(r)})(),'
            '(async()=>text(await tools.exec_command({cmd:"cat README.md"})))()]);',
            'text(await tools.mcp__codex_apps__github_fetch_issue_comments('
            '{issue_number:5,repo_full_name:"test/a"}).then(r=>r.structuredContent.comments.map(c=>({id:c.id,url:c.url}))));',
            'const c=load("comments").structuredContent.comments;text(c.at(-1).body);'
            'await Promise.allSettled([(async()=>text(await tools.web__run({search_query:[{q:"SMB reconnect"}]})))(),'
            '(async()=>text(await tools.exec_command({cmd:"cat evidence.txt"})))()]);',
            'text(ALL_TOOLS.filter(x=>/github_(fetch|search)_\\w+/.test(x.name)||/\\bissue[s]?\\d*\\b/i.test(x.description)));',
            'text(ALL_TOOLS.filter(x=>{return /github/.test(x.name);}));text(/github/.test("github"));',
            'text("a,b; c".split(/[,;]\\s*/).map(x=>x.replace(/\\w+/,"item")));',
            'text(await tools.mcp__codex_apps__github_search({query:"claims"}));'
            'text(await tools.mcp__codex_apps__github_get_repo({repo:"test/a"}));'
            'text(await tools.mcp__codex_apps__github_list_recent_issues({repo:"test/a"}));',
            '(async function(){text(await tools.exec_command({cmd:"cat README.md"}));})();',
            '(async()=>text(await tools.exec_command({cmd:"cat README.md"})))().then(r=>text(r));',
        ]
        for source in reads:
            with self.subTest(source=source):
                self.assertEqual(
                    check_claims.history.literal_targets(invocation(source), SESSION),
                    {"targets": set(), "unresolved_invocations": []},
                )

    def test_native_thread_reads_preserve_claim_discovery(self) -> None:
        cells = json.loads((Path(__file__).parent / "fixtures/thread-read-cells.json").read_text())
        for identifier, source in cells.items():
            with self.subTest(call_id=identifier):
                self.assertEqual(
                    check_claims.history.literal_targets(invocation(source), SESSION),
                    {"targets": set(), "unresolved_invocations": []},
                )
                real_claim = source + f"text(await tools.exec_command({{cmd:{json.dumps(COMMAND)}}}));"
                report = check_claims.history.literal_targets(invocation(real_claim), SESSION)
                self.assertEqual(report["targets"], {("test/a", 5)})
                self.assertTrue(report["unresolved_invocations"])
                for opaque in (
                    source + 'text(await tools.exec_command({cmd:load("claim-command")}));',
                    source.replace("codex_tui__", "unknown__"),
                    source.replace("codex_tui__", "codex_tui__write_"),
                ):
                    self.assertTrue(check_claims.history.literal_targets(invocation(opaque), SESSION)["unresolved_invocations"])

    def test_immediate_callbacks_still_discover_real_uncertain_claims(self) -> None:
        for source in (
            f'(async()=>text((await tools.exec_command({{cmd:{json.dumps(COMMAND)}}})).output))();',
            f'await tools.mcp__codex_apps__github_fetch_issue({{}}).then(async r=>'
            f'text((await tools.exec_command({{cmd:{json.dumps(COMMAND)}}})).output));',
            f'tools.exec_command({{cmd:{json.dumps(COMMAND)}}}).then(r=>text(r));',
        ):
            with self.subTest(source=source):
                report = check_claims.history.literal_targets(invocation(source), SESSION)
                self.assertEqual(report["targets"], {("test/a", 5)})
                self.assertTrue(report["unresolved_invocations"])
                self.assertEqual(report["unresolved_invocations"][0]["commands"], [COMMAND])

    def test_immediate_callbacks_and_read_tools_do_not_admit_unknown_execution(self) -> None:
        for source in (
            '(async()=>load("runner")())();',
            '(async runner=>runner())(load("runner"));',
            'await tools.mcp__codex_apps__github_add_comment_to_issue({body:load("claim")});',
            'await tools.mcp__codex_apps__github_fetch_issue({}).then(r=>eval(r.body));',
            'load("result").then(r=>text(r));',
            '(async()=>{const tools=load("tools");await tools.exec_command({cmd:load("claim")});})();',
        ):
            with self.subTest(source=source):
                self.assertTrue(check_claims.history.literal_targets(invocation(source), SESSION)["unresolved_invocations"])

    def test_opaque_cells_do_not_report_an_empty_decoded_command_list(self) -> None:
        for source in ('load("runner")();', 'text(await tools.exec_command({cmd:load("claim")}));'):
            with self.subTest(source=source):
                report = check_claims.history.literal_targets(invocation(source), SESSION)
                self.assertTrue(report["unresolved_invocations"])
                self.assertNotIn("commands", report["unresolved_invocations"][0])

    def test_unsupported_calls_report_their_location_and_keep_real_targets(self) -> None:
        sources = [
            'tools.exec_command({cmd:"cat README.md"}).then(r=>text(r)).catch(e=>text(String(e)))',
            'tools.exec_command({cmd:"cat README.md"}).finally(()=>text("done"))',
            'Promise.all([]).then(r=>text(r))',
            'async function main(){} await main()',
            'load("runner")()',
            'tools.exec_command({cmd:load("command")})',
        ]
        for source in sources:
            for newline in ("\n", "\r\n"):
                with self.subTest(source=source, newline=newline):
                    prefix = f'text(await tools.exec_command({{cmd:{json.dumps(COMMAND)}}}));'
                    cell = prefix + newline + 'text("é🙂"); ' + source
                    report = check_claims.history.literal_targets(invocation(cell), SESSION)
                    self.assertEqual(report["targets"], {("test/a", 5)})
                    item = report["unresolved_invocations"][0]
                    self.assertEqual(item["commands"], [COMMAND])
                    refusal = item["refusals"][0]
                    line = cell.split(newline)[refusal["line"] - 1]
                    self.assertTrue(line[refusal["column"] - 1:].startswith(source))
                    self.assertTrue(refusal["reason"])

    def test_immediate_arrow_parameters_shadow_outer_commands(self) -> None:
        for pattern in ("command", "(command)"):
            with self.subTest(pattern=pattern):
                source = f'const command={json.dumps(COMMAND)};(async {pattern}=>'
                source += 'text(await tools.exec_command({cmd:command})))();'
                report = check_claims.history.literal_targets(invocation(source), SESSION)
                self.assertEqual(report["targets"], set())
                self.assertTrue(report["unresolved_invocations"])

    def test_lexical_refusals_point_to_the_original_cell(self) -> None:
        for source, fragment in ((r'const \u0063=1;', r'\u0063'), ('(', '('), ('/*/.test("x")', '/*')):
            with self.subTest(source=source):
                cell = 'text("é🙂");\r\n' + source
                report = check_claims.history.literal_targets(invocation(cell), SESSION)
                self.assertEqual(report["targets"], set())
                refusal = report["unresolved_invocations"][0]["refusals"][0]
                line = cell.split("\r\n")[refusal["line"] - 1]
                self.assertTrue(line[refusal["column"] - 1:].startswith(fragment))

    def assert_discovered_targets(self, rows: list[dict], targets: set[tuple[str, int]]) -> None:
        report = check_claims.history.literal_targets(rows, SESSION)
        self.assertEqual(report["targets"], targets)
        self.assertTrue(all(item["reason"] == check_claims.history.UNFINISHED_CLAIM for item in report["unresolved_invocations"]))

    def test_known_target_requires_an_ended_attempt(self) -> None:
        for source, output in (
            (
                f"text(await tools.exec_command({{cmd:{json.dumps(COMMAND)}}}));",
                {"session_id": 123, "exit_code": None, "output": ""},
            ),
            (f"text((await tools.exec_command({{cmd:{json.dumps(COMMAND)}}})).output);", ""),
            (f"await tools.exec_command({{cmd:{json.dumps(COMMAND)}}});", ""),
        ):
            with self.subTest(source=source):
                report = check_claims.history.literal_targets(invocation(source, output), SESSION)
                self.assertEqual(report["targets"], {("test/a", 5)})
                self.assertTrue(report["unresolved_invocations"])

    def test_completion_belongs_to_the_exact_ended_attempt(self) -> None:
        source = f"text(await tools.exec_command({{cmd:{json.dumps(COMMAND)}}}));"
        for command, output, ended in (
            (COMMAND, {"exit_code": 0, "output": ""}, True),
            (COMMAND, {"exit_code": None, "session_id": 123, "output": ""}, False),
            (COMMAND + " &", {"exit_code": 0, "output": ""}, False),
            ("bash -c '" + COMMAND + " >receipt.json 2>&1 &'", {"exit_code": 0, "output": ""}, False),
            ("eval '" + COMMAND + " >receipt.json 2>&1 &'", {"exit_code": 0, "output": ""}, False),
            ("bash <<'EOF'\n" + COMMAND + " >receipt.json 2>&1 &\nEOF", {"exit_code": 0, "output": ""}, False),
            (COMMAND, {"exit_code": 0, "session_id": 123, "output": ""}, False),
        ):
            with self.subTest(command=command, output=output):
                rows = invocation(source.replace(json.dumps(COMMAND), json.dumps(command)), output)
                report = check_claims.history.literal_targets(rows, SESSION)
                self.assertEqual(report["targets"], {("test/a", 5)})
                self.assertEqual(not report["unresolved_invocations"], ended)

    def test_terminal_native_events_require_unique_bounded_execution(self) -> None:
        source = f"const r=await tools.exec_command({{cmd:{json.dumps(COMMAND)}}});text(r.output);"
        event = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": SESSION,
                "started_at_ms": 1767225601000,
                "item": {
                    "type": "CommandExecution",
                    "id": "process",
                    "source": "unified_exec_startup",
                    "command": ["sh", "-c", COMMAND],
                    "status": "completed",
                    "exit_code": 0,
                },
            },
        }
        for kind in ("ended", "immediate", "prior", "foreign", "duplicate", "unknown_count", "overlap", "yielded", "while", "do", "local_function", "parameterless_callback"):
            with self.subTest(kind=kind):
                code = source
                if kind == "immediate":
                    code = f'await Promise.allSettled([(async()=>{{{source}}})()]);'
                if kind == "duplicate":
                    code = f"for(const n of [1,2]){{{source}}}"
                if kind == "while":
                    code = f"let i=0;while(i++<2){{{source}}}"
                if kind == "do":
                    code = f"let i=0;do{{{source}}}while(i++<2);"
                if kind == "local_function":
                    code = f"const run=async()=>{{{source}}};await run();run();"
                if kind == "parameterless_callback":
                    code = f"[1,2].forEach(()=>{{{source}}});"
                if kind == "unknown_count":
                    code = f'load("jobs").map(async n=>{{{source}}});'
                rows = invocation(code)
                rows[0]["timestamp"] = "2026-01-01T00:00:00Z"
                rows[1]["timestamp"] = "2026-01-01T00:00:02Z"
                item = json.loads(json.dumps(event))
                if kind == "prior":
                    item["payload"]["started_at_ms"] -= 2000
                if kind == "foreign":
                    item["payload"]["thread_id"] = "other"
                if kind in {"overlap", "yielded"}:
                    other = invocation('await tools.exec_command({cmd:"sleep 10"});')
                    other[0]["payload"]["call_id"] = "other"
                    other[1]["payload"]["call_id"] = "other"
                    if kind == "yielded":
                        other[1]["payload"]["output"] = "Script running with cell ID 99"
                        rows = [*other, *rows]
                    else:
                        rows = [other[0], *rows, other[1]]
                rows.append(item)
                if kind == "duplicate":
                    rows.append(item)
                report = check_claims.history.literal_targets(rows, SESSION)
                self.assertEqual(report["targets"], {("test/a", 5)})
                self.assertEqual(not report["unresolved_invocations"], kind in {"ended", "immediate"})

    def test_only_later_polls_from_the_same_process_generation_can_complete(self) -> None:
        claim = f"text(await tools.exec_command({{cmd:{json.dumps(COMMAND)}}}));"
        poll = 'text(await tools.write_stdin({session_id:123,chars:""}));'

        def renamed(source: str, output: object, identifier: str) -> list[dict]:
            return [{"payload": {**row["payload"], "call_id": identifier}} for row in invocation(source, output)]

        for kind in ("later", "earlier", "reuse", "partial"):
            with self.subTest(kind=kind):
                finished = renamed(poll, {"exit_code": 0, "output": ""}, "poll")
                rows = renamed(claim, {"session_id": 123, "exit_code": None, "output": ""}, "claim")
                if kind == "earlier":
                    rows = [*finished, *rows]
                else:
                    if kind == "reuse":
                        rows.extend(renamed('text(await tools.exec_command({cmd:"sleep 10"}));', {"session_id": 123}, "new"))
                    if kind == "partial":
                        rows.extend(
                            renamed(
                                'text(await tools.exec_command({cmd:"sleep 10"}));text(await tools.exec_command({cmd:"sleep 20"}));',
                                {"session_id": 456},
                                "new",
                            )
                        )
                    rows.extend(finished)
                report = check_claims.history.literal_targets(rows, SESSION)
                self.assertEqual(report["targets"], {("test/a", 5)})
                self.assertEqual(not report["unresolved_invocations"], kind == "later")

    def test_background_bash_results_do_not_prove_claim_completion(self) -> None:
        for request, result in ((True, {}), (False, {"backgroundTaskId": "task"}), (False, {})):
            with self.subTest(request=request, result=result):
                rows = [
                    {
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "bash",
                                    "name": "Bash",
                                    "input": {"command": COMMAND, "run_in_background": request},
                                }
                            ]
                        }
                    },
                    {
                        "toolUseResult": result,
                        "message": {"content": [{"type": "tool_result", "tool_use_id": "bash", "content": ""}]},
                    },
                ]
                report = check_claims.history.literal_targets(rows, SESSION)
                self.assertEqual(report["targets"], {("test/a", 5)})
                self.assertEqual(not report["unresolved_invocations"], not request and not result)

    def test_nested_background_argv_and_terminal_events_stay_unfinished(self) -> None:
        command = ["bash", "-lc", COMMAND + " >receipt.json 2>&1 &"]
        rows = [
            {
                "payload": {
                    "type": "function_call",
                    "name": "shell",
                    "call_id": "shell",
                    "arguments": json.dumps({"command": command}),
                }
            },
            {"payload": {"type": "function_call_output", "call_id": "shell", "output": ""}},
        ]
        event = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"type": "CommandExecution", "id": "command", "command": command, "status": "completed", "exit_code": 0},
            },
        }
        for records in (rows, [event]):
            with self.subTest(records=records):
                report = check_claims.history.literal_targets(records, SESSION)
                self.assertEqual(report["targets"], {("test/a", 5)})
                self.assertTrue(report["unresolved_invocations"])

    def test_foreign_saved_output_cannot_finish_an_own_session_attempt(self) -> None:
        rows = [
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "claim",
                            "name": "Bash",
                            "input": {"command": COMMAND, "run_in_background": True},
                        }
                    ]
                }
            },
            {
                "toolUseResult": {"backgroundTaskId": "task"},
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "claim",
                            "content": "<persisted-output>\nFull output saved to: /recorded/foreign/tool-results/result.txt\npreview",
                        }
                    ]
                },
            },
        ]
        for directory in (None, Path("/recorded/native-session/tool-results")):
            with self.subTest(directory=directory):
                report = check_claims.history.literal_targets(rows, SESSION, directory=directory)
                self.assertEqual(report["targets"], {("test/a", 5)})
                self.assertTrue(report["unresolved_invocations"])

    def test_native_plain_text_polls_preserve_process_generation(self) -> None:
        def native(name: str, identifier: str, arguments: dict, output: str) -> list[dict]:
            return [
                {"payload": {"type": "function_call", "name": name, "call_id": identifier, "arguments": json.dumps(arguments)}},
                {"payload": {"type": "function_call_output", "call_id": identifier, "output": output}},
            ]

        pending = "Chunk ID: launch\nWall time: 1 seconds\nProcess running with session ID 123\nOutput:\n"
        completed = "Chunk ID: poll\nWall time: 0.1 seconds\nProcess exited with code 0\nOutput:\n"
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                rows = native("exec_command", "claim", {"cmd": COMMAND}, pending)
                if reuse:
                    rows.extend(native("exec_command", "new", {"cmd": "sleep 10"}, pending))
                rows.extend(native("write_stdin", "poll", {"session_id": 123, "chars": ""}, completed))
                report = check_claims.history.literal_targets(rows, SESSION)
                self.assertEqual(report["targets"], {("test/a", 5)})
                self.assertEqual(bool(report["unresolved_invocations"]), reuse)

    def test_old_runtime_unprinted_non_tty_launches_keep_input_context(self) -> None:
        boundary = check_claims.history.CLOSED_STDIN_SINCE
        for version in (None, ".".join(map(str, (boundary[0], boundary[1] - 1, boundary[2])))):
            for javascript in (False, True):
                with self.subTest(version=version, javascript=javascript):
                    rows = [] if version is None else [{"type": "session_meta", "payload": {"cli_version": version}}]
                    command = "CMD='uv run gh-plan.py claim 5'; bash"
                    if javascript:
                        rows.extend(invocation(f"text(await tools.exec_command({{cmd:{json.dumps(command)}}}));"))
                    else:
                        rows.extend(
                            [
                                {
                                    "payload": {
                                        "type": "function_call",
                                        "name": "exec_command",
                                        "call_id": "call",
                                        "arguments": json.dumps({"cmd": command}),
                                    }
                                },
                                {
                                    "payload": {
                                        "type": "function_call_output",
                                        "call_id": "call",
                                        "output": "Process running with session ID 41",
                                    }
                                },
                            ]
                        )
                    later = 'const t=tools;const m=load("method");await t[m]({session_id:41,chars:"$CMD\\n"});'
                    rows.extend({"payload": {**row["payload"], "call_id": "later"}} for row in invocation(later))
                    self.assertTrue(check_claims.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_explicit_interactive_launches_do_not_require_printed_process_ids(self) -> None:
        boundary = check_claims.history.CLOSED_STDIN_SINCE
        versions = [None, ".".join(map(str, (boundary[0], boundary[1] - 1, boundary[2]))), ".".join(map(str, boundary))]
        command = "CMD='uv run gh-plan.py claim 5'; bash"
        later = 'const t=tools;const m=load("method");await t[m]({session_id:41,chars:"$CMD\\n"});'
        for version in versions:
            for javascript in (False, True):
                for output in ("", "Process running with session ID 41"):
                    with self.subTest(version=version, javascript=javascript, output=output):
                        rows = [] if version is None else [{"type": "session_meta", "payload": {"cli_version": version}}]
                        if javascript:
                            rows.extend(
                                invocation(f"text(await tools.exec_command({{cmd:{json.dumps(command)},tty:true}}));", output)
                            )
                        else:
                            rows.extend(
                                [
                                    {
                                        "payload": {
                                            "type": "function_call",
                                            "name": "exec_command",
                                            "call_id": "call",
                                            "arguments": json.dumps({"cmd": command, "tty": True}),
                                        }
                                    },
                                    {"payload": {"type": "function_call_output", "call_id": "call", "output": output}},
                                ]
                            )
                        rows.extend({"payload": {**row["payload"], "call_id": "later"}} for row in invocation(later))
                        self.assertTrue(check_claims.history.literal_targets(rows, SESSION)["unresolved_invocations"])

    def test_one_shot_shell_calls_do_not_create_an_input_channel(self) -> None:
        for method in ("shell_command", "shell"):
            with self.subTest(method=method):
                rows = [
                    {
                        "type": "session_meta",
                        "payload": {"cli_version": ".".join(map(str, check_claims.history.CLOSED_STDIN_SINCE))},
                    }
                ]
                rows.extend(invocation(f'text(await tools.{method}({{command:"uv run gh-plan.py show 5"}}));'))
                rows.extend(
                    {"payload": {**row["payload"], "call_id": "later"}} for row in invocation('text(String(load("result")));')
                )
                self.assertEqual(
                    check_claims.history.literal_targets(rows, SESSION), {"targets": set(), "unresolved_invocations": []}
                )

    def test_noninteractive_helper_reads_do_not_taint_later_formatting(self) -> None:
        followup = 'text(String(load("result")).replace(/x/g,"y"));'
        command = '"uv run gh-plan.py --repo test/a show 5"'
        for options in (f"cmd:{command}", f"cmd:{command},tty:false", f'cmd:{command},"tty":false'):
            with self.subTest(options=options):
                rows = [
                    {
                        "type": "session_meta",
                        "payload": {"cli_version": ".".join(map(str, check_claims.history.CLOSED_STDIN_SINCE))},
                    }
                ]
                rows.extend(invocation(f"text(await tools.exec_command({{{options}}}));", {"session_id": 123}))
                rows.extend({"payload": {**row["payload"], "call_id": "later"}} for row in invocation(followup))
                self.assertEqual(
                    check_claims.history.literal_targets(rows, SESSION), {"targets": set(), "unresolved_invocations": []}
                )
        rows = [{"type": "session_meta", "payload": {"cli_version": ".".join(map(str, check_claims.history.CLOSED_STDIN_SINCE))}}]
        rows.extend(
            invocation(
                f'text(await tools.exec_command({{cmd:{command}}}));text(await tools.write_stdin({{session_id:1,chars:""}}));',
                {"session_id": 123},
            )
        )
        rows.extend({"payload": {**row["payload"], "call_id": "later"}} for row in invocation(followup))
        self.assertEqual(check_claims.history.literal_targets(rows, SESSION), {"targets": set(), "unresolved_invocations": []})

    def test_unknown_and_historical_runtime_records_keep_input_uncertainty(self) -> None:
        boundary = check_claims.history.CLOSED_STDIN_SINCE
        supported = ".".join(map(str, boundary))
        older = ".".join(map(str, (boundary[0], boundary[1] - 1, boundary[2])))
        for versions in ([], [None], ["unknown"], [older], [supported + "-alpha"], [supported, older]):
            with self.subTest(versions=versions):
                rows = [{"type": "session_meta", "payload": {"cli_version": version}} for version in versions]
                rows.extend(invocation('text(await tools.exec_command({cmd:"uv run gh-plan.py show 5"}));', {"session_id": 123}))
                rows.extend(
                    {"payload": {**row["payload"], "call_id": "later"}} for row in invocation('const t=tools;await t[load("method")]({session_id:123,chars:load("input")});')
                )
                self.assertTrue(any(item["call_id"] == "later" and item["reason"] == "interactive_uncertain"
                                    for item in check_claims.history.literal_targets(rows, SESSION)["unresolved_invocations"]))

    def test_ambiguous_input_capabilities_keep_the_pending_context(self) -> None:
        command = '"uv run gh-plan.py --repo test/a show 5"'
        followup = 'const t=tools;await t[load("method")]({session_id:123,chars:load("input")});'
        sources = [
            f"text(await tools.exec_command({{cmd:{command},{options}}}));"
            for options in (
                "tty:true",
                "tty:undefined",
                "tty:0",
                "tty:null",
                "tty:false,tty:true",
                'tty:true,"tty":false',
                "...{}",
                "unknown:false",
                '["tty"]:false',
                '"t\\u0074y":false',
            )
        ]
        sources.extend(
            (
                f"const tty=true;text(await tools.exec_command({{cmd:{command},tty}}));",
                f"text(await tools.shell_command({{cmd:{command}}}));",
                f'text(await tools.exec_command({{cmd:{command}}}));text(load("old"));',
                f'text(await tools.exec_command({{cmd:{command},workdir:await tools.exec_command({{cmd:"sh",tty:true}})}}));',
            )
        )
        for source in sources:
            with self.subTest(source=source):
                rows = [
                    {
                        "type": "session_meta",
                        "payload": {"cli_version": ".".join(map(str, check_claims.history.CLOSED_STDIN_SINCE))},
                    }
                ]
                rows.extend(invocation(source, {"session_id": 123}))
                rows.extend({"payload": {**row["payload"], "call_id": "later"}} for row in invocation(followup))
                report = check_claims.history.literal_targets(rows, SESSION)
                self.assertTrue(any(item["call_id"] == "later" and item["reason"] == "interactive_uncertain"
                                    for item in report["unresolved_invocations"]))
        rows = [{"type": "session_meta", "payload": {"cli_version": ".".join(map(str, check_claims.history.CLOSED_STDIN_SINCE))}}]
        rows.extend(invocation(f"await tools.exec_command({{cmd:{command},tty:true}});"))
        rows.extend({"payload": {**row["payload"], "call_id": "later"}} for row in invocation(followup))
        self.assertTrue(any(item["call_id"] == "later" and item["reason"] == "interactive_uncertain"
                                    for item in check_claims.history.literal_targets(rows, SESSION)["unresolved_invocations"]))

    def test_finished_or_unexecuted_native_commands_cannot_start_input_context(self) -> None:
        followup = 'text(String(load("result")).replace(/x/g,"y"));'
        finished = {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "CommandExecution",
                    "id": "done",
                    "command": ["sh", "-c", "uv run gh-plan.py show 5"],
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": '{"session_id":123}',
                },
            },
        }
        source = 'text(await tools.exec_command({cmd:"cat <<EOF\\ngh-plan.py claim status\nEOF"}));'
        failure = [
            {"type": "input_text", "text": "Script failed\nWall time 0.0 seconds\nOutput:\n"},
            {"type": "input_text", "text": "Script error:\nSyntaxError: Invalid or unexpected token"},
        ]
        for prefix in ([finished], invocation(source, failure)):
            with self.subTest(prefix=prefix):
                rows = [*prefix, *({"payload": {**row["payload"], "call_id": "later"}} for row in invocation(followup))]
                self.assertEqual(
                    check_claims.history.literal_targets(rows, SESSION), {"targets": set(), "unresolved_invocations": []}
                )

    def test_created_issue_claims_preserve_receipts_without_clearing_arbitrary_execution(self) -> None:
        create = 'const q=s=>s.replaceAll("!", "!!");const body=load("body");'
        create += 'const r=await tools.exec_command({cmd:"uv run gh-plan.py --repo test/a create title --body "+q(body)});'
        claim = 'const p=JSON.parse(r.output);text(await tools.exec_command({cmd:"uv run gh-plan.py --repo test/a claim "+p.issue.number}));'
        sources = [
            (create, False, True),
            (create + claim, True, True),
            (create + claim + 'text(await tools.exec_command({cmd:"cat gates.log"}));', True, True),
            (
                'for(const item of [{title:"one"},{title:"two"}]){' + create + "text(JSON.parse(r.output).issue.number);}",
                False,
                True,
            ),
            (
                'text(await tools.apply_patch(load("patch")));for(const [pr,file] of [[1,"one"],[2,"two"]]){'
                "text(await tools.exec_command({cmd:`uv run gh-plan.py --repo test/a show ${pr} > ${file}`}));}",
                False,
                False,
            ),
        ]
        receipt = {"claim": {"session": SESSION}, "issue": {"repo": "test/a", "number": 6}}
        prefix = "text(await tools.exec_command({cmd:" + json.dumps(COMMAND) + "}));"
        for source, writes_claim, uncertain in sources:
            with self.subTest(source=source):
                report = check_claims.history.literal_targets(
                    invocation(prefix + source, json.dumps(receipt) if writes_claim else ""), SESSION
                )
                self.assertEqual(report["targets"], {("test/a", 5), ("test/a", 6)} if writes_claim else {("test/a", 5)})
                if uncertain:
                    self.assertTrue(report["unresolved_invocations"])
                else:
                    self.assertTrue(
                        all(item["reason"] == check_claims.history.UNFINISHED_CLAIM for item in report["unresolved_invocations"])
                    )

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
                self.assert_discovered_targets(invocation(source), {("test/a", 5)})

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
        self.assert_discovered_targets(invocation(source), {("test/a", 1), ("test/b", 2)})

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

    def test_metadata_regex_filters_preserve_native_claim_targets(self) -> None:
        filters = (
            'text(ALL_TOOLS.filter(x=>/github|skills|launchplane|session|supervisor/.test(x.name+" "+x.description))'
            ".map(x=>({name:x.name,description:x.description.slice(0,130)})).slice(0,110));",
            'const found=ALL_TOOLS.filter(x=>/launchplane|dokploy/i.test(x.name+" "+x.description));'
            "text(found.map(x=>({name:x.name,description:x.description.slice(0,160)})));",
        )
        for predicate in filters:
            for command in (
                'pwd; rg -n "claim|gh-plan.py" /fixture/MEMORY.md',
                COMMAND,
            ):
                source = f"const r=await tools.exec_command({{cmd:{json.dumps(command)},max_output_tokens:5000}});text(r.output);"
                with self.subTest(predicate=predicate, command=command):
                    report = check_claims.history.literal_targets(
                        invocation(
                            source + predicate,
                            json.dumps({"exit_code": 0, "output": ""}),
                        ),
                        SESSION,
                    )
                    self.assertEqual(
                        report["targets"],
                        {("test/a", 5)} if command == COMMAND else set(),
                    )
                    self.assertEqual(
                        {r["reason"] for r in report["unresolved_invocations"]},
                        {check_claims.history.UNFINISHED_CLAIM}
                        if command == COMMAND
                        else set(),
                    )
                    if command == COMMAND:
                        report = check_claims.history.literal_targets(
                            invocation(source + predicate, {"session_id": 41}), SESSION
                        )
                        self.assertEqual(report["targets"], {("test/a", 5)})
                        self.assertTrue(report["unresolved_invocations"])
                        self.assertTrue(
                            all(
                                r["reason"] == check_claims.history.UNFINISHED_CLAIM
                                for r in report["unresolved_invocations"]
                            )
                        )

    def test_metadata_regex_filters_do_not_break_native_poll_completion(self) -> None:
        predicate = 'text(ALL_TOOLS.filter(x=>/launchplane|dokploy/i.test(x.name+" "+x.description)));'
        for version in (None, "0.86.0", "0.164.0-alpha.1", "0.164.0"):
            with self.subTest(version=version):
                rows = (
                    []
                    if version is None
                    else [{"type": "session_meta", "payload": {"cli_version": version}}]
                )
                rows.extend(
                    invocation(
                        f"text(await tools.exec_command({{cmd:{json.dumps(COMMAND)}}}));",
                        {"session_id": 41},
                    )
                )
                rows.extend(
                    {"payload": {**r["payload"], "call_id": "metadata"}}
                    for r in invocation(predicate)
                )
                poll = 'text(await tools.write_stdin({session_id:41,chars:""}));'
                rows.extend(
                    {"payload": {**r["payload"], "call_id": "poll"}}
                    for r in invocation(poll, {"exit_code": 0})
                )
                report = check_claims.history.literal_targets(rows, SESSION)
                self.assertEqual(report["targets"], {("test/a", 5)})
                self.assertEqual(report["unresolved_invocations"], [])

    def test_regex_values_and_executable_shapes_remain_conditional(self) -> None:
        expressions = [
            "const r=/x/;text(ALL_TOOLS.filter(x=>r.test(x.name)));",
            'text(ALL_TOOLS.filter(x=>new RegExp("x").test(x.name)));',
            'text(ALL_TOOLS.filter(x=>RegExp("x").test(x.name)));',
            "text(ALL_TOOLS.filter(/x/.test));",
            "const r={value:/x/};",
            "const r=[/x/];",
            'const a=1;a\n/tools.exec_command({cmd:"uv run gh-plan.py claim 5"})/i.test(a);',
            'text(ALL_TOOLS.filter(x=>/x//**/.test(x.name)));',
            'text(ALL_TOOLS.filter(x=>/x/ /*comment*/ .test(x.name)));',
            'text(ALL_TOOLS.filter(x=>/*/.test(x.name)));',
        ]
        for flags in ("g", "y", "m", "s", "u", "v", "d", "ii"):
            expressions.append(f"text(ALL_TOOLS.filter(x=>/x/{flags}.test(x.name)));")
        for operation in (
            "exec(x.name)",
            "compile(x.name)",
            "test()",
            "test(x.name,x.description)",
            "test.call(x.name)",
            '["test"](x.name)',
            "test?.(x.name)",
            "lastIndex",
        ):
            separator = "" if operation.startswith("[") else "."
            expressions.append(f"text(ALL_TOOLS.filter(x=>/x/{separator}{operation}));")
        for method in ("replace", "match"):
            expressions.append(f"text(ALL_TOOLS.map(x=>x.name.{method}(/x/)));")
        expressions.append(
            "text(ALL_TOOLS.filter(x=>/'/.test(x.name)));const v=load(\"claim\");text(v);/'/;"
        )
        for source in expressions:
            with self.subTest(source=source):
                self.assertFalse(check_claims.history.claim_javascript.analyse(source)[0])
                prefix = (
                    f"text(await tools.exec_command({{cmd:{json.dumps(COMMAND)}}}));"
                )
                report = check_claims.history.literal_targets(
                    invocation(prefix + source), SESSION
                )
                self.assertIn(("test/a", 5), report["targets"])
                self.assertTrue(report["unresolved_invocations"])

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
            'const paths=["test_claim.py"];await Promise.allSettled(paths.map(async p=>text(await tools.exec_command({'
            'cmd:`uv run ${p} > ${p.split("/").at(-1).replace(".py","")}.log`}))));',
            'const jobs={one:"uv run gh-plan.py --repo test/a show 5 --format=json",two:"git status --short"};'
            "await Promise.allSettled(Object.entries(jobs).map(async([name,cmd])=>text(await tools.exec_command({cmd:`${cmd} > ${name}.log`}))));",
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
                    self.assert_discovered_targets(invocation(prefix + source), {("test/a", 5)} if prefix else set())

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
                self.assert_discovered_targets(invocation(source), targets)

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
