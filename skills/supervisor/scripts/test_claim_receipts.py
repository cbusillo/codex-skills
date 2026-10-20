# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Native receipt fixtures exercise shutdown acceptance on both harnesses."""
import json
import argparse
import shlex
import sys
import subprocess
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

import claim_receipts as receipts
import session_record

SESSION = "native-fixture"

# Recorded option signatures from `uv run --help`; tests never probe host tools.
UV_RUN_HELP = """Usage: uv run [OPTIONS] [COMMAND]
Options:
      --extra <EXTRA>
      --group <GROUP>
      --no-group <NO_GROUP>
      --only-group <ONLY_GROUP>
      --env-file <ENV_FILE>
  -w, --with <WITH>
      --with-editable <WITH_EDITABLE>
      --with-requirements <WITH_REQUIREMENTS>
  -s, --script
      --package <PACKAGE>
      --index <INDEX>
      --default-index <DEFAULT_INDEX>
  -i, --index-url <INDEX_URL>
  -P, --upgrade-package <UPGRADE_PACKAGE>
      --exclude-newer <EXCLUDE_NEWER>
      --cache-dir <CACHE_DIR>
      --refresh-package <REFRESH_PACKAGE>
  -p, --python <PYTHON>
Global options:
  -q, --quiet...
      --color <COLOR_CHOICE>
      --directory <DIRECTORY>
      --project <PROJECT>
      --config-file <CONFIG_FILE>
      --no-config
  -h, --help
"""


def shortest_parser_option(parser, destination, tail):
    """Choose an abbreviation the maintained parser actually accepts today."""
    option = next(option for action in parser._actions if action.dest == destination
                  for option in action.option_strings if option.startswith("--"))
    for length in range(3, len(option) + 1):
        prefix = option[:length]
        try:
            _, unknown = parser.parse_known_args([prefix, *tail])
        except Exception:
            continue
        if not unknown:
            return prefix
    raise AssertionError("maintained parser cannot accept its own option")


def parser_fixture_value(action):
    """Use the parser's declared choices, arity and converters as fixture data."""
    if action.nargs == 0:
        return []
    value = str(next(iter(action.choices))) if action.choices else "7"
    if action.type is not None:
        action.type(value)
    count = action.nargs if isinstance(action.nargs, int) else 1
    return [value] * count


def parser_required_arguments(parser):
    words = []
    for action in parser._actions:
        if action.required:
            if action.option_strings:
                words.append(next((word for word in action.option_strings if word.startswith("--")), action.option_strings[0]))
            words.extend(parser_fixture_value(action))
    return words


def claim(repo="example/a", number=1, identifier=10):
    return {"operation": receipts.CLAIM_OPERATION, "ok": True, "claim": {"session": SESSION},
            "claim_comment": {"comment": {"url": f"https://github.com/{repo}/issues/{number}#issuecomment-{identifier}"}}}


def proof(*sources, clear=True):
    return {"operation": receipts.CHECK_OPERATION, "ok": clear, "claims_clear": clear,
            "session_id": SESSION, "harness": "codex", "coverage": {"complete": True},
            "released_claims": [{"repository": repo, "record": number, "claim_comment": identifier}
                                for repo, number, identifier in sources]}


def output(value, harness, call_id="tool"):
    # Helper stdout stays nested even when its data happens to name a status.
    encoded = json.dumps({"output": json.dumps(value)})
    if harness == "codex":
        return {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": call_id,
                "output": [{"type": "input_text", "text": encoded}]}}
    return {"type": "user", "sessionId": SESSION, "message": {"content": [
        {"type": "tool_result", "tool_use_id": call_id, "content": encoded}]}}


def native_result(identifier, status=0):
    # Representative transport, not evidence of a direct patch producer.
    return {'type': 'response_item', 'payload': {'type': 'function_call_output',
            'call_id': identifier, 'output': f'Exit code: {status if status is not None else "unknown"}\nWall time: 0.01 seconds\nOutput:\n'}}


def shell_result(value, harness, identifier):
    """Explicit representative direct-shell transport, separate from stdout."""
    if harness == "claude":
        return output(value, harness, identifier)
    record = native_result(identifier, value.get("exit_code"))
    if type(value.get("session_id")) is int:
        record["payload"]["output"] = "Process running with session ID " + str(value["session_id"])
    else:
        record["payload"]["output"] += value.get("output", "")
    return record


def composed_result(value, identifier):
    """Declared tools result printed by a composed cell, not direct stdout."""
    return {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": identifier,
            "output": [{"type": "input_text", "text": json.dumps(value)}]}}


def cell_header(cell=None):
    """One declared representative outer-cell header, independent of stdout."""
    status = 'Script running with cell ID ' + cell if cell is not None else 'Script completed'
    return {'type': 'input_text', 'text': status + '\nWall time 0.01 seconds'}


def persisted_serial_result(identifier):
    """Derive private fields/part IDs from the normalizer, then lose provenance."""
    code = 'text(await tools.exec_command({cmd:"printf done"}));' * 2
    call = {"payload": {"type": "custom_tool_call", "name": "functions.exec", "call_id": identifier, "input": code}}
    result = composed_result({"exit_code": 0}, identifier)
    result["payload"]["output"] *= 2
    records, _ = receipts.serial_ownership_records([call, result])
    native = next(record for record in records if receipts.calls_and_results(record, "codex")[1])
    # Persisted JSON can copy every key, but not an internal identity fact.
    return json.loads(json.dumps(native, default=lambda value: True))


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
                '# then `gh pr create --fill` after CI is green\ngit status',
                '# then $(uv run gh-plan.py claim 1) later\nls',
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
    if harness == "codex":
        printed_source = 'text(await tools.exec_command({cmd:"uv run gh-plan.py claim 1"}));'
        native = history(harness, {})[1:3]
        native[0]["payload"].update(name="functions.exec", call_id="read-cell", arguments=
            'const read=(e,c)=>text(c);read(eval,' + json.dumps(printed_source) + ');')
        native[1] = composed_result(printed_source, "read-cell")
        native[1]["payload"]["output"].insert(0, cell_header())
        rows[-end:-end] = native
    return rows


class UvProbeTests(unittest.TestCase):
    def test_runtime_probe_uses_only_the_trusted_executable_with_a_timeout(self):
        executable = "/trusted/runtime/uv"
        with patch.object(receipts.shutil, "which", return_value=executable), patch.object(
                receipts.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, UV_RUN_HELP)) as run:
            self.assertEqual(receipts.runtime_uv_help(), UV_RUN_HELP)
            self.assertEqual(run.call_args.args[0][0], executable)
            self.assertGreater(run.call_args.kwargs["timeout"], 0)
            self.assertLess(run.call_args.kwargs["timeout"], 60)
        with patch.object(receipts.shutil, "which", return_value=None), patch.object(receipts.subprocess, "run") as run:
            self.assertIsNone(receipts.runtime_uv_help())
            run.assert_not_called()
        with patch.object(receipts.shutil, "which", return_value=executable), patch.object(
                receipts.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 5)):
            self.assertIsNone(receipts.runtime_uv_help())


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        receipts.uv_run_options.cache_clear()
        mocked_help = patch.object(receipts, "runtime_uv_help", return_value=UV_RUN_HELP)
        mocked_help.start()
        self.addCleanup(mocked_help.stop)
        self.addCleanup(receipts.uv_run_options.cache_clear)

    def test_uv_options_follow_runtime_help_without_running_transcript_paths(self):
        helper = "/catalog/gh-plan.py --repo example/a claim 1"
        added_help = UV_RUN_HELP + "      --fixture-option <VALUE>\n"
        with patch.object(receipts, "runtime_uv_help", return_value=added_help) as probe:
            for command in ("uv run --fixture-option value " + helper,
                            "/transcript/uv --color=never run -qq -p3.12 " + helper):
                rows = history("codex", claim())
                rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                rows[2] = output({"exit_code": 0, "output": json.dumps(claim())}, "codex", "tool-0")
                self.assertFalse(receipts.ownership_state(rows, "codex")["pending"])
                self.assertEqual(receipts.target_records(rows, "codex"), {("example/a", 1)})
            self.assertEqual(probe.call_count, 1)
        receipts.uv_run_options.cache_clear()
        with patch.object(receipts, "runtime_uv_help", return_value=None):
            for prefix in ("uv run --fixture-option value ", "uv --quiet run "):
                rows[1]["payload"]["arguments"] = json.dumps({"cmd": prefix + helper})
                self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])
            rows[1]["payload"]["arguments"] = json.dumps({"cmd": "uv run " + helper})
            self.assertFalse(receipts.ownership_state(rows, "codex")["pending"])
            for harness in ("codex", "claude"):
                command = "uv run --with fixture python3 - <<'PY'\nimport os\nos.system(" + repr("uv run " + helper) + ")\nPY"
                native = history(harness, {})
                if harness == "codex":
                    native[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    native[1]["message"]["content"][0]["input"] = {"command": command}
                self.assertTrue(receipts.ownership_state(native, harness)["pending"])

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
                            payload.update(name=tool, arguments=json.dumps({"command": command} if tool == "shell_command" else {"command": ["/bin/bash", "-lc", command]}))
                    without_check = [*rows[:3], *rows[-2:]]
                    self.assertFalse(session_record.summarize(without_check, "codex")["safe_verdict"])
                    self.assertTrue(receipts.ownership_state(rows, "codex")["indices"])
                    self.assertTrue(session_record.summarize(rows, "codex")["safe_verdict"])

    def test_visible_foreground_wrappers_finish_and_preserve_claim_disposition(self):
        helper = "/catalog/gh-plan.py --repo example/a claim 1"
        commands = ("timeout 300 uv run " + helper, "GH_PROMPT_DISABLED=1 uv run " + helper,
                    "env X=1 uv run " + helper, "uv run --directory /repo " + helper,
                    "uv run --script " + helper, "uv run python3 " + helper,
                    "uv run --env-file .env " + helper, "uv run --package pkg " + helper,
                    "uv run -p 3.12 " + helper, "uv run --with-requirements deps.txt " + helper,
                    "uv run --extra deps " + helper)
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
        for cell in ('const query="claim"; text(await tools.exec_command({cmd:`uv run gh-plan.py --repo example/a search ${query}`}));',
                     'for(const n of [1,2]) text(await tools.exec_command({cmd:`uv run gh-plan.py show ${n} --sections claim`}));',
                     'const r="example/a";text(await tools.exec_command({cmd:`uv run gh-plan.py --repo ${r} search claim`}));'):
            with self.subTest(cell=cell):
                rows = history("codex", {})
                rows[1]["payload"].update(name="functions.exec", arguments=cell)
                self.assertFalse(receipts.ownership_state(rows, "codex")["indices"])

    def test_wrapped_pr_creation_keeps_repository_and_created_pr_provenance(self):
        url = "https://github.com/example/a/pull/2"
        for harness in ("codex", "claude"):
            for command, result in (("timeout 300 gh pr create --repo example/a", url),
                                    ("/opt/homebrew/bin/gh pr create --repo example/a", url),
                                    ("/opt/homebrew/bin/gh pr new --repo example/a", url),
                                    ("timeout 300 uv run gh-pr.py --repo example/a create",
                                     json.dumps({"operation": "github.pr.create", "ok": True, "url": url}))):
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {})
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    rows[2] = output({"exit_code": 0, "output": result}, harness, "tool-0")
                    self.assertTrue(receipts.ownership_state(rows, harness)["indices"])
                    self.assertFalse(receipts.ownership_state(rows, harness)["pending"])
                    self.assertFalse(receipts.target_repositories(rows, harness))
                    self.assertEqual(receipts.created_prs(rows, harness), {url})

    def test_created_pr_urls_require_isolated_invocations_and_poll_results(self):
        own = "https://github.com/example/a/pull/2"
        unrelated = "https://github.com/example/a/pull/3"
        for harness in ("codex", "claude"):
            for creator in ("gh pr create --repo example/a", "uv run gh-pr.py --repo example/a create"):
                with self.subTest(harness=harness, creator=creator):
                    rows = history(harness, {})
                    command = creator + " && gh pr list --repo example/a"
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    response = {"operation": "github.pr.create", "ok": True, "url": unrelated} if "gh-pr.py" in creator else own + "\n" + unrelated
                    rows[2] = output({"exit_code": 0, "output": json.dumps(response)}, harness, "tool-0")
                    self.assertEqual(receipts.created_prs(rows, harness), set())
                    self.assertTrue(receipts.ownership_state(rows, harness)["indices"])
                    self.assertFalse(receipts.ownership_state(rows, harness)["pending"])
        rows = history("codex", {})
        rows[1]["payload"]["arguments"] = json.dumps({"cmd": "gh pr create --repo example/a"})
        rows[2] = output({"session_id": 123, "output": ""}, "codex", "tool-0")
        poll = {"payload": {"type": "function_call", "name": "functions.exec", "call_id": "poll", "arguments":
            'text(await tools.write_stdin({session_id:123}));text(await tools.exec_command({cmd:"gh pr list"}));'}}
        response = {"payload": {"type": "function_call_output", "call_id": "poll", "output": [
            {"type": "text", "text": json.dumps({"exit_code": 0, "output": own})},
            {"type": "text", "text": json.dumps({"exit_code": 0, "output": unrelated})}]}}
        rows[-2:-2] = [poll, response]
        # Complete serial native wrappers bind each printed result position.
        self.assertEqual(receipts.created_prs(rows, "codex"), {own})
        poll["payload"]["arguments"] = ('const pending=tools.write_stdin({session_id:123});'
            'text(await pending);text(await tools.exec_command({cmd:"gh pr list"}));')
        self.assertEqual(receipts.created_prs(rows, "codex"), set())
        # The same isolated poll can supply its own process output.
        poll["payload"]["arguments"] = json.dumps({"session_id": 123})
        poll["payload"]["name"] = "write_stdin"
        response["payload"]["output"] = json.dumps({"exit_code": 0, "output": own})
        self.assertEqual(receipts.created_prs(rows, "codex"), {own})

    def test_option_discovery_uses_the_authoritative_helper_parsers(self):
        import argparse
        for helper in ("gh-plan.py", "gh-pr.py"):
            parser = receipts.helper_parser(helper)
            repository = next(action for action in parser._actions if action.dest == "repo")
            with self.subTest(helper=helper):
                self.assertTrue(receipts.repository_option(repository.option_strings[0], helper))
                # A real added option changes abbreviation ambiguity immediately.
                collision = repository.option_strings[0] + "-fixture-option"
                while collision in parser._option_string_actions:
                    collision += "-extra"
                parser.add_argument(collision, action="store_true")
                try:
                    self.assertFalse(receipts.repository_option(repository.option_strings[0][:3], helper))
                    self.assertTrue(receipts.repository_option(repository.option_strings[0], helper))
                finally:
                    receipts.helper_parser.cache_clear()
        parser = receipts.helper_parser("gh-plan.py")
        claim_parser = next(action for action in parser._actions
                            if isinstance(action, argparse._SubParsersAction)).choices["claim"]
        toggle = "--fixture-toggle"
        while toggle in claim_parser._option_string_actions:
            toggle += "-extra"
        claim_parser.add_argument(toggle, action="store_true")
        try:
            self.assertEqual(receipts.claim_operand(["gh-plan.py", "claim", toggle, "7"]), "7")
        finally:
            receipts.helper_parser.cache_clear()

    def test_creation_targets_bind_literal_heads_and_definitive_no_write_results(self):
        url = "https://github.com/example/a/pull/2"
        for harness in ("codex", "claude"):
            for command, head in (("git push origin feat && gh pr create --repo example/a --head feat --body 'Refs #1'", "feat"),
                                  ("gh pr new -Rexample/a -Howner:feat -t title -b 'Refs #1' | cat", "feat"),
                                  ("uv run gh-pr.py --repo example/a create --head feat", "feat"),
                                  ('gh pr create --repo example/a --head "$HEAD" | cat', None)):
                rows = history(harness, {})
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                with self.subTest(harness=harness, command=command):
                    relevant = [{key: target[key] for key in ("invocation", "repository", "head", "url")}
                                for target in receipts.created_pr_targets(rows, harness)]
                    self.assertEqual(relevant, [{
                        "invocation": "tool-0", "repository": "example/a", "head": head, "url": None}])
            for outcome in ("not_started", "unknown"):
                report = {"operation": "github.pr.create", "ok": False, "write_outcome": outcome, "completed_steps": []}
                rows = history(harness, report)
                rows[2] = output({"exit_code": 1, "output": json.dumps(report)}, harness, "tool-0")
                with self.subTest(harness=harness, outcome=outcome):
                    self.assertEqual(bool(receipts.created_pr_targets(rows, harness)), outcome != "not_started")
            rows = history(harness, {"operation": "github.pr.create", "ok": True, "url": url})
            self.assertEqual(receipts.created_pr_targets(rows, harness)[0]["url"], url)
            self.assertEqual(receipts.created_pr_targets(rows, harness)[0]["repository"], "example/a")

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
            for action in ("--repo example/a show 1 --sections claim", "--repo=example/a search create"):
                with self.subTest(harness=harness, read=action):
                    rows = history(harness, {})
                    command = 'GHP=/catalog/gh-plan.py; uv run "$GHP" ' + action
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertFalse(receipts.ownership_state(rows, harness)["indices"])

    def test_foreground_compounds_and_launcher_options_preserve_completion_and_release(self):
        helper = "/catalog/gh-plan.py --repo example/a claim 1"
        for harness in ("codex", "claude"):
            for command in ("builtin command uv run " + helper,
                            "if uv run " + helper + "; then :; fi",
                            "if false; then :; elif uv run " + helper + "; then :; fi",
                            "while uv run " + helper + "; do break; done",
                            "until uv run " + helper + "; do :; done",
                            "! uv run " + helper,
                            "{ uv run " + helper + "; }",
                            "(uv run " + helper + ")",
                            "uv --directory /repo run " + helper,
                            "stdbuf -oL uv run " + helper,
                            "env -i /usr/bin/uv run " + helper,
                            "env -S 'uv run " + helper + "'",
                            "exec -a worker uv run " + helper,
                            "stdbuf -o L uv run " + helper,
                            "bash -ec 'uv run " + helper + "'",
                            "bash -euo pipefail -c 'uv run " + helper + "; echo ok'",
                            "bash --login -c 'uv run " + helper + "'",
                            "sh -ec 'uv run " + helper + "'",
                            "bash -O extglob -c 'uv run " + helper + "'",
                            "bash +O extglob -lc 'uv run " + helper + "'",
                            "uv run --color never " + helper,
                            "uv run --index https://index.example/simple " + helper,
                            "uv run -P package " + helper,
                            "uv run --exclude-newer 2026-01-01 " + helper,
                            *("uv run " + option + (" managed " if takes_value else " ") + helper
                              for option, takes_value in receipts.HIDDEN_UV_RUN_OPTIONS.items()),
                            "bash -lc 'cd /repo && uv run " + helper + "'",
                            "bash -c 'uv run " + helper + " > claim.json'",
                            "for n in 1 2; do uv run " + helper + "; done",
                            "if true; then uv run " + helper + "; fi",
                            "/usr/bin/time -o timing.log uv run " + helper,
                            *("uv run " + option + " " + helper for option in (
                                "--cache-dir /cache", "--index-url https://index.example/simple",
                                "--default-index https://index.example/simple", "--config-file uv.toml",
                                "--no-group test", "--only-group test",
                                "--refresh-package package"))):
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, claim(), {**proof(("example/a", 1, 10)), "harness": harness})
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    rows[2] = output({"exit_code": 0, "output": json.dumps(claim())}, harness, "tool-0")
                    self.assertTrue(receipts.ownership_state(rows, harness)["indices"])
                    self.assertFalse(receipts.ownership_state(rows, harness)["pending"])
                    self.assertEqual(receipts.target_records(rows, harness), {("example/a", 1)})
                    self.assertTrue(receipts.acceptance(rows, harness, SESSION)["clear"])
                    missing = [*rows[:3], *rows[-(2 if harness == "codex" else 1):]]
                    self.assertFalse(receipts.acceptance(missing, harness, SESSION)["clear"])
            for command in ("rg /catalog/gh-plan.py claim", "for n in 1; do cat /catalog/gh-plan.py claim; done"):
                rows = history(harness, {})
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                self.assertFalse(receipts.ownership_state(rows, harness)["indices"])

    def test_nonliteral_code_commands_cannot_hide_ownership_literals(self):
        writer = "uv run /catalog/gh-plan.py --repo example/a claim 1"
        quoted = json.dumps(writer)
        for cell in (
            'const cmds=[' + quoted + '];for(const c of cmds) text(await tools.exec_command({cmd:c}));',
            'const run=c=>tools.exec_command({cmd:c});text(await run(' + quoted + '));',
            'const cmds=[' + quoted + '];await Promise.all(cmds.map(c=>tools.exec_command({cmd:c})));',
            'const {c}={c:' + quoted + '};text(await tools.exec_command({cmd:c}));',
            'const x="file; ' + writer + '";text(await tools.exec_command({cmd:`cat ${x}`}));',
            'const c=' + quoted + ';text(await tools.exec_command({cmd:`${c}`}));',
        ):
            with self.subTest(cell=cell):
                rows = history("codex", claim(), proof(("example/a", 1, 10)))
                rows[1]["payload"].update(name="functions.exec", arguments=cell)
                rows[2] = output({"exit_code": 0, "output": json.dumps(claim())}, "codex", "tool-0")
                self.assertTrue(receipts.ownership_state(rows, "codex")["indices"])
                # A computed command is discovered but cannot borrow the outer
                # result as completion proof for its hidden process calls.
                self.assertFalse(receipts.acceptance(rows, "codex", SESSION)["clear"])
        fixture = 'const fixture=' + quoted + ';'
        for cell in (
            fixture + 'const x="file";text(await tools.exec_command({cmd:`cat ${x}`}));',
            fixture + 'const cmd="cat file";text(await tools.exec_command({cmd}));',
            'const q="claim";text(await tools.exec_command({cmd:`uv run gh-plan.py search ${q}`}));',
            'for(const n of [1,2])text(await tools.exec_command({cmd:`uv run gh-plan.py show ${n} --sections claim`}));',
        ):
            with self.subTest(read=cell):
                rows = history("codex", {})
                rows[1]["payload"].update(name="functions.exec", arguments=cell)
                self.assertFalse(receipts.ownership_state(rows, "codex")["indices"])

    def test_literal_blocking_executors_wait_for_children_and_keep_release_proof(self):
        argv = ["uv", "run", "/catalog/gh-plan.py", "--repo", "example/a", "claim", "1"]
        helper = " ".join(argv)
        blocking = ("bash -c '" + helper + "'",
                    "bash -c '" + helper + "; echo done'",
                    "bash -lc 'cd /repo && " + helper + " && echo ok'",
                    "sh -c '" + helper + " | tee claim.json'",
                    "zsh -c '" + helper + "; true'",
                    "bash -lc '" + helper + ' --next-action "words; literal"' + "'",
                    "python3 -c 'import subprocess; subprocess.run(" + repr(argv).replace("'", '"') + ")'",
                    "python3 -c 'import subprocess; subprocess.run(" + repr(argv).replace("'", '"') + ", check=True, capture_output=True)'" )
        uncertain = ("bash -c '" + helper + " &'",
                     "python3 -c 'import subprocess; subprocess.Popen(" + repr(argv).replace("'", '"') + ")'",
                     "python3 -c 'import subprocess; subprocess.run(" + repr(argv).replace("'", '"') + ", shell=True)'",
                     "python3 -c 'import subprocess; subprocess.run(" + repr(argv).replace("'", '"') + ', executable="other-program")' + "'")
        for harness in ("codex", "claude"):
            for command in (*blocking, *uncertain):
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, claim(), {**proof(("example/a", 1, 10)), "harness": harness})
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    rows[2] = output({"exit_code": 0, "output": json.dumps(claim())}, harness, "tool-0")
                    self.assertTrue(receipts.ownership_state(rows, harness)["indices"])
                    if command in blocking:
                        self.assertFalse(receipts.ownership_state(rows, harness)["pending"])
                        self.assertEqual(receipts.target_records(rows, harness), {("example/a", 1)})
                        self.assertTrue(receipts.acceptance(rows, harness, SESSION)["clear"])
                        missing = [*rows[:3], *rows[-(2 if harness == "codex" else 1):]]
                        self.assertFalse(receipts.acceptance(missing, harness, SESSION)["clear"])
                        rows[2] = output({"session_id": 123, "output": ""}, harness, "tool-0")
                    self.assertTrue(receipts.ownership_state(rows, harness)["pending"])
                    self.assertFalse(receipts.acceptance(rows, harness, SESSION)["clear"])

    def test_argparse_repository_prefixes_and_variable_gh_keep_ownership_visible(self):
        for harness in ("codex", "claude"):
            for helper, action, read in (("gh-plan.py", "claim 1", ["show", "1"]),
                                         ("gh-pr.py", "create", ["view", "1"])):
                parser = receipts.helper_parser(helper)
                full = next(item.option_strings[0] for item in parser._actions if item.dest == "repo")
                prefixes = {full, shortest_parser_option(parser, "repo", ["example/a", *read])}
                for prefix in prefixes:
                    for option in (prefix + " example/a", prefix + "=example/a"):
                        with self.subTest(harness=harness, option=option, helper=helper):
                            rows = history(harness, claim(), {**proof(("example/a", 1, 10)), "harness": harness})
                            command = "uv run " + helper + " " + option + " " + action
                            if harness == "codex":
                                rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                            else:
                                rows[1]["message"]["content"][0]["input"] = {"command": command}
                            rows[2] = output({"exit_code": 0, "output": json.dumps(claim())}, harness, "tool-0")
                            self.assertTrue(receipts.ownership_state(rows, harness)["indices"])
                            self.assertFalse(receipts.ownership_state(rows, harness)["pending"])
                            self.assertTrue(receipts.acceptance(rows, harness, SESSION)["clear"])
            for command in ('GH=gh; "$GH" pr create --repo example/a',
                            'GH=gh; "$GH" --repo example/a pr create'):
                with self.subTest(harness=harness, variable=command):
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

    def test_generated_javascript_file_writes_reach_later_script_execution(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5'
        for program, owning in (('import os\nos.system(' + repr(writer) + ')\n', True),
                                ('writer=' + repr(writer) + '\nprint(writer)\n', False)):
            shell = "cat > /repo/run.py <<'PY'\n" + program + 'PY'
            patch_text = ('*** Begin Patch\n*** Add File: /repo/run.py\n' +
                          ''.join('+' + line + '\n' for line in program.splitlines()) + '*** End Patch')
            for call in ('text(await tools.exec_command({cmd:' + json.dumps(shell) + '}));',
                         'text(await tools.apply_patch(' + json.dumps(patch_text) + '));'):
                source = '(async()=>{' + call + '})()'
                for inert in ('const fixture=' + json.dumps(source) + ';',
                              'await eval("1+1"); const fixture=' + json.dumps(source) + ';',
                              'const fs=await import("node:fs/promises"); const fixture=' + json.dumps(source) + ';'):
                    with self.subTest(inert_source=inert):
                        self.assertEqual(list(receipts.native_file_effects('functions.exec', inert)), [])
                        self.assertEqual(list(receipts.shell_texts('functions.exec', inert)), [])
                for cell in ('await eval(' + json.dumps(source) + ');',
                             'const source=' + json.dumps(source) + '; await eval(source);',
                             'await Function("tools","text",' + json.dumps('return ' + source) + ')(tools,text);'):
                    for command, executes in (('python3 /repo/run.py', True),
                                              ('cat /repo/run.py', False), ('python3 /other/run.py', False)):
                        rows = history('codex', {}, {})
                        rows[1]['payload'].update(name='functions.exec', arguments=cell)
                        rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                        rows[3]['payload']['arguments'] = json.dumps({'cmd': command})
                        rows[4] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-1')
                        with self.subTest(program=program, cell=cell, command=command):
                            self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning and executes)

    def test_generated_javascript_reads_execute_recorded_sources_without_erasing_alternatives(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5'
        program = 'import os\nos.system(' + repr(writer) + ')\n'
        patch_text = ('*** Begin Patch\n*** Add File: /repo/run.py\n' +
                      ''.join('+' + line + '\n' for line in program.splitlines()) + '*** End Patch')
        overwrite = "cat > /repo/run.py <<'PY'\nprint('safe')\nPY"
        for command, owning in (('python3 /repo/run.py', True), ('cat /repo/run.py', False),
                                ('python3 /other/run.py', False)):
            read = 'text(await tools.exec_command({cmd:' + json.dumps(command) + '}));'
            source = '(async()=>{' + read + '})()'
            safe = '(async()=>{text(await tools.exec_command({cmd:' + json.dumps(overwrite) + '}));})()'
            for cell in ('await eval(' + json.dumps(source) + ');',
                         'if(false) await eval(' + json.dumps(safe) + '); ' + read,
                         'if(false) await eval(' + json.dumps(safe) + '); await eval(' + json.dumps(source) + ');'):
                rows = history('codex', {}, {})
                rows[1]['payload'].update(name='apply_patch', arguments=patch_text)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                rows[3]['payload'].update(name='functions.exec', arguments=cell)
                rows[4] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-1')
                with self.subTest(cell=cell):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)
        for generated in (True, False):
            write = "cat > /repo/run.py <<'PY'\n" + program + 'PY'
            write_call = 'text(await tools.exec_command({cmd:' + json.dumps(write) + '}));'
            read_call = 'text(await tools.exec_command({cmd:"python3 /repo/run.py"}));'
            write_source = '(async()=>{' + write_call + '})()'
            read_source = '(async()=>{' + read_call + '})()'
            cell = ('await eval(' + json.dumps(write_source) + '); ' + read_call if generated
                    else write_call + 'await eval(' + json.dumps(read_source) + ');')
            rows = history('codex', {})
            rows[1]['payload'].update(name='functions.exec', arguments=cell)
            rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
            with self.subTest(mixed_source=cell):
                self.assertTrue(receipts.ownership_state(rows, 'codex')['pending'])

    def test_generated_javascript_partial_patch_keeps_recorded_python_context(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5'
        base = "import os\ndef run():\n    print('safe')\nrun()\n"
        add = '*** Begin Patch\n*** Add File: /repo/run.py\n' + ''.join('+' + line + '\n' for line in base.splitlines()) + '*** End Patch'
        for action, owning in (('os.system(' + repr(writer) + ')', True), ("print('safe')", False)):
            update = "*** Begin Patch\n*** Update File: /repo/run.py\n@@\n-    print('safe')\n+    " + action + '\n*** End Patch'
            call = 'text(await tools.apply_patch(' + json.dumps(update) + '));'
            cell = 'await eval(' + json.dumps('(async()=>{' + call + '})()') + ');'
            rows = history('codex', {}, {}, {})
            rows[1]['payload'].update(name='apply_patch', arguments=add)
            rows[3]['payload'].update(name='functions.exec', arguments=cell)
            rows[5]['payload']['arguments'] = json.dumps({'cmd': 'python3 /repo/run.py'})
            for index in (0, 1, 2):
                rows[2 + 2 * index] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-' + str(index))
            with self.subTest(action=action):
                self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)

        # Independent operands must not suppress or lend bodies to one another.
        header = '*** Begin Patch\n*** Add File: /repo/run.py\n'
        complete = header + "+print('setup')\n*** End Patch"
        for body, owning in ((['import os', 'os.system(' + repr(writer) + ')'], True),
                             (['writer=' + repr(writer), 'print(writer)'], False)):
            setup = 'const path="/repo/run.py";const lines=' + json.dumps(body) + ';'
            split = '"*** Begin Patch\\n*** Add File: "+path+"\\n"+lines.map(l=>"+"+l).join("\\n")+"\\n*** End Patch"'
            variants = (
                setup + 'await tools.apply_patch("*** Begin Patch\\n*** Add File: "+path+"\\n+print(1)\\n*** End Patch");await tools.apply_patch(' + split + ');',
                setup + 'const full=' + json.dumps(complete) + ';const cond=false;await tools.apply_patch(cond?full:' + split + ');',
                'await tools.apply_patch(' + json.dumps(complete) + ');globalThis.H=' + json.dumps(header)
                + ';globalThis.B=' + json.dumps('\n'.join('+' + line for line in body) + '\n*** End Patch')
                + ';await Function("return tools.apply_patch(H+B)")();',
            )
            for source in variants:
                for cell in (source, 'await eval(' + json.dumps('(async()=>{' + source + '})()') + ');',
                             'await Function(' + json.dumps('return (async()=>{' + source + '})()') + ')();'):
                    for command, executes in (('python3 /repo/run.py', True), ('cat /repo/run.py', False)):
                        rows = history('codex', {}, {})
                        rows[1]['payload'].update(name='functions.exec', arguments=cell)
                        rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                        rows[3]['payload']['arguments'] = json.dumps({'cmd': command})
                        rows[4] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-1')
                        with self.subTest(independent_patch=cell, command=command):
                            self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning and executes)
        source = ('const end="\\n*** End Patch";await tools.apply_patch(' + json.dumps(header)
                  + '+["print(1)"].map(l=>"+"+l).join("\\n")+end);await tools.apply_patch('
                  + json.dumps(header.replace('run.py', 'tool.py')) + '+'
                  + json.dumps(['import os', 'os.system(' + repr(writer) + ')'])
                  + '.map(l=>"+"+l).join("\\n")+end);')
        for cell in (source, 'await eval(' + json.dumps('(async()=>{' + source + '})()') + ');',
                     'await Function(' + json.dumps('return (async()=>{' + source + '})()') + ')();'):
            for path, owning in (('run.py', False), ('tool.py', True)):
                rows = history('codex', {}, {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                rows[3]['payload']['arguments'] = json.dumps({'cmd': 'python3 /repo/' + path})
                rows[4] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-1')
                with self.subTest(separate_patch_bodies=cell, path=path):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)
        # Keywords are call operands when they name object properties.
        for action, owning in (('os.system(' + repr(writer) + ')', True),
                               ('print(' + repr(writer) + ')', False)):
            for name in ('return', 'if', 'for', 'while', 'switch', 'catch', 'with'):
                for access in ('.' + name, '?.' + name):
                    cell = ('const h=' + json.dumps(header) + ';const b='
                            + json.dumps('+import os\n+' + action + '\n*** End Patch')
                            + ';const o={' + name + ':tools.apply_patch};await o' + access + '([h,b].join(""));')
                    rows = history('codex', {}, {})
                    rows[1]['payload'].update(name='functions.exec', arguments=cell)
                    rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                    rows[3]['payload']['arguments'] = json.dumps({'cmd': 'python3 /repo/run.py'})
                    rows[4] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-1')
                    with self.subTest(keyword_patch_member=access, action=action):
                        self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)
        # Literal properties and proven distinct allocations keep separate
        # operand origins; opaque receiver identities retain alternatives.
        for action, owning in (('os.system(' + repr(writer) + ')', True),
                               ('print(' + repr(writer) + ')', False)):
            safe = '[' + json.dumps(header) + ',"+print(1)",end].join("")'
            own = '[' + json.dumps(header.replace('run.py', 'tool.py')) + ',' + json.dumps('+import os\n+' + action) + ',end].join("")'
            setup = 'const end="\\n*** End Patch";'
            cells = (
                (setup + 'const cfg={a:' + safe + ',b:' + own + '};await tools.apply_patch(cfg.a);await tools.apply_patch(cfg.b);', [('run.py', False), ('tool.py', owning)]),
                (setup + 'const cfg={a:' + safe + ',b:' + own + '};await tools.apply_patch(cfg["a"]);await tools.apply_patch(cfg["b"]);', [('run.py', False), ('tool.py', owning)]),
                (setup + 'const cfg={a:' + safe + ',b:' + own + '};await tools.apply_patch(cfg?.a);await tools.apply_patch(cfg?.b);', [('run.py', False), ('tool.py', owning)]),
                (setup + 'const cfg={box:{a:' + safe + ',b:' + own + '}};await tools.apply_patch(cfg.box.a);await tools.apply_patch(cfg.box.b);', [('run.py', False), ('tool.py', owning)]),
                (setup + 'const cfg=[' + safe + ',' + own + '];await tools.apply_patch(cfg[0]);await tools.apply_patch(cfg[1]);', [('run.py', False), ('tool.py', owning)]),
                (setup + 'const cfg={a:' + safe + ',b:' + own + '};const alias=cfg;await tools.apply_patch(alias.a);await tools.apply_patch(alias.b);', [('run.py', False), ('tool.py', owning)]),
                (setup + 'const a={},b={};a.x=' + safe + ';b.x=' + own + ';await tools.apply_patch(a.x);await tools.apply_patch(b.x);', [('run.py', False), ('tool.py', owning)]),
                (setup + 'const a={},b={};const alias=a;alias.x=' + own + ';b.x=' + safe + ';await tools.apply_patch(a.x);', [('tool.py', owning)]),
                (setup + 'const a=getObj(),b=getObj();a.x=' + safe + ';b.x=' + own.replace('tool.py', 'run.py') + ';await tools.apply_patch(a.x);', [('run.py', owning)]),
                (setup + 'const a={};a.x=' + safe + ';a[unknown]=' + own.replace('tool.py', 'run.py') + ';await tools.apply_patch(a.x);', [('run.py', owning)]),
                (setup + 'const a={},b={};b.x=' + safe + ';a[unknown]=' + own.replace('tool.py', 'run.py') + ';await tools.apply_patch(b.x);', [('run.py', False)]),
                (setup + 'const a={};a.x=' + safe + ';other[unknown]=' + own.replace('tool.py', 'run.py') + ';await tools.apply_patch(a.x);', [('run.py', owning)]),
                (setup + 'const cfg={a:' + safe + ',b:' + own + '};await tools.apply_patch(cfg[unknown]);', [('tool.py', owning)]),
                (setup + 'const a={};a.x=' + own + ';const b={__proto__:a};await tools.apply_patch(b.x);', [('tool.py', owning)]),
                (setup + 'const a={},b={};a.x=' + own + ';b.__proto__=a;await tools.apply_patch(b.x);', [('tool.py', owning)]),
                (setup + 'const a={},b={};a.x=' + own + ';Object.setPrototypeOf(b,a);await tools.apply_patch(b.x);', [('tool.py', owning)]),
                (setup + 'const a={},b={};a.x=' + own + ';Reflect.setPrototypeOf(b,a);await tools.apply_patch(b.x);', [('tool.py', owning)]),
                (setup + 'const a={},b={};a.x=' + own + ';Object.assign(b,a);await tools.apply_patch(b.x);', [('tool.py', owning)]),
                (setup + 'const a={},b={};const aa=a,bb=b;a.x=' + own + ';Object.assign(bb,aa);await tools.apply_patch(b.x);', [('tool.py', owning)]),
                (setup + 'const a={};a.x=' + own + ';const b={...a};await tools.apply_patch(b.x);', [('tool.py', owning)]),
                (setup + 'const a=[];a[0]=' + own + ';const b=[...a];await tools.apply_patch(b[0]);', [('tool.py', owning)]),
                (setup + 'const cfg={a:[' + safe + ',' + own.replace('tool.py', 'run.py') + ']};cfg.a.reverse();await tools.apply_patch(cfg.a[0]);', [('run.py', owning)]),
                (setup + 'const a={},b={};a.x=' + safe + ';b.x=' + own + ';const cfg={a:[1,2]};cfg.a.reverse();await tools.apply_patch(a.x);await tools.apply_patch(b.x);', [('run.py', False), ('tool.py', owning)]),
                (setup + 'const a={},b={};a.x=' + own.replace('tool.py', 'run.py') + ';b.x=' + safe + ';function unused(){Object.assign(b,a)}await tools.apply_patch(b.x);', [('run.py', False)]),
                (setup + 'const a={},b={};a.x=' + own.replace('tool.py', 'run.py') + ';b.x=' + safe + ';function copy(){Object.assign(b,a)}copy();await tools.apply_patch(b.x);', [('run.py', owning)]),
            )
            for source, reads in cells:
                for cell in (source, 'await eval(' + json.dumps('(async()=>{' + source + '})()') + ');',
                             'await Function(' + json.dumps('return (async()=>{' + source + '})()') + ')();'):
                    for path, expected in reads:
                        rows = history('codex', {}, {})
                        rows[1]['payload'].update(name='functions.exec', arguments=cell)
                        rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                        rows[3]['payload']['arguments'] = json.dumps({'cmd': 'python3 /repo/' + path})
                        rows[4] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-1')
                        with self.subTest(aggregate_patch=cell, path=path):
                            self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], expected)


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
        rows = ordinary_read_history("codex")
        cell = next(row["payload"] for row in rows
                    if row.get("payload", {}).get("call_id") == "read-cell"
                    and row["payload"].get("name") == "functions.exec")
        cell["arguments"] = cell["arguments"].replace("=>text(c)", "=>e(c)")
        self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])

    def test_mocked_process_diagnostics_preserve_native_closeout(self):
        writer = "uv run gh-plan.py --repo example/a claim 1"
        recorder = "def record(command): return 0\n"
        forms = (
            "from unittest.mock import patch\nwith patch('os.system', record):\n    os.system(WRITER)",
            "from unittest.mock import patch as replace\nwith replace('os.system', record):\n    exec(PROGRAM, {})",
            "from unittest import mock\nwith mock.patch.object(os, 'system', record):\n    exec(compile(PROGRAM, '<fixture>', 'exec'), {})",
        )
        controls = (
            "from unittest.mock import patch\nwith patch('os.system', record): pass\nos.system(WRITER)",
            "from os import system\nfrom unittest.mock import patch\nwith patch('os.system', record): system(WRITER)",
            "from os import system as original\nfrom unittest.mock import patch\ndef writes(command): original(command)\nwith patch('os.system', writes): os.system(WRITER)",
            "from unittest.mock import patch\nwith patch('os.system', record):\n    exec(PROGRAM, {})\nos.system(WRITER)",
        )
        for body, owning in (*((body, False) for body in forms), *((body, True) for body in controls)):
            program = ("import os\n" + recorder + "WRITER = " + repr(writer)
                       + "\nPROGRAM = " + repr("import os\nos.system(" + repr(writer) + ")") + "\n" + body)
            self.assertEqual(receipts.python_ownership(program), owning, body)
            for harness in ("codex", "claude"):
                rows = history(harness, {}, {})
                patch_text = ('*** Begin Patch\n*** Add File: /repo/diagnostic.py\n'
                              + '\n'.join('+' + line for line in program.splitlines()) + '\n*** End Patch')
                if harness == "codex":
                    rows[1]["payload"].update(name="apply_patch", arguments=patch_text)
                    rows[3]["payload"]["arguments"] = json.dumps({"cmd": "python /repo/diagnostic.py"})
                else:
                    rows[1]["message"]["content"][0].update(name="Write", input={"file_path": "/repo/diagnostic.py", "content": program})
                    rows[3]["message"]["content"][0]["input"] = {"command": "python /repo/diagnostic.py"}
                rows[4] = output({"exit_code": 0, "output": ""}, harness, "tool-1")
                with self.subTest(harness=harness, body=body):
                    state = receipts.ownership_state(rows, harness)
                    self.assertEqual(bool(state["indices"]), owning)
                    self.assertEqual(state["pending"], owning)

    def test_conditional_process_patches_keep_unpatched_paths(self):
        setup = "import contextlib, subprocess, sys\nfrom unittest.mock import patch\ndef fake(*a, **k): return 0\n"
        launch = "subprocess.run(['uv', 'run', '/catalog/gh-plan.py', '--repo', 'o/r', 'claim', '12'])"
        entries = (
            "if '--dry-run' in sys.argv: stack.enter_context(patch('subprocess.run', fake))",
            "for item in sys.argv[1:]: stack.enter_context(patch('subprocess.run', fake))",
            "while input():\n        stack.enter_context(patch('subprocess.run', fake))\n        break",
            "try:\n        stack.enter_context(patch('subprocess.run', fake))\n    except Exception: pass",
            "if input(): enter(stack)",
        )
        for entry in entries:
            program = setup + "def enter(stack): stack.enter_context(patch('subprocess.run', fake))\nwith contextlib.ExitStack() as stack:\n    " + entry + "\n    " + launch
            with self.subTest(entry=entry):
                self.assertTrue(receipts.python_ownership(program))
        self.assertFalse(receipts.python_ownership(setup + "with contextlib.ExitStack() as stack:\n    stack.enter_context(patch('subprocess.run', fake))\n    " + launch))

    def test_default_process_patch_forms_and_restoration(self):
        setup = ("import os\nfrom unittest.mock import patch, DEFAULT\n"
                 "WRITER = 'uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
                 "PROGRAM = \"import os\\nos.system('uv run /catalog/gh-plan.py --repo o/r claim 12')\"\n"
                 "def record(*a, **k): return None\n")
        forms = (
            ("with patch('os.system'): exec(PROGRAM, {})", False),
            ("with patch.object(os, 'system', side_effect=record): exec(PROGRAM, {})", False),
            ("with patch('os.system', return_value=0): exec(PROGRAM, {})", False),
            ("with patch('os.system', autospec=True): exec(PROGRAM, {})", False),
            ("with patch(target='os.system', new=record): exec(PROGRAM, {})", False),
            ("with patch.object(target=os, attribute='system', new=record): exec(PROGRAM, {})", False),
            ("with patch('os.system', wraps=os.system): exec(PROGRAM, {})", True),
            ("with patch('os.system', wraps=os.system, return_value=None): exec(PROGRAM, {})", False),
            ("with patch('os.system', wraps=os.system, side_effect=record): exec(PROGRAM, {})", False),
            ("def default(*a, **k): return DEFAULT\nwith patch('os.system', wraps=os.system, side_effect=default): exec(PROGRAM, {})", True),
            ("with patch('os.system', side_effect=supplied): exec(PROGRAM, {})", True),
            ("from supplied import callback\nwith patch('os.system', side_effect=callback): exec(PROGRAM, {})", True),
            ("from supplied import callback\nwith patch('os.system') as mocked:\n    mocked.side_effect=callback\n    exec(PROGRAM, {})", True),
            ("import subprocess\nfrom supplied import callback\nwith patch('subprocess.run') as mocked:\n    mocked.side_effect=callback\n    subprocess.run(args=WRITER, shell=True)", True),
            ("with patch('os.system') as mocked:\n    mocked.side_effect=record\n    exec(PROGRAM, {})", False),
            ("from supplied import callback\nwith patch('os.system') as mocked:\n    mocked._mock_wraps=callback\n    exec(PROGRAM, {})", True),
            ("from supplied import callback\nwith patch('os.system') as mocked:\n    mocked.wraps=callback\n    exec(PROGRAM, {})", False),
            ("with patch('os.system', new_callable=supplied): exec(PROGRAM, {})", True),
            ("with patch('os.system', wraps=os.system, side_effect=[DEFAULT]): exec(PROGRAM, {})", True),
            ("@patch('os.system', record)\ndef run(): exec(PROGRAM, {})\nrun()", False),
            ("@patch('os.system')\ndef run(mock): exec(PROGRAM, {})\nrun()", False),
            ("@patch('os.system', record)\ndef run(): exec(PROGRAM, {})\nrun()\nos.system(WRITER)", True),
            ("p=patch('os.system', record)\np.start()\nexec(PROGRAM, {})\np.stop()", False),
            ("p=patch('os.system', record)\np.start()\nexec(PROGRAM, {})\np.stop()\nos.system(WRITER)", True),
            ("seen=[]\nwith patch('os.system', side_effect=seen.append): exec(PROGRAM, {})\nfor command in seen: os.system(command)", True),
            ("seen=[]\nwith patch('os.system', side_effect=seen.append): exec(PROGRAM, {})", False),
            ("import subprocess\n@patch('os.system')\n@patch('subprocess.run')\ndef run(mock_run, mock_system):\n    mock_system.return_value=WRITER\n    return os.system('echo safe')\nos.system(run())", True),
            ("from os import system as original\ndef writes(command): original(command)\n@patch('os.system', record)\n@patch('os.system', writes)\ndef run(): os.system(WRITER)\nrun()", False),
        )
        for body, owning in forms:
            with self.subTest(body=body):
                self.assertEqual(receipts.python_ownership(setup + body), owning)

    def assert_python_patch_ownership(self, program, expected):
        for harness in ("codex", "claude"):
            with self.subTest(harness=harness, program=program):
                rows = history(harness, {})
                command = "python3 -c " + shlex.quote(program)
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                self.assertEqual(receipts.ownership_state(rows, harness)["pending"], expected)
                self.assertEqual(receipts.executable_file_content("run.py", program), expected)

    def test_python_patch_decorators_preserve_receivers(self):
        setup = 'import os, subprocess, asyncio\nfrom unittest.mock import patch, Mock, MagicMock, AsyncMock, NonCallableMock, NonCallableMagicMock, DEFAULT\nWRITER=\'uv run /catalog/gh-plan.py --repo o/r claim 12\'\nPROGRAM="import os\\nos.system(\'uv run /catalog/gh-plan.py --repo o/r claim 12\')"\ndef record(*a, **k): return 0\n'
        programs = (
            ("class F:\n def claim(self): os.system(WRITER)\n @patch('subprocess.run',record)\n def run(self): self.claim()\nF().run()", True),
            ("class F:\n def claim(self): os.system(WRITER)\n @patch('subprocess.run')\n def run(self,mock): self.claim()\nF().run()", True),
            ("@patch('os.system',record)\nclass F:\n def __init__(self): os.system(WRITER)\nF()", True),
            ("@patch('os.system',record)\nclass F:\n def test_claim(self): os.system(WRITER)\nF().test_claim()", False),
            ("@patch('os.system',record)\nclass F:\n @staticmethod\n def claim(): os.system(WRITER)\nF.claim()", True),
            ("class Base:\n def claim(self): os.system(WRITER)\nclass Child(Base):\n @patch('subprocess.run',record)\n def run(self): super().claim()\nChild().run()", True),
            ("class Base:\n def test_claim(self): os.system(WRITER)\n@patch('os.system',record)\nclass Child(Base): pass\nChild().test_claim()", False),
            ("@patch('os.system',record)\nclass F:\n @staticmethod\n def test_claim(): os.system(WRITER)\nF.test_claim()", False),
            ("@patch('os.system',record)\nclass F:\n @classmethod\n def test_claim(cls): os.system(WRITER)\nF.test_claim()", False),
            ("patch.TEST_PREFIX='case_'\n@patch('os.system',record)\nclass F:\n def case_claim(self): os.system(WRITER)\nF().case_claim()", False),
        )
        for body, expected in programs:
            self.assert_python_patch_ownership(setup + body, expected)
            if expected:
                self.assert_python_patch_ownership((setup + body).replace("uv run /catalog/gh-plan.py --repo o/r claim 12", "echo safe"), False)

    def test_python_explicit_mock_replacements_preserve_behavior(self):
        setup = 'import os, subprocess, asyncio\nfrom unittest.mock import patch, Mock, MagicMock, AsyncMock, NonCallableMock, NonCallableMagicMock, DEFAULT\nWRITER=\'uv run /catalog/gh-plan.py --repo o/r claim 12\'\nPROGRAM="import os\\nos.system(\'uv run /catalog/gh-plan.py --repo o/r claim 12\')"\ndef record(*a, **k): return 0\n'
        programs = (
            ("with patch('os.system',Mock(return_value=0)): exec(PROGRAM,{})", False),
            ("with patch('os.system',new_callable=Mock): exec(PROGRAM,{})", False),
            ("with patch.object(os,'system',new=Mock()): exec(PROGRAM,{})", False),
            ("with patch('os.system',MagicMock(return_value=0)): exec(PROGRAM,{})", False),
            ("with patch('os.system',new_callable=MagicMock): exec(PROGRAM,{})", False),
            ("with patch.object(os,'system',new=MagicMock()): exec(PROGRAM,{})", False),
            ("with patch('os.system',AsyncMock(return_value=0)): exec(PROGRAM,{})", False),
            ("with patch('os.system',new_callable=AsyncMock): exec(PROGRAM,{})", False),
            ("with patch.object(os,'system',new=AsyncMock()): exec(PROGRAM,{})", False),
            ("with patch('os.system',NonCallableMock(return_value=0)): exec(PROGRAM,{})", False),
            ("with patch('os.system',new_callable=NonCallableMock): exec(PROGRAM,{})", False),
            ("with patch.object(os,'system',new=NonCallableMock()): exec(PROGRAM,{})", False),
            ("with patch('os.system',NonCallableMagicMock(return_value=0)): exec(PROGRAM,{})", False),
            ("with patch('os.system',new_callable=NonCallableMagicMock): exec(PROGRAM,{})", False),
            ("with patch.object(os,'system',new=NonCallableMagicMock()): exec(PROGRAM,{})", False),
            ("@patch.multiple('os',system=DEFAULT)\ndef run(system):\n system.return_value=WRITER\n return os.system('x')\nos.system(run())", True),
            ("def factory(): return record\nwith patch('os.system',new_callable=factory): exec(PROGRAM,{})", False),
            ("with patch('os.system',Mock(wraps=os.system)): exec(PROGRAM,{})", True),
            ("with patch('os.system',Mock(side_effect=os.system)): exec(PROGRAM,{})", True),
            ("Mock(return_value=os.system)('x')(WRITER)", True),
            ("mock=Mock(wraps=os.system)\nmock(WRITER)", True),
            ("with patch('os.system',AsyncMock(wraps=os.system)): exec(PROGRAM,{})", False),
            ("async def run():\n with patch('os.system',AsyncMock(wraps=os.system)):\n  await os.system(WRITER)\nasyncio.run(run())", True),
            ("with patch.multiple('os',system=record): exec(PROGRAM,{})", False),
            ("with patch.multiple('os',system=record): exec(PROGRAM,{})\nos.system(WRITER)", True),
            ('os.system=record\nexec(PROGRAM,{})', False),
            ('os.system=MagicMock()\nexec(PROGRAM,{})', False),
            ('from os import system\nos.system=record\nsystem(WRITER)', True),
        )
        for body, expected in programs:
            self.assert_python_patch_ownership(setup + body, expected)
            if expected:
                self.assert_python_patch_ownership((setup + body).replace("uv run /catalog/gh-plan.py --repo o/r claim 12", "echo safe"), False)

    def test_python_patch_exceptions_do_not_call_original(self):
        setup = 'import os, subprocess, asyncio\nfrom unittest.mock import patch, Mock, MagicMock, AsyncMock, NonCallableMock, NonCallableMagicMock, DEFAULT\nWRITER=\'uv run /catalog/gh-plan.py --repo o/r claim 12\'\nPROGRAM="import os\\nos.system(\'uv run /catalog/gh-plan.py --repo o/r claim 12\')"\ndef record(*a, **k): return 0\n'
        programs = (
            ("with patch('os.system',side_effect=OSError):\n try: exec(PROGRAM,{})\n except OSError: pass", False),
            ("with patch('os.system',side_effect=RuntimeError('x')):\n try: exec(PROGRAM,{})\n except RuntimeError: pass", False),
            ("with patch('os.system',side_effect=subprocess.CalledProcessError(1,'x')):\n try: exec(PROGRAM,{})\n except subprocess.CalledProcessError: pass", False),
            ("def OSError(command): os.system(command)\nfrom os import system as original\ndef OSError(command): original(command)\nwith patch('os.system',side_effect=OSError): exec(PROGRAM,{})", True),
        )
        for body, expected in programs:
            self.assert_python_patch_ownership(setup + body, expected)
            if expected:
                self.assert_python_patch_ownership((setup + body).replace("uv run /catalog/gh-plan.py --repo o/r claim 12", "echo safe"), False)

    def test_python_patch_lifecycle_preserves_injections(self):
        setup = 'import os, subprocess, asyncio\nfrom unittest.mock import patch, Mock, MagicMock, AsyncMock, NonCallableMock, NonCallableMagicMock, DEFAULT\nWRITER=\'uv run /catalog/gh-plan.py --repo o/r claim 12\'\nPROGRAM="import os\\nos.system(\'uv run /catalog/gh-plan.py --repo o/r claim 12\')"\ndef record(*a, **k): return 0\n'
        programs = (
            ("with patch('os.system',record):\n p=patch('subprocess.run',record)\n p.start()\n patch.stopall()\n exec(PROGRAM,{})", False),
            ("p=patch('os.system',record)\np.start()\nexec(PROGRAM,{})\npatch.stopall()\nos.system(WRITER)", True),
            ("@patch('os.system')\n@patch('shutil.which')\ndef run(mock_which,mock_system):\n mock_system.return_value=WRITER\n return os.system('x')\nos.system(run())", True),
        )
        for body, expected in programs:
            self.assert_python_patch_ownership(setup + body, expected)
            if expected:
                self.assert_python_patch_ownership((setup + body).replace("uv run /catalog/gh-plan.py --repo o/r claim 12", "echo safe"), False)

    def test_python_patch_mutations_prevent_result_reuse(self):
        setup = 'import os, subprocess, asyncio\nfrom unittest.mock import patch, Mock, MagicMock, AsyncMock, NonCallableMock, NonCallableMagicMock, DEFAULT\nWRITER=\'uv run /catalog/gh-plan.py --repo o/r claim 12\'\nPROGRAM="import os\\nos.system(\'uv run /catalog/gh-plan.py --repo o/r claim 12\')"\ndef record(*a, **k): return 0\n'
        programs = (
            ("def quiet(): patch('os.system',record).start()\nif cond: quiet()\nelse: quiet()\nexec(PROGRAM,{})", False),
        )
        for body, expected in programs:
            self.assert_python_patch_ownership(setup + body, expected)
            if expected:
                self.assert_python_patch_ownership((setup + body).replace("uv run /catalog/gh-plan.py --repo o/r claim 12", "echo safe"), False)

    def test_recorded_mocked_escape_diagnostic_is_not_ownership(self):
        # Immutable historical command input; analyze it without executing it.
        program = 'import sys,types,json\nfrom unittest.mock import patch\nfrom pathlib import Path\nsys.path.insert(0,str(Path.cwd()/\'skills/supervisor/scripts\'))\nimport claim_receipts\nclaim=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nreport=[]\nfor wrapped in (\'(q,)\',\'{"pack": (q,)}\'):\n module=types.ModuleType(\'fill_fixture\')\n def fill(argument):\n  (argument[\'pack\'][0] if isinstance(argument,dict) else argument[0]).append(\'item\')\n module.fill=fill\n program=\'from fill_fixture import fill\\nimport os\\nq=[]\\nfill(\'+wrapped+\')\\nfor item in q: os.system(\'+repr(claim)+\')\'\n called=[]\n with patch.dict(sys.modules,{\'fill_fixture\':module}),patch(\'os.system\',side_effect=called.append):exec(program,{})\n report.append({\'wrapper\':wrapped,\'actual_commands\':called,\'classified\':claim_receipts.python_ownership(program)})\np=Path(\'/Volumes/Developer-Artifacts/task-evidence/codex-skills/claimrel-native-regression\')\n(p/\'r7-wrapped-escape-before.json\').write_text(json.dumps(report,indent=2)+\'\\n\')\nprint(json.dumps(report))\n'
        self.assert_python_patch_ownership(program, False)
        # Preserve the recorded uv/heredoc routing as well as the source bytes.
        command = "uv run --python 3.12 python - <<'PY'\n" + program + "PY"
        for harness in ("codex", "claude"):
            rows = history(harness, {})
            if harness == "codex":
                rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            else:
                rows[1]["message"]["content"][0]["input"] = {"command": command}
            with self.subTest(harness=harness):
                state = receipts.ownership_state(rows, harness)
                self.assertFalse(state["indices"])
                self.assertFalse(state["pending"])


    def test_python_async_completion_preserves_return_values(self):
        setup = "import asyncio,os,subprocess\nfrom unittest.mock import AsyncMock\nWRITER='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
        programs = (
            ("async def main():\n seen=[]\n r=AsyncMock(side_effect=lambda: seen.append(WRITER))\n pending=r()\n jobs=[pending,r()]\n await asyncio.gather(*jobs)\n return seen[1] if len(seen)>1 else 'echo safe'\nos.system(asyncio.run(main()))", True),
            ("async def main():\n seen=[]\n r=AsyncMock(side_effect=lambda: seen.append(WRITER))\n pending=r()\n jobs=[pending,pending]\n await asyncio.gather(*jobs)\n return seen[1] if len(seen)>1 else 'echo safe'\nos.system(asyncio.run(main()))", False),
            ('async def main(): return WRITER\nos.system(asyncio.run(main()))', True),
            ('async def main():\n r=AsyncMock(return_value=WRITER)\n return await r()\nos.system(asyncio.run(main()))', True),
            ('async def main():\n r=AsyncMock(return_value=WRITER)\n values=await asyncio.gather(r())\n return values[0]\nos.system(asyncio.run(main()))', True),
            ("async def main():\n first=AsyncMock(return_value='echo safe')\n second=AsyncMock(return_value=WRITER)\n values=await asyncio.gather(first(),second())\n return values[1]\nos.system(asyncio.run(main()))", True),
            ("async def main():\n first=AsyncMock(return_value='echo safe')\n second=AsyncMock(return_value=WRITER)\n values=await asyncio.gather(first(),second())\n return values[0]\nos.system(asyncio.run(main()))", False),
            ('async def main():\n r=AsyncMock(return_value=WRITER)\n return await asyncio.create_task(r())\nos.system(asyncio.run(main()))', True),
            ('async def main():\n r=AsyncMock(return_value=WRITER)\n return await asyncio.ensure_future(r())\nos.system(asyncio.run(main()))', True),
            ('async def main():\n r=AsyncMock(return_value=WRITER)\n return await asyncio.wait_for(r(),timeout=1)\nos.system(asyncio.run(main()))', True),
            ('loop=asyncio.new_event_loop()\ntry:\n r=AsyncMock(return_value=WRITER)\n os.system(loop.run_until_complete(r()))\nfinally: loop.close()', True),
        )
        for body, expected in programs:
            self.assert_python_patch_ownership(setup + body, expected)
            if expected:
                self.assert_python_patch_ownership((setup + body).replace("uv run /catalog/gh-plan.py --repo o/r claim 12", "echo safe"), False)

    def test_python_deferred_callbacks_reach_async_consumers(self):
        setup = "import asyncio,os,subprocess\nfrom unittest.mock import AsyncMock\nWRITER='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
        programs = (
            ('async def main():\n r=AsyncMock(side_effect=lambda c: subprocess.run(c,shell=True))\n await asyncio.gather(r(WRITER))\nasyncio.run(main())', True),
            ('async def main():\n r=AsyncMock(side_effect=lambda c: subprocess.run(c,shell=True))\n await asyncio.create_task(r(WRITER))\nasyncio.run(main())', True),
            ('async def main():\n r=AsyncMock(side_effect=lambda c: subprocess.run(c,shell=True))\n await asyncio.ensure_future(r(WRITER))\nasyncio.run(main())', True),
            ('async def main():\n r=AsyncMock(side_effect=lambda c: subprocess.run(c,shell=True))\n await asyncio.wait_for(r(WRITER),timeout=1)\nasyncio.run(main())', True),
            ('async def main():\n r=AsyncMock(side_effect=lambda c: subprocess.run(c,shell=True))\n await asyncio.wait_for(fut=r(WRITER),timeout=1)\nasyncio.run(main())', True),
            ('from asyncio import run as run_async\nr=AsyncMock(side_effect=lambda c: subprocess.run(c,shell=True))\nrun_async(r(WRITER))', True),
            ('loop=asyncio.new_event_loop()\ntry:\n r=AsyncMock(side_effect=lambda c: subprocess.run(c,shell=True))\n loop.run_until_complete(r(WRITER))\nfinally: loop.close()', True),
        )
        for body, expected in programs:
            self.assert_python_patch_ownership(setup + body, expected)
            if expected:
                self.assert_python_patch_ownership((setup + body).replace("uv run /catalog/gh-plan.py --repo o/r claim 12", "echo safe"), False)

    def test_python_deferred_escape_keeps_data_boundaries(self):
        setup = "import asyncio,os,subprocess\nfrom unittest.mock import AsyncMock\nWRITER='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
        programs = (
            ("from n_async_fixture import consume\nr=AsyncMock(side_effect=lambda c: os.system(c))\nconsume((r(WRITER),))", True),
            ("from n_async_fixture import consume\nr=AsyncMock(side_effect=lambda c: os.system(c))\nconsume([r(WRITER)])", True),
            ('r=AsyncMock(side_effect=lambda c: os.system(c))\nr(WRITER)', False),
            ('r=AsyncMock(side_effect=lambda c: os.system(c))\nprint(r(WRITER))', False),
            ('def ignore(x): return None\nr=AsyncMock(side_effect=lambda c: os.system(c))\nignore(r(WRITER))', False),
            ('from n_async_fixture import consume\nr=AsyncMock(side_effect=lambda c: os.system(c))\nconsume(r(WRITER))', True),
            ("from n_async_fixture import consume\nr=AsyncMock(side_effect=lambda c: os.system(c))\nconsume({'job':r(WRITER)})", True),
            ('from n_async_fixture import consume\nr=AsyncMock(return_value=WRITER)\nos.system(consume(r()))', True),
            ('from n_async_fixture import ignore\nignore(WRITER)', False),
        )
        for body, expected in programs:
            self.assert_python_patch_ownership(setup + body, expected)
            if expected:
                self.assert_python_patch_ownership((setup + body).replace("uv run /catalog/gh-plan.py --repo o/r claim 12", "echo safe"), False)

    def test_python_fixture_tables_keep_sources_under_patch(self):
        writer = "uv run /catalog/gh-plan.py --repo o/r claim 12"
        sources = ["import os\nos.system(" + repr(writer) + ")\nmarker=" + repr(index) for index in range(12)]
        for mapping in (False, True):
            table = repr(dict(enumerate(sources)) if mapping else sources)
            loop = "for key, program in PROGRAMS.items(): exec(program, {})" if mapping else "for program in PROGRAMS: exec(program, {})"
            setup = "import os\nfrom unittest.mock import patch\ndef record(command): return 0\nPROGRAMS=" + table + "\n"
            for patched in (False, True):
                program = setup + ("with patch('os.system', record):\n    " + loop if patched else loop)
                with self.subTest(mapping=mapping, patched=patched):
                    self.assertEqual(receipts.python_ownership(program), not patched)

    def test_default_mocked_fixture_results_clear_native_ownership(self):
        sources = ["import os\nos.system('uv run /catalog/gh-plan.py --repo o/r claim 12')\nmarker=" + repr(index) for index in range(12)]
        program = ("import os\nfrom unittest.mock import patch\nseen=[]\nPROGRAMS=" + repr(sources)
                   + "\nwith patch('os.system', side_effect=seen.append):\n    for program in PROGRAMS: exec(program, {})")
        for harness in ('codex', 'claude'):
            rows = history(harness, {}, {})
            if harness == 'codex':
                rows[1]['payload'].update(name='apply_patch', arguments='*** Begin Patch\n*** Add File: /repo/diagnostic.py\n' + '\n'.join('+' + line for line in program.splitlines()) + '\n*** End Patch')
                rows[3]['payload']['arguments'] = json.dumps({'cmd': 'python /repo/diagnostic.py'})
            else:
                rows[1]['message']['content'][0].update(name='Write', input={'file_path': '/repo/diagnostic.py', 'content': program})
                rows[3]['message']['content'][0]['input'] = {'command': 'python /repo/diagnostic.py'}
            rows[4] = output({'exit_code': 0, 'output': ''}, harness, 'tool-1')
            with self.subTest(harness=harness):
                state = receipts.ownership_state(rows, harness)
                self.assertFalse(state['indices'])
                self.assertFalse(state['pending'])

    def test_python_source_products_preserve_linear_execution_cost(self):
        writer = "uv run /catalog/gh-plan.py --repo o/r claim 12"
        for mocked in (False, True):
            counts = []
            for size in (2, 4):
                tables = ([f'import os\na={index}\n' for index in range(size)],
                          ['os.system(' + repr(writer) + f')\nb={index}\n' for index in range(size)],
                          [f'print({index})\n' for index in range(size)])
                program = ('import os\nfrom unittest.mock import patch\ndef record(*a): return 0\nA='
                           + repr(tables[0]) + '\nB=' + repr(tables[1]) + '\nC=' + repr(tables[2]) + '\n')
                body = 'for a in A:\n    for b in B:\n        for c in C: exec(a+b+c, {})\n'
                program += ('with patch("os.system", record):\n' + '\n'.join('    ' + line for line in body.splitlines())) if mocked else body
                calls = []
                def profile(frame, event, _argument):
                    if event == 'call' and frame.f_code.co_filename == receipts.__file__:
                        calls.append(None)
                previous = sys.getprofile()
                try:
                    sys.setprofile(profile)
                    self.assertEqual(receipts.python_ownership(program), not mocked)
                finally:
                    sys.setprofile(previous)
                counts.append(len(calls))
            with self.subTest(mocked=mocked):
                self.assertGreater(counts[0], 0)
                self.assertLessEqual(counts[1], counts[0] * 3)

    def test_python_source_products_preserve_consumers_and_large_fixture_tables(self):
        writer = "uv run /catalog/gh-plan.py --repo o/r claim 12"
        for size, consumers in ((12, ("exec(a+b,{})", "exec(str(a+b),{})", "exec(f'{a+b}',{})",
                                     "exec(compile(a+b,'<fixture>','exec'),{})", "print(a+b)",
                                     "subprocess.run(['python','-c',a+b])")), (65, ("exec(a+b,{})",))):
            setup = ('import os, subprocess\nfrom unittest.mock import patch\ndef record(*a): return 0\nA='
                     + repr([f'import os\na={index}\n' for index in range(size)]) + '\nB='
                     + repr(['os.system(' + repr(writer) + f')\nb={index}\n' for index in range(size)]) + '\n')
            for consumer in consumers:
                body = 'for a in A:\n    for b in B: ' + consumer + '\n'
                for mocked in (False, True):
                    program = setup + ('with patch("os.system", record):\n' + '\n'.join('    ' + line for line in body.splitlines()) if mocked else body)
                    owning = not consumer.startswith('print') and (not mocked or consumer.startswith('subprocess'))
                    with self.subTest(size=size, consumer=consumer, mocked=mocked):
                        self.assertEqual(receipts.python_ownership(program), owning)

    def test_python_nonseparable_sources_keep_an_owning_witness(self):
        writer = "uv run /catalog/gh-plan.py --repo o/r claim 12"
        program = ('import os\ndef factory(index): return os.system\nA='
                   + repr([f'factory({index})' for index in range(12)]) + '\nB='
                   + repr(['(' + repr(writer + ' --worker w' + str(index)) + ')' for index in range(12)])
                   + '\nfor a in A:\n    for b in B: exec(a+b)')
        self.assertTrue(receipts.python_ownership(program))
        setup = ('A=' + repr([f'import os\na={index}\n' for index in range(12)]) + '\nB='
                 + repr(['os.system(' + repr(writer) + f')\nb={index}\n' for index in range(12)]))
        self.assertTrue(receipts.python_ownership(setup + "\nfor a in A:\n    for b in B: exec((a+b).replace('\\n',';'),{})"))

    def test_python_argv_source_products_bound_repeated_analysis(self):
        counts = []
        for size in (9, 18):
            program = ('import subprocess\nC=' + repr([f'print({index})' for index in range(size)])
                       + '\nA=' + repr([f'print({index})' for index in range(size)])
                       + '\nfor code in C:\n    for first in A:\n        for second in A: subprocess.run(["python","-c",code,first,second])')
            calls = []
            def profile(frame, event, _argument):
                if event == 'call' and frame.f_code.co_filename == receipts.__file__:
                    calls.append(None)
            previous = sys.getprofile()
            try:
                sys.setprofile(profile)
                self.assertFalse(receipts.python_ownership(program))
            finally:
                sys.setprofile(previous)
            counts.append(len(calls))
        self.assertGreater(counts[0], 0)
        self.assertLessEqual(counts[1], counts[0] * 3)

    def test_small_python_source_products_keep_patch_correlation(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        sources = ['import os\nos.system=print\ncmd=' + repr(writer) + '\n',
                   'import os\ncmd="echo safe"\n']
        program = 'S=' + repr(sources) + '\nfor source in S: exec(source+"os.system(cmd)\\n",{})'
        self.assertFalse(receipts.python_ownership(program))
        self.assertTrue(receipts.python_ownership(program.replace('os.system=print', 'pass')))
        sources = [source.replace('import os\n', 'import os,atexit\n')
                   + 'atexit.register(lambda: os.system(cmd))' for source in sources]
        program = 'import subprocess\nS=' + repr(sources) + '\nfor source in S: subprocess.run(["python","-c",source])'
        self.assertFalse(receipts.python_ownership(program))
        self.assertTrue(receipts.python_ownership(program.replace('os.system=print', 'pass')))

    def test_python_source_transforms_keep_the_current_patch_context(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        core = 'import os\nos.system(' + repr(writer) + ')\n'
        forms = (('\n    ' + core.replace('\n', '\n    '), 'textwrap.dedent(PROGRAM)'),
                 (' \n' + core + ' \n', 'PROGRAM.strip()'),
                 (' \n' + core, 'PROGRAM.lstrip()'),
                 (core + ' \n', 'PROGRAM.rstrip()'),
                 (' \n' + core + ' \n', 'str.strip(PROGRAM)'),
                 (core.replace(writer, 'TOKEN'), "PROGRAM.replace('TOKEN',WRITER)"),
                 ('import os\nos.system({command!r})\n', 'PROGRAM.format(command=WRITER)'),
                 ('import os\nos.system({0!r})\n', 'PROGRAM.format(*[WRITER])'),
                 ('import os\nos.system({command!r})\n', "PROGRAM.format(**{'command':WRITER})"))
        for source, expression in forms:
            setup = ('import os,textwrap\nfrom unittest.mock import patch\nWRITER=' + repr(writer)
                     + '\nPROGRAM=' + repr(source) + '\ndef record(*a): return 0\n')
            for mocked in (False, True):
                body = 'exec(' + expression + ',{})'
                program = setup + ('with patch("os.system",record): ' + body if mocked else body)
                with self.subTest(expression=expression, mocked=mocked):
                    self.assertEqual(receipts.python_ownership(program), not mocked)
        sources = [' \n' + core + 'marker=' + repr(index) + '\n' for index in range(65)]
        setup = 'from unittest.mock import patch\nS=' + repr(sources) + '\ndef record(*a): return 0\n'
        self.assertFalse(receipts.python_ownership(setup + 'with patch("os.system",record):\n    for source in S: exec(source.strip(),{})'))
        self.assertTrue(receipts.python_ownership(setup + 'for source in S: exec(source.strip(),{})'))
        starts = [f'import os\nmarker={index}\n' for index in range(12)]
        ends = [core, 'print("safe")\n']
        setup = 'A=' + repr(starts) + '\nB=' + repr(ends) + '\n'
        self.assertTrue(receipts.python_ownership(setup + "for first in A:\n    for second in B: exec((first+second).replace('if ','if False: #'),{})"))
        setup = 'PROGRAM=' + repr(core) + '\nWRITER=' + repr(writer) + '\nTAILS=' + repr([f'marker={index}\n' for index in range(12)]) + '\n'
        self.assertFalse(receipts.python_ownership(setup + "for tail in TAILS: unused=PROGRAM+tail\nexec(PROGRAM.replace(WRITER,'echo safe'),{})"))
        for action in ('claim', 'show'):
            template = 'uv run /catalog/gh-plan.py --repo {} ' + action + ' {}'
            for arguments, owning in (('*map(str,[repo,n])', action == 'claim'),
                                      ("*['o/r',12]", action == 'claim'), ('*[]', False)):
                program = 'import os\nTEMPLATE=' + repr(template) + '\nos.system(TEMPLATE.format(' + arguments + '))'
                with self.subTest(action=action, arguments=arguments):
                    self.assertEqual(receipts.python_ownership(program), owning)
            template = 'uv run /catalog/gh-plan.py --repo {repo} ' + action + ' {n}'
            for arguments, owning in (('**supplied', action == 'claim'), ('**{}', False)):
                program = 'import os\nTEMPLATE=' + repr(template) + '\nos.system(TEMPLATE.format(' + arguments + '))'
                with self.subTest(action=action, arguments=arguments):
                    self.assertEqual(receipts.python_ownership(program), owning)

    def test_python_display_consumers_follow_actual_callbacks(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        setup = 'import os\nfrom unittest.mock import patch\nWRITER=' + repr(writer) + '\n'
        programs = (
            ('single', "rows=[]\nfor cmd in [WRITER,'echo safe']:\n print(cmd)\n rows.append(cmd)\nfor cmd in rows: os.system(cmd)", True),
            ('multiple', "rows=[]\nfor cmd in [WRITER,'echo safe']:\n print(cmd,'value',sep=':',end='end',flush=True,file=None)\n rows.append(cmd)\nfor cmd in rows: os.system(cmd)", True),
            ('invalidsep', 'class Value:\n def __str__(self):\n  os.system(WRITER)\n  return "safe"\ntry: print(Value(),sep=[])\nexcept (TypeError,AttributeError): pass', False),
            ('invalidend', 'class Value:\n def __str__(self):\n  os.system(WRITER)\n  return "safe"\ntry: print(Value(),end=[])\nexcept (TypeError,AttributeError): pass', False),
            ('invalidfile', 'class Value:\n def __str__(self):\n  os.system(WRITER)\n  return "safe"\ntry: print(Value(),file=\'bad\')\nexcept (TypeError,AttributeError): pass', False),
            ('shadow', "def print(*args): os.system(WRITER)\nfor item in [1]: print('echo safe')", True),
            ('patch', "def record(*args): os.system(WRITER)\nwith patch('builtins.print',record):\n for item in [1]: print('echo safe')", True),
            ('alias', "from builtins import print as display\nfor item in [1]: display('echo safe')", False),
            ('savedalias', "from builtins import print as display\ndef record(*args): os.system(WRITER)\nwith patch('builtins.print',record):\n for item in [1]: display('echo safe')", False),
            ('customstr', "class Value:\n def __str__(self):\n  os.system(WRITER)\n  return 'safe'\nfor item in [1]: print(Value())", True),
            ('customfile', "class Destination:\n def write(self,chunk): os.system(WRITER)\nfor item in [1]: print('safe',file=Destination())", True),
            ('customflush', "class Destination:\n def write(self,chunk): return None\n def flush(self): os.system(WRITER)\nprint('safe',file=Destination(),flush=True)", True),
            ('noflush', "class Destination:\n def write(self,chunk): return None\n def flush(self): os.system(WRITER)\nprint('safe',file=Destination(),flush=False)", False),
            ('flushbool', "seen=[]\nclass Flag:\n def __bool__(self):\n  if not seen: os.system(WRITER)\n  return False\nclass Destination:\n def write(self,chunk): seen.append(chunk)\nprint('safe',file=Destination(),flush=Flag())", True),
            ('flushlen', "class Flag:\n def __len__(self):\n  os.system(WRITER)\n  return 0\nprint('safe',flush=Flag())", True),
            ('invalidflush', "class Flag:\n def __bool__(self): return 'invalid'\nclass Destination:\n def write(self,chunk): os.system(WRITER)\ntry: print('safe',file=Destination(),flush=Flag())\nexcept TypeError: pass", False),
            ('invalidfile_str', "class Value:\n def __str__(self):\n  os.system(WRITER)\n  return 'safe'\ntry: print(Value(),file='bad')\nexcept AttributeError: pass", False),
            ('closed_stdout', 'class Value:\n def __str__(self):\n  os.system(WRITER)\n  return "safe"\nimport sys,io\nsys.stdout=io.StringIO()\nsys.stdout.close()\ntry: print(Value())\nexcept ValueError: pass', True),
        )
        for name, body, expected in programs:
            with self.subTest(consumer=name):
                self.assert_python_patch_ownership(setup + body, expected)
                if expected:
                    self.assert_python_patch_ownership((setup + body).replace(writer, 'echo safe'), False)

    def test_generic_python_text_keeps_ownership_scaffolding(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        setup = 'import os,sys,shlex\nWRITER=' + repr(writer) + '\n'
        programs = (
            ('quoted_argv', 'argv=shlex.split(WRITER)\nos.system(" ".join(shlex.quote(word) for word in argv))'),
            ('quote', 'cmd=WRITER+sys.argv[1]\nos.system("bash -lc "+shlex.quote(cmd))'),
            ('ssh', 'cmd=WRITER+sys.argv[1]\nos.system("ssh host "+shlex.quote(cmd))'),
            ('redirect', 'cmd=WRITER+sys.argv[1]\nos.system(f"{cmd} 2>&1")'),
            ('exec_repr', 'cmd=WRITER+sys.argv[1]\nexec(f"import os\\nos.system({cmd!r})",{})'),
            ('str', 'cmd=WRITER+sys.argv[1]\nos.system(str(cmd))'),
            ('repr', 'cmd=WRITER+sys.argv[1]\nexec("import os\\nos.system("+repr(cmd)+")",{})'),
            ('overflow_table', 'commands=[WRITER.encode().decode()]\nfor cmd in commands: os.system("bash -lc "+shlex.quote(cmd))'),
            ('file', 'class Destination:\n def write(self,chunk): os.system(chunk)\ncmd=WRITER+sys.argv[1]\nprint(cmd,file=Destination())'),
        )
        for name, body in programs:
            with self.subTest(conversion=name):
                self.assert_python_patch_ownership(setup + body, True)
                self.assert_python_patch_ownership((setup + body).replace(writer, 'echo safe'), False)

    def test_python_data_consumers_run_class_special_protocols(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        protocols = (
            ('len', '__len__', '1'), ('bool', '__bool__', 'True'), ('bool', '__len__', '1'),
            ('int', '__int__', '1'), ('int', '__index__', '1'), ('float', '__float__', '1.0'),
            ('float', '__index__', '1'), ('complex', '__complex__', '1j'), ('complex', '__float__', '1.0'),
            ('complex', '__index__', '1'), ('hash', '__hash__', '1'), ('dir', '__dir__', '[]'),
        )
        for operation, method, result in protocols:
            for use in (operation + '(value)', 'saved=builtins.' + operation + '\nsaved(value)',
                        'functools.partial(' + operation + ',value)()'):
                program = ('import os,builtins,functools\nWRITER=' + repr(writer) + '\nclass Value:\n def '
                           + method + '(self):\n  os.system(WRITER)\n  return ' + result + '\nvalue=Value()\n' + use)
                with self.subTest(operation=operation, method=method, use=use):
                    self.assert_python_patch_ownership(program, True)
                    self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
            for owning in (True, False):
                program = ('import os\nWRITER=' + repr(writer) + '\nclass Value:\n def ' + method + '(self):\n  '
                           + ('os.system(WRITER)\n  ' if owning else '') + 'return ' + result
                           + '\nvalue=Value()\nvalue.' + method + '=lambda: '
                           + (result if owning else 'os.system(WRITER) or ' + result) + '\n' + operation + '(value)')
                with self.subTest(class_special=operation, method=method, owning=owning):
                    self.assert_python_patch_ownership(program, owning)
        program = ('import os\nclass Value:\n def __len__(self):\n  os.system(' + repr(writer)
                   + ')\n  return 1\ntry: int(Value())\nexcept TypeError: pass')
        self.assert_python_patch_ownership(program, False)

    def test_python_uncertain_data_protocols_preserve_reachable_writers(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        cases = (('key-alternatives',
          'import os,sys\n'
          "W='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
          "os.system(min([W,'echo safe'],key=lambda c:(c==W) if sys.argv[1:] else (c!=W)))"),
         ('reverse',
          'import os,sys\n'
          "W='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
          "os.system(sorted(['echo safe',W],reverse=len(sys.argv)>1)[0])"),
         ('pretty-alternatives',
          'import os,sys,pprint\n'
          "W='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
          'class V:\n'
          " def __repr__(self): return 'echo safe' if sys.argv[1:] else W\n"
          'os.system(pprint.pformat(V()))'),
         ('pretty-repeat',
          'import os,sys,pprint\n'
          'seen=PRETTY_COMMANDS\n'
          'class V:\n'
          ' def __repr__(self): return seen.pop(0)\n'
          "pprint.pformat({'0':0 if sys.argv[1:] else 1,'1':0 if sys.argv[1:] else 1,'2':0 if sys.argv[1:] "
          "else 1,'3':0 if sys.argv[1:] else 1,'4':0 if sys.argv[1:] else 1,'5':0 if sys.argv[1:] else "
          "1,'6':0 if sys.argv[1:] else 1,'7':0 if sys.argv[1:] else 1,'8':0 if sys.argv[1:] else 1,'9':0 "
          "if sys.argv[1:] else 1,'10':0 if sys.argv[1:] else 1,'11':0 if sys.argv[1:] else 1,'12':0 if "
          "sys.argv[1:] else 1,'13':0 if sys.argv[1:] else 1,'14':0 if sys.argv[1:] else 1,'15':0 if "
          "sys.argv[1:] else 1,'last':[[V()]]},width=1)\n"
          'os.system(seen.pop(0))'),
         ('any',
          'import os\n'
          'class V:\n'
          ' def __bool__(self):\n'
          "  os.system('uv run /catalog/gh-plan.py --repo o/r claim 12')\n"
          '  return True\n'
          'any([V()])'),
         ('all',
          'import os\n'
          'class V:\n'
          ' def __len__(self):\n'
          "  os.system('uv run /catalog/gh-plan.py --repo o/r claim 12')\n"
          '  return 1\n'
          'all([V()])'),
         ('sum',
          'import os\n'
          'class V:\n'
          ' def __radd__(self,other):\n'
          "  os.system('uv run /catalog/gh-plan.py --repo o/r claim 12')\n"
          '  return 0\n'
          'sum([V()])'),
         ('bytes',
          'import os\n'
          'class V:\n'
          ' def __bytes__(self):\n'
          "  os.system('uv run /catalog/gh-plan.py --repo o/r claim 12')\n"
          "  return b''\n"
          'bytes(V())'),
         ('range',
          'import os\n'
          'class V:\n'
          ' def __index__(self):\n'
          "  os.system('uv run /catalog/gh-plan.py --repo o/r claim 12')\n"
          '  return 1\n'
          'range(V())'),
         ('set',
          'import os\n'
          'class V:\n'
          ' def __hash__(self):\n'
          "  os.system('uv run /catalog/gh-plan.py --repo o/r claim 12')\n"
          '  return 1\n'
          'set([V()])'),
         ('frozenset',
          'import os\n'
          'class V:\n'
          ' def __hash__(self):\n'
          "  os.system('uv run /catalog/gh-plan.py --repo o/r claim 12')\n"
          '  return 1\n'
          'frozenset([V()])'),
         ('dict',
          'import os\n'
          'class V:\n'
          ' def __hash__(self):\n'
          "  os.system('uv run /catalog/gh-plan.py --repo o/r claim 12')\n"
          '  return 1\n'
          'dict([(V(),1)])'),
         ('json-dump',
          'import os,json,io\n'
          "W='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
          'class V: pass\n'
          'def encode(v):\n'
          ' os.system(W)\n'
          ' return None\n'
          'json.dump(V(),io.StringIO(),default=encode)'),
         ('json-loads',
          'import os,json,io\n'
          "W='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
          'def encode(v):\n'
          ' os.system(W)\n'
          ' return None\n'
          "json.loads('{}',object_hook=encode)"),
         ('json-encoder',
          'import os,json,io\n'
          "W='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
          'class V: pass\n'
          'class Encoder(json.JSONEncoder):\n'
          ' def default(self,v):\n'
          '  os.system(W)\n'
          '  return None\n'
          'json.dumps(V(),cls=Encoder)'),
         ('json-dump-keywords',
          'import os,json,io\n'
          "W='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
          'class V: pass\n'
          'def encode(v):\n'
          ' os.system(W)\n'
          ' return None\n'
          'json.dump(obj=V(),fp=io.StringIO(),default=encode)'),
         ('json-dumps-keywords',
          'import os,json,io\n'
          "W='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
          'class V: pass\n'
          'def encode(v):\n'
          ' os.system(W)\n'
          ' return None\n'
          'json.dumps(obj=V(),default=encode)'),
         ('json-loads-keywords',
          'import os,json,io\n'
          "W='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
          'def encode(v):\n'
          ' os.system(W)\n'
          ' return None\n'
          "json.loads(s='{}',object_hook=encode)"),
         ('bytes-keyword',
          'import os\n'
          'class V:\n'
          ' def __bytes__(self):\n'
          "  os.system('uv run /catalog/gh-plan.py --repo o/r claim 12')\n"
          "  return b''\n"
          'bytes(source=V())'))
        for name, program in cases:
            if name == 'pretty-repeat':
                import pprint
                observed = []
                class CountingValue:
                    def __repr__(self):
                        observed.append(None)
                        return 'safe'
                pprint.pformat({**{str(index):1 for index in range(16)}, 'last':[[CountingValue()]]}, width=1)
                program = program.replace('PRETTY_COMMANDS', repr(['echo safe'] * len(observed) + [writer]))
            with self.subTest(operation=name):
                self.assert_python_patch_ownership(program, True)
                self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)

        for expression in ('any([Value()])', 'all([Value()])', 'bytes(Value())'):
            program = ('import os\nclass Value:\n def __bool__(self): return False\n def __len__(self): os.system('
                       + repr(writer) + '); return 0\n def __bytes__(self): return b""\n def __index__(self): os.system('
                       + repr(writer) + '); return 0\n' + expression)
            with self.subTest(skipped_protocol=expression):
                self.assert_python_patch_ownership(program, False)
        for expression in ('json.dumps([1,2],default=encode)', 'json.loads("1",object_hook=encode)'):
            program = 'import os,json\ndef encode(item): os.system(' + repr(writer) + '); return None\n' + expression
            with self.subTest(skipped_json_callback=expression):
                self.assert_python_patch_ownership(program, False)
        program = ('import os\nclass Value:\n def __hash__(self): return 1\nd=dict([(Value(),'
                   + repr(writer) + ')])\nfor command in d.values(): os.system(command)')
        self.assert_python_patch_ownership(program, True)
        self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)

        for expression in ('any(iterable=[Value()])','all(iterable=[Value()])',
                           'range(stop=Value())','set(iterable=[Value()])',
                           'bytes(Value(),source=Value())'):
            program = ('import os\nclass Value:\n def __bool__(self): os.system(' + repr(writer)
                       + '); return True\n def __index__(self): os.system(' + repr(writer)
                       + '); return 1\n def __hash__(self): os.system(' + repr(writer)
                       + '); return 1\ntry: ' + expression + '\nexcept TypeError: pass')
            with self.subTest(invalid_arguments=expression):
                self.assert_python_patch_ownership(program, False)

    def test_python_unique_collections_keep_process_independent_positions(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        for constructor in ('set(["echo safe",W,"echo other"])', 'frozenset(["echo safe",W,"echo other"])',
                            '{"echo safe",W,"echo other"}', '{word for word in ["echo safe",W,"echo other"]}'):
            for index in range(3):
                for consumer in ('list(VALUE)[INDEX]', 'tuple(VALUE)[INDEX]'):
                    program = 'import os\nW=' + repr(writer) + '\nos.system(' + consumer.replace('VALUE', constructor).replace('INDEX', str(index)) + ')'
                    with self.subTest(constructor=constructor, index=index, consumer=consumer):
                        self.assert_python_patch_ownership(program, True)
                        self.assert_python_patch_ownership(program.replace(writer, 'echo third'), False)
            program = 'import os\nW=' + repr(writer) + '\nos.system(next(iter(' + constructor + ')))'
            with self.subTest(constructor=constructor, consumer='next'):
                self.assert_python_patch_ownership(program, True)
                self.assert_python_patch_ownership(program.replace(writer, 'echo third'), False)
        for index in range(2):
            program = 'import os\nW=' + repr(writer) + '\nos.system(list({("echo safe",0),(W,0)})[' + str(index) + '][0])'
            with self.subTest(salted_tuple=index):
                self.assert_python_patch_ownership(program, True)
                self.assert_python_patch_ownership(program.replace(writer, 'echo third'), False)
        for collection, suffix in (('{1,0}', ''), ('set([1,0])', ''), ('frozenset([1,0])', ''),
                                   ('{n for n in [1,0]}', ''), ('{1.0,0.0}', ''), ('{True,False}', ''),
                                   ('{(1,),(0,)}', '[0]')):
            for consumer in ('next(iter(VALUE))', 'list(VALUE)[0]', 'tuple(VALUE)[0]'):
                index = consumer.replace('VALUE', collection) + suffix
                if '1.0' in collection:
                    index = 'int(' + index + ')'
                program = 'import os\nW=' + repr(writer) + '\ncommands=[W,"echo safe"]\nos.system(commands[' + index + '])'
                with self.subTest(numeric_collection=collection, consumer=consumer):
                    self.assert_python_patch_ownership(program, True)
                    self.assert_python_patch_ownership(program.replace(writer, 'echo third'), False)
        for index, expected in (('int(sys.argv[1])', True), ('-2', True), ('-1', False), ('99', False), ('"invalid"', False)):
            program = 'import os,sys\nW=' + repr(writer) + '\ncommands=[W,"echo safe"]\nos.system(commands[' + index + '])'
            with self.subTest(uncertain_index=index):
                self.assert_python_patch_ownership(program, expected)
                self.assert_python_patch_ownership(program.replace(writer, 'echo third'), False)

    def test_python_unique_collection_mutation_keeps_members_and_uncertain_order(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        setup = 'import os\nW=' + repr(writer) + '\n'
        cases = (
            ('values={"echo safe"}\nvalues.add(W)\nos.system(list(values)[0])',True),
            ('values={"echo safe"}\nvalues.update([W])\nos.system(tuple(values)[0])',True),
            ('values={"echo safe",W}\nvalues=values.copy()\nos.system(list(values)[0])',True),
            ('values={"echo safe",W}\nvalues=set(values)\nos.system(list(values)[0])',True),
            ('values={"echo safe",W}\nvalues=frozenset(values)\nos.system(list(values)[0])',True),
            ('values={"echo safe",W}\nvalues.remove(W)\nos.system(list(values)[0])',False),
            ('values={"echo safe",W}\nvalues.discard(W)\nos.system(next(iter(values)))',False),
            ('values={"echo safe",W}\nvalues.update(set(["echo other"]))\nvalues.remove(W)\nos.system(list(values)[0])',False),
            ('values={W}\nvalues.pop()\nfor command in values: os.system(command)',False),
            ('values={"echo safe",W}\nvalues.pop()\nos.system(list(values)[0])',True),
        )
        for body,expected in cases:
            with self.subTest(mutation=body):
                self.assert_python_patch_ownership(setup+body,expected)
                self.assert_python_patch_ownership((setup+body).replace(writer,'echo third'),False)

    def test_python_unique_collection_order_does_not_enumerate_permutations(self):
        def measured(size, consumer, limit=None):
            # Executable-looking fixture strings stay concrete in choices().
            # They must remain cheap data while a unique order is unknown.
            items = ['import math\nvalue=' + str(index) for index in range(size)]
            program = 'import os\nitems=' + repr(items) + '\nos.system(' + consumer + ')'
            calls = 0
            def profile(frame, event, argument):
                nonlocal calls
                if event == 'call':
                    calls += 1
                    if limit is not None and calls > limit:
                        raise AssertionError('unique collection enumerated process-dependent orders')
            sys.setprofile(profile)
            try:
                self.assertFalse(receipts.python_ownership(program))
            finally:
                sys.setprofile(None)
            return calls
        calls = measured(640, 'list(set(items))[0]')
        measured(1280, 'list(set(items))[0]', calls * 3)
        for consumer in ('tuple(frozenset(items))[600]', 'next(iter(set(items)))', '"".join(set(items))'):
            measured(1280, consumer)
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        items = ['echo safe ' + str(index) for index in range(1280)] + [writer]
        for consumer in ('list(set(items))[600]', 'tuple(frozenset(items))[600]', 'next(iter(set(items)))'):
            program = 'import os\nitems=' + repr(items) + '\nos.system(' + consumer + ')'
            with self.subTest(large_writer=consumer):
                self.assertTrue(receipts.python_ownership(program))
                self.assertFalse(receipts.python_ownership(program.replace(writer, 'echo third')))

    def test_python_json_default_follows_returned_receivers(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        setup = 'import os,json,io\nW=' + repr(writer) + '\nclass A: pass\nclass B: pass\n'
        cases = (
            ('direct', 'def encode(v):\n if isinstance(v,B):\n  os.system(W)\n  return None\n return B()\n'),
            ('container', 'def encode(v):\n if isinstance(v,B):\n  os.system(W)\n  return None\n return {"child":[B()]}\n'),
            ('captured', 'def make(command):\n class V:\n  def run(self): os.system(command)\n  def done(self): return command==W\n return V()\ndef encode(v):\n if isinstance(v,A): return make("echo safe")\n v.run()\n return None if v.done() else make(W)\n'),
            ('defaults', 'def make(command):\n class V:\n  def run(self, command=command): os.system(command)\n  def done(self, command=command): return command==W\n return V()\ndef encode(v):\n if isinstance(v,A): return make("echo safe")\n v.run()\n return None if v.done() else make(W)\n'),
            ('repeated', 'commands=["echo safe","echo other",W]\ndef encode(v):\n os.system(commands.pop(0))\n return A()\n'),
        )
        for name, callback in cases:
            for call in ('json.dumps(A(),default=encode)', 'json.dump(A(),io.StringIO(),default=encode)',
                         'json.dumps(obj=A(),default=encode)', 'json.dump(obj=A(),fp=io.StringIO(),default=encode)'):
                program = setup + callback + 'try: ' + call + '\nexcept (RecursionError,IndexError): pass'
                with self.subTest(returned=name, call=call):
                    self.assert_python_patch_ownership(program, True)
                    self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
        for returned in ("b'payload'", '1j', '{1}', '(item for item in [1])', 'lambda:None', 'functools.partial(lambda:None)'):
            callback = 'import functools\nsteps=[lambda:(' + returned + '),lambda:os.system(W)]\ndef encode(value): return steps.pop(0)()\n'
            for call in ('json.dumps(A(),default=encode)','json.dump(A(),io.StringIO(),default=encode)'):
                program = setup+callback+call
                with self.subTest(unsupported_return=returned,call=call):
                    self.assert_python_patch_ownership(program,True)
                    self.assert_python_patch_ownership(program.replace(writer,'echo safe'),False)
            control = setup+'import functools\ndef encode(value):return ('+returned+')\ntry: json.dumps(A(),default=encode)\nexcept (RecursionError,ValueError): pass'
            with self.subTest(recursive_unsupported=returned):
                self.assert_python_patch_ownership(control,False)
        for call in ('json.dumps(A(),cls=Encoder)', 'json.dump(A(),io.StringIO(),cls=Encoder)'):
            program = setup + 'class Encoder(json.JSONEncoder):\n def default(self,v):\n  if isinstance(v,B):\n   os.system(W)\n   return None\n  return [B()]\n' + call
            with self.subTest(encoder=call):
                self.assert_python_patch_ownership(program, True)
                self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
        controls = (
            'def encode(v): return W\njson.dumps(A(),default=encode)',
            'def encode(v): return v\ntry: json.dumps(A(),default=encode)\nexcept ValueError: pass',
            'def encode(v):\n class V: pass\n return V()\ntry: json.dumps(A(),default=encode)\nexcept RecursionError: pass',
            'def encode(v): os.system(W); return None\njson.dumps([1,2],default=encode)',
            'def encode(v): os.system(W); return None\ntry: json.dumps(A(),default=encode,unsupported=True)\nexcept TypeError: pass',
            'def encode(v): os.system(W); return None\ntry: json.dump(A(),io.StringIO(),default=encode,unsupported=True)\nexcept TypeError: pass',
        )
        for control in controls:
            with self.subTest(control=control):
                self.assert_python_patch_ownership(setup + control, False)

    def test_python_json_default_does_not_revisit_serializable_subclasses(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        setup = 'import os,json,io\nW=' + repr(writer) + '\n'
        for native, argument in (('int','1'),('float','1.5'),('str','"value"'),('list','[]'),('tuple','()'),('dict','{}')):
            body = ('class A:\n def convert(self): return B(' + argument + ')\nclass B(' + native
                    + '):\n def convert(self): os.system(W); return None\n')
            for call in ('json.dumps(A(),default=lambda value:value.convert())',
                         'json.dump(A(),io.StringIO(),default=lambda value:value.convert())'):
                with self.subTest(native=native, call=call):
                    self.assert_python_patch_ownership(setup + body + call, False)
        for native, argument in (('list','[C()]'),('tuple','(C(),)'),('dict','{"child":C()}')):
            body = ('class A:\n def convert(self): return B(' + argument + ')\nclass B(' + native
                    + '):\n def convert(self): return None\nclass C:\n def convert(self): os.system(W); return None\n')
            for call in ('json.dumps(A(),default=lambda value:value.convert())',
                         'json.dump(A(),io.StringIO(),default=lambda value:value.convert())'):
                program = setup + body + call
                with self.subTest(native_child=native, call=call):
                    self.assert_python_patch_ownership(program, True)
                    self.assert_python_patch_ownership(program.replace(writer,'echo safe'), False)
        for native, method, returned in (('list','__iter__','iter([C()])'),('tuple','__iter__','iter([C()])'),
                                          ('dict','items','[("child",C())]')):
            body = ('class A:\n def convert(self): return B([0])\nclass B(' + native + '):\n def ' + method
                    + '(self): return ' + returned + '\nclass C:\n def convert(self): os.system(W); return None\n')
            # A nonempty dict reaches its items() override in CPython.
            if native == 'dict':
                body = body.replace('B([0])','B({"present":0})')
            for call in ('json.dumps(A(),default=lambda value:value.convert())',
                         'json.dump(A(),io.StringIO(),default=lambda value:value.convert())'):
                program = setup + body + call
                with self.subTest(native_protocol=native, call=call):
                    self.assert_python_patch_ownership(program, True)
                    self.assert_python_patch_ownership(program.replace(writer,'echo safe'), False)
        for native in ('list','tuple'):
            body = ('class A:\n def convert(self): return B()\nclass B(' + native
                    + '):\n def __iter__(self): os.system(W);return iter([])\n')
            for call,expected in (('json.dumps(A(),default=lambda value:value.convert())',True),
                                  ('json.dumps(A(),default=lambda value:value.convert(),indent=1)',True),
                                  ('json.dump(A(),io.StringIO(),default=lambda value:value.convert())',False)):
                with self.subTest(empty_native=native, call=call):
                    self.assert_python_patch_ownership(setup+body+call,expected)
                    self.assert_python_patch_ownership((setup+body+call).replace(writer,'echo safe'),False)
        program = (setup + 'class A:\n def convert(self): return B()\nclass B(dict):\n def items(self): os.system(W);return []\n'
                   'json.dumps(A(),default=lambda value:value.convert())')
        self.assert_python_patch_ownership(program, False)
        for native,method,returned,argument in (('list','__iter__','iter([1])','[1]'),
                                                ('tuple','__iter__','iter([1])','[1]'),
                                                ('dict','items','[("child",1)]','{"present":0}')):
            body = ('class B(' + native + '):\n def ' + method + '(self): os.system(W); return ' + returned + '\n')
            for operand in ('B(' + argument + ')', '[B(' + argument + ')]', '{"child":B(' + argument + ')}'):
                for call in ('json.dumps(' + operand + ')','json.dump(' + operand + ',io.StringIO())'):
                    with self.subTest(no_default=native, operand=operand, call=call):
                        self.assert_python_patch_ownership(setup+body+call,True)
                        self.assert_python_patch_ownership((setup+body+call).replace(writer,'echo safe'),False)
        body = 'class B(dict):\n def items(self): os.system(W);return []\n'
        self.assert_python_patch_ownership(setup+body+'json.dumps(B())',False)
        program = (setup + 'def make():\n class B(list):\n  def __iter__(self): return iter([make()])\n return B()\n'
                   'class A:\n def convert(self): return make()\ntry: json.dumps(A(),default=lambda value:value.convert())\nexcept RecursionError: pass')
        self.assert_python_patch_ownership(program, False)

    def assert_json_protocol_paths(self, body, c_writer, pure_writer, *, value='B()', default=''):
        # Fixed callback fixtures cover both supported encoder paths. An
        # indented dumps may choose either; dump uses the iterable encoder.
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        setup = 'import os,json,io\nW=' + repr(writer) + '\n'
        suffix = ',' + default if default else ''
        cases = (('json.dumps(' + value + suffix + ')', c_writer),
                 ('json.dumps(' + value + suffix + ',indent=1)', c_writer or pure_writer),
                 ('json.dump(' + value + ',io.StringIO()' + suffix + ')', pure_writer))
        for call, expected in cases:
            program = setup + body + 'try: ' + call + '\nexcept (TypeError,ValueError,OverflowError): pass'
            with self.subTest(call=call):
                self.assert_python_patch_ownership(program, expected)
                self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)

    def test_python_json_native_container_truth_controls_iteration(self):
        c_iteration = {'list': True, 'tuple': True, 'dict': False}
        for native, iteration in (('list', '__iter__'), ('tuple', '__iter__'), ('dict', 'items')):
            for method, results in (('__len__', (('1', True), ('0', False), ('-1', False), ('None', False))),
                                    ('__bool__', (('True', True), ('False', False), ('1', False), ('None', False)))):
                for returned, iterates in results:
                    body = ('class Base(' + native + '):\n def ' + method + '(self): return ' + returned
                            + '\n def ' + iteration + '(self): os.system(W);return iter([])\nclass B(Base): pass\n')
                    with self.subTest(native=native, method=method, returned=returned):
                        self.assert_json_protocol_paths(body, c_iteration[native], iterates)
            for truth, writes in (('def __len__(self): os.system(W); return 0', True),
                                  ('def __bool__(self): return False\n def __len__(self): os.system(W); return 1', False)):
                with self.subTest(native=native, truth=truth):
                    self.assert_json_protocol_paths('class B(' + native + '):\n ' + truth + '\n', False, writes)
            for returned, writes in (('True', True), ('False', False)):
                argument = '{"child":C()}' if native == 'dict' else '[C()]'
                body = ('class B(' + native + '):\n def __bool__(self): return ' + returned
                        + '\nclass C:\n def convert(self): os.system(W);return None\n')
                with self.subTest(native=native, child_truth=returned):
                    self.assert_json_protocol_paths(body, True, writes, value='B(' + argument + ')',
                                                    default='default=lambda value:value.convert()')
            body = ('class B(' + native + '):\n def __len__(self): os.system(W);return 1\n def '
                    + iteration + '(self): os.system(W);return iter([])\n')
            for indent in ('1.5', '1j', 'b"x"', '[]', '{}'):
                for call in ('json.dumps(B(),indent=' + indent + ')', 'json.dump(B(),io.StringIO(),indent=' + indent + ')'):
                    program = 'import os,json,io\nW="uv run /catalog/gh-plan.py --repo o/r claim 12"\n' + body + 'try: ' + call + '\nexcept TypeError: pass'
                    with self.subTest(native=native, invalid_indent=indent, call=call):
                        self.assert_python_patch_ownership(program, False)

    def test_python_json_container_length_uses_index_protocol(self):
        c_iteration = {'list': True, 'tuple': True, 'dict': False}
        for native, iteration in (('list', '__iter__'), ('tuple', '__iter__'), ('dict', 'items')):
            for returned, iterates in (('1', True), ('0', False), ('-1', False), ('None', False),
                                       ('True', True), ('False', False), (repr(2**100), False)):
                for writer_at in ('index', 'iteration'):
                    body = ('class Index:\n def __index__(self): '
                            + ('os.system(W);' if writer_at == 'index' else '') + 'return ' + returned
                            + '\nclass Base(' + native + '):\n def __len__(self): return Index()\n def '
                            + iteration + '(self): ' + ('os.system(W);' if writer_at == 'iteration' else '')
                            + 'return iter([])\nclass B(Base): pass\n')
                    with self.subTest(native=native, returned=returned, writer_at=writer_at):
                        self.assert_json_protocol_paths(body, writer_at == 'iteration' and c_iteration[native],
                                                        writer_at == 'index' or iterates)
            for returned, iterates in (('0', False), ('1', True)):
                for writer_at in ('index', 'iteration'):
                    body = ('class Index(int):\n def __index__(self): '
                            + ('os.system(W);' if writer_at == 'index' else '') + 'return 1'
                            + '\nclass B(' + native + '):\n def __len__(self): return Index(' + returned + ')\n def '
                            + iteration + '(self): ' + ('os.system(W);' if writer_at == 'iteration' else '')
                            + 'return iter([])\n')
                    with self.subTest(native=native, int_subclass=returned, writer_at=writer_at):
                        self.assert_json_protocol_paths(body, writer_at == 'iteration' and c_iteration[native],
                                                        writer_at == 'iteration' and iterates)
            body = ('class Index:\n def __index__(self): os.system(W); return 1\nclass B(' + native
                    + '):\n def __bool__(self): return False\n def __len__(self): return Index()\n')
            with self.subTest(native=native, bool_priority=True):
                self.assert_json_protocol_paths(body, False, False)
            for declaration in ('class Index(int):\n def __new__(cls,value): return int.__new__(cls,1)\n',
                                'class Base(int):\n def __new__(cls,value): return int.__new__(cls,1)\nclass Index(Base): pass\n'):
                body = (declaration + 'class B(' + native + '):\n def __len__(self): return Index(0)\n def '
                        + iteration + '(self): os.system(W);return iter([])\n')
                with self.subTest(native=native, new_override=declaration):
                    self.assert_json_protocol_paths(body, c_iteration[native], True)
        for argument in ('1.5', '"0"'):
            body = 'class Index(int):\n def __index__(self): os.system(W);return 1\n'
            with self.subTest(int_subclass_json=argument):
                self.assert_json_protocol_paths(body, False, False, value='Index(' + argument + ')',
                                                default='default=lambda value:os.system(W)')

    def test_python_json_load_hooks_preserve_alternative_writer_values(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        for hook, source in (('object_hook','{}'), ('object_pairs_hook','{}'), ('parse_float','1.25'),
                             ('parse_int','1'), ('parse_constant','NaN')):
            program = ('import os,json,sys\nW=' + repr(writer) + '\nos.system(json.loads('
                       + repr(source) + ',' + hook + '=lambda value: W if sys.argv[1:] else "echo safe"))')
            with self.subTest(hook=hook):
                self.assert_python_patch_ownership(program, True)
                self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
        program = ('import os,json,sys\nW=' + repr(writer) + '\nos.system(json.loads("{}",'
                   'object_hook=lambda value: W if sys.argv[1:] else "echo safe",object_pairs_hook=lambda value:"echo safe"))')
        self.assert_python_patch_ownership(program, False)
        program = ('import os,json,sys\nW=' + repr(writer) + '\ntry: os.system(json.loads("{}",'
                   'object_hook=lambda value: W,unsupported=True))\nexcept TypeError: pass')
        self.assert_python_patch_ownership(program, False)

    def test_python_process_salted_hash_keeps_both_possible_branches(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        # The source may run in another Python process with a different seed.
        for operand in ('a', b'a', ('a',1)):
            program = ('import os\nos.system({' + repr(hash(operand)) + ':"echo safe"}.get(hash('
                       + repr(operand) + '),' + repr(writer) + '))')
            with self.subTest(operand=operand):
                self.assert_python_patch_ownership(program, True)
                self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
        program = 'import os,sys\nos.system({0:"echo safe"}.get(sys.argv[1],' + repr(writer) + '))'
        self.assert_python_patch_ownership(program, True)
        self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)

    def test_python_json_primitive_source_dags_do_not_expand_for_callbacks(self):
        def measured(size, limit=None):
            program = 'import sys,json\nitems=[]\n' + ''.join('items=items+["x"] if sys.argv[1:] else items+["y"]\n' for _ in range(size)) + 'json.dumps(items)'
            calls = 0
            def profile(frame, event, argument):
                nonlocal calls
                if event == 'call':
                    calls += 1
                    if limit is not None and calls > limit:
                        raise AssertionError('JSON serializer expanded callback-free source alternatives')
            sys.setprofile(profile)
            try:
                self.assertFalse(receipts.python_ownership(program))
            finally:
                sys.setprofile(None)
            return calls
        calls = measured(8)
        measured(16, calls * 3)

    def test_python_pretty_print_preserves_actual_repr_visits(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        import contextlib
        import io
        import pprint
        for expression in ('pprint.pformat([[value]],width=1)',
                           'pprint.pformat([[value]],width=80)',
                           'pprint.pformat([[value]],width=1,depth=1)',
                           'pprint.pformat([[value]],1,1,1)',
                           'pprint.pprint([[value]],width=1)',
                           'pprint.pformat({"nested":(value,)})'):
            observed = []
            class CountingValue:
                def __repr__(self):
                    observed.append(None)
                    return 'long repr string'
            with contextlib.redirect_stdout(io.StringIO()):
                eval(expression, {'pprint':pprint, 'value':CountingValue()})
            visits = len(observed)
            for owning in (True, False):
                commands = ['echo safe'] * (visits - 1) + [writer] + ['echo safe'] * 4 if owning else ['echo safe'] * visits + [writer]
                program = ('import os,pprint\nseen=' + repr(commands)
                           + '\nclass Value:\n def __repr__(self):\n  os.system(seen.pop(0))\n  return "long repr string"\nvalue=Value()\n' + expression)
                with self.subTest(expression=expression, owning=owning):
                    self.assert_python_patch_ownership(program, owning)
        for expression, owning in (('pprint.pformat(Value(),stream="bad")', False),
                                   ('pprint.pprint(Value(),stream="bad")', True)):
            program = ('import os,pprint\nWRITER=' + repr(writer) + '\nclass Value:\n def __repr__(self):\n  os.system(WRITER)\n  return "safe"\ntry: '
                       + expression + '\nexcept (TypeError,AttributeError): pass')
            with self.subTest(invalid_stream=expression):
                self.assert_python_patch_ownership(program, owning)
                self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
        observed = []
        class CountingValue:
            def __repr__(self):
                observed.append(None)
                return 'safe'
        class CountingStream:
            @property
            def write(self):
                observed.append(None)
                return self.emit
            def emit(self, text):
                observed.append(None)
        pprint.pprint(CountingValue(), stream=CountingStream())
        visits = len(observed)
        for position in range(visits + 1):
            commands = ['echo safe'] * (visits + 1)
            commands[position] = writer
            program = ('import os,pprint\nseen=' + repr(commands)
                       + '\nclass Value:\n def __repr__(self):\n  os.system(seen.pop(0))\n  return "safe"'
                       + '\nclass Stream:\n @property\n def write(self):\n  os.system(seen.pop(0))\n  return self.emit'
                       + '\n def emit(self,text):\n  os.system(seen.pop(0))\npprint.pprint(Value(),stream=Stream())')
            with self.subTest(stream_callback_position=position):
                self.assert_python_patch_ownership(program, position < visits)
        for owning in (True, False):
            fields = ','.join(repr(str(index)) + ':0 if sys.argv[1:] else 1' for index in range(16))
            program = ('import os,sys,pprint\nWRITER=' + repr(writer) + '\nclass Value:\n def __repr__(self):\n  os.system(WRITER)\n  return "safe"'
                       + '\npprint.pformat({' + fields + ',"last":Value()})')
            with self.subTest(widened_tree=owning):
                self.assert_python_patch_ownership(program if owning else program.replace(writer, 'echo safe'), owning)
        program = ('import os,sys,pprint\nseen=["echo safe",' + repr(writer)
                   + ']\nclass Value:\n def __repr__(self):\n  os.system(seen.pop(0))\n  return "safe"\npprint.pformat({'
                   + fields + ',"last":Value()})\nos.system(seen.pop(0))')
        self.assert_python_patch_ownership(program, True)
        self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)

    def test_python_ordering_and_json_consumers_run_only_used_callbacks(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        for operation, method in (('sorted', '__lt__'), ('min', '__lt__'), ('max', '__gt__')):
            for items, owning in (('[value,value]', True), ('[value]', False), ('[]', False)):
                if operation != 'sorted' and items == '[]':
                    continue
                program = ('import os\nWRITER=' + repr(writer) + '\nclass Value:\n def ' + method
                           + '(self,other):\n  os.system(WRITER)\n  return False\nvalue=Value()\n' + operation + '(' + items + ')')
                with self.subTest(operation=operation, items=items):
                    self.assert_python_patch_ownership(program, owning)
                    self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
            for owning in (True, False):
                commands = [writer, 'echo safe'] if owning else ['echo safe', writer]
                program = ('import os\nseen=' + repr(commands) + '\nclass Value:\n def ' + method
                           + '(self,other):\n  os.system(seen.pop(0))\n  return False\nvalue=Value()\n' + operation + '([value,value])')
                with self.subTest(comparison_once=operation, owning=owning):
                    self.assert_python_patch_ownership(program, owning)
            program = ('import os\nclass Value:\n def ' + method + '(self,other):\n  os.system('
                       + repr(writer) + ')\n  return False\nvalue=Value()\n' + operation + '([value,value],key=lambda item:0)')
            self.assert_python_patch_ownership(program, False)
        program = ('import os\nseen=[' + repr(writer) + ',"echo safe"]\nclass Flag:\n def __bool__(self):\n  os.system(seen.pop(0))\n  return False\nsorted([1],key=lambda item:seen.pop(0),reverse=Flag())')
        self.assert_python_patch_ownership(program, True)
        self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
        program = ('import os\nWRITER=' + repr(writer) + '\nclass Left:\n def __lt__(self,other): return NotImplemented\nclass Right:\n def __gt__(self,other):\n  os.system(WRITER)\n  return False\nsorted([Right(),Left()])')
        self.assert_python_patch_ownership(program, True)
        self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
        for operation in ('min', 'max'):
            program = ('import os,sys\nWRITER=' + repr(writer) + '\nranks={"echo safe":int(sys.argv[1]),WRITER:0}\nos.system('
                       + operation + '(["echo safe",WRITER],key=lambda value:ranks[value]))')
            self.assert_python_patch_ownership(program, True)
            self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
        for expression, method, result in (('complex(value,0)', '__complex__', '1j'),
                                           ('complex(0,value)', '__float__', '1.0'),
                                           ('complex(real=value)', '__complex__', '1j'),
                                           ('complex(imag=value)', '__float__', '1.0')):
            program = ('import os\nWRITER=' + repr(writer) + '\nclass Value:\n def ' + method
                       + '(self):\n  os.system(WRITER)\n  return ' + result + '\nvalue=Value()\n' + expression)
            with self.subTest(complex_operands=expression):
                self.assert_python_patch_ownership(program, True)
                self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
        for expression, owning in (('json.dumps(value,default=encode)', True),
                                   ('json.dumps([{"nested":value}],default=encode)', True),
                                   ('json.dumps([1,"safe"],default=encode)', False)):
            program = ('import os,json\nWRITER=' + repr(writer) + '\nclass Value: pass\nvalue=Value()\ndef encode(item):\n os.system(WRITER)\n return None\n' + expression)
            with self.subTest(json_default=expression):
                self.assert_python_patch_ownership(program, owning)
                self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)

    def test_python_pretty_print_bounds_optional_primitive_trees(self):
        def measured(size, limit=None):
            fields = ','.join(repr(str(index)) + ':0 if sys.argv[1:] else 1' for index in range(size))
            program = 'import sys,pprint\npprint.pformat({' + fields + '})'
            calls = 0
            def profile(frame, event, argument):
                nonlocal calls
                if event == 'call':
                    calls += 1
                    if limit is not None and calls > limit:
                        raise AssertionError('pretty-print alternatives exceed linear growth budget')
            sys.setprofile(profile)
            try:
                self.assertFalse(receipts.python_ownership(program))
            finally:
                sys.setprofile(None)
            return calls
        small = measured(8)
        self.assertLessEqual(measured(16, small * 3), small * 3)


    def test_python_text_conversions_follow_actual_callbacks(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        setup = 'import os\nWRITER=' + repr(writer) + '\n'
        programs = (
            ('print([Value()])', '__repr__'),
            ('print((Value(),))', '__repr__'),
            ('print({"key":Value()})', '__repr__'),
            ('print([{"key":(Value(),)}])', '__repr__'),
            ('str(Value())', '__str__'),
            ('repr(Value())', '__repr__'),
            ('ascii(Value())', '__repr__'),
            ("f'{Value()}'", '__str__'),
            ("f'{Value()!s}'", '__str__'),
            ("f'{Value()!r}'", '__repr__'),
            ("f'{Value()!a}'", '__repr__'),
            ("f'{Value():x}'", '__format__'),
            ('format(Value())', '__str__'),
            ('format(Value(),"x")', '__format__'),
            ("'{}'.format(Value())", '__str__'),
            ("'{:x}'.format(Value())", '__format__'),
            ("'%s' % Value()", '__str__'),
            ("'%r' % Value()", '__repr__'),
            ("'%s:%s' % (Value(),Value())", '__str__'),
            ("'%(v)s' % {'v':Value()}", '__str__'),
        )
        for expression, method in programs:
            parameters = 'self,spec' if method == '__format__' else 'self'
            program = (setup + 'class Value:\n def ' + method + '(' + parameters
                       + '):\n  os.system(WRITER)\n  return "safe"\n' + expression)
            with self.subTest(expression=expression):
                self.assert_python_patch_ownership(program, True)
                self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
        for expression in ('repr(Value())', 'ascii(Value())', "f'{Value()!r}'", "'x{!r}'.format(Value())", 'shlex.quote(Value())'):
            program = (setup + 'import shlex\nclass Value:\n def __str__(self):\n  os.system(WRITER)\n  return "safe"\n def __repr__(self): return "safe"\ntry: '
                       + expression + '\nexcept TypeError: pass')
            with self.subTest(skipped_str=expression):
                self.assert_python_patch_ownership(program, False)
        program = (setup + 'class Value:\n def __str__(self): return 123\nclass Destination:\n def write(self,chunk): os.system(WRITER)\ntry: print(Value(),file=Destination())\nexcept TypeError: pass')
        self.assert_python_patch_ownership(program, False)

    def test_python_text_conversion_results_are_evaluated_once(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        for expression, method in (('str(Value())', '__str__'), ("f'{Value()}'", '__str__'),
                                   ("'%s' % Value()", '__str__'), ("'{}'.format(Value())", '__str__'),
                                   ('repr(Value())', '__repr__')):
            for owning in (True, False):
                commands = [writer, 'echo safe'] if owning else ['echo safe', writer]
                program = ('import os\nseen=' + repr(commands) + '\nclass Value:\n def ' + method
                           + '(self): return seen.pop(0)\nos.system(' + expression + ')')
                with self.subTest(expression=expression, owning=owning):
                    self.assert_python_patch_ownership(program, owning)
        for expression in ('list(map(str,[Value()]))', 'from builtins import str as display\ndisplay(Value())'):
            program = ('import os\nWRITER=' + repr(writer) + '\nclass Value:\n def __str__(self):\n  os.system(WRITER)\n  return "safe"\n' + expression)
            with self.subTest(indirect=expression):
                self.assert_python_patch_ownership(program, True)
                self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)

    def test_python_numeric_formats_preserve_uncertain_command_scaffolding(self):
        header = 'uv run /catalog/gh-plan.py --repo o/r'
        setup = 'import os,sys\n'
        programs = (
            "os.system('uv run /catalog/gh-plan.py --repo o/r claim %d' % int(sys.argv[1]))",
            "os.system(f'uv run /catalog/gh-plan.py --repo o/r claim {int(sys.argv[1]):d}')",
            "n=int(sys.argv[1]);os.system('uv run /catalog/gh-plan.py --repo o/r claim {:d}'.format(n))",
            "spec=sys.argv[2];os.system(f'uv run /catalog/gh-plan.py --repo o/r claim {12:{spec}}')",
            "spec=sys.argv[2];os.system('uv run /catalog/gh-plan.py --repo o/r claim '+format(12,spec))",
            "n=int(sys.argv[1]);spec=sys.argv[2];os.system(f'uv run /catalog/gh-plan.py --repo o/r claim {n:{spec}}')",
            "n=int(sys.argv[1]);os.system(f'uv run /catalog/gh-plan.py --repo o/r claim {n:>4}')",
            "n=int(sys.argv[1]);os.system('uv run /catalog/gh-plan.py --repo o/r claim '+format(n,'x'))",
            "A=" + repr(header) + ";B='echo safe';os.system('%(h)s claim 5' % {'h':A if sys.argv[1:] else B})",
            "template=sys.argv[1]\nclass Value:\n def __format__(self,spec):\n  os.system("
            + repr(header + ' claim 12') + ");return 'safe'\nprint(template.format(Value()))",
        )
        for body in programs:
            with self.subTest(program=body):
                self.assert_python_patch_ownership(setup + body, True)
                self.assert_python_patch_ownership((setup + body).replace(header, 'echo safe'), False)

        for expression in (repr(header + ' claim %d') + " % 'bad'",
                           repr(header + ' claim {:d}') + ".format('bad')",
                           "f'" + header + " claim {12:bad}'"):
            program = setup + "try: os.system(" + expression + ")\nexcept (TypeError,ValueError): pass"
            with self.subTest(invalid_numeric_conversion=expression):
                self.assert_python_patch_ownership(program, False)

        for expression in (repr(header + ' claim {:d}') + '.format(Value())',
                           repr(header + ' claim %d') + ' % Value()'):
            program = (setup + 'class Value: pass\ntry: os.system(' + expression
                       + ')\nexcept (TypeError,ValueError): pass')
            with self.subTest(unsupported_object_numeric_conversion=expression):
                self.assert_python_patch_ownership(program, False)
        for conversion, method, returned in (('%d', '__int__', '12'), ('%d', '__index__', '12'),
                                              ('%x', '__index__', '12'), ('%f', '__float__', '12.0')):
            program = (setup + 'class Value:\n def ' + method + '(self):\n  os.system('
                       + repr(header + ' claim 12') + ');return ' + returned
                       + '\n' + repr('echo safe ' + conversion) + ' % Value()')
            with self.subTest(numeric_callback=method, conversion=conversion):
                self.assert_python_patch_ownership(program, True)
                self.assert_python_patch_ownership(program.replace(header, 'echo safe'), False)

    def test_python_unknown_templates_preserve_callback_count_and_protocols(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        programs = (
            "class Value:\n def __getitem__(self,key): return WRITER\nos.system(sys.argv[1] % Value())",
            "class Leaf:\n def __str__(self):\n  os.system(WRITER)\n  return 'safe'\nclass Value:\n @property\n def attr(self): return Leaf()\nsys.argv[1].format(Value())",
            "class Leaf:\n def __init__(self,command): self.command=command\n def __str__(self):\n  os.system(self.command)\n  return 'safe'\nclass Value:\n @property\n def first(self): return Leaf('echo safe')\n @property\n def second(self): return Leaf(WRITER)\nsys.argv[1].format(Value())",
            "saved=None\nclass Leaf:\n def __str__(self):\n  self.command=WRITER\n  return 'safe'\nclass Value:\n @property\n def attr(self):\n  global saved\n  saved=Leaf()\n  return saved\nsys.argv[1].format(Value())\nos.system(saved.command)",
            "class Leaf:\n def __repr__(self):\n  os.system(WRITER)\n  return 'safe'\nclass Value:\n @property\n def attr(self): return [Leaf()]\nsys.argv[1].format(Value())",
            "def make(command):\n class Leaf:\n  def __str__(self):\n   os.system(command)\n   return 'safe'\n return Leaf()\nclass Value:\n @property\n def first(self): return make('echo safe')\n @property\n def second(self): return make(WRITER)\nsys.argv[1].format(Value())",
            "def make(command):\n class Leaf:\n  def __str__(self,command=command):\n   os.system(command)\n   return 'safe'\n return Leaf()\nclass Value:\n @property\n def first(self): return make('echo safe')\n @property\n def second(self): return make(WRITER)\nsys.argv[1].format(Value())",
            "os.system(sys.argv[1] % WRITER)",
            "os.system((sys.argv[1]+' ') % WRITER)",
            "class Value:\n def __repr__(self):\n  os.system(WRITER)\n  return 'safe'\n(sys.argv[1]+' ').format(Value())",
            "seen=[WRITER,'echo safe']\nclass Value:\n def __format__(self,spec): return seen.pop(0)\nsys.argv[1].format(Value())\nos.system(seen.pop(0))",
            "seen=['echo safe','echo safe','echo safe',WRITER]\nclass Value:\n def __format__(self,spec): return seen.pop(0)\nsys.argv[1].format(Value())\nos.system(seen.pop(0))",
            "class Value:\n def __format__(self,spec): return 'safe'\n def __repr__(self):\n  os.system(WRITER)\n  return 'safe'\nsys.argv[1].format(Value())",
            "class Value:\n def __format__(self,spec): return 'safe'\n def __str__(self):\n  os.system(WRITER)\n  return 'safe'\nsys.argv[1].format(Value())",
            "class Value:\n @property\n def attr(self):\n  os.system(WRITER)\n  return 'safe'\nsys.argv[1].format(Value())",
            "class Value:\n def __getitem__(self,key):\n  os.system(WRITER)\n  return 'safe'\nsys.argv[1].format(Value())",
            "class Value:\n @property\n def attr(self): return WRITER\nos.system(sys.argv[1].format(Value()))",
            "class Value:\n def __getitem__(self,key): return WRITER\nos.system(sys.argv[1].format(Value()))",
            "class Value:\n def __repr__(self):\n  os.system(WRITER)\n  return 'safe'\nsys.argv[1] % Value()",
        )
        setup = 'import os,sys\nWRITER=' + repr(writer) + '\n'
        for body in programs:
            with self.subTest(program=body):
                self.assert_python_patch_ownership(setup + body, True)
                self.assert_python_patch_ownership((setup + body).replace(writer, 'echo safe'), False)
        for body in (
            "def make():\n class Value:\n  @property\n  def attr(self): return make()\n  def __str__(self): return 'safe'\n return Value()\nsys.argv[1].format(make())",
            "class Value:\n @property\n def attr(self): return Value()\n def __str__(self): return 'safe'\nsys.argv[1].format(Value())",
            "class Value:\n def __init__(self,word): self.word=word\n @property\n def attr(self): return Value(self.word+'x')\n def __str__(self): return self.word\nsys.argv[1].format(Value('safe'))",
        ):
            with self.subTest(recursive_return=body):
                self.assert_python_patch_ownership(setup + body, False)

    def test_python_optional_callbacks_preserve_captured_scope_alternatives(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        program = """import os, sys
WRITER = COMMAND
def make():
 command = WRITER
 class Value:
  def __format__(self, spec):
   nonlocal command
   command = 'echo safe'
   return 'safe'
 def get(): return command
 return Value(), get
v, get = make()
TEMPLATE.format(v)
os.system(get())
""".replace('COMMAND', repr(writer))
        for template in ('sys.argv[1]', "('{}' if sys.argv[1] else 'x')"):
            with self.subTest(template=template):
                source = program.replace('TEMPLATE', template)
                self.assert_python_patch_ownership(source, True)
                self.assert_python_patch_ownership(source.replace(writer, 'echo safe'), False)

    def test_python_format_alternatives_share_one_evaluation_state(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        programs = (
            ('__str__', "fmt='{}' if sys.argv[1:] else '{} ';print(fmt.format(Value()))"),
            ('__str__', "fmt='%s' if sys.argv[1:] else '%s ';print(fmt % Value())"),
            ('__format__', "spec='' if sys.argv[1:] else 'x';print(f'{Value():{spec}}')"),
            ('__format__', "spec='' if sys.argv[1:] else 'x';print(format(Value(),spec))"),
            ('__format__', "fmt='{:x}' if sys.argv[1:] else '{:y}';print(fmt.format(Value()))"),
            ('__str__', "value=Value();fmt='{0!s}{0!s}' if sys.argv[1:] else '{0!s} {0!s}';print(fmt.format(value))"),
        )
        for method, body in programs:
            # Explicit repeated fields still convert twice within each path.
            initial = ['echo safe'] * (2 if '{0!s}{0!s}' in body else 1) + [writer]
            parameters = 'self,spec' if method == '__format__' else 'self'
            program = ('import os,sys\nseen=' + repr(initial) + '\nclass Value:\n def '
                       + method + '(' + parameters + '): return seen.pop(0)\n' + body + '\nos.system(seen.pop(0))')
            with self.subTest(program=body):
                self.assert_python_patch_ownership(program, True)
                self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)

    def test_python_text_conversions_use_class_special_methods(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        setup = 'import os\nWRITER=' + repr(writer) + '\n'
        for expression, method in (('str(value)', '__str__'), ('repr(value)', '__repr__'),
                                   ('ascii(value)', '__repr__'), ("f'{value:x}'", '__format__'),
                                   ('print([value])', '__repr__')):
            for owning in (True, False):
                parameters = 'self,spec' if method == '__format__' else 'self'
                body = 'os.system(WRITER)\n  return "safe"' if owning else 'return "safe"'
                replacement = 'lambda *args: "safe"' if owning else 'lambda *args: os.system(WRITER) or "safe"'
                program = (setup + 'class Value:\n def ' + method + '(' + parameters + '):\n  '
                           + body + '\nvalue=Value()\nvalue.' + method + '=' + replacement + '\n' + expression)
                with self.subTest(expression=expression, owning=owning):
                    self.assert_python_patch_ownership(program, owning)

        for owning in (True, False):
            body = 'os.system(WRITER)\n  return False' if owning else 'return False'
            replacement = 'lambda: False' if owning else 'lambda: os.system(WRITER) or False'
            program = (setup + 'class Flag:\n def __bool__(self):\n  ' + body
                       + '\nflag=Flag()\nflag.__bool__=' + replacement + '\nprint("safe",flush=flag)')
            with self.subTest(flush_class_method=owning):
                self.assert_python_patch_ownership(program, owning)

        for kind in ('Mock', 'MagicMock'):
            for method, expression in (('__str__', 'print(value)'), ('__repr__', 'repr(value)'),
                                       ('__format__', 'format(value,"x")')):
                program = (setup + 'from unittest.mock import Mock,MagicMock\ndef callback(*args):\n os.system(WRITER)\n return "safe"\nvalue='
                           + kind + '()\nvalue.' + method + '=Mock(side_effect=callback)\n' + expression)
                with self.subTest(mock_class=kind, method=method):
                    self.assert_python_patch_ownership(program, True)
                    self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)

    def test_unknown_python_text_stays_uncertain_through_data_consumers(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        setup = 'import os,sys\nWRITER=' + repr(writer) + '\n'
        programs = (
            ('equal', "mode=f'{sys.argv[1]}'\nprograms=[]\nfor cmd in [WRITER]:\n if mode=='run': programs.append(cmd)\nfor cmd in programs: os.system(cmd)", True),
            ('conditional', "mode=f'{sys.argv[1]}'\nprograms=[]\nfor cmd in [WRITER]: programs.append(cmd if mode=='run' else 'echo safe')\nfor cmd in programs: os.system(cmd)", True),
            ('not_equal', "mode=f'{sys.argv[1]}'\nprograms=[]\nfor cmd in [WRITER]:\n if mode!='run': programs.append('echo safe')\n else: programs.append(cmd)\nfor cmd in programs: os.system(cmd)", True),
            ('truth', "flag=f'{sys.argv[1]}'\nprograms=[]\nfor cmd in [WRITER]:\n if flag: programs.append('echo safe')\n else: programs.append(cmd)\nfor cmd in programs: os.system(cmd)", True),
            ('loop_item', "programs=[]\nfor mode in [f'{sys.argv[1]}']:\n if mode=='run': programs.append(WRITER)\nfor cmd in programs: os.system(cmd)", True),
            ("(f'{sys.argv[1]}').replace('$','')", "mode=(f'{sys.argv[1]}').replace('$','')\nprograms=[]\nfor cmd in [WRITER]:\n if mode=='run': programs.append(cmd)\nfor cmd in programs: os.system(cmd)", True),
            ("(f'{sys.argv[1]}').lstrip('$')", "mode=(f'{sys.argv[1]}').lstrip('$')\nprograms=[]\nfor cmd in [WRITER]:\n if mode=='run': programs.append(cmd)\nfor cmd in programs: os.system(cmd)", True),
            ("' '.join(sys.argv[1:])", "mode=' '.join(sys.argv[1:])\nprograms=[]\nfor cmd in [WRITER]:\n if mode=='run': programs.append(cmd)\nfor cmd in programs: os.system(cmd)", True),
            ('literal_control', "mode='dry'\nprograms=[]\nfor cmd in [WRITER]:\n if mode=='run': programs.append(cmd)\nfor cmd in programs: os.system(cmd)", False),
        )
        for name, body, expected in programs:
            with self.subTest(consumer=name):
                self.assert_python_patch_ownership(setup + body, expected)
                if expected:
                    self.assert_python_patch_ownership((setup + body).replace(writer, 'echo safe'), False)

    def test_python_mutable_source_carriers_bound_comparison_work(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        for operation in ('programs.append(source)', 'programs.extend([source])',
                          'programs.append((1,source))', 'programs += [(1,source)]', 'paired sources', 'paired sources with display', 'paired sources rendered with display'):
            counts = []
            for size in (4, 8):
                sources = ['import os\nos.system(' + repr(writer) + ')\nmarker=' + repr(index) for index in range(size)]
                body = 'for source in S: ' + operation + '\n'
                body += ('for label,source in programs: exec(source,{})' if '(1,source)' in operation
                         else 'for source in programs: exec(source,{})')
                program = ('from unittest.mock import patch\nS=' + repr(sources)
                           + '\nprograms=[]\ndef record(*a): return 0\nwith patch("os.system",record):\n'
                           + '\n'.join('    ' + line for line in body.splitlines()))
                if operation.startswith('paired sources'):
                    pairs = [(writer if index == 0 else f'echo safe {index}', index == 0) for index in range(size)]
                    body = ("for command,expected in S:\n"
                            "    programs += [(f'import os\\nos.system({command!r})\\nmarker=0',expected),"
                            "(f'import os\\nos.system({command!r})\\nmarker=1',expected)]\n"
                            "for source,expected in programs: exec(source,{})")
                    if operation.endswith('with display'):
                        body = body.replace('    programs +=', '    print(command)\n    programs +=')
                    if 'rendered' in operation:
                        body = body.replace('for source,expected in programs: exec(source,{})', "exec('\\n'.join(source for source,expected in programs),{})")
                    program = ('from unittest.mock import patch\nS=' + repr(pairs)
                               + '\nprograms=[]\ndef record(*a): return 0\nwith patch("os.system",record):\n'
                               + '\n'.join('    ' + line for line in body.splitlines()))
                calls = []
                def profile(frame, event, _argument):
                    if event == 'call' and frame.f_code.co_filename == receipts.__file__:
                        calls.append(None)
                previous = sys.getprofile()
                try:
                    sys.setprofile(profile)
                    self.assertFalse(receipts.python_ownership(program))
                finally:
                    sys.setprofile(previous)
                counts.append(len(calls))
                self.assertTrue(receipts.python_ownership(program.replace('with patch("os.system",record):', 'if True:')))
            with self.subTest(operation=operation):
                self.assertGreater(counts[0], 0)
                self.assertLessEqual(counts[1], counts[0] * 3)

        setup = "import os\nWRITER='uv run /catalog/gh-plan.py --repo o/r claim 12'\n"
        programs = (
            ('target_rebind', 'cmd=WRITER\nfor cmd in ["echo safe"]: os.system(cmd)', False),
            ('target_writer', 'cmd="echo safe"\nfor cmd in [WRITER]: os.system(cmd)', True),
            ('source_append', 'source=["echo first"]\nfor cmd in source:\n os.system(cmd)\n if cmd=="echo first": source.append(WRITER)', True),
            ('source_name_rebind', 'source=["echo first"]\nfor cmd in source:\n source=[WRITER]\n os.system(cmd)', False),
            ('empty_loop_target', 'cmd=WRITER\nfor cmd in []: pass\nos.system(cmd)', True),
            ('break', "for cmd in [WRITER,'echo safe']:\n break\nos.system(cmd)", True),
            ('continue', "cmd=WRITER\nfor item in [1]:\n continue\n cmd='echo safe'\nos.system(cmd)", True),
            ('raise', "try:\n for cmd in [WRITER,'echo safe']:\n  raise RuntimeError('stop')\nexcept RuntimeError: pass\nos.system(cmd)", True),
            ('return', "def choose():\n for cmd in [WRITER,'echo safe']:\n  return cmd\nos.system(choose())", True),
            ('mutation', "source=[WRITER,'echo safe']\nfor cmd in source:\n source.clear()\nos.system(cmd)", True),
            ('alias_mutation', "source=[WRITER,'echo safe']\nalias=[]\nfor cmd in source:\n alias=source\n alias.clear()\nos.system(cmd)", True),
            ('opaque_escape', "from n_data_fixture import noop\nsource=[WRITER,'echo safe']\nfor cmd in source:\n noop(source)\n break\nos.system(cmd)", True),
            ('rollback_after_prefix', "source=[0,1]\nalias=[]\nprograms=[]\nfor item in source:\n programs.append(WRITER if item==0 else 'echo safe')\n if item==1:\n  alias=source\n  alias.clear()\nfor cmd in programs: os.system(cmd)", True),
            ('safe_builder', "programs=[]\nfor cmd,active in [('echo one',True),('echo two',False)]:\n programs.append(cmd if active else 'echo safe')\nfor cmd in programs: os.system(cmd)", False),
            ('writer_builder', "programs=[]\nfor cmd,active in [(WRITER,True),('echo two',False)]:\n programs.append(cmd if active else 'echo safe')\nfor cmd in programs: os.system(cmd)", True),
            ('implicit_int', "cmd='echo safe'\ntry:\n cmd=WRITER\n for item in [1]:\n  int('bad')\n  cmd='echo safe'\nexcept Exception: pass\nos.system(cmd)", True),
            ('implicit_index', "cmd='echo safe'\ntry:\n cmd=WRITER\n for item in [1]:\n  [][0]\n  cmd='echo safe'\nexcept Exception: pass\nos.system(cmd)", True),
            ('implicit_format', "cmd='echo safe'\ntry:\n cmd=WRITER\n for item in [1]:\n  f'{1:bad}'\n  cmd='echo safe'\nexcept Exception: pass\nos.system(cmd)", True),
            ('bad_unpack', "cmd='echo safe'\ntry:\n cmd=WRITER\n for a,b in [1]:\n  pass\n  cmd='echo safe'\nexcept Exception: pass\nos.system(cmd)", True),
            ('mixed_add', "cmd='echo safe'\ntry:\n cmd=WRITER\n for item in [1]:\n  'echo '+[]\n  cmd='echo safe'\nexcept Exception: pass\nos.system(cmd)", True),
            ('property_False', 'class Probe:\n @property\n def item(self):\n  os.system(WRITER)\n  return []\nobj=Probe()\nfor item in [1]:\n if False: len(obj.item)', False),
            ('property_True', 'class Probe:\n @property\n def item(self):\n  os.system(WRITER)\n  return []\nobj=Probe()\nfor item in [1]:\n if True: len(obj.item)', True),
            ('repr_list', "for item in [1]:\n row=[WRITER]\n program='import os\\nos.system('+repr(row)+'[0])'\nexec(program,{})", True),
            ('repr_tuple', "for item in [1]:\n row=(WRITER,)\n program='import os\\nos.system('+repr(row)+'[0])'\nexec(program,{})", True),
            ('repr_mapping', "for item in [1]:\n row={'command':WRITER}\n program='import os\\nos.system('+repr(row)+'.get('+repr('command')+'))'\nexec(program,{})", True),
            ('fstring_list', "for item in [1]:\n row=[WRITER]\n program=f'import os\\nos.system({row}[0])'\nexec(program,{})", True),
            ('tuple_alias', "inner=[]\nfor item in [1]: row=(inner,)+('echo safe',)\ninner.append(WRITER)\nfor cmd in row[0]: os.system(cmd)", True),
            ('list_alias', "inner=[]\nfor item in [1]: row=[inner]+['echo safe']\ninner.append(WRITER)\nfor cmd in row[0]: os.system(cmd)", True),
            ('extend_tuple', 'rows=[]\nfor item in [1]: rows += (WRITER,)\nfor cmd in rows: os.system(cmd)', True),
        )
        for name, body, expected in programs:
            with self.subTest(control=name):
                self.assert_python_patch_ownership(setup + body, expected)
                if expected:
                    self.assert_python_patch_ownership((setup + body).replace(writer, "echo safe"), False)

        previous_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(sys.int_info.default_max_str_digits)
            huge = '0x' + 'f' * sys.int_info.default_max_str_digits
            for expression in ('str(' + huge + ')', 'repr([' + huge + '])',
                               'ascii([' + huge + '])', "f'{[" + huge + "]}'",
                               *("f'{[" + huge + "]!" + conversion + "}'" for conversion in 'rsa')):
                for context in ('direct', 'loop'):
                    body = 'rendered=' + expression if context == 'direct' else 'for item in [1]:\n rendered=' + expression
                    body += "\ncmd='echo safe'" if context == 'direct' else "\n cmd='echo safe'"
                    program = (setup + "cmd=WRITER\ntry:\n " + body.replace('\n', '\n ')
                               + "\nexcept Exception: pass\nos.system(cmd)")
                    with self.subTest(control='conversion raises', operation=expression.split('(')[0], context=context):
                        self.assertTrue(receipts.python_ownership(program))
                        self.assertFalse(receipts.python_ownership(program.replace(writer, 'echo safe')))
                        self.assert_python_patch_ownership(program, True)
                        self.assert_python_patch_ownership(program.replace(writer, 'echo safe'), False)
        finally:
            sys.set_int_max_str_digits(previous_limit)

    def test_python_shared_sequence_branches_bound_comparison_work(self):
        writer = ['uv', 'run', '/catalog/gh-plan.py', '--repo', 'o/r', 'claim', '12']
        for operation in ('cmd.append(flag)', 'cmd.extend([flag])', 'cmd += [flag]'):
            counts = []
            for size in (8, 16):
                program = ('import subprocess,sys\ncmd=' + repr(writer) + '\n'
                           + ''.join('if sys.argv[1:]: ' + operation.replace('flag', repr('--flag' + str(index))) + '\n' for index in range(size))
                           + 'subprocess.run(cmd)')
                calls = [0]
                def profile(frame, event, _argument):
                    if event == 'call' and frame.f_code.co_filename == receipts.__file__:
                        calls[0] += 1
                        if counts and calls[0] > counts[0] * 3:
                            raise AssertionError('optional argument growth exceeds comparison-work bound')
                previous = sys.getprofile()
                try:
                    sys.setprofile(profile)
                    self.assertTrue(receipts.python_ownership(program))
                finally:
                    sys.setprofile(previous)
                counts.append(calls[0])
                self.assert_python_patch_ownership(program, True)
                self.assert_python_patch_ownership(program.replace(repr(writer), repr(['echo', 'safe'])), False)
            with self.subTest(operation=operation):
                self.assertGreater(counts[0], 0)
                self.assertLessEqual(counts[1], counts[0] * 3)

    def test_python_sequence_depth_does_not_limit_receipt_coverage(self):
        writer = ['uv', 'run', '/catalog/gh-plan.py', '--repo', 'o/r', 'claim', '12']
        conditional = 'if sys.argv[1:]: choice="--draft"\nelse: choice="--verbose"\n'
        literal = '[' + ','.join([*map(repr, writer), 'choice', *['"extra"'] * (2000 - len(writer) - 1)]) + ']'
        prefix = 'import subprocess,sys,os\ndef run(argv): subprocess.run(argv)\n'
        builder = 'cmd=' + repr(writer) + '\n' + 'cmd.append("extra")\n' * 1200
        programs = (
            ('literal', conditional + 'run(' + literal + ')'),
            ('appends', builder + 'run(cmd)'),
            ('members', builder + 'for word in cmd:\n os.system(word if word.endswith("claim 12") else "echo safe")'),
            ('exact_data', builder + "exec('import subprocess\\nsubprocess.run('+repr(cmd)+')')"),
            ('render', builder + 'os.system(" ".join(cmd))'),
        )
        for name, body in programs:
            # The members row needs a complete command rather than argv words.
            if name == 'members':
                command = ' '.join(writer)
                body = ('cmd=[' + repr(command) + ']\n' + 'cmd.append("echo safe")\n' * 1200
                        + 'for word in cmd: os.system(word)')
            with self.subTest(carrier=name):
                program = prefix + body
                self.assertTrue(receipts.python_ownership(program))
                if name == 'literal':
                    safe = program.replace(','.join(map(repr, writer)), ','.join(map(repr, ['echo', 'safe'])))
                elif name == 'members':
                    safe = program.replace(repr(command), repr('echo safe'))
                else:
                    safe = program.replace(repr(writer), repr(['echo', 'safe']))
                self.assertFalse(receipts.python_ownership(safe))

    def test_python_generators_preserve_full_and_partial_consumption(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 12'
        setup = 'import os, sys\nWRITER=' + repr(writer) + '\n'
        builder = 'rows=[]\nfor item in [WRITER]: rows.append(item)\ng=(str(row) for row in rows)\n'
        forms = [
            ('first_result', builder + 'os.system("".join(g))', True),
            ('fresh_generator', builder + 'first="".join(g)\ng=(str(row) for row in rows)\nos.system("".join(g))', True),
            ('partial_for', 'g=(x for x in ["echo safe",WRITER])\nfor x in g: break\nos.system("".join(g))', True),
            ('partial_next', 'g=(x for x in ["echo safe",WRITER])\nnext(g)\nos.system("".join(g))', True),
            ('partial_wrapped_next', 'g=(x for x in ["echo safe",WRITER])\nit=iter(g)\nnext(it)\nos.system("".join(g))', True),
            ('conditional_consumption', builder + 'if "--dry-run" in sys.argv: first="".join(g)\nos.system("".join(g))', True),
            ('mapper_callback', 'def mapper(item):\n os.system(WRITER)\n return item\ng=(mapper(x) for x in ["echo safe"])\nfirst="".join(g)\nsecond="".join(g)', True),
            ('map_exhausted', 'g=map(str,[WRITER])\nfirst="".join(g)\nos.system("".join(g))', False),
            ('exhausted_late_binding', 'command="echo safe"\ng=(command for x in [1])\nfirst="".join(g)\ncommand=WRITER\nos.system("".join(g))', False),
        ]
        for consumer in ('"".join(g)', 'list(g)', 'tuple(g)', 'sorted(g)'):
            forms.append((consumer, builder + 'first=' + consumer + '\nos.system("".join(g))', False))
            for binding in ('g=a if sys.argv[1:] else b',
                            'if sys.argv[1:]: g=a\nelse: g=b'):
                forms.append(('possible_alias_' + consumer + binding,
                              'a=(x for x in [WRITER])\nb=(x for x in ["echo safe"])\n'
                              + binding + '\nfirst=' + consumer + '\nos.system("".join(a))', True))
        for name, body, expected in forms:
            with self.subTest(control=name):
                self.assert_python_patch_ownership(setup + body, expected)
                self.assert_python_patch_ownership((setup + body).replace(writer, 'echo safe'), False)

    def test_python_object_patches_and_iterated_mock_values(self):
        writer = "uv run /catalog/gh-plan.py --repo o/r claim 12"
        setup = ("import os, subprocess, itertools\nfrom unittest.mock import patch, Mock, DEFAULT\nWRITER=" + repr(writer)
                 + "\ndef record(*a,**k): return 0\nclass Runner:\n    x=None\n    def run(self,command): os.system(command)\n")
        forms = (
            ('class-object', "with patch.object(Runner,'run',record): Runner().run(WRITER)", False),
            ('instance-object', "runner=Runner()\nwith patch.object(runner,'run',record): runner.run(WRITER)", False),
            ('class-multiple', 'with patch.multiple(Runner,run=record): Runner().run(WRITER)', False),
            ('instance-multiple', 'runner=Runner()\nwith patch.multiple(runner,run=record): runner.run(WRITER)', False),
            ('object-injection', "@patch('os.system')\n@patch.object(Runner,'x')\ndef run(mock_x,mock_system):\n    mock_system.return_value=WRITER\n    return os.system('echo safe')\nos.system(run())", True),
            ('multiple-injection', '@patch.multiple(Runner,x=DEFAULT)\ndef run(x):\n    x.return_value=WRITER\n    return Runner.x()\nos.system(run())', True),
            ('class-restored', "with patch.object(Runner,'run',record): pass\nRunner().run(WRITER)", True),
            ('instance-restored', "runner=Runner()\nwith patch.object(runner,'run',record): pass\nrunner.run(WRITER)", True),
            ('iter-clear', "with patch('subprocess.run',side_effect=iter([0,0])): subprocess.run(WRITER,shell=True)", False),
            ('cycle-clear', "with patch('subprocess.run',side_effect=itertools.cycle([0,0])): subprocess.run(WRITER,shell=True)", False),
            ('iter-returned-writer', "mock=Mock(side_effect=iter([WRITER]))\nos.system(mock('echo safe'))", True),
            ('cycle-returned-writer', "mock=Mock(side_effect=itertools.cycle([WRITER]))\nos.system(mock('echo safe'))", True),
            ('iter-second-safe', "mock=Mock(side_effect=iter([WRITER,'echo safe']))\nmock('ignored')\nos.system(mock('echo safe'))", False),
            ('cycle-second-safe', "mock=Mock(side_effect=itertools.cycle([WRITER,'echo safe']))\nmock('ignored')\nos.system(mock('echo safe'))", False),
            ('cycle-third-writer', "mock=Mock(side_effect=itertools.cycle([WRITER,'echo safe']))\nmock('ignored')\nmock('ignored')\nos.system(mock('echo safe'))", True),
            ('iter-delayed-body', 'def values():\n    os.system(WRITER)\n    yield 0\nmock=Mock(side_effect=iter(values()))', False),
            ('iter-consumed-body', "def values():\n    os.system(WRITER)\n    yield 0\nmock=Mock(side_effect=iter(values()))\nmock('echo safe')", True),
            ('iter-next-clear', "source=iter([WRITER,'echo safe'])\nnext(source)\nos.system(next(iter(source)))", False),
            ('cycle-next-writer', "source=itertools.cycle([WRITER,'echo safe'])\nnext(source)\nnext(source)\nos.system(next(iter(source)))", True),
            ('list-second-safe', "mock=Mock(side_effect=[WRITER,'echo safe'])\nmock('ignored')\nos.system(mock('echo safe'))", False),
            ('cycle-retains-consumed-prefix', "items=[WRITER]\nmock=Mock(side_effect=itertools.cycle(items))\nmock('ignored')\nitems.clear()\nos.system(mock('ignored'))", True),
            ('cycle-preserves-underlying-position', "source=iter(['echo safe',WRITER])\nmock=Mock(side_effect=itertools.cycle(source))\nmock('ignored')\nos.system(next(source))", True),
            ('list-iterator-preserves-duplicate-order', "source=iter(['echo safe',WRITER,'echo safe'])\nmock=Mock(side_effect=list(source))\nmock('ignored')\nmock('ignored')\nos.system(mock('ignored'))", False),
            ('iter-reads-appended-source', "items=['echo safe']\nmock=Mock(side_effect=iter(items))\nmock('ignored')\nitems.append(WRITER)\nos.system(mock('ignored'))", True),
            ('cycle-closed-source-stays-closed', "items=['echo safe']\nmock=Mock(side_effect=itertools.cycle(items))\nmock('ignored')\nmock('ignored')\nitems.append(WRITER)\nos.system(mock('ignored'))", False),
            ('Child-conditional-inherited', "import contextlib,sys\nclass Child(Runner):pass\nrunner=Child()\nwith contextlib.ExitStack() as stack:\n    if '--dry-run' in sys.argv:stack.enter_context(patch.object(Child,'run',record))\n    runner.run(WRITER)", True),
            ('Child-unconditional-inherited', "import contextlib,sys\nclass Child(Runner):pass\nrunner=Child()\nwith patch.object(Child,'run',record):runner.run(WRITER)", False),
            ('runner-conditional-inherited', "import contextlib,sys\nclass Child(Runner):pass\nrunner=Child()\nwith contextlib.ExitStack() as stack:\n    if '--dry-run' in sys.argv:stack.enter_context(patch.object(runner,'run',record))\n    runner.run(WRITER)", True),
            ('runner-unconditional-inherited', "import contextlib,sys\nclass Child(Runner):pass\nrunner=Child()\nwith patch.object(runner,'run',record):runner.run(WRITER)", False),
            ('iterator-next-method', 'source=iter([WRITER])\nos.system(source.__iter__().__next__())', True),
            ('iterator-bound-next-callback', "source=iter([WRITER])\nmock=Mock(side_effect=source.__next__)\nos.system(mock())", True),
        )
        for name, body, expected in forms:
            for safe in (False, True):
                program = setup + body
                if safe:
                    program = program.replace(writer, 'echo safe')
                owning = expected and not safe
                with self.subTest(name=name, safe=safe):
                    self.assertEqual(receipts.python_ownership(program), owning)
                for harness in ('codex', 'claude'):
                    rows = history(harness, {})
                    command = 'python3 -c ' + shlex.quote(program)
                    if harness == 'codex':
                        rows[1]['payload']['arguments'] = json.dumps({'cmd': command})
                    else:
                        rows[1]['message']['content'][0]['input'] = {'command': command}
                    rows[2] = output({'exit_code': 0, 'output': ''}, harness, 'tool-1')
                    with self.subTest(name=name, safe=safe, harness=harness):
                        self.assertEqual(receipts.ownership_state(rows, harness)['pending'], owning)

    def test_python_instance_patch_uses_attribute_protocol(self):
        writer = "uv run /catalog/gh-plan.py --repo o/r claim 12"
        forms = []
        for callback in ('getter', 'setter', 'deleter'):
            definition = ("class C:\n    @property\n    def run(self):\n        " + ("os.system(WRITER)" if callback == 'getter' else "pass")
                          + "\n        return lambda *a:None\n    @run.setter\n    def run(self,value):\n        " + ("os.system(WRITER)" if callback == 'setter' else "pass")
                          + "\n    @run.deleter\n    def run(self):\n        " + ("os.system(WRITER)" if callback == 'deleter' else "pass") + "\nobj=C()\n")
            forms.append((callback, definition + "with patch.object(obj,'run',lambda *a:None):pass", True))
            forms.append((callback+'-class-control', definition + "with patch.object(C,'run',lambda *a:None):pass", False))
        forms.append(('cached-property', "from functools import cached_property\nclass C:\n    @cached_property\n    def run(self):\n        os.system(WRITER)\n        return lambda *a:None\nobj=C()\nwith patch.object(obj,'run',lambda *a:None):pass", True))
        definition = "class C:\n    command='echo safe'\n    @property\n    def run(self):\n        os.system(self.command)\n        return lambda *a:None\n    @run.setter\n    def run(self,value):self.command=WRITER\n    @run.deleter\n    def run(self):pass\nobj=C()\n"
        for create in (False, True):
            forms.append(('create-'+str(create), definition + "with patch.object(obj,'run',lambda *a:None,create=" + repr(create) + "):pass", not create))
        forms.append(('restored-instance-value', "class C:\n    def __init__(self):self._value=WRITER\n    @property\n    def run(self):return self._value\n    @run.setter\n    def run(self,value):self._value=value\n    @run.deleter\n    def run(self):del self._value\nobj=C()\nwith patch.object(obj,'run','echo safe'):pass\nos.system(obj.run)", True))
        for name, body, expected in forms:
            for safe in (False, True):
                program = "import os\nfrom unittest.mock import patch\nWRITER=" + repr(writer) + "\n" + body
                if safe:
                    program = program.replace(writer, 'echo safe')
                owning = expected and not safe
                with self.subTest(name=name, safe=safe):
                    self.assertEqual(receipts.python_ownership(program), owning)
                for harness in ('codex', 'claude'):
                    rows = history(harness, {})
                    command = 'python3 -c ' + shlex.quote(program)
                    if harness == 'codex':
                        rows[1]['payload']['arguments'] = json.dumps({'cmd': command})
                    else:
                        rows[1]['message']['content'][0]['input'] = {'command': command}
                    rows[2] = output({'exit_code': 0, 'output': ''}, harness, 'tool-1')
                    with self.subTest(name=name, safe=safe, harness=harness):
                        self.assertEqual(receipts.ownership_state(rows, harness)['pending'], owning)

    def test_python_recreated_classes_keep_independent_bindings(self):
        writer = "uv run /catalog/gh-plan.py --repo o/r claim 12"
        source = "class C:\n    command=command\n    def run(self): os.system(self.command)"
        property_source = "class C:\n    command=command\n    @property\n    def run(self): os.system(self.command)"
        inherited_source = ("class Base:\n    command=command\n    def run(self): os.system(self.command)\n"
                            "class C(Base):\n    def run(self): super().run()")
        programs = []
        for definition, access in ((source, 'first.run()'), (property_source, 'first.run'), (inherited_source, 'first.run()')):
            for initial, later, expected in ((writer, 'echo safe', True), ('echo safe', writer, False)):
                programs.append(("SOURCE=" + repr(definition) + "\ncommand=" + repr(initial)
                                 + "\nexec(SOURCE)\nfirst=C()\ncommand=" + repr(later) + "\nexec(SOURCE)\n" + access, expected))
        programs.append(("SOURCE=" + repr(source) + "\ncommand=WRITER\nexec(SOURCE)\nfirst=C\ncommand='echo safe'\nexec(SOURCE)\nfirst().run()", True))
        for returns_instance in (False, True):
            factory = ("def build(operand):\n    class C:\n        command=operand\n        def run(self): os.system(self.command)\n    return C" + ('()' if returns_instance else '') + "\n")
            access = 'first.run()' if returns_instance else 'first().run()'
            for initial, later, expected in ((writer, 'echo safe', True), ('echo safe', writer, False)):
                programs.append((factory + "first=build(" + repr(initial) + ")\nsecond=build(" + repr(later) + ")\n" + access, expected))
            access_second = 'second.run()' if returns_instance else 'second().run()'
            programs.append((factory + "first=build('echo safe')\nsecond=build('echo safe')\nfirst.command=WRITER\n" + access_second, False))
        programs.append(("saved=[]\nfor command in [WRITER, 'echo safe']:\n    class C:\n        command=command\n        def run(self): os.system(self.command)\n    saved.append(C)\nsaved[0]().run()", True))
        for first, second, expected in ((writer, 'echo safe', True), ('echo safe', writer, False)):
            programs.append(("pending=" + repr([first, second])
                             + "\ndef decorate():\n    os.system(pending.pop(0))\n    return lambda cls: cls\n@decorate()\nclass C: pass", expected))
        for body, expected in programs:
            program = "import os\nWRITER=" + repr(writer) + "\n" + body
            with self.subTest(program=program):
                self.assertEqual(receipts.python_ownership(program), expected)

    def test_python_model_cache_does_not_confuse_transient_values(self):
        import builtins
        writer = "uv run /catalog/gh-plan.py --repo o/r claim 12"
        safe = "print('safe')"
        owning = "import os\nos.system(" + repr(writer) + ")"
        programs = (
            "def run(code): exec(code, {})\nrun(compile(" + repr(safe) + ", '<safe>', 'exec'))\nrun(compile(" + repr(owning) + ", '<writer>', 'exec'))",
            "import os\nfrom unittest.mock import patch\ndef record(*a): return 0\ndef run(context):\n    with context: os.system(" + repr(writer) + ")\nrun(patch('os.system', record))\nrun(patch('os.system', os.system))",
        )
        def colliding_repr(item):
            return builtins.repr(item) if isinstance(item, (str, int, float, bool, bytes, type(None))) else '<transient>'
        for program in programs:
            with self.subTest(program=program), patch.object(receipts, 'repr', colliding_repr, create=True):
                self.assertTrue(receipts.python_ownership(program))
                self.assertFalse(receipts.python_ownership(program.replace(writer, 'echo safe')))

    def test_python_patch_state_and_parsed_sources_preserve_cache_growth(self):
        for kind in ('patched_calls', 'embedded_source'):
            counts = []
            for repeats in (4, 16):
                program = ("from unittest.mock import patch\ndef leaf(): return 'safe'\ndef record(*a): return 0\nwith patch('os.system', record):\n" + "    leaf()\n" * repeats if kind == 'patched_calls' else "exec(\"def leaf(): return 'safe'\\nleaf()\", {})\n" * repeats)
                calls = []
                def profile(frame, event, _argument):
                    if event == 'call' and frame.f_code.co_filename == receipts.__file__ and frame.f_code.co_name == ('walk' if kind == 'patched_calls' else 'parse'):
                        calls.append(None)
                if kind == 'embedded_source':
                    with patch.object(receipts.ast, 'parse', wraps=receipts.ast.parse) as parse:
                        self.assertFalse(receipts.python_ownership(program))
                        counts.append(parse.call_count)
                else:
                    previous = sys.getprofile()
                    try:
                        sys.setprofile(profile)
                        self.assertFalse(receipts.python_ownership(program))
                    finally:
                        sys.setprofile(previous)
                    counts.append(len(calls))
            with self.subTest(kind=kind):
                self.assertLessEqual(counts[1], counts[0] * 2)

    def test_partial_python_fixture_edits_do_not_become_shell_writer_files(self):
        # Native apply_patch can contain several independent, indented hunks.
        # Concatenating these fragments is invalid Python, even when each edit
        # inserts only fixture text consumed by the test's own command reader.
        fixture = ('        rows = history("claude", {})\n'
                   '        rows[1]["message"]["content"][0]["input"] = {"command":\n'
                   '            \'echo "$(cat <<\\\'EOF\\\'; uv run gh-plan.py claim 1 &\\nbody\\nEOF\\n)"\'}\n'
                   '        self.assertTrue(receipts.ownership_state(rows, "claude")["pending"])\n'
                   '        subprocess.run(["rg", "gh-plan.py", "claim"])\n'
                   '        command = "uv run gh-plan.py --repo example/a claim 1"\n'
                   '        argv = ["python", "--version"]\n'
                   '        result = subprocess.run(argv, capture_output=True)')
        patch = ('*** Begin Patch\n*** Update File: /repo/test_sample.py\n@@\n'
                 '     def test_fixture(self):\n@@\n'
                 '                 self.assertTrue(True)\n' +
                 "\n".join("+" + line for line in fixture.splitlines()) + '\n*** End Patch')
        for harness in ("codex", "claude"):
            rows = history(harness, {})
            if harness == "codex":
                rows[1]["payload"].update(name="apply_patch", arguments=patch)
            else:
                rows[1]["message"]["content"][0].update(name="Edit", input={
                    "file_path": "/repo/test_sample.py", "new_string": fixture})
            execute = history(harness, {})[1:3]
            if harness == "codex":
                execute[0]["payload"].update(call_id="execute", arguments=json.dumps({"cmd": "uv run /repo/test_sample.py"}))
            else:
                execute[0]["message"]["content"][0].update(id="execute", input={"command": "uv run /repo/test_sample.py"})
            execute[1] = output({"exit_code": 0, "output": ""}, harness, "execute")
            rows[3:3] = execute
            with self.subTest(harness=harness, fixture=True):
                self.assertFalse(receipts.ownership_state(rows, harness)["indices"])
            # Invalid surrounding indentation still cannot hide an actual
            # process launch in a partial Python edit, including a split call.
            for launch in ('        subprocess.run(["uv", "run", "gh-plan.py", "claim", "1"])',
                           '        subprocess.run(["uv", "run", "gh-plan.py", "claim", "1",',
                           '                marker = True\n        command = "uv run gh-plan.py claim 1"\n        subprocess.run(command)',
                           '                marker = True\n        argv = ["uv", "run", "gh-plan.py", "claim", "1"]\n        subprocess.run(argv)',
                           '                marker = True\n        command = ("uv run gh-plan.py " "claim 1")\n        subprocess.run(command, shell=True)',
                           '                marker = True\n        argv = shlex.split("uv run gh-plan.py claim 1")\n        subprocess.run(argv)'):
                fragment = '    def run_claim():\n' + launch
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = ('*** Begin Patch\n*** Update File: /repo/test_sample.py\n@@\n'
                        '     def run_claim():\n@@\n' + '\n'.join('+' + line for line in launch.splitlines()) + '\n*** End Patch')
                else:
                    rows[1]["message"]["content"][0]["input"]["new_string"] = fragment
                with self.subTest(harness=harness, launch=launch):
                    self.assertTrue(receipts.ownership_state(rows, harness)["pending"])

        # An exact native removal is an erasure proof only at its own bound
        # terminal result. Discovery-only JS patches never supply that proof.
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 5'
        old = 'os.system(' + repr(writer) + ')'
        program = 'import os\n' + old + '\n'
        initial = '*** Begin Patch\n*** Add File: /repo/run.py\n+' + '\n+'.join(program.splitlines()) + '\n*** End Patch'
        removal = '*** Begin Patch\n*** Update File: /repo/run.py\n@@\n-' + old + '\n+print("safe")\n*** End Patch'
        def invoke(rows, harness, index, name, value):
            if harness == 'codex':
                rows[1 + 2 * index]['payload'].update(name=name, arguments=value if isinstance(value, str) else json.dumps(value))
            else:
                rows[1 + 2 * index]['message']['content'][0].update(name=name, input=value)
            rows[2 + 2 * index] = shell_result({'exit_code': 0, 'output': ''}, harness, 'tool-' + str(index))
        for harness in ('codex', 'claude'):
            write = ('apply_patch', initial) if harness == 'codex' else ('Write', {'file_path': '/repo/run.py', 'content': program})
            edit = ('apply_patch', removal) if harness == 'codex' else ('Edit', {'file_path': '/repo/run.py', 'old_string': old, 'new_string': 'print("safe")'})
            read = ('exec_command', {'cmd': 'python3 /repo/run.py'}) if harness == 'codex' else ('Bash', {'command': 'python3 /repo/run.py'})
            variants = ('complete', 'pending', 'running', 'unbound', 'unknown_base', 'unmatched',
                        'later_writer', 'earlier_execution', 'old_writer_pending', 'late_result', 'cached', 'detached', 'failed', 'unknown')
            if harness == 'codex':
                variants += ('native_text_failed', 'native_unknown',
                             'native_nested', 'native_unbound', 'native_cached', 'native_business', 'native_nested_status', 'native_text_quoted_failure', 'codemode_literal')
            for variant in variants:
                rows = history(harness, *({} for _ in range(4 if variant == 'later_writer' else 3)))
                invoke(rows, harness, 0, *write)
                invoke(rows, harness, 1, *edit)
                invoke(rows, harness, 2, *read)
                if variant.startswith('native_'):
                    rows[2] = native_result('tool-0')
                    rows[4] = native_result('tool-1')
                    if variant == 'native_text_quoted_failure':
                        rows[4]['payload']['output'] += json.dumps({'exit_code': 1})
                    elif variant == 'native_text_failed':
                        rows[4] = native_result('tool-1', status=1)
                    elif variant == 'native_unknown':
                        rows[4] = native_result('tool-1', status=None)
                    elif variant == 'native_nested':
                        rows[4]['payload']['output'] = json.dumps({'output': rows[4]['payload']['output']})
                    elif variant == 'native_unbound':
                        rows[4]['payload']['call_id'] = 'unrelated'
                    elif variant == 'native_cached':
                        cached = history(harness, {})[1:3]
                        cached[0]['payload'].update(call_id='cached-read', name='exec_command',
                                                  arguments=json.dumps({'cmd': 'cat saved-native-output.txt'}))
                        cached[1] = native_result('cached-read')
                        cached[1]['payload']['output'] += native_result('tool-1')['payload']['output']
                        rows[4:5] = cached
                    elif variant == 'native_nested_status':
                        rows[4]['payload']['output'] = {'output': json.dumps({'exit_code': 0, 'output': ''})}
                    elif variant == 'native_business':
                        rows[4]['payload']['output'] = {'operation': 'cached.report', 'metadata': {'exit_code': 0}}
                if variant == 'codemode_literal':
                    invoke(rows, harness, 1, 'functions.exec', 'text(await tools.apply_patch(' + json.dumps(removal) + '));')
                    rows[4]['payload']['output'] = [{'type': 'input_text', 'text': '{}'}]
                if variant == 'pending':
                    rows.pop(4)
                elif variant == 'running':
                    rows[4] = output({'session_id': 444, 'output': ''}, harness, 'tool-1')
                elif variant == 'unbound':
                    rows[4] = output({'exit_code': 0, 'output': ''}, harness, 'unrelated-tool')
                elif variant == 'failed':
                    rows[4] = output({'exit_code': 1, 'output': 'write failed'}, harness, 'tool-1')
                    if harness == 'claude':
                        rows[4]['message']['content'][0]['is_error'] = True
                elif variant == 'unknown':
                    rows[4] = output({'exit_code': 'unknown', 'output': ''}, harness, 'tool-1')
                    if harness == 'claude':
                        rows[4]['message']['content'][0]['is_error'] = 'unknown'
                elif variant == 'old_writer_pending':
                    rows.pop(2)
                elif variant == 'unknown_base':
                    unknown = ('functions.exec', 'const p=' + json.dumps(initial) + ';const ap=tools.apply_patch;await ap(p);') if harness == 'codex' else ('Edit', {'file_path': '/repo/run.py', 'new_string': program})
                    invoke(rows, harness, 0, *unknown)
                elif variant == 'unmatched':
                    unknown = ('apply_patch', removal.replace('-' + old, '-does_not_match()')) if harness == 'codex' else ('Edit', {'file_path': '/repo/run.py', 'old_string': 'does_not_match()', 'new_string': 'print("safe")'})
                    invoke(rows, harness, 1, *unknown)
                elif variant == 'later_writer':
                    invoke(rows, harness, 2, *write)
                    invoke(rows, harness, 3, *read)
                elif variant == 'earlier_execution':
                    rows[3:7] = rows[5:7] + rows[3:5]
                elif variant == 'late_result':
                    rows.append(rows.pop(4))
                elif variant == 'cached':
                    invoke(rows, harness, 1, 'exec_command' if harness == 'codex' else 'Bash', {'cmd' if harness == 'codex' else 'command': 'cat saved-result.json'})
                elif variant == 'detached':
                    unknown = ('functions.exec', 'const h=' + json.dumps(removal) + ';tools.apply_patch(h);') if harness == 'codex' else ('Bash', {'command': "apply_patch <<'PATCH' &\n" + removal + '\nPATCH'})
                    invoke(rows, harness, 1, *unknown)
                with self.subTest(harness=harness, native_removal=variant):
                    self.assertEqual(receipts.ownership_state(rows, harness)['pending'], variant not in {'complete', 'native_text_quoted_failure'})

            if harness == 'codex':
                # A copy carries its source's uncertainty; ordinary dormant
                # source precision must not recreate authority to erase it.
                rows = history(harness, {}, {}, {}, {}, {})
                invoke(rows, harness, 0, 'apply_patch', initial.replace('/repo/run.py', '/repo/template.py'))
                uncertain = ('*** Begin Patch\n*** Update File: /repo/template.py\n@@\n-'
                             + old + '\n+' + old + '\n*** End Patch')
                invoke(rows, harness, 1, 'functions.exec', 'const p=' + json.dumps(uncertain)
                       + ';const ap=tools.apply_patch;await ap(p);')
                invoke(rows, harness, 2, 'exec_command', {'cmd': 'cp template.py run.py'})
                invoke(rows, harness, 3, *edit)
                invoke(rows, harness, 4, *read)
                with self.subTest(harness=harness, native_removal_copied_uncertain_base=True):
                    self.assertTrue(receipts.ownership_state(rows, harness)['pending'])

            # A visible bound shell rewrite is the supported replacement
            # route when code-mode patch discovery cannot prove execution.
            rows = history(harness, {}, {}, {})
            invoke(rows, harness, 0, *write)
            replacement = "cat > /repo/run.py <<'PY'\nprint('safe')\nPY"
            invoke(rows, harness, 1, 'exec_command' if harness == 'codex' else 'Bash',
                   {'cmd' if harness == 'codex' else 'command': replacement})
            if harness == 'codex':
                rows[4] = native_result('tool-1')
            invoke(rows, harness, 2, *read)
            with self.subTest(harness=harness, native_removal_shell_replacement=True):
                self.assertFalse(receipts.ownership_state(rows, harness)['pending'])

            # Cross-history erasure additionally needs a native completion
            # timestamp; missing/untimed results cannot invent a settled edit.
            for result_mode in ('missing', 'untimed'):
                owner, reader = history(harness, {}, {}), history(harness, {})
                invoke(owner, harness, 0, *write)
                invoke(owner, harness, 1, *edit)
                invoke(reader, harness, 0, *read)
                if result_mode == 'missing':
                    owner.pop(4)
                for row in owner:
                    row.pop('timestamp', None)
                reader[1]['timestamp'] = reader[2]['timestamp'] = '2026-01-01T00:00:05Z'
                nodes = [{'harness': harness, 'records': rows} for rows in (owner, reader)]
                candidates = receipts.ownership_file_candidates(nodes)
                with self.subTest(harness=harness, native_removal_untimed_peer=result_mode):
                    self.assertTrue(receipts.ownership_state(reader, harness, file_candidates=candidates)['pending'])

            # A terminal edit cannot erase a later peer's owning contribution.
            peer_cases = ((None, False, '', 'complete'), (3, True, program, 'complete'), (4, True, program, 'complete'),
                          (1.5, True, program + 'os.system(' + repr(writer.replace('claim 5', 'claim 6')) + ')\n', 'complete'),
                          (1.5, False, program, 'complete'),
                          (1.5, True, program, 'pending'), (1.5, True, program, 'overlap'), (1.5, True, program, 'tied_end'), (1.5, True, program, 'unknown'),
                          (1.5, True, program, 'untimed'), (2, True, program, 'complete'),
                          (4.5, True, program, 'complete'), (0.5, False, program, 'complete'))
            for peer_at, pending, peer_program, peer_mode in peer_cases:
                owner = history(harness, {}, {})
                reader = history(harness, {})
                invoke(owner, harness, 0, *write)
                invoke(owner, harness, 1, *edit)
                invoke(reader, harness, 0, *read)
                for row, at in ((owner[1], 1), (owner[2], 1), (owner[3], 2), (owner[4], 4), (reader[1], 5), (reader[2], 5)):
                    row['timestamp'] = '2026-01-01T00:00:0' + str(at) + 'Z'
                peers = [owner]
                if peer_at is not None:
                    peer = history(harness, {})
                    peer_write = ('Write', {'file_path': '/repo/run.py', 'content': peer_program})
                    if harness == 'codex':
                        peer_write = ('apply_patch', '*** Begin Patch\n*** Add File: /repo/run.py\n+' + '\n+'.join(peer_program.splitlines()) + '\n*** End Patch')
                    invoke(peer, harness, 0, *peer_write)
                    peer[1]['timestamp'] = peer[2]['timestamp'] = '2026-01-01T00:00:0' + str(peer_at) + 'Z'
                    if peer_mode in {'overlap', 'tied_end'}:
                        peer[2]['timestamp'] = '2026-01-01T00:00:0' + ('3' if peer_mode == 'overlap' else '4') + 'Z'
                    if peer_mode == 'pending':
                        peer.pop(2)
                    elif peer_mode == 'unknown':
                        peer[2] = output({'exit_code': 'unknown', 'output': ''}, harness, 'tool-0')
                        peer[2]['timestamp'] = '2026-01-01T00:00:01.5Z'
                        if harness == 'claude':
                            peer[2]['message']['content'][0]['is_error'] = 'unknown'
                    elif peer_mode == 'untimed':
                        peer[1].pop('timestamp', None)
                        peer[2].pop('timestamp', None)
                    peers.append(peer)
                for order in (peers, list(reversed(peers))):
                    nodes = [{'harness': harness, 'records': rows} for rows in (*order, reader)]
                    candidates = receipts.ownership_file_candidates(nodes)
                    with self.subTest(harness=harness, native_removal_peer=peer_at, peer_program=peer_program, peer_mode=peer_mode, reverse=order != peers):
                        self.assertEqual(receipts.ownership_state(reader, harness, file_candidates=candidates)['pending'], pending)
                        owner_reader = json.loads(json.dumps(owner))
                        own_read = json.loads(json.dumps(reader[1:3]))
                        if harness == 'codex':
                            own_read[0]['payload']['call_id'] = own_read[1]['payload']['call_id'] = 'owner-read'
                        else:
                            own_read[0]['message']['content'][0]['id'] = 'owner-read'
                            own_read[1]['message']['content'][0]['tool_use_id'] = 'owner-read'
                        owner_reader[-(2 if harness == 'codex' else 1):-(2 if harness == 'codex' else 1)] = own_read
                        own_nodes = [{'harness': harness, 'records': rows} for rows in (owner_reader, *[rows for rows in order if rows is not owner])]
                        own_candidates = receipts.ownership_file_candidates(own_nodes)
                        self.assertEqual(receipts.ownership_state(owner_reader, harness, file_candidates=own_candidates)['pending'], pending)


            if harness == 'claude':
                for failure in (False, True):
                    rows = history(harness, {}, {}, {})
                    invoke(rows, harness, 0, *write)
                    invoke(rows, harness, 1, *edit)
                    invoke(rows, harness, 2, *read)
                    for index in (0, 1):
                        block = rows[2 + 2 * index]['message']['content'][0]
                        block['is_error'] = failure if index == 1 else False
                        block['content'] = json.dumps({'exit_code': 0 if failure and index == 1 else 1})
                    with self.subTest(harness=harness, native_write_edit_quoted_status=failure):
                        self.assertEqual(receipts.ownership_state(rows, harness)['pending'], failure)

            if harness == 'codex':
                for mode in ('success', 'failed', 'unknown', 'unbound', 'pending'):
                    rows = history(harness, {}, {}, {})
                    invoke(rows, harness, 0, 'exec_command', {'cmd': "cat > /repo/run.py <<'PY'\n" + program + 'PY'})
                    rows[2]['payload']['output'] = 'Process running with session ID 444'
                    invoke(rows, harness, 1, *edit)
                    invoke(rows, harness, 2, *read)
                    poll = {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'poll',
                            'name': 'write_stdin', 'arguments': json.dumps({'session_id': 444, 'chars': ''})}}
                    terminal = native_result('unrelated' if mode == 'unbound' else 'poll')
                    if mode == 'failed':
                        terminal = native_result(terminal['payload']['call_id'], status=1)
                        terminal['payload']['output'] += json.dumps({'exit_code': 0})
                    elif mode == 'unknown':
                        terminal = native_result(terminal['payload']['call_id'], status=None)
                    elif mode == 'pending':
                        terminal['payload']['output'] = 'Process running with session ID 444'
                    else:
                        terminal['payload']['output'] += json.dumps({'exit_code': 1})
                    rows[3:3] = [poll, terminal]
                    with self.subTest(harness=harness, native_polled_shell=mode):
                        self.assertEqual(receipts.ownership_state(rows, harness)['pending'], mode != 'success')

            if harness == 'codex':
                # An unrecognized direct transport cannot borrow a status
                # printed in stdout. Composed wrappers retain native metadata.
                for poll, composed in ((False, False), (True, False), (False, True), (True, True)):
                    for form in (('structured',) if composed else ('unrecognized', 'structured', 'forged_serial')):
                        for native_success in ((False, True) if composed else (False,)):
                            rows = history(harness, {}, {}, {})
                            invoke(rows, harness, 0, *write)
                            command = "printf 'print(1)\\n' > /repo/run.py"
                            invoke(rows, harness, 1, 'exec_command', {'cmd': command})
                            invoke(rows, harness, 2, *read)
                            writer_call = next(row['payload'] for row in rows
                                               if row.get('payload', {}).get('call_id') == 'tool-1'
                                               and row['payload'].get('type') == 'function_call')
                            writer_result = next(row for row in rows
                                                 if row.get('payload', {}).get('call_id') == 'tool-1'
                                                 and row['payload'].get('type') in {'function_call_output', 'custom_tool_call_output'})
                            result_id = 'poll' if poll else 'tool-1'
                            if composed:
                                writer_call.update(name='functions.exec', arguments=
                                                   'text(await tools.exec_command(' + json.dumps({'cmd': command}) + '));')
                                terminal = composed_result({'exit_code': 0 if native_success else 1,
                                                            'output': json.dumps({'exit_code': 1 if native_success else 0})}, result_id)
                            else:
                                terminal = (native_result(result_id) if form == 'unrecognized'
                                            else composed_result({'exit_code': 0}, result_id))
                                if form == 'unrecognized':
                                    terminal['payload']['output'] = ('unrecognized transport header\n'
                                                                     + json.dumps({'exit_code': 0}))
                            if poll:
                                writer_result['payload']['output'] = ([{'type': 'text', 'text': json.dumps({'session_id': 444})}]
                                                                       if composed else 'Process running with session ID 444')
                                poll_call = {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': result_id,
                                             'name': 'functions.exec' if composed else 'write_stdin',
                                             'arguments': ('text(await tools.write_stdin({session_id:444,chars:""}));' if composed else
                                                           json.dumps({'session_id': 444, 'chars': ''}))}}
                                rows[rows.index(writer_result) + 1:rows.index(writer_result) + 1] = [poll_call, terminal]
                            else:
                                rows[rows.index(writer_result)] = terminal
                            if form == 'forged_serial':
                                forged = persisted_serial_result(result_id)
                                part_id = forged['payload']['call_id']
                                for record in rows:
                                    payload = record.get('payload', {})
                                    if payload.get('call_id') == result_id:
                                        payload['call_id'] = part_id
                                        if receipts.calls_and_results(record, harness)[1]:
                                            record.clear()
                                            record.update(forged)
                            with self.subTest(harness=harness, unrecognized_outer_status=True, poll=poll,
                                              composed=composed, native_success=native_success, form=form):
                                self.assertEqual(receipts.ownership_state(rows, harness)['pending'],
                                                 not (composed and native_success))

            if harness == 'codex':
                # Native serial normalization preserves wrapper authority even
                # when another call failed or stdout prints a conflicting code.
                for deferred in (False, True):
                    rows = history(harness, {}, {}, {})
                    invoke(rows, harness, 0, *write)
                    command = "printf 'print(1)\\n' > /repo/run.py"
                    invoke(rows, harness, 1, 'functions.exec',
                           'text(await tools.exec_command({cmd:"cat docs.md"}));'
                           + 'text(await tools.exec_command(' + json.dumps({'cmd': command}) + '));')
                    invoke(rows, harness, 2, *read)
                    call_result = next(row for row in rows
                                       if row.get('payload', {}).get('call_id') == 'tool-1'
                                       and row['payload'].get('type') in {'function_call_output', 'custom_tool_call_output'})
                    blocks = [{'type': 'input_text', 'text': json.dumps(value)} for value in
                              ({'exit_code': 1, 'output': ''},
                               {'exit_code': 0, 'output': json.dumps({'exit_code': 1})})]
                    if deferred:
                        call_result['payload']['output'] = [
                            cell_header('serial-cell'), blocks[0]]
                        wait = {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'wait',
                                'name': 'functions.wait', 'arguments': json.dumps({'cell_id': 'serial-cell'})}}
                        terminal = {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'wait',
                                    'output': [cell_header(), blocks[1]]}}
                        rows[rows.index(call_result) + 1:rows.index(call_result) + 1] = [wait, terminal]
                    else:
                        call_result['payload']['output'] = blocks
                    with self.subTest(harness=harness, serial_native_status=True, deferred=deferred):
                        self.assertFalse(receipts.ownership_state(rows, harness)['pending'])

            if harness == 'codex':
                # Outer waits retain the launch's stdout/provenance contract in
                # both the local stream and raw cross-history candidate view.
                for count in (1, 2, 'process_poll'):
                    for stdout in (False, True):
                        for status in (0, 1):
                            rows = history(harness, {}, {}, {})
                            invoke(rows, harness, 0, *write)
                            command = "printf 'print(1)\\n' > /repo/run.py"
                            code = ('text(await tools.exec_command({cmd:"cat docs.md"}));' if count == 2 else '')
                            code += ('text((await tools.exec_command(' + json.dumps({'cmd': command}) + ')).output);'
                                     if stdout and count != 'process_poll'
                                     else 'text(await tools.exec_command(' + json.dumps({'cmd': command}) + '));')
                            invoke(rows, harness, 1, 'functions.exec', code)
                            invoke(rows, harness, 2, *read)
                            written = next(record for record in rows
                                           if 'tool-1' in receipts.calls_and_results(record, harness)[1])
                            blocks = [cell_header('outer-cell')]
                            if count == 2:
                                blocks.append({'type': 'input_text', 'text': json.dumps({'exit_code': 0, 'output': ''})})
                            written['payload']['output'] = blocks
                            poll = []
                            if count == 'process_poll':
                                # The waited cell is the poll, whose stdout
                                # projection can differ from the writer's.
                                written['payload']['output'] = composed_result({'session_id': 444}, 'tool-1')['payload']['output']
                                poll_code = ('text((await tools.write_stdin({session_id:444,chars:""})).output);'
                                             if stdout else 'text(await tools.write_stdin({session_id:444,chars:""}));')
                                poll = [{'payload': {'type': 'function_call', 'name': 'functions.exec', 'call_id': 'poll-cell',
                                                     'arguments': poll_code}},
                                        {'payload': {'type': 'function_call_output', 'call_id': 'poll-cell', 'output': blocks}}]
                            wait = {'payload': {'type': 'function_call', 'name': 'functions.wait', 'call_id': 'outer-wait',
                                    'arguments': json.dumps({'cell_id': 'outer-cell'})}}
                            terminal = composed_result({'exit_code': 0} if stdout else {'exit_code': status, 'output': ''}, 'outer-wait')
                            terminal['payload']['output'].insert(0, cell_header())
                            position = rows.index(written) + 1
                            rows[position:position] = [*poll, wait, terminal]
                            for index, record in enumerate(rows[1:], 1):
                                record['timestamp'] = f'2026-01-01T00:00:{index:02d}Z'
                            reader = [record for record in rows if any(identifier == 'tool-2'
                                      for identifier, _, _ in receipts.calls_and_results(record, harness)[0])
                                      or 'tool-2' in receipts.calls_and_results(record, harness)[1]]
                            writer_rows = [record for record in rows if all(record is not target for target in reader)]
                            peer_rows = [dict(rows[0]), *reader]
                            peers = [{'harness': harness, 'records': records} for records in (writer_rows, peer_rows)]
                            candidates = receipts.ownership_file_candidates(peers)
                            for scope, records, options in (('local', rows, {}), ('peer', peer_rows, {'file_candidates': candidates})):
                                with self.subTest(outer_cell=count, stdout=stdout, status=status, scope=scope):
                                    self.assertEqual(receipts.ownership_state(records, harness, **options)['pending'], stdout or status != 0)

            def transport(rows, index, mode='success'):
                identifier = 'tool-' + str(index)
                if harness == 'codex':
                    rows[2 + 2 * index] = native_result(identifier)
                    if mode != 'success':
                        rows[2 + 2 * index]['payload']['output'] = rows[2 + 2 * index]['payload']['output'].replace(
                            'Exit code: 0', 'Exit code: 1' if mode == 'failed' else 'Exit code: unknown')
                else:
                    rows[2 + 2 * index] = {'type': 'user', 'sessionId': SESSION, 'message': {'content': [
                        {'type': 'tool_result', 'tool_use_id': identifier, 'content': '',
                         'is_error': False if mode == 'success' else True if mode == 'failed' else 'unknown'}]}}

            def timing(rows, index, began, ended):
                for offset, at in ((1, began), (2, ended)):
                    rows[offset + 2 * index]['timestamp'] = '2026-01-01T00:00:0' + str(at) + 'Z'

            def shell(command):
                return ('exec_command', {'cmd': command}) if harness == 'codex' else ('Bash', {'command': command})

            def update(path, before, after):
                return (('apply_patch', '*** Begin Patch\n*** Update File: ' + path + '\n@@\n-'
                         + before + '\n+' + after + '\n*** End Patch') if harness == 'codex' else
                        ('Edit', {'file_path': path, 'old_string': before, 'new_string': after}))

            # A native shell write is settled by its outer transport, including
            # a peer's write. Failure/unknown results cannot certify erasure.
            for mode in ('success', 'failed', 'unknown', 'success_quoted_failure', 'failed_quoted_success'):
                for peer_base in (False, True):
                    base = history(harness, {})
                    invoke(base, harness, 0, *shell("cat > /repo/run.py <<'PY'\n" + program + 'PY'))
                    transport(base, 0, mode.split('_', 1)[0])
                    if 'quoted' in mode:
                        quoted = json.dumps({'exit_code': 1 if mode.startswith('success') else 0, 'output': ''})
                        if harness == 'codex':
                            base[2]['payload']['output'] += quoted
                        else:
                            base[2]['message']['content'][0]['content'] = quoted
                    timing(base, 0, 1, 1)
                    owner = history(harness, {}, {}, {})
                    invoke(owner, harness, 0, *write)
                    timing(owner, 0, 0.5, 0.5)
                    if not peer_base:
                        owner[1:3] = base[1:3]
                    invoke(owner, harness, 1, *edit)
                    timing(owner, 1, 2, 3)
                    invoke(owner, harness, 2, *read)
                    timing(owner, 2, 5, 5)
                    nodes = [{'harness': harness, 'records': rows} for rows in ((base, owner) if peer_base else (owner,))]
                    with self.subTest(harness=harness, native_shell_base=mode, peer_base=peer_base):
                        candidates = receipts.ownership_file_candidates(nodes)
                        self.assertEqual(receipts.ownership_state(owner, harness, file_candidates=candidates)['pending'], not mode.startswith('success'))

            # Failed and unknown full replacements are possible alternatives;
            # they cannot retire old bytes or make a later copy safe to erase.
            for mode in ('success', 'failed', 'unknown'):
                for peer_replace in (False, True):
                    for copied in (False, True):
                        owner = history(harness, {}, {}, {})
                        invoke(owner, harness, 0, *write)
                        timing(owner, 0, 1, 1)
                        safe = history(harness, {})
                        safe_write = (('apply_patch', initial.replace(program.strip().replace('\n', '\n+'), "print('safe')"))
                                      if harness == 'codex' else ('Write', {'file_path': '/repo/run.py', 'content': "print('safe')\n"}))
                        invoke(safe, harness, 0, *safe_write)
                        transport(safe, 0, mode)
                        timing(safe, 0, 2, 2)
                        if not peer_replace:
                            owner[3:5] = safe[1:3]
                            if harness == 'codex':
                                owner[3]['payload']['call_id'] = owner[4]['payload']['call_id'] = 'tool-1'
                            else:
                                owner[3]['message']['content'][0]['id'] = owner[4]['message']['content'][0]['tool_use_id'] = 'tool-1'
                        else:
                            owner[3:5] = []
                        if copied:
                            copy_rows = history(harness, {})
                            invoke(copy_rows, harness, 0, *shell('cp run.py copied.py'))
                            transport(copy_rows, 0)
                            timing(copy_rows, 0, 3, 3)
                            if harness == 'codex':
                                copy_rows[1]['payload']['call_id'] = copy_rows[2]['payload']['call_id'] = 'copy'
                            else:
                                copy_rows[1]['message']['content'][0]['id'] = copy_rows[2]['message']['content'][0]['tool_use_id'] = 'copy'
                            owner[-(4 if harness == 'codex' else 3):-(4 if harness == 'codex' else 3)] = copy_rows[1:3]
                        read_index = next(i for i, row in enumerate(owner) if i and (row.get('payload', {}).get('call_id') == 'tool-2' or
                                          any(block.get('id') == 'tool-2' for block in (row.get('message') or {}).get('content', ()) if isinstance(block, dict))))
                        command = 'python3 /repo/' + ('copied.py' if copied else 'run.py')
                        if harness == 'codex':
                            owner[read_index]['payload']['arguments'] = json.dumps({'cmd': command})
                        else:
                            owner[read_index]['message']['content'][0]['input'] = {'command': command}
                        owner[read_index]['timestamp'] = owner[read_index + 1]['timestamp'] = '2026-01-01T00:00:05Z'
                        nodes = [{'harness': harness, 'records': rows} for rows in ((owner, safe) if peer_replace else (owner,))]
                        with self.subTest(harness=harness, native_full_replacement=mode, peer=peer_replace, copied=copied):
                            candidates = receipts.ownership_file_candidates(nodes)
                            self.assertEqual(receipts.ownership_state(owner, harness, file_candidates=candidates)['pending'], mode != 'success')

            # Copy authority follows source readiness, including descendants and
            # transitive transfers. A settled harmless source remains erasable.
            for copy_command in ('cp template.py run.py', 'mv template.py run.py',
                                 'cp -r /repo/templates/. /repo/', 'cp template.py middle.py && cp middle.py run.py'):
                template_path = '/repo/templates/run.py' if 'templates/' in copy_command else '/repo/template.py'
                for mode in ('pending', 'late', 'tied', 'untimed', 'settled_safe', 'settled_writer', 'unknown', 'failed'):
                    for activation in (False, True):
                        template = ('import os\ndef f(): ' + old + "\nprint('a')\nprint('x')\n" if activation else
                                    "import os\nprint('a')\nprint('x')\n")
                        owner = history(harness, {}, {}, {})
                        first = (('apply_patch', '*** Begin Patch\n*** Add File: ' + template_path + '\n+' + '\n+'.join(template.splitlines()) + '\n*** End Patch')
                                 if harness == 'codex' else ('Write', {'file_path': template_path, 'content': template}))
                        invoke(owner, harness, 0, *first)
                        timing(owner, 0, 1, 1)
                        invoke(owner, harness, 1, *shell(copy_command))
                        transport(owner, 1)
                        timing(owner, 1, 2, 2.2)
                        invoke(owner, harness, 2, *update('/repo/run.py', "print('x')", "print('y')"))
                        timing(owner, 2, 3, 4)
                        peer = history(harness, {})
                        new = "print('safe')" if mode == 'settled_safe' else 'f()' if activation else old
                        invoke(peer, harness, 0, *update(template_path, "print('a')", new))
                        timing(peer, 0, 2 if mode == 'tied' else 1.5, 1.6 if mode in {'settled_safe', 'settled_writer', 'failed', 'unknown'} else 4.5)
                        if mode == 'pending':
                            peer.pop(2)
                        elif mode == 'untimed':
                            peer[1].pop('timestamp')
                            peer[2].pop('timestamp')
                        elif mode in {'unknown', 'failed'}:
                            transport(peer, 0, mode)
                            peer[2]['timestamp'] = '2026-01-01T00:00:01.6Z'
                        reader = history(harness, {})
                        invoke(reader, harness, 0, *read)
                        timing(reader, 0, 5, 5)
                        own_read = json.loads(json.dumps(reader[1:3]))
                        end_count = 2 if harness == 'codex' else 1
                        if harness == 'codex':
                            own_read[0]['payload']['call_id'] = own_read[1]['payload']['call_id'] = 'owner-read'
                        else:
                            own_read[0]['message']['content'][0]['id'] = 'owner-read'
                            own_read[1]['message']['content'][0]['tool_use_id'] = 'owner-read'
                        for reverse in (False, True):
                            peers = (peer, owner) if reverse else (owner, peer)
                            nodes = [{'harness': harness, 'records': rows} for rows in (*peers, reader)]
                            with self.subTest(harness=harness, native_copy=copy_command, mode=mode, activation=activation, reverse=reverse):
                                candidates = receipts.ownership_file_candidates(nodes)
                                self.assertEqual(receipts.ownership_state(reader, harness, file_candidates=candidates)['pending'], mode != 'settled_safe')
                                own_reader = json.loads(json.dumps(owner))
                                own_reader[-end_count:-end_count] = own_read
                                own_nodes = [{'harness': harness, 'records': rows} for rows in (own_reader, peer)]
                                own_candidates = receipts.ownership_file_candidates(own_nodes)
                                self.assertEqual(receipts.ownership_state(own_reader, harness, file_candidates=own_candidates)['pending'], mode != 'settled_safe')
                        if copy_command == 'cp template.py run.py' and mode in {'pending', 'late'}:
                            # Same native history with another call's update
                            # launched before cp and completing after it.
                            local = json.loads(json.dumps(owner))
                            launch = json.loads(json.dumps(peer[1]))
                            if harness == 'codex':
                                launch['payload']['call_id'] = 'parallel-update'
                            else:
                                launch['message']['content'][0]['id'] = 'parallel-update'
                            local[3:3] = [launch]
                            if mode == 'late':
                                terminal = json.loads(json.dumps(peer[2]))
                                if harness == 'codex':
                                    terminal['payload']['call_id'] = 'parallel-update'
                                else:
                                    terminal['message']['content'][0]['tool_use_id'] = 'parallel-update'
                                local[-end_count:-end_count] = [terminal]
                            local[-end_count:-end_count] = json.loads(json.dumps(own_read))
                            with self.subTest(harness=harness, native_copy_parallel=mode, activation=activation):
                                self.assertTrue(receipts.ownership_state(local, harness)['pending'])

            # Copy settlement cannot forget a failed/unknown destination
            # writer when the source itself is still unsettled.
            for destination_mode in ('failed', 'unknown'):
                owner, peer = history(harness, {}, {}, {}, {}, {}), history(harness, {})
                invoke(owner, harness, 0, *write)
                transport(owner, 0, destination_mode)
                timing(owner, 0, 0.5, 0.5)
                template = "import os\nprint('a')\nprint('x')\n"
                template_write = (('apply_patch', '*** Begin Patch\n*** Add File: /repo/template.py\n+' + '\n+'.join(template.splitlines()) + '\n*** End Patch')
                                  if harness == 'codex' else ('Write', {'file_path': '/repo/template.py', 'content': template}))
                invoke(owner, harness, 1, *template_write)
                timing(owner, 1, 1, 1)
                invoke(owner, harness, 2, *shell('cp template.py run.py'))
                transport(owner, 2)
                timing(owner, 2, 2, 2.2)
                invoke(owner, harness, 3, *update('/repo/run.py', "print('x')", "print('y')"))
                timing(owner, 3, 3, 4)
                invoke(owner, harness, 4, *read)
                timing(owner, 4, 5, 5)
                invoke(peer, harness, 0, *update('/repo/template.py', "print('a')", "print('safe')"))
                timing(peer, 0, 1.5, 4.5)
                peer.pop(2)
                with self.subTest(harness=harness, native_copy_uncertain_destination=destination_mode):
                    candidates = receipts.ownership_file_candidates([{'harness': harness, 'records': rows} for rows in (owner, peer)])
                    self.assertTrue(receipts.ownership_state(owner, harness, file_candidates=candidates)['pending'])

            # A copy completion cannot use bytes first written to its source
            # while that copy was in flight to erase the earlier copied bytes.
            owner, peer, reader = history(harness, {}, {}), history(harness, {}), history(harness, {})
            template_write = ((write[0], initial.replace('/repo/run.py', '/repo/template.py')) if harness == 'codex' else
                              ('Write', {'file_path': '/repo/template.py', 'content': program}))
            invoke(owner, harness, 0, *template_write)
            timing(owner, 0, 1, 1)
            invoke(owner, harness, 1, *shell('cp template.py run.py'))
            transport(owner, 1)
            timing(owner, 1, 2, 4)
            invoke(peer, harness, 0, *update('/repo/template.py', old, "print('safe')"))
            timing(peer, 0, 3, 3)
            invoke(reader, harness, 0, *read)
            timing(reader, 0, 5, 5)
            for order in ((owner, peer), (peer, owner)):
                nodes = [{'harness': harness, 'records': rows} for rows in (*order, reader)]
                with self.subTest(harness=harness, native_copy_source_changed_inflight=True, reverse=order[0] is peer):
                    candidates = receipts.ownership_file_candidates(nodes)
                    self.assertTrue(receipts.ownership_state(reader, harness, file_candidates=candidates)['pending'])

            # Within one shell call, visible writes and copies are sequential.
            rows = history(harness, {}, {}, {})
            invoke(rows, harness, 0, *shell("cat > /repo/template.py <<'PY' && cp template.py run.py\n" + program + 'PY'))
            transport(rows, 0)
            invoke(rows, harness, 1, *edit)
            invoke(rows, harness, 2, *read)
            with self.subTest(harness=harness, native_copy_same_shell=True):
                self.assertFalse(receipts.ownership_state(rows, harness)['pending'])

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
            for command in ('uv run gh-plan.py claim --help', 'uv run gh-plan.py claim 7 --help',
                            'uv run gh-plan.py claim --worker w --help', 'uv run gh-plan.py claim --worker --help',
                            'uv run gh-plan.py claim 7 --next-action "uv run gh-plan.py claim 1" --help',
                            'uv run gh-pr.py create --help', 'gh pr create --help',
                            'uv run gh-pr.py create --title --help',
                            'uv run gh-pr.py create --body-file --help',
                            'printf start; uv run gh-pr.py create --head --help; printf end'):
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

    def test_unrecognized_executors_cannot_hide_quoted_ownership_commands(self):
        helper = "uv run /catalog/gh-plan.py --repo example/a claim 1"
        for harness in ("codex", "claude"):
            for command in ("sudo -u me bash -c " + shlex.quote(helper),
                            "su deploy -c " + shlex.quote(helper),
                            "sudo su - deploy -c " + shlex.quote(helper),
                            "su -c " + shlex.quote(helper) + " deploy",
                            "su deploy --command=" + shlex.quote(helper),
                            "su -lc " + shlex.quote(helper) + " deploy",
                            "su deploy -c" + shlex.quote(helper),
                            "runuser -u deploy -- " + helper,
                            "sudo runuser -u deploy -- " + helper,
                            "runuser deploy -c " + shlex.quote(helper),
                            "sudo -iu me bash -c " + shlex.quote(helper),
                            "sudo -uME bash -c " + shlex.quote(helper),
                            "flock /tmp/claim-lock -c " + shlex.quote(helper),
                            "flock --command=" + shlex.quote(helper) + " /tmp/claim-lock",
                            "flock /tmp/claim-lock --command=" + shlex.quote(helper),
                            "ssh -p 22 host " + shlex.quote(helper),
                            "ssh -p22 host " + shlex.quote(helper),
                            "ssh -vp 22 host " + shlex.quote(helper),
                            '"$PY" gh-plan.py --repo example/a claim 1',
                            "zsh --emulate sh -c " + shlex.quote(helper)):
                rows = history(harness, {}, {**proof(), "harness": harness})
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                with self.subTest(harness=harness, command=command):
                    self.assertTrue(receipts.ownership_state(rows, harness)["indices"])
                    self.assertTrue(receipts.ownership_state(rows, harness)["pending"])
                    self.assertFalse(receipts.acceptance(rows, harness, SESSION)["clear"])
            handoff = "Handoff.\n" + helper + " --resume-from 10"
            for command in ("printf '%s' " + shlex.quote(helper), "rg " + shlex.quote(helper) + " source.py",
                            "git grep " + shlex.quote(helper),
                            "uv run gh-plan.py search " + shlex.quote(helper),
                            "gh issue comment 1 --body " + shlex.quote(handoff),
                            "uv run /catalog/gh-plan.py --repo example/a update-section 1 Status --body-file - <<'EOF'\n" + handoff + "\nEOF",
                            "GH_PAGER=cat gh issue comment 1 --body-file - <<'EOF'\n" + handoff + "\nEOF",
                            "timeout 60 gh issue comment 1 --body-file - <<'EOF'\n" + handoff + "\nEOF",
                            "command gh issue comment 1 --body-file - <<'EOF'\n" + handoff + "\nEOF",
                            "cat > note.md <<'EOF' && uv run gh-plan.py update-section 1 S --body-file note.md\n" + handoff + "\nEOF",
                            "git commit -m " + shlex.quote(handoff),
                            "gh pr edit 1 --body " + shlex.quote(handoff),
                            "jq --arg command " + shlex.quote(handoff) + " .",
                            "tee " + shlex.quote(handoff),
                            "gh api repos/example/a -f " + shlex.quote("note=" + handoff),
                            "git commit -m gh-plan.py claim",
                            "uv pip show gh-plan.py claim",
                            "sudo -u me gh issue comment 1 --body " + shlex.quote(handoff),
                            "su deploy -c " + shlex.quote("echo " + shlex.quote(handoff)),
                            "sudo su - deploy --command=" + shlex.quote("printf '%s' " + shlex.quote(handoff)),
                            "su -s /bin/sh deploy -c " + shlex.quote("gh issue comment 1 --body " + shlex.quote(handoff)),
                            "runuser -u deploy -- printf '%s' " + shlex.quote(handoff),
                            "runuser -u deploy -- gh issue comment 1 --body " + shlex.quote(handoff),
                            "sudo -p " + shlex.quote(handoff) + " -u me printf x",
                            "flock /tmp/claim-lock gh pr edit 1 --body " + shlex.quote(handoff),
                            "sudo -u me bash -c " + shlex.quote("printf '%s' " + shlex.quote(handoff)),
                            "ssh -p 22 host " + shlex.quote(shlex.join(["gh", "issue", "comment", "1", "--body", handoff])),
                            "zsh --emulate sh -c " + shlex.quote("printf '%s' " + shlex.quote(helper)),
                            "python3 -c " + shlex.quote("fixture=" + repr(helper) + "; print(fixture)")):
                rows = history(harness, {})
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                with self.subTest(harness=harness, read=command):
                    self.assertFalse(receipts.ownership_state(rows, harness)["indices"])

    def test_helper_creation_parse_errors_are_no_write_without_changing_native_gh(self):
        parser = receipts.helper_parser("gh-pr.py")
        parsing_error = receipts.sys.modules["github_api"].ArgumentParsingError
        for arguments in (["--title", "--help"], ["--body-file", "--help"],
                          ["--head", "--help"], ["--title"], ["--draft=unexpected"]):
            with self.subTest(arguments=arguments), self.assertRaises(parsing_error):
                parser.parse_known_args(["create", *arguments])
            for harness in ("codex", "claude"):
                with self.subTest(harness=harness, arguments=arguments):
                    rows = history(harness, {})
                    command = "printf start; " + shlex.join(["uv", "run", "gh-pr.py", "create", *arguments]) + "; printf end"
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertFalse(receipts.ownership_state(rows, harness)["indices"])
                    self.assertFalse(receipts.created_pr_targets(rows, harness))
                    self.assertTrue(receipts.acceptance(rows, harness, SESSION)["clear"])
        # This is a literal option value, so the helper can still create a PR.
        self.assertTrue(receipts.ownership_command(["gh-pr.py", "create", "--title=--help"]))
        # Native gh uses a different option parser. Its help-shaped value is
        # deliberately not proof that a write was avoided.
        self.assertTrue(receipts.ownership_command(["gh", "pr", "create", "--title", "--help"]))

    def test_literal_helpers_with_variable_actions_cannot_become_known_reads(self):
        for harness in ("codex", "claude"):
            for helper, action, receipt, tail in (
                ("gh-plan.py", "claim", claim(),
                 "1 --worker fixture --session " + SESSION + " --branch work/fixture --next-action inspect"),
                ("gh-pr.py", "create", {"operation": "github.pr.create", "ok": True},
                 "--head work/fixture --title fixture --body-file /fixture/body.md"),
            ):
                for variable in ("$ACTION", "${ACTION}"):
                    command = ("ACTION=" + action + "; uv run " + helper + ' --repo example/a "'
                               + variable + '" ' + tail)
                    rows = history(harness, receipt, {**proof(), "harness": harness})
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    rows[2] = output({"exit_code": 0, "output": json.dumps(receipt)}, harness, "tool-0")
                    with self.subTest(harness=harness, helper=helper, variable=variable):
                        self.assertTrue(receipts.ownership_state(rows, harness)["indices"])
                        self.assertTrue(receipts.ownership_state(rows, harness)["pending"])
                        self.assertFalse(receipts.acceptance(rows, harness, SESSION)["clear"])
                rows = history(harness, {})
                command = "uv run " + helper + " --repo example/a invalid-literal-command"
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                self.assertFalse(receipts.ownership_state(rows, harness)["indices"])

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
                        blocks.insert(0, cell_header())
                        rows[2] = {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "tool-0", "output": blocks}}
                        self.assertEqual(receipts.ownership_state(rows, "codex")["pending"], state != "finished")

    def test_visible_serial_poll_results_bind_only_their_own_native_wrapper(self):
        self.check_visible_serial_poll_results(stdout=False)
        self.check_visible_serial_poll_results(stdout=True)
        rows = history("codex", claim())
        # A serial-looking unbound output cannot remove the real launch.
        rows[2] = persisted_serial_result("tool-0")
        self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])

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

    def test_shell_file_provenance_stays_with_its_own_command_and_body(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12 --resume-from 2'
        commands = (
            ("gh issue comment 1 --body-file - <<'A'\n" + writer + "\nA\nprintf 'echo ok\\n' > check.sh", False),
            ("cat > handoff.md <<'A'\n" + writer + "\nA\ncat > check.sh <<'B'\necho ok\nB", False),
            ("cat > check.sh <<'B'\necho ok\nB\ncat > handoff.md <<'A'\n" + writer + "\nA", False),
            ("cat > check.sh <<'B'\necho ok\nB\ngh issue comment 1 --body-file - <<'A'\n" + writer + "\nA", False),
            ("cat > handoff.md <<'A'\necho ok\nA\nprintf '%s%s\\n' 'echo ' '" + writer + "' > check.sh", False),
            ("cat > handoff.md <<'A'\necho ok\nA\necho echo '" + writer + "' > check.sh", False),
            ("printf 'echo ok\\n' > check.sh <<'A'\n" + writer + "\nA", False),
            ("cat > check.sh <<'A' <<'B'\n" + writer + "\nA\necho ok\nB", False),
            ("cat > check.sh > handoff.md <<'A'\n" + writer + "\nA", False),
            ("cat > handoff.md <<'A'\necho ok\nA\ncat > check.sh <<'B'\n" + writer + "\nB", True),
            ("cat > handoff.md <<'A'\necho ok\nA\nprintf '%s\\n' '" + writer + "' > check.sh", True),
            ("printf '%s\\n' '" + writer + "' > check.sh <<'A'\necho ok\nA", True),
            ("cat <<'A' | cat > check.sh\n" + writer + "\nA", True),
            ("cat <<'A' | cat > check.sh <<'B'\n" + writer + "\nA\necho ok\nB", False),
            ("echo -e '#!/bin/sh\\n" + writer + "' > check.sh", True),
            ("echo -n '" + writer + "' > check.sh", True),
            ("echo -en '#!/bin/sh\\n" + writer + "' > check.sh", True),
            ("echo -e '\\x75\\0166" + writer[2:] + "' > check.sh", True),
            ("printf -- '%s\\n' '" + writer + "' > check.sh", True),
            ("printf '%b' '#!/bin/sh\\n" + writer + "' > check.sh", True),
            ("printf '\\165\\166" + writer[2:] + "\\n' > check.sh", True),
            ("printf '\\x75\\x76" + writer[2:] + "\\n' > check.sh", True),
            ("printf '%b' '\\0165\\0166" + writer[2:] + "\\n' > check.sh", True),
            ("printf '%b' '\\165\\166" + writer[2:] + "\\n' > check.sh", True),
            ("printf '%b\\n%s' 'echo ok\\n' '" + writer + "' > check.sh", True),
            ("printf '%q ' " + writer + " > check.sh", True),
            ("printf '%q ' '" + writer + "' > check.sh", False),
            ("printf '%s %q\\n' echo '" + writer + "' > check.sh", False),
            ("echo -e 'echo " + writer + "' > check.sh", False),
            ("echo -n echo '" + writer + "' > check.sh", False),
            ("echo -E echo '" + writer + "' > check.sh", False),
            ("echo -- '" + writer + "' > check.sh", False),
            ("echo -unknown '" + writer + "' > check.sh", False),
            ("printf -- '%s%s\\n' 'echo ' '" + writer + "' > check.sh", False),
            ("printf '%b%s\\n' 'echo ' '" + writer + "' > check.sh", False),
            ("printf '%b%s' 'echo ok\\c' '" + writer + "' > check.sh", False),
            ("echo -e 'echo ok\\c\\n" + writer + "' > check.sh", False),
            ("printf '\\x25s' '" + writer + "' > check.sh", False),
        )
        for harness in ("codex", "claude"):
            for command, pending in commands:
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, claim(), claim())
                    for index, text in ((1, command), (3, "bash check.sh")):
                        if harness == "codex":
                            rows[index]["payload"]["arguments"] = json.dumps({"cmd": text})
                        else:
                            rows[index]["message"]["content"][0]["input"] = {"command": text}
                    rows[2], rows[4] = output({}, harness, "tool-0"), output({}, harness, "tool-1")
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)

    def test_nested_script_ownership_survives_the_shell_parser_boundary(self):
        writer = 'uv run /catalog/gh-plan.py --repo example/a claim 1'
        nested = "python3 - <<'PY'\nimport os; os.system(" + repr(writer) + ")\nPY\n"
        shell_nested = "cat <<'CODE' | bash\n" + writer + "\nCODE\n"
        data = "cat <<'DATA'\n" + nested + "DATA\n"
        for harness in ('codex', 'claude'):
            for content, expected in ((nested, True), (shell_nested, True), (data, False)):
                with self.subTest(harness=harness, content=content):
                    rows = history(harness, {}, {})
                    if harness == 'codex':
                        patch = '*** Begin Patch\n*** Add File: /repo/run.sh\n' + '\n'.join('+' + line for line in content.splitlines()) + '\n*** End Patch'
                        rows[1]['payload'].update(name='apply_patch', arguments=patch)
                        rows[3]['payload']['arguments'] = json.dumps({'cmd': 'bash /repo/run.sh'})
                    else:
                        rows[1]['message']['content'][0].update(name='Write', input={'file_path': '/repo/run.sh', 'content': content})
                        rows[3]['message']['content'][0]['input'] = {'command': 'bash /repo/run.sh'}
                    rows[2], rows[4] = output({}, harness, 'tool-0'), output({}, harness, 'tool-1')
                    self.assertEqual(receipts.ownership_state(rows, harness)['pending'], expected)
            for command in ("cat > run.sh <<'OUT'\n" + nested + "OUT\nbash run.sh",
                            "python3 - <<'OUT'\nimport subprocess; subprocess.run(" + repr(shell_nested) + ", shell=True)\nOUT"):
                rows = history(harness, {})
                if harness == 'codex': rows[1]['payload']['arguments'] = json.dumps({'cmd': command})
                else: rows[1]['message']['content'][0]['input'] = {'command': command}
                rows[2] = output({}, harness, 'tool-0')
                with self.subTest(harness=harness, command=command):
                    self.assertTrue(receipts.ownership_state(rows, harness)['pending'])

    def test_executable_wrappers_cannot_turn_hidden_ownership_into_no_write(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12 > claim.json'
        for harness in ("codex", "claude"):
            for command in ("bash <<'EOF'\n" + writer + "\nEOF",
                            "'bash' <<'EOF'\n" + writer + "\nEOF",
                            "sh -s <<'EOF'\n" + writer + "\nEOF",
                            "cat <<'EOF' | bash\n" + writer + "\nEOF",
                            "cat > run-claim.sh <<'EOF'\n" + writer + "\nEOF\nbash run-claim.sh",
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

    def test_node_launch_arguments_follow_call_sites_and_current_bindings(self):
        writer = 'uv run /catalog/gh-plan.py --repo example/a claim 1'
        command, executable = json.dumps(writer), json.dumps('/catalog/gh-plan.py')
        args = json.dumps(['--repo', 'example/a', 'claim', '1'])
        uv_args = json.dumps(['run', '/catalog/gh-plan.py', '--repo', 'example/a', 'claim', '1'])
        thrower = "const maybeThrow=()=>{if(process.env.FAIL)throw Error('stop')};"
        executing = (
            "const {execSync}=require('child_process'); const cmd=" + command + "; execSync(`${cmd} --json`)",
            "const {spawnSync}=require('child_process'); const helper=" + executable + "; spawnSync(helper," + args + ")",
            "const {spawnSync}=require('child_process'); const args=" + uv_args + "; spawnSync('uv',args)",
            "const {execSync}=require('child_process'); let cmd='echo done'; cmd=" + command + "; execSync(cmd)",
            "const {execFileSync}=require('child_process'); const helper=" + executable + "; const args=" + args + "; execFileSync(helper,args)",
            "const cp=require('node:child_process'); let args=[]; args=" + uv_args + "; cp.spawn('uv',args)",
            "const {spawnSync:launch}=require('child_process'); const helper='uv'; const args=" + uv_args + "; launch(helper,args)",
            "const {execSync}=require('child_process'); const prefix='uv run'; const helper=" + executable + "; const n=1; execSync(`${prefix} ${helper} --repo example/a claim ${n}`)",
            "const {execSync}=require('child_process'); execSync(process.env.CMD || " + command + ")",
            "const {execSync}=require('child_process'); let cmd='echo done'; cmd=process.env.CMD || " + command + "; execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=''; cmd+=" + command + "; execSync(cmd)",
            "const {execSync}=require('child_process'); const cmd=process.env.CMD || " + command + "; execSync(`${cmd} --json`)",
            "const {spawnSync}=require('child_process'); const args=process.env.ARGS || " + uv_args + "; spawnSync('uv',args)",
            "const {spawnSync}=require('child_process'); const args=process.env.ARGS || ['pr','create']; spawnSync('gh',args)",
            "const {spawnSync}=require('child_process'); spawnSync(" + command + ", {shell:true,stdio:'inherit'})",
            "const {spawn}=require('child_process'); const cmd=" + command + "; const options={shell:true}; spawn(cmd,options)",
            "const {execFileSync}=require('child_process'); execFileSync(" + command + ", [], {shell:true})",
            "const {spawnSync}=require('child_process'); spawnSync('gh pr create --fill',{shell:true})",
            "const {spawnSync}=require('child_process'); const options={shell:true}; spawnSync(" + command + ",{shell:false,...options})",
            "const {spawnSync}=require('child_process'); const key='shell'; spawnSync(" + command + ",{[key]:true})",
            "const {execSync}=require('child_process'); let cmd=" + command + "; if(process.env.DRY) cmd='echo skip'; execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; if(process.env.DRY) {cmd='echo skip';} execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd='echo skip'; if(process.env.GO) cmd=" + command + "; execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd='echo skip'; if(process.env.DRY) {cmd='echo other';} else {cmd=" + command + ";} execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; if(process.env.DRY) if(process.env.OTHER) cmd='echo skip'; execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; process.env.DRY && (cmd='echo skip'); execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; cmd=process.env.DRY?'echo skip':cmd; execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; while(process.env.DRY) {cmd='echo skip';} execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; function neverCalled(){cmd='echo skip';} execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; const neverCalled=()=>{cmd='echo skip';}; execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; const neverCalled=()=>cmd='echo skip'; execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd='echo skip'; function setCommand(){cmd=" + command + ";} setCommand(); execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; function readCommand(){cmd='echo skip';} const callback=readCommand; execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; switch(process.env.MODE){case 'dry':cmd='echo skip';break;} execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; switch(process.env.MODE){} execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd='echo skip'; switch(process.env.MODE){case 'live':cmd=" + command + ";break;case 'dry':cmd='echo dry';break;default:cmd='echo other';} execSync(cmd)",
            "const {execSync}=require('child_process');" + thrower + "let cmd=" + command + ";try{maybeThrow();cmd='echo skip';}catch(error){}execSync(cmd)",
            "const {execSync}=require('child_process');" + thrower + "let cmd='echo skip';try{cmd=" + command + ";maybeThrow();}catch(error){cmd='echo other';}execSync(cmd)",
            "const {execSync}=require('child_process');" + thrower + "let cmd=" + command + ";try{maybeThrow();cmd='echo skip';}catch{}finally{if(process.env.DRY)cmd='echo other';}execSync(cmd)",
            "const {execSync}=require('child_process');" + thrower + "let cmd=" + command + ";try{maybeThrow();cmd='echo skip';}finally{execSync(cmd)}",
        )
        data = (
            "const {execSync}=require('child_process'); const fixture=" + command + "; const cmd='echo done'; execSync(`${cmd} --json`)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; cmd='echo done'; execSync(cmd)",
            "const {execSync}=require('child_process'); const fixture=" + command + "; execSync(process.env.READ_COMMAND)",
            "const {execSync}=require('child_process'); let cmd='echo '; cmd+=" + command + "; execSync(cmd)",
            "const {spawnSync}=require('child_process'); const fixture=" + command + "; const helper='printf'; const args=['%s',fixture]; spawnSync(helper,args)",
            "const {spawnSync}=require('child_process'); const args=process.env.ARGS || [" + command + "]; spawnSync('printf',args)",
            "const {execFileSync}=require('child_process'); const fixture=" + command + "; execFileSync('cat',[fixture])",
            "const {spawnSync}=require('child_process'); const fixture=" + command + "; spawnSync('gh',['issue','comment','1','--body',fixture])",
            "const {execSync}=require('child_process'); const fixture=" + command + "; execSync(`printf '%s' '${fixture}'`)",
            "const cp=require('child_process'); let args=" + uv_args + "; args=[]; cp.spawnSync('printf',args)",
            "const fixture=" + command + "; console.log(`${fixture}`)",
            "const {execSync}=require('child_process'); const fixture=" + command + "; const pattern=/claim/; pattern.exec(fixture)",
            "const {spawnSync}=require('child_process'); spawnSync(" + command + ",{shell:false})",
            "const {spawn}=require('child_process'); const options={shell:false}; spawn(" + command + ",options)",
            "const {execFileSync}=require('child_process'); execFileSync(" + command + ",[],{shell:false})",
            "const {spawnSync}=require('child_process'); const fixture=" + command + "; spawnSync('printf',{shell:true})",
            "const {spawnSync}=require('child_process'); const options={shell:true}; spawnSync(" + command + ",{...options,shell:false})",
            "const {execSync}=require('child_process'); let cmd=" + command + "; if(process.env.DRY) cmd='echo first'; else cmd='echo second'; execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + "; if(process.env.DRY) {cmd='echo first';} else {cmd='echo second';} execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd='echo skip'; if(false) cmd=" + command + "; execSync(cmd)",
            "const {execSync}=require('child_process'); const cmd='echo skip'; function neverCalled(){cmd=" + command + ";} execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd='echo skip'; const neverCalled=()=>{cmd=" + command + ";}; execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd='echo skip'; const neverCalled=()=>cmd=" + command + "; execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + ";switch(process.env.MODE){case 'dry':cmd='echo dry';break;default:cmd='echo other';}execSync(cmd)",
            "const {execSync}=require('child_process'); let cmd=" + command + ";switch(process.env.MODE){case 'dry':cmd='echo dry';default:cmd='echo other';}execSync(cmd)",
            "const {execSync}=require('child_process');" + thrower + "let cmd=" + command + ";try{maybeThrow();cmd='echo first';}catch{cmd='echo second';}execSync(cmd)",
            "const {execSync}=require('child_process');" + thrower + "let cmd=" + command + ";try{maybeThrow();}catch{}finally{cmd='echo done';}execSync(cmd)",
            "const {execSync}=require('child_process');" + thrower + "let cmd=" + command + ";try{cmd='echo done';maybeThrow();}catch{}execSync(cmd)",
            "const {execSync}=require('child_process');" + thrower + "let cmd='echo skip';try{maybeThrow();cmd=" + command + ";}catch{execSync(cmd)}",
        )
        for harness in ('codex', 'claude'):
            for expected, bodies in ((True, executing), (False, data)):
                for body in bodies:
                    with self.subTest(harness=harness, expected=expected, body=body):
                        rows = history(harness, {})
                        shell = "node <<'EOF'\n" + body + "\nEOF"
                        if harness == 'codex':
                            rows[1]['payload']['arguments'] = json.dumps({'cmd': shell})
                        else:
                            rows[1]['message']['content'][0]['input'] = {'command': shell}
                        rows[2] = output({'exit_code': 0, 'output': ''}, harness, 'tool-0')
                        self.assertEqual(bool(receipts.ownership_state(rows, harness)['indices']), expected)
                        self.assertEqual(receipts.ownership_state(rows, harness)['pending'], expected)
                        self.assertEqual(session_record.summarize(rows, harness)['safe_verdict'], not expected)

    def test_heredoc_ownership_follows_stdin_consumers_not_header_words(self):
        writer = 'uv run /catalog/gh-plan.py --repo example/a claim 1'
        python_writer = 'import os\nos.system(' + repr(writer) + ')'
        node_writer = 'require("child_process").execSync(' + json.dumps(writer) + ')'
        executing = (
            ("{ bash; } <<'EOF'", writer),
            ("(bash) <<'EOF'", writer),
            ("cat <<'EOF' | (cd /repo && bash)", writer),
            ("while read -r l; do eval \"$l\"; done <<'EOF'", writer),
            ("sudo bash <<'EOF'", writer),
            ("sudo -u deploy sh -s <<'EOF'", writer),
            ("doas sh -s <<'EOF'", writer),
            ("flock /tmp/l bash <<'EOF'", writer),
            ("ssh host bash -s <<'EOF'", writer),
            ("ssh host <<'EOF'", writer),
            ("uv run - <<'EOF'", python_writer),
            ("node <<'EOF'", "const {execSync} = require('child_process'); const cmd = " + json.dumps(writer) + "; execSync(cmd)"),
            ("node <<'EOF'", "const {execSync} = require('child_process'); const n = 1; execSync(`" + writer.rsplit(" ", 1)[0] + " ${n}`)"),
            ("env FLAG=value timeout 60 bash -s -- arg <<'EOF'", writer),
            ("cat <<'EOF' | env FLAG=value bash -s", writer),
            ("cat <<'EOF' | cat | sh", writer),
            ("cat 2> errors.log <<'EOF' | sh", writer),
            ("bash -s 0<<'EOF'", writer),
            ("source /dev/stdin <<'EOF'", writer),
            (". /dev/stdin <<'EOF'", writer),
            ("bash /dev/stdin <<'EOF'", writer),
            ("bash 2>&1 <<'EOF'", writer),
            ("bash <<'EOF' <&0", writer),
            ("bash 3<<'EOF' <&3", writer),
            ("cat <<'EOF' | bash 2>&1", writer),
            ("python3 <<'EOF'", python_writer),
            ("python3 /dev/stdin <<'EOF'", python_writer),
            ("python3 2>&1 <<'EOF'", python_writer),
            ("uv run python3 -I -W ignore - <<'EOF'", python_writer),
            ("node --input-type=commonjs <<'EOF'", node_writer),
            ("node - <<'EOF'", node_writer),
            ("node >&2 <<'EOF'", node_writer),
            ("node <<'EOF'", "const cp = require('node:child_process'); cp.execSync(" + json.dumps(writer) + ")"),
            ("node --input-type=module <<'EOF'", "import {execSync as run} from 'node:child_process'; run(" + json.dumps(writer) + ")"),
            ("\"$INTERPRETER\" <<'EOF'", writer),
        )
        data = (
            ("uv run - <<'EOF'", "fixture = " + repr(writer) + "; print(fixture)"),
            ("sudo gh issue comment 1 --body-file - <<'EOF'", writer),
            ("flock /tmp/l gh issue comment 1 --body-file - <<'EOF'", writer),
            ("ssh host cat <<'EOF'", writer),
            ("{ gh issue comment 1 --body-file -; } <<'EOF'", writer),
            ("(gh issue comment 1 --body-file -) <<'EOF'", writer),
            ("cat <<'EOF' | (cd /repo && gh issue comment 1 --body-file -)", writer),
            ("node <<'EOF'", "const {execSync} = require('child_process'); const fixture = " + json.dumps(writer) + "; const cmd = 'echo done'; execSync(cmd)"),
            ("bash script.sh <<'EOF'", writer),
            ("bash -c 'printf done' <<'EOF'", writer),
            ("bash -o errexit script.sh <<'EOF'", writer),
            ("python3 script.py <<'EOF'", python_writer),
            ("python3 -c 'print(1)' <<'EOF'", python_writer),
            ("python3 -m json <<'EOF'", python_writer),
            ("node script.js <<'EOF'", node_writer),
            ("node -e 'console.log(1)' <<'EOF'", node_writer),
            ("node <<'EOF'", "const fixture = " + json.dumps(writer) + "; console.log(fixture)"),
            ("node <<'EOF'", "// " + node_writer + "\nconsole.log('done')"),
            ("node <<'EOF'", "const pattern = /claim/; pattern.exec(" + json.dumps(writer) + ")"),
            ("node <<'EOF'", "const fixture = \"const pattern = require('child_process')\"; const pattern = /claim/; pattern.exec(" + json.dumps(writer) + ")"),
            ("cat <<'EOF' && printf ok | bash", writer),
            ("cat <<'EOF'; printf ok | python3", python_writer),
            ("cat <<'EOF' | bash script.sh", writer),
            ("cat <<'EOF' | bash -c 'printf done'", writer),
            ("cat > note.md <<'EOF' | bash", writer),
            ("cat >2<<'EOF' | bash", writer),
            ("bash -s 3<<'EOF'", writer),
            ("eval <<'EOF'", writer),
            ("source setup.sh <<'EOF'", writer),
            ("printf x <<'EOF' | bash", writer),
            ("echo done <<'EOF' | bash", writer),
            ("cat input.txt <<'EOF' | bash", writer),
        )
        for harness in ("codex", "claude"):
            for expected, cases in ((True, executing), (False, data)):
                for header, body in cases:
                    command = header + "\n" + body + "\nEOF"
                    rows = history(harness, {})
                    if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                    rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                    with self.subTest(harness=harness, command=command):
                        self.assertEqual(bool(receipts.ownership_state(rows, harness)["indices"]), expected)
                        self.assertEqual(receipts.ownership_state(rows, harness)["pending"], expected)

    def test_stdin_forms_share_wrapper_normalization_and_consumer_classification(self):
        writer = 'uv run /catalog/gh-plan.py --repo example/a claim 1'
        python_writer = 'import os; os.system(' + repr(writer) + ')'
        node_writer = 'require("child_process").execSync(' + json.dumps(writer) + ')'
        commands = [
            ("python3 <<'EOF'\nimport subprocess; subprocess.run(['gh','pr','create'])\nEOF", True),
            ("python3 <<< " + shlex.quote("import subprocess; subprocess.run(['gh','pr','create'])"), True),
            ("python3 <<< " + shlex.quote("fixture=['gh','pr','create']; print(fixture)"), False),
            ("UV_CACHE_DIR=/cache uv run - <<'EOF'\n" + python_writer + "\nEOF", True),
            ("env UV_CACHE_DIR=/cache timeout 60 uv run - <<'EOF'\n" + python_writer + "\nEOF", True),
            ("env UV_CACHE_DIR=/cache uv run - <<'EOF'\nfixture=" + repr(writer) + "; print(fixture)\nEOF", False),
            ("uv run --unknown-reader python3 - <<'EOF'\n" + python_writer + "\nEOF", True),
        ]
        for header in ("sudo -s", "sudo -i", "sudo --shell", "doas -s", "sudo su - deploy", "su - deploy",
                       "sudo su - deploy -c bash", "su deploy -c 'sh -s'", "runuser -u deploy -- bash",
                       "runuser deploy", "runuser deploy -c bash",
                       "sudo su - deploy -c 'cd /srv/app && bash -s'",
                       "runuser deploy -c 'cd /srv/app && sh -s'",
                       "bash -c 'cd /x && bash'", "bash -c '{ cd /x; bash -s; }'",
                       "{ cat prelude.sh - | bash; }", "bash -c 'cat prelude.sh - | bash'"):
            commands.append((header + " <<'EOF'\n" + writer + "\nEOF", True))
        commands.extend((
            ("cat <<'EOF' | runuser -u deploy -- sh\n" + writer + "\nEOF", True),
            ("echo " + shlex.quote(writer) + " | runuser -u deploy -- bash", True),
            ("su - deploy -c 'printf done' <<'EOF'\n" + writer + "\nEOF", False),
            ("runuser -u deploy <<'EOF'\n" + writer + "\nEOF", False),
            ("runuser --help <<'EOF'\n" + writer + "\nEOF", False),
            ("su --help <<'EOF'\n" + writer + "\nEOF", False),
            ("su deploy -c <<'EOF'\n" + writer + "\nEOF", False),
            ("su deploy -c 'cd /x && printf done' <<'EOF'\n" + writer + "\nEOF", False),
            ("bash -c 'cd /x && cat' <<'EOF'\n" + writer + "\nEOF", False),
            ("bash -c 'cd /x && bash < other.sh' <<'EOF'\n" + writer + "\nEOF", False),
            ("bash -c 'cat prelude.sh - | bash < other.sh' <<'EOF'\n" + writer + "\nEOF", False),
            ("bash -c 'cat prelude.sh - | cat' <<'EOF'\n" + writer + "\nEOF", False),
            ("su deploy -c 'python3 -' <<'EOF'\n" + python_writer + "\nEOF", True),
            ("runuser -u deploy -- node <<'EOF'\n" + node_writer + "\nEOF", True),
        ))
        for reader, body in (("bash", writer), ("python3", python_writer), ("node", node_writer), ("ssh host", writer)):
            commands.append((reader + " <<< " + shlex.quote(body), True))
        for reader, body in (("gh issue comment 1 --body-file -", writer), ("cat", writer),
                             ("bash -c 'printf done'", writer), ("sudo -s cat", writer),
                             ("python3", "fixture=" + repr(writer) + "; print(fixture)"),
                             ("node", "const fixture=" + json.dumps(writer) + "; console.log(fixture)")):
            commands.append((reader + " <<< " + shlex.quote(body), False))
        for harness in ("codex", "claude"):
            for command, expected in commands:
                rows = history(harness, {})
                if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                with self.subTest(harness=harness, command=command):
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], expected)

    def test_here_string_file_provenance_requires_the_connected_input(self):
        writer = 'uv run /catalog/gh-plan.py --repo example/a claim 1'
        commands = (
            ("cat > check.sh <<< " + shlex.quote(writer), True),
            ("cat <<< " + shlex.quote(writer) + " | tee check.sh >/dev/null", True),
            ("cat > handoff.md <<< " + shlex.quote(writer) + "; echo done > check.sh", False),
            ("cat <<< " + shlex.quote(writer) + " | cat >check.sh <<< 'echo done'", False),
        )
        for harness in ("codex", "claude"):
            for command, expected in commands:
                rows = history(harness, {}, {})
                for index, text in ((1, command), (3, "bash check.sh")):
                    if harness == "codex": rows[index]["payload"]["arguments"] = json.dumps({"cmd": text})
                    else: rows[index]["message"]["content"][0]["input"] = {"command": text}
                rows[2], rows[4] = output({}, harness, "tool-0"), output({}, harness, "tool-1")
                with self.subTest(harness=harness, command=command):
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], expected)

    def test_xargs_stdin_is_code_only_for_its_executable_child(self):
        writer = 'uv run /catalog/gh-plan.py --repo example/a claim 1'
        reads = (
            "xargs -I{} rg -n {} skills/", "xargs -n1 rg -l", "xargs",
            "xargs -r0 -I bash rg -n bash skills/",
            "xargs -n 1 -L 2 -P 3 -d '\\n' -E bash -s 4096 rg -l",
            "xargs --replace={} --max-args=1 --max-lines=2 --max-procs=3 --delimiter='\\n' --eof=bash --max-chars=4096 rg -n {} skills/",
            "xargs -a notes.txt sh -c", "xargs --arg-file=notes.txt sh -c",
            "xargs -anotes.txt sh -c", "xargs -L2 -d, -Ebash -s4096 rg -n",
            "xargs -n1 env MODE=test rg -l", "xargs -n1 uv run gh-plan.py show 1",
            "xargs -n1 sudo -u me rg -n", "xargs -0 xargs -0 rg -n",
            "xargs -I{} rg -e 'gh pr create' {} skills/",
        )
        executes = (
            "xargs -0 sh -c", "xargs -I{} bash -c {}", "xargs -n1 '$EXEC'",
            "xargs -0 env MODE=test bash -c", "xargs -P4 -I{} sh -c {}",
            "xargs --max-args 1 --replace={} sh -c {}", "xargs -a /dev/stdin sh -c",
            "xargs -0 xargs -0 sh -c",
            "xargs -n1 sudo -u me sh -c", "xargs --arg-file=/dev/stdin --max-procs=4 sh -c",
        )
        for harness in ("codex", "claude"):
            for expected, readers in ((False, reads), (True, executes)):
                for reader in readers:
                    for command in ("printf '%s\\n' " + shlex.quote(writer) + " | " + reader,
                                    reader + " <<'EOF'\n" + writer + "\nEOF"):
                        rows = history(harness, {})
                        if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                        else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                        rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                        with self.subTest(harness=harness, command=command):
                            self.assertEqual(receipts.ownership_state(rows, harness)["pending"], expected)

    def test_multiple_heredocs_keep_their_own_command_and_final_stdin(self):
        writer = 'uv run /catalog/gh-plan.py --repo example/a claim 1'
        for command, expected in (
            ("cat <<'DATA' && bash <<'CODE'\n" + writer + "\nDATA\nprintf done\nCODE", False),
            ("cat <<'DATA' && bash <<'CODE'\nprintf done\nDATA\n" + writer + "\nCODE", True),
            ("bash <<'FIRST' <<'LAST'\n" + writer + "\nFIRST\nprintf done\nLAST", False),
            ("bash <<'FIRST' <<'LAST'\nprintf done\nFIRST\n" + writer + "\nLAST", True),
            ("bash <<'DATA' < input.txt\n" + writer + "\nDATA", False),
            ("python3 - <<'CODE'; cat <<'DATA'\nprint('done')\nCODE\nimport os\nos.system(" + repr(writer) + ")\nDATA", False),
            ("cat <<'DATA' | bash <<'CODE'\n" + writer + "\nDATA\nprintf done\nCODE", False),
        ):
            for harness in ("codex", "claude"):
                rows = history(harness, {})
                if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                with self.subTest(harness=harness, command=command):
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], expected)

    def test_unquoted_heredoc_escape_removal_precedes_interpreter_execution(self):
        helper = 'uv run /catalog/gh-plan.py --repo example/a claim 1'
        command = "bash <<EOF\n\\$(" + helper + ")\nEOF"
        for harness in ("codex", "claude"):
            rows = history(harness, {})
            if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            else: rows[1]["message"]["content"][0]["input"] = {"command": command}
            rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
            self.assertTrue(receipts.ownership_state(rows, harness)["pending"])

    def test_unquoted_heredoc_expansion_respects_backslash_not_body_quotes(self):
        helper = 'uv run /catalog/gh-plan.py --repo example/a claim 1'
        for body, expected in (
            ("\\$(" + helper + ")", False),
            ("\\`" + helper + "\\`", False),
            ("'$(" + helper + ")'", True),
            ('"$(' + helper + ')"', True),
            ("'`" + helper + "`'", True),
            ("\\\\$(" + helper + ")", True),
            ("\\\\`" + helper + "`", True),
            ("Updated $(date)\n" + helper, False),
            ("Updated `date`\n" + helper, False),
            ("$(printf '%s' " + shlex.quote(helper) + ")", False),
        ):
            command = "env FORMAT=template cat <<EOF\n" + body + "\nEOF"
            for harness in ("codex", "claude"):
                rows = history(harness, {})
                if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                with self.subTest(harness=harness, body=body):
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], expected)

    def test_regex_after_arrow_and_control_header_preserves_tool_suffixes(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5 > claim.json'
        prefixes = (
            r'const lines=["https://x"]; lines.filter(s => /^https?:\/\//.test(s)); ',
            "const lines=['x']; lines.filter(s => /'/ .test(s)); ",
            "if(true) /'/.test('x'); ", 'if((true)) /"/.test("x"); ',
            'let n=0; while(n++ < 1) /`/.test("x"); ',
            'for(let n=0;n<1;n++) /[/*]/.test("x"); ',
            "for await(const line of ['x']) /'/ .test(line); ",
        )
        for command, owning in ((writer, True), (writer + ' &', True), ('echo safe', False)):
            call = 'text(await tools.exec_command({cmd:' + json.dumps(command) + '}));'
            generated = 'await eval(' + json.dumps('(async()=>{' + call + '})()') + ');'
            for prefix in prefixes:
                for suffix in (call, generated):
                    rows = history('codex', {})
                    rows[1]['payload'].update(name='functions.exec', arguments=prefix + suffix)
                    rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                    with self.subTest(prefix=prefix, suffix=suffix):
                        self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)
            patch = '*** Begin Patch\n*** Add File: /repo/claim.sh\n+' + command + '\n*** End Patch'
            for prefix in prefixes:
                cell = prefix + 'text(await tools.apply_patch(' + json.dumps(patch) + '));'
                with self.subTest(patch=cell):
                    self.assertEqual(list(receipts.native_file_effects('functions.exec', cell)),
                                     list(receipts.native_file_effects('apply_patch', patch)))
        cell = 'const a=4,b=2; const ratio=a / b // ' + call + '\ntext(ratio);'
        self.assertEqual(list(receipts.shell_texts('functions.exec', cell)), [])
        self.assertFalse(receipts.opaque_ownership('functions.exec', cell))

    def test_generated_sources_keep_enclosing_command_bindings_and_computed_operands(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5 > claim.json'
        for command, owning in ((writer, True), (writer + ' &', True), ('echo safe', False),
                                ('echo ' + shlex.quote(writer), False)):
            binding = 'const c=' + json.dumps(command) + '; '
            cells = (
                binding + 'await eval("(async()=>{text(await tools.exec_command({cmd:c}));})()");',
                binding + 'await eval(' + json.dumps('(async()=>{text(await tools.exec_command({"cmd":c}));})()') + ');',
                binding + 'await eval(' + json.dumps("(async()=>{text(await tools.exec_command({'command':c}));})()") + ');',
                binding + 'await eval(' + json.dumps('(async()=>{text(await tools.exec_command({"cmd":c + " && echo done"}));})()') + ');',
                'const a="safe", c=' + json.dumps(command) + '; await eval("(async()=>{text(await tools.exec_command({cmd:c}));})()");',
                binding + 'await eval(`(async()=>{text(await tools.exec_command({cmd:${JSON.stringify(c)}}));})()`);',
                binding + 'await eval("(async()=>{text(await tools.exec_command({cmd:" + JSON.stringify(c) + "}));})()");',
                'const cmd=' + json.dumps(command) + '; await eval("(async()=>{text(await tools.exec_command({cmd}));})()");',
                binding + 'const encode=x=>JSON.stringify(x); await eval("(async()=>{text(await tools.exec_command({cmd:" + encode(c) + "}));})()");',
            )
            for cell in cells:
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                with self.subTest(cell=cell):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)
            inert = binding + 'await eval("text(c)");'
            self.assertEqual(list(receipts.shell_texts('functions.exec', inert)), [])
            self.assertFalse(receipts.opaque_ownership('functions.exec', inert))

    def test_generated_bound_file_writes_reach_only_later_execution(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5'
        for program, owning in (('import os\nos.system(' + repr(writer) + ')\n', True),
                                ('print(' + repr(writer) + ')\n', False)):
            shell = "cat > /repo/run.py <<'PY'\n" + program + 'PY'
            patch = '*** Begin Patch\n*** Add File: /repo/run.py\n' + ''.join('+' + line + '\n' for line in program.splitlines()) + '*** End Patch'
            cells = (
                'const c=' + json.dumps(shell) + '; await eval("(async()=>{text(await tools.exec_command({cmd:c}));})()");',
                'const c=' + json.dumps(shell) + '; await eval(' + json.dumps('(async()=>{text(await tools.exec_command({"cmd":c}));})()') + ');',
                'const c=' + json.dumps(shell) + '; await Function("c",' + json.dumps('return tools.exec_command({cmd:c});') + ')(c);',
                'const c=' + json.dumps(shell) + '; await eval(`(async()=>{text(await tools.exec_command({cmd:${JSON.stringify(c)}}));})()`);',
                'const c=' + json.dumps(shell) + '; const encode=x=>JSON.stringify(x); await eval("(async()=>{text(await tools.exec_command({cmd:" + encode(c) + "}));})()");',
                'const p=' + json.dumps(patch) + '; await eval("(async()=>{text(await tools.apply_patch(p));})()");',
            )
            patch_calls = (
                'const{apply_patch}=tools;await apply_patch(p);',
                'await tools?.apply_patch(p);', 'await tools.apply_patch?.(p);',
                'await tools["apply_patch"](p);',
                'await tools.apply_patch.call(tools,p);',
                'const ap=tools.apply_patch;await ap(p);',
                'await Reflect.apply(tools.apply_patch,tools,[p]);',
            )
            for call in patch_calls:
                for source in (call, 'await eval(' + json.dumps('(async()=>{' + call + '})()') + ');',
                               'await Function("p",' + json.dumps('return (async()=>{' + call + '})()') + ')(p);'):
                    cells += ('const p=' + json.dumps(patch) + ';' + source,)
            setup = 'const P=' + json.dumps(patch) + ';'
            possible_inputs = (
                'const {patch}={patch:P};await tools.apply_patch(patch);',
                'const {value:p}={value:P};await tools.apply_patch(p);',
                'const [p]=[P];await tools.apply_patch(p);',
                'let p;({p}={p:P});await tools.apply_patch(p);',
                'function make(){return P}await tools.apply_patch(make());',
                'class Make{static patch(){return P}}await tools.apply_patch(Make.patch());',
                'class Make{static get patch(){return P}}await tools.apply_patch(Make.patch);',
                'const ps=[P];for(const p of ps)await tools.apply_patch(p);',
                'const ps=[P];ps.forEach(p=>tools.apply_patch(p));',
                'let p;p=P;await tools.apply_patch(p);',
                'let p=P;p+="";const ap=tools.apply_patch;await ap(p);',
                'const o={};o.patch=P;await tools.apply_patch(o.patch);',
                'const o={patch:P};await tools.apply_patch(o.patch);',
                'const o={};o["patch"]=P;const ap=tools.apply_patch;await ap(o["patch"]);',
                'globalThis.P=P;const ap=tools.apply_patch;await ap(globalThis.P);',
                'globalThis["P"]=P;const ap=tools.apply_patch;await ap(globalThis["P"]);',
                'const key="P";globalThis[key]=P;const ap=tools.apply_patch;await ap(globalThis[key]);',
                'globalThis.P=P;const p=globalThis.P;const ap=tools.apply_patch;await ap(p);',
                'globalThis.P=P;const g=globalThis;const ap=tools.apply_patch;await ap(g.P);',
            )
            for body in possible_inputs:
                source = setup + body
                cells += (source, 'await eval(' + json.dumps('(async()=>{' + source + '})()') + ');',
                          'await Function(' + json.dumps('return (async()=>{' + source + '})()') + ')();')
            cells += (setup + 'globalThis.text=tools.apply_patch;await eval("text(P)");',)
            safe_patch = '*** Begin Patch\n*** Add File: /repo/run.py\n+print("safe")\n*** End Patch'
            # A later assignment is not the value of an earlier write.
            cells += (setup + 'globalThis.P=P;await Function("return tools.apply_patch(globalThis.P)")();globalThis.P='
                      + json.dumps(safe_patch) + ';',
                      setup + 'let p=P;const ap=tools.apply_patch;await ap(p);p=' + json.dumps(safe_patch) + ';')
            header, body = patch.split('\n', 1)
            fragments = 'const h=' + json.dumps(header + '\n') + ';const b=' + json.dumps(body) + ';'
            for operand in ('h+b', '`${h}${b}`'):
                call = 'tools.apply_patch(' + operand + ')'
                for source in (fragments + 'await eval(' + json.dumps(call) + ');',
                               'await Function(' + json.dumps(fragments + call) + ')();'):
                    effects = list(receipts.native_file_effects('functions.exec', source))
                    with self.subTest(generated_patch=source):
                        self.assertTrue(any(program.strip() in content for effect in effects
                                            if effect['kind'] == 'write' for content in effect['contents']))
                    cells += (source,)
            for operand in ('[h,b].join("")', 'h.concat(b)', 'h+b+x', '`${h}${b}${x}`'):
                source = fragments + 'let x="";x+="";await tools.apply_patch(' + operand + ');'
                cells += (source, 'await eval(' + json.dumps('(async()=>{' + source + '})()') + ');',
                          'await Function(' + json.dumps('return (async()=>{' + source + '})()') + ')();')
            cells += (
                'let p=' + json.dumps(safe_patch) + ';eval(' + json.dumps('p=' + json.dumps(patch))
                + ');await tools.apply_patch(p);',
                'globalThis.P=' + json.dumps(safe_patch) + ';eval(' + json.dumps('globalThis.P=' + json.dumps(patch))
                + ');await tools.apply_patch(globalThis.P);',
                'await Function(' + json.dumps('return this["to"+"ols"]["apply_"+"patch"](' + json.dumps(patch) + ')') + ')();',
                'await globalThis.eval(' + json.dumps('"use strict";this["to"+"ols"]["apply_"+"patch"](' + json.dumps(patch) + ')') + ');',
            )
            split_header, split_body = patch.split('\n', 2)[:2], patch.split('\n', 2)[2]
            header = '\n'.join(split_header) + '\n'
            split_cells = (
                'const h=' + json.dumps(header) + ';const b=' + json.dumps(split_body)
                + ';await tools.apply_patch([h,b].join(""));',
                'const path="/repo/run.py";const lines=' + json.dumps(program.splitlines())
                + ';await tools.apply_patch("*** Begin Patch\\n*** Add File: "+path+"\\n"'
                + '+lines.map(l=>"+"+l).join("\\n")+"\\n*** End Patch");',
            )
            linked = ('import os\nW=' + repr(writer) + '\nos.system(W)\n') if owning else ('W=' + repr(writer) + '\nprint(W)\n')
            linked_header = '*** Begin Patch\n*** Add File: /repo/run.py\n'
            linked_body = ''.join('+' + line + '\n' for line in linked.splitlines()) + '*** End Patch'
            split_setup = 'const h=' + json.dumps(linked_header) + ';const b=' + json.dumps(linked_body) + ';'
            repeated = 'import os\nW="echo safe"\nos.system(W)\nW=' + repr(writer) + '\nos.system(W)\n' if owning else linked
            split_cells += (
                'const h=' + json.dumps(linked_header) + ';const lines=' + json.dumps(repeated.splitlines())
                + ';await tools.apply_patch(h+lines.map(l=>"+"+l).join("\\n")+"\\n*** End Patch");',
                'const path="/repo/run.py";const lines=' + json.dumps(linked.splitlines())
                + ';await tools.apply_patch("*** Begin Patch\\n*** Add File: "+path+"\\n"'
                + '+lines.map(l=>"+"+l).join("\\n")+"\\n*** End Patch");',
                split_setup + 'globalThis.H=h;globalThis.B=b;await Function("return tools.apply_patch(H+B)")();',
                split_setup + 'globalThis.H=h;globalThis.B=b;await(0,eval)("tools.apply_patch(H+B)");',
                split_setup + 'await Function("h","b","return tools.apply_patch([h,b].join(\'\'))")(h,b);',
                'const h=' + json.dumps(linked_header + '+import os\n') + ';const b='
                + json.dumps(linked_body.replace('+import os\n', '')) + ';await tools.apply_patch([h,b].join(""));',
            )
            for source in split_cells:
                cells += (source, 'await eval(' + json.dumps('(async()=>{' + source + '})()') + ');',
                          'await Function(' + json.dumps('return (async()=>{' + source + '})()') + ')();')
                # A write alone is not execution, even when its contents own.
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=source)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                with self.subTest(split_without_execution=source):
                    self.assertFalse(receipts.ownership_state(rows, 'codex')['pending'])
            for cell in cells:
                for command, executes in (('python3 /repo/run.py', True), ('cat /repo/run.py', False)):
                    rows = history('codex', {}, {})
                    rows[1]['payload'].update(name='functions.exec', arguments=cell)
                    rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                    rows[3]['payload']['arguments'] = json.dumps({'cmd': command})
                    rows[4] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-1')
                    with self.subTest(cell=cell, command=command):
                        self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning and executes)

        removed = "os.system(" + repr(writer) + ")"
        before = '*** Begin Patch\n*** Add File: /repo/run.py\n+import os\n+' + removed + '\n*** End Patch'
        header = '*** Begin Patch\n*** Update File: /repo/run.py\n'
        body = '@@\n-' + removed + '\n+print("safe")\n*** End Patch'
        update = 'const h=' + json.dumps(header) + ';const b=' + json.dumps(body) + ';await tools.apply_patch(h+b);'
        for prefix in ('', 'await tools.apply_patch(' + json.dumps(before) + ');'):
            rows = history('codex', {}, {})
            rows[1]['payload'].update(name='functions.exec', arguments=prefix + update)
            rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
            rows[3]['payload']['arguments'] = json.dumps({'cmd': 'python3 /repo/run.py'})
            rows[4] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-1')
            with self.subTest(removal=prefix + update):
                self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], bool(prefix))

    def test_generated_sources_keep_unresolved_reachable_command_operands(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5 > claim.json'
        for command, owning in ((writer, True), (writer + ' &', True), ('echo safe', False),
                                ('echo ' + shlex.quote(writer), False)):
            setup = 'const cmds=[' + json.dumps(command) + ']; '
            source = '(async()=>{text(await tools.exec_command({cmd:c}));})()'
            invoke = 'await eval(' + json.dumps(source) + ');'
            cells = (
                setup + 'for (const c of cmds){' + invoke + '}',
                setup + 'await Promise.all(cmds.map(async c=>{' + invoke + '}));',
                setup + 'const [c]=cmds; ' + invoke,
                setup + 'try{throw cmds[0]}catch(c){' + invoke + '}',
                setup + 'const f=Function("c", "return tools.exec_command({cmd:c})"); await f(cmds[0]);',
                setup + 'await Function("c", "return tools.exec_command({cmd:c})")(cmds[0]);',
                setup + 'const f=Function("c", "return tools.exec_command({cmd:c})"); const alias=f; await f("echo safe"); await alias(cmds[0]);',
                'const f=Function(' + json.dumps('c=' + json.dumps(command)) + ', "return tools.exec_command({cmd:c})"); await f(); await f("echo safe");',
                setup + 'const AsyncFunction=(async()=>{}).constructor; await new AsyncFunction("c", "return tools.exec_command({cmd:c})")(cmds[0]);',
            )
            for factory in ('Function', '(async()=>{}).constructor', 'new AsyncFunction'):
                prelude = 'const AsyncFunction=(async()=>{}).constructor;' if factory.startswith('new ') else ''
                for parameters, operand in (('', 'arguments[0]'), ('"...a",', 'a[0]'), ('"{c}",', 'c')):
                    body = json.dumps('return tools.exec_command({cmd:' + operand + '})')
                    actual = '{c}' if parameters == '"{c}",' else 'c'
                    for use in ('await Promise.all(cmds.map(f));', 'cmds.forEach(f);',
                                'for(const c of cmds) await f(' + actual + ');'):
                        values = '[{c:' + json.dumps(command) + '}]' if parameters == '"{c}",' and 'of cmds' not in use else '[' + json.dumps(command) + ']'
                        cells += (prelude + 'const cmds=' + values + ';const f=' + factory + '(' + parameters + body + ');' + use,)
            for cell in cells:
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                with self.subTest(cell=cell):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)

    def test_javascript_command_bindings_do_not_cross_lexical_shadow_sites(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5 > claim.json'
        for command, owning in ((writer, True), ('echo safe', False),
                                ('echo ' + shlex.quote(writer), False)):
            binding = json.dumps(command)
            call = 'text(await tools.exec_command({cmd}));'
            source = '(async()=>{text(await tools.exec_command({cmd:cmd}));})()'
            evaluated = 'await eval(' + json.dumps(source) + ');'
            cells = (
                'const cmd="git status --short"; ' + call + ' const run=async cmd=>{' + call + '}; await run(' + binding + ');',
                '{const cmd=' + binding + ';' + call + '}{const cmd="echo safe";' + call + '}',
                'const cmd="echo safe"; for (const cmd of [' + binding + ']){' + call + '}',
                'const cmd="echo safe"; try{throw ' + binding + '}catch(cmd){' + call + '}',
                'const cmd="echo safe"; {const [cmd]=[' + binding + '];' + call + '}',
                'const cmd="echo safe"; {const {value:cmd}={value:' + binding + '};' + call + '}',
                'const cmd="echo safe"; async function run(cmd){' + call + '} await run(' + binding + ');',
                'let cmd="echo safe"; cmd=' + binding + ';' + call,
                'const W=' + binding + '; let run=tools.exec_command; run=tools.exec_command; await run({cmd:W});',
                'const W=' + binding + '; let run=tools.exec_command; if(true){run=tools.exec_command;} await run({cmd:W});',
                'const W=' + binding + '; const first=tools.exec_command; let run=first; run=first; await run({cmd:W});',
                'const W=' + binding + '; let t=tools; t=tools; await t["exec_command"]({cmd:W});',
                'const W=' + binding + ';const c=console;c.log=tools.exec_command;let run=tools.exec_command;run=console.log;await run({cmd:W});',
                'const cmd="echo safe"; const object={async run(cmd){' + call + '}}; await object.run(' + binding + ');',
                'const cmd="echo safe"; async function run(cmd){' + evaluated + '} await run(' + binding + ');',
                'const cmd="echo safe"; const run=async(cmd)=>{' + evaluated + '}; await run(' + binding + ');',
                'const cmd="echo safe"; for (const cmd of [' + binding + ']){' + evaluated + '}',
            )
            for cell in cells:
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                with self.subTest(cell=cell):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)
            inert = ('const fixture=' + json.dumps(writer) + '; const cmd="echo safe"; '
                     + 'await eval(' + json.dumps(source) + ');')
            self.assertFalse(receipts.opaque_ownership('functions.exec', inert))

    def test_unresolved_generated_file_inputs_preserve_later_execution_only(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5'
        for program, owning in (('import os\nos.system(' + repr(writer) + ')\n', True),
                                ('print(' + repr(writer) + ')\n', False)):
            shell = "cat > /repo/run.py <<'PY'\n" + program + 'PY'
            patch = '*** Begin Patch\n*** Add File: /repo/run.py\n' + ''.join('+' + line + '\n' for line in program.splitlines()) + '*** End Patch'
            for operand, source in ((shell, 'text(await tools.exec_command({cmd:c}));'),
                                    (patch, 'text(await tools.apply_patch(c));')):
                cells = (
                    'const commands=[' + json.dumps(operand) + '];for(const c of commands){'
                    + 'await eval(' + json.dumps('(async()=>{' + source + '})()') + ');}',
                    'const commands=[' + json.dumps(operand) + '];const f=(async()=>{}).constructor("c",'
                    + json.dumps('return ' + source) + ');for(const c of commands) await f(c);',
                )
                for cell in cells:
                    for command, executes in (('python3 /repo/run.py', True), ('cat /repo/run.py', False)):
                        rows = history('codex', {}, {})
                        rows[1]['payload'].update(name='functions.exec', arguments=cell)
                        rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                        rows[3]['payload']['arguments'] = json.dumps({'cmd': command})
                        rows[4] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-1')
                        with self.subTest(cell=cell, command=command):
                            self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning and executes)

    def test_nested_generated_sources_are_discovered_once(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5'
        for depth in (6, 12):
            cell = 'text(await tools.exec_command({cmd:' + json.dumps(writer) + '}));'
            for _ in range(depth):
                cell = 'await eval(' + json.dumps('(async()=>{' + cell + '})()') + ');'
            visited = []
            original = receipts.javascript_generated_sources
            def discover(source):
                visited.append(source)
                yield from original(source)
            with patch.object(receipts, 'javascript_generated_sources', side_effect=discover):
                text = receipts.unparsed_command_text(cell)
            self.assertIn(writer, text)
            self.assertEqual(len(visited), len(set(visited)), 'generated descendants were repeatedly rediscovered')
        data = 'const data={' + ','.join('k' + str(index) + ':"safe"' for index in range(1000)) + '};'
        totals = []
        original_projection = receipts.javascript_projection
        for count in (16, 64):
            source = data + 'const ap=tools.apply_patch;' + 'text(data.k0);' * count
            processed = []
            def project(value, **kwargs):
                processed.append(len(value))
                return original_projection(value, **kwargs)
            with patch.object(receipts, 'javascript_projection', side_effect=project):
                self.assertEqual(list(receipts.javascript_possible_patches(source)), [])
            totals.append(sum(processed))
        # Extra tiny call operands must not each re-project the large data RHS.
        self.assertLess(totals[1] - totals[0], len(data))

        work = []
        for count in (16, 64):
            source = 'const cfg={h:"*** Begin Patch\\n*** Add File: /repo/run.py\\n",' + ','.join(
                'k' + str(index) + ':"safe"' for index in range(count)) + '};'
            source += ''.join('consume(cfg.k' + str(index) + ');' for index in range(count))
            with patch.object(receipts, 'javascript_patch_input', wraps=receipts.javascript_patch_input) as classify:
                values = list(receipts.javascript_possible_patches(source))
            work.append((classify.call_count, sum(len(value) for _, value in values)))
        # Count actual traversal/synthesis with the header inside the shared
        # aggregate. Distinct reads must not each rebuild its entire closure.
        self.assertLessEqual(work[1][0] - work[0][0], 8 * (64 - 16))
        self.assertLessEqual(work[1][1], 6 * work[0][1])

        writer_counts = []
        original_content = receipts.executable_content
        for count in (16, 64):
            source = 'let c="safe";' + ''.join('eval(' + json.dumps('const k="safe' + str(index)
                + '";tools.exec_command({cmd:c});') + ');' for index in range(count))
            with patch.object(receipts, 'executable_content', wraps=original_content) as classify:
                generated = list(receipts.javascript_generated_sources(source))
            writer_counts.append((classify.call_count, sum(map(len, generated))))
        # Bound actual parser work and emitted discovery bytes. A cache alone
        # cannot hide a repeated all-source traversal or duplicate carriers.
        self.assertLessEqual(writer_counts[1][0] - writer_counts[0][0], 3 * (64 - 16))
        self.assertLessEqual(writer_counts[1][1], 6 * writer_counts[0][1])
        totals = []
        for count in (16, 64):
            source = data + ''.join('globalThis.a' + str(index) + '=data;' for index in range(count))
            source += 'await Function("return 0")();'
            processed = []
            def project(value, **kwargs):
                processed.append(len(value))
                return original_projection(value, **kwargs)
            with patch.object(receipts, 'javascript_projection', side_effect=project):
                list(receipts.javascript_generated_sources(source))
            totals.append(sum(processed))
        self.assertLess(totals[1] - totals[0], len(data))

        # Count real splitter traversal, not calls to a memoized wrapper.
        parts = receipts.node_expression_parts
        scanned = []
        class Counted(str):
            def __getitem__(self, index):
                if isinstance(index, int):
                    work[0] += 1
                return super().__getitem__(index)
        def measure(code, *args, **kwargs):
            return parts(Counted(code), *args, **kwargs)
        for count in (16, 64):
            source = ''.join('o.k' + str(index) + '=1;' for index in range(count))
            _, code, _, _ = receipts.javascript_projection(source)
            work = [0]
            with patch.object(receipts, 'node_expression_parts', side_effect=measure):
                list(receipts.javascript_source_calls(source, code))
            scanned.append(work[0])
        with self.subTest(first_expression_work=True):
            self.assertLessEqual(scanned[1], 6 * scanned[0])
        # Include the shared scope index and copied spans so binding lookup
        # cannot hide the same quadratic traversal outside the splitter.
        class SourceWork(str):
            def __iter__(self):
                for value in super().__iter__():
                    work[0] += 1
                    yield value
            def __getitem__(self, index):
                value = super().__getitem__(index)
                work[0] += len(value) if isinstance(index, slice) else 1
                return SourceWork(value) if isinstance(index, slice) else value
        for form in ('assignments', 'scopes', 'aliases'):
            scanned = []
            for count in (16, 64):
                if form == 'assignments':
                    source = ''.join('o.k' + str(index) + '=1;' for index in range(count))
                elif form == 'scopes':
                    source = ''.join('function f' + str(index) + '(globalThis){return globalThis;}' for index in range(count))
                    source += 'await globalThis.eval("0");'
                else:
                    source = 'const g=globalThis;' + 'await (0,g.eval)("0");' * count
                _, code, _, _ = receipts.javascript_projection(source)
                work = [0]
                calls = list(receipts.javascript_source_calls(source, SourceWork(code)))
                with self.subTest(source_discovery_calls=form, count=count):
                    self.assertEqual(len(calls), 0 if form == 'assignments' else 1 if form == 'scopes' else count)
                scanned.append(work[0])
            with self.subTest(source_discovery_work=form):
                self.assertLessEqual(scanned[1], 6 * scanned[0])
        # Count executed classifier lines too: memoized RHS values must not
        # still enumerate an entire aggregate for every distinct member read.
        traversal = []
        for count in (16, 64):
            source = 'const cfg={' + ','.join('k' + str(index) + ':eval' for index in range(count)) + '};'
            source += ''.join('await cfg.k' + str(index) + '("0");' for index in range(count))
            _, code, _, _ = receipts.javascript_projection(source)
            work = [0]
            owner_file = receipts.javascript_source_calls.__code__.co_filename
            def executed(frame, event, arg):
                if frame.f_code.co_filename == owner_file:
                    if event == 'line':
                        work[0] += 1
                    return executed
                return None
            previous = sys.gettrace()
            sys.settrace(executed)
            try:
                calls = list(receipts.javascript_source_calls(source, code))
            finally:
                sys.settrace(previous)
            self.assertEqual(len(calls), count)
            traversal.append(work[0])
        self.assertLessEqual(traversal[1], 6 * traversal[0])
        for operand in ("x", ""):
            traversal = []
            for count in (16, 64):
                source = 'const cfg={' + ','.join('k' + str(index) + ':eval' for index in range(count)) + '};'
                source += ''.join('await cfg.k' + str(index) + '(' + operand + ');' for index in range(count))
                _, code, _, _ = receipts.javascript_projection(source)
                work = [0]
                owner_file = receipts.javascript_source_calls.__code__.co_filename
                def executed(frame, event, arg):
                    if frame.f_code.co_filename == owner_file:
                        if event == 'line':
                            work[0] += 1
                        return executed
                    return None
                previous = sys.gettrace()
                sys.settrace(executed)
                try:
                    calls = list(receipts.javascript_source_calls(source, code))
                finally:
                    sys.settrace(previous)
                self.assertEqual(len(calls), count)
                traversal.append(work[0])
            with self.subTest(aggregate_operand_work=operand):
                self.assertLessEqual(traversal[1], 6 * traversal[0])

        # Static cfg.kI safe members in the original row are now inert.
        # Keep synthesis meaningful by selecting one shared patch aggregate.
        work = []
        for count in (16, 64):
            source = 'const shared={h:"*** Begin Patch\\n*** Add File: /repo/run.py\\n",b:'
            source += json.dumps('+import os\n+os.system(' + repr(writer) + ')\n*** End Patch') + '};'
            source += 'const cfg={' + ','.join('k' + str(index) + ':shared' for index in range(count)) + '};'
            source += ''.join('consume(cfg.k' + str(index) + ');' for index in range(count))
            with patch.object(receipts, 'javascript_patch_input', wraps=receipts.javascript_patch_input) as classify:
                values = list(receipts.javascript_possible_patches(source))
            self.assertTrue(any(writer in value and '/repo/run.py' in value for _, value in values))
            work.append((classify.call_count, sum(len(value) for _, value in values)))
        self.assertLessEqual(work[1][0] - work[0][0], 8 * (64 - 16))
        self.assertLessEqual(work[1][1], 6 * work[0][1])


        # Source conversion can demand a getter after ordinary invocations
        # settle. Its captured lexical source handle still has to be scanned.
        for handle, discovers_eval in (('eval', True), ('text', False)):
            source = 'const src=' + json.dumps('tools.exec_command({cmd:' + json.dumps(writer) + '})') + ';'
            source += 'const make=(e,s)=>({get args(){e(s);return ["c",'
            source += json.dumps('return tools.exec_command({cmd:c})') + ']}});'
            source += 'const o=make(' + handle + ',src);Function(...o.args)("echo safe");'
            with self.subTest(captured_getter_handle=handle):
                atoms, code, _, _ = receipts.javascript_projection(source)
                calls = list(receipts.javascript_source_calls(source, code, literal_atoms=atoms))
                self.assertTrue(any(kind == 'constructor' for _, _, kind, _, _ in calls))
                self.assertEqual(any(kind == 'eval' for _, _, kind, _, _ in calls), discovers_eval)

        # Only an actually read accessor executes. Data functions and
        # accessor replacement keep the same original lexical capture.
        getter_array = '["c",' + json.dumps('return tools.exec_command({cmd:c})') + ']'
        active_getter = 'get args(){e(s);return ' + getter_array + '}'
        read_getter = 'get args(){text(s);return ' + getter_array + '}'
        for members, property_name, discovers_eval in (
            (active_getter, None, False),
            (active_getter + ',other:' + getter_array, 'other', False),
            ('args(){e(s);return ' + getter_array + '}', 'args', False),
            ('args:()=>{e(s);return ' + getter_array + '}', 'args', False),
            (active_getter + ',args:' + getter_array, 'args', False),
            ('args:' + getter_array + ',' + active_getter, 'args', True),
            (active_getter + ',' + read_getter, 'args', False),
            (read_getter + ',' + active_getter, 'args', True),
            (active_getter + ',set args(v){text(s)}', 'args', True),
            ('set args(v){text(s)},' + active_getter, 'args', True),
            (read_getter + ',set args(v){e(s)}', 'args', False),
            ('set args(v){e(s)}', 'args', False),
        ):
            source = 'const src=' + json.dumps('tools.exec_command({cmd:' + json.dumps(writer) + '})') + ';'
            source += 'const make=(e,s)=>({' + members + '});const o=make(eval,src);'
            operand = getter_array if property_name is None else 'o.' + property_name
            source += 'Function(...' + operand + ')("echo safe");'
            with self.subTest(accessor_members=members, read_property=property_name):
                atoms, code, _, _ = receipts.javascript_projection(source)
                calls = list(receipts.javascript_source_calls(source, code, literal_atoms=atoms))
                self.assertTrue(any(kind == 'constructor' for _, _, kind, _, _ in calls))
                self.assertEqual(any(kind == 'eval' for _, _, kind, _, _ in calls), discovers_eval)
        source = 'const make=()=>({get args(){return Function("c",'
        source += json.dumps('return tools.exec_command({cmd:c})') + ')}});const o=make();o.args("echo safe");'
        atoms, code, _, _ = receipts.javascript_projection(source)
        calls = list(receipts.javascript_source_calls(source, code, literal_atoms=atoms))
        self.assertTrue(any(kind == 'constructor' for _, _, kind, _, _ in calls))

        # A captured receiver must not replay every prior self-member
        # assignment under a new reference spelling. Abort the old traversal
        # deterministically before it can hang this module.
        for initial, scoped in (("{}", False), ("getReceiver()", False), ("{}", True), ("getReceiver()", True), ("eval", "wrapper"), ("text", "wrapper"), ("eval", "tag"), ("text", "tag"), ("eval", "call"), ("text", "call"), ("eval", "parameter"), ("text", "parameter")):
            traversals = []
            for count in (4, 8):
                writes = ("const get=({h})=>h;" + "".join("function s" + str(index) + "(){v=get({h:v});}" for index in range(count)) if scoped == "parameter" else
                          "const tag=()=>v;" + "".join("function s" + str(index) + "(){v=tag" + ("`x`" if scoped == "tag" else "()") + ";}" for index in range(count)) if scoped in {"tag", "call"} else
                          "".join("function s" + str(index) + "(){v={x:v};}" for index in range(count)) if scoped == "wrapper" else
                          "".join("function s" + str(index) + "(){v=v.next;}" for index in range(count)) if scoped
                          else "".join("v=v.a" + str(index) + ";" for index in range(count)))
                source = "let v=" + initial + ";" + writes + ("f(v.x);" if scoped is True else "f(v);")
                work = [0]
                owner_file = receipts.javascript_source_calls.__code__.co_filename
                def executed(frame, event, arg):
                    if frame.f_code.co_filename == owner_file:
                        if event == "line":
                            work[0] += 1
                            if work[0] > 250_000:
                                raise AssertionError("captured receiver traversal did not stay bounded")
                        return executed
                    return None
                previous = sys.gettrace()
                sys.settrace(executed)
                try:
                    _, code, _, _ = receipts.javascript_projection(source)
                    calls = list(receipts.javascript_source_calls(source, code))
                finally:
                    sys.settrace(previous)
                if scoped in {"wrapper", "tag", "call", "parameter"} and initial == "eval":
                    self.assertTrue(calls)
                else:
                    self.assertEqual(calls, [])
                traversals.append(work[0])
            with self.subTest(captured_receiver_work=initial, cross_scope=scoped):
                self.assertLessEqual(traversals[1], 6 * traversals[0])

        for shape in ('forward', 'double', 'append', 'prepend', 'duplicate'):
            traversals = []
            for depth in (4, 8):
                with self.subTest(shape=shape, depth=depth):
                    if depth == 8 and not traversals:
                        self.skipTest('depth4 did not complete; depth8 work is unmeasurable')
                    # Bound the baseline at250k, then use the same6x growth
                    # budget as the final assertion for the second measurement.
                    budget = 250_000 if depth == 4 else 6 * traversals[0]
                    if shape in {'forward', 'double'}:
                        source = 'const f0=(f,...a)=>f(...a);'
                        for index in range(1, depth):
                            spread = '...a,...a' if shape == 'double' else '...a'
                            source += 'const f' + str(index) + '=(f,...a)=>f' + str(index - 1) + '(f,' + spread + ');'
                        source += 'f' + str(depth - 1) + '(eval,"void 0");'
                    else:
                        args = {'append': '...a,"x"', 'prepend': '"x",...a',
                                'duplicate': '...a,...a'}[shape]
                        source = 'const r=(f,...a)=>{if(flag)r(f,' + args + ');return f(...a)};r(eval,"void 0");'
                        source += 'text("safe");' * depth
                    count = [0]
                    owner_file = receipts.javascript_source_calls.__code__.co_filename
                    def executed(frame, event, arg):
                        if frame.f_code.co_filename == owner_file:
                            if event == 'line':
                                count[0] += 1
                                if count[0] > budget:
                                    raise AssertionError('packet traversal did not stay bounded')
                            return executed
                        return None
                    previous = sys.gettrace()
                    sys.settrace(executed)
                    try:
                        atoms, code, _, _ = receipts.javascript_projection(source)
                        calls = list(receipts.javascript_source_calls(source, code, literal_atoms=atoms))
                    finally:
                        sys.settrace(previous)
                    self.assertTrue(any(kind == 'eval' for _, _, kind, _, _ in calls))
                    traversals.append(count[0])
            if len(traversals) == len((4, 8)):
                with self.subTest(packet_work=shape):
                    self.assertLessEqual(traversals[1], 6 * traversals[0])

    def test_javascript_command_discovery_covers_noncanonical_tool_callees(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 5'
        calls = (
            'const {exec_command}=tools;await exec_command(ARGS);',
            'await tools?.exec_command(ARGS);', 'await tools.exec_command?.(ARGS);',
            'await tools.exec_command.call(tools,ARGS);',
            'const run=tools.exec_command.bind(tools);await run(ARGS);',
            'const o={run:tools.exec_command};await o.run(ARGS);',
            'await Reflect.apply(tools.exec_command,tools,[ARGS]);',
        )
        for command, owning in ((writer, True), ('echo safe', False),
                                ('echo ' + shlex.quote(writer), False)):
            setup = 'const W=' + json.dumps(command) + ';const cmd=W;'
            for call in calls:
                for fields in ('{cmd:W}', '{"cmd":W}', '{cmd}'):
                    source = call.replace('ARGS', fields)
                    for cell in (setup + source, setup + 'await eval(' + json.dumps('(async()=>{' + source + '})()') + ');'):
                        rows = history('codex', {})
                        rows[1]['payload'].update(name='functions.exec', arguments=cell)
                        rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                        with self.subTest(cell=cell):
                            self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)

    def test_generated_constructor_parameters_cover_callback_and_reflective_uses(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 5'
        source = json.dumps('return tools.exec_command({cmd:c})')
        uses = (
            'for(const c of cmds) await f(c);', 'await Promise.all(cmds.map(f));',
            'cmds.forEach(f);', 'await Promise.all(cmds.map(c=>f(c)));',
            'await f.call(null,W);', 'await f.apply(null,[W]);',
            'await f.bind(null,W)();',
        )
        for command, owning in ((writer, True), ('echo safe', False),
                                ('echo ' + shlex.quote(writer), False)):
            setup = 'const W=' + json.dumps(command) + ';const cmds=[W];'
            for factory in ('Function', '(async()=>{}).constructor'):
                for use in uses:
                    cell = setup + 'const f=' + factory + '("c",' + source + ');await f("echo safe");' + use
                    rows = history('codex', {})
                    rows[1]['payload'].update(name='functions.exec', arguments=cell)
                    rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                    with self.subTest(cell=cell):
                        self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)
            cell = setup + 'const f$=Function("c",' + source + ');await f$("echo safe");await Promise.all(cmds.map(f$));'
            with self.subTest(dollar_identifier=cell):
                self.assertEqual(receipts.opaque_ownership('functions.exec', cell), owning)

    def test_mutable_helpers_and_late_tool_aliases_cannot_discharge_ownership(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 5'
        bodies = (
            'let run=console.log;const go=()=>run({cmd:W});run=tools.exec_command;await go();',
            'let run=console.log;const job=Promise.resolve().then(()=>run({cmd:W}));run=tools.exec_command;await job;',
            'globalThis.text=tools.exec_command;let run=tools.exec_command;run=text;await run({cmd:W});',
            'globalThis.text=tools.exec_command;await text({cmd:W});',
            'globalThis.text=tools.exec_command;const c=W;await eval("text({cmd:c})");',
            'const g=globalThis;g["text"]=tools.exec_command;await text({cmd:W});',
            'globalThis.console={log:tools.exec_command};let run=tools.exec_command;run=console.log;await run({cmd:W});',
            'Object.assign(console,{log:tools.exec_command});let run=tools.exec_command;run=console.log;await run({cmd:W});',
            'Object.defineProperty(console,"log",{value:tools.exec_command});let run=tools.exec_command;run=console.log;await run({cmd:W});',
            'let run=console.log;run &&=tools.exec_command;await run({cmd:W});',
            'let run=console.log;[run]=[tools.exec_command];await run({cmd:W});',
            'let run=console.log;({exec_command:run}=tools);await run({cmd:W});',
        )
        for command, owning in ((writer, True), ('echo safe', False)):
            for body in bodies:
                cell = 'const W=' + json.dumps(command) + ';' + body
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                with self.subTest(cell=cell):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)

    def test_bare_assignment_patterns_eval_and_spaced_rest_invalidate_initializers(self):
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 5'
        bodies = (
            'let cmd="echo safe";[cmd]=[W];await tools.exec_command({cmd});',
            'let cmd="echo safe";({cmd}={cmd:W});await tools.exec_command({cmd});',
            'let cmd="echo safe";for([cmd] of [[W]]){}await tools.exec_command({cmd});',
            'let cmd="echo safe";eval("cmd=W");await tools.exec_command({cmd});',
            'var cmd="echo safe";const source="cmd=W";eval(source);await tools.exec_command({cmd});',
            'const cmd="echo safe";async function run(...cmd\n){await tools.exec_command({cmd:cmd[0]});}await run(W);',
            'const cmd="echo safe";const run=async(...cmd \n)=>{await tools.exec_command({cmd:cmd[0]});};await run(W);',
        )
        for command, owning in ((writer, True), ('echo safe', False)):
            for body in bodies:
                cell = 'const W=' + json.dumps(command) + ';' + body
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                with self.subTest(cell=cell):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)

    def test_generated_source_bindings_respect_eval_and_constructor_scope(self):
        for source in ("function f(){", "async function f(){", "const f=function named(){", "const f=()=>{"):
            with self.subTest(incomplete_callable=source):
                self.assertEqual(receipts.javascript_projection(source)[2], [])
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5'
        source = 'return tools.exec_command({cmd:c});'
        async_source = '(async()=>{await tools.exec_command({cmd:c});})()'
        cells = [
            ('const c=' + json.dumps(writer) + '; try{await Function(' + json.dumps(source) + ')();}catch{}', False),
            ('const c=' + json.dumps(writer) + '; try{await (async()=>{}).constructor(' + json.dumps(source) + ')();}catch{}', False),
            ('const c=' + json.dumps(writer) + '; const indirect=eval; try{await indirect(' + json.dumps(async_source) + ');}catch{}', False),
            ('const c=' + json.dumps(writer) + '; try{await eval?.(' + json.dumps(async_source) + ');}catch{}', False),
            ('const c=' + json.dumps(writer) + '; try{await globalThis["eval"](' + json.dumps(async_source) + ');}catch{}', False),
            ('const c=' + json.dumps(writer) + '; await eval(' + json.dumps(async_source) + ');', True),
            ('const c=' + json.dumps(writer) + '; await Function("c",' + json.dumps(source) + ')(c);', True),
            ('const c=' + json.dumps(writer) + '; await Function("c",' + json.dumps(source) + ')("echo safe");', False),
            ('globalThis.c=' + json.dumps(writer) + '; await Function(' + json.dumps(source) + ')();', True),
            ('globalThis.c=' + json.dumps(writer) + '; const c="echo safe"; await eval(' + json.dumps(async_source) + ');', False),
            ('globalThis.c=' + json.dumps(writer) + '; const c="echo safe"; await Function(' + json.dumps(source) + ')();', True),
            ('await Function(' + json.dumps('c=' + json.dumps(writer)) + ',' + json.dumps(source) + ')();', True),
            ('const c=' + json.dumps(writer) + '; await eval(' + json.dumps('(async()=>{const c="echo safe";await tools.exec_command({cmd:c});})()') + ');', False),
            ('const c=' + json.dumps(writer) + '; await eval("text({cmd:c})");', False),
            ('const c=' + json.dumps(writer) + '; await eval(' + json.dumps('text({"cmd":c})') + ');', False),
            ('const cmd=' + json.dumps(writer) + '; await eval("const object={cmd};text(object.cmd)");', False),
        ]
        for command, owning in ((writer, True), ('echo safe', False)):
            setup = 'const cmds=[' + json.dumps(command) + '];'
            child = 'for(const c of cmds) await eval("text({cmd:c})");'
            cells.extend(((setup + 'globalThis.text=tools.exec_command;' + child, owning),
                          (setup + child, False),
                          (setup + 'await eval("globalThis.text=tools.exec_command");' + child, owning),
                          (setup + 'await eval(' + json.dumps('(async()=>{globalThis.text=tools.exec_command;' + child + '})()') + ');', owning)))
        for command, owning in ((writer, True), ('echo safe', False)):
            assigned = json.dumps(command)
            eval_assignment = 'cmd=' + assigned
            sloppy = ('const cmd="echo safe";function run(){eval('
                      + json.dumps('var cmd=' + assigned) + ');return tools.exec_command({cmd});}return run();')
            cells.extend((
                ('let cmd="echo safe";eval(' + json.dumps(eval_assignment) + ');await tools.exec_command({cmd});', owning),
                ('await Function("c",' + json.dumps('eval(' + json.dumps('c=' + assigned)
                    + ');return tools.exec_command({cmd:c});') + ')("echo safe");', owning),
                ('await Function(' + json.dumps(sloppy) + ')();', owning),
                ('await Function(' + json.dumps('return this["to"+"ols"]["exec_"+"command"]({cmd:' + assigned + '})') + ')();', owning),
                ('await Function(' + json.dumps('"use strict";' + sloppy) + ')();', False),
                ('try{await Function(' + json.dumps('"use strict";return this["to"+"ols"]["exec_"+"command"]({cmd:' + assigned + '})') + ')();}catch{}', False),
                ('const cmd="echo safe";function run(){eval(' + json.dumps('var cmd=' + assigned)
                    + ');return tools.exec_command({cmd});}await run();', False),
            ))
            cells.append(('let c="echo safe";(eval)(' + json.dumps('c=' + assigned)
                          + ');await tools.exec_command({cmd:c});', owning))
            for definition in ('function set(v){globalThis.C=v}',
                               'const set=v=>{globalThis.C=v};',
                               'const set=v=>globalThis.C=v;',
                               'function set(v){Object.assign(globalThis,{C:v})}'):
                prefix = 'const W=' + assigned + ';' + definition
                finish = 'try{await Function("return tools.exec_command({cmd:C})")();}catch{}'
                for use, executes in (('', False), ('set(W);', True),
                                      ('[W].forEach(set);', True), ('eval("set(W)");', True),
                                      ('function launch(){set(W)}', False),
                                      ('function launch(){set(W)}launch();', True),
                                      ('function launch(){eval("set(W)")}', False)):
                    cells.append((prefix + use + finish, owning and executes))
            cells.extend((
                ('const W=' + assigned + ';function set(v){if(false)set(v);globalThis.C=v}'
                 + 'try{await Function("return tools.exec_command({cmd:C})")();}catch{}', False),
                ('const W=' + assigned + ';class Start{static{globalThis.C=W}}'
                 + 'await Function("return tools.exec_command({cmd:C})")();', owning),
            ))
            for setter in ('function set(v){globalThis.C=v}set(CMD);',
                           'for(const w of [CMD])globalThis.C=w;',
                           '[CMD].forEach(w=>{globalThis.C=w});'):
                cells.append((setter.replace('CMD', assigned)
                    + 'await Function("return tools.exec_command({cmd:C})")();', owning))
            for invoke in ('const e=eval;await e(SOURCE);', 'await globalThis.eval(SOURCE);',
                           'await (0,eval)(SOURCE);'):
                indirect_sloppy = sloppy.replace('return run();', 'run();')
                indirect_this = 'this["to"+"ols"]["exec_"+"command"]({cmd:' + assigned + '});'
                cells.extend((
                    (invoke.replace('SOURCE', json.dumps(indirect_sloppy)), owning),
                    (invoke.replace('SOURCE', json.dumps('"use strict";' + indirect_sloppy)), False),
                    (invoke.replace('SOURCE', json.dumps(indirect_this)), owning),
                    (invoke.replace('SOURCE', json.dumps('"use strict";' + indirect_this)), owning),
                ))
            cells.extend((
                ('globalThis.C="echo safe";eval(' + json.dumps('globalThis.C=' + assigned)
                    + ');await eval("tools.exec_command({cmd:globalThis.C})");', owning),
                ('globalThis.C="echo safe";Object.assign(globalThis,{C:' + assigned
                    + '});await eval("tools.exec_command({cmd:globalThis.C})");', owning),
                ('globalThis.C="echo safe";Object.assign(globalThis,{C:' + assigned
                    + '});await Function("return tools.exec_command({cmd:C})")();', owning),
                ('globalThis.C="echo safe";Reflect.set(globalThis,"C",' + assigned
                    + ');await Function("return tools.exec_command({cmd:C})")();', owning),
            ))
            cells.extend((
                ('await Function("v","globalThis.C=v")(' + assigned
                 + ');await Function("return tools.exec_command({cmd:C})")();', owning),
                ('const c=' + assigned + ';eval("globalThis.C=c");'
                 + 'await Function("return tools.exec_command({cmd:C})")();', owning),
                ('const c="echo safe";const fixture=' + assigned
                 + ';globalThis.opts={cmd:c};await Function("return tools.exec_command(globalThis.opts)")();text(fixture);', False),
                ('const c="echo safe";const fixture=' + assigned
                 + ';globalThis.opts={cmd:c};await Function("return tools.exec_command({cmd:globalThis.opts.cmd})")();text(fixture);', False),
            ))
            for mutation in ('globalThis.C=c;', 'Object.assign(globalThis,{C:c});',
                             'Reflect.set(globalThis,"C",c);'):
                for operand in ('C', 'globalThis.C'):
                    child = 'return Function(' + json.dumps('return tools.exec_command({cmd:' + operand + '})') + ')();'
                    cells.append(('await Function("c",' + json.dumps(mutation + child) + ')(' + assigned + ');', owning))
        # Packet selection is shared by ordinary forwarding and native source
        # conversion; actual commands must remain paired with their formals.
        packet_body = json.dumps('return tools.exec_command({cmd:c})')
        for command, owning in ((writer, True), (writer + ' &', True), ('echo safe', False)):
            quoted = json.dumps(command)
            fixture = 'const fixture=' + json.dumps(writer) + ';'
            for call in ('f(c,b)', 'f.call(null,c,b)', 'f.apply(null,[c,b])',
                         'Reflect.apply(f,null,[c,b])', 'f.bind(null,c,b)()'):
                cells.append((fixture + 'const run=(f,c,b)=>' + call
                    + ';run(Function,"c",' + packet_body + ')(' + quoted + ');text(fixture);', owning))
            cells.append((fixture + 'const inner=(f,c,b)=>f(c,b);'
                + 'const run=(f,...args)=>inner(f,...args);run(Function,"c",'
                + packet_body + ')(' + quoted + ');text(fixture);', owning))
            executable = json.dumps('tools.exec_command({cmd:' + quoted + '})')
            cells.append(('const inner=(e,s)=>e(s);const run=(...a)=>inner(...a);run(eval,'
                + executable + ');', owning))
            cells.append(('const name=()=>"ignored";Function("ignored","c",' + packet_body
                + ')("echo safe",' + quoted + ');', owning))
        cells.append(('const name=()=>"ignored";Function(name(),"c",' + packet_body
            + ')("echo safe",' + json.dumps(writer) + ');', True))
        x_body = json.dumps('return tools.exec_command({cmd:x})')
        for first, second, owning in (('echo safe', 'echo safe', False), (writer, 'echo safe', True)):
            cells.append(('const mk=(a,b,c)=>Function(a,b,c);const two=(a,b,c)=>mk(a,b,c);'
                + 'const run=()=>{two("x","y",' + x_body + ')(' + json.dumps(first) + ','
                + json.dumps(writer) + ');two("y","x",' + x_body + ')('
                + json.dumps(writer) + ',' + json.dumps(second) + ')};run();', owning))
        for definition, use in (
            ('const run=async(f,a,b)=>f(a,b);', '.then(g=>g(W))'),
            ('function* run(f,a,b){return f(a,b)}', '.next().value(W)'),
            ('const run=(f,a,b)=>{const g=f(a,b);g(W);return g};', '("echo safe")'),
        ):
            cells.append(('const W=' + json.dumps(writer) + ';' + definition
                + 'run(Function,"c",' + packet_body + ')' + use + ';', True))
        # These paths really may construct and execute the writer. A source
        # certificate must not turn unresolved alternatives into a clear call.
        for setup, invoke in (
            ('let F=Function;if(flag)F=unknown;const run=(f,c,b)=>f(c,b);',
             'run(F,"c",BODY)(W);'),
            ('const run=(f,c,b)=>{if(flag)f=unknown;return f(c,b)};',
             'run(Function,"c",BODY)(W);'),
            ('let mk=(c,b)=>Function(c,b);if(flag)mk=text;',
             'mk("c",BODY)(W);'),
            ('let mk;if(flag)mk=(c,b)=>Function(c,b);',
             'mk("c",BODY)(W);'),
        ):
            cells.append(('const W=' + json.dumps(writer) + ';' + setup
                + invoke.replace('BODY', packet_body), True))
        cells.append(('const run=unknown;run(eval,'
            + json.dumps('tools.exec_command({cmd:' + json.dumps(writer) + '})') + ');', True))
        for cell, owning in cells:
            rows = history('codex', {})
            rows[1]['payload'].update(name='functions.exec', arguments=cell)
            rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
            with self.subTest(cell=cell):
                self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)

        for command, owning in ((writer, True), ('echo safe', False)):
            source = '(async()=>{await tools.exec_command({cmd:' + json.dumps(command) + '});})()'
            cells = []
            for name in ('eval', 'Function', 'text'):
                operand = json.dumps(('return ' if name == 'Function' else '') + source)
                cells.append(('const c=[' + name + '].slice();await c[0](' + operand + ')'
                              + ('()' if name == 'Function' else '') + ';', owning and name != 'text'))
            for name in ('eval', 'Function'):
                operand = json.dumps(('return ' if name == 'Function' else '') + source)
                for callee in ('globalThis.' + name, name):
                    call = 'await ' + callee + '(' + operand + ')' + ('()' if name == 'Function' else '') + ';'
                    for prefix, expected in (
                        ('if(unknown) globalThis.' + name + '=text;', owning),
                        ('function unused(){globalThis.' + name + '=text;}', owning),
                        ('function unused(){globalThis.' + name + '=text;globalThis.' + name + '(' + operand + ')' + ('()' if name == 'Function' else '') + ';}', owning),
                        ('globalThis.' + name + '=unknown;', owning),
                        ('globalThis.' + name + '=text;', False),
                        ('', owning),
                    ):
                        cells.append((prefix + call, expected))
            for callee in ('(0,globalThis.eval)', 'globalThis?.eval', '[globalThis.eval][0]',
                           '(0,globalThis.Function)', 'globalThis?.Function', '[globalThis["Function"]][0]',
                           '(0,globalThis["ev"+"al"])', 'globalThis?.["ev"+"al"]'):
                constructor = 'Function' in callee
                cells.append(('await ' + callee + '(' + json.dumps(('return ' if constructor else '') + source)
                              + ')' + ('()' if constructor else '') + ';', owning))
            for receiver in ('this.eval', 'this?.eval', '(0,this.eval)', '[this.eval][0]',
                             'this["ev"+"al"]', 'this.Function', '(0,this.Function)'):
                constructor = 'Function' in receiver
                inner = receiver + '(' + json.dumps(('return ' if constructor else '') + source) + ')' + ('()' if constructor else '')
                cells.extend((('try{await ' + inner + ';}catch{}', False),
                              ('await Function(' + json.dumps('return ' + inner) + ')();', owning),
                              ('try{await Function(' + json.dumps('"use strict";return ' + inner) + ')();}catch{}', False),
                              ('await(0,eval)(' + json.dumps(inner) + ');', owning),
                              ('await(0,eval)(' + json.dumps('"use strict";' + inner) + ');', owning),
                              ('try{await eval(' + json.dumps(inner) + ');}catch{}', False)))
            for setup, callee, expected in (
                ('const e=globalThis.eval;', 'e', owning),
                ('const g=globalThis;', '(0,g.eval)', owning),
                ('const holder={g:globalThis};', 'holder.g.eval', owning),
                ('const o={};o.run=eval;const p=o;', 'p.run', owning),
                ('const o={};o.run=eval;const {run}=o;', 'run', owning),
                ('const o={};o.run=eval;', '[o][0].run', owning),
                ('const o={};const p=o;o.run=eval;', 'p.run', owning),
                ('const o={};const p=o;const q=p;o.run=eval;', 'q.run', owning),
                ('const o={};const p=[o];o.run=eval;', 'p[0].run', owning),
                ('const o={};const p=(o);o.run=eval;', 'p.run', owning),
                ('const o={};o.run=eval;const p=o;o.run=text;', 'p.run', False),
                ('const o={};o.run=eval;o.run=text;const {run}=o;', 'run', False),
                ('const o={};o.run=eval;const {run}=o;o.run=text;', 'run', owning),
                ('const o={};o.run=text;const {run}=o;o.run=eval;', 'run', False),
                ('const o={};o.run=eval;const run=o.run;o.run=text;', 'run', owning),
                ('const o={};o.run=text;const p=o;', 'p.run', False),
                ('let o={};o.run=eval;const p=o;o={run:text};', 'p.run', owning),
                ('let o={};const p=o;o={};o.run=eval;', 'p.run', False),
                ('let o={child:{}};const p=o.child;o.child.run=eval;', 'p.run', owning),
                ('let o={child:{run:eval}};const p=o.child;o.child={run:text};', 'p.run', owning),
                ('let o={child:{run:eval}};const p=o.child;p.run=text;', 'o.child.run', False),
                ('const o={};const p=o;p.run=eval;', 'o.run', owning),
                ('const api={};api.call=eval;', 'api.call', owning),
                ('function make(){return ()=>{}}const api={};api.call=make();', 'api.call', False),
                ('const api={};api.apply=eval;', 'api.apply', owning),
                ('function make(){return ()=>{}}const api={};api.apply=make();', 'api.apply', False),
                ('const api={};api.bind=eval;', 'api.bind', owning),
                ('function make(){return ()=>{}}const api={};api.bind=make();', 'api.bind', False),
                ('let a={},b={};b.run=eval;function swap(){a=b;b=a}swap();', 'a.run', owning),
                ('let a={},b={};b.run=text;function swap(){a=b;b=a}swap();', 'a.run', False),
                ('let e=globalThis.eval;e=text;', 'e', False),
                ('let e=text;e=globalThis.eval;', 'e', owning),
                ('let e=globalThis.eval;if(flag)e=text;', 'e', owning),
                ('let e=text;if(flag)e=globalThis.eval;', 'e', owning),
                ('let e=globalThis.eval;e=unknown;', 'e', owning),
                ('let e=globalThis.eval;e=getHandle();', 'e', owning),
                ('let g=globalThis;g={eval:text};', 'g.eval', False),
                ('let g={eval:text};g=globalThis;', 'g.eval', owning),
                ('let g=globalThis;if(flag)g={eval:text};', 'g.eval', owning),
                ('let g=globalThis;g=unknown;', 'g.eval', owning),
                ('globalThis.text=eval;let e=eval;e=text;', 'e', owning),
                ('const globalThis={eval:text};', 'globalThis.eval', False),
                ('const o={eval:text};', 'o.eval', False),
                ('const o={eval:text};', 'o?.eval', False),
                ('const o={eval:text};', '(0,o.eval)', False),
                ('const o={eval:text};', '[o.eval][0]', False),
                ('function preview(globalThis){return globalThis;}', 'globalThis.eval', owning),
                ('function preview(){const globalThis={eval:text};return globalThis;}', 'globalThis.eval', owning),
            ):
                cells.append((setup + 'await ' + callee + '(' + json.dumps(source) + ');', expected))
            constructor_body = json.dumps("return tools.exec_command({cmd:" + json.dumps(command) + "});")
            for setup, callee, expected in (
                ("const o={};const F=o.constructor.constructor;", "F", owning),
                ("let F=Function;const o={};F=o.constructor.constructor;", "F", owning),
                ("const f=async()=>{};const F=f.constructor;", "F", owning),
                ("const o={};const F=o.constructor;", "F", False),
                ("const o={constructor:text};const F=o.constructor;", "F", False),
                ("const o={};", "o.constructor", False),
                ("const o={constructor:text};", "o.constructor", False),
                ("const o='safe';const F=o.constructor;", "F", False),
                ("const o='safe';", "o.constructor", False),
                ("const o='safe';const F=o.constructor.constructor;", "F", owning),
                ("const o=1;const F=o.constructor;", "F", False),
                ("const o=1;", "o.constructor", False),
                ("const o=true;const F=o.constructor;", "F", False),
                ("const o=false;", "o.constructor", False),
                ("const o=(/* 'decoy' */ ' ' /* \"decoy\" */);const F=o.constructor;", "F", False),
                ("const undefined=Function;const F=undefined;", "F", owning),
                ("const {value:o}={value:'safe'};const F=o.constructor;", "F", False),
                ("const [o]=['safe'];const F=o.constructor.constructor;", "F", owning),
                ("const String={raw:()=>eval};const F=String.raw`x`;", "F.constructor", owning),
                ("const String={raw:()=>\"safe\"};const F=String.raw`x`;", "F.constructor", False),
                ("const flag=true;let tag=()=>\"safe\";if(flag)tag=(()=>()=>eval)();const F=tag`x`;", "F.constructor", owning),
                ("const flag=true;let tag=()=>\"safe\";if(flag)tag=()=>\"other\";const F=tag`x`;", "F.constructor", False),
                ("const flag=true;if(flag)String.raw=()=>\"safe\";const F=String.raw`x`;", "F.constructor", False),
            ):
                cells.append((setup + "await " + callee + '("tools","text",' + constructor_body + ')(tools,text);', expected))
            # A call result is not an exact callable replacement merely
            # because its expression starts with a function/arrow spelling.
            for replacement in ("(()=>o)()", "function(){return o}()"):
                for invoke in ("o.run(SRC)", "o.run.call(null,SRC)",
                               "o.run.apply(null,[SRC])", "o.run.bind(null)(SRC)"):
                    cells.append(("let o={run:eval};o=" + replacement + ";await "
                                  + invoke.replace("SRC", json.dumps(source)) + ";", owning))
            cells.append(("let o={run:eval};o=()=>{};await o.run(" + json.dumps(source) + ");", False))
            # A definite write through a receiver that may name another
            # object cannot retire this object's earlier source member.
            uncertain = ("const flag=true;let o={run:eval};let alias=o;"
                         "if(flag)alias={run:text};alias.run=text;")
            for invoke in ("o.run(SRC)", "o.run.call(null,SRC)",
                           "o.run.apply(null,[SRC])", "o.run.bind(null)(SRC)"):
                cells.append((uncertain + "await " + invoke.replace("SRC", json.dumps(source)) + ";", owning))
            for setup, invoke, expected in (
                (uncertain + "const {run:e}=o;", "e.call(null,SRC)", owning),
                (uncertain + "const {run:e}=o;", "e.apply(null,[SRC])", owning),
                (uncertain + "const {run:e}=o;", "e.bind(null)(SRC)", owning),
                ("let o={run:eval};const alias=o;alias.run=text;", "o.run(SRC)", False),
                ("let o={run:eval};const alias=o;alias.run=text;const {run:e}=o;", "e.call(null,SRC)", False),
                ("const flag=false;let o={run:text};let alias=o;if(flag)alias={run:text};alias.run=eval;", "o.run(SRC)", owning),
                ("const o=getReceiver();o.run=eval;const alias=o;alias.run=text;", "o.run(SRC)", owning),
            ):
                cells.append((setup + "await " + invoke.replace("SRC", json.dumps(source)) + ";", expected))
            # Destructuring captures the selected value, not its container.
            for handle in ("eval", "text"):
                for setup, invoke in (
                    ("const {run:e}={run:HANDLE};", "e.call(null,SRC)"),
                    ("const [e]=[HANDLE];", "e.apply(null,[SRC])"),
                    ("const [e]=[HANDLE];", "e.bind(null)(SRC)"),
                    ("const {run:e}={run:HANDLE};const alias=e;", "alias.call(null,SRC)"),
                    ("const o={run:HANDLE};const {run:e}=o;o.run=text;", "e.call(null,SRC)"),
                    ("const o={run:eval};o.run=HANDLE;const {run:e}=o;", "e.call(null,SRC)"),
                ):
                    cells.append((setup.replace("HANDLE", handle) + "await "
                                  + invoke.replace("SRC", json.dumps(source)) + ";", owning and handle == "eval"))
            # A shadowable tag can return a source handle even when the result
            # binding has no earlier handle to preserve.
            for setup, invoke, expected in (
                ("const String={raw:()=>eval};let F=String.raw`x`;", "F(SRC)", owning),
                ("const String={raw:()=>eval};let F=eval;F=String.raw`x`;", "F(SRC)", owning),
                ("String.raw=()=>eval;const F=String.raw`x`;", "F(SRC)", owning),
                ("const alias=String;alias.raw=()=>eval;const F=String.raw`x`;", "F(SRC)", owning),
                ("const String={raw:()=>eval};const F=String.raw`x`;const alias=F;", "alias.call(null,SRC)", owning),
                ("const tag=()=>eval;const F=tag`x`;", "F.apply(null,[SRC])", owning),
                ("const tag=()=>({run:eval});const F=tag`x`;", "F.run(SRC)", owning),
                ("const tag=()=>({run:text});const F=tag`x`;", "F.run(SRC)", False),
                ("const tag=()=>{text(eval)};const F=tag`x`;", "F(SRC)", False),
                ("const tag=()=>globalThis.eval;const F=tag`x`;", "F(SRC)", owning),
                ("const tag=()=>eval.call;const F=tag`x`;", "F(null,SRC);", False),
                ("const tag=()=>text.call;const F=tag`x`;", "F(null,SRC);", False),
                ("const tag=()=>eval.call;const F=tag`x`;try{", "F(null,SRC);}catch{}", False),
                ("const tag=()=>text.call;const F=tag`x`;try{", "F(null,SRC);}catch{}", False),
                ("const g=globalThis;g.text=eval;const {run:e}={run:text};", "e.call(null,SRC)", owning),
                ("const g=globalThis;if(flag)g.text=eval;const {run:e}={run:text};", "e.call(null,SRC)", owning),
                ("const String={raw:function(){return eval}};const F=String.raw`x`;", "F(SRC)", owning),
                ("const tag=()=>{if(flag){return eval}return text};const F=tag`x`;", "F(SRC)", owning),
                ("const String={raw:()=>text};const F=String.raw`x`;", "F(SRC)", False),
                ("const String={raw:()=>\"safe\"};const F=String.raw`x`;", "F(SRC)", False),
                ("const String={raw:function(){text(eval);return text}};const F=String.raw`x`;", "F(SRC)", False),
                ("const tag=()=>{function unused(){return eval}return text};const F=tag`x`;", "F(SRC)", False),
                ("const F=String.raw`x`;", "F(SRC)", False),
                ("let F=eval;F=String.raw`x`;", "F(SRC)", False),
            ):
                cells.append((setup + "await " + invoke.replace("SRC", json.dumps(source)) + ";", expected))
            # Calls and tags use the same finite callable/result facts.
            for setup, invoke, expected in (
                ("globalThis.String.raw=()=>eval;const F=String.raw`x`;", "F(SRC)", owning),
                ("let F=eval;globalThis.String.raw=()=>eval;F=String.raw`x`;", "F(SRC)", owning),
                ("window.String.raw=()=>eval;const F=String.raw`x`;", "F(SRC)", owning),
                ("self.String.raw=()=>eval;const F=String.raw`x`;", "F(SRC)", owning),
                ("const S=globalThis.String;S.raw=()=>eval;const F=String.raw`x`;", "F(SRC)", owning),
                ("globalThis.String={raw:()=>eval};const F=String.raw`x`;", "F(SRC)", owning),
                ("const String={raw:()=>eval};const F=globalThis.String.raw`x`;", "F(SRC)", False),
                ("const F=globalThis.String.raw`x`;", "F(SRC)", False),
                ("const tag=async()=>eval;const F=tag`x`;", "F(SRC)", False),
                ("const tag=async()=>eval;const F=await tag`x`;", "F(SRC)", owning),
                ("const tag=async()=>text;const F=await tag`x`;", "F(SRC)", False),
                ("const tag=function*(){return eval};const F=tag`x`;", "F(SRC)", False),
                ("const tag=function*(){return eval};const F=await tag`x`;", "F(SRC)", False),
                ("const tag=async function*(){return eval};const F=await tag`x`;", "F(SRC)", False),
                ("function tag(){return eval}const F=tag`x`;", "F(SRC)", owning),
                ("function tag(){return text}const F=tag`x`;", "F(SRC)", False),
                ("const F=tag`x`;function tag(){return eval}", "F(SRC)", owning),
                ("const String={raw(){return eval}};const F=String.raw`x`;", "F(SRC)", owning),
                ("const String={raw(){return text}};const F=String.raw`x`;", "F(SRC)", False),
                ("const String={async raw(){return eval}};const F=await String.raw`x`;", "F(SRC)", owning),
                ("const String={*raw(){return eval}};const F=String.raw`x`;", "F(SRC)", False),
                ("const tag=()=>{const o={m(){return eval}};return text};const F=tag`x`;", "F(SRC)", False),
                ("const tag=()=>{class C{m(){return eval}}return text};const F=tag`x`;", "F(SRC)", False),
                ("const get=()=>eval;const F=get();", "F(SRC)", owning),
                ("function get(){return eval}const F=get();", "F(SRC)", owning),
                ("const get=()=>text;const F=get();", "F(SRC)", False),
                ("const get=async()=>eval;const F=get();", "F(SRC)", False),
                ("const get=async()=>eval;const F=await get();", "F(SRC)", owning),
                ("const get=function*(){return eval};const F=get();", "F(SRC)", False),
                ("const tag=(s,e)=>e;const F=tag`${eval}`;", "F(SRC)", owning),
                ("const tag=(s,e)=>e;const F=tag`${text}`;", "F(SRC)", False),
                ("const tag=(s,e)=>text;const F=tag`${eval}`;", "F(SRC)", False),
                ("const tag=(s,e)=>e;const F=tag`${'safe'}`;", "F(SRC)", False),
                ("const tag=async(s,e)=>e;const F=await tag`${eval}`;", "F(SRC)", owning),
                ("const tag=async(s,e)=>e;const F=tag`${eval}`;", "F(SRC)", False),
                ("const tag=async(s,e)=>e;const F=await tag`${text}`;", "F(SRC)", False),
                ("const tag=async(s,e)=>text;const F=await tag`${eval}`;", "F(SRC)", False),
                ("const get=e=>e;const F=get(eval);", "F(SRC)", owning),
                ("const get=e=>e;const F=get(text);", "F(SRC)", False),
                ("const get=e=>text;const F=get(eval);", "F(SRC)", False),
                ("const get=async e=>e;const F=await get(eval);", "F(SRC)", owning),
                ("const get=async e=>e;const F=get(eval);", "F(SRC)", False),
                ("const get=async e=>e;const F=await get(text);", "F(SRC)", False),
                ("const get=async e=>text;const F=await get(eval);", "F(SRC)", False),
                ("const get=function*(e){return e};const F=await get(eval);", "F(SRC)", False),
                ("const get=()=>eval.call;const F=get();", "F(null,SRC)", False),
                ("const get=()=>eval.apply;const F=get();", "F(null,[SRC])", False),
                ("const get=()=>eval;const F=get();", "F.bind(null)(SRC)", owning),
                ("const get=()=>text.call;const F=get();", "F(null,SRC)", False),
            ):
                cells.append((setup + "try{await " + invoke.replace("SRC", json.dumps(source)) + ";}catch{}", expected))
            # Parameter, invocation and mutation facts share lexical provenance.
            for cell, executes in (
                ('const get=async()=>{for await(const x of [0]){return eval}};const F=await get();await F(SRC);', True),
                ('const get=async()=>{for await(const x of [0]){return text}};const F=await get();await F(SRC);', False),
                ('const o={await(){return eval}};const F=o.await();await F(SRC);', True),
                ('const String={ async raw(){return eval}};const F=await String.raw`x`;await F(SRC);', True),
                ('const String={a:1, async raw(){return eval}};const F=await String.raw`x`;await F(SRC);', True),
                ('const String={ async *raw(){return eval}};const F=await String.raw`x`;try{await F(SRC)}catch{}', False),
                ('const get=({h})=>h;const F=get({h:eval});await F(SRC);', True),
                ('const get=({h:e})=>e;const F=get({h:eval});await F(SRC);', True),
                ('const get=({h:{e}})=>e;const F=get({h:{e:eval}});await F(SRC);', True),
                ('const get=({h})=>text;const F=get({h:eval});await F(SRC);', False),
                ('const get=(e=eval)=>e;const F=get();await F(SRC);', True),
                ('const get=(e=eval)=>e;const F=get(text);await F(SRC);', False),
                ('const get=({e=eval})=>e;const F=get({});await F(SRC);', True),
                ('const get=({e=eval})=>e;const F=get({e:text});await F(SRC);', False),
                ('const get=([,e])=>e;const F=get([text,eval]);await F(SRC);', True),
                ('const get=(...a)=>a[1];const F=get(text,eval);await F(SRC);', True),
                ('const get=(...a)=>a[1];const F=get(eval,text);await F(SRC);', False),
                ('const tag=(...a)=>a[1];const F=tag`${eval}`;await F(SRC);', True),
                ('const tag=(...a)=>a[1];const F=tag`${text}`;await F(SRC);', False),
                ('const get=({h})=>h.call;const F=get({h:eval});try{await F(null,SRC);}catch{}', False),
                ('const get=({h})=>h.apply;const F=get({h:eval});try{await F(null,[SRC]);}catch{}', False),
                ('const run=(s,e,c)=>e(c);await run`${eval}${SRC}`;', True),
                ('await lib.tag`${eval}${SRC}`;', True),
                ('await lib.tag`${text}${SRC}`;', False),
                ('String.raw`${eval}${SRC}`;', False),
                ('const run=(s,e,c)=>e(c);await run(null,eval,SRC);', True),
                ('const run=(e,c)=>text(c);await run(eval,SRC);', False),
                ('const run=(s,e,c)=>text(c);await run`${eval}${SRC}`;', False),
                ('await eval(null,SRC);', False),
                ('Object.defineProperty(String,"raw",{value:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('let F=eval;Object.defineProperty(String,"raw",{value:()=>eval});F=String.raw`x`;await F(SRC);', True),
                ('Object.assign(String,{raw:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('let F=eval;Object.assign(String,{raw:()=>eval});F=String.raw`x`;await F(SRC);', True),
                ('({a:String.raw}={a:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('({a:{h:String.raw}}={a:{h:()=>eval}});const F=String.raw`x`;await F(SRC);', True),
                ('const S=String;Object.assign(S,{raw:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('Object.assign(globalThis.String,{raw:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('const assign=Object.assign;assign(String,{raw:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('const define=globalThis.Object.defineProperty;define(String,"raw",{value:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('const S=Object.assign(String,{raw:()=>eval});const F=S.raw`x`;await F(SRC);', True),
                ('const S=Object.defineProperty(String,"raw",{value:()=>eval});const F=S.raw`x`;await F(SRC);', True),
                ('Object.assign.call(null,String,{raw:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('Object.assign.apply(null,[String,{raw:()=>eval}]);const F=String.raw`x`;await F(SRC);', True),
                ('Object.assign(String,{other:()=>eval});const F=String.raw`x`;try{await F(SRC)}catch{}', False),
                ('Object.defineProperty(String,"other",{value:()=>eval});const F=String.raw`x`;try{await F(SRC)}catch{}', False),
                ('Object.assign(String,{raw:()=>text});const F=String.raw`x`;await F(SRC);', False),
                ('const Object={assign:()=>text};Object.assign(String,{raw:()=>eval});const F=String.raw`x`;try{await F(SRC)}catch{}', False),
                ('Object.assign=text;Object.assign(String,{raw:()=>eval});const F=String.raw`x`;try{await F(SRC)}catch{}', False),
                ('const String={raw:()=>text};Object.assign(globalThis.String,{raw:()=>eval});const F=String.raw`x`;await F(SRC);', False),
                ('if(flag)Object.assign(String,{raw:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('const F=String.raw`x`;Object.assign(String,{raw:()=>eval});try{await F(SRC)}catch{}', False),
                ('const F=await eval;await F(SRC);', True),
                ('const F=await (await (eval));await F(SRC);', True),
                ('const F=await text;await F(SRC);', False),
            ):
                cells.append((cell.replace("SRC", json.dumps(source)), owning and executes))
            # Independent design findings and lexical capture controls.
            for cell, executes in (
                ('try{await (0,eval.call)(null,SRC)}catch{}', False),
                ('try{await (0,eval.apply)(null,[SRC])}catch{}', False),
                ('await (eval.call)(null,SRC);', True),
                ('const get=()=>{label:{if(true){return eval}}};const F=get();await F(SRC);', True),
                ('const get=()=>{label:{if(true){return text}}};const F=get();await F(SRC);', False),
                ('const c=[eval];const d=c.slice();c[0]=text;await d[0](SRC);', True),
                ('const c=[text];const d=c.slice();c[0]=eval;await d[0](SRC);', False),
                ('const c=[eval];const d=c.slice();d[0]=text;await c[0](SRC);', True),
                ('const c=[eval];const d=c.slice();d[0]=text;await d[0](SRC);', False),
                ('const c={},k=["a"];for(let i=0;i<1;i++){c[k[i]]=1}', False),
                ('const [{a}]=[{a:1}];', False),
                ('const k="raw";const o=String;function f(){return o}f()[k]=()=>eval;const F=String.raw`x`;await F(SRC);', True),
                ('const [e]=[text];await e(SRC);', False),
                ('let F=eval;function g(o){F.x(o)}F=String.raw`x`;try{await F(SRC)}catch{}', False),
                ('const tag=()=>eval.call;const F=tag`x`;try{await F(null,SRC)}catch{}', False),
                ('const get=()=>eval.call;const F=get();try{await F(null,SRC)}catch{}', False),
                ('const get=()=>eval.apply;const F=get();try{await F(null,[SRC])}catch{}', False),
                ('const get=({h})=>h.call;const F=get({h:eval});try{await F(null,SRC)}catch{}', False),
                ('const get=({h})=>h.apply;const F=get({h:eval});try{await F(null,[SRC])}catch{}', False),
                ('const get=({h})=>h;const F=get({h:eval});await F.call(null,SRC);', True),
                ('const F=eval.call.bind(eval);await F(null,SRC);', True),
                ('const get=({h})=>h.call.bind(h);const F=get({h:eval});await F(null,SRC);', True),
                ('const F=eval.apply.bind(eval);await F(null,[SRC]);', True),
                ('Object.defineProperty(String,"raw",{get(){return ()=>eval}});const F=String.raw`x`;await F(SRC);', True),
                ('const k="raw";Object.defineProperty(String,k,{value:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('Object.defineProperties(String,{raw:{value:()=>eval}});const F=String.raw`x`;await F(SRC);', True),
                ('Reflect.set(String,"raw",()=>eval);const F=String.raw`x`;await F(SRC);', True),
                ('Reflect.defineProperty(String,"raw",{value:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('Reflect.apply(Object.assign,null,[String,{raw:()=>eval}]);const F=String.raw`x`;await F(SRC);', True),
                ('Object.assign.bind(null,String)({raw:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('function f(t){Object.assign(t,{raw:()=>eval})}f(String);const F=String.raw`x`;await F(SRC);', True),
                ('Object.defineProperty(String,"raw",{get(){return ()=>text}});const F=String.raw`x`;await F(SRC);', False),
                ('const k="other";Object.defineProperty(String,k,{value:()=>eval});const F=String.raw`x`;try{await F(SRC)}catch{}', False),
                ('const src={raw:()=>eval};Object.assign(String,{...src});const F=String.raw`x`;await F(SRC);', True),
                ('const k="raw";Object.assign(String,{[k]:()=>eval});const F=String.raw`x`;await F(SRC);', True),
                ('const src={other:()=>eval};Object.assign(String,{...src});const F=String.raw`x`;try{await F(SRC)}catch{}', False),
                ('const src={raw:()=>text};Object.assign(String,{...src});const F=String.raw`x`;await F(SRC);', False),
                ('const k="h";let F=text;({[k]:F}={[k]:eval});await F(SRC);', True),
                ('const k="h";let F=text;({wrap:{[k]:F}}={wrap:{[k]:eval}});await F(SRC);', True),
                ('let F={};({...F}={raw:()=>eval});const G=F.raw`x`;await G(SRC);', True),
                ('let F=[];[...F]=[eval];await F[0](SRC);', True),
                ('const get=(a,b)=>b;const F=get(...[text,eval]);await F(SRC);', True),
                ('const args=[text,eval];const get=(a,b)=>b;const F=get(...args);await F(SRC);', True),
                ('const get=(a,b)=>b;const F=get(...[eval,text]);await F(SRC);', False),
                ('const get=({...a})=>a.h;const F=get({h:eval});await F(SRC);', True),
                ('const get=(...a)=>a[0];const F=get(eval);await F(SRC);', True),
                ('const get=({...a})=>a.h;const F=get({h:text});await F(SRC);', False),
                ('let F=eval;F=String.raw`x`;try{await F(SRC)}catch{}', False),
                ('const F=eval.call;try{await F(null,SRC)}catch{}', False),
                ('const F=eval.apply;try{await F(null,[SRC])}catch{}', False),
                ('const F=eval.bind(null);await F(SRC);', True),
                ('eval.bind(null,SRC);', False),
                ('const call=eval.call;await Reflect.apply(call,eval,[null,SRC]);', True),
                ('const call=eval.call;try{await Reflect.apply(call,null,[null,SRC])}catch{}', False),
                ('const call=eval.call;await call.constructor(SRC)();', True),
                ('const get=e=>({h:e});const F=get(eval);await F.h(SRC);', True),
                ('const get=e=>({h:e});const F=get(text);get(eval);await F.h(SRC);', False),
                ('const get=e=>()=>e;const F=get(eval);const G=F();await G(SRC);', True),
                ('const get=e=>()=>e;const F=get(text);get(eval);const G=F();await G(SRC);', False),
                ('const get=e=>e.bind(null);const F=get(eval);await F(SRC);', True),
                ('const get=e=>e.bind(null);const F=get(text);get(eval);await F(SRC);', False),
                ('const run=e=>e;const F=run(text);const unused=run(eval);await F(SRC);', False),
                ('const get=()=>{const o={for(){return eval}};return text};const F=get();await F(SRC);', False),
                ('const get=()=>{const o={if(){return eval}};return text};const F=get();await F(SRC);', False),
                ('const get=()=>{const o={while(){return eval}};return text};const F=get();await F(SRC);', False),
                ('const get=()=>{const o={switch(){return eval}};return text};const F=get();await F(SRC);', False),
                ('const get=()=>{const o={catch(){return eval}};return text};const F=get();await F(SRC);', False),
                ('const get=()=>{const o={with(){return eval}};return text};const F=get();await F(SRC);', False),
                ('const get=()=>{const o={await(){return eval}};return text};const F=get();await F(SRC);', False),
                ('const get=()=>{const o={function(){return eval}};return text};const F=get();await F(SRC);', False),
                ('const get=()=>{const o={m(){return eval}};return text};const F=get();await F(SRC);', False),
                ('const get=()=>{const o={for(x){return eval}};return text};const F=get();await F(SRC);', False),
                ('const o={for(x){return eval}};const F=o.for(0);await F(SRC);', True),
                ('const get=()=>{class C{for(x){return eval}}return text};const F=get();await F(SRC);', False),
                ('const get=()=>{const o={if(x){return eval}};return text};const F=get();await F(SRC);', False),
                ('const o={if(x){return eval}};const F=o.if(0);await F(SRC);', True),
                ('const get=()=>{class C{if(x){return eval}}return text};const F=get();await F(SRC);', False),
                ('const get=()=>{class C{static if(x){return eval}}return text};const F=get();await F(SRC);', False),
            ):
                cells.append((cell.replace("SRC", json.dumps(source)), owning and executes))

            # Reference, capture and loop-phase order share the same facts;
            # opposite directions preserve owning alternatives and clear reads.
            for cell, executes in (
                ('const k="eval";const F=globalThis[k];F(SRC);', True),
                ('const k="eval";globalThis[k](SRC);', True),
                ('const k="eval";(0,globalThis[k])(SRC);', True),
                ('const k="eval";globalThis?.[k](SRC);', True),
                ('const k="eval";const F=globalThis?.[k];F(SRC);', True),
                ('let F=eval;{F(SRC);}F=text;', True),
                ('let F=eval;const o={run:F};F=text;o.run(SRC);', True),
                ('const o={run:eval};const {run:F}=o;o.run=text;F(SRC);', True),
                ('const o={run:text};const {run:F}=Object.assign(o,{run:eval});o.run=eval;F(SRC);', True),
                ('let F=eval;for(const i of [0,1]){F(SRC);F=text;}', True),
                ('const o={run:eval};for(const i of [0,1]){o.run(SRC);o.run=text;}', True),
                ('let F=eval;let i=0;while(i++<2){F(SRC);F=text;}', True),
                ('const o={run:eval};let i=0;while(i++<2){o.run(SRC);o.run=text;}', True),
                ('let F=eval;let i=0;do{F(SRC);F=text;}while(i++<1);', True),
                ('const o={run:eval};let i=0;do{o.run(SRC);o.run=text;}while(i++<1);', True),
                ('let F=eval;for(let i=0;i<2;(F(SRC),i++)){F=text;}', False),
                ('const o={run:eval};for(let i=0;i<2;(o.run(SRC),i++)){o.run=text;}', False),
                ('let F=eval;F=globalThis.getEval();F(SRC);', True),
                ('let F=eval;F=Reflect.get(globalThis,"eval");F(SRC);', True),
                ('let F=eval;F=Reflect.get(globalThis,"text");F(SRC);', False),
                ('let F=eval;F=getEval();F(SRC);', True),
                ('let F=eval;F=(()=>text)();F(SRC);', False),
                ('const c=[eval];c.slice=()=>[text];const d=c.slice();d[0](SRC);', False),
                ('const c=[eval];c.slice=text;const d=c.slice();try{d[0](SRC);}catch{}', False),
                ('const k="text";const F=globalThis[k];F(SRC);', False),
                ('const k="text";globalThis[k](SRC);', False),
                ('const k="text";(0,globalThis[k])(SRC);', False),
                ('const k="text";globalThis?.[k](SRC);', False),
                ('const k="text";const F=globalThis?.[k];F(SRC);', False),
                ('let F=text;{F(SRC);}F=eval;', False),
                ('let F=text;const o={run:F};F=eval;o.run(SRC);', False),
                ('const o={run:text};const {run:F}=o;o.run=eval;F(SRC);', False),
                ('const o={run:text};const {run:F}=Object.assign(o,{run:text});o.run=eval;F(SRC);', False),
                ('let F=text;for(const i of [0,1]){F(SRC);F=eval;}', True),
                ('const o={run:text};for(const i of [0,1]){o.run(SRC);o.run=eval;}', True),
                ('let F=text;let i=0;while(i++<2){F(SRC);F=eval;}', True),
                ('const o={run:text};let i=0;while(i++<2){o.run(SRC);o.run=eval;}', True),
                ('let F=text;let i=0;do{F(SRC);F=eval;}while(i++<1);', True),
                ('const o={run:text};let i=0;do{o.run(SRC);o.run=eval;}while(i++<1);', True),
                ('let F=text;let i=0;while((F(SRC),i++<2)){F=eval;}', True),
                ('const o={run:text};let i=0;while((o.run(SRC),i++<2)){o.run=eval;}', True),
                ('let F=text;let i=0;for(;(F(SRC),i++<2);){F=eval;}', True),
                ('const o={run:text};let i=0;for(;(o.run(SRC),i++<2);){o.run=eval;}', True),
                ('let F=text;for(let i=0;i<2;(F(SRC),i++)){F=eval;}', True),
                ('const o={run:text};for(let i=0;i<2;(o.run(SRC),i++)){o.run=eval;}', True),
                ('let F=text;let i=0;for(F(SRC);i++<2;){F=eval;}', False),
                ('const o={run:text};let i=0;for(o.run(SRC);i++<2;){o.run=eval;}', False),
                ('let F=text;for(const i of (F(SRC),[0,1])){F=eval;}', False),
                ('const o={run:text};for(const i of (o.run(SRC),[0,1])){o.run=eval;}', False),
                ('let F=text;F=globalThis.getEval();F(SRC);', False),
                ('let F=text;F=Reflect.get(globalThis,"eval");F(SRC);', True),
                ('let F=text;F=Reflect.get(globalThis,"text");F(SRC);', False),
                ('let F=text;F=getEval();F(SRC);', False),
                ('let F=text;F=(()=>text)();F(SRC);', False),
                ('const c=[text];c.slice=()=>[eval];const d=c.slice();d[0](SRC);', True),
                ('const c=[text];c.slice=text;const d=c.slice();try{d[0](SRC);}catch{}', False),
                ('const a=[text],b=[text];const d=a.slice();try{d[0]=eval;}catch{}const e=b.slice();e[0](SRC);', False),
                ('const a=[text],b=[text];const d=a.slice();try{d[0]=text;}catch{}const e=b.slice();e[0](SRC);', False),
                ('let F=eval;for(let i=0;i<2;(F(SRC),i++)){if(i===0)continue;F=text;}', True),
                ('const o={run:eval};for(let i=0;i<2;(o.run(SRC),i++)){if(i===0)continue;o.run=text;}', True),
                ('let F=eval;for(let i=0;i<2;(F=eval,F(SRC),i++)){F=text;}', True),
                ('let F=text;for(let i=0;i<2;(F=text,F(SRC),i++)){F=eval;}', False),
                ('const o={run:eval};for(let i=0;i<2;(o.run=eval,o.run(SRC),i++)){o.run=text;}', True),
                ('const o={run:text};for(let i=0;i<2;(o.run=text,o.run(SRC),i++)){o.run=eval;}', False),
                ('let F=eval;for(let i=0;i<0;(F=text,i++)){}F(SRC);', True),
                ('const o={run:eval};for(let i=0;i<0;(o.run=text,i++)){}o.run(SRC);', True),
                ('let F=eval;for(let i=0;i<2;(flag?(F=text):0,F(SRC),i++)){}', True),
                ('const o={run:eval};for(let i=0;i<2;(flag?(o.run=text):0,o.run(SRC),i++)){}', True),
                ('let F=eval;for(let i=0;i<2;(flag&&(F=text),F(SRC),i++)){}', True),
                ('const o={run:eval};for(let i=0;i<2;(flag&&(o.run=text),o.run(SRC),i++)){}', True),
                ('let F=eval;for(let i=0;i<2;((()=>{F=text}),F(SRC),i++)){}', True),
                ('const o={run:eval};for(let i=0;i<2;((()=>{o.run=text}),o.run(SRC),i++)){}', True),
                ('let F=eval;for(let i=0;i<2;(((F=text)),F(SRC),i++)){}', False),
                ('const o={run:eval};for(let i=0;i<2;(((o.run=text)),o.run(SRC),i++)){}', False),
            ):
                cells.append((cell.replace("SRC", json.dumps(source)), owning and executes))

            for inner, executes in (
                ("this.String.raw=()=>eval;const F=String.raw`x`;F(SRC);", True),
                ("this.get=()=>eval;const F=get();F(SRC);", True),
                ('"use strict";this.String.raw=()=>eval;const F=String.raw`x`;F(SRC);', False),
            ):
                child = inner.replace("SRC", json.dumps(source))
                cells.append(("try{await Function(" + json.dumps(child) + ")();}catch{}", owning and executes))

            for handle in ("eval", "text"):
                for reverse in (False, True):
                    definitions = ["function first(){v=v.next;}", "function second(){v=v.next;}",
                                   "function seed(){v={run:" + handle + "};}"]
                    setup = "let v={run:text};" + "".join(reversed(definitions) if reverse else definitions) + "seed();"
                    cells.append((setup + "await v.run(" + json.dumps(source) + ");", owning and handle == "eval"))
            for member, args in (("call", "null," + json.dumps(source)),
                                 ("apply", "null,[" + json.dumps(source) + "]")):
                cells.append(("let o=eval;if(flag)o={" + member + ":eval};await o." + member + "(" + args + ");", owning))
            for setup, callee in (
                ('const g=globalThis;async function run(g){await g.eval(SRC)}await run({eval:text});', ''),
                ('async function run(globalThis){await globalThis.eval(SRC)}await run({eval:text});', ''),
                ('async function run(){const globalThis={eval:text};await globalThis.eval(SRC)}await run();', ''),
                ('const e=eval;async function run(e){await e(SRC)}await run(text);', ''),
            ):
                cells.append((setup.replace('SRC', json.dumps(source)), False))
            for name in ('return', 'new', 'in', 'of', 'delete'):
                for access in ('.' + name, '?.' + name):
                    cells.append(('const o={' + name + ':eval};await o' + access + '(' + json.dumps(source) + ');', owning))
            for cell, expected in cells:
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                with self.subTest(source_handle=cell):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], expected)


    def test_composed_regex_and_nested_templates_keep_executable_calls_visible(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5 > claim.json'
        for command, owning in ((writer, True), (writer + ' &', True), ('echo safe', False)):
            calls = ('text(await tools.exec_command({cmd:' + json.dumps(command) + '}));',
                     "text(await tools.exec_command({cmd:'" + command + "'}));")
            fixture = 'const fixture=' + json.dumps('text(await tools.exec_command({cmd:`' + writer + '`}));') + '; '
            for call in calls:
                for prefix in (r'const re=/^https:\/\//; ', r'const re=/[/*]/; ',
                               "const re=/'/; ", 'const re=/"/; '):
                    for inert in ('', fixture):
                        cell = prefix + inert + call
                        rows = history('codex', {})
                        rows[1]['payload'].update(name='functions.exec', arguments=cell)
                        rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                        with self.subTest(cell=cell):
                            self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)
                            self.assertEqual(list(receipts.shell_texts('functions.exec', cell)), [command])
                            self.assertEqual(list(receipts.native_file_effects('functions.exec', cell)),
                                             list(receipts.native_file_effects('exec_command', {'cmd': command})))
            cell = ('const flag=false; text(`${flag ? `on` : '
                    '(await tools.exec_command({cmd:' + json.dumps(command) + '})).output}`);')
            rows = history('codex', {})
            rows[1]['payload'].update(name='functions.exec', arguments=cell)
            rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
            with self.subTest(cell=cell):
                self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)
                self.assertEqual(list(receipts.shell_texts('functions.exec', cell)), [command])
            for setup, operand in (('', '{}'), ('', '({})'),
                                   ('const holder={return:12}; ', 'holder.return'),
                                   ('const holder={return:12}; ', 'holder. return'),
                                   ('const holder={return:12}; ', 'holder. /* property */ return'),
                                   ('const of=12; ', 'of'), ('const $return=12; ', '$return'),
                                   ('const n=12; ', 'n')):
                cell = (setup + 'const value=' + operand + ' / (await tools.exec_command({cmd:'
                        + json.dumps(command) + '})).exit_code / 2;')
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                with self.subTest(division=cell):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)
                    self.assertEqual(list(receipts.shell_texts('functions.exec', cell)), [command])

    def test_composed_command_discovery_keeps_concatenated_and_interpolated_suffixes(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5 > claim.json'
        for command, owning in ((writer, True), (writer + ' &', True), ('echo safe', False)):
            suffix = json.dumps('; ' + command)
            expressions = (
                'text(await tools.exec_command({cmd:`cat notes.txt` + ' + json.dumps(' && ' + command) + '}));',
                'const cmd=`cat notes.txt` + ' + json.dumps(' && ' + command) + '; text(await tools.exec_command({cmd}));',
                'const stamp=`${Date.now()}`; const prefix="cd /repo"; text(await tools.exec_command({cmd:prefix + ' + suffix + '}));',
                'const half=total / 2; const prefix="cd /repo"; text(await tools.exec_command({cmd:prefix + ' + suffix + '}));',
                'const c=' + json.dumps(command) + '; const v=n // ratio:\n/ (await tools.exec_command({cmd:c})).exit_code / 2;',
            )
            for cell in expressions:
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                with self.subTest(cell=cell):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)

    def test_generated_source_discovery_keeps_imports_and_fixtures_inert(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5 > claim.json'
        fake = 'text(await tools.exec_command({cmd:' + json.dumps(writer) + '}));'
        cells = (
            'const fs=await import("node:fs/promises"); const t=await fs.readFile("skills/github/scripts/gh-plan.py","utf8"); text(t.includes("def claim"));',
            'const fs=await import("node:fs/promises"); const fixture=' + json.dumps(fake) + '; text(!!fs);',
            'await import("./gh-plan.py-claim-fixture.js"); const fixture=' + json.dumps(fake) + ';',
            'await import("gh-plan.py-claim-fixture"); const fixture=' + json.dumps(fake) + ';',
            'class A {constructor(){}} const fixture=' + json.dumps(fake) + ';',
            'const fixture=' + json.dumps(fake) + '; text(({}).constructor);',
            'const half=total / 2; // Function gh-plan.py claim\ntext(half);',
            'const stamp=`${Date.now()}`; const fixture=' + json.dumps(fake) + '; text(stamp);',
            'const half=total / 2; const fixture=' + json.dumps(fake) + '; text(half);',
            'const Constructor=Function; const fixture=' + json.dumps(fake) + '; text(Constructor.name);',
            'await eval("1+1"); const fixture=' + json.dumps(fake) + ';',
            'const metadata=eval;const fixture=' + json.dumps(fake) + ';text(fixture);',
            'const metadata=Function;const fixture=' + json.dumps(fake) + ';text(fixture);',
            'const metadata=[eval];const fixture=' + json.dumps(fake) + ';text(fixture);',
            'const o={e:eval,read:text};const fixture=' + json.dumps(fake) + ';o.read(fixture);',
            'const o={eval:text};o.eval(' + json.dumps(fake) + ');',
            'const o={eval:text};o.eval("text(1)");',
            'const o={eval:text};o. eval(' + json.dumps(fake) + ');',
            'const o={eval:text};o.\neval(' + json.dumps(fake) + ');',

        )
        for cell in cells:
            rows = history('codex', {})
            rows[1]['payload'].update(name='functions.exec', arguments=cell)
            rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
            with self.subTest(cell=cell):
                self.assertFalse(receipts.ownership_state(rows, 'codex')['pending'])
                self.assertEqual(list(receipts.shell_texts('functions.exec', cell)), [])
        for handle in ('eval', 'Function', '[eval]', '{e:eval}'):
            cell = ('const metadata=' + handle + ';const fixture=' + json.dumps(fake)
                    + ';await tools.exec_command({cmd:"cat notes.txt"});text(fixture);')
            rows = history('codex', {})
            rows[1]['payload'].update(name='functions.exec', arguments=cell)
            rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
            with self.subTest(unused_handle=cell):
                self.assertFalse(receipts.ownership_state(rows, 'codex')['pending'])
                self.assertEqual(list(receipts.shell_texts('functions.exec', cell)), ['cat notes.txt'])
        for command, owning in ((writer, True), (writer + ' &', True),
                                ('echo safe', False), ('echo ' + shlex.quote(writer), False)):
            source = '(async()=>{text(await tools.exec_command({cmd:' + json.dumps(command) + '}));})()'
            for cell in ('await eval(' + json.dumps(source) + ');',
                         'await Function("tools","text",' + json.dumps('return ' + source) + ')(tools,text);',
                         'const Constructor=Function; await Constructor("tools","text",' + json.dumps('return ' + source) + ')(tools,text);',
                         'const source=' + json.dumps(source) + '; await eval(source);',
                         'const suffix=""; const source=`' + source + '${suffix}`; await eval(source);',
                         'const Constructor=(async()=>{}).constructor; await Constructor("tools","text",' + json.dumps(source) + ')(tools,text);'):
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                with self.subTest(generated_source=cell):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)

    def test_dynamic_javascript_literals_cannot_hide_ownership(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5 > claim.json'
        for command, owning in ((writer, True), (writer + ' &', True), ('echo safe', False)):
            call = 'text(await tools.exec_command({cmd:' + json.dumps(command) + '}));'
            source = '(async()=>{' + call + '})()'
            for cell in (
                'const a=[eval];await a[0](' + json.dumps(source) + ');',
                'const {e}={e:eval};await e(' + json.dumps(source) + ');',
                'const o={e:eval};await o["e"](' + json.dumps(source) + ');',
                'const o = {\n e:eval,\n};\nawait o.e(' + json.dumps(source) + ');',
                'const [F]=[Function];await F(' + json.dumps('return ' + source) + ')();',
                'await(0,eval)(' + json.dumps(source) + ');',
                'const run=(f,s)=>f(s);await run(eval,' + json.dumps(source) + ');',
                'const run=(f,...args)=>f(...args);await run(Function,"c",'
                + json.dumps('return tools.exec_command({cmd:c})') + ')(' + json.dumps(command) + ');',
                'const inner=(f,c,b)=>f(c,b);const run=(f,...args)=>inner(f,...args);'
                + 'run(Function,"c",' + json.dumps('return tools.exec_command({cmd:c})')
                + ')(' + json.dumps(command) + ');',
                'let e;e=eval;await e(' + json.dumps(source) + ');',
                'const o={e:eval};await o.e(' + json.dumps(source) + ');',
                'await Reflect.construct(Function,[' + json.dumps('return ' + source) + '])();',
                'await (0,eval)(' + json.dumps(source) + ');',
                'await eval?.(' + json.dumps(source) + ');',
                'await globalThis["eval"](' + json.dumps(source) + ');',
                'await globalThis["ev"+"al"](' + json.dumps(source) + ');',
                'await globalThis["Function"](' + json.dumps('return ' + source) + ')();',
                'await eval.call(null,' + json.dumps(source) + ');',
                'await eval.apply(null,[' + json.dumps(source) + ']);',
                'await Reflect.apply(eval,null,[' + json.dumps(source) + ']);',
                'await [eval][0](' + json.dumps(source) + ');',
                'await Function.call(null,' + json.dumps('return ' + source) + ')();',
                'await Function.apply(null,[' + json.dumps('return ' + source) + '])();',
                'await Reflect.apply(Function,null,[' + json.dumps('return ' + source) + '])();',
                'await [Function][0](' + json.dumps('return ' + source) + ')();',
                'await eval(' + json.dumps(source) + ');',
                'await Function("tools", "text", ' + json.dumps('return ' + source) + ')(tools,text);',
                'const AsyncFunction=(async()=>{}).constructor; await new AsyncFunction("tools", "text", '
                + json.dumps(call) + ')(tools,text);',
                'await new (async()=>{}).constructor("tools", "text", ' + json.dumps(call) + ')(tools,text);',
                'await eval(' + json.dumps(source.replace('gh-plan.py', r'gh-plan\x2epy')) + ');',
            ):
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                with self.subTest(cell=cell):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], owning)

    def test_quoted_patch_calls_do_not_fabricate_file_writes(self):
        patch = '*** Begin Patch\n*** Add File: /repo/fixture.py\n+print("fixture")\n*** End Patch'
        call = 'text(await tools.apply_patch(' + json.dumps(patch) + '));'
        direct = list(receipts.native_file_effects('apply_patch', patch))
        self.assertTrue(any(effect['kind'] == 'write' for effect in direct))
        self.assertEqual(list(receipts.native_file_effects('functions.exec', call)), direct)
        for cell in ('const fixture=`' + call + '`;',
                     'const fixture=' + json.dumps(call) + ';',
                     '// ' + call + '\n', '/* ' + call + ' */'):
            with self.subTest(cell=cell):
                self.assertFalse(any(effect['kind'] == 'write'
                                     for effect in receipts.native_file_effects('functions.exec', cell)))
        writer = 'uv run /catalog/gh-plan.py --repo o/r claim 5'
        patch = '*** Begin Patch\n*** Add File: /repo/run.py\n+import os\n+os.system(' + repr(writer) + ')\n*** End Patch'
        safe_patch = '*** Begin Patch\n*** Add File: /repo/run.py\n+print("safe")\n*** End Patch'
        for cell, writes in (('const p=' + json.dumps(safe_patch) + ';eval(' + json.dumps('const unused=' + json.dumps(patch)) + ');await tools.apply_patch(p);', True),
                             ('const p=' + json.dumps(patch) + ';text(p);', False),
                     ('const unused=' + json.dumps(patch) + ';await tools.exec_command({cmd:"echo safe"});', False),
                     ('globalThis.P=' + json.dumps(patch) + ';text(globalThis.P);', False),
                     ('globalThis["P"]=' + json.dumps(patch) + ';text(globalThis["P"]);', False),
                     ('globalThis.P=' + json.dumps(patch) + ';await tools.exec_command({cmd:"echo safe"});', False),
                     ('globalThis.P=' + json.dumps(patch) + ';const P=' + json.dumps(patch.replace("import os\n+os.system(" + repr(writer) + ")", "print(\"safe\")")) + ';const ap=tools.apply_patch;await ap(P);', True)):
            rows = history('codex', {}, {})
            rows[1]['payload'].update(name='functions.exec', arguments=cell)
            rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
            rows[3]['payload']['arguments'] = json.dumps({'cmd': 'python3 /repo/run.py'})
            rows[4] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-1')
            with self.subTest(inert_patch=cell):
                self.assertEqual(any(effect['kind'] == 'write' for effect in receipts.native_file_effects('functions.exec', cell)), writes)
                self.assertFalse(receipts.ownership_state(rows, 'codex')['pending'])

    def test_composed_fixture_strings_and_comments_are_not_tool_invocations(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5 > claim.json'
        fake = 'text(await tools.exec_command({cmd:`' + writer + '`}));'
        read = 'text(await tools.exec_command({cmd:"echo safe"}));'
        patch_text = ('*** Begin Patch\n*** Add File: /repo/fixture.py\n'
                      '+def fixture():\n+    cell = ' + repr(fake) + '\n*** End Patch')
        cells = (
            'const fixture=' + json.dumps(fake) + '; ' + read,
            'text(await tools.apply_patch(' + json.dumps(patch_text) + ')); ' + read,
            '// ' + fake + '\n' + read,
            '/* ' + fake + ' */ ' + read,
            'const fixture=`text(await tools.exec_command({cmd:"' + writer + '"}));`; ' + read,
            'const fixture=String.raw`text(await tools.exec_command({cmd:"' + writer + '"}));`; ' + read,
        )
        for cell in cells:
            rows = history('codex', {})
            rows[1]['payload'].update(name='functions.exec', arguments=cell)
            rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
            with self.subTest(cell=cell):
                self.assertEqual(list(receipts.shell_texts('functions.exec', cell)), ['echo safe'])
                self.assertFalse(receipts.ownership_state(rows, 'codex')['pending'])
        for cell in (
            '`${await tools.exec_command({cmd:' + json.dumps(writer) + '})}`',
            'const fixture=' + json.dumps(fake) + '; text(await tools.exec_command({cmd:' + json.dumps(writer) + '}));',
            'const fixture=' + json.dumps(fake) + '; text(await tools.exec_command({cmd:' + json.dumps(writer + ' &') + '}));',
            '// inert\u2028text(await tools.exec_command({cmd:' + json.dumps(writer) + '}));',
        ):
            rows = history('codex', {})
            rows[1]['payload'].update(name='functions.exec', arguments=cell)
            rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
            with self.subTest(owning_cell=cell):
                self.assertTrue(receipts.ownership_state(rows, 'codex')['pending'])
                self.assertTrue(list(receipts.shell_texts('functions.exec', cell)))

    def test_static_composed_templates_bind_only_fresh_native_results(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 5 > claim.json'
        forms = ('`' + writer + '`', 'String.raw`' + writer + '`',
                 '`' + writer.replace('gh-plan.py', 'gh-plan\\x2epy') + '`',
                 '`' + writer.replace('gh-plan.py', 'gh-plan\\u002epy') + '`')
        for literal in forms:
            for suffix, pending in (('', False), (' &', True)):
                command = literal[:-1] + suffix + literal[-1:]
                cell = 'text(await tools.exec_command({cmd:' + command + '}));'
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                with self.subTest(literal=literal, suffix=suffix):
                    self.assertEqual(receipts.ownership_state(rows, 'codex')['pending'], pending)
            for cell in (
                'await tools.exec_command({cmd:' + literal + '});',
                'text((await tools.exec_command({cmd:' + literal + '})).output);',
                'text(await tools.exec_command({cmd:' + literal + '}));'
                + 'await tools.exec_command({cmd:' + literal + '});',
            ):
                rows = history('codex', {})
                rows[1]['payload'].update(name='functions.exec', arguments=cell)
                rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
                self.assertTrue(receipts.ownership_state(rows, 'codex')['pending'], cell)
        rows = history('codex', {})
        rows[1]['payload'].update(name='functions.exec', arguments=
            'String.raw=()=>"uv run /catalog/gh-plan.py claim 5"; '
            'text(await tools.exec_command({cmd:String.raw`echo safe`}));')
        rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
        self.assertTrue(receipts.ownership_state(rows, 'codex')['pending'])
        for literal in ('`uv run /catalog/gh-plan.py claim ${number}`',
                        'String.raw`uv run /catalog/gh-plan.py claim ${number}`',
                        '`uv run /catalog/gh-plan.py claim 1\\xQZ`'):
            rows = history('codex', {})
            rows[1]['payload'].update(name='functions.exec', arguments=
                'text(await tools.exec_command({cmd:' + literal + '}));')
            rows[2] = output({'exit_code': 0, 'output': ''}, 'codex', 'tool-0')
            self.assertTrue(receipts.ownership_state(rows, 'codex')['pending'], literal)

    def test_composed_strings_preserve_javascript_bytes_and_file_effects(self):
        for literal, expected in (
            ('`one\\ntwo`', 'one\ntwo'), ('String.raw`one\\ntwo`', 'one\\ntwo'),
            ('`echo \\`quiet\\``', 'echo `quiet`'),
            ('String.raw`echo \\`quiet\\``', 'echo \\`quiet\\`'),
            ('`echo \\${literal}`', 'echo ${literal}'),
            ('String.raw`echo \\${literal}`', 'echo \\${literal}'),
            ('`\\u{1F600} \\uD83D\\uDE00`', '\U0001f600 \U0001f600'),
            ('`a\r\nb\rc`', 'a\nb\nc'),
            ('`a\\\nb`', 'ab'), ('String.raw`a\\\nb`', 'a\\\nb'),
        ):
            cell = 'text(await tools.exec_command({cmd:' + literal + '}));'
            with self.subTest(literal=literal):
                self.assertEqual(list(receipts.shell_texts('functions.exec', cell)), [expected])
                self.assertEqual(receipts.composed_calls(cell)[0][1]['cmd'], expected)
        command = "cat > fixture.py <<'PY'\nwriter = 'uv run gh-plan.py claim 1'\nPY"
        direct = list(receipts.native_file_effects('exec_command', {'cmd': command}))
        for literal in ('`' + command + '`', 'String.raw`' + command + '`'):
            cell = 'text(await tools.exec_command({cmd:' + literal + ',workdir:"/repo"}));'
            self.assertEqual(list(receipts.native_file_effects('functions.exec', cell)),
                             [dict(effect, cwd='/repo') for effect in direct])

    def test_unparsed_composed_ownership_cannot_become_no_ownership(self):
        for cell in ('const cmd = "gh pr create --repo client/repo > pr.json"; text(await tools.exec_command({cmd:cmd}));',
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
                            "nohup gh pr create --title x &",
                            "coproc uv run scripts/gh-plan.py --repo example/a claim 5",
                            "coproc JOB { uv run scripts/gh-plan.py --repo example/a claim 5; }",
                            "gen() { uv run scripts/gh-plan.py --repo example/a claim 5; }; coproc gen",
                            "uv run scripts/gh-plan.py --repo example/a claim 5; ((nohup ./writer.sh &) &)"):
                rows = history(harness, claim(), {**proof(), "harness": harness})
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                self.assertFalse(session_record.summarize(rows, harness)["safe_verdict"])
                self.assertTrue(receipts.ownership_state(rows, harness)["pending"])
            # Quoted data and ordinary foreground nohup are not detachment.
            for command in ('nohup gh pr create --title "&"', "nohup gh pr create --title coproc"):
                if harness == "codex":
                    rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    rows[1]["message"]["content"][0]["input"] = {"command": command}
                self.assertTrue(session_record.summarize(rows, harness)["safe_verdict"])

    def test_visible_composed_ownership_can_finish_through_an_outer_cell_poll(self):
        rows = history("codex", claim(), proof())
        rows[1]["payload"].update(name="functions.exec", arguments='text(await tools.exec_command({cmd:"uv run scripts/gh-plan.py --repo example/a claim 5"}));')
        rows[2] = output(cell_header('owning-cell')['text'], "codex", "tool-0")
        poll = {"type": "response_item", "payload": {"type": "function_call", "name": "functions.wait", "call_id": "wait",
                "arguments": json.dumps({"cell_id": "owning-cell"})}}
        rows[3:3] = [poll, output({"exit_code": 0, "output": ""}, "codex", "wait")]
        self.assertTrue(session_record.summarize(rows, "codex")["safe_verdict"])
        rows[4] = output("", "codex", "wait")
        self.assertFalse(session_record.summarize(rows, "codex")["safe_verdict"])
        # Each wait returns only new output, so preserve earlier visible results.
        rows[1]["payload"]["arguments"] += 'text(await tools.exec_command({cmd:"gh pr create --title x"}));'
        rows[2] = {"payload": {"type": "custom_tool_call_output", "call_id": "tool-0", "output": [
            cell_header('owning-cell'),
            {"type": "input_text", "text": json.dumps({"exit_code": 0, "output": json.dumps({"exit_code": 0})})}]}}
        rows[4] = {"payload": {"type": "custom_tool_call_output", "call_id": "wait", "output": [
            cell_header(),
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
        for deferred in (False, True):
            records = history("codex", claim())
            checked = composed_result({'exit_code': 0, 'output': json.dumps(proof(("example/a", 1, 10)))}, 'launch')
            additions = [call, checked]
            if deferred:
                checked['payload']['call_id'] = 'outer-wait'
                checked['payload']['output'].insert(0, cell_header())
                additions = [call, {'payload': {'type': 'function_call_output', 'call_id': 'launch', 'output': [
                    cell_header('checker-cell')]}},
                    {'payload': {'type': 'function_call', 'name': 'functions.wait', 'call_id': 'outer-wait',
                                 'arguments': json.dumps({'cell_id': 'checker-cell'})}}, checked]
            records[-2:-2] = additions
            with self.subTest(single_checker_outer_wait=deferred):
                self.assertTrue(session_record.summarize(records, 'codex')['safe_verdict'])

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
                with patch.object(release_session_claims, "inventory", return_value=[]):
                    result = release_session_claims.validate_closure(entry, "supervisor-fixture")
                self.assertTrue(result["declared_safe_verdict"])
                self.assertFalse(result["safe_verdict"])
                self.assertIsNone(finished_map.closeout_request(entry, result))

    def test_a_claim_write_without_its_source_receipt_is_not_clear(self):
        value = {"operation": receipts.CLAIM_OPERATION, "claim": {"session": SESSION}, "ok": False,
                 "write_outcome": "unknown"}
        self.assertFalse(session_record.summarize(history("codex", value), "codex")["safe_verdict"])


    def test_claim_options_before_operand_preserve_exact_or_unresolved_scope(self):
        options = "--worker w --session native-fixture --branch b --next-action n"
        cases = ((f"{options} client/repo#7", ("client/repo", 7)),
                 (f"{options} $ISSUE", (None, None)),
                 (f"{options} $OWNER/repo#7", (None, None)),
                 (f"{options} 7", (None, 7)),
                 (options, (None, None)),
                 ("--worker=w --session=native-fixture --branch=b --next-action=n -- client/repo#7", ("client/repo", 7)))
        for harness in ("codex", "claude"):
            for action, target in cases:
                with self.subTest(harness=harness, action=action):
                    rows = history(harness, claim(), {})
                    command = "uv run /catalog/gh-plan.py claim " + action + " > claim.json"
                    if harness == "codex":
                        rows[3]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[3]["message"]["content"][0]["input"] = {"command": command}
                    rows[4] = output({"exit_code": 0, "output": ""}, harness, "tool-1")
                    self.assertIn({"invocation": "tool-1", "repository": target[0], "number": target[1]},
                                  receipts.claim_targets(rows, harness))
                    self.assertIn(("example/a", 1), receipts.target_records(rows, harness))
                    if target[0]:
                        self.assertIn(target[0], receipts.target_repositories(rows, harness))
            rows = history(harness, claim("client/repo", 7))
            command = "uv run /catalog/gh-plan.py claim " + options + " 7"
            if harness == "codex":
                rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
            else:
                rows[1]["message"]["content"][0]["input"] = {"command": command}
            self.assertEqual(receipts.target_records(rows, harness), {("client/repo", 7)})


    def test_only_an_isolated_claim_can_supply_its_own_unknown_operand_scope(self):
        for harness in ("codex", "claude"):
            for operand in ("$ISSUE", "7"):
                for wrapper in ("{}", "cd /repo && {} 2>&1 | tee claim.json"):
                    with self.subTest(harness=harness, operand=operand, wrapper=wrapper):
                        rows = history(harness, claim("client/repo", 7))
                        command = wrapper.format("uv run /catalog/gh-plan.py claim --worker w " + operand)
                        if harness == "codex":
                            rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                        else:
                            rows[1]["message"]["content"][0]["input"] = {"command": command}
                        self.assertEqual(receipts.target_records(rows, harness), {("client/repo", 7)})
            for operand, number in (("$ISSUE", None), ("7", 7)):
                for suffix, result in ((" > claim.json; cat old.json", claim("client/repo", 7)),
                                       ("", [claim("client/repo", 7), claim("other/repo", 7)])):
                    with self.subTest(harness=harness, operand=operand, suffix=suffix):
                        rows = history(harness, claim(), {})
                        command = "uv run /catalog/gh-plan.py claim --worker w " + operand + suffix
                        if harness == "codex":
                            rows[3]["payload"]["arguments"] = json.dumps({"cmd": command})
                        else:
                            rows[3]["message"]["content"][0]["input"] = {"command": command}
                        rows[4] = output(result, harness, "tool-1")
                        self.assertIn({"invocation": "tool-1", "repository": None, "number": number},
                                      receipts.claim_targets(rows, harness))


    def test_numeric_claim_variable_repository_needs_its_own_canonical_destination(self):
        for harness in ("codex", "claude"):
            for option in ('--repo "$REPO"', '--repo=${REPO}'):
                for canonical in (True, False):
                    with self.subTest(harness=harness, option=option, canonical=canonical):
                        rows = history(harness, claim("client/repo", 7) if canonical else {})
                        command = "uv run /catalog/gh-plan.py " + option + " claim --worker w 7"
                        if harness == "codex":
                            rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                        else:
                            rows[1]["message"]["content"][0]["input"] = {"command": command}
                        self.assertEqual(receipts.target_records(rows, harness),
                                         {("client/repo" if canonical else None, 7)})
                        self.assertEqual(receipts.target_repositories(rows, harness),
                                         {"client/repo"} if canonical else set())


    def test_only_claim_destinations_require_repository_inventory(self):
        for harness in ("codex", "claude"):
            for command in (
                "gh issue view -R upstream/x 5",
                "gh pr view https://github.com/upstream/x/pull/9",
                "uv run gh-plan.py --repo upstream/x show 1",
                "gh pr create --repo upstream/x",
                "uv run gh-pr.py --repo upstream/x create",
                "uv run gh-plan.py --repo example/a claim 1 --admission-link https://github.com/upstream/x/issues/9",
            ):
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {})
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertEqual(receipts.target_repositories(rows, harness),
                                     {"example/a"} if "claim 1" in command else set())

    def test_serial_numeric_claim_scope_uses_its_own_canonical_native_result(self):
        rows = history("codex", claim("client/repo", 7))
        rows[1]["payload"].update(name="functions.exec", arguments=
            'text(await tools.exec_command({cmd:"git status --short"}));'
            'text(await tools.exec_command({cmd:"cat README.md"}));'
            'text(await tools.exec_command({cmd:"uv run gh-plan.py claim 7"}));')
        wrappers = [{"exit_code": 0, "output": "clean"}, {"exit_code": 0, "output": "read"},
                    {"exit_code": 0, "output": json.dumps(claim("client/repo", 7))}]
        rows[2]["payload"]["output"] = [{"type": "input_text", "text": json.dumps(value)} for value in wrappers]
        self.assertEqual(receipts.target_records(rows, "codex"), {("client/repo", 7)})
        self.assertFalse(receipts.unscoped_claim_invocations(rows, "codex"))
        # Dropped native prints cannot donate another result's canonical scope.
        rows[2]["payload"]["output"].pop(0)
        self.assertEqual(receipts.target_records(rows, "codex"), {(None, 7)})

    def test_numeric_claim_scope_cannot_borrow_mixed_poll_output(self):
        rows = history("codex", claim("foreign/repo", 7))
        rows[1]["payload"]["arguments"] = json.dumps({"cmd": "uv run gh-plan.py claim 7"})
        rows[2] = output({"session_id": 123}, "codex", "tool-0")
        for cell in (
            'text(await tools.write_stdin({session_id:123})); text(await tools.exec_command({cmd:"cat saved-claim.json"}));',
            'text(await tools.write_stdin({session_id:123})); text(await tools.write_stdin({session_id:456}));',
        ):
            with self.subTest(cell=cell):
                poll = {"payload": {"type": "custom_tool_call", "name": "functions.exec", "call_id": "poll", "input": cell}}
                mixed = rows[:3] + [poll, output(claim("foreign/repo", 7), "codex", "poll")] + rows[3:]
                self.assertEqual(receipts.target_records(mixed, "codex"), {(None, 7)})
        poll = {"payload": {"type": "function_call", "name": "write_stdin", "call_id": "poll", "arguments": '{"session_id":123}'}}
        isolated = rows[:3] + [poll, output(claim("foreign/repo", 7), "codex", "poll")] + rows[3:]
        self.assertEqual(receipts.target_records(isolated, "codex"), {("foreign/repo", 7)})

    def test_numeric_claim_scope_requires_its_native_session_identity(self):
        for harness in ("codex", "claude"):
            rows = history(harness, claim("foreign/repo", 7))
            value = claim("foreign/repo", 7)
            value["claim"]["session"] = "another-native-session"
            rows[2] = output(value, harness, "tool-0")
            if harness == "codex":
                rows[1]["payload"]["arguments"] = json.dumps({"cmd": "uv run gh-plan.py claim 7"})
            else:
                rows[1]["message"]["content"][0]["input"] = {"command": "uv run gh-plan.py claim 7"}
            self.assertEqual(receipts.target_records(rows, harness), {(None, 7)})



    def test_abbreviated_claim_options_preserve_the_literal_issue_operand(self):
        parser = receipts.helper_parser("gh-plan.py")
        claim_parser = next(item for item in parser._actions if isinstance(item, argparse._SubParsersAction)).choices["claim"]
        added_required = "--fixture-required"
        while added_required in claim_parser._option_string_actions:
            added_required += "-extra"
        claim_parser.add_argument(added_required, required=True)
        self.addCleanup(receipts.helper_parser.cache_clear)
        positional = next(item for item in claim_parser._actions if not item.option_strings)
        required = parser_required_arguments(claim_parser)
        options = [[]]
        optional = [item for item in claim_parser._actions if item.option_strings
                    and not item.required and item.nargs is None]
        for action in optional[:3]:
            values = parser_fixture_value(action)
            prefix = shortest_parser_option(claim_parser, action.dest, [*values, *required])
            options.extend(([prefix, *values], [prefix + "=" + values[0]]))
        repository = shortest_parser_option(parser, "repo", ["example/a", "show", "1"])
        for harness in ("codex", "claude"):
            for option in options:
                argv = [repository, "example/a", "claim", *option, *required]
                namespace, unknown = parser.parse_known_args(argv)
                self.assertFalse(unknown)
                with self.subTest(harness=harness, argv=argv):
                    rows = history(harness, {})
                    command = shlex.join(["uv", "run", "gh-plan.py", *argv]) + " > claim.json"
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertEqual(receipts.target_records(rows, harness), {(namespace.repo, int(getattr(namespace, positional.dest)))})
            for repo_option in ([repository, "example/a"], [repository + "=example/a"]):
                argv = ["uv", "run", "gh-plan.py", *repo_option, "claim", *options[-1], *required]
                inner = shlex.join(argv)
                for command in (inner, "bash -c " + shlex.quote(inner),
                                "python3 -c " + shlex.quote("import subprocess; subprocess.run(" + repr(argv) + ")")):
                    with self.subTest(harness=harness, command=command):
                        rows = history(harness, {})
                        if harness == "codex":
                            rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                        else:
                            rows[1]["message"]["content"][0]["input"] = {"command": command}
                        rows[2] = output({"exit_code": 0, "output": ""}, harness, "tool-0")
                        namespace, _ = parser.parse_known_args(argv[3:])
                        self.assertEqual(receipts.target_records(rows, harness), {(namespace.repo, int(getattr(namespace, positional.dest)))})
                        self.assertFalse(receipts.ownership_state(rows, harness)["pending"])


    def test_grouped_stdin_streams_reach_only_connected_code_readers(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        headers = (
            ("(cat) <<'EOF' | bash", True),
            ("(cat)<<'EOF' | bash", True),
            ("(cat <<'EOF') | bash", True),
            ("{ cat <<'EOF'; } | bash", True),
            ("cat <<'EOF' | (cd /repo && cat) | bash", True),
            ("cat <<'EOF' | { cd /repo; cat; } | bash", True),
            ("cat <<'EOF' | (cat | cat) | bash", True),
            ("(gh issue comment 1 --body-file -) <<'EOF' | bash", False),
            ("(gh issue comment 1 --body-file -)<<'EOF' | bash", False),
            ("cat <<'EOF' | (cd /repo && gh issue comment 1 --body-file -) | bash", False),
            ("cat <<'EOF' | (cd /repo && cat </dev/null) | bash", False),
            ("cat <<'EOF' | (cd /repo && cat >handoff.md) | bash", False),
        )
        for harness in ("codex", "claude"):
            for header, pending in headers:
                with self.subTest(harness=harness, header=header):
                    rows = history(harness, {})
                    command = header + "\n" + writer + "\nEOF"
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)

    def test_grouped_and_tee_outputs_preserve_their_own_script_provenance(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        producers = (
            ("cat <<'EOF' | (cat > run.sh)\n" + writer + "\nEOF", True),
            ("cat <<'EOF' | (cd /repo && cat) > run.sh\n" + writer + "\nEOF", True),
            ("tee run.sh <<'EOF' >/dev/null\n" + writer + "\nEOF", True),
            ("cat <<'EOF' | tee run.sh\n" + writer + "\nEOF", True),
            ("cat <<'EOF' | (cd /repo && gh issue comment 1 --body-file -) > run.sh\n" + writer + "\nEOF", False),
            ("cat > handoff.md <<'EOF'\n" + writer + "\nEOF\ncat harmless.txt | tee run.sh", False),
            ("tee --help run.sh <<'EOF'\n" + writer + "\nEOF", False),
        )
        for harness in ("codex", "claude"):
            for command, pending in producers:
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {}, {})
                    for index, text in ((1, command), (3, "bash run.sh")):
                        if harness == "codex":
                            rows[index]["payload"]["arguments"] = json.dumps({"cmd": text})
                        else:
                            rows[index]["message"]["content"][0]["input"] = {"command": text}
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)


    def test_adjacent_shell_quote_fragments_remain_one_raw_word(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        python = 'import os; os.system(' + repr(writer) + ')'
        commands = (
            ("python3 <<< " + shlex.quote(python), True),
            ("gh issue comment 1 --body-file - <<< " + shlex.quote(python), False),
            ("bash <<< 'uv '" + '"run "' + "'/catalog/gh-plan.py --repo client/repo claim 12'", True),
            ("gh issue comment 1 --body-file - <<< 'uv '" + '"run "' + "'/catalog/gh-plan.py --repo client/repo claim 12'", False),
            ("cat > 'run'\".\"'sh' <<'EOF'\n" + writer + "\nEOF\nbash run.sh", True),
            ("echo 'uv '" + '"run "' + "'/catalog/gh-plan.py --repo client/repo claim 12' > run.sh\nbash run.sh", True),
        )
        for harness in ("codex", "claude"):
            for command, pending in commands:
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {})
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)


    def test_literal_streams_and_multiline_commands_share_reader_flow(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        commands = (
            ("echo " + shlex.quote(writer) + " | bash", True),
            ("printf '%s\\n' " + shlex.quote(writer) + " | sh", True),
            ("echo " + shlex.quote(writer) + " | gh issue comment 1 --body-file -", False),
            ("printf '%s\\n' " + shlex.quote("echo " + writer) + " | bash", False),
            ("while read -r line; do\n eval \"$line\"\ndone <<'EOF'\n" + writer + "\nEOF", True),
            ("while read -r line; do\n printf '%s\\n' \"$line\"\ndone <<'EOF'\n" + writer + "\nEOF", False),
            ("{\n cat <<'EOF'\n" + writer + "\nEOF\n} | bash", True),
            ("{\n gh issue comment 1 --body-file - <<'EOF'\n" + writer + "\nEOF\n} | bash", False),
            ("echo " + shlex.quote('#!/bin/sh\\n' + writer) + " | bash", True),
            ("echo -E " + shlex.quote('#!/bin/sh\\n' + writer) + " | bash", False),
            ("echo " + shlex.quote('echo "fixture\\n' + writer + '"') + " | bash", False),
            ("{ printf '#'; printf '%s\\n' " + shlex.quote(writer) + "; } | bash", False),
            ("{ printf 'uv '; printf '%s\\n' " + shlex.quote(writer.removeprefix('uv ')) + "; } | bash", True),
        )
        for harness in ("codex", "claude"):
            for command, pending in commands:
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {})
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)

    def test_literal_streams_retain_provenance_through_groups_and_files(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        producers = (
            ("echo " + shlex.quote(writer) + " | tee run.sh", True),
            ("echo " + shlex.quote(writer) + " | sudo tee run.sh", True),
            ("printf '%s\\n' " + shlex.quote(writer) + " | cat > run.sh", True),
            ("{ echo '#!/bin/sh'; echo " + shlex.quote(writer) + "; } > run.sh", True),
            ("{\n echo '#!/bin/sh'\n echo " + shlex.quote(writer) + "\n} > run.sh", True),
            ("( echo '#!/bin/sh'; echo " + shlex.quote(writer) + ") > run.sh", True),
            ("echo " + shlex.quote(writer) + " | sudo gh issue comment 1 --body-file - > run.sh", False),
            ("echo " + shlex.quote('#!/bin/sh\\n' + writer) + " > run.sh", True),
            ("echo -E " + shlex.quote('#!/bin/sh\\n' + writer) + " > run.sh", False),
            ("{ printf '#'; printf '%s\\n' " + shlex.quote(writer) + "; } > run.sh", False),
        )
        for harness in ("codex", "claude"):
            for command, pending in producers:
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {}, {})
                    for index, text in ((1, command), (3, "bash run.sh")):
                        if harness == "codex":
                            rows[index]["payload"]["arguments"] = json.dumps({"cmd": text})
                        else:
                            rows[index]["message"]["content"][0]["input"] = {"command": text}
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)

    def test_candidate_file_streams_execute_only_in_connected_code_consumers(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        commands = (
            ("cat run.sh | bash", True), ("sudo cat run.sh | sh", True),
            ("bash <(cat run.sh)", True),
            ('bash -c "$(cat run.sh)"', True), ('eval "$(cat run.sh)"', True),
            ("cat run.sh", False), ("cat run.sh | gh issue comment 1 --body-file -", False),
            ('echo "$(cat run.sh)"', False), ('bash -c \'echo "$(cat run.sh)"\'', False),
            ('eval \'echo "$(cat run.sh)"\'', False),
        )
        for harness in ("codex", "claude"):
            for command, pending in commands:
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {}, {})
                    if harness == "codex":
                        rows[1]["payload"].update(name="apply_patch", arguments='*** Begin Patch\n*** Add File: /repo/run.sh\n+' + writer + '\n*** End Patch')
                        rows[3]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0].update(name="Write", input={"file_path": "/repo/run.sh", "content": writer})
                        rows[3]["message"]["content"][0]["input"] = {"command": command}
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)


        patch_body = '*** Begin Patch\n*** Add File: /repo/run.sh\n+' + writer + '\n*** End Patch'
        for same_call in (True, False):
            with self.subTest(mixed_patch_exec=same_call):
                rows = history("codex", *({} for _ in range(1 if same_call else 2)))
                command = "bash run.sh" if same_call else "echo done"
                rows[1]["payload"].update(name="exec", arguments=
                    "await tools.apply_patch(" + json.dumps(patch_body) + "); "
                    + "text(await tools.exec_command(" + json.dumps({"cmd": command}) + "))")
                if not same_call:
                    rows[3]["payload"]["arguments"] = json.dumps({"cmd": "bash run.sh"})
                self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])
        for field in ("workdir", "cwd"):
            rows = history("codex", {})
            other_patch = patch_body.replace("/repo/run.sh", "/wt/repo/run.sh")
            rows[1]["payload"].update(name="exec", arguments=
                "await tools.apply_patch(" + json.dumps(other_patch) + "); "
                + "text(await tools.exec_command(" + json.dumps({"cmd": "bash run.sh", field: "/wt/repo"}) + "))")
            self.assertTrue(receipts.ownership_state(rows, "codex")["pending"])
        for field in ("workdir", "cwd"):
            for directory, pending in (("child", True), ("other", False)):
                with self.subTest(relative_mixed_directory=directory, field=field):
                    rows = history("codex", {})
                    relative_patch = patch_body.replace("/repo/run.sh", "/repo/child/run.sh")
                    rows[1]["payload"].update(name="exec", arguments=
                        "await tools.apply_patch(" + json.dumps(relative_patch) + "); "
                        + "text(await tools.exec_command(" + json.dumps({"cmd": "bash run.sh", field: directory}) + "))")
                    self.assertEqual(receipts.ownership_state(rows, "codex")["pending"], pending)
        for harness in ("codex", "claude"):
            for field in ("workdir", "cwd", "working_directory"):
                for serialized in (False, True) if harness == "codex" else (False,):
                    with self.subTest(harness=harness, relative_directory=field, serialized=serialized):
                        rows = history(harness, {}, {})
                        write = {"cmd": "cat > run.sh <<'EOF'\n" + writer + "\nEOF", field: "child"}
                        read = {"cmd": "bash child/run.sh"}
                        if harness == "codex":
                            for index, args in ((1, write), (3, read)):
                                rows[index]["payload"].update(
                                    name="exec" if serialized else "exec_command",
                                    arguments="text(await tools.exec_command(" + json.dumps(args) + "))" if serialized else json.dumps(args))
                        else:
                            for index, args in ((1, write), (3, read)):
                                rows[index]["message"]["content"][0]["input"] = {"command": args["cmd"], **({field: "child"} if index == 1 else {})}
                        self.assertTrue(receipts.ownership_state(rows, harness)["pending"])
        rows = history("codex", {})
        rows[1]["payload"].update(name="exec", arguments=
            'text(await tools.exec_command({"cmd":"bash run.sh"})); '
            + "await tools.apply_patch(" + json.dumps(patch_body) + ")")
        self.assertFalse(receipts.ownership_state(rows, "codex")["pending"])


    def test_ordered_file_provenance_does_not_borrow_overwritten_or_future_content(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        for harness in ("codex", "claude"):
            for contents in ((writer, "echo done", None), (None, writer)):
                with self.subTest(harness=harness, contents=contents):
                    rows = history(harness, *({} for _ in contents))
                    for position, content in enumerate(contents):
                        index = 1 + position * 2
                        if harness == "codex":
                            command = "bash run.sh" if content is None else "cat > run.sh <<'EOF'\n" + content + "\nEOF"
                            rows[index]["payload"]["arguments"] = json.dumps({"cmd": command})
                            rows[index + 1] = shell_result({"exit_code": 0, "output": ""}, harness, "tool-" + str(position))
                        elif content is None:
                            rows[index]["message"]["content"][0]["input"] = {"command": "bash run.sh"}
                        else:
                            rows[index]["message"]["content"][0].update(name="Write", input={"file_path": "/repo/run.sh", "content": content})
                    candidates = receipts.ownership_file_candidates([{"harness": harness, "records": rows}])
                    self.assertFalse(receipts.ownership_state(rows, harness, file_candidates=candidates)["pending"])


        def invocation(rows, harness, identifier):
            return next(record for record in rows
                        if any(source == identifier for source, _, _ in receipts.calls_and_results(record, harness)[0]))

        def terminal(rows, harness, identifier):
            return next(record for record in rows if identifier in receipts.calls_and_results(record, harness)[1])

        def scripts(harness, commands):
            rows = history(harness, *({} for _ in commands))
            for index, command in enumerate(commands):
                identifier = f"tool-{index}"
                record = invocation(rows, harness, identifier)
                if harness == "codex":
                    record["payload"]["arguments"] = json.dumps({"cmd": command})
                else:
                    record["message"]["content"][0]["input"] = {"command": command}
                result = terminal(rows, harness, identifier)
                result.clear()
                result.update(shell_result({"exit_code": 0}, harness, identifier))
            return rows

        # Separate local calls must settle harmless non-Python sources too.
        # A source completing during its copy's interval remains uncertain.
        for harness in ("codex", "claude"):
            for copier in ("cp", "mv", "install", "cp -r", "transitive"):
                for mode in ("complete", "failed", "unknown", "pending", "overlap"):
                    recursive = copier == "cp -r"
                    source = "tmpl/run.sh" if recursive else "safe.sh"
                    destination = "out/run.sh" if recursive else "run.sh"
                    copy = "cp -r tmpl/. out" if recursive else ("cp safe.sh mid.sh" if copier == "transitive" else copier + " safe.sh run.sh")
                    commands = ["echo " + shlex.quote(writer) + " > " + destination,
                                "printf 'echo done\\n' > " + source, copy]
                    if copier == "transitive":
                        commands.append("cp mid.sh run.sh")
                    commands.append("bash " + destination)
                    rows = scripts(harness, commands)
                    if harness == "claude":
                        invocation(rows, harness, "tool-1")["message"]["content"][0].update(
                            name="Write", input={"file_path": "/repo/" + source, "content": "echo done"})
                    for index in range(len(commands)):
                        invocation(rows, harness, f"tool-{index}")["timestamp"] = f"2026-01-01T00:00:{index * 2:02d}Z"
                        terminal(rows, harness, f"tool-{index}")["timestamp"] = f"2026-01-01T00:00:{index * 2 + 1:02d}Z"
                    result = terminal(rows, harness, "tool-1")
                    if mode == "pending":
                        rows.remove(result)
                    elif mode == "overlap":
                        rows.remove(result)
                        copy_call = invocation(rows, harness, "tool-2")
                        rows.insert(rows.index(copy_call) + 1, result)
                        result["timestamp"] = "2026-01-01T00:00:04.5Z"
                    elif mode != "complete":
                        result.clear()
                        result.update(shell_result({"exit_code": 1} if mode == "failed" else {}, harness, "tool-1"))
                        result["timestamp"] = "2026-01-01T00:00:03Z"
                        if harness == "claude":
                            result["message"]["content"][0]["is_error"] = True if mode == "failed" else "unknown"
                    with self.subTest(harness=harness, settled_non_python_source=copier, result=mode):
                        self.assertEqual(receipts.ownership_state(rows, harness)["pending"], mode != "complete")

            # Copying into an ancestor must preserve unchanged own inputs.
            # A known nested counterpart can actually overwrite that input.
            for copier in ("cp -r", "cp -R", "cp -a"):
                for counterpart in (False, True):
                    commands = ["echo " + shlex.quote(writer) + " > run.sh",
                                "printf 'echo done\\n' > tmpl/run.sh"]
                    if counterpart:
                        commands.append("echo " + shlex.quote(writer) + " > tmpl/tmpl/run.sh")
                    commands += [copier + " tmpl/. /repo", "bash run.sh"]
                    rows = scripts(harness, commands)
                    with self.subTest(harness=harness, ancestor_copy=copier, known_counterpart=counterpart):
                        self.assertEqual(receipts.ownership_state(rows, harness)["pending"], counterpart)

        # The same implied-success relation serves an outer exit-zero and a
        # local reader. Ordered replay must retain later appends and copies.
        for harness in ("codex", "claude"):
            owner = "echo " + shlex.quote(writer) + " > run.sh"
            owning_source = "echo " + shlex.quote(writer) + " > owner.sh"
            safe_write = "printf 'echo done\\n' > run.sh"
            cases = (
                ("unconditional_local", safe_write + "; bash run.sh", True),
                ("newline_local", safe_write + "\nbash run.sh", True),
                ("recovered_local", safe_write + " || true; bash run.sh", True),
                ("guarded_plain", safe_write + " && bash run.sh", False),
                ("guarded_exit_trap", "trap 'exit 0' EXIT; " + safe_write + " && bash run.sh", False),
                ("guarded_nested_trap", "{ trap 'exit 0' EXIT; }; " + safe_write + " && bash run.sh", False),
                ("append_after_replace", "printf '#!/bin/sh\\n' > run.sh && echo " + shlex.quote(writer) + " >> run.sh && bash run.sh; echo end", True),
                ("copy_after_condition", "if " + safe_write + "; then cp owner.sh run.sh; bash run.sh; fi", True),
                ("copy_in_group", safe_write + " && { cp owner.sh run.sh || true; } && bash run.sh; echo end", True),
                ("or_list", "[ -f run.sh ] || " + safe_write + " && bash run.sh; echo end", True),
                ("literal_false_or", "false || " + safe_write + " && bash run.sh; echo end", False),
                ("literal_true_or", "true || " + safe_write + " && bash run.sh; echo end", True),
                ("or_condition", "if [ -f run.sh ] || " + safe_write + "; then bash run.sh; fi", True),
                ("and_or_list", "[ -f run.sh ] && true || " + safe_write + " && bash run.sh; echo end", True),
                ("negated_condition", "if ! " + safe_write + "; then bash run.sh; fi", True),
                ("negated_group_condition", "if ! { " + safe_write + "; }; then bash run.sh; fi", True),
                ("negated_subshell_condition", "if ! ( " + safe_write + " ); then bash run.sh; fi", True),
                ("guarded_condition", "if " + safe_write + "; then bash run.sh; fi", False),
                ("guarded_group_condition", "if { " + safe_write + "; }; then bash run.sh; fi", False),
                ("guarded_nested_condition", "if true; then if " + safe_write + "; then bash run.sh; fi; fi", False),
                ("guarded_then", "if true; then " + safe_write + " && bash run.sh; fi", False),
                ("guarded_subshell", safe_write + " && (cd /repo && bash run.sh); echo exit=$?", False),
                ("guarded_group", safe_write + " && { bash run.sh; }; echo exit=$?", False),
                ("guarded_function", "f() { bash run.sh; }; " + safe_write + " && f; echo exit=$?", False),
                ("guarded_body", "f() { " + safe_write + " && bash run.sh; }; f", False),
                ("guarded_loop", safe_write + " && for i in one; do bash run.sh; done; echo exit=$?", False),
                ("guarded_loop_body", "for i in one; do " + safe_write + " && bash run.sh; done", False),
            )
            for label, command, pending in cases:
                rows = scripts(harness, [owner, owning_source, command])
                with self.subTest(harness=harness, implied_success=label):
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)

            for path in ("run.sh", "run.py"):
                python = path.endswith(".py")
                program = "import os\nos.system(" + repr(writer) + ")" if python else writer
                harmless = "print('safe')" if python else "echo done"
                original = "printf '%s\\n' " + shlex.quote(program) + " > " + path
                write = "printf '%s\\n' " + shlex.quote(harmless) + " > " + path
                reader = ("python3 " if python else "bash ") + path
                hidden = (("last", write, False), ("and", write + " && echo done", False),
                          ("semicolon", write + "; echo done", True), ("newline", write + "\necho done", True),
                          ("or", write + " || true", True), ("or_colon", write + " || :", True),
                          ("pipeline", write + " | cat", True), ("negation", "! " + write, True),
                          ("background", write + " &", True),
                          ("exit", write + "; exit 0", True), ("group", "{ " + write + "; echo done; }", True),
                          ("subshell", "( " + write + "; echo done )", True),
                          ("if_condition", "if " + write + "; then :; fi", True),
                          ("while_condition", "while " + write + "; do break; done", True),
                          ("until_condition", "until " + write + "; do break; done", True),
                          ("if_body", "if true; then " + write + "; echo done; fi", True),
                          ("loop_body", "for i in one; do " + write + "; echo done; done", True),
                          ("case_body", "case one in one) " + write + "; echo done;; esac", True),
                          ("trap", "trap 'exit 0' EXIT; " + write, True),
                          ("nested_trap", "{ trap 'exit 0' EXIT; }; " + write, True),
                          ("called_trap", "f() { trap 'exit 0' EXIT; }; f; " + write, True),
                          ("unused_trap", "f() { trap 'exit 0' EXIT; }; " + write, False),
                          ("tee_last", "printf '%s\\n' " + shlex.quote(harmless) + " | tee " + path, False),
                          ("tee_hidden", "printf '%s\\n' " + shlex.quote(harmless) + " | tee " + path + "; echo done", True))
                for label, replacement, pending in hidden:
                    rows = scripts(harness, [original, replacement, reader])
                    with self.subTest(harness=harness, outer_success=label, path=path):
                        self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)
                for copier in ("cp", "mv", "install"):
                    for suffix, pending in (("", False), (" && echo done", False), ("; echo done", True), (" || true", True), ("negated", True)):
                        source = "safe.py" if python else "safe.sh"
                        commands = [original, "printf '%s\\n' " + shlex.quote(harmless) + " > " + source,
                                    ("! " if suffix == "negated" else "") + copier + " " + source + " " + path + ("" if suffix == "negated" else suffix), reader]
                        with self.subTest(harness=harness, copy_status=copier, suffix=suffix, path=path):
                            self.assertEqual(receipts.ownership_state(scripts(harness, commands), harness)["pending"], pending)


        # Successful scope termination can precede its final write. The same
        # endpoint relation must govern a caller's reader and native exit-zero.
        safe_write = "printf 'echo done\\n' > run.sh"
        owner = "echo " + shlex.quote(writer) + " > run.sh"
        termination_cases = (
            ("eval_exit", "[ -f run.sh ] && eval exit; " + safe_write, True, True),
            ("eval_quoted_exit", '[ -f run.sh ] && eval "exit 0"; ' + safe_write, True, True),
            ("exec_process", "[ -f run.sh ] && exec true; " + safe_write, True, True),
            ("exec_options", "[ -f run.sh ] && exec -a sample true; " + safe_write, True, True),
            ("source_exit", 'printf "exit 0\\n" > early.sh; . ./early.sh; ' + safe_write, True, True),
            ("source_named_exit", 'printf "exit 0\\n" > early.sh; source ./early.sh; ' + safe_write, True, True),
            ("conditional_exit_shadow", 'if [ -n "$X" ]; then exit() { :; }; fi; [ -f run.sh ] && exit 0; ' + safe_write, True, True),
            ("conditional_owning_shadow", 'if [ -n "$X" ]; then exit() { bash run.sh; }; fi; exit 0; ' + safe_write, True, True),
            ("conditional_previous_shadow", 'gen() { bash run.sh; }; if [ -n "$X" ]; then gen() { :; }; fi; gen; ' + safe_write, True, True),
            ("pipeline_exit_shadow", "printf x | exit() { :; }; [ -f run.sh ] && exit 0; " + safe_write, True, True),
            ("subshell_exit_shadow", "( exit() { :; } ); [ -f run.sh ] && exit 0; " + safe_write, True, True),
            ("unused_subshell_function", "gen() ( " + safe_write + " )", True, True),
            ("called_subshell_function", "gen() ( " + safe_write + " ); gen && bash run.sh", False, False),
            ("called_compound_owning_function", "gen() if true; then bash run.sh; fi; gen; " + safe_write, True, True),
            ("conditional_nested_redefinition", 'gen() { bash run.sh; }; change() { gen() { :; }; }; if [ -n "$X" ]; then change; fi; gen; ' + safe_write, True, True),
            ("unused_compound_function", "gen() if true; then " + safe_write + "; fi", True, True),
            ("unused_simple_function", "gen() " + safe_write, True, True),
            ("called_simple_function", "gen() " + safe_write + "; gen && bash run.sh", False, False),
            ("logout", "[ -f run.sh ] && logout 0; " + safe_write, True, True),
            ("bye", "[ -f run.sh ] && bye; " + safe_write, True, True),
            ("top_level_return", "[ -f run.sh ] && return 0; " + safe_write, True, True),
            ("exec_redirection", "exec >log; " + safe_write, True, False),
            ("shadowed_copy", "cp() { :; }; printf 'echo safe\\n' > safe.sh; cp safe.sh run.sh && bash run.sh", False, True),
            ("aliased_copy", "alias cp=':'\nprintf 'echo safe\\n' > safe.sh; cp safe.sh run.sh && bash run.sh", False, True),
            ("early_exit", "[ -f run.sh ] && exit 0; " + safe_write, True, True),
            ("early_exit_implicit", "[ -f run.sh ] && exit; " + safe_write, True, True),
            ("early_exit_final_exit", "[ -f run.sh ] && exit 0; " + safe_write + " && exit 0", True, True),
            ("early_exit_group", "{ [ -f run.sh ] && exit 0; " + safe_write + "; }", True, True),
            ("early_exit_subshell", "( [ -f run.sh ] && exit 0; " + safe_write + " ) && bash run.sh", False, True),
            ("early_return", "gen() { [ -f run.sh ] && return; " + safe_write + "; }; gen && bash run.sh", False, True),
            ("explicit_return", "gen() { [ -f run.sh ] && return 0; " + safe_write + "; }; gen && bash run.sh", False, True),
            ("return_completion", "gen() { [ -f run.sh ] && return 0; " + safe_write + "; }; gen", True, True),
            ("return_group", "gen() { { [ -f run.sh ] && return; }; " + safe_write + "; }; gen && bash run.sh", False, True),
            ("return_if", "gen() { if [ -f run.sh ]; then return; fi; " + safe_write + "; }; gen && bash run.sh", False, True),
            ("function_exit", "gen() { [ -f run.sh ] && exit 0; }; gen; " + safe_write, True, True),
            ("function_return_continues", "gen() { [ -f run.sh ] && return 0; }; gen; " + safe_write, True, False),
            ("subshell_exit_isolated", "( [ -f run.sh ] && exit 0; " + safe_write + " ); " + safe_write, True, False),
            ("pipeline_exit_isolated", "exit 0 | true; " + safe_write, True, False),
            ("nested_function_exit", "early() { [ -f run.sh ] && exit 0; }; ( early; " + safe_write + " ) && bash run.sh", False, True),
            ("command_exit", "[ -f run.sh ] && command -- exit 0; " + safe_write, True, True),
            ("command_options_exit", "[ -f run.sh ] && command -p -- exit 0; " + safe_write, True, True),
            ("builtin_exit", "[ -f run.sh ] && builtin exit 0; " + safe_write, True, True),
            ("shadowed_builtin_exit", "exit() { :; }; builtin exit 0; " + safe_write, True, True),
            ("shadowed_function", "exit() { :; }; exit; " + safe_write, True, False),
            ("command_query", "command -v exit; " + safe_write, True, False),
            ("skipped_exit", "false && exit 0; " + safe_write, True, False),
            ("unused_return", "gen() { [ -f run.sh ] && return; " + safe_write + "; }; " + safe_write, True, False),
            ("final_exit", safe_write + " && exit 0", True, False),
            ("final_exit_group", "{ " + safe_write + " && exit 0; }", True, False),
            ("final_exit_subshell", "( " + safe_write + " && exit 0 )", True, False),
            ("ordinary_function", "gen() { " + safe_write + "; }; gen && bash run.sh", False, False),
            ("final_return", "gen() { " + safe_write + " && return 0; }; gen && bash run.sh", False, False),
        )
        termination_cases += tuple(
            ("prefixed_function_" + prefix, "gen() { bash run.sh; }; " + prefix + " gen", False, True)
            for prefix in ("time", "time -p", "noglob", "nocorrect", "nocorrect noglob"))
        termination_cases += tuple(
            ("prefixed_exit_" + prefix, "[ -f run.sh ] && " + prefix + " exit 0; " + safe_write, True, True)
            for prefix in ("time", "time -p", "noglob", "nocorrect"))
        termination_cases += tuple(
            ("removed_" + name + "_" + removal,
             name + "() { :; }; " + removal + " " + name + "; "
             + ("[ -f run.sh ] && exit 0; " + safe_write if name == "exit" else "bash run.sh"),
             name == "exit", True)
            for removal in ("unset -f", "unset", "unfunction", "unhash -f", "disable -f")
            for name in ("exit", "bash"))
        termination_cases += (
            ("hashed_script_dispatch", "chmod +x run.sh; hash -p ./run.sh cp; cp a b", False, True),
            ("hash_declaration_only", "hash -p ./run.sh cp", False, False),
            ("hash_reset", "hash -p ./run.sh cp; hash -r; cp a b", False, False),
            ("hash_query", "hash -t cp; " + safe_write, True, False),
            ("hash_query_overrides_binding", "hash -t -p ./run.sh cp; " + safe_write, True, False),
            ("prefixed_eval", '[ -f run.sh ] && time eval "exit 0"; ' + safe_write, True, True),
            ("prefixed_trap", "time trap 'exit 0' EXIT; " + safe_write, True, True),
            ("prefixed_builtin", "exit() { :; }; time builtin exit 0; " + safe_write, True, True),
            ("prefixed_command_query", "time command -v exit; " + safe_write, True, False),
            ("external_env", "gen() { bash run.sh; }; env gen || true", False, False),
            ("external_nohup", "gen() { bash run.sh; }; nohup gen || true", False, False),
            ("command_bypasses_function", "gen() { bash run.sh; }; time command gen || true", False, False),
            ("coproc_function", "gen() { bash run.sh; }; coproc gen", False, True),
            ("coproc_completion", "coproc " + safe_write, True, True),
            ("child_removes_exit", "exit() { :; }; ( unset -f exit; [ -f run.sh ] && exit 0; " + safe_write + " ) && bash run.sh", False, True),
            ("child_removes_reader", "bash() { :; }; ( unset -f bash; bash run.sh )", False, True),
            ("child_removal_isolated", "exit() { :; }; ( unset -f exit; : ); exit; " + safe_write, True, False),
            ("child_reader_removal_isolated", "bash() { :; }; ( unset -f bash; : ); bash run.sh", False, False),
            ("child_conditional_shadow", 'gen() { bash run.sh; }; ( if [ -n "$X" ]; then gen() { :; }; fi; gen ); ' + safe_write, True, True),
            ("pattern_removes_reader", "bash() { :; }; unhash -fm 'ba*'; bash run.sh", False, True),
            ("conditional_removal_preserves_body", 'gen() { bash run.sh; }; if [ -n "$X" ]; then unset -f gen; fi; gen', False, True),
            ("variable_only_removal", "exit() { :; }; unset -v exit; exit; " + safe_write, True, False),
            ("unrelated_removal", "exit() { :; }; unset -f other; exit; " + safe_write, True, False),
            ("emulate_local_function", "gen() { bash run.sh; }; emulate zsh -c 'gen'", False, True),
            ("emulate_builtin_bypass", "gen() { bash run.sh; }; emulate zsh -c 'command gen' || true", False, False),
            ("emulate_clustered_exit", '[ -f run.sh ] && emulate zsh -fc "exit 0"; ' + safe_write, True, True),
            ("emulate_query", "emulate -l zsh; " + safe_write, True, False),
            ("emulate_exit", '[ -f run.sh ] && emulate zsh -c "exit 0"; ' + safe_write, True, True),
        )
        # Installing a handler may successfully finish its surrounding scope.
        # Query, reset and ignored-signal forms install no executable handler.
        handler_cases = [
            ("trap_" + signal, "trap 'exit 0' " + signal + "; " + safe_write, True, True)
            for signal in ("ERR", "ZERR", "EXIT", "exit", "SIGEXIT", "DEBUG", "RETURN", "INT")]
        handler_cases += [
            (hook, hook + "() { exit 0; }; " + safe_write, True, True)
            for hook in ("TRAPEXIT", "TRAPERR", "TRAPZERR", "TRAPDEBUG")]
        handler_cases += [
            ("inherited_debug_subshell", "trap 'if [[ $BASH_SUBSHELL == 1 ]]; then exit 0; fi' DEBUG; set -T; ( " + safe_write + " ) && bash run.sh", False, True),
            ("zsh_inherited_debug_subshell", "TRAPDEBUG() { if [[ $ZSH_SUBSHELL == 1 ]]; then exit 0; fi; }; ( " + safe_write + " ) && bash run.sh", False, True),
            ("zsh_short_debug", "TRAPDEBUG() exit 0; " + safe_write, True, True),
            ("debug_skip_lower", "shopt -s extdebug; trap '[[ $BASH_COMMAND != printf* ]]' debug; " + safe_write + " && bash run.sh", False, True),
            ("debug_skip_mixed", "shopt -s extdebug; trap '[[ $BASH_COMMAND != printf* ]]' dEbUg; " + safe_write + " && bash run.sh", False, True),
            ("invalid_sigdebug", "shopt -s extdebug; trap '[[ $BASH_COMMAND != printf* ]]' SIGDEBUG; " + safe_write + " && bash run.sh", False, False),
            ("debug_skip", "shopt -s extdebug; trap '[[ $BASH_COMMAND != printf* ]]' DEBUG; " + safe_write + " && bash run.sh", False, True),
            ("guarded_before_hook", "( " + safe_write + " ) && bash run.sh; trap 'exit 0' DEBUG", False, False),
            ("inherited_err_suppressed", "trap 'exit 0' ERR; set -E; ( " + safe_write + " ) && bash run.sh", False, False),
            ("inherited_function_err_suppressed", "trap 'return 0' ERR; set -E; gen() { " + safe_write + "; }; gen && bash run.sh", False, False),
            ("trap_explicit_action", "trap -- '-x; exit 0' ERR; " + safe_write, True, True),
            ("command_options_trap", "command -p -- trap 'exit 0' ERR; " + safe_write, True, True),
            ("query_print", "trap -p EXIT; " + safe_write, True, False),
            ("query_list", "trap -l; " + safe_write, True, False),
            ("query_bare", "trap; " + safe_write, True, False),
            ("reset", "trap - EXIT; " + safe_write, True, False),
            ("ignore", "trap '' EXIT; " + safe_write, True, False),
            ("guarded_trap", "trap 'exit 0' ERR; " + safe_write + " && bash run.sh", False, False),
            ("subshell_trap", "( trap 'exit 0' ERR; " + safe_write + " ) && bash run.sh", False, True),
            ("subshell_trap_isolated", "( trap 'exit 0' ERR; : ); " + safe_write, True, False),
            ("pipeline_trap_isolated", "trap 'exit 0' ERR | true; " + safe_write, True, False),
            ("function_trap_return", "gen() { trap 'return 0' ERR; " + safe_write + "; }; gen && bash run.sh", False, True),
            ("guarded_function_trap", "gen() { trap 'return 0' ERR; " + safe_write + " && bash run.sh; }; gen", False, False),
            ("skipped_hook", "if false; then TRAPEXIT() { exit 0; }; fi; " + safe_write, True, False),
            ("unused_hook", "unused() { TRAPEXIT() { exit 0; }; }; " + safe_write, True, False),
            ("called_hook", "install() { TRAPZERR() { exit 0; }; }; install; " + safe_write, True, True),
        ]
        # Background calls remain opaque to native ownership completion, but
        # their child endpoints cannot invalidate a later foreground write.
        for command in (
                "exit 0 & " + safe_write,
                "[ -f run.sh ] && exit 0 & " + safe_write,
                "{ [ -f run.sh ] && exit 0; } & " + safe_write,
                "trap 'exit 0' ERR & " + safe_write,
                "true && trap 'exit 0' ERR & " + safe_write):
            effects = []
            receipts.shell_code(command, effects=effects)
            replacement = next(effect for effect in effects if effect["kind"] == "write")
            with self.subTest(child_endpoint=command):
                self.assertTrue(replacement["replaces"])

        for harness in ("codex", "claude"):
            for label, command, later_reader, pending in (*termination_cases, *handler_cases):
                commands = [owner, command, *(["bash run.sh"] if later_reader else [])]
                rows = scripts(harness, commands)
                if harness == "codex":
                    # A declared direct native transport fixture, independent
                    # of the composed-result/serial normalization factories.
                    for index in range(len(commands)):
                        result = terminal(rows, harness, f"tool-{index}")
                        result.clear()
                        result.update(native_result(f"tool-{index}"))
                with self.subTest(harness=harness, scope_endpoint=label):
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)

        # Hash declarations are metadata. Only a later dispatch reads the
        # selected script, with the caller's current file/scope provenance.
        hash_owner = "printf '%s\\n' " + shlex.quote(writer) + " > run.sh && chmod +x run.sh"
        hash_bind = "hash -p ./run.sh cp"
        safe_file = "printf 'echo safe\\n' > safe.sh && chmod +x safe.sh"
        hash_cases = [
            ("declaration", [hash_bind], False),
            ("query", [hash_bind + "; hash -t cp"], False),
            ("dispatch", [hash_bind + "; cp a b"], True),
            ("reset", [hash_bind + "; hash -r; cp a b"], False),
            ("delete", [hash_bind + "; hash -d cp; cp a b"], False),
            ("path_reset", [hash_bind + '; PATH="$PATH"; cp a b'], False),
            ("function_precedence", ["cp() { :; }; " + hash_bind + "; cp a b"], False),
            ("command_bypass", ["cp() { :; }; " + hash_bind + "; command cp a b"], True),
            ("builtin_bypass", [hash_bind + "; builtin cp a b || true"], False),
            ("standard_path", [hash_bind + "; command -p cp a b"], False),
            ("child_inherits", [hash_bind + "; (cp a b)"], True),
            ("child_isolated", ["(" + hash_bind + "); cp a b"], False),
            ("child_replacement_isolated", [safe_file + "; " + hash_bind + "; (hash -p ./safe.sh cp); cp a b"], True),
            ("conditional_replacement", [safe_file + "; " + hash_bind + '; if [ -n "$X" ]; then hash -p ./safe.sh cp; fi; cp a b'], True),
            ("definite_replacement", [safe_file + "; " + hash_bind + "; hash -p ./safe.sh cp; cp a b"], False),
            ("separate_native_calls", [hash_bind, "cp a b"], False),
            ("current_bytes", [hash_bind + "; printf 'echo safe\\n' > run.sh && cp a b"], False),
            ("default_copy", [safe_file, "cp safe.sh run.sh", "bash run.sh"], False),
            ("hashed_copy", [safe_file, "hash -p ./safe.sh cp; cp safe.sh run.sh", "bash run.sh"], True),
            # A hashed wrapper can forward argv to the default reader.
            ("hashed_reader", [safe_file, "hash -p ./safe.sh bash; bash run.sh"], True),
            ("hashed_copy_redirection", [safe_file, "hash -p ./safe.sh cp; cp safe.sh run.sh > run.sh", "bash run.sh"], False),
            ("default_copy_redirection", ["cp run.sh owning.sh", "cp owning.sh run.sh > run.sh", "bash run.sh"], True),
            ("unknown_builtin_restoration", [hash_bind + "; enable cp || true; cp a b"], True),
            ("disabled_explicit_mutator", [hash_bind + "; enable -n hash; builtin hash -r || true; cp a b"], True),
            ("conditional_reset_default_reader", [safe_file, 'hash -p ./safe.sh bash; if [ -n "$X" ]; then hash -r; fi; bash run.sh'], True),
            ("conditional_delete_default_reader", [safe_file, 'hash -p ./safe.sh bash; if [ -n "$X" ]; then hash -d bash; fi; bash run.sh'], True),
            ("disabled_builtin", ["hash -p ./run.sh echo; enable -n echo; echo safe"], True),
            ("reenabled_builtin", ["hash -p ./run.sh echo; enable -n echo; enable echo; echo safe"], False),
            ("conditional_disable", ['hash -p ./run.sh echo; if [ -n "$X" ]; then enable -n echo; fi; echo safe'], True),
        ]
        # A Bash-only hash binding cannot borrow zsh external-command dispatch.
        hash_cases += [("builtin_" + name + "_" + command, ["hash -p ./run.sh " + name + "; " + command], pending)
                       for name, command, pending in (("echo", "echo safe", False), ("echo", "command echo safe", False),
                                                      ("echo", "builtin echo safe", False), ("pwd", "pwd", False),
                                                      ("trap", "trap -p", False))]
        # The native fixture records no Bash version or optional build features.
        # These names can therefore dispatch the hash on stock Bash3.2 or a
        # feature-disabled build; enable cannot create an absent builtin.
        for name in ("mapfile", "readarray", "compopt"):
            binding = "hash -p ./run.sh " + name
            hash_cases += [
                (name + "_version_dependent", [binding + "; " + name], True),
                (name + "_explicit_bash", ["/bin/bash -c " + shlex.quote(binding + "; " + name)], True),
                (name + "_failed_enable", [binding + "; enable " + name + " || true; " + name], True),
            ]
        # This newly added hash-precision control previously assumed ALIAS was
        # compiled in. Preserve both possible builds and test supported bypasses.
        hash_cases += [
            ("alias_feature_dependent", ["hash -p ./run.sh alias; alias"], True),
            ("alias_enable_restoration", ["hash -p ./run.sh alias; enable -n alias; enable alias || true; alias"], True),
            ("alias_builtin_bypass", ["hash -p ./run.sh alias; builtin alias"], False),
            ("alias_standard_path_bypass", ["hash -p ./run.sh alias; command -p alias"], False),
            ("alias_reset_bypass", ["hash -p ./run.sh alias; hash -r; alias"], False),
        ]
        # Literal code enters the same ordered parser and scope relation.
        # Fresh processes import only possible exported functions; declarations
        # and unrelated commands never execute a hash target.
        hash_cases += [
            ('f1_zsh_hash_copy', ["printf 'echo safe\\n' > safe.sh && chmod +x safe.sh; hash -p ./safe.sh cp; cp run.sh other.sh", 'bash other.sh'], True),
            ('f1_disabled_hash_copy', ["printf 'echo safe\\n' > safe.sh && chmod +x safe.sh; set +h; hash -p ./safe.sh cp; cp run.sh other.sh", 'bash other.sh'], True),
            ('f1_export_path_copy', ['printf \'echo safe\\n\' > safe.sh && chmod +x safe.sh; hash -p ./safe.sh cp; export PATH="$PATH"; cp run.sh other.sh', 'bash other.sh'], True),
            ('f1_forwarding_hash_reader', ['printf \'#!/bin/sh\\nexec /bin/bash "$@"\\n\' > wrap.sh; chmod +x wrap.sh; hash -p ./wrap.sh bash; bash run.sh'], True),
            ('f2_zsh_hash_assignment', ['hash cp=$PWD/run.sh; cp a b'], True),
            ('f2_zsh_commands_assignment', ['commands[cp]=$PWD/run.sh; cp a b'], True),
            ('f3_exec_hash', ['hash -p ./run.sh cp; exec cp a b'], True),
            ('f3_exec_options_hash', ['hash -p ./run.sh cp; exec -cl -a sample cp a b'], True),
            ('f4_time_assignment', ['gen() { bash run.sh; }; time X=1 gen'], True),
            ('f4_time_negation', ['gen() { bash run.sh; }; time ! gen'], True),
            ('f4_append_assignment', ['gen() { bash run.sh; }; X+=1 gen'], True),
            ('f5_eval_direct', ['gen() { bash run.sh; }; eval gen'], True),
            ('f5_eval_compound', ["gen() { bash run.sh; }; eval 'true; gen'"], True),
            ('f5_emulate_compound', ["gen() { bash run.sh; }; emulate zsh -c 'true; gen'"], True),
            ('f5_eval_true_control', ['gen() { bash run.sh; }; eval true'], False),
            ('f5_emulate_true_control', ["gen() { bash run.sh; }; emulate zsh -c 'true'"], False),
            ('f6_disabled_explicit_mutator', ['enable -n hash; builtin hash -p ./run.sh cp || true; cp a b'], True),
            ('direct_hash_control', ['hash -p ./run.sh cp; cp a b'], True),
            ('child_local_hash', ["/bin/bash -c 'hash -p ./run.sh cp; cp a b'"], True),
            ('child_same_body_owning_write', ['/bin/bash -c \'printf \'"\'"\'%s\\n\'"\'"\' \'"\'"\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'"\'"\' > child.sh && chmod +x child.sh && hash -p ./child.sh cp; cp a b\''], True),
            ('child_rewrite_before_hashed_read', ['/bin/bash -c \'hash -p ./run.sh cp; printf \'"\'"\'echo safe\\n\'"\'"\' > run.sh && cp a b\''], False),
            ('child_success_proves_guarded_rewrite', ['/bin/bash -c \'printf \'"\'"\'echo safe\\n\'"\'"\' > run.sh && true\' && bash run.sh'], False),
            ('child_failure_does_not_prove_rewrite', ['/bin/bash -c \'printf \'"\'"\'echo safe\\n\'"\'"\' > run.sh; false\'; bash run.sh'], True),
            ('new_shell_does_not_inherit_hash', ["hash -p ./run.sh cp; /bin/bash -c 'cp a b'"], False),
            ('parenthesized_subshell_inherits_hash', ['hash -p ./run.sh cp; (cp a b)'], True),
            ('new_shell_does_not_inherit_shadow', ["cp() { :; }; /bin/bash -c 'hash -p ./run.sh cp; cp a b'"], True),
            ('new_shell_does_not_inherit_function', ["gen() { bash run.sh; }; /bin/bash -c 'gen; true'"], False),
            ('new_shell_imports_exported_function', ["gen() { bash run.sh; }; export -f gen; /bin/bash -c 'gen; true'"], True),
            ('child_hash_does_not_escape', ["/bin/bash -c 'hash -p ./run.sh cp'; cp a b"], False),
            ('child_function_does_not_escape', ["/bin/bash -c 'gen() { bash run.sh; }'; gen; true"], False),
            ('ordinary_direct_read', ['bash run.sh'], True),
            ('eval_body_owning_producer', ['eval \'printf \'"\'"\'%s\\n\'"\'"\' \'"\'"\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'"\'"\' > nested.sh && bash nested.sh\''], True),
            ('eval_body_guarded_rewrite', ['eval \'printf \'"\'"\'echo safe\\n\'"\'"\' > run.sh && bash run.sh\''], False),
            ('eval_body_failed_rewrite', ['eval \'printf \'"\'"\'echo safe\\n\'"\'"\' > run.sh; false\'; bash run.sh'], True),
            ('eval_body_early_success', ['eval \'[ -f run.sh ] && exit 0; printf \'"\'"\'echo safe\\n\'"\'"\' > run.sh\'', 'bash run.sh'], True),
            ('emulate_body_owning_producer', ['emulate zsh -c \'printf \'"\'"\'%s\\n\'"\'"\' \'"\'"\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'"\'"\' > nested.sh && bash nested.sh\''], True),
            ('emulate_body_guarded_rewrite', ['emulate zsh -c \'printf \'"\'"\'echo safe\\n\'"\'"\' > run.sh && bash run.sh\''], False),
            ('emulate_body_failed_rewrite', ['emulate zsh -c \'printf \'"\'"\'echo safe\\n\'"\'"\' > run.sh; false\'; bash run.sh'], True),
            ('emulate_body_early_success', ['emulate zsh -c \'[ -f run.sh ] && exit 0; printf \'"\'"\'echo safe\\n\'"\'"\' > run.sh\'', 'bash run.sh'], True),
            ('child_body_owning_producer', ['/bin/bash -c \'printf \'"\'"\'%s\\n\'"\'"\' \'"\'"\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'"\'"\' > nested.sh && bash nested.sh\''], True),
            ('child_body_guarded_rewrite', ['/bin/bash -c \'printf \'"\'"\'echo safe\\n\'"\'"\' > run.sh && bash run.sh\''], False),
            ('child_body_failed_rewrite', ['/bin/bash -c \'printf \'"\'"\'echo safe\\n\'"\'"\' > run.sh; false\'; bash run.sh'], True),
            ('child_body_early_success', ['/bin/bash -c \'[ -f run.sh ] && exit 0; printf \'"\'"\'echo safe\\n\'"\'"\' > run.sh\' && bash run.sh'], True),
            ('literal_child_with_expanded_environment', ["X=$X /bin/bash -c 'hash -p ./run.sh cp; cp a b'"], True),
            ('literal_eval_with_expanded_environment', ["gen() { bash run.sh; }; X=$X eval 'gen'"], True),
            ('nested_fresh_shell_entry', ['/bin/bash -c \'/bin/bash -c \'"\'"\'hash -p ./run.sh cp; cp a b\'"\'"\'\''], True),
            ('child_scope_position_isolation', ["/bin/bash -c 'true; true'; printf 'echo safe\\n' > run.sh && bash run.sh"], False),
            ('child_outer_redirection_before_body', ["/bin/bash -c 'bash run.sh' > run.sh"], False),
            ('skipped_literal_child', ["false && /bin/bash -c 'hash -p ./run.sh cp; cp a b'"], False),
            ('skipped_literal_eval', ["gen() { bash run.sh; }; false && eval 'gen'"], False),
            ('hashed_intrinsic_tee', ["printf 'echo safe\\n' > safe.sh; hash -p ./safe.sh tee; printf 'echo safe\\n' | tee run.sh", 'bash run.sh'], True),
            ('direct_redirection_before_reader', ['bash run.sh > run.sh'], False),
            ('function_redirection_before_reader', ['gen() { bash run.sh; }; gen > run.sh'], False),
            ('exported_function_redefinition', ["gen() { :; }; export -f gen; gen() { bash run.sh; }; /bin/bash -c 'gen; true'"], True),
            ('unexported_function_redefinition', ["gen() { :; }; gen() { bash run.sh; }; /bin/bash -c 'gen; true'"], False),
            ('builtin_exec_hash_child', ['hash -p ./run.sh cp; builtin exec cp a b'], True),
            ('exec_clustered_argv0_hash_child', ['hash -p ./run.sh cp; exec -cla sample cp a b'], True),
            ('exec_absolute_path_bypasses_hash', ['hash -p ./run.sh cp; exec /bin/cp a b'], False),
            ('builtin_exec_absolute_path_bypasses_hash', ['hash -p ./run.sh cp; builtin exec /bin/cp a b'], False),
            ('exec_absolute_real_owner', ['hash -p ./run.sh cp; exec /repo/run.sh'], True),
            ('builtin_exec_absolute_real_owner', ['hash -p ./run.sh cp; builtin exec /repo/run.sh'], True),
            ('zsh_shopt_binding_metadata', ['hash shopt=$PWD/run.sh'], False),
            ('zsh_shopt_binding_query', ['hash shopt=$PWD/run.sh; hash'], False),
            ('zsh_shopt_binding_dispatch', ['hash shopt=$PWD/run.sh; shopt'], True),
            ('zsh_shopt_explicit_builtin', ['hash shopt=$PWD/run.sh; builtin shopt || true'], False),
            ('zsh_shopt_standard_path', ['hash shopt=$PWD/run.sh; command -p shopt || true'], False),
            ('zsh_command_echo_binding', ['hash echo=$PWD/run.sh; command echo safe'], True),
            ('zsh_echo_binding_plain_builtin', ['hash echo=$PWD/run.sh; echo safe'], False),
            ('zsh_echo_binding_explicit_builtin', ['hash echo=$PWD/run.sh; builtin echo safe'], False),
            ('zsh_echo_binding_standard_path', ['hash echo=$PWD/run.sh; command -p echo safe'], False),
            ('zsh_echo_binding_command_query', ['hash echo=$PWD/run.sh; command -v echo'], False),
            ('zsh_echo_binding_verbose_query', ['hash echo=$PWD/run.sh; command -V echo'], False),
            ('direct_skipped_literal_stdin', ["false && python3 <<'PYCODE'\nimport subprocess\nsubprocess.run(['uv', 'run', '/catalog/gh-plan.py', '--repo', 'client/repo', 'claim', '12'])\nPYCODE\n\ntrue"], False),
            ('direct_reached_literal_stdin', ["python3 <<'PYCODE'\nimport subprocess\nsubprocess.run(['uv', 'run', '/catalog/gh-plan.py', '--repo', 'client/repo', 'claim', '12'])\nPYCODE\n\ntrue"], True),
            ('direct_unknown_literal_stdin', ['[ -n "$X" ] && python3 <<\'PYCODE\'\nimport subprocess\nsubprocess.run([\'uv\', \'run\', \'/catalog/gh-plan.py\', \'--repo\', \'client/repo\', \'claim\', \'12\'])\nPYCODE\n\ntrue'], True),
            ('child_skipped_literal_stdin', ['false && /bin/bash -c \'python3 <<\'"\'"\'PYCODE\'"\'"\'\nimport subprocess\nsubprocess.run([\'"\'"\'uv\'"\'"\', \'"\'"\'run\'"\'"\', \'"\'"\'/catalog/gh-plan.py\'"\'"\', \'"\'"\'--repo\'"\'"\', \'"\'"\'client/repo\'"\'"\', \'"\'"\'claim\'"\'"\', \'"\'"\'12\'"\'"\'])\nPYCODE\n\'\ntrue'], False),
            ('child_reached_literal_stdin', ['/bin/bash -c \'python3 <<\'"\'"\'PYCODE\'"\'"\'\nimport subprocess\nsubprocess.run([\'"\'"\'uv\'"\'"\', \'"\'"\'run\'"\'"\', \'"\'"\'/catalog/gh-plan.py\'"\'"\', \'"\'"\'--repo\'"\'"\', \'"\'"\'client/repo\'"\'"\', \'"\'"\'claim\'"\'"\', \'"\'"\'12\'"\'"\'])\nPYCODE\n\'\ntrue'], True),
            ('child_unknown_literal_stdin', ['[ -n "$X" ] && /bin/bash -c \'python3 <<\'"\'"\'PYCODE\'"\'"\'\nimport subprocess\nsubprocess.run([\'"\'"\'uv\'"\'"\', \'"\'"\'run\'"\'"\', \'"\'"\'/catalog/gh-plan.py\'"\'"\', \'"\'"\'--repo\'"\'"\', \'"\'"\'client/repo\'"\'"\', \'"\'"\'claim\'"\'"\', \'"\'"\'12\'"\'"\'])\nPYCODE\n\'\ntrue'], True),
            ('unused_literal_stdin_function', ["gen() {\npython3 <<'PYCODE'\nimport subprocess\nsubprocess.run(['uv', 'run', '/catalog/gh-plan.py', '--repo', 'client/repo', 'claim', '12'])\nPYCODE\n}; true"], False),
            ('reached_literal_stdin_function', ["gen() {\npython3 <<'PYCODE'\nimport subprocess\nsubprocess.run(['uv', 'run', '/catalog/gh-plan.py', '--repo', 'client/repo', 'claim', '12'])\nPYCODE\n}; gen; true"], True),
            ('skipped_literal_here_string', ['false && python3 <<< \'import subprocess\nsubprocess.run([\'"\'"\'uv\'"\'"\', \'"\'"\'run\'"\'"\', \'"\'"\'/catalog/gh-plan.py\'"\'"\', \'"\'"\'--repo\'"\'"\', \'"\'"\'client/repo\'"\'"\', \'"\'"\'claim\'"\'"\', \'"\'"\'12\'"\'"\'])\'; true'], False),
            ('reached_literal_here_string', ['python3 <<< \'import subprocess\nsubprocess.run([\'"\'"\'uv\'"\'"\', \'"\'"\'run\'"\'"\', \'"\'"\'/catalog/gh-plan.py\'"\'"\', \'"\'"\'--repo\'"\'"\', \'"\'"\'client/repo\'"\'"\', \'"\'"\'claim\'"\'"\', \'"\'"\'12\'"\'"\'])\'; true'], True),
            ('skipped_heredoc_expansion', ['false && cat <<EOF\n$(uv run /catalog/gh-plan.py --repo client/repo claim 12)\nEOF\ntrue'], False),
            ('reached_heredoc_expansion', ['cat <<EOF\n$(uv run /catalog/gh-plan.py --repo client/repo claim 12)\nEOF\ntrue'], True),
            ('unknown_heredoc_expansion', ['[ -n "$X" ] && cat <<EOF\n$(uv run /catalog/gh-plan.py --repo client/repo claim 12)\nEOF\ntrue'], True),
            ('zero_body_loop_heredoc_expansion', ['while false; do :; done <<EOF\n$(uv run /catalog/gh-plan.py --repo client/repo claim 12)\nEOF\ntrue'], True),
            ('skipped_loop_heredoc_expansion', ['false && while false; do :; done <<EOF\n$(uv run /catalog/gh-plan.py --repo client/repo claim 12)\nEOF\ntrue'], False),
            ('zero_body_loop_literal_input', ["while false; do python3; done <<'PYCODE'\nimport subprocess\nsubprocess.run(['uv', 'run', '/catalog/gh-plan.py', '--repo', 'client/repo', 'claim', '12'])\nPYCODE\ntrue"], False),
            ('compound_redirection_before_reader', ['while bash run.sh; do break; done > run.sh'], False),
            ('skipped_compound_redirection_preserves_source', ['false && while false; do :; done > run.sh; bash run.sh'], True),
            ('context_unknown_root_command', ['hash echo=$PWD/run.sh; command echo safe'], True),
            ('context_known_bash_command', ["/bin/bash -c 'hash -p ./run.sh echo; command echo safe'"], False),
            ('context_known_zsh_command', ["/bin/zsh -c 'hash echo=$PWD/run.sh; command echo safe'"], True),
            ('context_known_bash_shopt', ["/bin/bash -c 'hash -p ./run.sh shopt; shopt'"], False),
            ('context_known_zsh_shopt', ["/bin/zsh -c 'hash shopt=$PWD/run.sh; shopt'"], True),
            ('context_copied_bash', ["/bin/bash -c 'hash -p ./run.sh echo; (command echo safe)'"], False),
            ('context_eval_bash', ['/bin/bash -c \'hash -p ./run.sh echo; eval \'"\'"\'command echo safe\'"\'"\'\''], False),
            ('context_nested_bash', ['/bin/zsh -c \'/bin/bash -c \'"\'"\'hash -p ./run.sh echo; command echo safe\'"\'"\'\''], False),
            ('context_hash_wrapper_alternative', ["printf 'echo safe\\n' > safe.sh; hash -p ./safe.sh bash; bash -c 'hash echo=$PWD/run.sh; command echo safe'"], True),
            ('context_hash_bash_zsh_hash_binding', ["/bin/bash -c 'hash cp=./run.sh; cp a b'"], False),
            ('context_hash_bash_zsh_commands_binding', ["/bin/bash -c 'commands[cp]=./run.sh; cp a b'"], False),
            ('context_hash_zsh_bash_hash_binding', ["/bin/zsh -c 'hash -p ./run.sh cp || true; cp a b'"], False),
            ('context_hash_zsh_invalid_replacement_keeps_binding', ["/bin/zsh -c 'hash cp=./run.sh; hash -p ./safe.sh cp || true; cp a b'"], True),
            ('context_hash_unknown_invalid_replacement_keeps_binding', ['hash cp=./run.sh; hash -p ./safe.sh cp || true; cp a b'], True),
            ('context_hash_zsh_named_dir_does_not_delete', ["/bin/zsh -c 'hash cp=./run.sh; hash -d cp || true; cp a b'"], True),
            ('context_hash_unknown_named_dir_does_not_delete', ['hash cp=./run.sh; hash -d cp || true; cp a b'], True),
            ('context_hash_bash_delete_clears', ["/bin/bash -c 'hash -p ./run.sh cp; hash -d cp; cp a b'"], False),
            ('context_hash_unknown_bash_origin_delete_clears', ['hash -p ./run.sh cp; hash -d cp; cp a b'], False),
            ('context_hash_unknown_bash_origin_replacement_clears', ['hash -p ./run.sh cp; hash -p ./safe.sh cp; cp a b'], False),
            ('context_hash_zsh_replacement_clears', ["/bin/zsh -c 'hash cp=./run.sh; hash cp=./safe.sh; cp a b'"], False),
            ('context_hash_unknown_zsh_origin_replacement_clears', ['hash cp=./run.sh; hash cp=./safe.sh; cp a b'], False),
            ('context_hash_unknown_shared_reset_clears', ['hash cp=./run.sh; hash -r; cp a b'], False),
            ('context_dispatch_bash_hash_command_echo', ['hash -p ./run.sh echo; command echo safe'], False),
            ('context_dispatch_zsh_hash_command_echo', ['hash echo=$PWD/run.sh; command echo safe'], True),
            ('context_dispatch_zsh_hash_bash_disable_echo', ['hash echo=$PWD/run.sh; enable -n echo || true; echo safe'], False),
            ('context_dispatch_bash_hash_bash_disable_echo', ['hash -p ./run.sh echo; enable -n echo; echo safe'], True),
            ('context_dispatch_zsh_hash_bash_disable_command_echo', ['hash echo=$PWD/run.sh; enable -n echo || true; command echo safe'], True),
            ('context_zsh_alias_core', ["/bin/zsh -c 'hash alias=$PWD/run.sh; alias'"], False),
            ('context_unknown_alias_bash_feature', ['hash -p ./run.sh alias; alias'], True),
            ('malformed_eval_keeps_prior_reader', ['bash run.sh; eval "echo it\'s" || true'], True),
            ('malformed_child_keeps_prior_reader', ['bash run.sh; bash -c "echo it\'s" || true'], True),
            ('malformed_heredoc_keeps_prior_reader', ["bash run.sh; sh -c 'cat <<EOF'"], True),
            ('malformed_eval_alone', ['eval "echo it\'s" || true'], False),
            ('malformed_eval_prefix', ['eval "bash run.sh\necho it\'s" || true'], True),
            ('malformed_bash_child_prefix', ['/bin/bash -c "bash run.sh\necho it\'s" || true'], True),
            ('malformed_zsh_child_prefix', ['/bin/zsh -c "bash run.sh\necho it\'s" || true'], False),
            ('malformed_eval_later_heredoc', ["eval 'bash run.sh\ncat <<EOF'"], True),
            ('eof_heredoc_same_line', ["eval 'bash run.sh; cat <<EOF'"], True),
            ('eof_heredoc_zsh_same_line', ["/bin/zsh -c 'bash run.sh; cat <<EOF'"], True),
            ('eof_heredoc_code_reader', ["eval 'bash <<EOF\nbash run.sh'"], True),
            ('literal_stdin_terminated_reader', ["bash <<'EOF'\nbash run.sh\nEOF"], True),
            ('literal_stdin_here_string_reader', ["bash <<< 'bash run.sh'"], True),
            ('literal_stdin_overridden', ["bash <<'EOF' </dev/null\nbash run.sh\nEOF"], False),
            ('literal_stdin_written_before_read', ["bash <<'EOF'\nprintf '%s\\n' " + shlex.quote(writer) + " > generated.sh\nbash generated.sh\nEOF"], True),
            ('literal_stdin_replaced_before_read', ["bash <<'EOF'\nprintf safe > run.sh && bash run.sh\nEOF"], False),
            ('literal_stdin_fresh_function_scope', ["gen() { bash run.sh; }; bash <<'EOF'\ngen\nEOF"], False),
            ('literal_stdin_eval_source_scope', ["gen() { bash run.sh; }; eval 'source /dev/stdin' <<'EOF'\ngen\nEOF"], True),
            ('literal_stdin_dot_scope', ["gen() { bash run.sh; }; . /dev/stdin <<'EOF'\ngen\nEOF"], True),
            ('literal_stdin_consumed_once', ["bash <<'EOF'\nbash\nEOF"], False),
            ('literal_zsh_stdin_malformed_eval', ['zsh <<\'EOF\'\neval "bash run.sh\necho it\'s" || true\nEOF'], False),
            ('eof_heredoc_direct_writer', ['eval ' + shlex.quote('bash <<EOF\n' + writer)], True),
            ('eof_heredoc_data', ["eval 'cat <<EOF\nbash run.sh'"], False),
            ('eof_heredoc_incomplete_compound', ["eval 'if true; then\nbash run.sh; cat <<EOF'"], False),
            ('malformed_eval_same_line', ['eval "bash run.sh; echo it\'s" || true'], False),
            ('malformed_eval_if', ['eval "if true; then\nbash run.sh\necho it\'s" || true'], False),
            ('malformed_eval_function', ['eval "gen() {\nbash run.sh\necho it\'s" || true'], False),
            ('malformed_eval_and', ['eval "bash run.sh &&\necho it\'s" || true'], False),
            ('malformed_eval_pipe', ['eval "bash run.sh |\necho it\'s" || true'], False),
            ('malformed_eval_quote', ['eval "printf \'data\nbash run.sh\necho it\'s" || true'], False),
            ('malformed_eval_complete_if', ['eval "if true; then\nbash run.sh\nfi\necho it\'s" || true'], True),
            ('malformed_eval_after_prefix', ['eval "bash run.sh\nif true; then\necho it\'s" || true'], True),
            ('malformed_eval_heredoc_data', ['eval "cat <<EOF\nbash run.sh\nEOF\necho it\'s" || true'], False),
            ('malformed_eval_and_skip', ['eval "echo it\'s" && bash run.sh'], False),
            ('malformed_zsh_eval', ["/bin/zsh -c " + shlex.quote('eval "bash run.sh\necho it\'s" || true')], False),
            ('malformed_zsh_eval_function', ["/bin/zsh -c " + shlex.quote('gen() { bash run.sh; }; eval "gen\necho it\'s" || true')], False),
            ('malformed_replacement_keeps_owner', ['eval "printf safe > run.sh\necho it\'s" || true', 'bash run.sh'], True),
            ('malformed_replacement_guard_keeps_proof', ['eval "printf safe > run.sh && bash run.sh\necho it\'s" || true'], False),
            ('computed_function_word', ['gen() { bash run.sh; }; f=gen; $f'], True),
            ('forwarded_function_word', ['run() { "$@"; }; gen() { bash run.sh; }; run gen'], True),
            ('computed_word_without_function', ['f=true; $f'], False),
            ('zsh_disable_unknown_context', ['hash echo=$PWD/run.sh; disable echo; echo safe'], True),
            ('zsh_disable_child', ["/bin/zsh -c 'hash echo=$PWD/run.sh; disable echo; echo safe'"], True),
            ('zsh_disable_invalid_in_bash', ["/bin/bash -c 'hash echo=$PWD/run.sh; disable echo; echo safe'"], False),
            ('zsh_explicit_disable', ["/bin/zsh -c 'hash echo=$PWD/run.sh; builtin disable echo; echo safe'"], True),
            ('zsh_enable_restores_builtin', ["/bin/zsh -c 'hash echo=$PWD/run.sh; disable echo; enable echo; echo safe'"], False),
            ('zsh_pattern_disable_exact', ["/bin/zsh -c 'hash echo=./run.sh; disable -m echo; echo safe'"], True),
            ('zsh_pattern_disable', ["/bin/zsh -c " + shlex.quote("hash echo=./run.sh; disable -m 'ec*'; echo safe")], True),
            ('zsh_pattern_restore', ["/bin/zsh -c " + shlex.quote("hash echo=./run.sh; disable echo; enable -m 'ec*'; echo safe")], False),
            ('bash_invalid_pattern_restore', ["/bin/bash -c 'hash -p ./run.sh echo; enable -n echo; enable -m echo || true; echo safe'"], True),
            ('zsh_pattern_alias_domain', ["/bin/zsh -c 'hash echo=./run.sh; disable -ma echo; echo safe'"], False),
            ('zsh_pattern_function_domain', ["/bin/zsh -c 'hash echo=./run.sh; disable -mf echo; echo safe'"], False),
            ('zsh_pattern_reserved_domain', ["/bin/zsh -c 'hash echo=./run.sh; disable -mr echo; echo safe'"], False),
            ('zsh_pattern_pattern_domain', ["/bin/zsh -c 'hash echo=./run.sh; disable -mp echo; echo safe'"], False),
            ('zsh_pattern_suffix_domain', ["/bin/zsh -c 'hash echo=./run.sh; disable -ms echo; echo safe'"], False),
            ('zsh_uncertain_pattern_disable', ["/bin/zsh -c " + shlex.quote("hash echo=./run.sh; disable -m 'e(ch)o'; echo safe")], True),
            ('zsh_uncertain_pattern_restore', ["/bin/zsh -c " + shlex.quote("hash echo=./run.sh; disable echo; enable -m 'e(ch)o'; echo safe")], True),
            ('ansi_literal_eval_reader', ["eval $'bash run.sh'"], True),
            ('ansi_literal_child_reader', ["bash -c $'bash run.sh'"], True),
            ('ansi_literal_producer', ["printf %s $'bash run.sh' | bash"], True),
        ]
        for harness in ("codex", "claude"):
            for label, commands, pending in hash_cases:
                with self.subTest(harness=harness, hash_dispatch=label):
                    self.assertEqual(receipts.ownership_state(scripts(harness, [hash_owner, *commands]), harness)["pending"], pending)

        # Shell lexical operators inside a word must not consume the next
        # executable unit as a false quote, group or heredoc body.
        ansi = "printf %s $'it\\'s'"
        lexical_cases = [("ansi_data", ansi, False),
                         ("ansi_substitution_data", "printf %s $'$(" + writer + ")'", False),
                         ("arithmetic_data", "echo $((1<<2))", False),
                         ("quoted_arithmetic_data", 'echo "$((1<<2))"', False),
                         ("quoted_group_data", "echo '((1<<2))'", False),
                         ("arithmetic_nested_writer", "echo $((1+$(" + writer + ")))", True),
                         ("bracket_arithmetic_data", "echo $[1<<2]", False),
                         ("bracket_nested_data", "echo $[1+$(printf 2)]", False),
                         ("ansi_control_tail_data", "printf %s $'\\c'", False),
                         ("ansi_control_escaped_quote_data", "printf %s $'\\c\\''", False),
                         ("ansi_control_character_data", "printf %s $'\\cX\\c?'", False),
                         ("backtick_data", "echo `printf %s gh-plan.py-claim`", False),
                         ("single_quoted_backtick", "echo '`" + writer + "`'", False),
                         ("escaped_backtick", "echo \\`" + writer + "\\`", False),
                         ("comment_backtick_pr", "# then `gh pr create --fill` after CI is green\ngit status", False),
                         ("comment_backtick_claim", "# claim with `gh-plan.py claim 2216` later\nls", False),
                         ("comment_dollar_claim", "# then $(" + writer + ") later\ngit status", False),
                         ("comment_apostrophe_writer", "# don't claim yet\necho `" + writer + "`", True),
                         ("inline_comment_writer", "ls # note\necho `" + writer + "`", True),
                         ("word_hash_writer", "echo word#`" + writer + "`", True),
                         ("quoted_word_hash_writer", 'echo "x"#`' + writer + '`', True),
                         ("escaped_space_hash_writer", "echo x\\ #`" + writer + "`", True),
                         ("quoted_hash_writer", 'echo "#`' + writer + '`"', True),
                         ("escaped_hash_writer", "echo \\#`" + writer + "`", True),
                         ("comment_no_newline", "# `" + writer + "`", False),
                         ("case_arithmetic_data", 'case "$m" in claim) ((n++));; esac', False),
                         ("child_quoted_substitution_data", "bash -c " + shlex.quote("printf %s '$(" + writer + ")'"), False),
                         ("skipped_child_subshell_expansion", "false && bash -c " + shlex.quote('out="$((cd /repo && ' + writer + ') 2>&1)"'), False),
                         ("skipped_child_bracket_writer", "false && bash -c " + shlex.quote("echo $[1+$(" + writer + ")]"), False),
                         ("ansi_escaped_writer", "eval $'uv \\x72un /catalog/gh-plan.py --repo client/repo claim 12'", True)]
        for label, prefix in (("ansi", ansi), ("shift", "echo $((1<<2))"), ("arithmetic_command", "((1<<2))"),
                              ("ansi_multiline", "printf %s $'it\\'s\ndata'"),
                              ("arithmetic_multiline", "echo $((1\n<<2))"),
                              ("bracket_arithmetic", "echo $[1<<2]"),
                              ("quoted_bracket_arithmetic", 'echo "$[1<<2]"'),
                              ("ansi_control_tail", "printf %s $'\\c'"),
                              ("ansi_control_escaped_quote", "printf %s $'\\c\\''"),
                              ("ansi_control_character", "printf %s $'\\cX\\c?'"),
                              ("time_arithmetic", "time ((x))"),
                              ("function_arithmetic", "f() ((x));")):
            body = prefix + "\n" + writer
            lexical_cases.extend(((label + "_root", body, True),
                                  (label + "_child", "bash -c " + shlex.quote(body), True),
                                  (label + "_stdin", "bash <<'BODY'\n" + body + "\nBODY", True)))
        for label, body in (
            ("case_arithmetic", 'case "$m" in claim) ((n++)); ' + writer + ' ;; esac'),
            ("parenthesized_case_arithmetic", 'case "$m" in (claim) ((n++)); ' + writer + ' ;; esac'),
            ("subshell_expansion", "out=$((cd /repo && " + writer + ") 2>&1)"),
            ("quoted_subshell_expansion", 'out="$((cd /repo && ' + writer + ') 2>&1)"'),
            ("nested_subshell_expansion", 'echo "$(printf %s $((cd /repo && ' + writer + ') 2>&1))"'),
            ("bracket_nested_writer", "echo $[1+$(" + writer + ")]"),
            ("backtick_writer", "echo `" + writer + "`"),
            ("quoted_backtick_writer", 'echo "`' + writer + '`"'),
            ("backtick_escaped_inner_quote", "echo `printf '%s' '\\`\\`'; " + writer + "`"),
        ):
            lexical_cases.extend(((label + "_root", body, True),
                                  (label + "_child", "bash -c " + shlex.quote(body), True),
                                  (label + "_stdin", "bash <<'BODY'\n" + body + "\nBODY", True)))
        for harness in ("codex", "claude"):
            for label, command, owning in lexical_cases:
                rows = scripts(harness, [command])
                rows.remove(terminal(rows, harness, "tool-0"))
                with self.subTest(harness=harness, shell_word=label):
                    state = receipts.ownership_state(rows, harness)
                    self.assertEqual(bool(state["indices"]), owning)
                    self.assertEqual(state["pending"], owning)
            with self.subTest(harness=harness, ansi_detached_completion=True):
                command = writer + "; " + ansi + "; nohup ./writer.sh &"
                self.assertTrue(receipts.ownership_state(scripts(harness, [command]), harness)["pending"])

        for harness in ("codex", "claude"):
            for commands in (("bash run.sh\necho it's",), ("printf safe > run.sh\necho it's", "bash run.sh")):
                with self.subTest(harness=harness, malformed_root=commands):
                    rows = scripts(harness, [hash_owner, *commands])
                    result = terminal(rows, harness, "tool-1")
                    result.clear()
                    result.update(shell_result({"exit_code": 2}, harness, "tool-1"))
                    self.assertTrue(receipts.ownership_state(rows, harness)["pending"])


        for harness in ("codex", "claude"):
            for shell, body, expected in (("/bin/bash", "hash echo=$PWD/run.sh; command echo safe", False),
                                          ("/bin/bash", "bash run.sh\necho it's", True),
                                          ("/bin/zsh", "bash run.sh\necho it's", False),
                                          ("/bin/bash", "hash shopt=$PWD/run.sh; shopt", False),
                                          ("/bin/zsh", "hash echo=$PWD/run.sh; command echo safe", True),
                                          (None, "hash echo=$PWD/run.sh; command echo safe", True),
                                          ({"untrusted": "bash"}, "hash echo=$PWD/run.sh; command echo safe", True)):
                with self.subTest(harness=harness, recorded_shell=shell):
                    rows = scripts(harness, [hash_owner, body])
                    if "\n" in body:
                        result = terminal(rows, harness, "tool-1")
                        result.clear()
                        result.update(shell_result({"exit_code": 2}, harness, "tool-1"))
                    if harness == "codex":
                        rows[3]["payload"]["arguments"] = json.dumps({"cmd": body, "shell": shell})
                    else:
                        # Bash's extra input key is not a shell-selection contract.
                        rows[3]["message"]["content"][0]["input"]["shell"] = shell
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], expected if harness == "codex" else True)
        for method in ("exec_command", "functions.exec_command"):
            for shell, expected in (("/bin/bash", False), ("/bin/zsh", True), (None, True)):
                with self.subTest(method=method, shell=shell):
                    effects = list(receipts.native_file_effects(method, {"cmd": "hash echo=$PWD/run.sh; command echo safe", "shell": shell}))
                    self.assertEqual(any(effect["kind"] == "read" and "./run.sh" in effect.get("paths", ()) for effect in effects), expected)
        for shell, expected in (("/bin/bash", False), ("/bin/zsh", True), (None, True)):
            with self.subTest(composed_shell=shell):
                args = {"cmd": "hash echo=$PWD/run.sh; command echo safe", "shell": shell, "workdir": "/repo"}
                effects = list(receipts.native_file_effects("functions.exec", "await tools.exec_command(" + json.dumps(args) + ")"))
                self.assertEqual(any(effect["kind"] == "read" and "./run.sh" in effect.get("paths", ()) for effect in effects), expected)


    def test_many_producer_streams_preserve_reader_and_data_behavior(self):
        for count in (12, 64):
            command = "; ".join(["echo 'a\\nb'"] * count)
            for harness in ("codex", "claude"):
                for suffix, pending in (("", False), (" | bash", False),
                                        ("; echo 'uv run /catalog/gh-plan.py --repo client/repo claim 12'; } | bash", True)):
                    text = "{ " + command + suffix if pending else "{ " + command + "; }" + suffix
                    rows = history(harness, {})
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": text})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": text}
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)


    def test_bounded_streams_preserve_computed_and_file_execution(self):
        prefix = "; ".join(["echo '#a\\n#b'"] * 12)
        python = "import os; os.system('uv run /catalog/' + 'gh-' + 'plan.py --repo client/repo ' + 'claim 12')"
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        commands = (
            ("{ " + prefix + "; printf '%s\\n' " + shlex.quote(python) + "; } | python3", True),
            ("{ " + prefix + "; echo " + shlex.quote(writer) + "; } > run.sh\nbash run.sh", True),
            ("{ " + prefix + "; } > run.sh\nbash run.sh", False),
        )
        for harness in ("codex", "claude"):
            for command, pending in commands:
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {})
                    if harness == "codex":
                        rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)


    def test_process_substitution_streams_reach_only_actual_code_readers(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        commands = (
            ("jq . <(" + writer + ")", True),
            ("while read -r l; do echo \"$l\"; done < <(" + writer + ")", True),
            ("printf done > >(" + writer + ")", True),
            ("echo " + shlex.quote("<(" + writer + ")"), False),
            ('echo "<(' + writer + ')"', False),
            ("cat <<EOF\n<(" + writer + ")\nEOF", False),
            ("bash <(echo " + shlex.quote(writer) + ")", True),
            ("source <(cat <<'EOF'\n" + writer + "\nEOF\n)", True),
            ("echo " + shlex.quote(writer) + " > >(bash)", True),
            ("echo " + shlex.quote(writer) + " | tee >(sh)", True),
            ("diff <(echo " + shlex.quote(writer) + ") b.txt", False),
            ("echo " + shlex.quote(writer) + " > >(gh issue comment 1 --body-file -)", False),
        )
        for harness in ("codex", "claude"):
            for command, pending in commands:
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {})
                    if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)

        create = 'uv run /catalog/gh-pr.py --repo client/repo create --title T'
        completed = (
            (create + " --body-file <(printf '%s\\n' \"$BODY\")", False),
            (create + " --body-file <(printf '%s\\n' " + shlex.quote(writer) + ")", False),
            (create + " --body B > >(printf done)", False),
            (create + " --body-file <(" + writer + ")", True),
            (create + " --body-file <((" + writer + ") | sort)", True),
            (create + " --body-file <((printf '%s\\n' " + shlex.quote(writer) + ") | sort)", False),
            (create + ' --body-file <(tool=/catalog/gh-plan.py; uv run "$tool" --repo client/repo claim 12)', True),
        )
        for harness in ("codex", "claude"):
            for command, pending in completed:
                with self.subTest(harness=harness, completed_process_body=command):
                    rows = history(harness, {})
                    if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                    rows[2] = shell_result({"exit_code": 0}, harness, "tool-0")
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)

    def test_coherent_echo_modes_do_not_invent_ownership_in_generated_scripts(self):
        bodies = ["#!/bin/sh", *["# ordinary\\ncomment $1" for _ in range(7)]]
        producer = "{ " + "; ".join("echo " + shlex.quote(body) for body in bodies) + "; }"
        for harness in ("codex", "claude"):
            for suffix in (" | bash", " > gen.sh\nbash gen.sh"):
                for prefix in ("", "set -euo pipefail; ", "set -o nounset; ", "shopt -s nullglob; ", "setopt pipefail; ",
                               "shopt -s xpg_echo; ", "setopt bsd_echo; ", "unsetopt bsd_echo; ", "set -o posix; ", "emulate sh; "):
                    command = prefix + producer + suffix
                    with self.subTest(harness=harness, command=command):
                        rows = history(harness, {})
                        if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                        else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                        self.assertFalse(receipts.ownership_state(rows, harness)["pending"])
            for query in ("shopt -q xpg_echo", "set -- -o posix", "emulate -l sh"):
                command = "{ " + "; ".join("echo " + shlex.quote(body) + "; " + query for body in bodies) + "; } | bash"
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {})
                    if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertFalse(receipts.ownership_state(rows, harness)["pending"])

    def test_explicit_child_shells_keep_independent_echo_modes(self):
        first = "echo " + shlex.quote(r"\x27")
        second = "echo " + shlex.quote(r"u\x76 run /catalog/gh-plan.py --repo client/repo claim 12")
        for harness in ("codex", "claude"):
            for command in ("{ bash -c " + shlex.quote(first) + "; zsh -c " + shlex.quote(second) + "; } | bash",
                            *("{ " + first + "; " + option + "; " + second + "; } | bash" for option in (
                                "shopt -s xpg_echo", "setopt bsd_echo", "unsetopt bsd_echo", "setopt no_bsd_echo",
                                "set -o posix", "set +o posix", "set -eo posix", "set -o bsd_echo", "emulate sh"))):
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {})
                    if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertTrue(receipts.ownership_state(rows, harness)["pending"])

    def test_other_history_candidates_use_its_latest_definitive_write(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        for harness in ("codex", "claude"):
            author, reader = history(harness, {}, {}), history(harness, {})
            for index, content in ((1, writer), (3, "echo done")):
                if harness == "codex":
                    author[index]["payload"]["arguments"] = json.dumps({"cmd": "cat > run.sh <<'EOF'\n" + content + "\nEOF"})
                else:
                    author[index]["message"]["content"][0].update(name="Write", input={"file_path": "/repo/run.sh", "content": content})
            if harness == "codex":
                author[2] = shell_result({"exit_code": 0, "output": ""}, harness, "tool-0")
                author[4] = shell_result({"exit_code": 0, "output": ""}, harness, "tool-1")
            if harness == "codex": reader[1]["payload"]["arguments"] = json.dumps({"cmd": "bash run.sh"})
            else: reader[1]["message"]["content"][0]["input"] = {"command": "bash run.sh"}
            histories = [{"harness": harness, "records": rows} for rows in (author, reader)]
            candidates = receipts.ownership_file_candidates(histories)
            self.assertFalse(receipts.ownership_state(reader, harness, file_candidates=candidates)["pending"])
            for write_at, read_at, replace_at, pending in ((1, 2, 3, True), (1, 3, 2, False), (2, 1, 3, False),
                                                        (1, 2, 2, True), (2, 2, 3, True)):
                with self.subTest(harness=harness, times=(write_at, read_at, replace_at)):
                    for row, at in ((author[1], write_at), (author[2], write_at),
                                    (reader[1], read_at), (reader[2], read_at),
                                    (author[3], replace_at), (author[4], replace_at)):
                        row["timestamp"] = f"2026-01-01T00:00:0{at}Z"
                    candidates = receipts.ownership_file_candidates(histories)
                    self.assertEqual(receipts.ownership_state(reader, harness, file_candidates=candidates)["pending"], pending)
            for row, at in ((author[1], 1), (author[2], 1), (author[3], 2), (author[4], 4),
                            (reader[1], 3), (reader[2], 3)):
                row["timestamp"] = f"2026-01-01T00:00:0{at}Z"
            candidates = receipts.ownership_file_candidates(histories)
            self.assertTrue(receipts.ownership_state(reader, harness, file_candidates=candidates)["pending"])
            # A different peer's equal-time writer must survive a simultaneous
            # harmless completion regardless of the histories' listing order.
            peer = history(harness, {})
            if harness == "codex":
                peer[1]["payload"]["arguments"] = json.dumps({"cmd": "cat > run.sh <<'EOF'\n" + writer + "\nEOF"})
            else:
                peer[1]["message"]["content"][0].update(name="Write", input={"file_path": "/repo/run.sh", "content": writer})
            peer[1]["timestamp"] = peer[2]["timestamp"] = "2026-01-01T00:00:04Z"
            for read_at in (4, 5):
                reader[1]["timestamp"] = f"2026-01-01T00:00:0{read_at}Z"
                for peers in ((author, peer), (peer, author)):
                    tied = [{"harness": harness, "records": rows} for rows in (*peers, reader)]
                    candidates = receipts.ownership_file_candidates(tied)
                    self.assertTrue(receipts.ownership_state(reader, harness, file_candidates=candidates)["pending"])
            reader[1]["timestamp"] = "2026-01-01T00:00:03Z"
            if harness == "codex":
                reader[1]["payload"].update(name="functions.exec", arguments=
                    'text(await tools.exec_command({cmd:"echo done"}));'
                    'text(await tools.exec_command({cmd:"bash run.sh"}));')
                reader[2]["payload"]["output"] = [
                    {"type": "text", "text": json.dumps({"exit_code": 0, "output": "done"})},
                    {"type": "text", "text": json.dumps({"exit_code": 0, "output": ""})}]
                # The replacement completes after the serial cell launched.
                # Split rows must retain the actual native launch timestamp.
                reader[2]["timestamp"] = "2026-01-01T00:00:05Z"
                candidates = receipts.ownership_file_candidates(histories)
                self.assertTrue(receipts.ownership_state(reader, harness, file_candidates=candidates)["pending"])

        for harness in ("codex", "claude"):
            for finish, replace_at, read_at, pending in ((9, 3, 10, True), (9, 3, 4, True),
                                                        (9, 11, 12, False), (None, 3, 10, True),
                                                        (None, 11, 12, True)):
                with self.subTest(harness=harness, owner_finish=finish, replace_at=replace_at, read_at=read_at):
                    owner, cleanup, reader = (history(harness, {}) for _ in range(3))
                    if harness == "codex":
                        owner[2] = shell_result({"exit_code": 0, "output": ""}, harness, "tool-0")
                        cleanup[2] = shell_result({"exit_code": 0, "output": ""}, harness, "tool-0")
                    for rows, content in ((owner, writer), (cleanup, "echo done")):
                        if harness == "codex":
                            rows[1]["payload"]["arguments"] = json.dumps({"cmd": "cat > run.sh <<'EOF'\n" + content + "\nEOF"})
                        else:
                            rows[1]["message"]["content"][0].update(name="Write", input={"file_path": "/repo/run.sh", "content": content})
                    if harness == "codex": reader[1]["payload"]["arguments"] = json.dumps({"cmd": "bash run.sh"})
                    else: reader[1]["message"]["content"][0]["input"] = {"command": "bash run.sh"}
                    owner[1]["timestamp"] = "2026-01-01T00:00:01Z"
                    if finish is not None:
                        owner[2]["timestamp"] = f"2026-01-01T00:00:{finish:02d}Z"
                    else:
                        owner.pop(2)
                    cleanup[1]["timestamp"] = "2026-01-01T00:00:02Z"
                    cleanup[2]["timestamp"] = f"2026-01-01T00:00:{replace_at:02d}Z"
                    reader[1]["timestamp"] = f"2026-01-01T00:00:{read_at:02d}Z"
                    histories = [{"harness": harness, "records": rows} for rows in (owner, cleanup, reader)]
                    candidates = receipts.ownership_file_candidates(histories)
                    self.assertEqual(receipts.ownership_state(reader, harness, file_candidates=candidates)["pending"], pending)

        for harness in ("codex", "claude"):
            for timed in (False, True):
                modes = (("missing", True), ("running", True), ("overlap", True),
                         ("terminal", False), ("future", False), ("detached", True))
                if harness == "codex":
                    modes += (("polled", False), ("late_poll", True))
                for mode, pending in modes:
                    with self.subTest(harness=harness, own_writer=mode, timed=timed):
                        rows = history(harness, {}, {})[:5]
                        command = "cat > run.sh <<'EOF'\n" + writer + "\nEOF"
                        if mode == "detached":
                            command = '{ sleep "$DELAY"; ' + command + "\n} &"
                        read = "printf 'echo done\\n' > run.sh && sleep 1 && bash run.sh"
                        if harness == "codex":
                            rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                            rows[3]["payload"]["arguments"] = json.dumps({"cmd": read})
                        else:
                            rows[1]["message"]["content"][0]["input"] = {"command": command}
                            rows[3]["message"]["content"][0]["input"] = {"command": read}
                        rows[2] = shell_result({"session_id": 123} if mode in {"running", "polled", "late_poll"} else {"exit_code": 0}, harness, "tool-0")
                        rows[4] = shell_result({"exit_code": 0}, harness, "tool-1")
                        if mode == "missing":
                            rows.pop(2)
                        elif mode == "overlap":
                            rows.append(rows.pop(2))
                        elif mode == "future":
                            rows = [rows[0], *rows[3:5], *rows[1:3]]
                        elif mode in {"polled", "late_poll"}:
                            poll = [{"type": "response_item", "payload": {"type": "function_call", "name": "write_stdin",
                                     "call_id": "writer-poll", "arguments": json.dumps({"session_id": 123})}},
                                    shell_result({"exit_code": 0}, harness, "writer-poll")]
                            if mode == "polled":
                                rows[3:3] = poll
                            else:
                                rows.extend(poll)
                        if timed:
                            for index, row in enumerate(rows[1:], 1):
                                row["timestamp"] = f"2026-01-01T00:00:{index:02d}Z"
                        histories = [{"harness": harness, "records": rows}]
                        candidates = receipts.ownership_file_candidates(histories)
                        self.assertEqual(receipts.ownership_state(rows, harness, file_candidates=candidates)["pending"], pending)
                        # Native binding must also work when the caller has no
                        # cross-history candidates to contribute.
                        self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)

        for harness in ("codex", "claude"):
            for timed in (False, True):
                for path in ("run.sh", "run.py"):
                    modes = (("missing", True), ("running", True), ("overlap", True), ("terminal", False),
                             ("overlapping_later_writer", True), ("overlapping_later_append", True),
                             ("overlapping_later_clear", False), ("overlapping_later_then_clear", False),
                             ("overlapping_later_harmless_append", False),
                             ("overlapping_later_running_harmless_append", False))
                    if harness == "codex":
                        modes += (("polled", False), ("late_poll", True))
                    for mode, pending in modes:
                        with self.subTest(harness=harness, replacement=mode, path=path, timed=timed):
                            python = path.endswith(".py")
                            overlapping = mode.startswith("overlapping_later_")
                            old = ("import subprocess\ndef main():\n    subprocess.run(" + repr(writer) + ", shell=True)\n") if python else writer
                            harmless_append = mode in {"overlapping_later_harmless_append", "overlapping_later_running_harmless_append"}
                            if python and harmless_append:
                                old += "main()\n"
                            new = "def main():\n    print('done')\n" if python else "echo done"
                            commands = ["cat > " + path + " <<'EOF'\n" + old + "\nEOF",
                                        'sleep "$DELAY"; cat > ' + path + " <<'EOF'\n" + new + "\nEOF"]
                            if python:
                                addition = "print('done')" if mode == "overlapping_later_clear" else "main()"
                                if harmless_append:
                                    addition = "print('hi')"
                                elif mode in {"overlapping_later_writer", "overlapping_later_append", "overlapping_later_then_clear"}:
                                    addition = "import subprocess\nsubprocess.run(" + repr(writer) + ", shell=True)"
                                commands.append("cat >> run.py <<'EOF'\n\n" + addition + "\nEOF")
                            elif overlapping:
                                content = "echo done" if mode == "overlapping_later_clear" or harmless_append else writer
                                redirect = ">>" if mode == "overlapping_later_append" or harmless_append else ">"
                                commands.append("cat " + redirect + " run.sh <<'EOF'\n" + content + "\nEOF")
                                commands[1] = "printf 'echo done\\n' > run.sh && sleep 30"
                            if mode == "overlapping_later_then_clear":
                                commands.append("cat > " + path + " <<'EOF'\n" + new + "\nEOF")
                            commands.append(("python3 " if python else "bash ") + path)
                            rows = history(harness, *({} for _ in commands))[:-2 if harness == "codex" else -1]
                            for index, command in enumerate(commands):
                                if harness == "codex":
                                    rows[2 * index + 1]["payload"]["arguments"] = json.dumps({"cmd": command})
                                else:
                                    rows[2 * index + 1]["message"]["content"][0]["input"] = {"command": command}
                                rows[2 * index + 2] = shell_result({"exit_code": 0}, harness, f"tool-{index}")
                            if mode in {"running", "polled", "late_poll"}:
                                rows[4] = shell_result({"session_id": 456}, harness, "tool-1")
                            elif mode == "missing":
                                rows.pop(4)
                            elif mode == "overlap":
                                rows.append(rows.pop(4))
                            elif overlapping:
                                if mode == "overlapping_later_running_harmless_append":
                                    rows[6] = shell_result({"session_id": 789}, harness, "tool-2")
                                completion = rows.pop(4)
                                rows.insert(len(rows) - 2, completion)
                            if mode in {"polled", "late_poll"}:
                                poll = [{"type": "response_item", "payload": {"type": "function_call", "name": "write_stdin",
                                         "call_id": "replacement-poll", "arguments": json.dumps({"session_id": 456})}},
                                        shell_result({"exit_code": 0}, harness, "replacement-poll")]
                                if mode == "polled":
                                    rows[5:5] = poll
                                else:
                                    rows.extend(poll)
                            if timed:
                                for index, row in enumerate(rows[1:], 1):
                                    row["timestamp"] = f"2026-01-01T00:00:{index:02d}Z"
                            histories = [{"harness": harness, "records": rows}]
                            candidates = receipts.ownership_file_candidates(histories)
                            with self.subTest(scope="own_with_candidates"):
                                self.assertEqual(receipts.ownership_state(rows, harness, file_candidates=candidates)["pending"], pending)
                            with self.subTest(scope="own_without_candidates"):
                                self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)

                            if overlapping and timed:
                                # The same overlap can be split across native
                                # writers and a reader, in either listing order.
                                peer_a = [rows[0], *rows[1:4], rows[-3]]
                                peer_b = [dict(rows[0]), *rows[4:-3]]
                                reader = [dict(rows[0]), *rows[-2:]]
                                for peers in ((peer_a, peer_b), (peer_b, peer_a)):
                                    nodes = [{"harness": harness, "records": value} for value in (*peers, reader)]
                                    candidates = receipts.ownership_file_candidates(nodes)
                                    with self.subTest(scope="peer", reversed=peers[0] is peer_b):
                                        self.assertEqual(receipts.ownership_state(reader, harness, file_candidates=candidates)["pending"], pending)
                with self.subTest(harness=harness, within_running_replacement=True, timed=timed):
                    rows = history(harness, {}, {})[:-2 if harness == "codex" else -1]
                    for index, command in enumerate(("cat > run.sh <<'EOF'\n" + writer + "\nEOF",
                                                     "sleep 1; printf 'echo done\\n' > run.sh && bash run.sh")):
                        if harness == "codex":
                            rows[2 * index + 1]["payload"]["arguments"] = json.dumps({"cmd": command})
                        else:
                            rows[2 * index + 1]["message"]["content"][0]["input"] = {"command": command}
                        rows[2 * index + 2] = shell_result({"session_id": 789} if index else {"exit_code": 0}, harness, f"tool-{index}")
                    if timed:
                        for index, row in enumerate(rows[1:], 1):
                            row["timestamp"] = f"2026-01-01T00:00:{index:02d}Z"
                    candidates = receipts.ownership_file_candidates([{"harness": harness, "records": rows}])
                    self.assertFalse(receipts.ownership_state(rows, harness, file_candidates=candidates)["pending"])

                for path in ("run.sh", "run.py"):
                    for active_writer, pending in (("unrelated", False), ("harmless_append", False), ("owning_replacement", True)):
                        with self.subTest(harness=harness, timed=timed, path=path, active_writer=active_writer):
                            python = path.endswith(".py")
                            owner = "import subprocess\nsubprocess.run(" + repr(writer) + ", shell=True)" if python else writer
                            harmless = "print('done')" if python else "echo done"
                            destination = "other.py" if python else "other.sh"
                            if active_writer != "unrelated":
                                destination = path
                            content = harmless if active_writer == "harmless_append" else owner
                            redirect = ">>" if active_writer == "harmless_append" else ">"
                            commands = ("cat > " + path + " <<'EOF'\n" + owner + "\nEOF",
                                        "cat " + redirect + " " + destination + " <<'EOF'\n" + content + "\nEOF",
                                        "cat > " + path + " <<'EOF' && " + ("python3 " if python else "bash ") + path + "\n" + harmless + "\nEOF")
                            rows = history(harness, {}, {}, {})[:-2 if harness == "codex" else -1]
                            for index, command in enumerate(commands):
                                if harness == "codex":
                                    rows[2 * index + 1]["payload"]["arguments"] = json.dumps({"cmd": command})
                                else:
                                    rows[2 * index + 1]["message"]["content"][0]["input"] = {"command": command}
                                rows[2 * index + 2] = shell_result({"session_id": 234} if index == 1 else {"exit_code": 0}, harness, f"tool-{index}")
                            rows.pop()
                            if timed:
                                for index, row in enumerate(rows[1:], 1):
                                    row["timestamp"] = f"2026-01-01T00:00:{index:02d}Z"
                            candidates = receipts.ownership_file_candidates([{"harness": harness, "records": rows}])
                            self.assertEqual(receipts.ownership_state(rows, harness, file_candidates=candidates)["pending"], pending)
                            self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)

    def test_older_partial_and_copy_writers_survive_overlapping_replacement(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        for harness in ("codex", "claude"):
            for style, path in (("append", "run.sh"), ("append", "run.py"), ("copy", "run.sh"), ("copy", "run.py")):
                for peer in (False, True):
                    for finish, pending in ((1, False), (3, True), (None, True)):
                        with self.subTest(harness=harness, style=style, path=path, peer=peer, finish=finish):
                            python = path.endswith(".py")
                            owner = "import subprocess\nsubprocess.run(" + repr(writer) + ", shell=True)" if python else writer
                            harmless = "print('done')" if python else "echo done"
                            template = "tmpl" + Path(path).suffix
                            setup = "cat > " + template + " <<'EOF'\n" + owner + "\nEOF"
                            active = ("sleep 5; cat >> " + path + " <<'EOF'\n" + owner + "\nEOF" if style == "append"
                                      else "sleep 5; cp " + template + " " + path)
                            commands = (setup, active, "cat > " + path + " <<'EOF'\n" + harmless + "\nEOF",
                                        ("python3 " if python else "bash ") + path)
                            rows = history(harness, {}, {}, {}, {})[:9]
                            for index, command in enumerate(commands):
                                if harness == "codex":
                                    rows[2 * index + 1]["payload"]["arguments"] = json.dumps({"cmd": command})
                                else:
                                    rows[2 * index + 1]["message"]["content"][0]["input"] = {"command": command}
                                rows[2 * index + 2] = shell_result({"exit_code": 0}, harness, f"tool-{index}")
                            for index, second in enumerate((0, 0, 1, finish or 5, 2, 2, 4, 4), 1):
                                rows[index]["timestamp"] = f"2026-01-01T00:00:{second:02d}Z"
                            completion = rows.pop(4)
                            if finish is not None:
                                rows.insert(4 if finish == 1 else 6, completion)
                            if peer:
                                author = [rows[0], *rows[1:4]]
                                reader = [dict(rows[0]), *rows[4:]]
                                if finish is not None:
                                    author.append(completion)
                                    reader.remove(completion)
                                nodes = [{"harness": harness, "records": value} for value in (author, reader)]
                            else:
                                reader = rows
                                nodes = [{"harness": harness, "records": rows}]
                            candidates = receipts.ownership_file_candidates(nodes)
                            self.assertEqual(receipts.ownership_state(reader, harness, file_candidates=candidates)["pending"], pending)
                            if not peer:
                                self.assertEqual(receipts.ownership_state(reader, harness)["pending"], pending)

    def test_unchanged_active_python_context_analysis_is_amortized(self):
        program = "import subprocess\nsubprocess.run(['uv','run','/catalog/gh-plan.py','--repo','client/repo','claim','12'])"
        for harness in ("codex", "claude"):
            for peer in (False, True):
                counts = []
                for reads in (3, 18):
                    rows = history(harness, *({} for _ in range(reads + 1)))
                    commands = ["cat > run.py <<'EOF'\n" + program + "\nEOF", *(["python3 run.py; python3 run.py"] * reads)]
                    for index, command in enumerate(commands):
                        if harness == "codex":
                            rows[2 * index + 1]["payload"]["arguments"] = json.dumps({"cmd": command})
                        else:
                            rows[2 * index + 1]["message"]["content"][0]["input"] = {"command": command}
                        rows[2 * index + 2] = shell_result({"session_id": 42} if index == 0 else {"exit_code": 0}, harness, f"tool-{index}")
                        for row in rows[2 * index + 1:2 * index + 3]:
                            row["timestamp"] = f"2026-01-01T00:00:{index:02d}Z"
                    candidates = ()
                    if peer:
                        author, rows = rows[:3], [dict(rows[0]), *rows[3:]]
                        candidates = receipts.ownership_file_candidates([{"harness": harness, "records": value}
                                                                        for value in (author, rows)])
                    with patch.object(receipts, "python_ownership", wraps=receipts.python_ownership) as classify:
                        self.assertTrue(receipts.ownership_state(rows, harness, file_candidates=candidates)["pending"])
                        counts.append(classify.call_count)
                with self.subTest(harness=harness, peer=peer):
                    self.assertLessEqual(counts[1], counts[0] * 2)

    def test_within_call_file_execution_obeys_write_order_and_replacement(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        scripts = (
            ("cat > run.sh <<'EOF'\n" + writer + "\nEOF\necho 'echo done' > run.sh && bash run.sh", False),
            ("bash run.sh\ncat > run.sh <<'EOF'\n" + writer + "\nEOF", False),
            ("cat > run.sh <<'EOF'\n" + writer + "\nEOF\nbash run.sh\necho 'echo done' > run.sh", True),
            ("for i in 1 2; do bash run.sh; cat > run.sh <<'EOF'\n" + writer + "\nEOF\ndone", True),
            ("while test -f again; do bash run.sh; cat > run.sh <<'EOF'\n" + writer + "\nEOF\ndone", True),
            ("f() { bash run.sh; }; cat > run.sh <<'EOF'\n" + writer + "\nEOF\nf", True),
            ("f() { bash run.sh; }; cat > run.sh <<'EOF'\n" + writer + "\nEOF", False),
            ("for i in 1 2; do bash run.sh; cat > run.sh <<'EOF'\n" + writer + "\nEOF\necho 'echo done' > run.sh; done", True),
            ("for i in 1; do bash run.sh; cat > run.sh <<'EOF'\n" + writer + "\nEOF\ndone", False),
            ("for i in 1 2; do bash run.sh; cat > run.sh <<'EOF'\n" + writer + "\nEOF\nbreak; done", False),
            ("while false; do cat > run.sh <<'EOF'\n" + writer + "\nEOF\ndone; bash run.sh", False),
            ("cat > run.sh <<'EOF'\n" + writer + "\nEOF\nf() { bash run.sh; }", False),
            ("f() { bash run.sh; }; cat > run.sh <<'EOF'\n" + writer + "\nEOF\necho 'echo done' > run.sh && f", False),
        )
        scripts += (("cat > run.sh <<'EOF'\n" + writer + "\nEOF\ncp missing.sh run.sh; bash run.sh", True),)
        for copier in ("cp", "mv", "install", "install -m 755", "cp -r"):
            # The actual destination receives the recorded executable source.
            scripts += (("cat > tmpl.sh <<'EOF'\n" + writer + "\nEOF\n" + copier + " tmpl.sh run.sh; bash run.sh", True),)
        for copier in ("cp tmpl.sh out", "cp tmpl.sh b.sh out"):
            scripts += (("cat > tmpl.sh <<'EOF'\n" + writer + "\nEOF\necho 'echo safe' > b.sh; mkdir out; " + copier + "; bash out/tmpl.sh", True),)
        for copier in ("cp -t out", "cp --target-directory=out"):
            scripts += (("cat > tmpl.sh <<'EOF'\n" + writer + "\nEOF\nmkdir out; " + copier + " tmpl.sh; bash out/tmpl.sh", True),)
        scripts += (("cat > tmpl.sh <<'EOF'\n" + writer + "\nEOF\ncp tmpl.sh run.sh; echo 'echo done' > tmpl.sh; bash run.sh", True),
                    ("cat > run.sh <<'EOF'\n" + writer + "\nEOF\necho 'echo done' > safe.sh && cp safe.sh run.sh && bash run.sh", False),
                    ("cat > tmpl/run.sh <<'EOF'\n" + writer + "\nEOF\ncp -r tmpl repo; bash repo/run.sh", True))
        scripts += (
            ("case dry in dry) echo " + shlex.quote(writer) + " > run.sh;; esac; bash run.sh", True),
            ("case wet in dry) echo " + shlex.quote(writer) + " > run.sh;; esac; bash run.sh", False),
            ("case dry in $PATTERN) echo " + shlex.quote(writer) + " > run.sh;; *) echo 'echo done' > run.sh;; esac; bash run.sh", True),
            ('case "$MODE" in dry) echo ' + shlex.quote(writer) + " > run.sh;; esac; bash run.sh", True),
            ("for i in 1 2; do bash run.sh; [ -f stop ] && break; cat > run.sh <<'EOF'\n" + writer + "\nEOF\ndone", True),
            ("for i in 1 2; do bash run.sh; [ -f stop ] && continue; cat > run.sh <<'EOF'\n" + writer + "\nEOF\ndone", True),
            ("f() { echo 'echo done' > run.sh; }; echo " + shlex.quote(writer) + " > run.sh; test -f dry && f; bash run.sh", True),
            ("f() { echo 'echo done' > run.sh; }; echo " + shlex.quote(writer) + " > run.sh; true && f && bash run.sh", False),
        )
        for branch, pending in (("false && echo 'echo done' > run.sh", True),
                                ("true || echo 'echo done' > run.sh", True),
                                ("test -f dry && echo 'echo done' > run.sh", True),
                                ("true && echo 'echo done' > run.sh", False),
                                ("false || echo 'echo done' > run.sh", False),
                                ("if false; then echo 'echo done' > run.sh; fi", True),
                                ("if test -f dry; then echo 'echo done' > run.sh; fi", True),
                                ("if true; then echo 'echo done' > run.sh; fi", False),
                                ("if false; then echo skipped; else echo 'echo done' > run.sh; fi", False),
                                ('case "$MODE" in dry) echo "echo done" > run.sh;; esac', True),
                                ("for f in *.bak; do echo 'echo done' > run.sh; done", True),
                                ("while test -f dry; do echo 'echo done' > run.sh; done", True),
                                ("for f in one; do echo 'echo done' > run.sh; done", False),
                                ("for i in one; do test -f stop && break; echo 'echo done' > run.sh; done", True),
                                ("for i in one; do false && break; echo 'echo done' > run.sh; done", False),
                                ("for i in one; do true && break; echo 'echo done' > run.sh; done", True),
                                ('case dry in dry) echo "echo done" > run.sh;; esac', False),
                                ("case dry in '*' ) echo 'echo done' > run.sh;; esac", True),
                                ('case dry in "*" ) echo "echo done" > run.sh;; esac', True),
                                (r"case dry in \* ) echo 'echo done' > run.sh;; esac", True),
                                ("case '*' in '*' ) echo 'echo done' > run.sh;; esac", False)):



            if pending:
                command = branch + "; bash run.sh"
            else:
                command = branch.replace(" > run.sh", " > run.sh && bash run.sh")
            scripts += (("echo " + shlex.quote(writer) + " > run.sh; " + command, pending),)
        # Pruning a definitely skipped owner still matters independently of
        # cleanup precision and success-dependent readers.
        scripts += (("false && echo " + shlex.quote(writer) + " > run.sh; bash run.sh", False),
                    ("true || echo " + shlex.quote(writer) + " > run.sh; bash run.sh", False),
                    ("if false; then echo " + shlex.quote(writer) + " > run.sh; fi; bash run.sh", False))
        for harness in ("codex", "claude"):
            for command, pending in scripts:
                with self.subTest(harness=harness, command=command):
                    rows = history(harness, {})
                    if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)


    def test_python_sparse_branch_changes_keep_aliases_and_new_values(self):
        writer = "uv run /catalog/gh-plan.py --repo client/repo claim 12"
        programs = (
            ("xs=[]\nalias=xs\nif flag:\n    if other: xs.append(CLAIM)\nelse: xs.clear()\nfor cmd in alias: os.system(cmd)", True),
            ("xs=[CLAIM]\nif flag:\n    if other: xs.clear()\n    else: xs.clear()\nelse: xs.clear()\nfor cmd in xs: os.system(cmd)", False),
            ("if flag:\n    xs=[]\n    def fill(out=xs): out.append(CLAIM)\n    fill()\nelse: xs=['echo safe']\nfor cmd in xs: os.system(cmd)", True),
            ("if flag:\n    xs=[]\n    xs.append('echo safe')\nelse:\n    xs=[]\n    xs.append('echo safe')\nfor cmd in xs: os.system(cmd)", False),
            ("class C: pass\nobj=C()\nobj.command='echo safe'\nif flag:\n    if other: obj.command=CLAIM\nos.system(obj.command)", True),
            ("class C: command=CLAIM\nif flag:\n    if other: C.command='echo safe'\n    else: C.command='echo safe'\nelse: C.command='echo safe'\nos.system(C.command)", False),
        )
        for program, expected in programs:
            with self.subTest(program=program):
                self.assertEqual(receipts.python_ownership("import os\nCLAIM=" + repr(writer) + "\n" + program), expected)

    def test_repeated_python_operands_do_not_repeat_lexical_analysis(self):
        counts = []
        for repeats in (4, 16):
            program = "text='echo safe'\n" + "result=transform(text)\n" * repeats
            with patch.object(receipts, "opaque_ownership", wraps=receipts.opaque_ownership) as classify:
                self.assertFalse(receipts.python_ownership(program))
                counts.append(classify.call_count)
        self.assertLessEqual(counts[1], counts[0] * 2)

    def test_repeated_file_bytes_do_not_repeat_python_analysis(self):
        for harness in ("codex", "claude"):
            counts = []
            for repeats in (4, 16):
                rows = history(harness, *({} for _ in range(repeats)))
                for index in range(repeats):
                    command = "cat > run.py <<'EOF'\nprint('safe data')\nEOF"
                    if harness == "codex":
                        rows[2 * index + 1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else:
                        rows[2 * index + 1]["message"]["content"][0]["input"] = {"command": command}
                with patch.object(receipts, "python_ownership", wraps=receipts.python_ownership) as classify:
                    candidates = receipts.ownership_file_candidates([{"harness": harness, "records": rows}])
                    self.assertFalse(receipts.ownership_state(rows, harness, file_candidates=candidates)["pending"])
                    counts.append(classify.call_count)
            with self.subTest(harness=harness):
                self.assertLessEqual(counts[1], counts[0] * 2)

    def test_python_branch_cost_depends_on_mutations_not_prior_allocations(self):
        for style in ("list", "dict", "instance", "class"):
            counts = []
            for size in (16, 64):
                if style == "class":
                    program = "class C: pass\n" * size
                else:
                    allocation = {"list": "[]", "dict": "{}", "instance": "C()"}[style]
                    program = ("class C: pass\n" if style == "instance" else "") + ("value = " + allocation + "\n") * size
                program += "if flag:\n    pass\n" * size
                calls = []
                def profile(frame, event, _argument):
                    if event == "call" and frame.f_code.co_filename == receipts.__file__:
                        calls.append(None)
                previous = sys.getprofile()
                try:
                    sys.setprofile(profile)
                    owning = receipts.python_ownership(program)
                finally:
                    sys.setprofile(previous)
                self.assertFalse(owning)
                counts.append(len(calls))
            with self.subTest(style=style):
                self.assertLessEqual(counts[1], counts[0] * 6)

    def test_python_repeated_clear_helpers_do_not_expand_with_call_paths(self):
        for style in ("function", "stored_function", "method", "constructor", "lexical_dag", "module_container", "keywords", "data_method", "escaped_container", "yield_from", "loop_yield_from", "generator_expression"):
            counts = []
            for depth in (3, 6):
                if style == "generator_expression":
                    expression = "['echo safe']"
                    for index in range(depth):
                        expression = f"(item{index} for item{index} in {expression})"
                    program = f"list({expression})"
                elif style in {"yield_from", "loop_yield_from"}:
                    program = "def leaf(): yield 'echo safe'\n"
                    for index in range(depth):
                        child = "leaf" if index == 0 else f"step{index - 1}"
                        body = f"yield from {child}(); yield from {child}()" if style == "yield_from" else f"\n    for _ in [1]: yield from {child}()"
                        program += f"def step{index}(): {body}\n"
                    program += f"list(step{depth - 1}())"
                elif style in {"module_container", "keywords", "data_method", "escaped_container"}:
                    parameter, forwarded = ("**kw", "**kw") if style == "keywords" else ("", "")
                    program = "import subprocess,shutil\nBASE=['echo']\n"
                    body = "shutil.copy('src','dest')" if style == "data_method" else "fill(BASE); subprocess.run(['echo','done'])" if style == "escaped_container" else "subprocess.run(BASE+['done'])"
                    program += f"def leaf({parameter}): {body}\n"
                    for index in range(depth):
                        child = "leaf" if index == 0 else f"step{index - 1}"
                        program += f"def step{index}({parameter}): {child}({forwarded}); {child}({forwarded})\n"
                    program += f"step{depth - 1}()"
                elif style == "lexical_dag":
                    program = "def a0(): pass\ndef b0(): pass\n"
                    for index in range(1, depth + 1):
                        for name in ("a", "b"):
                            program += f"def {name}{index}(): a{index - 1}(); b{index - 1}()\n"
                    program += f"a{depth}()"
                elif style == "constructor":
                    program = "import subprocess\nclass Leaf:\n    def __init__(self): subprocess.run(['echo','done'])\n    def run(self): pass\n"
                    for index in range(depth):
                        child = "Leaf" if index == 0 else f"Step{index - 1}"
                        program += f"class Step{index}:\n    def __init__(self): {child}().run()\n    def run(self): pass\n"
                    program += f"Step{depth - 1}().run()"
                else:
                    method = style == "method"
                    indent, parameter, receiver = ("    ", "self", "self.") if method else ("", "", "")
                    program = "import subprocess\n" + ("class Job:\n" if method else "")
                    program += f"{indent}def leaf({parameter}): subprocess.run(['echo','done'])\n"
                    for index in range(depth):
                        child = receiver + ("leaf" if index == 0 else f"step{index - 1}")
                        assignment = f"result{index} = " if style == "stored_function" else ""
                        program += f"{indent}def step{index}({parameter}): {assignment}{child}(); {child}()\n"
                    program += ("Job()." if method else "") + f"step{depth - 1}()"
                if style in {"lexical_dag", "yield_from", "loop_yield_from", "generator_expression"}:
                    calls = []
                    def profile(frame, event, _argument):
                        if event == "call" and frame.f_code.co_filename == receipts.__file__:
                            calls.append(None)
                    previous = sys.getprofile()
                    try:
                        sys.setprofile(profile)
                        owning = receipts.python_ownership(program)
                    finally:
                        sys.setprofile(previous)
                    self.assertFalse(owning)
                    counts.append(len(calls))
                else:
                    with patch.object(receipts, "ownership_command", wraps=receipts.ownership_command) as classify:
                        self.assertFalse(receipts.python_ownership(program))
                        counts.append(classify.call_count)
            with self.subTest(style=style):
                self.assertLessEqual(counts[1], counts[0] * (6 if style == "lexical_dag" else 3))


    def test_unknown_dictionary_keys_preserve_possible_values(self):
        writer = "uv run gh-plan.py --repo example/a claim 1"
        forms = (
            "d={}\nd.update([(a, COMMAND), (b, 'echo safe')])",
            "d={a: COMMAND, b: 'echo safe'}",
            "d={}\nd.update({a: COMMAND})\nd.update({b: 'echo safe'})",
            "d={**{a: COMMAND}, **{b: 'echo safe'}}",
            "d={a: COMMAND}\nd.update([(b, 'echo safe')])",
        )
        for form in forms:
            for command, owning in ((writer, True), ("echo safe", False)):
                program = ("import os,sys\na=sys.argv[1]\nb=sys.argv[2]\n"
                           + form.replace("COMMAND", repr(command))
                           + "\nfor command in d.values(): os.system(command)")
                for harness in ("codex", "claude"):
                    with self.subTest(harness=harness, form=form, owning=owning):
                        rows = history(harness, {})
                        shell = "python -c " + shlex.quote(program)
                        if harness == "codex":
                            rows[1]["payload"]["arguments"] = json.dumps({"cmd": shell})
                        else:
                            rows[1]["message"]["content"][0]["input"] = {"command": shell}
                        self.assertEqual(receipts.ownership_state(rows, harness)["pending"], owning)

    def test_dictionary_unknown_iterators_preserve_command_values(self):
        writer = "uv run gh-plan.py --repo example/a claim 1"
        operands = ("zip(['a','b'], [COMMAND, 'echo safe'])",
                    "reversed([('a', COMMAND), ('b', 'echo safe')])",
                    "filter(lambda pair: True, [('a', COMMAND), ('b', 'echo safe')])",
                    "itertools.zip_longest(['a','b'], [COMMAND, 'echo safe'])")
        for operand in operands:
            for build in ("d=dict(OPERAND)", "d={}\nd.update(OPERAND)"):
                for command, owning in ((writer, True), ("echo safe", False)):
                    program = ("import os,itertools\n" + build.replace("OPERAND", operand).replace("COMMAND", repr(command))
                               + "\nfor command in d.values(): os.system(command)")
                    self.assert_python_patch_ownership(program, owning)

    def test_mock_unknown_keyword_values_keep_snapshot_and_cache_state(self):
        setup = ("import os,sys\nfrom unittest.mock import patch,MagicMock\n"
                 "WRITER='uv run gh-plan.py --repo example/a claim 1'\n"
                 "PROGRAM='import os\\nos.system('+repr(WRITER)+')'\n"
                 "def record(*a,**k): return 0\n")
        programs = (
            ("opts={sys.argv[1]:0,'return_value':0}\nwith patch('os.system',MagicMock(**opts)):\n def f(): return 1\n f()\n exec(PROGRAM,{})", False),
            ("opts={sys.argv[1]:os.system}\nwith patch('os.system',MagicMock(**opts)): exec(PROGRAM,{})", True),
            ("m=MagicMock(**{sys.argv[1]:WRITER})\nos.system(m.payload)", True),
            ("opts={sys.argv[1]:0}\nwith patch('os.system',MagicMock(wraps=os.system,**opts)): exec(PROGRAM,{})", True),
            ("opts={sys.argv[1]:0}\nwith patch('os.system',MagicMock(wraps=os.system,return_value=0,**opts)): exec(PROGRAM,{})", False),
            ("m=MagicMock(**{sys.argv[1]:os.system})\nm.side_effect=record\nm._mock_wraps=None\nm.return_value=0\nwith patch('os.system',m): exec(PROGRAM,{})", False),
            ("m=MagicMock(**{sys.argv[1]:WRITER})\nm.payload='echo safe'\nos.system(m.payload)", False),
            ("opts={'return_value':0}\nconfigure(opts)\nwith patch('os.system',MagicMock(**opts)):\n def f(): return 1\n f()\n exec(PROGRAM,{})", True),
        )
        for body, expected in programs:
            self.assert_python_patch_ownership(setup + body, expected)
            if expected:
                self.assert_python_patch_ownership((setup + body).replace("uv run gh-plan.py --repo example/a claim 1", "echo safe"), False)

    def test_dynamic_dictionary_constructor_and_keywords_keep_values(self):
        writer = "uv run gh-plan.py --repo example/a claim 1"
        forms = (
            "d=dict([(a, COMMAND), ('k', 'echo safe')])",
            "d=dict({a: COMMAND, b: 'echo safe'})",
            "d=dict(**{a: COMMAND, b: 'echo safe'})",
            "d={}\nd.update(**{a: COMMAND})\nd.update(**{b: 'echo safe'})",
            "def collect(**kw): return kw\nd=collect(**{a: COMMAND, b: 'echo safe'})",
            "from functools import partial\ndef collect(**kw): return kw\nd=partial(collect, **{a: COMMAND})(known='echo safe')",
            "from functools import partial\ndef collect(**kw): return kw\nd=partial(collect, **{a: COMMAND})(**{b: 'echo safe'})",
            "def collect(command='echo safe'): return {'value': command}\nd=collect(**{a: COMMAND})",
            "def collect(*, command='echo safe'): return {'value': command}\nd=collect(**{a: COMMAND})",
        )
        for form in forms:
            for command, owning in ((writer, True), ("echo safe", False)):
                program = ("import os,sys\na=sys.argv[1]\nb=sys.argv[2]\n"
                           + form.replace("COMMAND", repr(command))
                           + "\nfor command in d.values(): os.system(command)")
                for harness in ("codex", "claude"):
                    with self.subTest(harness=harness, form=form, owning=owning):
                        rows = history(harness, {})
                        shell = "python -c " + shlex.quote(program)
                        if harness == "codex":
                            rows[1]["payload"]["arguments"] = json.dumps({"cmd": shell})
                        else:
                            rows[1]["message"]["content"][0]["input"] = {"command": shell}
                        self.assertEqual(receipts.ownership_state(rows, harness)["pending"], owning)

    def test_python_process_launches_resolve_only_literal_concatenation(self):
        writer = "uv run /catalog/gh-plan.py --repo client/repo claim 12"
        programs = (
            *[("import os\ndef deco(fn):\n    " + wrapper + "\nclass C:\n    command=" + repr(command) + "\n    def __init__(self,command): self.command=command\n" + descriptor + "    @deco\n    def run(" + parameter + "): os.system(" + parameter + ".command)\n" + consumer, owning)
              for wrapper in ("def wrapped(*a, **k): return fn(*a, **k)\n    return wrapped", "return lambda *a, **k: fn(*a, **k)", "def wrapped(receiver,*a,**k): return fn(receiver,*a,**k)\n    return wrapped")
              for descriptor, parameter, consumer in (
                  ("", "self", "C(" + repr(writer) + ").run()"),
                  ("    @classmethod\n", "cls", "C.run()"),
                  ("    @classmethod\n", "cls", "C('echo unused').run()"),
                  ("    @property\n", "self", "print(C(" + repr(writer) + ").run)"),
              ) for command, owning in ((writer, True), ("echo safe", False))
              for consumer in (consumer.replace(repr(writer), repr(command)),)],
            *[("import os\ndef deco(fn): return lambda *a, **k: fn(*a, **k)\nclass C:\n    @staticmethod\n    @deco\n    def run(command): os.system(command)\n" + consumer + "(" + repr(command) + ")", owning)
              for consumer in ("C.run", "C().run") for command, owning in ((writer, True), ("echo safe", False))],
            ("import os\ndef deco(fn): return lambda *a: fn(*a)\nclass C:\n    @property\n    @deco\n    def run(self): os.system(" + repr(writer) + ")\nprint(C.run)", False),
            ("import os\ndef deco(fn): return lambda *a: fn(*a)\nclass C:\n    @deco\n    def run(self): os.system(" + repr(writer) + ")\nprint(C().run)", False),
            *[("import os,json\nimport supplied_module as supplied\nd={}\nd.update(" + update + ")\n" + clear + "for key in d: os.system(" + repr(command) + ")", owning)
              for update, clear, runs in (
                  ("json.loads(open('cfg').read())", "", True),
                  ("supplied.load()", "", True),
                  ("supplied.load(" + repr(writer) + ")", "", True),
                  ("[]", "", False),
                  ("()", "", False),
                  ("{}", "", False),
                  ("''", "", False),
                  ("((key,value) for key,value in [])", "", False),
                  ("[('key', 'value')]", "", True),
                  ("(('key', 'value'),)", "", True),
                  ("((key,value) for key,value in [('key','value')])", "", True),
                  ("json.loads(open('cfg').read())", "d.clear()\n", False),
              ) for command, is_writer in ((writer, True), ("echo safe", False)) for owning in (runs and is_writer,)],
            *[("import os,json\ndef deco(fn):\n    d={}\n    d.update(json.loads(open('cfg').read()))\n    for key in d: return fn\n@deco\ndef claim(): os.system(" + repr(command) + ")\nclaim()", owning)
              for command, owning in ((writer, True), ("echo safe", False))],
            *[("import os\nd={}\nd.update(" + entries + ")\nos.system(d['key'])", owning)
              for template in ("[('key',COMMAND)]", "(('key',COMMAND),)")
              for command, owning in ((writer, True), ("echo safe", False)) for entries in (template.replace("COMMAND", repr(command)),)],
            *[("import click,os\n@click.group()\ndef cli(): pass\n@cli.group()\ndef sub(): pass\n" + nested + "@" + group + ".command()\ndef claim(): os.system(" + repr(command) + ")\n" + consumer, owning)
              for nested, group, arguments in (("", "sub", "['sub','claim']"), ("@sub.group()\ndef deep(): pass\n", "deep", "['sub','deep','claim']"))
              for command, is_writer in ((writer, True), ("echo safe", False))
              for consumer, invoked in (("cli(" + arguments + ")", True), ("print(cli)", False)) for owning in (is_writer and invoked,)],
            *[("import os\nfrom builtins import " + kind + " as descriptor\ndef deco(fn): return lambda *a,**k: fn(*a,**k)\nclass C:\n    command=" + repr(command) + "\n    @descriptor\n    @deco\n    def run(receiver): os.system(receiver.command)\n" + consumer, owning)
              for kind, consumer in (("classmethod", "C.run()"), ("property", "print(C().run)"))
              for command, owning in ((writer, True), ("echo safe", False))],
            *[("import os\ndef deco(fn): return lambda *a,**k: fn(*a,**k)\nclass C:\n    @deco\n    @staticmethod\n    def run(command): os.system(command)\nC.run(" + repr(command) + ")", owning)
              for command, owning in ((writer, True), ("echo safe", False))],
            *[("import os\nclass Wrapper:\n    def __call__(self): os.system(" + repr(command) + ")\ndef deco(fn): return Wrapper()\nclass C:\n    @deco\n    def run(self): pass\nC().run()", owning)
              for command, owning in ((writer, True), ("echo safe", False))],
            *[("import os\nd={'key':" + repr(writer) + "}\nd.update(" + update + ")\nos.system(d['key'])", False)
              for update in ("[('key','echo safe')]", "[('key'," + repr(writer) + "),('key','echo safe')]", "{'key':'echo safe'}")],
            *[("import os\ndef deco(fn):\n    " + body + "\n@deco\ndef command(): os.system(" + repr(writer) + ")\ntry: command()\nexcept TypeError: pass", False)
              for body in ("pass", "return None", "yield fn", "return 'data'", "return []")],
            *[("import os\nclass Wrapper:\n    def __init__(self,fn): self.fn=fn\n    def __call__(self): os.system(" + repr(command) + ")\ndef deco(fn): return Wrapper(fn)\n@deco\ndef command(): pass\ncommand()", owning)
              for command, owning in ((writer, True), ("echo safe", False))],
            *[("import os\nclass Decorate:\n    def __call__(self,fn):\n        os.system(" + repr(command) + ")\n        return fn\n@Decorate()\ndef command(): pass\nprint(command)", owning)
              for command, owning in ((writer, True), ("echo safe", False))],
            *[("import os,functools\n" + setup + "\ndef deco(fn):\n    def wrapped(*a, **k): return fn(*a, **k)\n    return " + replacement + "\n@deco\ndef command(): os.system(" + repr(command) + ")\ncommand()", owning)
              for setup, replacement in (
                  ("", "functools.update_wrapper(wrapped,fn)"),
                  ("", "functools.wraps(fn)(wrapped)"),
                  ("", "functools.lru_cache()(fn)"),
                  ("import supplied_module", "supplied_module.wrap(fn)"),
                  ("class Wrapper:\n    def __init__(self,fn): self.fn=fn\n    def __call__(self): return self.fn()", "Wrapper(fn)"),
              ) for command, owning in ((writer, True), ("echo safe", False))],
            ("import os,functools\ndef deco(fn):\n    def wrapped(): return fn()\n    return functools.update_wrapper(wrapped,fn)\n@deco\ndef command(): os.system(" + repr(writer) + ")\nprint(command)", False),
            *[("import os\ndef decorate(fn):\n    def wrapped(): return fn()\n    return wrapped\ndef run(c):\n" + initial + "    def inner(): return x\n" + decoration + "    x=c\n    return inner()\nprint(run('echo safe'))\nos.system(run(" + repr(command) + "))", owning)
              for initial, decoration in (
                  ("    @decorate\n", ""),
                  ("", "    inner=decorate(inner)\n"),
                  ("    x='echo safe'\n    @decorate\n", ""),
              ) for command, owning in ((writer, True), ("echo safe", False))],
            *[("import os\ndef handler(fn): return lambda: fn()\ndef make(c):\n    def inner(): return x\n    get=handler(inner)\n    x=c\n    return get\nprint(make('echo safe')())\nos.system(make(" + repr(command) + ")())", owning)
              for command, owning in ((writer, True), ("echo safe", False))],
            *[("import os\ndef register(cls):\n    os.system(" + repr(command) + ")\n    return cls\n@register\nclass C: pass", owning)
              for command, owning in ((writer, True), ("echo safe", False))],
            *[("import os\ndef register(cls):\n    cls().run()\n    return cls\n@register\nclass C:\n    def run(self): os.system(" + repr(command) + ")", owning)
              for command, owning in ((writer, True), ("echo safe", False))],
            ("import os\ndef identity(cls): return cls\n@identity\nclass C:\n    def run(self): os.system(" + repr(writer) + ")\nprint(C)", False),
            ("import os\ndef execute(cls):\n    os.system(cls.command)\n    return cls\ndef arm(cls):\n    cls.command=" + repr(writer) + "\n    return cls\n@execute\n@arm\nclass C: command='echo safe'", True),
            *[("import click,os\ndef decorate(fn): return fn\n@decorate\n@click.group()\ndef cli(): pass\n@cli.command()\ndef claim(): os.system(" + repr(writer) + ")\n" + consumer, owning)
              for consumer, owning in (("cli(['claim'])", True), ("print(cli)", False))],
            *[("import click,functools,os\ndef decorate(fn): return functools.update_wrapper(fn,fn)\n@decorate\n@click.group()\ndef cli(): pass\n@cli.command()\ndef claim(): os.system(" + repr(writer) + ")\n" + consumer, owning)
              for consumer, owning in (("cli(['claim'])", True), ("print(cli)", False))],
            ("import os\ndef identity(fn): return lambda: fn()\ndef run():\n    x='echo safe'\n    def inner(): return x\n    wrapped=identity(inner)\n    print(wrapped())\n    x=" + repr(writer) + "\n    os.system(wrapped())\nrun()", True),
            ("import os\ndef identity(fn): return lambda: fn()\ndef run():\n    x=" + repr(writer) + "\n    def inner(): return x\n    wrapped=identity(inner)\n    print(wrapped())\n    x='echo safe'\n    os.system(wrapped())\nrun()", False),
            *[("import os\nimport supplied_module as supplied\ndef unknown(cls): return supplied.wrap(cls)\n@unknown\nclass C:\n    def run(self): os.system(" + repr(command) + ")\nC().run()", owning)
              for command, owning in ((writer, True), ("echo safe", False))],
            *[("import os\nfrom dataclasses import dataclass as record\ndef identity(cls): return cls\n@identity\n@record\nclass C:\n    command: str\n    def run(self): os.system(self.command)\nC(" + repr(command) + ").run()", owning)
              for command, owning in ((writer, True), ("echo safe", False))],
            *[("import os\ndef decorate(fn):\n    def wrapped(): os.system(" + repr(writer) + ")\n    return wrapped\ndef factory(): return decorate\n@factory()\ndef command(): pass\n" + consumer, owning)
              for consumer, owning in (("print(command)", False), ("command()", True))],
            ("import os\ndef decorate(fn):\n    def wrapped(): return " + repr(writer) + "\n    return wrapped\ndef safe(fn): return lambda: 'echo safe'\n@safe\n@decorate\ndef command(): pass\nos.system(command())", False),
            *[(program.replace(writer,replacement), owning)
              for program in (
                  "import os\ncmd='echo safe'\ng=(cmd for _ in [1,2])\ndef consume():\n    def inner(): yield from g\n    yield from inner()\nfirst=consume(); print(next(first))\ncmd=" + repr(writer) + '\nos.system(next(consume()))',
                  "import os\ncmd='echo safe'\ng=(cmd for _ in [1,2])\ndef consume(): yield from (lambda: g)()\nfirst=consume(); print(next(first))\ncmd=" + repr(writer) + '\nos.system(next(consume()))',
                  "import os\ncmd='echo safe'\ng=(cmd for _ in [1,2])\ndef consume():\n    def inner(x=g): yield from x\n    yield from inner()\nfirst=consume(); print(next(first))\ncmd=" + repr(writer) + '\nos.system(next(consume()))',
                  "import os\ncmd='echo safe'\ndef run():\n    def inner(): return cmd\n    return inner()\nprint(run())\ncmd=" + repr(writer) + '\nos.system(run())',
                  "import os\ncmd='echo safe'\ndef run():\n    cmd='echo local'\n    def inner():\n        global cmd\n        return cmd\n    return inner()\nprint(run())\ncmd=" + repr(writer) + '\nos.system(run())',
                  "import os\ncmd='echo safe'\nclass Base:\n    g=(cmd for _ in [1,2])\nclass C(Base):\n    @classmethod\n    def consume(cls): yield from cls.g\nfirst=C.consume(); print(next(first))\ncmd=" + repr(writer) + '\nos.system(next(C.consume()))',
                  "import os\ncmd='echo safe'\nclass Base:\n    g=(cmd for _ in [1,2])\nclass C(Base):\n    def consume(self): yield from self.g\nobj=C()\nfirst=obj.consume(); print(next(first))\ncmd=" + repr(writer) + '\nos.system(next(obj.consume()))',
                  "import os\nclass Base: cmd='echo safe'\nclass C(Base):\n    @classmethod\n    def run(cls): return cls.cmd\nprint(C.run())\nBase.cmd=" + repr(writer) + '\nos.system(C.run())',
                  "import os\ncmd='echo safe'\ndef run():\n    class Local:\n        @staticmethod\n        def read(): return cmd\n    return Local.read()\nprint(run())\ncmd=" + repr(writer) + '\nos.system(run())',
                  "import os\ncmd='echo safe'\ndef decorate(fn):\n    def read(): return cmd\n    return read\ndef run():\n    @decorate\n    def inner(): return 'echo unused'\n    return inner()\nprint(run())\ncmd=" + repr(writer) + '\nos.system(run())',
              )
              for replacement, owning in ((writer, True), ("echo safe", False))],
            *[("import os,subprocess,shlex\nargv=" + ("['echo','dry'] if dry else " if conditional else "") + repr(["uv", "run", "--with", "requests", "/catalog/gh-plan.py", "--repo", "client/repo", verb, "12"]) + "\n" + consumer,
               verb == "claim")
              for conditional in (False, True) for verb in ("claim", "show")
              for consumer in ("os.system(' '.join(map(shlex.quote,argv)))", "subprocess.run(list(map(str,argv)))", "subprocess.run(tuple(map(str,argv)))")],
            *[("import os,shlex\ndef word(value,unused): return shlex.quote(value)\nargv=['echo','dry'] if dry else " + repr(["uv", "run", "/catalog/gh-plan.py", "--repo", "client/repo", verb, "12"]) + "\nos.system(' '.join(map(word,argv,[1,2,3,4,5,6,7])))", verb == "claim")
              for verb in ("claim", "show")],
            *[("import os\ndef word(value,unused):\n    os.system(" + repr(writer) + ")\n    return value\nos.system(' '.join(map(word," + repr(items) + ",[1])))", bool(items))
              for items in ([], ["echo safe"])],
            *[("import os\ndef passthru(xs):\n    for x in xs: yield x\nfor cmd in " + "passthru(" * depth + repr([command]) + ")" * depth + ": os.system(cmd)", owning)
              for depth in (2, 4) for command, owning in ((writer, True), ("echo safe", False))],
            ("import os\ncmd='echo safe'\ng=(cmd for _ in [1,2])\ndef consume(xs):\n    for c in xs: yield c\niterator=consume(g)\nprint(next(iterator))\ncmd=" + repr(writer) + "\nos.system(next(iterator))", True),
            ("import os\ncmd='echo safe'\ng=(cmd for _ in [1,2])\ndef consume(): yield from g\nprint(next(consume()))\ncmd=" + repr(writer) + "\nos.system(next(consume()))", True),
            ("import os\ncmd='echo safe'\nholder=[(cmd for _ in [1,2])]\ndef consume(): yield from holder[0]\nprint(next(consume()))\ncmd=" + repr(writer) + "\nos.system(next(consume()))", True),
            ("import os\ncmd='echo safe'\nclass C:\n    def consume(self): yield from self.g\nobj=C()\nobj.g=(cmd for _ in [1,2])\nprint(next(obj.consume()))\ncmd=" + repr(writer) + "\nos.system(next(obj.consume()))", True),
            ("import os\ncmd='echo safe'\nclass C:\n    g=(cmd for _ in [1,2])\n    @classmethod\n    def consume(cls): yield from cls.g\nprint(next(C.consume()))\ncmd=" + repr(writer) + "\nos.system(next(C.consume()))", True),
            ("import os\ndef recursive(xs):\n    yield from recursive((x for x in xs))\nfor c in recursive(['echo safe']): os.system(c)", False),
            ("import os\ndef commands():\n    yield 'echo safe'\n    yield " + repr(writer) + "\nlist(commands())\nos.system(*commands())", False),
            ("import os\ndef commands():\n    yield " + repr(writer) + "\n    yield 'echo safe'\nlist(commands())\nos.system(*commands())", True),
            ("import os\nfrom supplied_module import fill\nq=[]\nfill(q)\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", True),
            ("import os\nfrom supplied_module import fill\nq=[]\nfill((q,))\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", True),
            ("import os\nfrom supplied_module import fill\nq=[]\nfill({'pack': (q,)})\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", True),
            ("import os\nq=[]\nprint((q,))\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ("import os\nfrom supplied_module import fill\nq=[]\nfill(q)\nq.clear()\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ("import os\nfrom supplied_module import fill\nq={}\nfill(q)\nfor key in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", True),
            ("import os\nfrom supplied_module import fill\nq={}\nfill(q)\nq.clear()\nfor key in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ("import os\nfrom supplied_module import fill\nimport functools\nq=[]\nfunctools.partial(fill,q)()\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", True),
            ('import os\nimport operator\nq=[]\noperator.methodcaller("append",\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')(q)\nfor c in q: os.system(c)', True),
            ('import os\nimport operator\nq=[]\nadd=operator.methodcaller("append",\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\nadd(q)\nq.clear()\nfor c in q: os.system(c)', False),
            ('import os\nimport operator\nq=[]\noperator.methodcaller("copy")(q)\nfor c in q: os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')', False),
            ("import os\nimport heapq,functools\nq=[]\nfunctools.partial(heapq.heappush,q,'uv run /catalog/gh-plan.py --repo client/repo claim 12')()\nfor c in q: os.system(c)", True),
            ("import os\nimport heapq,functools\nq=[]\nfunctools.partial(heapq.heappush,q)('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nfor c in q: os.system(c)", True),
            ("import os\nimport inspect,functools\nq=[]\ntry: functools.partial(inspect.signature,q)()\nexcept TypeError: pass\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ("import os\nimport pprint,functools\nq=[]\nfunctools.partial(pprint.pprint,q)()\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ("import os\nq=[]\nprint(q)\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ("import os\nq=[]\nlist(q)\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ("import os\nimport json\nq=[]\njson.dumps(q)\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ('import os\nfrom supplied_module import fill\nq=["uv","run","/catalog/gh-plan.py","--repo","client/repo","show","12"]\nfill(q)\nos.system(" ".join(q))', False),
            ("import os\ndef leave(q): pass\nq=[]\nleave(q)\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ('import os\ndef create():\n    cmds=[]\n    def get(): return cmds\n    return get\na=create()\na().append("uv run /catalog/gh-plan.py --repo client/repo claim 12")\nb=create()\nfor c in b(): os.system(c)', False),
            ["import os,subprocess\nxs=['echo safe']\nxs=(x for x in xs)\nout=[]\nout.extend(xs)\nfor c in out: os.system(c)", False],
            ["import os,subprocess\nxs=['echo safe']\nxs=map(str,xs)\nout=[]\nout.extend(xs)\nfor c in out: os.system(c)", False],
            ["import os,subprocess\nxs=[]\nxs.append(xs)\nxs.append('echo safe')\nos.system(render(xs))", False],
            ['import os,subprocess\nd={}\nd["self"]=d\nd["cmd"]=\'echo safe\'\nos.system(render(d))', False],
            ["import os,subprocess\nxs=[['echo safe']]\ng=(y for x in xs for y in x)\nxs.append(g)\nfor c in g: os.system(c)", False],
            ["import os,subprocess\nxs=['uv run /catalog/gh-plan.py --repo client/repo claim 12']\nxs=(x for x in xs)\nout=[]\nout.extend(xs)\nfor c in out: os.system(c)", True],
            ["import os,subprocess\nxs=['uv run /catalog/gh-plan.py --repo client/repo claim 12']\nxs=map(str,xs)\nout=[]\nout.extend(xs)\nfor c in out: os.system(c)", True],
            ["import os,subprocess\nxs=[]\nxs.append(xs)\nxs.append('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nos.system(render(xs))", True],
            ['import os,subprocess\nd={}\nd["self"]=d\nd["cmd"]=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nos.system(render(d))', True],
            ["import os,subprocess\nxs=[['uv run /catalog/gh-plan.py --repo client/repo claim 12']]\ng=(y for x in xs for y in x)\nxs.append(g)\nfor c in g: os.system(c)", True],
            ["import os,subprocess\nxs=['echo safe']\ng=(x for x in xs)\nxs=['uv run /catalog/gh-plan.py --repo client/repo claim 12']\nout=[]\nout.extend(g)\nfor c in out: os.system(c)", False],
            ["import os,subprocess\nxs=['uv run /catalog/gh-plan.py --repo client/repo claim 12']\ng=(x for x in xs)\nxs=['echo safe']\nout=[]\nout.extend(g)\nfor c in out: os.system(c)", True],
            ['import os,subprocess\ncmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\ng=(cmd for _ in [1])\ncmd="echo safe"\nout=[]\nout.extend(g)\nfor c in out: os.system(c)', False],
            ['import os,subprocess\nxs=["echo safe"]\ng=(x for x in xs)\nxs.append(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\nout=[]\nout.extend(g)\nfor c in out: os.system(c)', True],
            ["import os,subprocess\ndef items():\n    os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\n    return [1]\n(x for x in items())", True],
            ["import os,subprocess\ndef items():\n    os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\n    return [1]\ng=(x for i in [1] for x in items())", False],
            ["import os,subprocess\ndef items():\n    os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\n    return [1]\ng=(x for i in [1] for x in items())\nlist(g)", True],
            ["import os,subprocess\ndef callback(c): os.system(c)\ng=map(callback,['uv run /catalog/gh-plan.py --repo client/repo claim 12'])", False],
            ["import os,subprocess\ndef callback(c): os.system(c)\ng=map(callback,['uv run /catalog/gh-plan.py --repo client/repo claim 12'])\ncallback=str\nlist(g)", True],
            ["import os,subprocess\ndef callback(c): os.system(c)\ng=map(str,['uv run /catalog/gh-plan.py --repo client/repo claim 12'])\nstr=callback\nlist(g)", False],
            ['import os,subprocess\nd={key:\'uv run /catalog/gh-plan.py --repo client/repo claim 12\' for key in ["x"]}\nos.system(d["x"])', True],
            ["import os,subprocess\nd={key:'uv run /catalog/gh-plan.py --repo client/repo claim 12' for key in []}\nfor key in d: os.system(d[key])", False],
            ['import os,subprocess\nd={key:\'uv run /catalog/gh-plan.py --repo client/repo claim 12\' for key in ["x"]}\nprint(d)', False],
            ('import os,subprocess\ncommands={"c":\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'}\nfor c in commands.values(): os.system(c)', True),
            ('import os,subprocess\ncommands={"c":\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'}\nfor name,c in commands.items(): os.system(c)', True),
            ('import os,subprocess\ncommands={"c":\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'}\nfor c in transform(commands): os.system(c)', True),
            ('import os,subprocess\nargv=[\'uv\', \'run\', \'/catalog/gh-plan.py\', \'--repo\', \'client/repo\']\nif v: argv.append("--trace")\nargv += [\'claim\', \'12\']\nsubprocess.run(list(argv))', True),
            ('import os,subprocess\nargv=[\'uv\', \'run\', \'/catalog/gh-plan.py\', \'--repo\', \'client/repo\']\nif v: argv.append("--trace")\nargv += [\'claim\', \'12\']\nsubprocess.run(tuple(argv))', True),
            ('import os,subprocess\nimport shlex\nargv=["echo","dry"] if dry else [\'uv\', \'run\', \'/catalog/gh-plan.py\', \'--repo\', \'client/repo\', \'claim\', \'12\']\nos.system(" ".join(shlex.quote(w) for w in argv))', True),
            ('import os,subprocess\nimport shlex\nargv=["uv","run","--with","requests","/catalog/gh-plan.py","--repo",repo,\'claim\',"12"]\nos.system(" ".join(shlex.quote(w) for w in argv))', True),
            ('import os,subprocess\ncommands={"c":\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'}\nos.system(commands.pop(action))', True),
            ('import os,subprocess\ncommands=[\'uv run /catalog/gh-plan.py --repo client/repo claim 12\',"echo safe"]\nos.system(commands.pop(index))', True),
            ("import os,subprocess\ncommands={}\nos.system(commands.setdefault(action,'uv run /catalog/gh-plan.py --repo client/repo claim 12'))", True),
            ('import os,subprocess\ncommands={"c":\'uv run /catalog/gh-plan.py --repo client/repo show 12\'}\nfor c in commands.values(): os.system(c)', False),
            ('import os,subprocess\ncommands={"c":\'uv run /catalog/gh-plan.py --repo client/repo show 12\'}\nfor name,c in commands.items(): os.system(c)', False),
            ('import os,subprocess\ncommands={"c":\'uv run /catalog/gh-plan.py --repo client/repo show 12\'}\nfor c in transform(commands): os.system(c)', False),
            ('import os,subprocess\nargv=[\'uv\', \'run\', \'/catalog/gh-plan.py\', \'--repo\', \'client/repo\']\nif v: argv.append("--trace")\nargv += [\'show\', \'12\']\nsubprocess.run(list(argv))', False),
            ('import os,subprocess\nargv=[\'uv\', \'run\', \'/catalog/gh-plan.py\', \'--repo\', \'client/repo\']\nif v: argv.append("--trace")\nargv += [\'show\', \'12\']\nsubprocess.run(tuple(argv))', False),
            ('import os,subprocess\nimport shlex\nargv=["echo","dry"] if dry else [\'uv\', \'run\', \'/catalog/gh-plan.py\', \'--repo\', \'client/repo\', \'show\', \'12\']\nos.system(" ".join(shlex.quote(w) for w in argv))', False),
            ('import os,subprocess\nimport shlex\nargv=["uv","run","--with","requests","/catalog/gh-plan.py","--repo",repo,\'show\',"12"]\nos.system(" ".join(shlex.quote(w) for w in argv))', False),
            ('import os,subprocess\ncommands={"c":\'uv run /catalog/gh-plan.py --repo client/repo show 12\'}\nos.system(commands.pop(action))', False),
            ('import os,subprocess\ncommands=[\'uv run /catalog/gh-plan.py --repo client/repo show 12\',"echo safe"]\nos.system(commands.pop(index))', False),
            ("import os,subprocess\ncommands={}\nos.system(commands.setdefault(action,'uv run /catalog/gh-plan.py --repo client/repo show 12'))", False),
            ("import os,subprocess\nimport heapq\nq=[]\nheapq.heappush(q,'uv run /catalog/gh-plan.py --repo client/repo claim 12')\nfor c in q: os.system(c)", True),
            ("import os,subprocess\nimport bisect\nq=[]\nbisect.insort(q,'uv run /catalog/gh-plan.py --repo client/repo claim 12')\nfor c in q: os.system(c)", True),
            ("import os,subprocess\nimport inspect\nq=[]\ntry: inspect.signature(q)\nexcept TypeError: pass\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ("import os,subprocess\nimport pprint\nq=[]\npprint.pprint(q)\nfor c in q: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ("import os,subprocess\nclass Claim:\n    def __enter__(self): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12'); return self\n    def __exit__(self,*exc): pass\nwith Claim(): pass", True),
            ("import os,subprocess\nclass Claim:\n    def __enter__(self): return self\n    def __exit__(self,*exc): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nwith Claim(): pass", True),
            ("import os,subprocess\nclass Claim:\n    def __enter__(self): return 'uv run /catalog/gh-plan.py --repo client/repo claim 12'\n    def __exit__(self,*exc): pass\nwith Claim() as c: os.system(c)", True),
            ("import os,subprocess\nclass Claim:\n    def __enter__(self): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12'); return self\n    def __exit__(self,*exc): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nClaim()", False),
            ("import os,subprocess\nimport contextlib\nwith contextlib.ExitStack() as stack: stack.callback(os.system,'uv run /catalog/gh-plan.py --repo client/repo claim 12')", True),
            ("import os,subprocess\nimport contextlib\nstack=contextlib.ExitStack()\nstack.callback(os.system,'uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ("import os,subprocess\nimport contextlib\nstack=contextlib.ExitStack()\nstack.callback(os.system,'uv run /catalog/gh-plan.py --repo client/repo claim 12')\nstack.close()", True),
            ("import os,subprocess\nimport contextlib\nwith contextlib.ExitStack() as stack: stack.push(lambda *exc: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12'))", True),
            ("import os,subprocess\nimport contextlib\nclass Claim:\n    def __enter__(self): return self\n    def __exit__(self,*exc): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nwith contextlib.ExitStack() as stack: stack.enter_context(Claim())", True),
            ('import os,subprocess\ndef commands():\n    if dry: yield "echo safe"\n    else: yield \'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nos.system(*commands())', True),
            ('import os,subprocess\ndef commands():\n    if dry: yield "echo safe"\n    else: yield \'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\ndef run(c): os.system(c)\nrun(*commands())', True),
            ('import os,subprocess\ndef commands():\n    if dry: yield "echo safe"\n    else: yield "echo done"\n    yield \'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nos.system(*commands())', False),
            ("import os,subprocess\ndef jobs():\n    os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\n    yield 1\nfrozenset(jobs())", True),
            ("import os,subprocess\ndef jobs():\n    os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\n    yield 1\nunused=jobs()", False),
            ("import os\nd={}\nd[key]='uv run /catalog/gh-plan.py --repo client/repo claim 12'\nos.system(d[key])", True),
            ("import os\ncmds=set()\ncmds.add('uv run /catalog/gh-plan.py --repo client/repo claim 12')\ncmds.add('uv run /catalog/gh-plan.py --repo client/repo claim 12')\ncmds.discard('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nfor c in cmds: os.system(c)", False),
            ("import os\ncmds={'uv run /catalog/gh-plan.py --repo client/repo claim 12','uv run /catalog/gh-plan.py --repo client/repo claim 12'}\ncmds.remove('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nfor c in cmds: os.system(c)", False),
            ("import os,subprocess\ncmds=[]\nif dry: cmds.append('uv run /catalog/gh-plan.py --repo client/repo claim 12')\ncmds.clear()\nfor c in cmds: os.system(c)", False),
            ('import os,subprocess\ncmds=[]\nif dry: cmds.append(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\nelse: cmds.append("echo safe")\ncmds.clear()\nfor c in cmds: os.system(c)', False),
            ("import os,subprocess\ncmds=['uv run /catalog/gh-plan.py --repo client/repo claim 12']\nother=cmds\nother.clear()\nfor c in cmds: os.system(c)", False),
            ("import os,subprocess\ndef create(): return []\na=create()\na.append('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nb=create()\nfor c in b: os.system(c)", False),
            ('import os,subprocess\ndef create(): return {}\na=create()\na["cmd"]=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nb=create()\nfor c in b.values(): os.system(c)', False),
            ("import os,subprocess\ncmds=[]\nother=cmds\ncmds += ['uv run /catalog/gh-plan.py --repo client/repo claim 12']\nfor c in other: os.system(c)", True),
            ("import os,subprocess\ncmds=[] + ['uv run /catalog/gh-plan.py --repo client/repo claim 12']\ncmds.clear()\nfor c in cmds: os.system(c)", False),
            ("import os,subprocess\ncmds=['uv run /catalog/gh-plan.py --repo client/repo claim 12','uv run /catalog/gh-plan.py --repo client/repo claim 12']\ncmds.remove('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nfor c in cmds: os.system(c)", True),
            ("import os,subprocess\ncmds=[]\ncmds.insert(0,'uv run /catalog/gh-plan.py --repo client/repo claim 12')\nfor c in cmds: os.system(c)", True),
            ("import os,subprocess\ncmds=set()\ncmds.add('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nfor c in cmds: os.system(c)", True),
            ('import os,subprocess\nd={}\nd.update(cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\nos.system(d["cmd"])', True),
            ('import os,subprocess\nd={}\nos.system(d.setdefault("cmd",\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'))', True),
            ('import os,subprocess\ncmds=[]\ncmds.append("uv")\ncmds.extend(["run","/catalog/gh-plan.py","--repo","client/repo","claim","12"])\nos.system(" ".join(cmds))', True),
            ('import os,subprocess\ndef commands():\n    yield "echo skip"\n    yield \'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nos.system(*commands())', False),
            ("import os,subprocess\nimport inspect\ndef jobs():\n    os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\n    yield None\ninspect.signature(jobs())", False),
            ("import os,subprocess\ncmds=[]\ncmds.append('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nfor c in cmds: os.system(c)", True),
            ('import os,subprocess\nargv=["uv","run","/catalog/gh-plan.py"]\nargv.extend(["--repo","client/repo","claim","12"])\nsubprocess.run(argv)', True),
            ("import os,subprocess\ncmds=[]\nother=cmds\nother.append('uv run /catalog/gh-plan.py --repo client/repo claim 12')\n[os.system(c) for c in cmds]", True),
            ("import os,subprocess\ncmds=[]\ndef configure(items=cmds): items.append('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nconfigure()\nfor c in cmds: os.system(c)", True),
            ("import os,subprocess\ncmds=[]\nif dry: cmds.append('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nfor c in cmds: os.system(c)", True),
            ("import os,subprocess\ncmds=['uv run /catalog/gh-plan.py --repo client/repo claim 12']\nif dry: cmds.clear()\nelse: cmds.clear()\nfor c in cmds: os.system(c)", False),
            ('import os,subprocess\nd={}\nother=d\nother["cmd"]=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nos.system(d["cmd"])', True),
            ('import os,subprocess\nd={"cmd":"echo done"}\nif dry: d["cmd"]=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nos.system(d["cmd"])', True),
            ('import os,subprocess\nd={"cmd":\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'}\nif dry: d["cmd"]="echo done"\nelse: d["cmd"]="echo skip"\nos.system(d["cmd"])', False),
            ('import os,subprocess\ncmds=["echo done"]\ncmds[0]=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nos.system(cmds[0])', True),
            ("import os,subprocess\ncmds=[]\ndef configure(items): items.append('uv run /catalog/gh-plan.py --repo client/repo claim 12')\nconfigure(cmds)\ncmds.clear()\nconfigure(cmds)\nfor c in cmds: os.system(c)", True),
            ("import os,subprocess\ncmds=[]\nfor c in cmds: os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ('import os,subprocess\ndef run(repo): os.system(" ".join(["uv","run","/catalog/gh-plan.py","--repo",repo,"claim","12"]))\nrun(x)', True),
            ('import os,subprocess\nimport shlex\nargv=["uv","run","/catalog/gh-plan.py","--repo",repo,"claim","12"]\nos.system(" ".join(shlex.quote(w) for w in argv))', True),
            ('import os,subprocess\ndef run(repo): os.system(" ".join(["uv","run","/catalog/gh-plan.py","--repo",repo,"show","12"]))\nrun(x)', False),
            ('import os,subprocess\nimport shlex\nargv=["uv","run","/catalog/gh-plan.py","--repo",repo,"show","12"]\nos.system(" ".join(shlex.quote(w) for w in argv))', False),
            ('import os,subprocess\nTEMPLATE="uv run /catalog/gh-plan.py --repo {} claim {}"\nos.system(TEMPLATE.format(*map(str,[repo,n])))', True),
            ('import os,subprocess\nTEMPLATE="uv run /catalog/gh-plan.py --repo {} show {}"\nos.system(TEMPLATE.format(*map(str,[repo,n])))', False),
            ("import os,subprocess\nimport contextlib\n@contextlib.contextmanager\ndef claimed():\n    os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\n    yield\nwith contextlib.ExitStack() as stack: stack.enter_context(claimed())", True),
            ("import os,subprocess\nimport contextlib\n@contextlib.contextmanager\ndef command(): yield 'uv run /catalog/gh-plan.py --repo client/repo claim 12'\nwith command() as c: os.system(c)", True),
            ("import os,subprocess\nimport contextlib\n@contextlib.contextmanager\ndef command(): yield 'uv run /catalog/gh-plan.py --repo client/repo claim 12'\nwith contextlib.ExitStack() as stack:\n    c=stack.enter_context(command())\n    os.system(c)", True),
            ("import os,subprocess\nimport collections\ndef jobs():\n    os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\n    yield None\ncollections.deque(jobs(),maxlen=0)", True),
            ('import os,subprocess\ndef commands(): yield "echo skip" if dry else \'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nos.system(*commands())', True),
            ('import os,subprocess\ndef commands(): yield "echo skip" if dry else \'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\ndef run(c): os.system(c)\nrun(*commands())', True),
            ("import os; os.system('uv run /catalog/' + 'gh-' + 'plan.py --repo client/repo ' + 'claim 12')", True),
            ("import subprocess; subprocess.run(['uv','run'] + ['/catalog/gh-plan.py','--repo','client/repo','claim','12'])", True),
            ("import subprocess; n=12; subprocess.run(['uv','run','/catalog/gh-plan.py','--repo','client/repo','claim',str(n)])", True),
            ('import os; n=12; os.system(f"uv run /catalog/gh-plan.py --repo client/repo claim {n}")', True),
            ("import subprocess; n=12; subprocess.run(args=['uv','run','/catalog/gh-plan.py','--repo','client/repo','claim',str(n)])", True),
            ("import os, subprocess; action=os.environ['ACTION']; subprocess.run(['uv','run','/catalog/gh-plan.py','--repo','client/repo',action,'12'])", True),
            ("import os, subprocess; helper=os.environ['HELPER']; subprocess.run([helper,'--repo','client/repo','claim','12'])", True),
            ('import subprocess; n=12; subprocess.run(["bash","-c",f"uv run /catalog/gh-plan.py --repo client/repo claim {n}"])', True),
            ("import os\ncmd=" + repr(writer) + "\nif os.environ.get('DRY'):\n    cmd='echo skip'\nos.system(cmd)", True),
            ("import os\ncmd=" + repr(writer) + "\nif os.environ.get('DRY'):\n    cmd='echo skip'\nelse:\n    cmd='echo done'\nos.system(cmd)", False),
            ("import os; cmd=" + repr(writer) + "; saved=cmd; cmd='echo done'; os.system(saved)", True),
            ("import os; cmd=" + repr(writer) + "; cmd='echo done'; os.system(cmd)", False),
            ("import os; cmd='echo done'; os.system(cmd); cmd=" + repr(writer), False),
            ("fixture='uv run /catalog/' + 'gh-plan.py --repo client/repo claim 12'; print(fixture)", False),
            ("import subprocess; fixture=" + repr(writer) + "; subprocess.run(unknown); print(fixture)", False),
            ("import subprocess; n=12; subprocess.run(['echo'," + repr(writer) + ",str(n)])", False),
            ("import subprocess; n=12; subprocess.run(['rg'," + repr(writer) + ",str(n)])", False),
            ("import subprocess\ndef sh(c): subprocess.run(c, shell=True, check=True)\nsh(" + repr(writer) + ")", True),
            ("import subprocess\ndef sh(c): subprocess.run(c, shell=True, check=True)\nsh(c=" + repr(writer) + ")", True),
            ("from subprocess import run; run(['uv','run','/catalog/gh-plan.py','--repo','client/repo','claim','12'])", True),
            ("import subprocess as sp; sp.run(['uv','run','/catalog/gh-plan.py','--repo','client/repo','claim','12'])", True),
            ("from subprocess import run as launch; launch(['uv','run','/catalog/gh-plan.py','--repo','client/repo','claim','12'])", True),
            ("import os\nfor c in [" + repr(writer) + "]:\n    os.system(c)", True),
            ("import os; cmd, _ = " + repr(writer) + ", 0; os.system(cmd)", True),
            ("import os; [cmd, _] = [" + repr(writer) + ", 0]; os.system(cmd)", True),
            ("import os\nfor cmd, _ in [('echo done', " + repr(writer) + "), ('echo skip', 'data')]:\n    os.system(cmd)", False),
            ("import os; commands=[" + repr(writer) + "]\nfor cmd in commands:\n    os.system(cmd)", True),
            ("import os; jobs=[('echo done', " + repr(writer) + ")]\nfor cmd, _ in jobs:\n    os.system(cmd)", False),
            ("import os\nfor cmd in []:\n    os.system(" + repr(writer) + ")", False),
            ("import os, subprocess\nargv=['uv','run','/catalog/gh-plan.py','--repo','client/repo','claim','12']\nif os.environ.get('DRY'):\n    argv=['echo','skip']\nsubprocess.run(argv)", True),
            ("import os, subprocess\nargv=['echo'," + repr(writer) + "]\nif os.environ.get('DRY'):\n    argv=['rg'," + repr(writer) + "]\nsubprocess.run(argv)", False),
            ("import os\nfor c in ['git fetch', " + repr(writer) + "]:\n    os.system(f'cd /wt/repo && {c}')", True),
            ("import subprocess\nfor c in ['git fetch', " + repr(writer) + "]:\n    subprocess.run(['bash', '-lc', c])", True),
            ("import subprocess, shlex; text=" + repr(writer) + "; subprocess.run('gh issue comment 5 --body ' + shlex.quote(text), shell=True)", False),
            ("import subprocess, shlex\nfor text in ['git fetch', " + repr(writer) + "]:\n    subprocess.run('gh issue comment 5 --body ' + shlex.quote(text), shell=True)", False),
            ("import subprocess\ndef sh(c): subprocess.run(c, shell=True)\ndef main(): sh(" + repr(writer) + ")\nmain()", True),
            ("import subprocess\nclass Job:\n    def run(self): subprocess.run(" + repr(writer) + ", shell=True)\nJob().run()", True),
            ("import subprocess\nclass Job:\n    def run(self, cmd): subprocess.run(cmd, shell=True)\njob = Job()\njob.run(" + repr(writer) + ")", True),
            ("import subprocess\nclass Job:\n    def run(self, cmd): subprocess.run(cmd, shell=True)\njob = Job()\njob.run('echo done')", False),
            ("import subprocess\nclass Job:\n    def run(self): subprocess.run(" + repr(writer) + ", shell=True)", False),
            ("import subprocess, threading\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)\nthreading.Thread(target=claim).start()", True),
            ("import subprocess\nfrom concurrent.futures import ThreadPoolExecutor\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)\nThreadPoolExecutor().submit(claim)", True),
            ("import subprocess\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)\ndef push(): print('done')\nfor step in (claim, push): step()", True),
            ("import subprocess\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)\nprint(claim)", False),
            ("try:\n    import subprocess\nexcept ImportError:\n    raise SystemExit(1)\nsubprocess.run(" + repr(writer) + ", shell=True)", True),
            ("if condition:\n    import subprocess as sp\nelse:\n    import subprocess as sp\nsp.run(" + repr(writer) + ", shell=True)", True),
            ("if condition:\n    from subprocess import run as launch\nelse:\n    launch=print\nlaunch(" + repr(writer) + ", shell=True)", True),
            ("import os\nfor action in " + repr([f'safe{i}' for i in range(12)] + ['claim']) + ":\n    os.system(f'uv run /catalog/gh-plan.py --repo client/repo {action} 12')", True),
            ("import subprocess\nfor text in " + repr([f'safe{i}' for i in range(12)] + [writer]) + ":\n    subprocess.run(['echo', text])", False),
            ("import os\nfor command in " + repr([f'echo safe{i}' for i in range(12)] + [writer]) + ":\n    os.system(command)", True),
            ("import subprocess\nfor command in " + repr([f'echo safe{i}' for i in range(12)] + [writer]) + ":\n    subprocess.run(['bash', '-c', command])", True),
            ("import inspect, subprocess\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)\ninspect.signature(claim)", False),
            ("import subprocess\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)\ncallbacks=dict(callback=claim)", False),
            ("import subprocess, threading\ndef sh(command): subprocess.run(command, shell=True)\nthreading.Thread(target=sh, args=(" + repr(writer) + ",)).start()", True),
            ("import subprocess\nfrom concurrent.futures import ThreadPoolExecutor\ndef sh(command): subprocess.run(command, shell=True)\nThreadPoolExecutor().submit(sh, " + repr(writer) + ")", True),
            ("import subprocess\nclass Job:\n    @staticmethod\n    def run(command): subprocess.run(command, shell=True)\nJob().run(" + repr(writer) + ")", True),
            ("import subprocess\n" + "\n".join(f'def step{i}(): print({i})' for i in range(12)) + "\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)\nfor step in (" + ','.join(f'step{i}' for i in range(12)) + ",claim): step()", True),
            ("import subprocess; text=" + repr(writer) + "; subprocess.run(transform(text), shell=True)", True),
            ("import subprocess; text=" + repr(writer) + "; subprocess.run('gh issue comment 5 --body ' + transform(text), shell=True)", False),
            ("import subprocess; text=" + repr(writer) + "; subprocess.run(['echo', transform(text)])", False),
            ("import subprocess; text=" + repr(writer) + "; subprocess.run(['python3', 'notify.py', '--message', transform(text)])", False),
            ("import subprocess\n[subprocess.run(c, shell=True) for c in ['git fetch', " + repr(writer) + "]]", True),
            ("import subprocess\n{subprocess.run(c, shell=True) for c in ['git fetch', " + repr(writer) + "]}", True),
            ("import subprocess\n{c:subprocess.run(c, shell=True) for c in ['git fetch', " + repr(writer) + "]}", True),
            ("import subprocess\nunused=(subprocess.run(c, shell=True) for c in [" + repr(writer) + "])", False),
            ("import subprocess\nlist(subprocess.run(c, shell=True) for c in [" + repr(writer) + "])", True),
            ("import subprocess\nfrom concurrent.futures import ThreadPoolExecutor\ndef sh(c): subprocess.run(c, shell=True)\ncmds=['git fetch'," + repr(writer) + "]\nwith ThreadPoolExecutor() as ex:\n    futures=[ex.submit(sh,c) for c in cmds]", True),
            ("import subprocess\nfrom concurrent.futures import ThreadPoolExecutor\ndef sh(c): subprocess.run(c, shell=True)\nwith ThreadPoolExecutor() as ex:\n    list(ex.map(sh,[" + repr(writer) + "]))", True),
            ("import subprocess, threading\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)\nthreading.Timer(0,claim).start()", True),
            ("import subprocess, functools\ndef sh(c): subprocess.run(c, shell=True)\njob=functools.partial(sh," + repr(writer) + ")\njob()", True),
            ("import subprocess, functools\ndef sh(c): subprocess.run(c, shell=True)\njob=functools.partial(sh," + repr(writer) + ")", False),
            ("import subprocess, atexit\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)\natexit.register(claim)", True),
            ("import subprocess, typer\napp=typer.Typer()\n@app.command()\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)\napp()", True),
            ("import subprocess, typer\napp=typer.Typer()\n@app.command()\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)", False),
            ("import subprocess, pytest\n@pytest.fixture\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)", False),
            ("import subprocess, click\n@click.command()\ndef claim(): subprocess.run(" + repr(writer) + ", shell=True)", False),
            ("import subprocess\nclass Job:\n    def __init__(self,cmd): self.cmd=cmd\n    def run(self): subprocess.run(self.cmd,shell=True)\njob=Job(" + repr(writer) + ")\njob.run()", True),
            ("import subprocess\nclass Job:\n    def __init__(self,cmd): self.cmd=cmd\n    def run(self): subprocess.run(self.cmd,shell=True)\na=Job(" + repr(writer) + ")\nb=Job('echo done')\na.run()", True),
            ("import subprocess\nclass Job:\n    def __init__(self,cmd): self.cmd=cmd\n    def run(self): subprocess.run(self.cmd,shell=True)\na=Job(" + repr(writer) + ")\nb=Job('echo done')\nb.run()", False),
            ("import subprocess\nCOMMAND='echo done'\ndef run(): subprocess.run(COMMAND,shell=True)\ndef main():\n    COMMAND=" + repr(writer) + "\n    print(COMMAND)\n    run()\nmain()", False),
            ("import subprocess\nCOMMAND='echo done'\ndef run(): subprocess.run(COMMAND,shell=True)\nCOMMAND=" + repr(writer) + "\nrun()", True),
            ("import subprocess\nCOMMAND=" + repr(writer) + "\ndef run(): subprocess.run(COMMAND,shell=True)\nCOMMAND='echo done'\nrun()", False),
            ("import subprocess\ndef main():\n    command=" + repr(writer) + "\n    def run(): subprocess.run(command,shell=True)\n    run()\nmain()", True),
            ("import subprocess\ncommands=[['echo', 'safe0'], ['echo', 'safe1'], ['echo', 'safe2'], ['echo', 'safe3'], ['echo', 'safe4'], ['echo', 'safe5'], ['echo', 'safe6'], ['echo', 'safe7'], ['echo', 'safe8'], ['echo', 'safe9']]\nfor cmd in commands:\n    subprocess.run(cmd)", False),
            ("import subprocess\nfor shell in ('/bin/bash','/bin/zsh'):\n    for command in ('echo -- marker','echo first','echo -E second'):\n        subprocess.run([shell,'-c',command])", False),
            ("import subprocess\nfor helper in ['/catalog/read0.py', '/catalog/read1.py', '/catalog/read2.py', '/catalog/read3.py', '/catalog/read4.py', '/catalog/read5.py', '/catalog/read6.py', '/catalog/read7.py', '/catalog/read8.py', '/catalog/read9.py', '/catalog/read10.py', '/catalog/read11.py', '/catalog/gh-plan.py']:\n    subprocess.run([helper,'--repo','client/repo','claim','12'])", True),
            ("import subprocess\nfor helper in ['/catalog/read0.py', '/catalog/read1.py', '/catalog/read2.py', '/catalog/read3.py', '/catalog/read4.py', '/catalog/read5.py', '/catalog/read6.py', '/catalog/read7.py', '/catalog/read8.py', '/catalog/read9.py', '/catalog/read10.py', '/catalog/read11.py', '/catalog/gh-plan.py']:\n    subprocess.run([helper,'--repo','client/repo','show','12'])", False),
            ("import os\nfor command in ['echo safe0', 'echo safe1', 'echo safe2', 'echo safe3', 'echo safe4', 'echo safe5', 'echo safe6', 'echo safe7', 'echo safe8', 'echo safe9', 'echo safe10', 'echo safe11', 'uv run /catalog/gh-plan.py --repo client/repo claim 12']:\n    os.system(command)", True),
            ("import subprocess\nfor command in " + repr([["/catalog/gh-plan.py", "--repo", "client/repo", verb, str(index)] for index in range(5) for action in receipts.helper_parser("gh-plan.py")._actions if isinstance(action, argparse._SubParsersAction) for verb in action.choices if not receipts.ownership_command(["/catalog/gh-plan.py", "--repo", "client/repo", verb, str(index)])]) + ":\n    subprocess.run(command)", False),
            ("import subprocess\nfor prefix in [['/catalog/read0.py'], ['/catalog/read1.py'], ['/catalog/read2.py'], ['/catalog/read3.py'], ['/catalog/read4.py'], ['/catalog/read5.py'], ['/catalog/read6.py'], ['/catalog/read7.py'], ['/catalog/read8.py'], ['/catalog/read9.py'], ['/catalog/read10.py'], ['/catalog/read11.py'], ['/catalog/gh-plan.py']]:\n    subprocess.run(prefix+['--repo','client/repo','claim','12'])", True),
            ("import subprocess\nfor prefix in [['/catalog/read0.py'], ['/catalog/read1.py'], ['/catalog/read2.py'], ['/catalog/read3.py'], ['/catalog/read4.py'], ['/catalog/read5.py'], ['/catalog/read6.py'], ['/catalog/read7.py'], ['/catalog/read8.py'], ['/catalog/read9.py'], ['/catalog/read10.py'], ['/catalog/read11.py'], ['/catalog/gh-plan.py']]:\n    subprocess.run(prefix+['--repo','client/repo','show','12'])", False),
            ("import subprocess\nfor text in ['safe0', 'safe1', 'safe2', 'safe3', 'safe4', 'safe5', 'safe6', 'safe7', 'safe8', 'safe9', 'safe10', 'safe11', 'uv run /catalog/gh-plan.py --repo client/repo claim 12']:\n    subprocess.run(['echo',text])", False),
            ("import os\nfor tool in ['ruff','mypy','pytest']:\n    for target in ['a','b','c']:\n        os.system(f'{tool} {target}')", False),
            ("import subprocess, threading\ndef claim(): subprocess.run(" + repr(writer) + ",shell=True)\nthreading.Timer(0,claim)", False),
            ("import subprocess, threading\ndef claim(): subprocess.run(" + repr(writer) + ",shell=True)\nthreading.Thread(target=claim)", False),
            ("import subprocess, threading\ndef claim(): subprocess.run(" + repr(writer) + ",shell=True)\njob=threading.Timer(0,claim)\njob.start()", True),
            ("import subprocess\nunused=lambda: subprocess.run(" + repr(writer) + ",shell=True)", False),
            ("import subprocess\njob=lambda: subprocess.run(" + repr(writer) + ",shell=True)\njob()", True),
            ("import subprocess\nCOMMAND='echo done'\ndef run():\n    global COMMAND\n    subprocess.run(COMMAND,shell=True)\n    COMMAND=" + repr(writer) + "\nrun()\nrun()", True),
            ("import subprocess\nCOMMAND=" + repr(writer) + "\ndef reset():\n    global COMMAND\n    COMMAND='echo done'\nreset()\nsubprocess.run(COMMAND,shell=True)", False),
            ("import subprocess\nCOMMAND=" + repr(writer) + "\ndef reset():\n    global COMMAND\n    if condition: COMMAND='echo done'\nreset()\nsubprocess.run(COMMAND,shell=True)", True),
            ("import subprocess\nCOMMAND=" + repr(writer) + "\ndef reset():\n    global COMMAND\n    if True: COMMAND='echo done'\nreset()\nsubprocess.run(COMMAND,shell=True)", False),
            ("import subprocess\ndef main():\n    command='echo done'\n    def run():\n        nonlocal command\n        subprocess.run(command,shell=True)\n        command=" + repr(writer) + "\n    run()\n    run()\nmain()", True),
            ("import subprocess\ndef main():\n    command=" + repr(writer) + "\n    def reset():\n        nonlocal command\n        command='echo done'\n    reset()\n    subprocess.run(command,shell=True)\nmain()", False),
            ("import subprocess, threading\ndef sh(c): subprocess.run(c,shell=True)\noptions={'c':" + repr(writer) + "}\nthreading.Thread(target=sh,kwargs=options).start()", True),
            ("import subprocess\nfrom concurrent.futures import ThreadPoolExecutor\ndef sh(c): subprocess.run(c,shell=True)\nwith ThreadPoolExecutor() as ex: ex.submit(sh,c=" + repr(writer) + ")", True),
            ("import subprocess,atexit\nCOMMAND='echo done'\ndef run(): subprocess.run(COMMAND,shell=True)\natexit.register(run)\nCOMMAND=" + repr(writer), True),
            ("import subprocess,atexit\ndef claim(): subprocess.run(" + repr(writer) + ",shell=True)\natexit.register(claim)\natexit.unregister(claim)", False),
            ("import subprocess\nCOMMAND='echo done'\ndef set_command():\n    global COMMAND\n    COMMAND=" + repr(writer) + "\ndef configure(): set_command()\nconfigure()\nCOMMAND='echo done'\nconfigure()\nsubprocess.run(COMMAND,shell=True)", True),
            ("import subprocess\ndef sh(*args): subprocess.run(args)\nsh('uv','run','/catalog/gh-plan.py','--repo','client/repo','claim','12')", True),
            ("import subprocess\ndef sh(**kw): subprocess.run(**kw)\nsh(args=['uv','run','/catalog/gh-plan.py','--repo','client/repo','claim','12'])", True),
            ("import subprocess\ndef sh(*args): subprocess.run(args)\nsh('echo'," + repr(writer) + ")", False),
            ("import subprocess\ndef sh(**kw): subprocess.run(**kw)\nsh(args=['python3','notify.py','--message'," + repr(writer) + "])", False),
            ("import subprocess\ndef sh(*args, **kw): subprocess.run(*args,**kw)\nargv=['uv','run','/catalog/gh-plan.py','--repo','client/repo','claim','12']\nsh(*[argv],**{'check':True})", True),
            ("def emit(c): print(c)\nemit(" + repr(writer) + ")", False),
            ("import subprocess\ndef unused(): subprocess.run(" + repr(writer) + ", shell=True)", False),
            ('import os; n=12; os.system(f"gh issue comment 5 --body \'next: uv run /catalog/gh-plan.py --repo r claim {n}\'")', False),
            ('import os; os.system(f"gh issue comment 5 --body \'next: uv run /catalog/gh-plan.py --repo r claim 12\'")', False),
            ('import subprocess; n=12; subprocess.run(["python3", "notify.py", "--message", f"run gh-plan.py --repo r claim {n}"])', False),
            ('import os, subprocess; n=os.environ["N"]; subprocess.run(["uv", "run", "python3", "notify.py", "--message", f"run gh-plan.py --repo r claim {n}"])', False),
        )
        programs += (
            ('import subprocess\ndef plan(*args): return ["uv","run","/catalog/gh-plan.py","--repo","client/repo",*args]\nsubprocess.run(plan(\'claim\',"12"))', True),
            ("import os\ncommand=lambda n: 'uv run /catalog/gh-plan.py --repo client/repo claim 12'\nos.system(command(12))", True),
            ("import os\nclass Job:\n    def command(self): return 'uv run /catalog/gh-plan.py --repo client/repo claim 12'\nos.system(Job().command())", True),
            ("import os\ndef commands():\n    yield 'uv run /catalog/gh-plan.py --repo client/repo claim 12'\nfor command in commands(): os.system(command)", True),
            ('import subprocess\ndef plan(*args): return ["uv","run","/catalog/gh-plan.py","--repo","client/repo",*args]\nsubprocess.run(plan(\'show\',"12"))', False),
            ("import os\ncommand=lambda n: 'echo done'\nos.system(command(12))", False),
            ("import os\nclass Job:\n    def command(self): return 'echo done'\nos.system(Job().command())", False),
            ("import os\ndef commands():\n    yield 'echo done'\nfor command in commands(): os.system(command)", False),
            ("import os,subprocess,threading,functools,atexit\nthreading.Thread(target=os.system,args=('uv run /catalog/gh-plan.py --repo client/repo claim 12',)).start()", True),
            ("import os,subprocess,functools\nfunctools.partial(os.system,'uv run /catalog/gh-plan.py --repo client/repo claim 12')()", True),
            ("import os,subprocess,atexit\natexit.register(os.system,'uv run /catalog/gh-plan.py --repo client/repo claim 12')", True),
            ("import os,subprocess\nfrom concurrent.futures import ThreadPoolExecutor\nThreadPoolExecutor().map(os.system,['uv run /catalog/gh-plan.py --repo client/repo claim 12'])", True),
            ("import os,subprocess,threading,functools,atexit\nthreading.Thread(target=subprocess.run,args=(['uv', 'run', '/catalog/gh-plan.py', '--repo', 'client/repo', 'claim', '12'],)).start()", True),
            ("import os,subprocess,functools\nfunctools.partial(subprocess.run,['uv', 'run', '/catalog/gh-plan.py', '--repo', 'client/repo', 'claim', '12'])()", True),
            ("import os,subprocess,atexit\natexit.register(subprocess.run,['uv', 'run', '/catalog/gh-plan.py', '--repo', 'client/repo', 'claim', '12'])", True),
            ("import os,subprocess\nfrom concurrent.futures import ThreadPoolExecutor\nThreadPoolExecutor().map(subprocess.run,[['uv', 'run', '/catalog/gh-plan.py', '--repo', 'client/repo', 'claim', '12']])", True),
            ("import os\ndef sh(c): os.system(c)\nlist(map(sh,['uv run /catalog/gh-plan.py --repo client/repo claim 12']))", True),
            ("import os\ndef sh(c): os.system(c)\nunused=map(sh,['uv run /catalog/gh-plan.py --repo client/repo claim 12'])", False),
            ("import os\nsorted(str(os.system(c)) for i,c in [(0,'uv run /catalog/gh-plan.py --repo client/repo claim 12')])", True),
            ("import os\nmin(str(os.system(c)) for i,c in [(0,'uv run /catalog/gh-plan.py --repo client/repo claim 12')])", True),
            ("import os\nmax(str(os.system(c)) for i,c in [(0,'uv run /catalog/gh-plan.py --repo client/repo claim 12')])", True),
            ("import os\ndict((str(i), os.system(c)) for i,c in [(0,'uv run /catalog/gh-plan.py --repo client/repo claim 12')])", True),
            ('import os\n"".join(str(os.system(c)) for i,c in [(0,\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')])', True),
            ("import click,os\n@click.group()\ndef cli(): pass\n@cli.command()\ndef claim(): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\ncli()", True),
            ("import click,os\n@click.group()\ndef cli(): pass\n@cli.command()\ndef claim(): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ("import argparse,os\ndef claim(args): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\np=argparse.ArgumentParser()\np.set_defaults(func=claim)\nargs=p.parse_args()\nargs.func(args)", True),
            ("import argparse,os\ndef claim(args): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\np=argparse.ArgumentParser()\np.set_defaults(func=claim)\nargs=p.parse_args()\nprint(args.func)", False),
            ("from dataclasses import dataclass\nimport os\n@dataclass\nclass Job:\n    cmd: str\n    def run(self): os.system(self.cmd)\nJob('uv run /catalog/gh-plan.py --repo client/repo claim 12').run()", True),
            ("from dataclasses import dataclass\nimport os\n@dataclass\nclass Job:\n    cmd: str\n    def run(self): os.system(self.cmd)\nJob('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ("import os\nclass Base:\n    def __init__(self,cmd): self.cmd=cmd\n    def run(self): os.system(self.cmd)\nclass Job(Base): pass\nJob('uv run /catalog/gh-plan.py --repo client/repo claim 12').run()", True),
            ('import os\nclass Job:\n    def __init__(self,cmd): self.cmd=cmd\n    def dry(self): self.cmd="echo "+self.cmd\n    def run(self): os.system(self.cmd)\njob=Job(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\nif dry: job.cmd="echo "+job.cmd\njob.run()', True),
            ('import os\nclass Job:\n    def __init__(self,cmd): self.cmd=cmd\n    def dry(self): self.cmd="echo "+self.cmd\n    def run(self): os.system(self.cmd)\njob=Job(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\nif dry: job.dry()\njob.run()', True),
            ('import os\nclass Job:\n    def __init__(self,cmd): self.cmd=cmd\n    def dry(self): self.cmd="echo "+self.cmd\n    def run(self): os.system(self.cmd)\njob=Job(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\nfor item in items: job.dry()\njob.run()', True),
            ('import os\nclass Job:\n    def __init__(self,cmd): self.cmd=cmd\n    def dry(self): self.cmd="echo "+self.cmd\n    def run(self): os.system(self.cmd)\njob=Job(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\ntry:\n    job.dry()\nexcept Exception: pass\njob.run()', True),
            ('import os\nclass Job:\n    def __init__(self,cmd): self.cmd=cmd\n    def dry(self): self.cmd="echo "+self.cmd\n    def run(self): os.system(self.cmd)\njob=Job(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\njob.dry()\njob.run()', False),
            ('import os\nclass Config: cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nif dry: Config.cmd="echo "+Config.cmd\nos.system(Config.cmd)', True),
            ('import os\nclass Config: cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nConfig.cmd="echo "+Config.cmd\nos.system(Config.cmd)', False),
            ("import os\ndef sh(c): os.system(c)\ncommands=map(sh,['uv run /catalog/gh-plan.py --repo client/repo claim 12'])\nlist(commands)", True),
            ('import os\nclass Base:\n    cmd="echo done"\n    @classmethod\n    def run(cls): os.system(cls.cmd)\nclass Job(Base): cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nJob.run()', True),
            ('import os\ndef main():\n    def unused(): yield "echo done"\n    os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\nmain()', True),
            ('import os\nclass Job:\n    cmd="echo done"\n    def configure(self): self.cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\n    def run(self): self.configure()\njob=Job()\njob.run()\njob.cmd="echo done"\njob.run()\nos.system(job.cmd)', True),
            ('import os\nclass Job:\n    cmd="echo done"\n    def configure(self): self.cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\njob=Job()\njob.cmd="echo done"\ndef configure(): job.configure()\nconfigure()\njob.cmd="echo done"\nconfigure()\nos.system(job.cmd)', True),
            ('import os\nclass Job:\n    cmd="echo done"\n    def configure(self): self.cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\njob=Job()\njob.cmd="echo done"\ndef configure(target): target.configure()\nconfigure(job)\njob.cmd="echo done"\nconfigure(job)\nos.system(job.cmd)', True),
            ("import os,sys\nclass Job:\n    def __init__(self,c): self.cmd=c\nfor n in map(int,sys.argv[1:]): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", True),
            ("import os,sys\nclass Job:\n    def __init__(self,c): self.cmd=c\nfor n in map(str.strip,sys.argv[1:]): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", True),
            ("import os,sys\nclass Job:\n    def __init__(self,c): self.cmd=c\nfor n in map(os.path.abspath,sys.argv[1:]): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", True),
            ("import os,sys\nclass Job:\n    def __init__(self,c): self.cmd=c\nfor n in map(Job,sys.argv[1:]): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", True),
            ("import os\nfor n in map(int,[]): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')", False),
            ('for n in map(int,["1"]): print(n)', False),
            ("import os\n[os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12') for n in map(int,ids)]", True),
            ('import os\ndef jobs():\n    os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\n    yield "item"\nfor i,c in enumerate(jobs()): pass', True),
            ('import os\ndef jobs():\n    os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\n    yield "item"\nfor c,d in zip(jobs(),[0]): pass', True),
            ('import os\ndef jobs():\n    os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\n    yield "item"\nfor c in tqdm(jobs()): pass', True),
            ('import os\ndef jobs():\n    os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\n    yield "item"\nfor c in itertools.chain(jobs()): pass', True),
            ('import os\ndef jobs():\n    os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\n    yield "item"\nprint(*jobs())', True),
            ('import os\ndef jobs():\n    os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\n    yield "item"\nitems=[*jobs()]', True),
            ('import os\ndef jobs():\n    os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\n    yield "item"\nprint(jobs())', False),
            ('import os\ndef jobs():\n    os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\n    yield "item"\nunused=tqdm(jobs())', False),
            ('import os\ndef jobs():\n    os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\n    yield "item"\nprint(tqdm(jobs()))', False),
            ('import os\ndef jobs():\n    os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\n    yield "item"\ninspect.signature(jobs())', False),
            ("import contextlib,os\n@contextlib.contextmanager\ndef claimed():\n    os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\n    yield\nwith claimed(): pass", True),
            ("import contextlib,os\nclass Job:\n    @contextlib.contextmanager\n    def claimed(self):\n        os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\n        yield\nwith Job().claimed(): pass", True),
            ("import os\ndef jobs(): yield 'uv run /catalog/gh-plan.py --repo client/repo claim 12'\nfor i,c in enumerate(jobs()): os.system(c)", True),
            ("import os\nos.system(' && '.join(['cd /repo', 'uv run /catalog/gh-plan.py --repo client/repo claim 12']))", True),
            ("import os\nos.system(' && '.join(['echo a', 'echo b']))", False),
            ("import os\nos.system('\\n'.join(['cd /repo', 'uv run /catalog/gh-plan.py --repo client/repo claim 12']))", True),
            ("import os\nos.system('\\n'.join(['echo a', 'echo b']))", False),
            ("import os\nos.system('; '.join(['cd /repo', 'uv run /catalog/gh-plan.py --repo client/repo claim 12']))", True),
            ("import os\nos.system('; '.join(['echo a', 'echo b']))", False),
            ("import os\nos.system(min(['uv run /catalog/gh-plan.py --repo client/repo claim 12']))", True),
            ('import os\nos.system(min(["echo a","echo b"]))', False),
            ("import os\nos.system(max(['uv run /catalog/gh-plan.py --repo client/repo claim 12']))", True),
            ('import os\nos.system(max(["echo a","echo b"]))', False),
            ('import os\ncommands=dict([("claim",\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')])\nos.system(commands["claim"])', True),
            ('import os\nclass Config:\n    cmd="echo done"\n    @classmethod\n    def arm(cls): cls.cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\ndef prepare(): Config.arm()\nprepare()\nConfig.cmd="echo done"\nprepare()\nos.system(Config.cmd)', True),
            ('import os\nclass Child:\n    def __init__(self): self.cmd="echo done"\n    def configure(self): self.cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\ndef configure(t): t.configure()\nclass Job:\n    def __init__(self):\n        self.child=Child()\n        self.parts=[self.child]\n    def run(self): self.child.configure()\njob=Job()\njob.run()\njob.child.cmd="echo done"\njob.run()\nos.system(job.child.cmd)', True),
            ('import os\nclass Child:\n    def __init__(self): self.cmd="echo done"\n    def configure(self): self.cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\ndef configure(t): t.configure()\nclass Job:\n    def __init__(self):\n        self.child=Child()\n        self.parts=[self.child]\n    def run(self): configure(self.child)\njob=Job()\njob.run()\njob.child.cmd="echo done"\njob.run()\nos.system(job.child.cmd)', True),
            ('import os\nclass Child:\n    def __init__(self): self.cmd="echo done"\n    def configure(self): self.cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\ndef configure(t): t.configure()\nclass Job:\n    def __init__(self):\n        self.child=Child()\n        self.parts=[self.child]\n    def run(self): \n        for p in self.parts: p.configure()\njob=Job()\njob.run()\njob.child.cmd="echo done"\njob.run()\nos.system(job.child.cmd)', True),
            ('import argparse,os\ndef claim(args): os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\np=argparse.ArgumentParser()\nsub=p.add_subparsers()\nc=sub.add_parser("claim")\nc.set_defaults(func=claim)\nargs=p.parse_args()\nargs.func(args)', True),
            ('import argparse,os\ndef claim(args): os.system(\'uv run /catalog/gh-plan.py --repo client/repo claim 12\')\np=argparse.ArgumentParser()\nsub=p.add_subparsers()\nc=sub.add_parser("claim")\nc.set_defaults(func=claim)\nargs=p.parse_args()\nprint(args.func)', False),
            ("import os\nclass Base:\n    def __init__(self,cmd): self.cmd=cmd\n    def run(self): os.system(self.cmd)\nclass Job(Base):\n    def __init__(self,cmd): super().__init__(cmd)\nJob('uv run /catalog/gh-plan.py --repo client/repo claim 12').run()", True),
            ("import click,os\n@click.group()\ndef cli(): pass\ndef claim(): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\ncli.add_command(claim)\ncli()", True),
            ("import click,os\n@click.group()\ndef cli(): pass\ndef claim(): os.system('uv run /catalog/gh-plan.py --repo client/repo claim 12')\ncli.add_command(claim)", False),
            ("from dataclasses import dataclass\nimport os\n@dataclass\nclass Job:\n    cmd: str\n    def __post_init__(self): os.system(self.cmd)\nJob('uv run /catalog/gh-plan.py --repo client/repo claim 12')", True),
            ('from dataclasses import dataclass\nimport os\n@dataclass\nclass Job: cmd: str\ndef make(): return Job("echo done")\nfirst=make()\nfirst.cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nsecond=make()\nos.system(second.cmd)', False),
            ('import os\nclass Job:\n    cmd="echo done"\n    def run(self): os.system(self.cmd)\ndef factory(): return Job()\na=factory()\na.cmd=\'uv run /catalog/gh-plan.py --repo client/repo claim 12\'\nb=factory()\nb.run()', False),
            ("import os\nclass Job:\n    def __init__(self,cmd): os.system(cmd)\nlist(map(Job,['uv run /catalog/gh-plan.py --repo client/repo claim 12']))", True),
            ("import os\nclass Job:\n    def __init__(self,cmd): os.system(cmd)\nunused=map(Job,['uv run /catalog/gh-plan.py --repo client/repo claim 12'])", False),
            ('import os\nclass Job:\n    def __init__(self,cmd): os.system(cmd)\nlist(map(Job,["echo done"]))', False),
        )
        for harness in ("codex", "claude"):
            for program, pending in programs:
                with self.subTest(harness=harness, program=program):
                    rows = history(harness, {})
                    command = "python3 -c " + shlex.quote(program)
                    if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)
                    self.assertEqual(receipts.executable_file_content("run.py", program), pending)


    def test_recursive_copy_keeps_unknown_descendant_bytes_uncertain(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        base = 'import os\ndef helper():\n    pass\n'
        for copier in ('cp -r', 'cp --recursive', 'cp -a', 'cp --archive'):
            with self.subTest(copier=copier):
                candidates, sources = set(), {}
                receipts.apply_file_effect({'kind': 'write', 'path': '/repo/run.py',
                    'contents': (base,), 'replaces': True}, None, candidates, sources)
                for effect in receipts.native_file_effects('exec_command',
                        {'cmd': copier + ' /elsewhere/tmpl/. /repo'}):
                    receipts.apply_file_effect(effect, '/repo', candidates, sources)
                updated = 'def helper():\n    os.system(' + repr(writer) + ')'
                receipts.apply_file_effect({'kind': 'write', 'path': '/repo/run.py',
                    'contents': (updated,), 'replaces': False,
                    'updates': (('def helper():\n    pass', updated, 1),)}, None, candidates, sources)
                self.assertIn('/repo/run.py', candidates)

    def test_generated_python_edits_compose_recorded_source_before_execution(self):
        writer = 'uv run /catalog/gh-plan.py --repo client/repo claim 12'
        base = 'import subprocess\ndef main():\n    subprocess.run(' + repr(writer) + ', shell=True)\n    return None\n'
        caller = 'if __name__ == "__main__":\n    main()\n'
        harmless = 'print("done")\n'

        def invocation(harness, rows, position, method, value):
            index = 1 + 2 * position
            if harness == "codex":
                rows[index]["payload"].update(name=method, arguments=json.dumps(value) if isinstance(value, dict) else value)
                rows[index + 1] = native_result("tool-" + str(position))
            else:
                rows[index]["message"]["content"][0].update(name=method, input=value)

        def write(harness, content, path="/repo/run.py"):
            if harness == "codex":
                return "apply_patch", "*** Begin Patch\n*** Add File: " + path + "\n" + "\n".join("+" + line for line in content.splitlines()) + "\n*** End Patch"
            return "Write", {"file_path": path, "content": content}

        def edit(mode, addition):
            if mode == "Edit":
                return mode, {"file_path": "/repo/run.py", "old_string": "    return None\n", "new_string": "    return None\n" + addition}
            if mode == "apply_patch":
                return mode, "*** Begin Patch\n*** Update File: /repo/run.py\n@@\n     return None\n" + "\n".join("+" + line for line in addition.splitlines()) + "\n*** End Patch"
            return "exec_command", {"cmd": "cat >> run.py <<'EOF'\n" + addition + "EOF"}

        for harness in ("codex", "claude"):
            shell = "exec_command" if harness == "codex" else "Bash"
            run = {"cmd" if harness == "codex" else "command": "python3 run.py"}
            for mode in ("Edit", "apply_patch"):
                for peer in (False, True):
                    for activate in (False, True):
                        with self.subTest(harness=harness, completed_partial=mode, peer_base=peer, activate=activate):
                            source = ('import subprocess\ndef helper():\n    subprocess.run(' + repr(writer)
                                      + ', shell=True)\ndef main():\n    print("plan")\n    return 0\nmain()\n')
                            rows = history(harness, {}, {}, {}, {})
                            invocation(harness, rows, 0, *write(harness, source))
                            changes = [('    print("plan")', '    print("plan v2")'),
                                       ('    return 0', '    helper()\n    return 0' if activate else '    print("done")\n    return 0')]
                            for position, (old, new) in enumerate(changes, 1):
                                value = ({"file_path": "/repo/run.py", "old_string": old, "new_string": new} if mode == "Edit" else
                                         '*** Begin Patch\n*** Update File: /repo/run.py\n@@\n'
                                         + '\n'.join('-' + line for line in old.splitlines()) + '\n'
                                         + '\n'.join('+' + line for line in new.splitlines()) + '\n*** End Patch')
                                invocation(harness, rows, position, mode, value)
                            invocation(harness, rows, 3, shell, run)
                            for index, row in enumerate(rows[1:9], 1):
                                row["timestamp"] = f"2026-01-01T00:00:{index:02d}Z"
                            if peer:
                                author, rows = rows[:3], [dict(rows[0]), *rows[3:]]
                                candidates = receipts.ownership_file_candidates([{"harness": harness, "records": value}
                                                                                for value in (author, rows)])
                            else:
                                candidates = ()
                            self.assertEqual(receipts.ownership_state(rows, harness, file_candidates=candidates)["pending"], activate)
                    with self.subTest(harness=harness, late_partial=mode, peer=peer):
                        source = ('import subprocess\ndef helper():\n    subprocess.run(' + repr(writer)
                                  + ', shell=True)\ndef main():\n    return 0\nmain()\n')
                        old, new = '    return 0', '    helper()\n    return 0'
                        value = ({"file_path": "/repo/run.py", "old_string": old, "new_string": new} if mode == "Edit" else
                                 '*** Begin Patch\n*** Update File: /repo/run.py\n@@\n-    return 0\n+    helper()\n+    return 0\n*** End Patch')
                        rows = history(harness, {}, {}, {}, {})[:9]
                        invocation(harness, rows, 0, *write(harness, source))
                        invocation(harness, rows, 1, mode, value)
                        invocation(harness, rows, 2, *write(harness, source))
                        invocation(harness, rows, 3, shell, run)
                        for index, second in enumerate((0, 0, 1, 3, 2, 2, 4, 4), 1):
                            rows[index]["timestamp"] = f"2026-01-01T00:00:{second:02d}Z"
                        completion = rows.pop(4)
                        rows.insert(6, completion)
                        if peer:
                            author, rows = [rows[0], *rows[1:4], completion], [dict(rows[0]), *rows[4:6], *rows[7:]]
                            candidates = receipts.ownership_file_candidates([{"harness": harness, "records": value}
                                                                            for value in (author, rows)])
                        else:
                            candidates = ()
                        self.assertTrue(receipts.ownership_state(rows, harness, file_candidates=candidates)["pending"])
            if harness == "codex":
                with self.subTest(duplicate_anchored_patch=True):
                    source = 'import subprocess\ndef preview():\n    print("plan")\n    return 0\ndef main():\n    print("plan")\n    return 0\nmain()\n'
                    patch_body = '*** Begin Patch\n*** Update File: /repo/run.py\n@@ def main():\n     print("plan")\n+    subprocess.run(' + repr(writer) + ', shell=True)\n     return 0\n*** End Patch'
                    rows = history(harness, {}, {}, {})
                    invocation(harness, rows, 0, *write(harness, source))
                    invocation(harness, rows, 1, "apply_patch", patch_body)
                    invocation(harness, rows, 2, shell, run)
                    self.assertTrue(receipts.ownership_state(rows, harness)["pending"])

            for mode in ("Edit", "apply_patch"):
                with self.subTest(harness=harness, uncertain_edit_followed_by_exact=mode):
                    source = 'import subprocess\ndef helper():\n    pass\ndef preview():\n    print("plan")\n    return 0\ndef main():\n    print("plan")\n    return 0\nmain()\n'
                    rows = history(harness, {}, {}, {}, {})
                    invocation(harness, rows, 0, *write(harness, source))
                    old = '    print("plan")\n    return 0'
                    new = '    print("plan")\n    helper()\n    return 0'
                    if mode == "Edit":
                        value = {"file_path": "/repo/run.py", "old_string": old, "new_string": new}
                    else:
                        value = '*** Begin Patch\n*** Update File: /repo/run.py\n@@ def main():\n     print("plan")\n+    helper()\n     return 0\n*** End Patch'
                    invocation(harness, rows, 1, mode, value)
                    invocation(harness, rows, 2, "Edit", {"file_path": "/repo/run.py",
                               "old_string": "def helper():\n    pass",
                               "new_string": "def helper():\n    subprocess.run(" + repr(writer) + ", shell=True)"})
                    invocation(harness, rows, 3, shell, run)
                    self.assertTrue(receipts.ownership_state(rows, harness)["pending"])

            dormant = 'import subprocess\ndef helper():\n    pass\n'
            launched = dormant + 'helper()\n'
            for mutation in ("cp tmpl.py run.py", "mv tmpl.py run.py", "git checkout -- run.py",
                             "cp /elsewhere/tmpl.py run.py", "mv /elsewhere/tmpl.py run.py",
                             "install /elsewhere/tmpl.py run.py", "cp -t /repo /elsewhere/run.py",
                             "cp --target-directory=/repo /elsewhere/run.py",
                             "cp -r /elsewhere/tmpl/. /repo",
                             "git checkout run.py", "git checkout HEAD run.py", "git -C /repo restore .",
                             "sed -i 's/x/y/' run.py", "perl -pi -e 's/x/y/' run.py",
                             "git checkout -- .", "git restore .", "git restore /repo/",
                             "git stash pop", "git stash branch other", "git reset --hard", "git reset --merge", "git reset --keep", "git switch other",
                             "git restore --pathspec-from-file=paths", "git --work-tree=/repo restore .",
                             "git pull", "git merge other", "git rebase other", "git cherry-pick abc",
                             "git apply x.patch", "patch -p1 < x.patch",
                             "git restore --source=HEAD -- run.py", "git restore -SW -- run.py",
                             "git restore --staged -W -- run.py", "git restore -S --worktree -- run.py",
                             "git restore --staged --worktree -- run.py"):
                with self.subTest(harness=harness, source_mutation=mutation):
                    rows = history(harness, {}, {}, {}, {}, {})
                    invocation(harness, rows, 0, *write(harness, dormant))
                    invocation(harness, rows, 1, *write(harness, launched, "/repo/tmpl.py"))
                    invocation(harness, rows, 2, shell, {"cmd" if harness == "codex" else "command": mutation})
                    invocation(harness, rows, 3, "Edit", {"file_path": "/repo/run.py", "old_string": "def helper():\n    pass",
                               "new_string": "def helper():\n    subprocess.run(" + repr(writer) + ", shell=True)"})
                    invocation(harness, rows, 4, shell, run)
                    self.assertTrue(receipts.ownership_state(rows, harness)["pending"])

            for reader in ("rg main run.py", "sed -n '1,20p' run.py", "cat run.py", "git diff -- run.py",
                           "uv run scripts/validate.py run.py", "uv run scripts/validate.py .",
                           "git -C /repo-other restore .", "git restore ../repo-other", "git merge --dry-run other",
                           "patch -d /repo-other -p1 < x.patch",
                           "git restore -S -- run.py", "git restore --staged -- run.py"):
                with self.subTest(harness=harness, source_reader=reader):
                    rows = history(harness, {}, {}, {}, {})
                    invocation(harness, rows, 0, *write(harness, base))
                    invocation(harness, rows, 1, shell, {"cmd" if harness == "codex" else "command": reader})
                    invocation(harness, rows, 2, "Edit", {"file_path": "/repo/run.py", "old_string": base,
                               "new_string": base + "# documentation\n"})
                    invocation(harness, rows, 3, shell, run)
                    self.assertFalse(receipts.ownership_state(rows, harness)["pending"])

            for mode in ("Edit", "apply_patch", "append"):
                for addition, replaced, expected in ((caller, False, True), ('message = "main()"\n', False, False),
                                                      (caller, True, False)):
                    with self.subTest(harness=harness, mode=mode, addition=addition, replaced=replaced):
                        rows = history(harness, *({} for _ in range(4 if replaced else 3)))
                        invocation(harness, rows, 0, *write(harness, base))
                        method, value = edit(mode, addition)
                        if harness == "claude" and method == "exec_command":
                            method, value = "Bash", {"command": value["cmd"]}
                        invocation(harness, rows, 1, method, value)
                        if replaced:
                            invocation(harness, rows, 2, *write(harness, harmless))
                        invocation(harness, rows, 3 if replaced else 2, shell, run)
                        self.assertEqual(receipts.ownership_state(rows, harness)["pending"], expected)

                # A peer's dormant definition must survive until a second
                # peer's partial edit and a third history's real script read.
                with self.subTest(harness=harness, mode=mode, peers=True):
                    author, editor, reader = (history(harness, {}) for _ in range(3))
                    invocation(harness, author, 0, *write(harness, base))
                    method, value = edit(mode, caller)
                    if harness == "claude" and method == "exec_command":
                        method, value = "Bash", {"command": value["cmd"]}
                    invocation(harness, editor, 0, method, value)
                    invocation(harness, reader, 0, shell, run)
                    for number, rows in enumerate((author, editor, reader), 1):
                        rows[1]["timestamp"] = rows[2]["timestamp"] = f"2026-01-01T00:00:0{number}Z"
                    nodes = [{"harness": harness, "records": rows} for rows in (author, editor, reader)]
                    candidates = receipts.ownership_file_candidates(nodes)
                    self.assertTrue(receipts.ownership_state(reader, harness, file_candidates=candidates)["pending"])

            for mode in ("Edit", "apply_patch"):
                with self.subTest(harness=harness, mode=mode, documentation_only=True):
                    rows = history(harness, {}, {}, {})
                    invocation(harness, rows, 0, *write(harness, base))
                    if mode == "Edit":
                        value = {"file_path": "/repo/run.py", "old_string": base, "new_string": base + "# documentation\n"}
                    else:
                        value = "*** Begin Patch\n*** Update File: /repo/run.py\n@@\n" + "\n".join(" " + line for line in base.splitlines()) + "\n+# documentation\n*** End Patch"
                    invocation(harness, rows, 1, mode, value)
                    invocation(harness, rows, 2, shell, run)
                    self.assertFalse(receipts.ownership_state(rows, harness)["pending"])

            for count in (10, 64):
                for addition, pending in ((caller, True), ("print('done')\n", False),
                                          ("import inspect\ninspect.signature(main)\n", False)):
                    with self.subTest(harness=harness, alternative_definitions=count, addition=addition):
                        rows = history(harness, *({} for _ in range(count + 2)))
                        for position in range(count):
                            content = base if position == 7 else "def main():\n    print(" + repr(str(position)) + ")\n"
                            command = "if test -f choice" + str(position) + "; then cat > run.py <<'EOF'\n" + content + "EOF\nfi"
                            invocation(harness, rows, position, shell, {"cmd" if harness == "codex" else "command": command})
                        command = "cat >> run.py <<'EOF'\n" + addition + "EOF"
                        invocation(harness, rows, count, shell, {"cmd" if harness == "codex" else "command": command})
                        invocation(harness, rows, count + 1, shell, run)
                        self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)

            for completion, pending in ((2, False), (5, True), (None, True)):
                with self.subTest(harness=harness, replacement_completion=completion):
                    author, cleanup, reader = history(harness, {}), history(harness, {}), history(harness, {}, {})
                    invocation(harness, author, 0, *write(harness, base))
                    invocation(harness, cleanup, 0, *write(harness, harmless))
                    command = "cat >> run.py <<'EOF'\n" + caller + "EOF"
                    invocation(harness, reader, 0, shell, {"cmd" if harness == "codex" else "command": command})
                    invocation(harness, reader, 1, shell, run)
                    author[1]["timestamp"] = author[2]["timestamp"] = "2026-01-01T00:00:01Z"
                    cleanup[1]["timestamp"] = "2026-01-01T00:00:02Z"
                    if completion is None:
                        cleanup.pop(2)
                    else:
                        cleanup[2]["timestamp"] = f"2026-01-01T00:00:0{completion}Z"
                    reader[1]["timestamp"] = reader[2]["timestamp"] = "2026-01-01T00:00:03Z"
                    reader[3]["timestamp"] = reader[4]["timestamp"] = "2026-01-01T00:00:04Z"
                    nodes = [{"harness": harness, "records": rows} for rows in (author, cleanup, reader)]
                    candidates = receipts.ownership_file_candidates(nodes)
                    self.assertEqual(receipts.ownership_state(reader, harness, file_candidates=candidates)["pending"], pending)

            with self.subTest(harness=harness, uncertain_invalid_base=True):
                rows = history(harness, {}, {}, {})
                command = "test -f choice && printf 'x = (' > run.py"
                invocation(harness, rows, 0, shell, {"cmd" if harness == "codex" else "command": command})
                addition = "import os; os.system(" + repr(writer) + ")\n"
                command = "cat >> run.py <<'EOF'\n" + addition + "EOF"
                invocation(harness, rows, 1, shell, {"cmd" if harness == "codex" else "command": command})
                invocation(harness, rows, 2, shell, run)
                self.assertTrue(receipts.ownership_state(rows, harness)["pending"])


    def test_property_accessors_preserve_descriptor_execution(self):
        writer = "uv run /catalog/gh-plan.py --repo client/repo claim 12"
        programs = [
            ("import os,functools\n" + prefix + "class C:\n" + body.replace("COMMAND", repr(command)) + "\n" + consumer.replace("COMMAND", repr(command)), owning)
            for prefix, body, consumer, executes in (
                ("", "    @property\n    def run(self): os.system(COMMAND)\n    @run.setter\n    def run(self,value): pass", "print(C().run)", True),
                ("", "    @property\n    def run(self): os.system(COMMAND)\n    @run.deleter\n    def run(self): pass", "print(C().run)", True),
                ("", "    @property\n    def run(self): return 'echo safe'\n    @run.getter\n    def run(self): os.system(COMMAND)", "print(C().run)", True),
                ("", "    @property\n    def run(self): os.system(COMMAND)\n    @run.setter\n    def run(self,value): pass", "c=C()\nc.run='unused'", False),
                ("", "    @property\n    def run(self): os.system(COMMAND)\n    @run.setter\n    def run(self,value): pass", "print(C.run)", False),
                ("", "    @property\n    def run(self): return None\n    @run.setter\n    def run(self,value): os.system(value)", "c=C()\nc.run=COMMAND", True),
                ("", "    @property\n    def run(self): return None\n    @run.deleter\n    def run(self): os.system(COMMAND)", "c=C()\ndel c.run", True),
                ("prop=property\n", "    @prop\n    def run(self): os.system(COMMAND)", "print(C().run)", True),
                ("import builtins\nprop=builtins.property\n", "    @prop\n    def run(self): os.system(COMMAND)", "print(C().run)", True),
                ("", "    @functools.cached_property\n    def run(self): os.system(COMMAND)", "print(C().run)", True),
                ("from functools import cached_property as prop\n", "    @prop\n    def run(self): os.system(COMMAND)", "print(C().run)", True),
                ("", "    def _run(self): os.system(COMMAND)\n    run=property(fget=_run)", "print(C().run)", True),
                ("prop=property\n", "    def _run(self): os.system(COMMAND)\n    run=prop(_run)", "print(C().run)", True),
                ("", "    def _get(self): return None\n    def _set(self,value): os.system(value)\n    run=property(fget=_get,fset=_set)", "c=C()\nc.run=COMMAND", True),
                ("", "    def _get(self): return None\n    def _del(self): os.system(COMMAND)\n    run=property(fget=_get,fdel=_del)", "c=C()\ndel c.run", True),
                ("", "    @functools.cached_property\n    def run(self): os.system(COMMAND)", "c=C()\nc.run='echo safe'\nprint(c.run)", False),
                ("", "    @property\n    def run(self): os.system(COMMAND)", "c=C()\ntry: c.run='echo safe'\nexcept AttributeError: pass\nprint(c.run)", True),
                ("", "    @property\n    def run(self): os.system(COMMAND)", "C.run.fget(C())", True),
                ("", "    @property\n    def run(self): return COMMAND\n    @run.setter\n    def run(self,value): os.system(value)", "c=C()\nc.run += ' '", True),
            )
            for command, is_writer in ((writer, True), ("echo safe", False))
            for owning in (executes and is_writer,)
        ]
        programs.extend([
            ("import os,functools\nclass C:\n    command='echo safe'\n    @functools.cached_property\n    def run(self): return self.command\nc=C()\nprint(c.run)\nc.command=" + repr(writer) + "\nos.system(c.run)", False),
            ("import os,functools\nclass C:\n    command='echo safe'\n    @functools.cached_property\n    def run(self): return self.command\nc=C()\nprint(c.run)\nc.command=" + repr(writer) + "\ndel c.run\nos.system(c.run)", True),
            ("import os\nclass C:\n    @property\n    def run(self): os.system(" + repr(writer) + ")\n    @run.getter\n    def run(self): return 'echo safe'\nprint(C().run)", False),
            ("import os\nclass C:\n    @property\n    def run(self): os.system(" + repr(writer) + ")\nc=C()\ntry: del c.run\nexcept AttributeError: pass", False),
            ("import os\nclass C:\n    @property\n    def run(self): return 'echo safe'\nif bool('yes'): C.run=None\nc=C()\nc.run=" + repr(writer) + "\nos.system(c.run)", True),
        ])
        for harness in ("codex", "claude"):
            for program, pending in programs:
                with self.subTest(harness=harness, program=program):
                    rows = history(harness, {})
                    command = "python3 -c " + shlex.quote(program)
                    if harness == "codex": rows[1]["payload"]["arguments"] = json.dumps({"cmd": command})
                    else: rows[1]["message"]["content"][0]["input"] = {"command": command}
                    self.assertEqual(receipts.ownership_state(rows, harness)["pending"], pending)
                    self.assertEqual(receipts.executable_file_content("run.py", program), pending)


if __name__ == "__main__":
    unittest.main()
