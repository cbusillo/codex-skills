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
    start = {"type": "session_meta", "payload": {"id": SESSION, "cwd": "/repo"}} if harness == "codex" else {"type": "user", "sessionId": SESSION, "cwd": "/repo"}
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


def file_execution_histories(harness):
    writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12 > claim.json'
    child, parent = history(harness, claim()), history(harness, claim())
    if harness == "codex":
        child[1]["payload"].update(name="apply_patch", arguments='*** Begin Patch\n*** Add File: /repo/run-claim.sh\n+' + writer + '\n*** End Patch')
        parent[1]["payload"]["arguments"] = json.dumps({"cmd": "bash /repo/run-claim.sh"})
    else:
        child[1]["message"]["content"][0].update(name="Write", input={"file_path": "/repo/run-claim.sh", "content": writer})
        parent[1]["message"]["content"][0]["input"] = {"command": "bash /repo/run-claim.sh"}
    child[2], parent[2] = output({}, harness, "tool-0"), output({}, harness, "tool-0")
    return [{"harness": harness, "session_id": SESSION, "records": rows} for rows in (parent, child)]


def ordinary_read_history(harness):
    """Native edits and subsequent reads from an ordinary regression-test task."""
    rows = history(harness)
    end = 2 if harness == "codex" else 1
    content = 'writer = "uv run gh-plan.py claim 1"\nassert "claim" in writer\n'
    edit = history(harness, {})[1:3]
    if harness == "codex":
        edit[0]["payload"].update(call_id="edit", name="apply_patch", arguments=
            '*** Begin Patch\n*** Add File: /repo/test_sample.py\n' +
            '\n'.join('+' + line for line in content.splitlines()) + '\n*** End Patch')
    else:
        edit[0]["message"]["content"][0].update(id="edit", name="Write", input={
            "file_path": "/repo/test_sample.py", "content": content})
    edit[1] = output({}, harness, "edit")
    rows[-end:-end] = edit
    commands = ('uv run /repo/test_sample.py', 'uv run ruff check /repo/test_sample.py',
                'python3 -m py_compile /repo/test_sample.py', 'timeout 60 cat /repo/test_sample.py',
                "python3 - <<'PY'\nline='gh-plan.py claim'\nif 'claim' in line: print(line)\nPY",
                'timeout 60 rg -n "gh-plan.py.*claim" skills',
                'bash -lc "sed -n 1,80p /repo/gh-plan.py | grep claim"')
    for index, command in enumerate(commands):
        identifier = f"read-{index}"
        native = history(harness, {})[1:3]
        if harness == "codex":
            native[0]["payload"].update(call_id=identifier, arguments=json.dumps({"cmd": command}))
        else:
            native[0]["message"]["content"][0].update(id=identifier, input={"command": command})
        native[1] = output({"exit_code": 0, "output": ""}, harness, identifier)
        rows[-end:-end] = native
    return rows


class ReceiptTests(unittest.TestCase):
    def test_codex_native_shell_surfaces_enforce_and_accept_claim_checks(self):
        for tool in ("shell_command", "shell", "local_shell_call"):
            for owning in (claim(), {"operation": "github.pr.create", "ok": True}):
                with self.subTest(tool=tool, operation=owning["operation"]):
                    rows = history("codex", owning, proof(("example/a", 1, 10)))
                    for index in (1, 3):
                        payload = rows[index]["payload"]
                        command = json.loads(payload["arguments"])["cmd"]
                        if tool == "local_shell_call":
                            payload.update(type=tool, action={"type": "exec", "command": ["/bin/bash", "-lc", command]})
                            payload.pop("arguments")
                            rows[index + 1]["payload"]["type"] = "local_shell_call_output"
                        else:
                            payload.update(name=tool, arguments=json.dumps({"command": command} if tool == "shell_command" else {"cmd": ["/bin/bash", "-lc", command]}))
                    without_check = [*rows[:3], *rows[-2:]]
                    self.assertFalse(session_record.summarize(without_check, "codex")["safe_verdict"])
                    self.assertTrue(receipts.ownership_state(rows, "codex")["indices"])
                    self.assertTrue(session_record.summarize(rows, "codex")["safe_verdict"])

    def test_visible_foreground_wrappers_finish_and_preserve_claim_disposition(self):
        helper = "/catalog/gh-plan.py --repo example/a claim 1"
        commands = ("timeout 300 uv run " + helper, "GH_PROMPT_DISABLED=1 uv run " + helper,
                    "env X=1 uv run " + helper, "uv run --directory /repo " + helper,
                    "uv run --script " + helper, "uv run python3 " + helper)
        for harness in ("codex", "claude"):
            for command in commands:
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, claim(), {**proof(("example/a", 1, 10)), "harness": harness})
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                        rows[3]["payload"]["arguments"] = json.dumps({"cmd": "timeout 300 uv run scripts/check_claims.py"})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                        rows[3]["message"]["content"][0]["input"] = {"command": "timeout 300 uv run scripts/check_claims.py"}
                    rows[2] = output({"exit_code": 0, "output": json.dumps(claim())}, harness, "tool-0")
                    self.assertFalse(receipts.ownership_state(rows, harness)["pending"])
                    self.assertEqual(receipts.target_records(rows, harness), {("example/a", 1)})
                    self.assertTrue(receipts.acceptance(rows, harness, SESSION)["clear"])
                    # A completed command still needs its own exact release proof.
                    missing = [*rows[:3], *rows[-(2 if harness == "codex" else 1):]]
                    self.assertFalse(receipts.acceptance(missing, harness, SESSION)["clear"])

    def test_claim_search_operand_does_not_become_an_ownership_action(self):
        for harness in ("codex", "claude"):
            for command in ("uv run gh-plan.py --repo example/a search claim",
                            "timeout 60 uv run gh-plan.py --repo=example/a search claim",
                            "uv run gh-plan.py show 1 --sections claim"):
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {})
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertFalse(receipts.ownership_state(rows, harness)["indices"])
                    self.assertTrue(receipts.acceptance(rows, harness, SESSION)["clear"])
        rows = history("codex", {})
        rows[1]["payload"].update(name="functions.exec", arguments=
            'const query="claim"; text(await tools.exec_command({cmd:`uv run gh-plan.py --repo example/a search ${query}`}));')
        self.assertFalse(receipts.ownership_state(rows, "codex")["indices"])

    def test_wrapped_pr_creation_keeps_repository_and_created_pr_provenance(self):
        url = "https://github.com/example/a/pull/2"
        for harness in ("codex", "claude"):
            for command, result in (("timeout 300 gh pr create --repo example/a", url),
                                    ("timeout 300 uv run gh-pr.py --repo example/a create",
                                     json.dumps({"operation": "github.pr.create", "ok": True, "url": url}))):
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {})
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    rows[2] = output({"exit_code": 0, "output": result}, harness, "tool-0")
                    self.assertFalse(receipts.ownership_state(rows, harness)["pending"])
                    self.assertEqual(receipts.target_repositories(rows, harness), {"example/a"})
                    self.assertEqual(receipts.created_prs(rows, harness), {url})

    def test_unexpanded_helper_executable_requires_ownership_disposition(self):
        for harness in ("codex", "claude"):
            for helper, action in (("gh-plan.py", "claim 1"), ("gh-pr.py", "create")):
                for variable in ('$GHP', '${GHP}'):
                    with self.subTest(harness=harness, helper=helper, variable=variable):
                        command = 'GHP=/catalog/' + helper + '; uv run "' + variable + '" --repo example/a ' + action
                        rows = history(harness, {}, {**proof(), "harness": harness})
                        if harness == "codex":
                            rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                        else:
                            rows[1]["message"]["content"][0]["input"] = {"command": command}
                        rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                        self.assertTrue(receipts.ownership_state(rows, harness)["pending"])
                        self.assertFalse(receipts.acceptance(rows, harness, SESSION)["clear"])

    def test_claude_shared_result_record_does_not_decode_an_unrelated_deep_read(self):
        rows = history("claude", claim(), {**proof(("example/a", 1, 10)), "harness": "claude"})
        rows[3]["message"]["content"].append({"type": "tool_use", "id": "read", "name": "Bash", "input": {"command": "cat saved.json"}})
        saved = claim()
        for _ in range(20):
            saved = {"output": saved}
        rows[4]["message"]["content"].extend(output(saved, "claude", "read")["message"]["content"])
        self.assertTrue(receipts.acceptance(rows, "claude", SESSION)["clear"])

    def test_json_quoted_literal_arguments_preserve_native_process_completion(self):
        rows = history("codex", claim())
        rows[1]["payload"].update(name="functions.exec", arguments=
            'text(await tools.exec_command({cmd:"cat docs.md","workdir":"/repo","max_output_tokens":1000}));'
            'text(await tools.exec_command({"cmd":"uv run gh-plan.py --repo example/a claim 1","yield_time_ms":1000}));')
        rows[2] = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "tool-0", "output": [
            {"type": "input_text", "text": json.dumps({"exit_code": 0, "output": "ordinary read"})},
            {"type": "input_text", "text": json.dumps({"session_id": 123, "output": ""})}]}}
        self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])
        poll = {"payload": {"type": "function_call", "name": "functions.exec", "call_id": "poll", "arguments":
            'text(await tools.write_stdin({"session_id":123,"chars":"","yield_time_ms":1000}));'}}
        rows[-2:-2] = [poll, output({"exit_code": 0, "output": json.dumps(claim())}, "codex", "poll")]
        self.assertFalse(receipts.ownership_state(rows, "codex")["pending"])

    def test_generated_python_writer_requires_execution_of_the_same_native_path(self):
        for harness in ("codex", "claude"):
            for script_path in ("/other/run-claim.py", "/repo/run-claim.py"):
                with self.subTest(harness=harness, path=script_path):
                    rows = history(harness, {})
                    content = 'import subprocess\nsubprocess.run(["uv", "run", "gh-plan.py", "claim", "1"])'
                    if harness == "codex":
                        rows[1]["payload"].update(name="apply_patch", arguments=
                            '*** Begin Patch\n*** Add File: /repo/run-claim.py\n' +
                            '\n'.join('+' + line for line in content.splitlines()) + '\n*** End Patch')
                    else:
                        rows[1]["message"]["content"][0].update(name="Write", input={"file_path": "/repo/run-claim.py", "content": content})
                    native = history(harness, {})[1:3]
                    if harness == "codex":
                        native[0]["payload"].update(call_id="execute", arguments=json.dumps({"cmd": "python3 " + script_path}))
                    else:
                        native[0]["message"]["content"][0].update(id="execute", input={"command": "python3 " + script_path})
                    native[1] = output({"exit_code": 0, "output": ""}, harness, "execute")
                    rows[3:3] = native
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], script_path == "/repo/run-claim.py")

    def test_read_commands_and_tests_do_not_acquire_file_ownership(self):
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness):
                self.assertFalse(receipts.ownership_state(ordinary_read_history(harness), harness)["indices"])
            for text in ('writer = "uv run gh-plan.py claim 1"\nassert "claim" in writer\n',
                         'writer = """\nuv run gh-plan.py claim 1\n"""\nprint(writer)\n'):
                rows = history(harness, {})
                if harness == "codex":
                    rows[1]["payload"].update(name="apply_patch", arguments=
                        '*** Begin Patch\n*** Add File: /repo/test_sample.py\n+' + text.replace('\n', '\n+') + '\n*** End Patch')
                else:
                    rows[1]["message"]["content"][0].update(name="Write", input={"file_path": "/repo/test_sample.py", "content": text})
                call = history(harness, {})[1:3]
                command = "uv run /repo/test_sample.py"
                if harness == "codex":
                    call[0]["payload"].update(call_id="read", arguments=json.dumps({"cmd": command}))
                else:
                    call[0]["message"]["content"][0].update(id="read", input={"command": command})
                call[1] = output({"exit_code": 0, "output": ""}, harness, "read")
                rows[3:3] = call
                with self.subTest(harness=harness, content=text):
                    self.assertFalse(receipts.ownership_state(rows, harness)["indices"])

    def test_wrapped_and_computed_reads_of_claim_text_are_not_writers(self):
        commands = ("python3 - <<'PY'\nline='gh-plan.py claim'\nif 'gh-plan.py' in line and 'claim' in line: print(line)\nPY",
                    'timeout 60 rg -n "gh-plan.py.*claim" skills',
                    'bash -lc "sed -n 1,80p /repo/gh-plan.py | grep claim"')
        for harness in ("codex", "claude"):
            for command in commands:
                rows = history(harness, {})
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                with self.subTest(harness=harness, command=command):
                    self.assertFalse(receipts.ownership_state(rows, harness)["indices"])
        rows = history("codex", {})
        rows[1]["payload"].update(name="functions.exec", arguments=
            'const files=["/repo/gh-plan.py"];for(const f of files) text(await tools.exec_command({cmd:`rg -n claim ${f}`}));')
        self.assertFalse(receipts.ownership_state(rows, "codex")["indices"])

    def test_quoted_pr_body_heredoc_is_data_for_ownership_and_detachment(self):
        for body in ('the "don\'t" rule', 'the "R&D policy'):
            rows = history("claude", {"operation": "github.pr.create", "ok": True}, {**proof(), "harness": "claude"})
            command = 'gh pr create --title T --body "$(cat <<\'EOF\'\n' + body + '\nEOF\n)"'
            rows[1]["message"]["content"][0]["input"] = {"command":command}
            rows[2] = output({"exit_code": 0, "output": ""}, "claude", "tool-0")
            with self.subTest(body=body):
                self.assertTrue(receipts.ownership_state(rows, "claude")["indices"])
                self.assertFalse(receipts.ownership_state(rows, "claude")["pending"])
        rows = history("claude", {})
        rows[1]["message"]["content"][0]["input"] = {"command":
            'echo "$(cat <<\'EOF\'; uv run gh-plan.py claim 1 &\nbody\nEOF\n)"'}
        self.assertTrue(receipts.ownership_state(rows, "claude")["pending"])
        rows[1]["message"]["content"][0]["input"] = {"command": "echo '$(uv run gh-plan.py claim 1)'"}
        self.assertFalse(receipts.ownership_state(rows, "claude")["indices"])

    def test_reads_of_deep_data_and_saved_receipts_do_not_change_acceptance(self):
        for harness in ("codex", "claude"):
            for value in (claim(), {"operation": "github.pr.create", "ok": True}):
                rows = history(harness, claim(), {**proof(("example/a", 1, 10)), "harness": harness})
                read = history(harness, {})[1:3]
                if harness == "codex":
                    read[0]["payload"].update(call_id="read", arguments=json.dumps({"cmd": "cat saved.json"}))
                else:
                    read[0]["message"]["content"][0].update(id="read", input={"command": "cat saved.json"})
                for depth in (0, 20):
                    cached = value
                    for _ in range(depth):
                        cached = {"output": cached}
                    read[1] = output(cached, harness, "read")
                    checked = [*rows[:-2], *read, *rows[-2:]] if harness == "codex" else [*rows[:-1], *read, rows[-1]]
                    with self.subTest(harness=harness, operation=value["operation"], depth=depth):
                        self.assertTrue(receipts.acceptance(checked, harness, SESSION)["clear"])
                # Malformed output of a real owner still refuses coverage.
                if harness == "codex":
                    read[0]["payload"]["arguments"] = json.dumps({"cmd": "uv run gh-plan.py claim 2"})
                else:
                    read[0]["message"]["content"][0]["input"] = {"command": "uv run gh-plan.py claim 2"}
                self.assertFalse(receipts.acceptance(checked, harness, SESSION)["clear"])

    def test_dynamic_read_does_not_borrow_ownership_words_from_another_command(self):
        for options in ('', 'workdir:root,'):
            rows = history("codex", {})
            rows[1]["payload"].update(name="functions.exec", arguments=
                'for (const ref of [1, 2]) { text(await tools.exec_command({cmd:`uv run gh-plan.py show ${ref} --full`})); }'
                'text(await tools.exec_command({' + options + 'cmd:"rg claim docs.md"}));')
            self.assertFalse(receipts.ownership_state(rows, "codex")["indices"])

    def test_computed_ownership_cannot_hide_its_words_in_a_data_command_field(self):
        for cell in (
            'const a={cmd:"claim"};text(await tools.exec_command({cmd:`uv run gh-plan.py ${a.cmd} 5`}));',
            'const a={cmd:"uv run gh-plan.py --repo example/a claim 1"};text(await tools.exec_command({cmd:a.cmd+" &"}));',
        ):
            rows = history("codex", {})
            rows[1]["payload"].update(name="functions.exec", arguments=cell)
            rows[2] = output({"exit_code": 0, "output": ""}, "codex", "tool-0")
            self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])

    def test_hidden_writer_cannot_borrow_saved_results_from_a_read(self):
        writer = 'await tools.exec_command({cmd:"uv run gh-plan.py --repo example/a claim 1"});'
        for read in ('text(await tools.exec_command({cmd:"cat saved-exec.json"}));',
                     'text((await tools.exec_command({cmd:"cat saved-exec.json"})).output);'):
            with self.subTest(read=read):
                rows = history("codex", {})
                rows[1]["payload"].update(name="functions.exec", arguments=writer + read)
                saved = {"exit_code": 0, "output": json.dumps({"exit_code": 0, "output": ""})}
                rows[2] = output(saved, "codex", "tool-0")
                self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])

    def test_aliased_and_bracketed_exec_ownership_stays_pending(self):
        command = '"uv run gh-plan.py --repo example/a claim 1"'
        for cell in ('const run=tools.exec_command;text(await run({cmd:' + command + '}));',
                     'text(await tools["exec_command"]({cmd:' + command + '}));',
                     'const run=tools.exec_command;text(await run({cmd:\n' + command + '}));'):
            with self.subTest(cell=cell):
                rows = history("codex", {})
                rows[1]["payload"].update(name="functions.exec", arguments=cell)
                rows[2] = output({"session_id": 123, "output": ""}, "codex", "tool-0")
                self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])

    def test_computed_reads_cannot_borrow_ownership_from_noncommand_js(self):
        for helper, note in (('gh-plan.py show', 'text("next: claim");'),
                             ('gh-plan.py show', 'const note="claim";text(note);'),
                             ('gh pr view', '// create the next report\n'),
                             ('gh-pr.py view', '/* create later */')):
            with self.subTest(helper=helper, note=note):
                rows = history("codex", {})
                rows[1]["payload"].update(name="functions.exec", arguments=
                    'for(const n of [1,2]) text(await tools.exec_command({cmd:`uv run ' + helper + ' ${n}`}));' + note)
                self.assertFalse(receipts.ownership_state(rows, "codex")["indices"])

    def test_ownership_help_is_read_only_on_both_harnesses(self):
        for harness in ("codex", "claude"):
            for command in ('uv run gh-plan.py claim --help', 'uv run gh-pr.py create --help', 'gh pr create --help'):
                rows = history(harness, {})
                if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                self.assertFalse(receipts.ownership_state(rows, harness)["indices"], command)
                self.assertFalse(receipts.claim_targets(rows, harness), command)
            rows = history(harness, {})
            command = 'gh pr create --title --help'
            if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            else: rows[1]["message"]["content"][0]["input"] = {"command": command}
            self.assertTrue(receipts.ownership_state(rows, harness)["indices"])

    def test_ownership_completion_does_not_require_wrappers_for_unrelated_reads(self):
        rows = history("codex", claim())
        rows[1]["payload"].update(name="functions.exec", arguments=
            'text(await tools.exec_command({cmd:"uv run gh-plan.py --repo example/a claim 1"}));'
            'text((await tools.exec_command({cmd:"cat docs.md"})).output);')
        rows[2] = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "tool-0", "output": [
            {"type": "input_text", "text": json.dumps({"exit_code": 0, "output": json.dumps(claim())})},
            {"type": "input_text", "text": "ordinary document text"}]}}
        self.assertFalse(receipts.ownership_state(rows, "codex")["pending"])

    def test_stdout_read_cannot_supply_or_poison_a_writers_native_result(self):
        self.check_read_cannot_supply_or_poison_a_writers_native_result(stdout=True)
        self.check_read_cannot_supply_or_poison_a_writers_native_result(stdout=False)

    def check_read_cannot_supply_or_poison_a_writers_native_result(self, *, stdout):
        read = ('text((await tools.exec_command({cmd:"cat saved-exec.json"})).output);' if stdout else
                'text(await tools.exec_command({cmd:"cat saved-exec.json"}));')
        writer = 'text(await tools.exec_command({cmd:"uv run gh-plan.py --repo example/a claim 1"}));'
        for read_first in (False, True):
            for state in ("missing", "running", "finished"):
                for cached in ({"exit_code": 0, "output": json.dumps(proof())},
                               {"session_id": 987, "output": ""}):
                    with self.subTest(read_first=read_first, state=state, cached=cached):
                        rows = history("codex", claim())
                        rows[1]["payload"].update(name="functions.exec", arguments=read + writer if read_first else writer + read)
                        native = None if state == "missing" else {"session_id": 123, "output": ""} if state == "running" else {"exit_code": 0, "output": json.dumps(claim())}
                        read_value = cached if stdout else {"exit_code": 0, "output": json.dumps(cached)}
                        blocks = [{"type": "input_text", "text": json.dumps(read_value)}]
                        if native is not None:
                            blocks.insert(1 if read_first else 0, {"type": "input_text", "text": json.dumps(native)})
                        blocks.insert(0, {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"})
                        rows[2] = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "tool-0", "output": blocks}}
                        self.assertEqual(receipts.ownership_state(rows, "codex")["pending"], state != "finished")

    def test_visible_serial_poll_results_bind_only_their_own_native_wrapper(self):
        self.check_visible_serial_poll_results(stdout=False)
        self.check_visible_serial_poll_results(stdout=True)

    def check_visible_serial_poll_results(self, *, stdout):
        for writer_finished in (False, True):
            rows = history("codex", claim())
            rows[2] = output({"session_id": 123, "output": ""}, "codex", "tool-0")
            poll = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "functions.exec", "call_id": "poll", "input":
                ('text((await tools.exec_command({cmd:"cat docs.md"})).output);' if stdout else
                 'text(await tools.exec_command({cmd:"cat docs.md"}));') +
                'text(await tools.write_stdin({session_id:123,chars:""}));'}}
            # The first read may print an old terminal/check report. Only the
            # second native wrapper describes the actual writer's process.
            cached = {"exit_code": 0, "output": json.dumps(proof())}
            read = cached if stdout else {"exit_code": 0, "output": json.dumps(cached)}
            writer = {"exit_code": 0, "output": json.dumps(claim())} if writer_finished else {"session_id": 123, "output": ""}
            result = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "poll", "output": [
                {"type": "input_text", "text": json.dumps(value)} for value in (read, writer)]}}
            rows[-2:-2] = [poll, result]
            self.assertEqual(receipts.ownership_state(rows, "codex")["pending"], not writer_finished)

    def test_file_provenance_covers_execution_in_another_native_history(self):
        for harness in ("codex", "claude"):
            nodes = file_execution_histories(harness)
            result = receipts.acceptance(nodes[0]["records"], harness, SESSION, histories=nodes)
            self.assertFalse(result["clear"])
            if harness == "codex":
                nodes[0]["records"][1]["payload"]["arguments"] = json.dumps({"cmd": "cat /repo/run-claim.sh"})
            else:
                nodes[0]["records"][1]["message"]["content"][0]["input"] = {"command": "cat /repo/run-claim.sh"}
            self.assertTrue(receipts.acceptance(nodes[0]["records"], harness, SESSION, histories=nodes)["clear"])

    def test_generated_ownership_script_execution_is_not_limited_to_bash(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12 > claim.json'
        for harness in ("codex", "claude"):
            for execute in ('source run-claim.sh', '. run-claim.sh', 'chmod +x run-claim.sh && ./run-claim.sh'):
                rows = history(harness, claim())
                command = "cat > run-claim.sh <<'EOF'\n" + writer + "\nEOF\n" + execute
                if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                rows[2] = output({}, harness, "tool-0")
                self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"], command)

    def test_native_file_writes_are_linked_to_later_script_execution(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12 > claim.json'
        patch_text = '*** Begin Patch\n*** Add File: /repo/run-claim.sh\n+' + writer + '\n*** End Patch'
        for harness, command, composed in ((h, c, m) for h, m in (("codex", False), ("codex", True), ("claude", False))
                                            for c in ("bash /repo/run-claim.sh > out.json", "bash < /repo/run-claim.sh", "cat < /repo/run-claim.sh | bash",
                                                      "bash <run-claim.sh", "cat <run-claim.sh | bash", "bash run-claim.sh>out.json")):
            rows = history(harness, claim())
            if harness == "codex":
                rows[1]["payload"].update(name="functions.exec" if composed else "apply_patch",
                                         arguments='text(await tools.apply_patch(' + json.dumps(patch_text) + '));' if composed else patch_text)
            else:
                rows[1]["message"]["content"][0].update(name="Write", input={"file_path": "/repo/run-claim.sh", "content": writer})
            rows[2] = output({}, harness, "tool-0")
            self.assertTrue(session_record.summarize(rows, harness)["safe_verdict"])
            executed = history(harness, claim())[1:3]
            if harness == "codex":
                executed[0]["payload"].update(call_id="execute", arguments=json.dumps({"cmd": command}))
            else:
                executed[0]["message"]["content"][0].update(id="execute", input={"command": command})
            executed[1] = output({}, harness, "execute")
            rows[3:3] = executed
            self.assertTrue(receipts.ownership_state(rows, harness)["pending"])
            self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"])

    def test_executable_wrappers_cannot_turn_hidden_ownership_into_no_write(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12 > claim.json'
        for harness in ("codex", "claude"):
            for command in ("bash <<'EOF'\n" + writer + "\nEOF",
                            "'bash' <<'EOF'\n" + writer + "\nEOF",
                            "sh -s <<'EOF'\n" + writer + "\nEOF",
                            "cat <<'EOF' | bash\n" + writer + "\nEOF",
                            "cat > run-claim.sh <<'EOF'\n" + writer + "\nEOF\nbash run-claim.sh",
                            "bash -c '" + writer + "'",
                            "python3 - <<'PY'\nimport os\nos.system(" + repr(writer) + ")\nPY"):
                rows = history(harness, claim(), {**proof(), "harness": harness})
                if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                rows[2] = output({}, harness, "tool-0")
                self.assertTrue(receipts.ownership_state(rows, harness)["pending"], command)
                self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"], command)

    def test_heredoc_bearing_ownership_still_requires_disposition_on_both_harnesses(self):
        for harness in ("codex", "claude"):
            for writer in ('uv run /catalog/gh-plan.py --repo client/repo claim 12 > claim.json',
                           'uv run /catalog/gh-pr.py --repo client/repo create > pr.json',
                           'gh pr create --repo client/repo > pr.json'):
                for command in (writer + " && python3 - <<'PY'\nprint('ok')\nPY",
                                "cat > note.md <<'EOF'\nexample data\nEOF\n" + writer,
                                writer + ' <<< "input"', writer + ' && echo "a<<b"',
                                "cat <<EOF\n$(" + writer + ")\nEOF",
                                "cat <<EOF\n`" + writer + "`\nEOF"):
                    rows = history(harness, claim())
                    if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                    rows[2] = output({}, harness, "tool-0")
                    self.assertTrue(receipts.ownership_state(rows, harness)["indices"], command)
                    self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"], command)
            # Mentions in inert heredoc bodies create no ownership obligation.
            rows = history(harness, claim())
            command = "cat <<'EOF'\nuv run /catalog/gh-plan.py claim 12 &\ngh pr create\nEOF"
            if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            else: rows[1]["message"]["content"][0]["input"] = {"command": command}
            rows[2] = output({}, harness, "tool-0")
            self.assertTrue(session_record.summarize(rows, harness)["safe_verdict"])

    def test_unparsed_composed_ownership_cannot_become_no_ownership(self):
        for cell in ('text(await tools.exec_command({cmd:`uv run /catalog/gh-plan.py --repo client/repo claim 5 > claim.json`}));',
                     'const cmd = "gh pr create --repo client/repo > pr.json"; text(await tools.exec_command({cmd:cmd}));',
                     'const cmd = "uv run /catalog/gh-pr.py create > pr.json"; text(await tools.exec_command({cmd}));'):
            rows = history("codex", claim(), proof())
            rows[1]["payload"].update(name="functions.exec", arguments=cell)
            rows[2] = output({"exit_code": 0, "output": ""}, "codex", "tool-0")
            self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])
            self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])

    def test_shorthand_claim_detachment_cannot_hide_behind_the_hash(self):
        command = 'uv run /catalog/gh-plan.py claim client/repo#7 > claim.json 2>&1 &'
        for harness in ("codex", "claude"):
            rows = history(harness, claim(), {**proof(), "harness": harness})
            if harness == "codex":
                rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            else:
                rows[1]["message"]["content"][0]["input"] = {"command": command}
            rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
            self.assertTrue(receipts.ownership_state(rows, harness)["pending"])
            self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"])

    def test_shorthand_claim_cannot_adopt_a_saved_no_write_refusal(self):
        refused = {"operation": receipts.CLAIM_OPERATION, "ok": False, "write_outcome": "not_started"}
        for harness in ("codex", "claude"):
            rows = history(harness, refused)
            command = 'uv run /catalog/gh-plan.py claim client/repo#7 > claim.json; cat refused.json'
            if harness == "codex":
                rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            else:
                rows[1]["message"]["content"][0]["input"] = {"command": command}
            self.assertFalse(receipts.no_write_claims(rows, harness))
            self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"])

    def test_hash_references_preserve_scope_and_shell_comments_remain_data(self):
        for harness in ("codex", "claude"):
            for scope, ref in (('', 'client/repo#7'), ('--repo client/repo ', "'#7'"), ('--repo client/repo ', '"#7"'),
                               ('', '\\\n client/repo#7'), ('--repo \\\n client/repo ', "'#7'"), ('', '"client/repo\\\n#7"')):
                rows = history(harness, claim())
                command = 'uv run /catalog/gh-plan.py ' + scope + 'claim ' + ref + ' > claim.json'
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                rows[2] = output({}, harness, "tool-0")
                self.assertEqual(receipts.target_records(rows, harness), {("client/repo", 7)})
                self.assertIn("client/repo", receipts.target_repositories(rows, harness))
        for command in ('echo quiet # ; uv run /catalog/gh-plan.py claim client/repo#7 &',
                        'echo "#" # ; uv run /catalog/gh-plan.py claim client/repo#7 &'):
            self.assertFalse(receipts.claim_commands("Bash", {"command": command}))
        command = 'uv run /catalog/gh-plan.py claim client/repo#7 # ignored &'
        self.assertTrue(receipts.isolated_command("Bash", {"command": command}, "gh-plan.py"))

    def test_hidden_composed_ownership_results_cannot_clear_pending_writers(self):
        for cell, result in (
            ('await tools.exec_command({cmd:"uv run scripts/gh-plan.py --repo example/a claim 5", yield_time_ms:1000});', ""),
            ('text(await tools.exec_command({cmd:"gh pr create --title x"})); await tools.exec_command({cmd:"uv run scripts/gh-plan.py --repo example/a claim 5"});', {"exit_code": 0, "output": ""}),
        ):
            rows = history("codex", claim(), proof())
            rows[1]["payload"].update(name="functions.exec", arguments=cell)
            rows[2] = output(result, "codex", "tool-0")
            self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])
            self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])
        # Visible foreground execution preserves the supported exit path.
        rows[1]["payload"]["arguments"] = 'text(await tools.exec_command({cmd:"uv run scripts/gh-plan.py --repo example/a claim 5"}));'
        self.assertTrue(session_record.summarize(rows, "codex")["safe_verdict"])
        # A normalized helper receipt is not another native execution result.
        rows[1]["payload"]["arguments"] += 'await tools.exec_command({cmd:"gh pr create --title x"});'
        rows[2] = output({"exit_code": 0, "output": json.dumps({**claim(), "exit_code": 0})}, "codex", "tool-0")
        rows[4] = output(proof(("example/a", 1, 10)), "codex", "tool-1")
        self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])
        self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])

    def test_shell_detached_ownership_never_uses_shell_exit_as_writer_completion(self):
        for harness in ("codex", "claude"):
            for command in ("uv run scripts/gh-plan.py --repo example/a claim 5 &",
                            "nohup gh pr create --title x &"):
                rows = history(harness, claim(), {**proof(), "harness": harness})
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"])
                self.assertTrue(receipts.ownership_state(rows, harness)["pending"])
            # Quoted data and ordinary foreground nohup are not detachment.
            command = 'nohup gh pr create --title "&"'
            if harness == "codex":
                rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            else:
                rows[1]["message"]["content"][0]["input"] = {"command": command}
            self.assertTrue(session_record.summarize(rows, harness)["safe_verdict"])

    def test_visible_composed_ownership_can_finish_through_an_outer_cell_poll(self):
        rows = history("codex", claim(), proof())
        rows[1]["payload"].update(name="functions.exec", arguments='text(await tools.exec_command({cmd:"uv run scripts/gh-plan.py --repo example/a claim 5"}));')
        rows[2] = output("Script running with cell ID owning-cell", "codex", "tool-0")
        poll = {"type": "response_item", "payload": {"type": "function_call", "name": "functions.wait", "call_id": "wait",
                "arguments": json.dumps({"cell_id": "owning-cell"})}}
        rows[3:3] = [poll, output({"exit_code": 0, "output": ""}, "codex", "wait")]
        self.assertTrue(session_record.summarize(rows, "codex")["safe_verdict"])
        rows[4] = output("", "codex", "wait")
        self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])
        # Each wait returns only new output, so preserve earlier visible results.
        rows[1]["payload"]["arguments"] += 'text(await tools.exec_command({cmd:"gh pr create --title x"}));'
        rows[2] = {"payload": {"type": "custom_tool_call_output", "call_id": "tool-0", "output": [
            {"type": "input_text", "text": "Script running with cell ID owning-cell\nWall time 0.1 seconds\nOutput:\n"},
            {"type": "input_text", "text": json.dumps({"exit_code": 0, "output": json.dumps({"exit_code": 0})})}]}}
        rows[4] = {"payload": {"type": "custom_tool_call_output", "call_id": "wait", "output": [
            {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
            {"type": "input_text", "text": json.dumps({"exit_code": 0, "output": ""})}]}}
        self.assertTrue(session_record.summarize(rows, "codex")["safe_verdict"])
        # An empty final wait cannot borrow saved native JSON from earlier stdout.
        rows[4]["payload"]["output"].pop()
        self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])
        self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])

    def test_every_process_in_an_ownership_batch_must_reach_its_terminal_result(self):
        rows = history("codex", claim())
        rows[1]["payload"].update(name="functions.exec", arguments='text(await tools.exec_command({cmd:"gh pr create --title x"})); text(await tools.exec_command({cmd:"uv run scripts/gh-plan.py --repo example/a claim 1 > claim.json"}));')
        rows[2] = {"payload": {"type": "custom_tool_call_output", "call_id": "tool-0", "output": [
            {"type": "input_text", "text": json.dumps({"session_id": handle, "output": ""})} for handle in (123, 456)]}}
        def poll(identifier, handle):
            return {"type": "response_item", "payload": {"type": "function_call", "name": "write_stdin", "call_id": identifier,
                    "arguments": json.dumps({"session_id": handle})}}
        checked = history("codex", proof())[1:-2]
        checked[0]["payload"]["call_id"] = "check"
        checked[1]["payload"]["call_id"] = "check"
        rows[-2:-2] = [poll("first", 123), output({}, "codex", "first"), *checked]
        self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])
        rows[-2:-2] = [poll("second", 456), output({}, "codex", "second")]
        self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])
        fresh = history("codex", proof())[1:-2]
        fresh[0]["payload"]["call_id"] = "fresh"
        fresh[1]["payload"]["call_id"] = "fresh"
        rows[-2:-2] = fresh
        self.assertTrue(session_record.summarize(rows, "codex")["safe_verdict"])

    def test_claude_background_ownership_needs_terminal_notice_before_a_new_check(self):
        checked = {**proof(), "harness": "claude"}
        rows = history("claude", claim(), checked)
        rows[1]["message"]["content"][0]["input"] = {"command": "gh pr create --title x", "run_in_background": True}
        rows[2]["toolUseResult"] = {"backgroundTaskId": "owned-task", "stdout": ""}
        rows[2]["message"]["content"][0]["content"] = "Command running in background with ID: owned-task."
        self.assertFalse(session_record.summarize(rows, "claude")["safe_verdict"])
        def notice(status):
            return {"type": "user", "sessionId": SESSION, "message": {"content":
                    f"<task-notification><task-id>owned-task</task-id><status>{status}</status></task-notification>"}}
        rows.insert(3, notice("running"))
        self.assertFalse(session_record.summarize(rows, "claude")["safe_verdict"])
        stop = {"type": "assistant", "sessionId": SESSION, "message": {"content": [
                {"type": "tool_use", "id": "stop", "name": "TaskStop", "input": {"task_id": "owned-task"}}]}}
        rows.insert(4, stop)
        self.assertFalse(session_record.summarize(rows, "claude")["safe_verdict"])
        rows[-1:-1] = [notice("completed")]
        self.assertFalse(session_record.summarize(rows, "claude")["safe_verdict"])
        fresh = history("claude", checked)[1:-1]
        fresh[0]["message"]["content"][0]["id"] = "fresh"
        fresh[1]["message"]["content"][0]["tool_use_id"] = "fresh"
        rows[-1:-1] = fresh
        self.assertTrue(session_record.summarize(rows, "claude")["safe_verdict"])

    def test_ownership_launched_before_the_check_must_finish_before_its_launch(self):
        for command, result in (("uv run scripts/gh-plan.py --repo example/a claim 5 > claim.json", {}),
                                ("gh pr create --title x", "https://github.com/example/a/pull/5")):
            rows = history("codex", claim(), proof())
            rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            # Codex writes both calls before either tool result.
            rows[2] = output(result, "codex", "tool-0")
            rows[2:4] = [rows[3], rows[2]]
            self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])
            missing = rows[:3] + rows[4:]
            self.assertFalse(session_record.summarize(missing, "codex")["safe_verdict"])
            # A new check after the terminal ownership result can clear.
            fresh = history("codex", proof())[1:-2]
            fresh[0]["payload"]["call_id"] = "fresh"
            fresh[1]["payload"]["call_id"] = "fresh"
            rows[-2:-2] = fresh
            self.assertTrue(session_record.summarize(rows, "codex")["safe_verdict"])

    def test_an_ownership_process_handle_stays_pending_until_its_terminal_poll(self):
        rows = history("codex", claim(), proof())
        rows[1]["payload"]["arguments"] = json.dumps({"cmd": "uv run scripts/gh-plan.py --repo example/a claim 5 > claim.json"})
        rows[2]["payload"]["output"] = "Process running with session ID 123"
        self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])
        poll = {"type": "response_item", "payload": {"type": "function_call", "name": "write_stdin", "call_id": "poll-own", "arguments": '{"session_id":123}'}}
        rows[-2:-2] = [poll, output({}, "codex", "poll-own")]
        self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])
        fresh = history("codex", proof())[1:-2]
        fresh[0]["payload"]["call_id"] = "fresh"
        fresh[1]["payload"]["call_id"] = "fresh"
        rows[-2:-2] = fresh
        self.assertTrue(session_record.summarize(rows, "codex")["safe_verdict"])

    def test_claude_split_records_share_the_api_message_launch_boundary(self):
        rows = history("claude", claim(), {**proof(), "harness": "claude"})
        rows[1]["message"].update(id="shared-api-message")
        rows[1]["message"]["content"][0]["input"] = {"command": "uv run scripts/gh-plan.py --repo example/a claim 5 > claim.json"}
        rows[2] = output({}, "claude", "tool-0")
        rows[3]["message"].update(id="shared-api-message")
        # Native shape: use A, result A, use B, result B; both use records
        # share the same API message even though their results interleave.
        self.assertFalse(session_record.summarize(rows, "claude")["safe_verdict"])
        rows[3]["message"]["id"] = "later-api-message"
        self.assertTrue(session_record.summarize(rows, "claude")["safe_verdict"])
        # Launch B before result A: the terminal ownership boundary protects
        # the session independently of message grouping.
        rows[2:4] = [rows[3], rows[2]]
        self.assertFalse(session_record.summarize(rows, "claude")["safe_verdict"])

    def test_delayed_check_cannot_cover_ownership_or_newer_attempts_after_launch(self):
        for harness in ("codex", "claude"):
            checked = {**proof(("example/a", 1, 10)), "harness": harness}
            for command, value in (("gh pr create --title x", "https://github.com/example/a/pull/5"),
                                   ("uv run scripts/gh-pr.py create --title x", {"operation": "github.pr.create", "ok": True, "url": "https://github.com/example/a/pull/5"}),
                                   ("uv run scripts/gh-plan.py --repo example/a claim 5 > claim.json", {}),
                                   ("uv run scripts/check_claims.py --bad-argument", {})):
                rows = history(harness, claim(), checked)
                late = ({"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "late", "arguments": json.dumps({"cmd": command})}}
                        if harness == "codex" else {"type": "assistant", "sessionId": SESSION, "message": {"content": [
                            {"type": "tool_use", "name": "Bash", "id": "late", "input": {"command": command}}]}})
                if harness == "codex":
                    rows[4]["payload"]["output"] = "Process running with session ID 123"
                    poll = {"type": "response_item", "payload": {"type": "function_call", "name": "write_stdin", "call_id": "poll", "arguments": '{"session_id":123}'}}
                    rows[-2:-2] = [late, output(value, harness, "late"), poll, output(checked, harness, "poll")]
                else:
                    # Parallel Bash calls in one assistant record; the check
                    # returns after the ownership tool's result.
                    rows[3]["message"]["content"].extend(late["message"]["content"])
                    rows.insert(4, output(value, harness, "late"))
                self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"])

    def test_compound_no_write_refusal_keeps_the_supported_exit_path(self):
        refused = {"operation": receipts.CLAIM_OPERATION, "ok": False, "write_outcome": "not_started", "completed_steps": []}
        for harness in ("codex", "claude"):
            for command in ("cd /repo && uv run scripts/gh-plan.py claim 42",
                            "uv run scripts/gh-plan.py claim 42 2>&1",
                            "cd /repo && uv run scripts/gh-plan.py claim 42 2>&1 | tee claim.json"):
                rows = history(harness, refused)
                if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                self.assertEqual(receipts.claim_targets(rows, harness), [])
                self.assertTrue(session_record.summarize(rows, harness)["safe_verdict"])
            for command in ("cd /repo && uv run scripts/gh-plan.py claim 42 > claim.json; cat saved.json",
                            "uv run scripts/gh-plan.py claim 42 2> error.json; cat saved.json"):
                rows = history(harness, refused)
                if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"])

    def test_claim_and_checker_polls_require_one_tool_and_one_output(self):
        refused = {"operation": receipts.CLAIM_OPERATION, "ok": False, "write_outcome": "not_started"}
        checked = proof(("example/a", 1, 10))
        for value in (refused, checked):
            for cell in ('text(await tools.write_stdin({session_id:123})); text(await tools.exec_command({cmd:"cat saved.json"}));',
                         'text(await tools.write_stdin({session_id:123})); text(await tools.write_stdin({session_id:456}));',
                         'text(await tools.write_stdin({session_id:123})); text(load("saved-proof"));'):
                rows = history("codex", value) if value is refused else history("codex", claim(), value)
                rows[-3]["payload"]["output"] = "Process running with session ID 123"
                poll = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "functions.exec", "call_id": "poll", "input": cell}}
                rows[-2:-2] = [poll, output(value, "codex", "poll")]
                self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])
            rows = history("codex", value) if value is refused else history("codex", claim(), value)
            rows[-4]["payload"].update(name="functions.exec", arguments='text(await tools.exec_command({cmd:"uv run scripts/' + ('gh-plan.py claim 1' if value is refused else 'check_claims.py') + '"})); text(load("saved-proof"));')
            self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])

    def test_claude_parallel_result_blocks_keep_each_tools_output_separate(self):
        refused = {"operation": receipts.CLAIM_OPERATION, "ok": False, "write_outcome": "not_started"}
        checked = {**proof(("example/a", 1, 10)), "harness": "claude"}
        for value in (refused, checked):
            rows = history("claude", value) if value is refused else history("claude", claim(), value)
            rows[-3]["message"]["content"].append({"type": "tool_use", "id": "old", "name": "Bash", "input": {"command": "cat saved.json"}})
            result = rows[-2]["message"]["content"]
            result[0]["content"] = ""
            result.append(output(value, "claude", "old")["message"]["content"][0])
            self.assertFalse(session_record.summarize(rows, "claude")["safe_verdict"])

    def test_bound_no_write_refusals_do_not_create_ownership_or_stale_a_proof(self):
        for harness in ("codex", "claude"):
            refused = {"operation": receipts.CLAIM_OPERATION, "ok": False, "write_outcome": "not_started", "completed_steps": []}
            for cause in ("claim_conflict", "invalid_branch", "missing_next_action", "argument_error"):
                value = {**refused, "error_code": cause}
                rows = history(harness, value)
                self.assertEqual(list(receipts.ownership_activity(rows, harness)), [])
                self.assertEqual(receipts.claim_targets(rows, harness), [])
                self.assertTrue(session_record.summarize(rows, harness)["safe_verdict"])
                checked = {**proof(("example/a", 1, 10)), "harness": harness}
                self.assertTrue(session_record.summarize(history(harness, claim(), checked, value), harness)["safe_verdict"])
            for change in ({"write_outcome": "unknown"}, {"write_outcome": None},
                           {"completed_steps": ["post_claim"]}, {"claim_comment": {"url": "https://github.com/example/a/issues/1#issuecomment-10"}}):
                self.assertFalse(session_record.summarize(history(harness, {**refused, **change}), harness)["safe_verdict"])
            rows = history(harness, refused)
            if harness == "codex": rows[2]["payload"]["call_id"] = "unrelated"
            else: rows[2]["message"]["content"][0]["tool_use_id"] = "unrelated"
            self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"])

    def test_saved_proof_in_a_check_batch_cannot_clear_redirected_later_ownership(self):
        for harness in ("codex", "claude"):
            saved = {**proof(("example/a", 1, 10)), "harness": harness}
            for command in ("uv run scripts/check_claims.py; cat saved.json",
                            "uv run scripts/check_claims.py || cat saved.json",
                            "uv run scripts/check_claims.py; tee visible.json < saved.json"):
                rows = history(harness, claim(), saved, claim(number=2), saved)
                rows[6] = output({}, harness, "tool-2")  # Redirected ownership has no source key.
                if harness == "codex": rows[7]["payload"]["arguments"] = json.dumps({"cmd": command})
                else: rows[7]["message"]["content"][0]["input"] = {"command": command}
                self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"])
            rows = history(harness, claim(), saved)
            rows[4] = output([{**saved, "claims_clear": False}, saved], harness, "tool-1")
            self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"])
            rows[4] = output(saved, harness, "tool-1")
            if harness == "codex": rows[3]["payload"]["arguments"] = json.dumps({"cmd": "uv run scripts/check_claims.py | tee proof.json"})
            else: rows[3]["message"]["content"][0]["input"] = {"command": "uv run scripts/check_claims.py | tee proof.json"}
            self.assertTrue(session_record.summarize(rows, harness)["safe_verdict"])

    def test_composed_check_and_poll_batches_cannot_adopt_saved_output(self):
        for cell in ('text(await tools.exec_command({cmd:"uv run scripts/check_claims.py"})); text(await tools.exec_command({cmd:"cat saved.json"}));',
                     'text(await tools.exec_command({cmd:"uv run scripts/check_claims.py"})); text(await tools.exec_command({cmd:other}));'):
            rows = history("codex", claim(), proof(("example/a", 1, 10)))
            rows[3]["payload"].update(name="functions.exec", arguments=cell)
            self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])
        rows = history("codex", claim(), proof(("example/a", 1, 10)))
        rows[4]["payload"]["output"] = "Process running with session ID 123"
        poll = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "functions.exec", "call_id": "poll", "input":
                'text(await tools.write_stdin({session_id:123})); text(await tools.exec_command({cmd:"cat saved.json"}));'}}
        rows[-2:-2] = [poll, output(proof(("example/a", 1, 10)), "codex", "poll")]
        self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])

    def test_a_no_write_receipt_in_a_batch_cannot_exempt_an_opaque_claim(self):
        for harness in ("codex", "claude"):
            refused = {"operation": receipts.CLAIM_OPERATION, "ok": False, "write_outcome": "not_started"}
            rows = history(harness, refused)
            command = "uv run scripts/gh-plan.py claim 1 > claim.json; cat refused.json"
            if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            else: rows[1]["message"]["content"][0]["input"] = {"command": command}
            self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"])

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
