# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Native receipt fixtures exercise shutdown acceptance on both harnesses."""
import json
import asyncio
import argparse
import shlex
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
        for header in ("sudo -s", "sudo -i", "sudo --shell", "doas -s", "sudo su - deploy", "su - deploy"):
            commands.append((header + " <<'EOF'\n" + writer + "\nEOF", True))
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


if __name__ == "__main__":
    unittest.main()
